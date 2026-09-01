"""scripts/smoke_pinata.py — Phase 0 Pinata smoke test (needs PINATA_JWT).

Uploads a tiny JSON doc, prints the CID, then verifies it resolves via the
gateway. Without a JWT it explains exactly which human action (H2) is missing.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    import requests

    import services.blockchain as bc

    print("=" * 72)
    print("PHASE 0 SMOKE TEST — Pinata IPFS pinning")
    print("=" * 72)

    jwt = os.getenv("PINATA_JWT")
    if not jwt:
        print("  PINATA_JWT is not set — see HUMAN_ACTIONS.md item H2.")
        print("  Steps: pinata.cloud -> API Keys -> JWT -> paste into .env")
        print("\nRESULT: BLOCKED-HUMAN")
        return 2

    payload = {"pinataContent": {"smoke": str(uuid.uuid4()), "project": "hhgoa-task3"}}
    try:
        resp = requests.post(
            bc.PINATA_PIN_URL,
            headers={"Authorization": f"Bearer {jwt}"},
            json=payload,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Pinata unreachable: {exc}\nRESULT: FAIL")
        return 1

    print(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"  body: {resp.text[:300]}")
        print("\nRESULT: FAIL")
        return 1

    cid = resp.json().get("IpfsHash")
    gateway = os.getenv("PINATA_GATEWAY", "gateway.pinata.cloud").rstrip("/")
    print(f"  pinned CID: {cid}")
    print(f"  gateway URL: https://{gateway}/ipfs/{cid}")
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
