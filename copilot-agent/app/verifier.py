"""Follow-up statement verifier (ARCHITECTURE.md section 9).

Every statement the model returns for a turn passes through here before the
panel sees it. The checks prove attribution and numeric fidelity, not
semantic faithfulness (a known limit):

* ``fact``: must cite >= 1 record id that a tool returned in this turn, and
  every numeric token in the text must appear in a cited record's fields
  (value, units, range, dates, dose text, status).
* ``no_record_found``: must correspond to a tool call in this turn that
  returned no records; it may not cite records.
* ``clarification``: no citations required; still subject to the domain
  deny-lists.
* ``refusal``: rendered as one of two fixed sentences (scope or advice),
  never as model prose: a refusal that quotes the request ("I cannot advise
  on the dose") must not be lost to the recommendation deny-list, and a
  recommendation must not be smuggled through by labelling it a refusal.
* Domain constraints on every kind: no recommendation or directive language;
  absence is never negation ("no known allergies", "never", "not done", ...)
  unless the phrase is quoted from a cited record; "abnormal/high/low/normal"
  only when a cited result's abnormal flag says so; no cross-patient text.

Rejected statements are dropped and counted; their text is never returned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.contracts import ADVICE_REFUSAL_TEXT, SCOPE_REFUSAL_TEXT, Citation, RecordType, StatementKind, VerifiedStatement
from app.providers.base import ModelStatement, ModelTurnAnswer
from app.tools import ToolOutput

# Modal / directive phrasing that would turn a record lookup into advice.
_RECOMMENDATION = re.compile(
    r"\b(should|recommend|recommended|recommends|advise|advised|suggest|suggests|consider|needs? to|must|ought to|"
    r"increase the dose|decrease the dose|start(?:ing)? (?:the )?patient on|stop(?:ping)? (?:the )?patient|prescribe|titrate|switch to)\b",
    re.I,
)
# Absence-as-negation: only allowed when quoted from a cited record.
_NEGATION = re.compile(r"\b(no known allergies|nka|nkda|never|not done|not performed|not completed|no problems|non[- ]?compliant|non[- ]?adherent|did not take|is not taking|has not taken)\b", re.I)
# Interpretation words allowed only when a cited result's abnormal flag says so.
_INTERPRETATION = re.compile(r"\b(abnormal|high|low|elevated|normal|within (?:normal|reference) (?:range|limits)|out of range|critical)\b", re.I)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# Topics a refusal declines that make it the advice refusal rather than the scope refusal.
_ADVICE_TOPIC = re.compile(r"(advice|advis\w*|dos(?:e|es|ing|age)|treat\w*|diagnos\w*|interpret\w*|recommend\w*)", re.I)
_FLAG_WORDS = {"high": {"high", "yes"}, "elevated": {"high", "yes"}, "low": {"low", "yes"}, "abnormal": {"high", "low", "yes"}, "critical": {"high", "low", "yes"}, "normal": {"no"}, "within normal range": {"no"}, "within normal limits": {"no"}, "within reference range": {"no"}, "out of range": {"high", "low", "yes"}}


@dataclass
class TurnEvidence:
    """What the tools returned in this turn: record ids, their fields, and which calls came back empty."""

    outputs: list[ToolOutput]

    def records(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for o in self.outputs:
            for r in o.records:
                rid = r.get("record_id")
                if isinstance(rid, str):
                    out.setdefault(rid, r)
        return out

    def any_empty_call(self) -> bool:
        return any(not o.records and o.error is None for o in self.outputs) or any(
            len(o.records) == 1 and o.records[0].get("record_id") == "allergies:none" for o in self.outputs
        )


def _record_numbers(record: dict[str, Any]) -> set[str]:
    numbers: set[str] = set()
    for key in ("value", "units", "range", "date", "started_at", "ended_at", "modified_at", "dosage_text", "status_value", "begdate", "enddate", "drug_name", "test_name", "title", "plan_text", "summary", "source_span"):
        v = record.get(key)
        if isinstance(v, (str, int, float)):
            numbers.update(_normalize_number(n) for n in _NUMBER.findall(str(v)))
    return numbers


def _normalize_number(token: str) -> str:
    t = token.replace(",", ".")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t.lstrip("0") or "0"


def _record_text(record: dict[str, Any]) -> str:
    return " ".join(str(v) for v in record.values() if isinstance(v, (str, int, float))).lower()


def _citation_for(rid: str, record: dict[str, Any]) -> Citation:
    prefix = rid.split(":", 1)[0]
    rtype = {
        "procedure_result": RecordType.LAB_RESULT,
        "procedure_order": RecordType.LAB_ORDER,
        "prescriptions": RecordType.MEDICATION,
        "lists": RecordType.MEDICATION if "drug_name" in record else RecordType.ALLERGY,
        "form_soap": RecordType.PRIOR_NOTE,
    }.get(prefix, RecordType.PRIOR_NOTE)
    raw = record.get("date") or record.get("modified_at") or record.get("started_at") or record.get("begdate")
    try:
        ts = datetime.fromisoformat(str(raw)) if raw else datetime.fromtimestamp(0, tz=timezone.utc)
    except ValueError:
        ts = datetime.fromtimestamp(0, tz=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return Citation(record_type=rtype, record_id=rid, timestamp=ts)


def _domain_violation(text: str, cited: Iterable[dict[str, Any]]) -> str | None:
    cited = list(cited)
    if _RECOMMENDATION.search(text):
        return "recommendation_language"
    neg = _NEGATION.search(text)
    if neg and not any(neg.group(0).lower() in _record_text(r) for r in cited):
        return "absence_as_negation"
    for m in _INTERPRETATION.finditer(text):
        word = m.group(0).lower()
        allowed = _FLAG_WORDS.get(word)
        if allowed is None:
            continue
        flags = {str(r.get("abnormal_flag")).lower() for r in cited if r.get("abnormal_flag") is not None}
        if not (flags & allowed):
            return "interpretation_without_flag"
    return None


def _numbers_supported(text: str, cited: Iterable[dict[str, Any]]) -> bool:
    supported: set[str] = set()
    for r in cited:
        supported |= _record_numbers(r)
    return all(_normalize_number(n) in supported for n in _NUMBER.findall(text))


def canonical_refusal(text: str) -> str:
    """The fixed sentence a refusal is rendered with; the model's wording never reaches the panel."""
    if text in (SCOPE_REFUSAL_TEXT, ADVICE_REFUSAL_TEXT):
        return text
    return ADVICE_REFUSAL_TEXT if (_RECOMMENDATION.search(text) or _INTERPRETATION.search(text) or _ADVICE_TOPIC.search(text)) else SCOPE_REFUSAL_TEXT


def verify_statement(stmt: ModelStatement, evidence: TurnEvidence) -> tuple[VerifiedStatement | None, str | None]:
    """Return (verified statement, None) or (None, rejection code)."""
    records = evidence.records()
    text = " ".join(stmt.text.split())
    if not text:
        return None, "empty"
    if stmt.kind is StatementKind.FACT:
        if not stmt.citation_record_ids:
            return None, "citation_missing"
        unknown = [rid for rid in stmt.citation_record_ids if rid not in records or rid == "allergies:none"]
        if unknown:
            return None, "citation_unknown"
        cited = [records[rid] for rid in stmt.citation_record_ids]
        if not _numbers_supported(text, cited):
            return None, "numeric_unsupported"
    elif stmt.kind is StatementKind.NO_RECORD_FOUND:
        if not evidence.any_empty_call():
            return None, "absence_without_empty_search"
        if stmt.citation_record_ids:
            return None, "absence_with_citations"
        cited = []
    elif stmt.kind is StatementKind.REFUSAL:
        # Fixed message, chosen by what the model declined: advice/interpretation wording -> the advice sentence.
        return VerifiedStatement(text=canonical_refusal(text), kind=stmt.kind, citations=[]), None
    else:
        cited = [records[rid] for rid in stmt.citation_record_ids if rid in records]
    violation = _domain_violation(text, cited)
    if violation:
        return None, violation
    citations = [_citation_for(rid, records[rid]) for rid in stmt.citation_record_ids if rid in records and rid != "allergies:none"]
    return VerifiedStatement(text=text, kind=stmt.kind, citations=citations), None


def verify_turn(answer: ModelTurnAnswer, evidence: TurnEvidence) -> tuple[list[VerifiedStatement], int, list[str]]:
    """Verify every statement; return (kept, rejected count, rejection codes)."""
    kept: list[VerifiedStatement] = []
    codes: list[str] = []
    for stmt in answer.statements:
        verified, code = verify_statement(stmt, evidence)
        if verified is not None:
            kept.append(verified)
        else:
            codes.append(code or "rejected")
    return kept, len(codes), codes


# Shared with app.briefing. Exposed deliberately rather than copied: these encode
# the safety rules the Week 1 tests gate, and a second copy of a deny-list drifts
# from the gated one in a way no test would catch.
ADVICE_TOPIC_PATTERN = _ADVICE_TOPIC
NUMBER_PATTERN = _NUMBER
RECOMMENDATION_PATTERN = _RECOMMENDATION
normalize_number = _normalize_number

__all__ = [
    "ADVICE_TOPIC_PATTERN",
    "NUMBER_PATTERN",
    "RECOMMENDATION_PATTERN",
    "TurnEvidence",
    "canonical_refusal",
    "normalize_number",
    "verify_statement",
    "verify_turn",
]
