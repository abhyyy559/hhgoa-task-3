"""Backend tests — happy path + every unhappy path (CONTRACTS.md §6).

All four services are monkeypatched at the module boundary (the same objects
``app.main`` calls), so no model, live search API, or chain is touched. The
pipeline runs in a background thread; tests poll ``main._JOBS[job_id].done``.
"""
from __future__ import annotations

import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import app.main as main
from contracts.schemas import (
    CanonicalStatus,
    OnChainRecord,
    SearchCandidate,
    SearchOutput,
    SourceType,
    VerificationDecision,
    VerificationOutput,
    VisionOutput,
    VisionStatus,
)
from services import vision as vision_service
from services.blockchain import BlockchainConfigError, BlockchainWriteError
from services.search import SearchConfigError

_ARR = np.zeros((32, 32, 3), dtype=np.uint8)
PNG_BYTES = cv2.imencode(".png", _ARR)[1].tobytes()

FULL_EVENT_ORDER = [
    "face_detected",
    "query_sent",
    "candidates_returned",
    "candidate_selected",
    "verification_run",
    "verification_result",
    "record_built",
    "blockchain_tx_submitted",
    "blockchain_confirmed",
    "reverification_run",
]


def ok_vision(image_bgr):
    return VisionOutput(
        face_id="face-1",
        embedding=[0.1] * 512,
        bbox=[0, 0, 10, 10],
        quality_score=0.95,
        status=VisionStatus.OK,
    )


def status_vision(status: VisionStatus):
    def _run(image_bgr):
        return VisionOutput(
            face_id="face-x",
            embedding=None,
            bbox=None if status == VisionStatus.NO_FACE_DETECTED else [0, 0, 1, 1],
            quality_score=0.1,
            status=status,
        )

    return _run


def ok_search(image_bytes, *, image_url=None):
    return SearchOutput(
        candidates=[
            SearchCandidate(
                candidate_id="c-1",
                candidate_url="https://example.com/post",
                source_type=SourceType.WEB,
                thumbnail_url="https://img.example.com/t.jpg",
            )
        ],
        status=CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH,
    )


def empty_search(image_bytes, *, image_url=None):
    return SearchOutput(candidates=[], status=CanonicalStatus.NO_SEARCH_RESULTS)


def matching_verify(vinput):
    return VerificationOutput(
        candidate_id=vinput.candidate_id,
        independent_similarity_score=0.83,
        zone="HIGH",
        decision=VerificationDecision.CANDIDATE_MATCH,
        reason="test: independent pass scored 0.83",
    )


def uncertain_verify(vinput):
    return VerificationOutput(
        candidate_id=vinput.candidate_id,
        independent_similarity_score=0.40,
        zone="UNCERTAIN",
        decision=VerificationDecision.UNCERTAIN,
        reason="test: deferred to a human",
    )


def rejecting_verify(vinput):
    return VerificationOutput(
        candidate_id=vinput.candidate_id,
        independent_similarity_score=0.10,
        zone="LOW",
        decision=VerificationDecision.NO_MATCH,
        reason="test: below review threshold",
    )


def fake_anchor(record, tx_hash: str = "0xabc123"):
    return OnChainRecord(
        record_id=record.record_id,
        content_hash=record.content_hash,
        content_cid=record.content_cid,
        source_reference_hash=record.source_reference_hash,
        verification_result=record.verification_result,
        verification_timestamp=record.verification_timestamp,
        tx_hash=tx_hash,
        block_number=42,
        confirmed=True,
    )


def patch_pipeline(
    monkeypatch,
    *,
    detect=ok_vision,
    search=ok_search,
    verify=matching_verify,
    pin=lambda record: "bafyfakeCID",
    anchor=None,
):
    monkeypatch.setattr(vision_service, "detect_and_encode", detect)
    monkeypatch.setattr(main.search_service, "search", search)
    monkeypatch.setattr(main.verification_service, "verify", verify)
    monkeypatch.setattr(main.blockchain_service, "pin_to_ipfs", pin)
    if anchor is not None:
        monkeypatch.setattr(main.blockchain_service, "anchor_record", anchor)


def start_job(client) -> str:
    resp = client.post(
        "/api/pipeline/start",
        files={"image": ("photo.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


def wait_done(job_id: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = main._JOBS[job_id]
        if job.done:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


@pytest.fixture()
def client(monkeypatch):
    main._JOBS.clear()
    return TestClient(main.app)


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_start_returns_job_id(client, monkeypatch):
    patch_pipeline(monkeypatch, verify=rejecting_verify)  # cheap terminal path
    resp = client.post(
        "/api/pipeline/start",
        files={"image": ("photo.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["job_id"] in main._JOBS


def test_happy_path_verified_match_anchored_on_chain(client, monkeypatch):
    patch_pipeline(monkeypatch, anchor=lambda r: fake_anchor(r, "0xdeadbeef"))
    job_id = start_job(client)
    job = wait_done(job_id)

    assert job.status == CanonicalStatus.BLOCKCHAIN_CONFIRMED
    assert job.verification.decision == VerificationDecision.CANDIDATE_MATCH
    assert job.on_chain_record.confirmed is True
    assert job.on_chain_record.tx_hash == "0xdeadbeef"
    assert job.polygonscan_url == "https://amoy.polygonscan.com/tx/0xdeadbeef"
    assert job.error_detail is None

    result = client.get(f"/api/pipeline/{job_id}/result").json()
    assert [e["stage"] for e in result["events"]] == FULL_EVENT_ORDER
    assert result["verification"]["decision"] == "candidate_match"
    assert result["on_chain_record"]["confirmed"] is True
    assert result["status"] == "BLOCKCHAIN_CONFIRMED"
    # Data-lineage integrity: no scoring data ever leaks into the event log.
    for event in result["events"]:
        assert "embedding" not in event["detail"]
        assert "similarity_score" not in event["detail"]


def test_no_face_is_visible_terminal_state(client, monkeypatch):
    patch_pipeline(monkeypatch, detect=status_vision(VisionStatus.NO_FACE_DETECTED))
    job_id = start_job(client)
    wait_done(job_id)
    assert main._JOBS[job_id].status == CanonicalStatus.NO_FACE_DETECTED
    events = client.get(f"/api/pipeline/{job_id}/events").json()
    assert [e["stage"] for e in events] == ["face_detected"]


def test_low_quality_is_visible_terminal_state(client, monkeypatch):
    patch_pipeline(monkeypatch, detect=status_vision(VisionStatus.LOW_IMAGE_QUALITY))
    job_id = start_job(client)
    wait_done(job_id)
    assert main._JOBS[job_id].status == CanonicalStatus.LOW_IMAGE_QUALITY


def test_no_search_results_is_visible_terminal_state(client, monkeypatch):
    patch_pipeline(monkeypatch, search=empty_search)
    job_id = start_job(client)
    wait_done(job_id)
    job = main._JOBS[job_id]
    assert job.status == CanonicalStatus.NO_SEARCH_RESULTS
    # Lineage: the outbound query call was made ("OK"), its outcome was
    # "found nothing" — a distinct terminal state from SEARCH_API_FAILURE.
    assert [e.stage.value for e in job.events] == [
        "face_detected",
        "query_sent",
        "query_sent",
    ]
    assert [e.status for e in job.events][-1] == "NO_SEARCH_RESULTS"


def test_search_config_error_is_search_api_failure(client, monkeypatch):
    def no_key(image_bytes, *, image_url=None):
        raise SearchConfigError("GOOGLE_VISION_API_KEY is not set")

    patch_pipeline(monkeypatch, search=no_key)
    job_id = start_job(client)
    wait_done(job_id)
    job = main._JOBS[job_id]
    assert job.status == CanonicalStatus.SEARCH_API_FAILURE
    assert "GOOGLE_VISION_API_KEY" in (job.error_detail or "")


def test_uncertain_never_becomes_a_match(client, monkeypatch):
    patch_pipeline(monkeypatch, verify=uncertain_verify)
    job_id = start_job(client)
    wait_done(job_id)
    job = main._JOBS[job_id]
    # Honest uncertainty zone: candidates returned, no high-confidence match.
    assert job.status == CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH
    assert all(e.stage != main.EventStage.RECORD_BUILT for e in job.events)


def test_verification_reject_is_terminal_no_chain_write(client, monkeypatch):
    patch_pipeline(monkeypatch, verify=rejecting_verify)
    job_id = start_job(client)
    wait_done(job_id)
    job = main._JOBS[job_id]
    assert job.status == CanonicalStatus.VERIFICATION_FAILED
    assert [e.stage.value for e in job.events][-1] == "verification_result"
    assert all(e.stage != main.EventStage.RECORD_BUILT for e in job.events)


def test_blockchain_config_failure_is_visible(client, monkeypatch):
    def config_boom(record):
        raise BlockchainConfigError("AMOY_PRIVATE_KEY is not set")

    patch_pipeline(monkeypatch, anchor=config_boom)
    job_id = start_job(client)
    wait_done(job_id)
    job = main._JOBS[job_id]
    assert job.status == CanonicalStatus.BLOCKCHAIN_FAILURE
    assert "AMOY_PRIVATE_KEY" in (job.error_detail or "")


def test_blockchain_write_failure_is_visible(client, monkeypatch):
    def write_boom(record):
        raise BlockchainWriteError("transaction reverted")

    patch_pipeline(monkeypatch, anchor=write_boom)
    job_id = start_job(client)
    wait_done(job_id)
    job = main._JOBS[job_id]
    assert job.status == CanonicalStatus.BLOCKCHAIN_FAILURE
    # Never claim BLOCKCHAIN_CONFIRMED when the write failed.
    assert all(
        e.status != CanonicalStatus.BLOCKCHAIN_CONFIRMED.value for e in job.events
    )


def test_unknown_job_404(client):
    assert client.get("/api/pipeline/nope/status").status_code == 404
    assert client.get("/api/pipeline/nope/result").status_code == 404
    assert client.get("/api/pipeline/nope/events").status_code == 404


def test_bad_upload_400(client):
    resp = client.post(
        "/api/pipeline/start",
        files={"image": ("photo.png", b"this is not an image", "image/png")},
    )
    assert resp.status_code == 400


def test_empty_upload_400(client):
    resp = client.post(
        "/api/pipeline/start",
        files={"image": ("photo.png", b"", "image/png")},
    )
    assert resp.status_code == 400


def test_missing_upload_422(client):
    resp = client.post("/api/pipeline/start")
    assert resp.status_code == 422

