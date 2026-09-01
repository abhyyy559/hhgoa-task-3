"""scripts/demo_pipeline.py — run the pipeline and print its live output.

Part A: the REAL pipeline (real buffalo_l model) over a synthetic image via
        the actual FastAPI job manager — shows the honest NO_FACE_DETECTED
        terminal state with the full event log.
Part B: the full happy path (match verified -> record -> anchored) with the
        live search/chain calls stubbed, because GOOGLE_VISION_API_KEY and
        AMOY_PRIVATE_KEY are still pending (HUMAN_ACTIONS H1/H3). The stubs
        are labelled loudly in the output — this is the integration rehearsal,
        not a live run.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import app.main as main  # noqa: E402
from contracts.schemas import (  # noqa: E402
    CanonicalStatus,
    OnChainRecord,
    SearchCandidate,
    SearchOutput,
    SourceType,
    VerificationDecision,
    VerificationOutput,
)
from services import vision as vision_service  # noqa: E402


def print_events(events) -> None:
    print(f"  {'STAGE':<24} {'STATUS':<40} DETAIL")
    for e in events:
        detail = json.dumps(e.detail, default=str)[:60]
        print(f"  {e.stage.value:<24} {e.status:<40} {detail}")


def run_job(label: str, image_bgr: np.ndarray, image_bytes: bytes = b"x") -> None:
    job_id = f"demo-{label}"
    main._JOBS.clear()
    main._JOBS[job_id] = main.JobState(job_id)
    main._run_pipeline(job_id, image_bytes, image_bgr)
    job = main._JOBS[job_id]
    print(f"\nFINAL STATUS: {job.status.value if job.status else None}")
    if job.error_detail:
        print(f"error_detail: {job.error_detail}")
    print("EVENT LOG:")
    for e in job.events:
        print(f"  {e.stage.value:<24} {e.status:<40} {json.dumps(e.detail, default=str)[:70]}")
    return job


def main_part_a() -> None:
    print("=" * 72)
    print("PART A — REAL pipeline (real buffalo_l, no API keys) on a synthetic image")
    print("=" * 72)
    rng = np.random.default_rng(3)
    img = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
    print("(synthetic noise image — expect the honest no-face terminal state)\n")
    run_job("no-face", img)


def main_part_b() -> None:
    print("\n" + "=" * 72)
    print("PART B — full happy path with STUBBED search/chain (keys pending H1/H3)")
    print("=" * 72)

    # STUB: live search needs GOOGLE_VISION_API_KEY (H1) — stub retrieval.
    def stub_search(image_bytes, *, image_url=None):
        return SearchOutput(
            candidates=[SearchCandidate(
                candidate_id="stub-1",
                candidate_url="https://example.com/team-member-post",
                source_type=SourceType.WEB,
            )],
            status=CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH,
        )

    # STUB: live anchoring needs AMOY_PRIVATE_KEY (H3) — return a fake receipt.
    def stub_anchor(record):
        return OnChainRecord(
            record_id=record.record_id,
            content_hash=record.content_hash,
            content_cid=record.content_cid,
            source_reference_hash=record.source_reference_hash,
            verification_result=record.verification_result,
            verification_timestamp=record.verification_timestamp,
            tx_hash="0xstub" + "0" * 60,
            block_number=42,
            confirmed=True,
        )

    # STUB: live verification needs a real candidate page on the web.
    def stub_verify(vinput):
        return VerificationOutput(
            candidate_id=vinput.candidate_id,
            independent_similarity_score=0.71,
            zone="HIGH",
            decision=VerificationDecision.CANDIDATE_MATCH,
            reason="STUBbed verification (keys pending); real pass re-fetches the page",
        )

    from skimage import data  # real face so the real vision model finds one
    import cv2

    img = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)
    main.search_service.search = stub_search
    main.verification_service.verify = stub_verify
    main.blockchain_service.anchor_record = stub_anchor
    main.blockchain_service.pin_to_ipfs = lambda record: "bafydemoCIDstub"
    print("(vision model REAL; search/verification/chain stubbed — see labels)\n")
    job = run_job("happy", img, image_bytes=b"demo")
    if job.status != CanonicalStatus.BLOCKCHAIN_CONFIRMED:
        raise SystemExit("happy path did not reach BLOCKCHAIN_CONFIRMED")


if __name__ == "__main__":
    main_part_a()
    main_part_b()
    print("\nDEMO COMPLETE — every stage of the §5 event log was emitted.")
