"""Bundle tools for follow-up turns (ARCHITECTURE.md section 8, "Tool inventory").

Every tool is a pure function over the stored bundle and the verified
briefing. None touches OpenEMR, the network or any other patient: the
bundle is single-patient by construction. Each call returns a bounded
``ToolOutput`` whose records carry their ``record_id`` - the only ids a
follow-up answer may cite. Failures return ``error`` rather than an empty
list, so "could not check" never looks like "nothing there".

Tool argument schemas are strict (``additionalProperties: false``); the model
cannot name a patient, a bundle or a tool outside this inventory.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import Field, ValidationError

from app.contracts import (
    ContextBundle,
    EvidenceMatch,
    EvidenceSource,
    MedicationRecord,
    PendingDocumentFact,
    StrictModel,
)
from app.medications import ingredient_key
from app.providers.prompt import PENDING_LABEL
from app.synonyms import normalize, resolve_commitment, resolve_record, resolve_record_panel

MAX_RECORDS = 10
MAX_TEXT_CHARS = 4000


class ToolOutput(StrictModel):
    """Bounded tool result: records (each with ``record_id``), truncation flag, or a fixed error code."""

    tool: str
    records: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None
    pending_count: int = Field(
        default=0,
        ge=0,
        description="find_results only: pending document values for the same test. They are not records here; an absence statement must account for them.",
    )

    def record_ids(self) -> set[str]:
        return {str(r["record_id"]) for r in self.records if "record_id" in r}


# --------------------------------------------------------------------------- #
# Argument schemas (strict)
# --------------------------------------------------------------------------- #


class NoArgs(StrictModel):
    pass


class FindResultsArgs(StrictModel):
    test_query: str = Field(min_length=1, max_length=80, description="Test name or panel as the physician would say it, e.g. 'HbA1c', 'lipid panel', 'potassium'.")
    limit: int | None = Field(default=None, ge=1, le=MAX_RECORDS, description="Max results to return, 1-10; null for the default of 10.")
    since: str | None = Field(default=None, description="ISO date (YYYY-MM-DD); only results on or after it. null for all.")


class FindOrdersArgs(StrictModel):
    test_query: str = Field(min_length=1, max_length=80, description="Test name or panel, e.g. 'lipid panel'.")
    since: str | None = Field(default=None, description="ISO date (YYYY-MM-DD); only orders on or after it. null for all.")


class FindMedicationsArgs(StrictModel):
    drug_query: str = Field(min_length=1, max_length=80, description="Drug name as written or its ingredient, e.g. 'metformin'.")
    include_inactive: bool | None = Field(default=None, description="true to include inactive records (the default when null), false for active only.")


class FindPendingArgs(StrictModel):
    test_query: str | None = Field(default=None, max_length=80, description="Test name or panel, e.g. 'HbA1c'; null for every pending value.")


PENDING_TOOL = "find_pending_document_facts"

TOOL_ARGS: dict[str, type[StrictModel]] = {
    "list_commitments": NoArgs,
    "find_results": FindResultsArgs,
    "find_orders": FindOrdersArgs,
    "find_medications": FindMedicationsArgs,
    "get_baseline_note": NoArgs,
    "list_allergies": NoArgs,
    PENDING_TOOL: FindPendingArgs,
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_commitments": "The verified plan commitments from the briefing, each with its evidence state, summary and cited record ids.",
    "find_results": "Lab/test results for this patient matching a test name or panel, newest first, with value, units, range, abnormal flag as recorded, status and date.",
    "find_orders": "Lab/test orders for this patient matching a test name or panel, newest first, with status and date.",
    "find_medications": "Medication records for this patient from both the prescriptions table and the medication list, with status as recorded and dates.",
    "get_baseline_note": "The baseline note's plan text (verbatim) with its record id and date.",
    "list_allergies": "Allergy entries as recorded, or an explicit statement that no entries are on file (which is not the same as no known allergies).",
    PENDING_TOOL: (
        "Values read from documents uploaded for this patient that a clinician has NOT yet verified or filed. "
        "Not chart records. Each has the document id, page, verification status, and conflicts_with: filed results "
        "for the same test and day with a different value."
    ),
}


# Keywords strict tool schemas do not accept at the provider; the same constraints are still
# enforced server-side by ``run_tool`` (Pydantic), so nothing is lost - a bad value is an error.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern", "format", "default", "title"})


def strict_schema(schema: Any) -> Any:
    """Make a Pydantic JSON schema acceptable as a strict tool schema (see _UNSUPPORTED_SCHEMA_KEYWORDS)."""
    if isinstance(schema, dict):
        cleaned = {k: strict_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYWORDS}
        if cleaned.get("type") == "object":
            cleaned["additionalProperties"] = False
            cleaned.setdefault("properties", {})
            cleaned["required"] = list(cleaned["properties"].keys())  # strict mode: every property listed; optional ones are nullable
        if "anyOf" in cleaned and isinstance(cleaned["anyOf"], list):
            cleaned["anyOf"] = [strict_schema(x) for x in cleaned["anyOf"]]
        return cleaned
    if isinstance(schema, list):
        return [strict_schema(x) for x in schema]
    return schema


def tool_definitions(*, include_pending: bool = False) -> list[dict[str, Any]]:
    """Provider-neutral tool definitions: name, description, strict JSON schema (no numeric/length bounds).

    ``find_pending_document_facts`` is offered only when the bundle holds
    pending facts, so a chart with no uploads sees exactly the Week 1 tools.
    """
    defs = []
    for name, model in TOOL_ARGS.items():
        if name == PENDING_TOOL and not include_pending:
            continue
        schema = strict_schema(model.model_json_schema())
        defs.append({"name": name, "description": TOOL_DESCRIPTIONS[name], "input_schema": schema})
    return defs


# --------------------------------------------------------------------------- #
# Implementations
# --------------------------------------------------------------------------- #


def _iso(dt: Any) -> str | None:
    return None if dt is None else dt.isoformat()


def _since_ok(dt: Any, since: str | None) -> bool:
    return since is None or dt.date().isoformat() >= since


def _test_keys(query: str) -> frozenset[str] | None:
    r = resolve_commitment(query)
    if r is not None:
        return r.keys
    norm = normalize(query)
    return frozenset({norm}) if norm else None


def _record_key(name: str, code: str | None) -> str:
    return resolve_record(name, code) or normalize(name)


def list_commitments(bundle: ContextBundle, matches: list[EvidenceMatch], _: NoArgs) -> ToolOutput:
    records = [
        {
            "record_id": m.commitment.commitment_id,
            "kind": m.commitment.kind.value,
            "source_span": m.commitment.source_span,
            "state": None if m.state is None else m.state.value,
            "summary": m.summary,
            "cited_record_ids": [c.record_id for c in m.citations],
        }
        for m in matches
    ]
    return ToolOutput(tool="list_commitments", records=records[:MAX_RECORDS], truncated=len(records) > MAX_RECORDS)


def find_results(bundle: ContextBundle, _: list[EvidenceMatch], args: FindResultsArgs) -> ToolOutput:
    if EvidenceSource.LAB_RESULTS in bundle.data_quality.sources_unavailable:
        return ToolOutput(tool="find_results", error="source_unavailable")
    keys = _test_keys(args.test_query)
    if keys is None:
        return ToolOutput(tool="find_results", error="unresolvable_query")
    rows = [r for r in bundle.lab_results if _record_key(r.test_name, r.code) in keys and _since_ok(r.observed_at, args.since)]
    rows.sort(key=lambda r: (r.observed_at, r.result_id), reverse=True)
    pending = sum(1 for f in bundle.pending_document_facts if _record_key(f.test_name, None) in keys)
    limit = args.limit or MAX_RECORDS
    records = [
        {
            "record_id": r.result_id,
            "test_name": r.test_name,
            "value": str(r.value),
            "units": r.units,
            "range": r.range,
            "abnormal_flag": None if r.abnormal_flag is None else r.abnormal_flag.value,
            "status": r.status.value,
            "date": _iso(r.observed_at),
            "order_id": r.order_id,
        }
        for r in rows[:limit]
    ]
    return ToolOutput(tool="find_results", records=records, truncated=len(rows) > limit, pending_count=pending)


def find_orders(bundle: ContextBundle, _: list[EvidenceMatch], args: FindOrdersArgs) -> ToolOutput:
    if EvidenceSource.LAB_ORDERS in bundle.data_quality.sources_unavailable:
        return ToolOutput(tool="find_orders", error="source_unavailable")
    keys = _test_keys(args.test_query)
    if keys is None:
        return ToolOutput(tool="find_orders", error="unresolvable_query")
    rows = [
        o
        for o in bundle.lab_orders
        if (_record_key(o.test_name, o.code) in keys or bool(resolve_record_panel(o.test_name) & keys)) and _since_ok(o.ordered_at, args.since)
    ]
    rows.sort(key=lambda o: (o.ordered_at, o.order_id, o.sequence), reverse=True)
    records = [
        {"record_id": o.record_id, "test_name": o.test_name, "status": o.status.value, "date": _iso(o.ordered_at), "order_id": o.order_id}
        for o in rows[:MAX_RECORDS]
    ]
    return ToolOutput(tool="find_orders", records=records, truncated=len(rows) > MAX_RECORDS)


def _med_row(m: MedicationRecord) -> dict[str, Any]:
    return {
        "record_id": m.record_id,
        "source_table": m.source_table.value,
        "drug_name": m.drug_name,
        "dosage_text": m.dosage_text,
        "active": m.active,
        "status_field": m.status_field,
        "status_value": m.status_value,
        "started_at": _iso(m.started_at),
        "ended_at": _iso(m.ended_at),
        "modified_at": _iso(m.modified_at),
    }


def find_medications(bundle: ContextBundle, _: list[EvidenceMatch], args: FindMedicationsArgs) -> ToolOutput:
    if EvidenceSource.MEDICATIONS in bundle.data_quality.sources_unavailable:
        return ToolOutput(tool="find_medications", error="source_unavailable")
    key = ingredient_key(args.drug_query)
    if not key:
        return ToolOutput(tool="find_medications", error="unresolvable_query")
    include_inactive = True if args.include_inactive is None else args.include_inactive
    rows = [m for m in bundle.medications if key in normalize(m.drug_name).split() and (include_inactive or m.active is True)]
    rows.sort(key=lambda m: (m.timestamp, m.record_id), reverse=True)
    return ToolOutput(tool="find_medications", records=[_med_row(m) for m in rows[:MAX_RECORDS]], truncated=len(rows) > MAX_RECORDS)


def get_baseline_note(bundle: ContextBundle, _: list[EvidenceMatch], __: NoArgs) -> ToolOutput:
    note = bundle.prior_note
    text = note.plan_text
    truncated = len(text) > MAX_TEXT_CHARS
    return ToolOutput(
        tool="get_baseline_note",
        records=[{"record_id": note.note_id, "encounter_id": note.encounter_id, "date": _iso(note.note_date), "plan_text": text[:MAX_TEXT_CHARS]}],
        truncated=truncated,
    )


def list_allergies(bundle: ContextBundle, _: list[EvidenceMatch], __: NoArgs) -> ToolOutput:
    if EvidenceSource.ALLERGIES in bundle.data_quality.sources_unavailable:
        return ToolOutput(tool="list_allergies", error="source_unavailable")
    records = [
        {
            "record_id": a.record_id,
            "title": a.title,
            "coded": a.coded,
            "code": a.code,
            "reaction": a.reaction,
            "severity": a.severity,
            "active": a.active,
            "begdate": _iso(a.begdate),
            "enddate": _iso(a.enddate),
        }
        for a in bundle.allergies
    ]
    if not records:
        records = [{"record_id": "allergies:none", "statement": "no allergy entries on file (not confirmed NKA)"}]
    return ToolOutput(tool="list_allergies", records=records[:MAX_RECORDS], truncated=len(records) > MAX_RECORDS)


# Printed flags in the Week 1 vocabulary the verifier checks interpretation words against.
_PRINTED_FLAG = {"H": "high", "HH": "high", "L": "low", "LL": "low", "A": "yes", "N": "no"}


def _same_value(printed: str | None, recorded: Decimal) -> bool:
    if printed is None:
        return False
    try:
        return Decimal(printed) == recorded
    except InvalidOperation:
        return False


def _conflicts(fact: PendingDocumentFact, bundle: ContextBundle) -> list[str]:
    """Filed results for the same test on the same day whose value differs from the pending one."""
    if fact.collection_date is None or fact.value_text is None:
        return []  # no day to compare on, or no value: not the same observation, so not a conflict
    key = _record_key(fact.test_name, None)
    return sorted(
        r.result_id
        for r in bundle.lab_results
        if _record_key(r.test_name, r.code) == key
        and r.observed_at.date() == fact.collection_date
        and not _same_value(fact.value_text, r.value)
    )


def find_pending_document_facts(bundle: ContextBundle, _: list[EvidenceMatch], args: FindPendingArgs) -> ToolOutput:
    """Pending document values, each labelled and located; never mixed into ``find_results``."""
    facts = bundle.pending_document_facts
    if args.test_query is not None and args.test_query.strip():
        keys = _test_keys(args.test_query)
        if keys is None:
            return ToolOutput(tool=PENDING_TOOL, error="unresolvable_query")
        facts = [f for f in facts if _record_key(f.test_name, None) in keys]
    records = [
        {
            "record_id": f.fact_id,
            "label": PENDING_LABEL,
            "source": "uploaded document",
            "document_id": f.document_id,
            "page": f.page,
            "test_name": f.test_name,
            "value": f.value_text,
            "units": f.unit,
            "range": f.reference_range,
            # Only a flag printed on the report counts as recorded; a derived comparison is not one.
            "abnormal_flag": _PRINTED_FLAG.get(f.abnormal_flag) if f.abnormal_flag and f.flag_source == "extracted" else None,
            "flag_source": f.flag_source,
            "date": None if f.collection_date is None else f.collection_date.isoformat(),
            "verification_status": f.verification_status,
            "conflicts_with": _conflicts(f, bundle),
        }
        for f in facts
    ]
    return ToolOutput(tool=PENDING_TOOL, records=records[:MAX_RECORDS], truncated=len(records) > MAX_RECORDS)


TOOL_IMPLEMENTATIONS: dict[str, Callable[[ContextBundle, list[EvidenceMatch], Any], ToolOutput]] = {
    "list_commitments": list_commitments,
    "find_results": find_results,
    "find_orders": find_orders,
    "find_medications": find_medications,
    "get_baseline_note": get_baseline_note,
    "list_allergies": list_allergies,
    PENDING_TOOL: find_pending_document_facts,
}


def run_tool(bundle: ContextBundle, matches: list[EvidenceMatch], name: str, raw_args: dict[str, Any]) -> ToolOutput:
    """Validate arguments strictly and run the tool; unknown tools and bad arguments are errors, never guesses."""
    model = TOOL_ARGS.get(name)
    impl = TOOL_IMPLEMENTATIONS.get(name)
    if model is None or impl is None:
        return ToolOutput(tool=name, error="unknown_tool")
    try:
        args = model.model_validate(raw_args)
    except ValidationError:
        return ToolOutput(tool=name, error="invalid_arguments")
    try:
        return impl(bundle, matches, args)
    except Exception:  # noqa: BLE001 - a tool failure must surface as an error, not a crash or an empty list
        return ToolOutput(tool=name, error="tool_failed")


def serialize_output(output: ToolOutput) -> str:
    """Compact JSON for the model; ``record_id`` values are the only citable ids."""
    data = output.model_dump(mode="json")
    if not data["pending_count"]:
        del data["pending_count"]  # unchanged output for every chart with no pending values
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "MAX_RECORDS",
    "PENDING_LABEL",
    "PENDING_TOOL",
    "TOOL_ARGS",
    "ToolOutput",
    "run_tool",
    "serialize_output",
    "strict_schema",
    "tool_definitions",
]
