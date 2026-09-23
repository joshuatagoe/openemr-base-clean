"""The guideline corpus: committed content plus a deterministic build step (feature PRD F07, ADR-006).

Nothing here is fetched at runtime. The source text lives in
``fixtures/corpus/`` under version control, ``sources.json`` carries the
per-document provenance, and :func:`build_corpus` turns the pair into indexed
passages. Identical inputs always produce an identical ``corpus_version``, so a
retrieval difference between two runs is attributable to the corpus rather than
mistaken for a model regression (F07 §3.3).

Three rules shape everything below.

**Provenance is a build-time obligation, not a display convention.** A source
missing publisher, publication year (or retrieval date), population scope,
evidence tier or licence notice raises :class:`CorpusBuildError`. A passage
without provenance is never indexed (F07 §5), because a citation that cannot be
resolved back to a named document and section is worse than no citation.

**Population scope is provenance.** A guideline can be authoritative, current,
freely licensed and still written about someone other than this patient
(ADR-006 §8.2). It sits beside publisher and year in every citation so the
physician can judge applicability rather than assume it.

**Tier admissibility is enforced, not advisory** (F07 §3.7). A Tier B passage -
patient-education grade - may support a *care-process* statement: how often
something is typically done, what a test measures. It may never support a
*clinical threshold, target or decision boundary*. :func:`supports` decides
this and :meth:`Corpus.screen_claims` applies it, dropping and counting the
claims that fail. The rule exists because mixing a patient-education page into
a guideline corpus creates a new failure surface: a consumer web page read as
clinical authority. CDC's "tested at least twice a year" is admissible; CDC's
"the A1C goal is 7% or less", on the same page, is inadmissible twice over and
is recorded in ``sources.json`` as an excluded passage the build refuses to
index.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import ConfigDict, Field

from app.contracts import StrictModel

CORPUS_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "corpus"
SPEC_FILENAME = "sources.json"

#: Bumped whenever the chunking algorithm itself changes shape, so a corpus
#: built by an older build step is never mistaken for one built by this code.
BUILDER_VERSION = "v1"

#: Metadata a source must carry before any of its passages may be indexed.
REQUIRED_SOURCE_FIELDS = (
    "source_id",
    "title",
    "publisher",
    "population_scope",
    "evidence_tier",
    "document_kind",
    "source_url",
    "licence_basis",
    "licence_notice",
    "text_file",
)

_HEADING_MAX_CHARS = 100
_BULLET = "•"
_TERMINAL = (".", ":", "?", "!", '"', ")")
# Trailing reference superscripts ("...treatment strategies.12", "...resources5")
# are stripped before asking whether a line finished its sentence.
_TRAILING_REFS = re.compile(r"[\d,;\s–—-]+$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class CorpusBuildError(RuntimeError):
    """Raised when the corpus cannot be built with full provenance.

    The build fails loudly rather than indexing a passage whose document,
    section, tier or population scope cannot be named (F07 §5).
    """


class EvidenceTier(StrEnum):
    """How authoritative the document is, as a closed set.

    ``A`` is clinical guidance; ``B`` is patient education. The distinction is
    load-bearing - see :func:`supports`.
    """

    A = "A"
    B = "B"

    @property
    def label(self) -> str:
        return f"Tier {self.value}"


class ClaimKind(StrEnum):
    """What a claim asserts, which decides which tiers may support it."""

    #: How often something is typically done, or what a test measures.
    CARE_PROCESS = "care_process"
    #: A clinical threshold, target or decision boundary.
    THRESHOLD = "threshold"


class DropReason(StrEnum):
    """Why a claim was withheld before display. Both are counted, never silent."""

    TIER_INADMISSIBLE = "tier_inadmissible"
    UNRESOLVABLE_CITATION = "unresolvable_citation"


def supports(tier: EvidenceTier, claim_kind: ClaimKind) -> bool:
    """Whether a passage at ``tier`` may support a claim of ``claim_kind``.

    This is the whole of F07 §3.7 in one expression: patient-education grade
    evidence describes what usually happens, never where a clinical boundary
    lies.
    """
    if claim_kind is ClaimKind.THRESHOLD:
        return tier is EvidenceTier.A
    return True


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class FrozenModel(StrictModel):
    """Corpus values are immutable once built."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class ExcludedPassage(FrozenModel):
    """A passage the build refuses to index, with the reason on the record.

    Recording an exclusion keeps it deliberate and reviewable rather than an
    oversight someone later reintroduces (F07 §9.2).
    """

    text: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SourceDocument(FrozenModel):
    """One document's provenance. Every field here reaches the physician."""

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    publication_year: int | None = Field(default=None, ge=1900, le=2100)
    retrieved: str | None = Field(default=None, min_length=1, description="ISO date, for undated sources.")
    population_scope: str = Field(min_length=1, description="Who the document was written about.")
    evidence_tier: EvidenceTier
    document_kind: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_sha256: str | None = None
    licence_basis: str = Field(min_length=1)
    licence_notice: str = Field(min_length=1, description="Quoted verbatim from the document itself.")
    text_file: str = Field(min_length=1)
    text_provenance: str = Field(min_length=1)
    text_sha256: str = Field(min_length=64, max_length=64)
    excluded_passages: tuple[ExcludedPassage, ...] = ()


class CorpusChunk(FrozenModel):
    """One retrievable passage, tagged with everything needed to cite it."""

    chunk_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    section_label: str = Field(min_length=1, description='Top-level section, e.g. "Principle 7: ...".')
    section_heading: str = Field(min_length=1, description="Sub-heading the passage sits under.")
    passage_kind: str = Field(min_length=1, description='"recommendation_bullet" or "narrative".')
    ordinal: int = Field(ge=1)
    text: str = Field(min_length=1)


class Citation(FrozenModel):
    """What a physician sees beside a consideration (F07 §3.4, AC-2)."""

    chunk_id: str
    source_id: str
    document_title: str
    publisher: str
    publication_year: int | None
    retrieved: str | None
    population_scope: str
    evidence_tier: EvidenceTier
    document_kind: str
    section_label: str
    section_heading: str
    passage_kind: str
    quote: str
    source_url: str
    corpus_version: str


class SupportedClaim(FrozenModel):
    """A candidate statement plus the passages offered as its support."""

    claim_id: str = Field(min_length=1)
    claim_kind: ClaimKind
    supporting_chunk_ids: tuple[str, ...] = ()


class ScreenedClaim(FrozenModel):
    """A claim that survived screening, carrying only admissible support."""

    claim_id: str
    claim_kind: ClaimKind
    citations: tuple[Citation, ...]


class DroppedClaim(FrozenModel):
    """A claim withheld before display, with the reason stated."""

    claim_id: str
    claim_kind: ClaimKind
    reason: DropReason
    detail: str


class ScreeningResult(FrozenModel):
    """The outcome of applying F07 §3.7. Drops are counted, never silent."""

    kept: tuple[ScreenedClaim, ...] = ()
    dropped: tuple[DroppedClaim, ...] = ()

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


# --------------------------------------------------------------------------- #
# The corpus
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, eq=False)
class Corpus:
    """Indexed passages plus the manifest that makes them inspectable."""

    corpus_id: str
    corpus_version: str
    disclaimer: str
    chunking: dict[str, Any]
    sources: tuple[SourceDocument, ...]
    chunks: tuple[CorpusChunk, ...]
    _by_chunk_id: dict[str, CorpusChunk] = field(repr=False, default_factory=dict)
    _by_source_id: dict[str, SourceDocument] = field(repr=False, default_factory=dict)

    # -- lookup ------------------------------------------------------------- #

    def chunk(self, chunk_id: str) -> CorpusChunk | None:
        """The passage, or ``None`` when the identifier no longer resolves."""
        return self._by_chunk_id.get(chunk_id)

    def chunks_for(self, source_id: str) -> tuple[CorpusChunk, ...]:
        return tuple(chunk for chunk in self.chunks if chunk.source_id == source_id)

    def source(self, source_id: str) -> SourceDocument:
        return self._by_source_id[source_id]

    def citation_for(self, chunk_id: str) -> Citation:
        """Resolve a passage to a full citation.

        Raises ``KeyError`` when the chunk is unknown: a claim whose support
        cannot be resolved is withheld, never shown uncited (F07 §5).
        """
        chunk = self._by_chunk_id[chunk_id]
        source = self._by_source_id[chunk.source_id]
        return Citation(
            chunk_id=chunk.chunk_id,
            source_id=source.source_id,
            document_title=source.title,
            publisher=source.publisher,
            publication_year=source.publication_year,
            retrieved=source.retrieved,
            population_scope=source.population_scope,
            evidence_tier=source.evidence_tier,
            document_kind=source.document_kind,
            section_label=chunk.section_label,
            section_heading=chunk.section_heading,
            passage_kind=chunk.passage_kind,
            quote=chunk.text,
            source_url=source.source_url,
            corpus_version=self.corpus_version,
        )

    # -- tier admissibility (F07 §3.7) -------------------------------------- #

    def screen_claims(self, claims: Iterable[SupportedClaim]) -> ScreeningResult:
        """Apply the tier rule to candidate claims before anything is displayed.

        A claim survives only if at least one of its supporting passages both
        resolves and is admissible for its kind. Inadmissible support is
        stripped from a surviving claim, so a Tier B passage never rides along
        as apparent backing for a threshold.
        """
        kept: list[ScreenedClaim] = []
        dropped: list[DroppedClaim] = []

        for claim in claims:
            if not claim.supporting_chunk_ids:
                dropped.append(
                    DroppedClaim(
                        claim_id=claim.claim_id,
                        claim_kind=claim.claim_kind,
                        reason=DropReason.UNRESOLVABLE_CITATION,
                        detail="claim offered no supporting passage",
                    )
                )
                continue

            resolved: list[CorpusChunk] = []
            missing: list[str] = []
            for chunk_id in claim.supporting_chunk_ids:
                found = self.chunk(chunk_id)
                if found is None:
                    missing.append(chunk_id)
                else:
                    resolved.append(found)

            if missing:
                dropped.append(
                    DroppedClaim(
                        claim_id=claim.claim_id,
                        claim_kind=claim.claim_kind,
                        reason=DropReason.UNRESOLVABLE_CITATION,
                        detail=f"unresolvable chunk id(s): {', '.join(missing)}",
                    )
                )
                continue

            admissible = [
                chunk
                for chunk in resolved
                if supports(self._by_source_id[chunk.source_id].evidence_tier, claim.claim_kind)
            ]
            if not admissible:
                tiers = sorted({self._by_source_id[c.source_id].evidence_tier.label for c in resolved})
                dropped.append(
                    DroppedClaim(
                        claim_id=claim.claim_id,
                        claim_kind=claim.claim_kind,
                        reason=DropReason.TIER_INADMISSIBLE,
                        detail=(
                            f"a {claim.claim_kind.value} claim cannot rest on "
                            f"{'/'.join(tiers)} evidence alone (F07 §3.7)"
                        ),
                    )
                )
                continue

            kept.append(
                ScreenedClaim(
                    claim_id=claim.claim_id,
                    claim_kind=claim.claim_kind,
                    citations=tuple(self.citation_for(chunk.chunk_id) for chunk in admissible),
                )
            )

        return ScreeningResult(kept=tuple(kept), dropped=tuple(dropped))

    # -- manifest (F07 §3.5, AC-1) ------------------------------------------ #

    def manifest(self) -> dict[str, Any]:
        """A JSON-serialisable record of what is in the corpus and on what basis."""
        return {
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "builder_version": BUILDER_VERSION,
            "disclaimer": self.disclaimer,
            "built_from": "committed fixtures under fixtures/corpus; nothing is fetched at runtime",
            "chunking": dict(self.chunking),
            "chunk_count": len(self.chunks),
            "sources": [self._manifest_entry(source) for source in self.sources],
        }

    def _manifest_entry(self, source: SourceDocument) -> dict[str, Any]:
        chunks = self.chunks_for(source.source_id)
        section_structure: list[str] = []
        for chunk in chunks:
            if chunk.section_label not in section_structure:
                section_structure.append(chunk.section_label)
        return {
            "source_id": source.source_id,
            "title": source.title,
            "publisher": source.publisher,
            "publication_year": source.publication_year,
            "retrieved": source.retrieved,
            "population_scope": source.population_scope,
            "evidence_tier": source.evidence_tier.value,
            "evidence_tier_label": source.evidence_tier.label,
            "document_kind": source.document_kind,
            "section_structure": section_structure,
            "licence_basis": source.licence_basis,
            "licence_notice": source.licence_notice,
            "source_url": source.source_url,
            "source_sha256": source.source_sha256,
            "text_file": source.text_file,
            "text_sha256": source.text_sha256,
            "text_provenance": source.text_provenance,
            "chunk_count": len(chunks),
            "excluded_passages": [
                {"text": item.text, "reason": item.reason} for item in source.excluded_passages
            ],
        }


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_corpus(root: Path | str | None = None) -> Corpus:
    """Build the corpus from committed fixtures.

    ``root`` defaults to :data:`CORPUS_FIXTURES`; tests pass a copy so they can
    perturb an input and observe the version change.
    """
    root = Path(root) if root is not None else CORPUS_FIXTURES
    spec = _load_spec(root)

    corpus_id = _require(spec, "corpus_id", "corpus spec")
    disclaimer = _require(spec, "disclaimer", "corpus spec")
    chunking = _chunking_params(spec)

    sources: list[SourceDocument] = []
    chunks: list[CorpusChunk] = []
    for raw in spec.get("sources") or []:
        source, source_chunks = _build_source(root, raw, chunking)
        sources.append(source)
        chunks.extend(source_chunks)

    if not sources:
        raise CorpusBuildError(f"{root / SPEC_FILENAME} lists no sources")

    _reject_excluded_passages(sources, chunks)
    _reject_duplicate_chunk_ids(chunks)

    corpus_version = _corpus_version(corpus_id, chunking, sources, chunks)
    return Corpus(
        corpus_id=corpus_id,
        corpus_version=corpus_version,
        disclaimer=disclaimer,
        chunking=chunking,
        sources=tuple(sources),
        chunks=tuple(chunks),
        _by_chunk_id={chunk.chunk_id: chunk for chunk in chunks},
        _by_source_id={source.source_id: source for source in sources},
    )


def _load_spec(root: Path) -> dict[str, Any]:
    spec_path = root / SPEC_FILENAME
    if not spec_path.is_file():
        raise CorpusBuildError(f"corpus spec not found: {spec_path}")
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - guards a corrupt commit
        raise CorpusBuildError(f"corpus spec is not valid JSON: {spec_path}: {exc}") from exc
    if not isinstance(spec, dict):
        raise CorpusBuildError(f"corpus spec must be a JSON object: {spec_path}")
    return spec


def _require(spec: dict[str, Any], key: str, what: str) -> Any:
    value = spec.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise CorpusBuildError(f"{what} is missing required field '{key}'")
    return value


def _chunking_params(spec: dict[str, Any]) -> dict[str, Any]:
    raw = spec.get("chunking") or {}
    try:
        params = {
            "chunker_version": str(_require(raw, "chunker_version", "chunking parameters")),
            "min_chunk_chars": int(_require(raw, "min_chunk_chars", "chunking parameters")),
            "max_chunk_chars": int(_require(raw, "max_chunk_chars", "chunking parameters")),
        }
    except (TypeError, ValueError) as exc:
        raise CorpusBuildError(f"chunking parameters are not numeric: {exc}") from exc
    if params["min_chunk_chars"] < 1 or params["max_chunk_chars"] <= params["min_chunk_chars"]:
        raise CorpusBuildError("chunking parameters must satisfy 0 < min_chunk_chars < max_chunk_chars")
    return params


def _build_source(
    root: Path, raw: dict[str, Any], chunking: dict[str, Any]
) -> tuple[SourceDocument, list[CorpusChunk]]:
    if not isinstance(raw, dict):
        raise CorpusBuildError("each entry of 'sources' must be a JSON object")

    source_id = raw.get("source_id") or "<unnamed source>"
    for required in REQUIRED_SOURCE_FIELDS:
        value = raw.get(required)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise CorpusBuildError(
                f"source '{source_id}' is missing required metadata field '{required}'; "
                "a passage without provenance is never indexed (F07 §5)"
            )

    if raw.get("publication_year") is None and not raw.get("retrieved"):
        raise CorpusBuildError(
            f"source '{source_id}' must carry a 'publication_year' or, for undated content, a 'retrieved' date"
        )

    tier_value = raw["evidence_tier"]
    try:
        tier = EvidenceTier(tier_value)
    except ValueError as exc:
        allowed = ", ".join(t.value for t in EvidenceTier)
        raise CorpusBuildError(
            f"source '{source_id}' has unknown evidence_tier {tier_value!r}; allowed: {allowed}"
        ) from exc

    text_path = root / str(raw["text_file"])
    if not text_path.is_file():
        raise CorpusBuildError(f"source '{source_id}' text file not found: {text_path}")
    text = _normalise_text(text_path.read_text(encoding="utf-8"))

    excluded = tuple(
        ExcludedPassage(text=item.get("text", ""), reason=item.get("reason", ""))
        for item in raw.get("excluded_passages") or []
    )

    source = SourceDocument(
        source_id=str(raw["source_id"]),
        title=str(raw["title"]),
        publisher=str(raw["publisher"]),
        publication_year=raw.get("publication_year"),
        retrieved=raw.get("retrieved"),
        population_scope=str(raw["population_scope"]),
        evidence_tier=tier,
        document_kind=str(raw["document_kind"]),
        source_url=str(raw["source_url"]),
        source_sha256=raw.get("source_sha256"),
        licence_basis=str(raw["licence_basis"]),
        licence_notice=str(raw["licence_notice"]),
        text_file=str(raw["text_file"]),
        text_provenance=str(raw.get("text_provenance") or "committed plain text"),
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        excluded_passages=excluded,
    )

    blocks = _segment(text, raw, source.title)
    chunks = _chunks_from_blocks(source.source_id, blocks, chunking)
    if not chunks:
        raise CorpusBuildError(f"source '{source.source_id}' produced no passages from {text_path}")
    return source, chunks


def _normalise_text(raw: str) -> str:
    """LF line endings, no trailing whitespace: the hash must not track checkout style."""
    unified = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]
    return "\n".join(lines).strip("\n") + "\n"


# --------------------------------------------------------------------------- #
# Segmentation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Block:
    section_label: str
    section_heading: str
    passage_kind: str
    text: str


def _is_complete(text: str) -> bool:
    """Did this line finish its sentence, ignoring a trailing reference marker?"""
    return _TRAILING_REFS.sub("", text).endswith(_TERMINAL)


def _is_heading(line: str) -> bool:
    if len(line) > _HEADING_MAX_CHARS:
        return False
    if not line[:1].isupper() and not line[:1].isdigit():
        return False
    return not _TRAILING_REFS.sub("", line).endswith(_TERMINAL)


def _split_bullets(line: str) -> list[tuple[bool, str]]:
    """A single extracted line can pack several bullets; each becomes its own part."""
    head, _, rest = line.partition(_BULLET)
    parts: list[tuple[bool, str]] = []
    if head.strip():
        parts.append((False, head.strip()))
    if _BULLET in line:
        for piece in rest.split(_BULLET):
            if piece.strip():
                parts.append((True, piece.strip()))
    return parts


def _segment(text: str, raw: dict[str, Any], title: str) -> list[_Block]:
    """Turn a document's plain text into headed blocks.

    The extracted text is one paragraph per line, with running headers and bare
    page numbers interleaved and bullets wrapping across lines - including
    across page breaks. Dropping the furniture first and then joining a line to
    its predecessor whenever it is a continuation reconstructs the paragraphs
    deterministically.
    """
    drop_patterns = [re.compile(p) for p in raw.get("drop_line_patterns") or []]
    section_marker = re.compile(raw["section_marker"]) if raw.get("section_marker") else None
    label_template = raw.get("section_label_template") or "{title}"
    skip_until = re.compile(raw["skip_until"]) if raw.get("skip_until") else None
    skip_headings = {str(h) for h in raw.get("skip_headings") or []}
    # Back matter (references, further-reading lists) sits at the end of each
    # section and contains headings of its own - bibliography entries wrap in a
    # way that looks like a sub-heading. Once a skip heading is seen, skipping
    # therefore runs to the end of the section rather than to the next heading.
    skip_to_end_of_section = bool(raw.get("skip_to_end_of_section"))

    section_label = str(raw.get("default_section_label") or title)
    heading = section_label
    started = skip_until is None
    awaiting_title: str | None = None
    skipping = False

    blocks: list[_Block] = []
    open_kind: str | None = None
    open_parts: list[str] = []

    def flush() -> None:
        nonlocal open_kind, open_parts
        if open_kind is not None and open_parts:
            blocks.append(
                _Block(
                    section_label=section_label,
                    section_heading=heading,
                    passage_kind=open_kind,
                    text=" ".join(" ".join(open_parts).split()),
                )
            )
        open_kind, open_parts = None, []

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in drop_patterns):
            continue
        if not started:
            if skip_until is not None and skip_until.match(line):
                started = True
            else:
                continue

        if section_marker is not None:
            match = section_marker.match(line)
            if match:
                flush()
                awaiting_title = match.group(1) if match.groups() else ""
                skipping = False
                continue

        if awaiting_title is not None:
            flush()
            section_label = label_template.format(awaiting_title, title=line)
            heading = line
            awaiting_title = None
            continue

        if skipping and skip_to_end_of_section:
            continue

        for is_bullet, part in _split_bullets(line):
            if is_bullet:
                flush()
                if not skipping:
                    open_kind, open_parts = "recommendation_bullet", [part]
                continue

            continuing = open_kind is not None and (part[:1].islower() or not _is_complete(" ".join(open_parts)))
            if continuing:
                open_parts.append(part)
                continue

            if _is_heading(part):
                flush()
                heading = part
                skipping = part in skip_headings
                continue

            flush()
            if not skipping:
                open_kind, open_parts = "narrative", [part]

    flush()
    return blocks


def _merge_blocks(blocks: Sequence[_Block], max_chars: int) -> list[_Block]:
    """Join neighbouring blocks that share a heading *and* a passage kind.

    Sub-section granularity is what F07 §9.5 plans for (roughly 80-120
    passages), and it is also the granularity a physician can act on: the four
    bullets under "Glycemic treatment goals" are one answer to "which target
    applies", not four competing ones.

    Kind stays pure across a merge. A narrative sentence must never end up
    inside a passage cited as a recommendation (F07 §11).
    """
    merged: list[_Block] = []
    for block in blocks:
        if merged:
            previous = merged[-1]
            joinable = (
                previous.section_label == block.section_label
                and previous.section_heading == block.section_heading
                and previous.passage_kind == block.passage_kind
                and len(previous.text) + 1 + len(block.text) <= max_chars
            )
            if joinable:
                merged[-1] = _Block(
                    section_label=previous.section_label,
                    section_heading=previous.section_heading,
                    passage_kind=previous.passage_kind,
                    text=f"{previous.text} {block.text}",
                )
                continue
        merged.append(block)
    return merged


def _chunks_from_blocks(
    source_id: str, blocks: Sequence[_Block], chunking: dict[str, Any]
) -> list[CorpusChunk]:
    min_chars = chunking["min_chunk_chars"]
    max_chars = chunking["max_chunk_chars"]

    chunks: list[CorpusChunk] = []
    per_section: dict[str, int] = {}
    for block in _merge_blocks(blocks, max_chars):
        for text in _split_to_max(block.text, max_chars):
            if len(text) < min_chars:
                continue
            slug = _slug(block.section_label)
            per_section[slug] = per_section.get(slug, 0) + 1
            chunks.append(
                CorpusChunk(
                    chunk_id=f"{source_id}::{slug}::{per_section[slug]:03d}",
                    source_id=source_id,
                    section_label=block.section_label,
                    section_heading=block.section_heading,
                    passage_kind=block.passage_kind,
                    ordinal=len(chunks) + 1,
                    text=text,
                )
            )
    return chunks


def _split_to_max(text: str, max_chars: int) -> list[str]:
    """Split an over-long block on sentence boundaries, greedily and deterministically."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(text):
        candidate = f"{current} {sentence}".strip() if current else sentence
        if current and len(candidate) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _slug(value: str) -> str:
    return _SLUG_STRIP.sub("-", value.lower()).strip("-") or "section"


# --------------------------------------------------------------------------- #
# Build-time guards
# --------------------------------------------------------------------------- #


def _reject_excluded_passages(sources: Sequence[SourceDocument], chunks: Sequence[CorpusChunk]) -> None:
    """No passage recorded as inadmissible may reach the index.

    The exclusions in ``sources.json`` are enforced here rather than trusted to
    stay out by hand, so re-transcribing a source cannot quietly reintroduce
    one (F07 §9.2).
    """
    for source in sources:
        for excluded in source.excluded_passages:
            needle = " ".join(excluded.text.split())
            for chunk in chunks:
                if needle and needle in chunk.text:
                    raise CorpusBuildError(
                        f"chunk '{chunk.chunk_id}' contains an excluded passage of "
                        f"source '{source.source_id}': {excluded.text!r} - {excluded.reason}"
                    )


def _reject_duplicate_chunk_ids(chunks: Sequence[CorpusChunk]) -> None:
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.chunk_id in seen:
            raise CorpusBuildError(f"duplicate chunk id '{chunk.chunk_id}'")
        seen.add(chunk.chunk_id)


# --------------------------------------------------------------------------- #
# Versioning (F07 §3.3, AC-4, AC-5)
# --------------------------------------------------------------------------- #


def _corpus_version(
    corpus_id: str,
    chunking: dict[str, Any],
    sources: Sequence[SourceDocument],
    chunks: Sequence[CorpusChunk],
) -> str:
    """A hash over content *and* chunking, so either kind of change is visible.

    Deliberately not a hash of the built object's ``repr``: it must not move
    when a comment, a field order or the JSON formatting of the spec changes,
    and it must move when a single word of a source does.
    """
    payload = {
        "builder": BUILDER_VERSION,
        "corpus_id": corpus_id,
        "chunking": chunking,
        "sources": [
            {
                "source_id": source.source_id,
                "title": source.title,
                "publisher": source.publisher,
                "publication_year": source.publication_year,
                "retrieved": source.retrieved,
                "population_scope": source.population_scope,
                "evidence_tier": source.evidence_tier.value,
                "licence_notice": source.licence_notice,
                "source_url": source.source_url,
                "source_sha256": source.source_sha256,
                "text_sha256": source.text_sha256,
            }
            for source in sources
        ],
        "chunks": [[chunk.chunk_id, chunk.text] for chunk in chunks],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{BUILDER_VERSION}.{digest[:16]}"
