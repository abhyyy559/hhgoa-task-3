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
# Canonical Status Enum — split by layer (CONTRACTS.md v3)
# Only the owning layer may emit its values.
# Legacy v2 values kept as aliases during migration; new code must use v3.
# ---------------------------------------------------------------------------
class CanonicalStatus(str, Enum):
    # Vision-level
    NO_FACE_DETECTED = "NO_FACE_DETECTED"
    MULTIPLE_FACES_DETECTED = "MULTIPLE_FACES_DETECTED"
    LOW_IMAGE_QUALITY = "LOW_IMAGE_QUALITY"
    # Search-level — SearchService may ONLY emit these
    SEARCH_RESULTS_FOUND = "SEARCH_RESULTS_FOUND"
    NO_SEARCH_RESULTS = "NO_SEARCH_RESULTS"
    SEARCH_API_FAILURE = "SEARCH_API_FAILURE"
    # Pipeline-level — only backend may emit, only after verification ran
    PIPELINE_MATCH_VERIFIED = "PIPELINE_MATCH_VERIFIED"
    PIPELINE_NO_CONFIDENT_MATCH = "PIPELINE_NO_CONFIDENT_MATCH"
    PIPELINE_ERROR = "PIPELINE_ERROR"
    # Blockchain-level
    BLOCKCHAIN_FAILURE = "BLOCKCHAIN_FAILURE"
    BLOCKCHAIN_CONFIRMED = "BLOCKCHAIN_CONFIRMED"
    # Legacy v2 aliases (deprecated — do not emit from new code)
    SEARCH_SUCCESS_MATCH_VERIFIED = "SEARCH_SUCCESS_MATCH_VERIFIED"
    SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH = "SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


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


# Canonical event-log stages (CONTRACTS.md §5 v3 — includes candidate_retry)
class EventStage(str, Enum):
    FACE_DETECTED = "face_detected"
    QUERY_SENT = "query_sent"
    CANDIDATES_RETURNED = "candidates_returned"
    CANDIDATE_SELECTED = "candidate_selected"
    VERIFICATION_RUN = "verification_run"
    VERIFICATION_RESULT = "verification_result"
    CANDIDATE_RETRY = "candidate_retry"
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
    is_social_domain: bool = False
    found_via: Optional[str] = None
    # Retrieval provenance tier (tagging only, never a score):
    # full_match | partial_match | page | visually_similar | null (non-Vision)
    match_type: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class SearchOutput(BaseModel):
    candidates: list[SearchCandidate] = Field(default_factory=list)
    status: CanonicalStatus
    # Google's web-entity guesses (e.g. celebrity names + scores) — retrieval
    # metadata from the provider, never our verification judgment.
    web_entities: list[dict[str, Any]] = Field(default_factory=list)

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
    is_social_domain: bool = False
    query_embedding: list[float] = Field(min_length=512, max_length=512)
    # dHash of the query photo (lets verification tell same-photo reposts
    # apart from same-face-different-photo matches). None = skip the check.
    query_phash: Optional[int] = None

    model_config = ConfigDict(extra="forbid")


class VerificationOutput(BaseModel):
    candidate_id: str
    independent_similarity_score: float
    zone: Zone
    decision: VerificationDecision
    reason: str
    faces_checked_in_candidate: int = 1
    # True = the scored image is (near-)identical bytes to the query photo
    # (repost), as opposed to a different photo of the same face. The
    # similarity itself is always face-only; this flag says what KIND of
    # evidence it is.
    same_photo: bool = False
    extracted_metadata: Optional[dict[str, Any]] = None

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
    query_embedding_hash: Optional[str] = None
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


class CandidateLineupEntry(BaseModel):
    """One evaluated candidate for the digital-presence lineup (v3)."""

    candidate_id: str
    candidate_url: str
    source_type: str = "web"
    is_social_domain: bool = False
    found_via: Optional[str] = None
    match_type: Optional[str] = None
    thumbnail_url: Optional[str] = None
    decision: str = "no_match"
    zone: str = "LOW"
    similarity: float = 0.0
    faces_checked: int = 0
    same_photo: bool = False
    profile_username: Optional[str] = None
    handles: list[str] = Field(default_factory=list)
    title: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ResolvedIdentity(BaseModel):
    """Best-effort identity resolution from retrieval evidence (v3).

    NOT a verification verdict — a ranking of who the web evidence points
    to, with the signals listed so a judge can audit it. ``kind`` is
    public_figure when one entity dominates corroborations, else
    low_presence (common person: resolve from face-verified hits only).
    ``confidence`` is high/medium/low and always paired with ``signals``.
    An official handle is NEVER claimed from a blue-check (invisible
    unauthenticated) — only name-match + profile-shape + corroboration.
    """

    name: Optional[str] = None
    kind: str = "low_presence"
    handle: Optional[str] = None
    profile_url: Optional[str] = None
    confidence: str = "low"
    # Officiality vocabulary (fixed set — never free text):
    # officially_linked = name-matching profile + face-verified hit + 2+
    #   independent corroborations; strongly_supported = name-matching
    #   profile + corroboration but short of that bar; unverified =
    #   candidate exists without establishing support; unknown = default.
    # platform_verified is NEVER emitted: blue-checks are invisible without
    # platform login, and claiming one would be fabrication.
    officiality: str = "unknown"
    signals: list[str] = Field(default_factory=list)
    alternates: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class PipelineResultResponse(BaseModel):
    job_id: str
    status: CanonicalStatus
    verification: Optional[VerificationOutput] = None
    on_chain_record: Optional[OnChainRecord] = None
    polygonscan_url: Optional[str] = None
    source_url: Optional[str] = None
    candidate_lineup: list[CandidateLineupEntry] = Field(default_factory=list)
    web_entities: list[dict[str, Any]] = Field(default_factory=list)
    resolved_identity: Optional[ResolvedIdentity] = None
    # Honest per-stage timings (ms), derived from the §5 event log — never
    # estimated. Keys present only for stages the run actually reached.
    latency_ms: dict[str, float] = Field(default_factory=dict)
    events: list[PipelineEvent] = Field(default_factory=list)
    error_detail: Optional[str] = None

    model_config = ConfigDict(extra="forbid")
