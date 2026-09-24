"""Week 2 document briefing: one uploaded lab document in, one grounded briefing out.

This module is the wiring. Every stage already exists and is tested on its own;
this is the first place they are called in sequence from a real request:

    PHP module (signed POST)            <- the document lives in OpenEMR's own
      -> extract_lab_document              ``documents`` table (PRD CR1, D1)
      -> HybridRetriever                 sparse + dense + RRF   (F08)
      -> Reranker                        fake or Bedrock        (F09, ADR-002)
      -> answer model                    proposes considerations from the top
                                         evidence only          (CR3)
      -> build_briefing                  screens every claim    (F11)
      -> DocumentBriefingResponse

The HTTP contract (request and response below) is what the PHP module and the
panel build against. Do not widen it without changing both sides.

The answer model PROPOSES; build_briefing DISPOSES. A consideration the model
invents without a resolvable chunk, with a number absent from its sources, with
a directive, or resting a threshold on Tier B evidence is dropped before
display and counted. That is the Week 1 verification spine, extended to
guideline claims.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.briefing import Briefing
from app.contracts import StrictModel
from app.evidence import RetrievalStatus

SUPPORTED_MEDIA_TYPES = ("application/pdf", "image/png", "image/jpeg")


# --------------------------------------------------------------------------- #
# HTTP contract - PHP module -> agent
# --------------------------------------------------------------------------- #


class DocumentBriefingRequest(StrictModel):
    """Signed body of ``POST /v1/documents/briefing``.

    Signed with the same HMAC scheme as ``POST /v1/bundles``
    (``X-Copilot-Signature`` + ``X-Copilot-Timestamp`` over the raw body), so
    the module reuses ``BundleSigner`` and ``GuzzleAgentClient`` unchanged.

    ``patient_uuid`` never ``pid`` - the same rule as the Week 1 bundle.
    ``document_id`` is OpenEMR's ``documents.id``; it becomes every citation's
    ``source_id``, so click-to-source can resolve back to the stored file.
    """

    correlation_id: UUID
    patient_uuid: UUID
    document_id: int = Field(ge=1)
    media_type: Literal["application/pdf", "image/png", "image/jpeg"]
    document_base64: str = Field(min_length=1, description="The stored file's bytes. Never logged.")
    question: str | None = Field(
        default=None,
        max_length=500,
        description="Optional physician question. A request for advice or dosing earns the fixed refusal.",
    )


# --------------------------------------------------------------------------- #
# HTTP contract - agent -> PHP module -> panel
# --------------------------------------------------------------------------- #


class BriefingStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class Provenance(StrictModel):
    """What produced this briefing - shown in the panel, not hidden in logs.

    ``reranker`` is displayed so that nothing claims more than it does: a
    deterministic lexical fallback must never read as a learned reranker.
    """

    extraction_model: str
    answer_model: str
    reranker: str
    corpus_version: str
    evidence_status: RetrievalStatus


class DocumentBriefingResponse(StrictModel):
    """Everything the panel renders.

    ``status="degraded"`` still returns 200 with whatever could be produced and
    a fixed ``degraded_reason`` - the same contract as the Week 1 briefing, so
    a failure is stated rather than surfacing as a broken panel.
    """

    correlation_id: UUID
    patient_uuid: UUID
    document_id: int
    status: BriefingStatus = BriefingStatus.OK
    degraded_reason: str | None = Field(
        default=None, description="Fixed string. Never a raw exception, never patient-specific."
    )
    briefing: Briefing | None = None
    rendered_text: str = ""
    provenance: Provenance | None = None


__all__ = [
    "SUPPORTED_MEDIA_TYPES",
    "BriefingStatus",
    "DocumentBriefingRequest",
    "DocumentBriefingResponse",
    "Provenance",
]
