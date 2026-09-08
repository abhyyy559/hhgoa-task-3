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
    PipelineEvent,
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
                candidate_url="https://www.instagram.com/p/test123/",
                source_type=SourceType.SOCIAL,
                thumbnail_url="https://img.example.com/t.jpg",
                is_social_domain=True,
                found_via="serpapi_lens",
            )
        ],
        status=CanonicalStatus.SEARCH_RESULTS_FOUND,
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
    gallery_candidates=None,
):
    import services.gallery as gallery_service

    monkeypatch.setattr(vision_service, "detect_and_encode", detect)
    monkeypatch.setattr(main.search_service, "search", search)
    monkeypatch.setattr(main.verification_service, "verify", verify)
    monkeypatch.setattr(main.blockchain_service, "pin_to_ipfs", pin)
    if anchor is not None:
        monkeypatch.setattr(main.blockchain_service, "anchor_record", anchor)
    # Isolate from real enrolled gallery by default; opt-in via gallery_candidates.
    if gallery_candidates is None:
        monkeypatch.setattr(gallery_service, "search_gallery_candidates", lambda *a, **k: [])
    else:
        monkeypatch.setattr(
            gallery_service, "search_gallery_candidates", lambda *a, **k: gallery_candidates
        )


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
    # Honest uncertainty zone: v3 PIPELINE_NO_CONFIDENT_MATCH, with retry events.
    assert job.status == CanonicalStatus.PIPELINE_NO_CONFIDENT_MATCH
    assert all(e.stage != main.EventStage.RECORD_BUILT for e in job.events)
    assert any(e.stage == main.EventStage.CANDIDATE_RETRY for e in job.events)


def test_verification_reject_is_terminal_no_chain_write(client, monkeypatch):
    patch_pipeline(monkeypatch, verify=rejecting_verify)
    job_id = start_job(client)
    wait_done(job_id)
    job = main._JOBS[job_id]
    assert job.status == CanonicalStatus.PIPELINE_NO_CONFIDENT_MATCH
    assert [e.stage.value for e in job.events][-1] == "verification_result"
    assert all(e.stage != main.EventStage.RECORD_BUILT for e in job.events)


def test_nonsocial_web_match_does_not_anchor(client, monkeypatch):
    """v3 §2: verified open-web non-social match alone is not sufficient."""

    def web_only_search(image_bytes, *, image_url=None):
        return SearchOutput(
            candidates=[
                SearchCandidate(
                    candidate_id="web-1",
                    candidate_url="https://example.com/blog-post",
                    source_type=SourceType.WEB,
                    thumbnail_url=None,
                    is_social_domain=False,
                    found_via="google_vision",
                )
            ],
            status=CanonicalStatus.SEARCH_RESULTS_FOUND,
        )

    patch_pipeline(monkeypatch, search=web_only_search, verify=matching_verify)
    job_id = start_job(client)
    job = wait_done(job_id)
    assert job.status == CanonicalStatus.PIPELINE_NO_CONFIDENT_MATCH
    assert all(e.stage != main.EventStage.RECORD_BUILT for e in job.events)


def test_all_top_candidates_evaluated_for_full_presence(client, monkeypatch):
    """First match anchors, but every top candidate is still verified so the
    lineup shows the full digital presence, not just the first hit."""

    def two_social_search(image_bytes, *, image_url=None):
        return SearchOutput(
            candidates=[
                SearchCandidate(
                    candidate_id="s-1",
                    candidate_url="https://www.instagram.com/p/first/",
                    source_type=SourceType.SOCIAL,
                    thumbnail_url="https://img.example.com/first.jpg",
                    is_social_domain=True,
                    found_via="serpapi_lens",
                    match_type="full_match",
                ),
                SearchCandidate(
                    candidate_id="s-2",
                    candidate_url="https://x.com/user/status/2",
                    source_type=SourceType.SOCIAL,
                    thumbnail_url="https://img.example.com/second.jpg",
                    is_social_domain=True,
                    found_via="serpapi_lens",
                    match_type="partial_match",
                ),
            ],
            status=CanonicalStatus.SEARCH_RESULTS_FOUND,
        )

    def both_match(inp):
        return VerificationOutput(
            candidate_id=inp.candidate_id,
            independent_similarity_score=0.90,
            zone="HIGH",
            decision=VerificationDecision.CANDIDATE_MATCH,
            reason="test match",
        )

    patch_pipeline(
        monkeypatch,
        search=two_social_search,
        verify=both_match,
        anchor=lambda r: fake_anchor(r, "0xpresence"),
    )
    job_id = start_job(client)
    job = wait_done(job_id)
    assert job.status == CanonicalStatus.BLOCKCHAIN_CONFIRMED
    # Deterministic: FIRST match anchors …
    assert job.verification.candidate_id == "s-1"
    # … while BOTH appear as matches in the lineup.
    matched = [e for e in job.candidate_lineup if e["decision"] == "candidate_match"]
    assert [e["candidate_id"] for e in matched] == ["s-1", "s-2"]


def test_latency_derived_from_event_log_not_estimated():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)

    def _ev(stage, seconds):
        return PipelineEvent(
            job_id="lat",
            stage=stage,
            timestamp=(base + timedelta(seconds=seconds)).isoformat(),
            status="x",
            detail={},
        )

    events = [
        _ev(main.EventStage.FACE_DETECTED, 0.0),
        _ev(main.EventStage.QUERY_SENT, 1.0),
        _ev(main.EventStage.CANDIDATES_RETURNED, 4.0),
        _ev(main.EventStage.VERIFICATION_RUN, 5.0),
        _ev(main.EventStage.RECORD_BUILT, 9.0),
        _ev(main.EventStage.BLOCKCHAIN_TX_SUBMITTED, 10.0),
        _ev(main.EventStage.REVERIFICATION_RUN, 20.0),
    ]
    lat = main.derive_latency_ms(events)
    assert lat["face_ms"] == 1000.0
    assert lat["search_ms"] == 3000.0
    assert lat["verify_ms"] == 4000.0
    assert lat["record_ms"] == 1000.0
    assert lat["chain_ms"] == 10000.0
    assert lat["total_ms"] == 20000.0
    # Skipped stages are absent, never zero-filled.
    assert main.derive_latency_ms(events[:2]) == {"face_ms": 1000.0, "total_ms": 1000.0}
    assert main.derive_latency_ms([]) == {}


def test_happy_path_result_carries_latency(client, monkeypatch):
    patch_pipeline(monkeypatch, anchor=lambda r: fake_anchor(r, "0xlat"))
    job_id = start_job(client)
    job = wait_done(job_id)
    assert job.status == CanonicalStatus.BLOCKCHAIN_CONFIRMED
    resp = client.get(f"/api/pipeline/{job_id}/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latency_ms"]["total_ms"] >= 0
    assert body["latency_ms"]["face_ms"] >= 0


def test_gallery_match_anchors_with_labeled_source(client, monkeypatch):
    """Enrolled-gallery matches anchor (labeled), bypassing the social gate."""

    def gallery_search(image_bytes, *, image_url=None):
        return SearchOutput(
            candidates=[
                SearchCandidate(
                    candidate_id="gal-1",
                    candidate_url="http://127.0.0.1:8000/api/gallery/profiles/abc",
                    source_type=SourceType.WEB,
                    thumbnail_url="http://127.0.0.1:8000/api/gallery/images/abc.jpg",
                    is_social_domain=False,
                    found_via=None,
                )
            ],
            status=CanonicalStatus.SEARCH_RESULTS_FOUND,
        )

    patch_pipeline(
        monkeypatch,
        search=gallery_search,
        verify=matching_verify,
        anchor=lambda r: fake_anchor(r, "0xgal123"),
    )
    job_id = start_job(client)
    job = wait_done(job_id)
    assert job.status == CanonicalStatus.BLOCKCHAIN_CONFIRMED
    assert job.source_url == "http://127.0.0.1:8000/api/gallery/profiles/abc"
    assert job.lookup_source_url(job.canonical_record.record_id) == job.source_url
    built = [e for e in job.events if e.stage == main.EventStage.RECORD_BUILT][0]
    assert built.detail["match_source"] == "enrolled_gallery"


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


# ---------------------------------------------------------------------------
# New endpoints: SSE stream, DELETE, job-store bounds, richer health
# ---------------------------------------------------------------------------
import asyncio


def test_health_includes_checks(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "pipeline_version" in body
    assert isinstance(body["checks"], dict)
    for key in (
        "vision_model_pack",
        "google_vision_key_set",
        "pinata_jwt_set",
        "amoy_wallet_set",
        "amoy_contract_deployed",
    ):
        assert key in body["checks"]


def test_sse_stream_replays_and_terminates():
    """The pure SSE generator must replay existing events and end with 'done'."""
    job = main.JobState("stream-job")
    job.events.append(
        PipelineEvent(
            job_id="stream-job",
            stage=main.EventStage.FACE_DETECTED,
            timestamp="2026-09-01T00:00:00+00:00",
            status="OK",
            detail={"face_id": "abc"},
        )
    )
    job.done = True

    async def collect():
        chunks = []
        async for chunk in main._event_stream_iter(job):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    blob = "".join(chunks)
    assert 'event: pipeline' in blob
    assert '"face_id":"abc"' in blob
    assert 'event: done' in blob


def test_delete_finished_job(client, monkeypatch):
    patch_pipeline(monkeypatch, verify=rejecting_verify)
    job_id = start_job(client)
    wait_done(job_id)
    assert client.delete(f"/api/pipeline/{job_id}").status_code == 200
    # Deleted job is gone from the registry.
    assert client.get(f"/api/pipeline/{job_id}/status").status_code == 404


def test_delete_running_job_is_409(client):
    # Insert a job that is explicitly NOT done.
    main._JOBS["still-running"] = main.JobState("still-running")
    assert client.delete("/api/pipeline/still-running").status_code == 409


def test_delete_unknown_job_is_404(client):
    assert client.delete("/api/pipeline/nope").status_code == 404


def test_job_store_is_bounded(monkeypatch):
    monkeypatch.setattr(main, "MAX_JOBS", 3)
    main._JOBS.clear()
    for i in range(6):
        j = main.JobState(f"j{i}")
        j.created_at = float(i)  # deterministic ordering
        main._JOBS[f"j{i}"] = j
    main._trim_job_store()
    assert len(main._JOBS) <= 3


def test_gallery_enroll_and_retrieve_endpoints(client, tmp_path, monkeypatch):
    import services.gallery as gallery
    monkeypatch.setattr(gallery, "GALLERY_DIR", tmp_path / "gallery")
    monkeypatch.setattr(gallery, "GALLERY_INDEX_FILE", tmp_path / "gallery" / "gallery_index.json")
    gallery._ensure_gallery_dirs()

    # Enroll via API
    resp = client.post(
        "/api/gallery/enroll",
        data={
            "full_name": "Dr. Sarah Chen",
            "role": "Director of Cyber Defense",
            "organization": "HH Goa Advanced AI",
            "bio": "Lead architect on zero-trust biometric pipeline.",
        },
        files={"image": ("photo.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["member"]["full_name"] == "Dr. Sarah Chen"
    member_id = data["member"]["member_id"]

    # List gallery members
    list_resp = client.get("/api/gallery/members")
    assert list_resp.status_code == 200
    members = list_resp.json()["members"]
    assert len(members) == 1
    assert members[0]["member_id"] == member_id

    # Retrieve profile HTML
    prof_resp = client.get(f"/api/gallery/profiles/{member_id}")
    assert prof_resp.status_code == 200
    assert "text/html" in prof_resp.headers["content-type"]
    assert "Dr. Sarah Chen" in prof_resp.text

    # Retrieve member image
    img_resp = client.get(f"/api/gallery/images/{data['member']['image_filename']}")
    assert img_resp.status_code == 200
    assert img_resp.content == PNG_BYTES


def test_multi_candidate_pipeline_evaluation(client, monkeypatch):
    """Ensure pipeline evaluates candidate #1 when candidate #0 is rejected/fails."""
    # Search returns 2 candidates (second is social to satisfy v3 §2)
    c1 = SearchCandidate(
        candidate_id="cand-0-nomatch",
        candidate_url="http://campus.edu/student/other",
        source_type=SourceType.WEB,
        thumbnail_url="http://campus.edu/student/other.jpg",
        is_social_domain=False,
    )
    c2 = SearchCandidate(
        candidate_id="cand-1-match",
        candidate_url="https://www.instagram.com/p/target123/",
        source_type=SourceType.SOCIAL,
        thumbnail_url="https://img.example.com/target.jpg",
        is_social_domain=True,
        found_via="serpapi_lens",
    )
    multi_search = SearchOutput(
        candidates=[c1, c2],
        status=CanonicalStatus.SEARCH_RESULTS_FOUND,
    )

    def multi_verify(inp):
        if inp.candidate_id == "cand-0-nomatch":
            return VerificationOutput(
                candidate_id="cand-0-nomatch",
                decision=VerificationDecision.NO_MATCH,
                zone="LOW",
                independent_similarity_score=0.12,
                reason="Faces do not match (cosine 0.12)",
            )
        else:
            return VerificationOutput(
                candidate_id="cand-1-match",
                decision=VerificationDecision.CANDIDATE_MATCH,
                zone="HIGH",
                independent_similarity_score=0.92,
                reason="Match confirmed (cosine 0.92)",
            )

    patch_pipeline(
        monkeypatch,
        search=lambda *a, **kw: multi_search,
        verify=multi_verify,
        anchor=lambda r: fake_anchor(r, "0xdeadbeef"),
    )
    job_id = start_job(client)
    job = wait_done(job_id)
    assert job.status == CanonicalStatus.BLOCKCHAIN_CONFIRMED
    assert job.verification.candidate_id == "cand-1-match"
    assert job.verification.decision == VerificationDecision.CANDIDATE_MATCH



