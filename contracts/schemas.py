"""Executable version of CONTRACTS.md — the single source of truth.

Every service validates its input/output against these pydantic models.
SearchOutput uses extra="forbid": if SearchService ever leaks an
embedding, similarity score, or match decision into its payload, this is a
runtime ValidationError, not a code-review catch (CONTRACTS.md §2).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Canonical Status Enum (CONTRACTS.md, used identically everywhere)
# ---------------------------------------------------------------------------
class CanonicalStatus(str, Enum):
    NO_FACE_DETECTED = "NO_FACE_DETECTED"
    MULTIPLE_FACES_DETECTED = "MULTIPLE_FACES_DETECTED"
    LOW_IMAGE_QUALITY = "LOW_IMAGE_QUALITY"
    SEARCH_SUCCESS_MATCH_VERIFIED = "SEARCH_SUCCESS_MATCH_VERIFIED"
    SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH = "SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH"
    NO_SEARCH_RESULTS = "NO_SEARCH_RESULTS"
    SEARCH_API_FAILURE = "SEARCH_API_FAILURE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    BLOCKCHAIN_FAILURE = "BLOCKCHAIN_FAILURE"
    BLOCKCHAIN_CONFIRMED = "BLOCKCHAIN_CONFIRMED"


class VisionStatus(str, Enum):
    OK = "OK"
    NO_FACE_DETECTED = "NO_FACE_DETECTED"
    MULTIPLE_FACES_DETECTED = "MULTIPLE_FACES_DETECTED"
    LOW_IMAGE_QUALITY = "LOW_IMAGE_QUALITY"


class SourceType(str, Enum):
    WEB = "web"
    SOCIAL = "social"


class Zone(str, Enum):
    HIGH = "HIGH"
    UNCERTAIN = "UNCERTAIN"
    LOW = "LOW"


class VerificationDecision(str, Enum):
    CANDIDATE_MATCH = "candidate_match"
    UNCERTAIN = "uncertain"
    NO_MATCH = "no_match"


# Canonical event-log stages (CONTRACTS.md §5)
class EventStage(str, Enum):
    FACE_DETECTED = "face_detected"
    QUERY_SENT = "query_sent"
    CANDIDATES_RETURNED = "candidates_returned"
    CANDIDATE_SELECTED = "candidate_selected"
    VERIFICATION_RUN = "verification_run"
    VERIFICATION_RESULT = "verification_result"
    RECORD_BUILT = "record_built"
    BLOCKCHAIN_TX_SUBMITTED = "blockchain_tx_submitted"
    BLOCKCHAIN_CONFIRMED = "blockchain_confirmed"
    REVERIFICATION_RUN = "reverification_run"

# ---------------------------------------------------------------------------
# §2 SearchService Output — STRICT. Retrieval only; scoring is forbidden here.
# ---------------------------------------------------------------------------
class SearchCandidate(BaseModel):
    candidate_id: str
    candidate_url: str
    source_type: SourceType
    thumbnail_url: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class SearchOutput(BaseModel):
    candidates: list[SearchCandidate] = Field(default_factory=list)
    status: CanonicalStatus

    model_config = ConfigDict(extra="forbid")


def assert_no_scoring_fields(payload: dict[str, Any]) -> None:
    """Runtime guard: reject any payload implying SearchService scored a match.

    CONTRACTS.md §2: embedding / similarity_score / confidence / match_decision
    are forbidden in the SearchService payload. Called by SearchService before
    constructing SearchOutput; a violation raises immediately.
    """
    forbidden = ("embedding", "similarity_score", "confidence", "match_decision")
    present = [k for k in payload if k.lower() in forbidden]
    if present:
        raise ValueError(
            f"CONTRACTS.md §2 violation: SearchService payload contains forbidden "
            f"field(s) {present} — scoring/retrieval independence breached"
        )



# ---------------------------------------------------------------------------
# §1 VisionService Output
# ---------------------------------------------------------------------------
class VisionOutput(BaseModel):
    face_id: str
    embedding: Optional[list[float]] = None
    bbox: Optional[list[int]] = None
    quality_score: float = 0.0
    status: VisionStatus

    model_config = ConfigDict(extra="forbid")

# ---------------------------------------------------------------------------
# §3 VerificationService Input/Output
# ---------------------------------------------------------------------------
class VerificationInput(BaseModel):
    """What VerificationService receives: candidate coordinates + the query
    embedding held separately by the backend — never routed via SearchService."""
    candidate_id: str
    candidate_url: str
    thumbnail_url: Optional[str] = None
    query_embedding: list[float] = Field(min_length=512, max_length=512)

    model_config = ConfigDict(extra="forbid")


class VerificationOutput(BaseModel):
    candidate_id: str
    independent_similarity_score: float
    zone: Zone
    decision: VerificationDecision
    reason: str

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# §4 Canonical Record — BlockchainService input (minimized, no biometric data)
# ---------------------------------------------------------------------------
class CanonicalRecord(BaseModel):
    record_version: str = "1.0"
    record_id: str
    content_hash: str
    content_cid: Optional[str] = None
    source_reference_hash: str
    verification_result: VerificationDecision
    verification_timestamp: str
    pipeline_version: str

    model_config = ConfigDict(extra="forbid")


class OnChainRecord(BaseModel):
    """What actually gets anchored on-chain — provenance fields only."""
    record_id: str
    content_hash: str
    content_cid: Optional[str]
    source_reference_hash: str
    verification_result: str
    verification_timestamp: str
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    confirmed: bool = False

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# §5 Event Log (Data Lineage)
# ---------------------------------------------------------------------------
class PipelineEvent(BaseModel):
    job_id: str
    stage: EventStage
    timestamp: str
    status: str
    detail: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# §6 API Surface — request/response bodies for the FastAPI backend
# ---------------------------------------------------------------------------
class PipelineStartResponse(BaseModel):
    job_id: str

    model_config = ConfigDict(extra="forbid")


class PipelineStatusResponse(BaseModel):
    job_id: str
    # Optional: null until the pipeline emits its first event
    # (CONTRACTS.md amendment log, 2026-09-01) — a fake stage/status is
    # worse than an explicit null.
    stage: Optional[EventStage] = None
    status: Optional[CanonicalStatus] = None

    model_config = ConfigDict(extra="forbid")


class PipelineResultResponse(BaseModel):
    job_id: str
    status: CanonicalStatus
    verification: Optional[VerificationOutput] = None
    on_chain_record: Optional[OnChainRecord] = None
    polygonscan_url: Optional[str] = None
    events: list[PipelineEvent] = Field(default_factory=list)
    error_detail: Optional[str] = None

    model_config = ConfigDict(extra="forbid")
