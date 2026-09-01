"""FastAPI backend — pipeline manager + data-lineage event log (CONTRACTS.md §6).

Endpoints
---------
- ``POST /api/pipeline/start`` → ``{job_id}`` — accepts an image upload and
    starts the pipeline in a background thread. The pipeline takes several
    seconds (face model + live search + possible chain confirmation), so a
    blocking request would be a bug, not a simplification.
- ``GET /api/pipeline/{job_id}/status`` → ``{stage, status}`` (null until the
    pipeline emits its first event — CONTRACTS.md amendment 2026-09-01).
- ``GET /api/pipeline/{job_id}/result`` → full structured result incl. the
    verification chain, on-chain record, Polygonscan link and the event log.
- ``GET /api/pipeline/{job_id}/events`` → the raw §5 event log (debugging +
    the UI's future live feed).

Canonical status enum — one vocabulary everywhere (§6): every service's
internal vocabulary is normalized here at the boundary. Every unhappy path
(no face, no search results, verification reject, chain write failure) is a
real, typed, visible terminal state — never a silent success. The one mapping
decision worth stating: a server-side vision failure (model pack missing) is
surfaced as ``NO_FACE_DETECTED`` + ``error_detail`` carrying the real
exception, because the honest statement is "the query image could not be
processed" — a fake happy path is never an option.

Blockchain anchoring runs only for ``candidate_match`` results; the chain
write never blocks an HTTP request (background job waits for the receipt and
records submit + confirm in the event log).
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile

from contracts.schemas import (
    CanonicalRecord,
    CanonicalStatus,
    EventStage,
    OnChainRecord,
    PipelineEvent,
    PipelineResultResponse,
    PipelineStartResponse,
    PipelineStatusResponse,
    SearchOutput,
    VerificationDecision,
    VerificationInput,
    VerificationOutput,
)
from services import blockchain as blockchain_service
from services import search as search_service
from services import verification as verification_service
from services import vision as vision_service

app = FastAPI(
    title="Face Identification & Blockchain Verification",
    version=blockchain_service.PIPELINE_VERSION,
    description=(
        "Pipeline: face detection → live web/social search (retrieval only) → "
        "independent verification → integrity anchoring on Polygon Amoy."
    ),
)

# Verification decision → backend-owned canonical status (§6: the backend owns
# the verdict vocabulary; SearchService never emits a verified-match status).
_DECISION_TO_STATUS: dict[VerificationDecision, CanonicalStatus] = {
    VerificationDecision.CANDIDATE_MATCH: CanonicalStatus.SEARCH_SUCCESS_MATCH_VERIFIED,
    VerificationDecision.UNCERTAIN: CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH,
    VerificationDecision.NO_MATCH: CanonicalStatus.VERIFICATION_FAILED,
}

POLYGONSCAN_TX_URL = "https://amoy.polygonscan.com/tx/{tx_hash}"


class JobState:
    """In-memory job record: current stage/status + the full §5 event log."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.stage: Optional[EventStage] = None
        self.status: Optional[CanonicalStatus] = None
        self.events: list[PipelineEvent] = []
        self.verification: Optional[VerificationOutput] = None
        self.canonical_record: Optional[CanonicalRecord] = None
        self.on_chain_record: Optional[OnChainRecord] = None
        self.polygonscan_url: Optional[str] = None
        self.error_detail: Optional[str] = None
        self.done = False
        self._lock = threading.Lock()

    def emit(
        self,
        stage: EventStage,
        status: Any,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append one §5 event; job stage/status track the latest emission.

        ``status`` may be a CanonicalStatus (job status follows it) or a
        verification decision value (permitted by §5) — the job's canonical
        status then simply stays at the last canonical one.
        """
        status_value = (
            status.value if isinstance(status, CanonicalStatus) else str(status)
        )
        event = PipelineEvent(
            job_id=self.job_id,
            stage=stage,
            timestamp=datetime.now(timezone.utc).isoformat(),
            status=status.value if isinstance(status, CanonicalStatus) else str(status),
            detail=detail or {},
        )
        with self._lock:
            self.events.append(event)
            self.stage = stage
            if isinstance(status, CanonicalStatus):
                self.status = status


_JOBS: dict[str, JobState] = {}
_JOBS_LOCK = threading.Lock()


def _set_error(job: JobState, message: str) -> None:
    with job._lock:
        job.error_detail = message


def _fail_job(job: JobState, stage: EventStage, status: CanonicalStatus, message: str) -> None:
    """Record a real, typed, visible failure state — the contract's core rule."""
    job.emit(stage, CanonicalStatus(status), {"error": message})
    _set_error(job, message)


def _run_pipeline(job_id: str, image_bytes: bytes, image_bgr: np.ndarray) -> None:
    job = _JOBS[job_id]
    try:
        _pipeline_body(job, image_bytes, image_bgr)
    except Exception as exc:  # noqa: BLE001 — a crashed job must still be visible
        job.emit(
            job.stage or EventStage.FACE_DETECTED,
            job.status or CanonicalStatus.VERIFICATION_FAILED,
            {"unhandled_error": f"{type(exc).__name__}: {exc}"},
        )
    finally:
        job.done = True


def _pipeline_body(job: JobState, image_bytes: bytes, image_bgr: np.ndarray) -> None:
    # ---- Stage 1: face detection (VisionService) ---------------------------
    try:
        detected = vision_service.detect_and_encode(image_bgr)
    except Exception as exc:  # includes VisionModelNotReadyError
        _fail_job(
            job,
            EventStage.FACE_DETECTED,
            CanonicalStatus.NO_FACE_DETECTED,
            f"Query image could not be processed ({type(exc).__name__}: {exc})",
        )
        return
    if detected.status in (
        vision_service.VisionStatus.NO_FACE_DETECTED,
        vision_service.VisionStatus.LOW_IMAGE_QUALITY,
    ):
        _fail_job(
            job,
            EventStage.FACE_DETECTED,
            CanonicalStatus(detected.status.value),
            f"VisionService returned {detected.status.value}; query embedding "
            f"refused (quality_score={detected.quality_score:.3f})",
        )
        return
    job.emit(
        EventStage.FACE_DETECTED,
        (
            CanonicalStatus(detected.status.value)
            if detected.status is not vision_service.VisionStatus.OK
            else "OK"  # §1 vocabulary; amendment log 2026-09-01
        ),
        {
            "face_id": detected.face_id,
            "bbox": detected.bbox,
            "quality_score": detected.quality_score,
            "multiple_faces": detected.status
            == vision_service.VisionStatus.MULTIPLE_FACES_DETECTED,
        },
    )
    assert detected.embedding is not None  # OK / MULTIPLE both carry one

    # ---- Stage 2: live web/social search (SearchService — retrieval only) --
    job.emit(
        EventStage.QUERY_SENT,
        "OK",  # §1 vocabulary; amendment log 2026-09-01
        {"provider": "google_vision_web_detection", "image_bytes": len(image_bytes)},
    )
    try:
        search_out: SearchOutput = search_service.search(image_bytes)
    except search_service.SearchConfigError as exc:
        _fail_job(job, EventStage.QUERY_SENT, CanonicalStatus.SEARCH_API_FAILURE, str(exc))
        return
    except Exception as exc:  # noqa: BLE001 — provider failure, distinct from "nothing found"
        _fail_job(
            job,
            EventStage.QUERY_SENT,
            CanonicalStatus.SEARCH_API_FAILURE,
            f"Search failed: {type(exc).__name__}: {exc}",
        )
        return
    if search_out.status in (
        CanonicalStatus.NO_SEARCH_RESULTS,
        CanonicalStatus.SEARCH_API_FAILURE,
    ):
        _fail_job(
            job,
            EventStage.QUERY_SENT,
            search_out.status,
            f"SearchService returned {search_out.status.value}",
        )
        return

    # ---- Stage 3: candidates returned + deterministic selection ------------
    job.emit(
        EventStage.CANDIDATES_RETURNED,
        CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH,
        {"candidate_count": len(search_out.candidates)},
    )
    # Selection policy (deliberate, documented): first candidate = provider
    # order. SearchService is retrieval-only and forbidden from ranking by
    # similarity (CONTRACTS.md §2), so there is no better ordering to invent.
    selected = search_out.candidates[0]
    job.emit(
        EventStage.CANDIDATE_SELECTED,
        CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH,
        {
            "candidate_id": selected.candidate_id,
            "candidate_url": selected.candidate_url,
            "source_type": selected.source_type.value,
            "policy": "first candidate in provider order",
        },
    )

    # ---- Stage 4: independent verification (VerificationService) -----------
    vinput = VerificationInput(
        candidate_id=selected.candidate_id,
        candidate_url=selected.candidate_url,
        thumbnail_url=selected.thumbnail_url,
        query_embedding=detected.embedding,
    )
    try:
        verified = verification_service.verify(vinput)
    except Exception as exc:  # noqa: BLE001 — includes VisionModelNotReadyError
        message = f"Verification could not run ({type(exc).__name__}: {exc})"
        job.emit(EventStage.VERIFICATION_RUN, CanonicalStatus.VERIFICATION_FAILED, {"error": message})
        _fail_job(job, EventStage.VERIFICATION_RESULT, CanonicalStatus.VERIFICATION_FAILED, message)
        return
    job.verification = verified
    job.emit(
        EventStage.VERIFICATION_RUN,
        verified.decision.value,  # §5 permits decision values here
        {"candidate_id": verified.candidate_id},
    )
    mapped = _DECISION_TO_STATUS[verified.decision]
    job.emit(
        EventStage.VERIFICATION_RESULT,
        mapped,
        {
            "candidate_id": verified.candidate_id,
            "decision": verified.decision.value,
            "zone": verified.zone.value,
            "similarity": verified.independent_similarity_score,
            "reason": verified.reason,
        },
    )
    if verified.decision is not VerificationDecision.CANDIDATE_MATCH:
        # uncertain / no_match: explicit terminal states, never a fake match.
        return

    # ---- Stage 5: canonical record + best-effort IPFS pin ------------------
    try:
        record = blockchain_service.build_canonical_record(
            verified, source_url=selected.candidate_url
        )
    except Exception as exc:  # noqa: BLE001
        _fail_job(
            job,
            EventStage.RECORD_BUILT,
            CanonicalStatus.BLOCKCHAIN_FAILURE,
            f"Canonical record build failed ({type(exc).__name__}: {exc})",
        )
        return
    # Pinning is best-effort (§4: content_cid nullable) — never fatal.
    cid = blockchain_service.pin_to_ipfs(record)
    if cid:
        record = blockchain_service.with_content_cid(record, cid)
    job.canonical_record = record
    job.emit(
        EventStage.RECORD_BUILT,
        CanonicalStatus.SEARCH_SUCCESS_MATCH_VERIFIED,
        {
            "record_id": record.record_id,
            "content_hash": record.content_hash,
            "content_cid": record.content_cid,
            "ipfs_pinned": cid is not None,
        },
    )

    # ---- Stage 6: anchor on Polygon Amoy (never blocks an HTTP request) ----
    try:
        on_chain = blockchain_service.anchor_record(record)
    except blockchain_service.BlockchainConfigError as exc:
        _fail_job(job, EventStage.BLOCKCHAIN_TX_SUBMITTED, CanonicalStatus.BLOCKCHAIN_FAILURE, str(exc))
        return
    except blockchain_service.BlockchainWriteError as exc:
        _fail_job(job, EventStage.BLOCKCHAIN_TX_SUBMITTED, CanonicalStatus.BLOCKCHAIN_FAILURE, str(exc))
        return

    job.on_chain_record = on_chain
    job.polygonscan_url = POLYGONSCAN_TX_URL.format(tx_hash=on_chain.tx_hash)
    job.emit(
        EventStage.BLOCKCHAIN_TX_SUBMITTED,
        CanonicalStatus.BLOCKCHAIN_CONFIRMED,
        {
            "tx_hash": on_chain.tx_hash,
            "block_number": on_chain.block_number,
            "note": "submitted and receipt-awaited in one anchoring call",
        },
    )
    job.emit(
        EventStage.BLOCKCHAIN_CONFIRMED,
        CanonicalStatus.BLOCKCHAIN_CONFIRMED,
        {"tx_hash": on_chain.tx_hash, "block_number": on_chain.block_number},
    )

    # ---- Stage 7: re-verification (tamper-evidence check) ------------------
    integrity = blockchain_service.verify_integrity(
        record.model_dump(mode="json"), on_chain
    )
    job.emit(
        EventStage.REVERIFICATION_RUN,
        CanonicalStatus.BLOCKCHAIN_CONFIRMED
        if integrity["intact"]
        else CanonicalStatus.BLOCKCHAIN_FAILURE,
        integrity,
    )
    if not integrity["intact"]:  # practically impossible without a bug — surface it
        _set_error(job, "Re-verification mismatch: on-chain hash != rebuilt hash")


# ---------------------------------------------------------------------------
# HTTP endpoints (CONTRACTS.md §6)
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "pipeline_version": blockchain_service.PIPELINE_VERSION}


@app.post("/api/pipeline/start", response_model=PipelineStartResponse)
async def start_pipeline(image: UploadFile = File(...)) -> PipelineStartResponse:
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty image upload")
    image_bgr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise HTTPException(status_code=400, detail="upload is not a decodable image")

    job_id = str(uuid.uuid4())
    with _JOBS_LOCK:
        _JOBS[job_id] = JobState(job_id)
    threading.Thread(
        target=_run_pipeline,
        args=(job_id, raw, image_bgr),
        daemon=True,
        name=f"pipeline-{job_id}",
    ).start()
    return PipelineStartResponse(job_id=job_id)


def _get_job(job_id: str) -> JobState:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id!r}")
    return job


@app.get("/api/pipeline/{job_id}/status", response_model=PipelineStatusResponse)
def pipeline_status(job_id: str) -> PipelineStatusResponse:
    job = _get_job(job_id)
    return PipelineStatusResponse(job_id=job.job_id, stage=job.stage, status=job.status)


@app.get("/api/pipeline/{job_id}/result", response_model=PipelineResultResponse)
def pipeline_result(job_id: str) -> PipelineResultResponse:
    job = _get_job(job_id)
    if job.status is None:
        raise HTTPException(
            status_code=409,
            detail="job accepted; the pipeline has not emitted its first event yet",
        )
    return PipelineResultResponse(
        job_id=job.job_id,
        status=job.status,
        verification=job.verification,
        on_chain_record=job.on_chain_record,
        polygonscan_url=job.polygonscan_url,
        events=list(job.events),
        error_detail=job.error_detail,
    )


@app.get("/api/pipeline/{job_id}/events", response_model=list[PipelineEvent])
def pipeline_events(job_id: str) -> list[PipelineEvent]:
    job = _get_job(job_id)
    with job._lock:
        return list(job.events)


