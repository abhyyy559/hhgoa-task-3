"""scripts/tamper_demo.py — the Phase 4 live tamper-evidence demo.

Runs the full integrity story with real cryptography and no network:
1. Build the canonical §4 record (real SHA-256 over canonical JSON).
2. Re-verify the untouched record against its on-chain hash  → INTACT.
3. Edit ONE pixel of the underlying content (analog: any tampering)
   and rebuild the hash                                        → MISMATCH.

What this demonstrates (say it out loud): content integrity, NOT facial
identity — the image could still depict the same person even untampered.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from contracts.schemas import (  # noqa: E402
    OnChainRecord,
    VerificationDecision,
    VerificationOutput,
)
from services import blockchain as bc  # noqa: E402


def main() -> None:
    print("=" * 72)
    print("TAMPER-EVIDENCE DEMO — content integrity, not facial identity")
    print("=" * 72)

    # -- 1. "Capture" content (in the real demo: the photo + its verification)
    rng = np.random.default_rng(42)
    image = rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
    image_bytes = image.tobytes()
    print(f"\n[1] content captured: {image.shape} image, "
          f"sha256(content)={hashlib.sha256(image_bytes).hexdigest()[:16]}...")

    verification = VerificationOutput(
        candidate_id="demo-candidate",
        independent_similarity_score=0.83,
        zone="HIGH",
        decision=VerificationDecision.CANDIDATE_MATCH,
        reason="demo: independent re-verification scored 0.83",
    )
    record = bc.build_canonical_record(verification, source_url="https://page.example.com/post")
    record = bc.with_content_cid(record, "bafydemoCID123")
    print(f"[2] canonical record built:  record_id={record.record_id}")
    print(f"    content_hash (sha256)  = {record.content_hash}")

    on_chain = OnChainRecord(
        record_id=record.record_id,
        content_hash=record.content_hash,
        content_cid=record.content_cid,
        source_reference_hash=record.source_reference_hash,
        verification_result=record.verification_result,
        verification_timestamp=record.verification_timestamp,
        tx_hash="0x" + "ab" * 32,
        block_number=42,
        confirmed=True,
    )
    print(f"[3] anchored on-chain at block {on_chain.block_number}, "
          f"tx {on_chain.tx_hash[:14]}...")

    # -- 4. Re-verification of the UNTOUCHED content
    verdict = bc.verify_integrity(record.model_dump(mode="json"), on_chain)
    print(f"\n[4] re-verify untouched record:")
    print(f"    rebuilt  = {verdict['recomputed_content_hash'][:16]}...")
    print(f"    on-chain = {verdict['onchain_content_hash'][:16]}...")
    print(f"    -> INTACT = {verdict['intact']}")

    # -- 5. Tamper: flip ONE pixel of the content, rebuild the fingerprint
    tampered = record.model_dump(mode="json")
    tampered["verification_result"] = "no_match"  # one field = one pixel's worth of lie
    verdict2 = bc.verify_integrity(tampered, on_chain)
    print(f"\n[5] TAMPER: verification_result flipped 'candidate_match' -> 'no_match'")
    print(f"    rebuilt  = {verdict2['recomputed_content_hash'][:16]}...")
    print(f"    on-chain = {verdict2['onchain_content_hash'][:16]}...")
    print(f"    -> INTACT = {verdict2['intact']}   (hash mismatch = tamper detected)")

    # -- 6. Content-level analog: one pixel of the image itself
    image2 = image.copy()
    image2[0, 0, 0] ^= 0x01  # flip a single bit in one pixel
    h1 = hashlib.sha256(image_bytes).hexdigest()
    h2 = hashlib.sha256(image2.tobytes()).hexdigest()
    print(f"\n[6] content-level analog: one pixel flipped in the image bytes")
    print(f"    before = {h1[:16]}...")
    print(f"    after  = {h2[:16]}...")
    print(f"    -> fingerprints differ: {h1 != h2}")

    print("\n" + "=" * 72)
    print("This demonstrates CONTENT INTEGRITY, not facial identity — the")
    print("image could still depict the same person even untampered.")
    print("=" * 72)
    sys.exit(0 if (verdict["intact"] and not verdict2["intact"] and h1 != h2) else 1)


if __name__ == "__main__":
    main()
