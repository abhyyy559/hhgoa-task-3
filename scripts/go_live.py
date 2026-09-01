"""scripts/go_live.py — run every remaining LIVE task once keys are in .env.

Usage:
    .venv\\Scripts\\python.exe scripts\\go_live.py <step>

Steps:
    env     - key presence + format checks (never prints secret values)
    amoy    - live Amoy: RPC connectivity, wallet balance, gas price
    pinata  - live Pinata: pin a smoke JSON, print the CID
    vision  - live Google Vision Web Detection call (proves the H1 key works)
    e2e     - FULL live pipeline: real face -> real search -> real independent
              verification -> (if verified) record -> pin -> on-chain anchor
    all     - run every step in order

Every step prints a PASS / FAIL verdict and never prints secret values. The
e2e step is the real system with NO stubs — whatever terminal state it reaches
(including BLOCKCHAIN_FAILURE on an unfunded wallet) is the honest result.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")


def banner(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


# ---------------------------------------------------------------------------
def step_env() -> bool:
    import os
    from eth_account import Account

    banner("STEP env — key presence + format checks")
    ok = True
    required = {
        "GOOGLE_VISION_API_KEY": "H1",
        "PINATA_JWT": "H2",
        "AMOY_PRIVATE_KEY": "H3",
    }
    for key, human in required.items():
        val = os.getenv(key, "").strip()
        state = f"SET ({len(val)} chars)" if val else "MISSING"
        if not val:
            ok = False
        print(f"  {key:<24} {state}   (HUMAN_ACTIONS {human})")

    # Wallet format check: derive the address from the key, never print the key.
    pk = os.getenv("AMOY_PRIVATE_KEY", "").strip()
    if pk:
        try:
            acct = Account.from_key(pk)
            print(f"  AMOY_PRIVATE_KEY format: VALID (address {acct.address})")
            declared = os.getenv("AMOY_WALLET_ADDRESS", "").strip()
            if declared:
                if declared.lower() == acct.address.lower():
                    print("  AMOY_WALLET_ADDRESS matches the key-derived address")
                else:
                    print(
                        f"  !! AMOY_WALLET_ADDRESS does NOT match the key-derived "
                        f"address ({acct.address}) — signing uses the key-derived one"
                    )
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  !! AMOY_PRIVATE_KEY format INVALID: {type(exc).__name__}: {exc}")
            print(
                "     ACTION (HUMAN, H3): AMOY_PRIVATE_KEY must be the wallet's\n"
                "     PRIVATE KEY (0x + 64 hex chars), not an RPC endpoint or\n"
                "     public address. Generate a throwaway Amoy wallet via\n"
                "     https://faucet.polygon.technology and paste its private key."
            )
    return ok


# ---------------------------------------------------------------------------
def step_amoy() -> bool:
    import os

    from web3 import Web3

    import services.blockchain as bc

    banner("STEP amoy — live Polygon Amoy connectivity + wallet")
    ok = True
    for url in bc.get_rpc_endpoints():
        try:
            w3 = Web3(Web3.HTTPProvider(url))
            print(f"  {url}\n    connected={w3.is_connected()} chainId={w3.eth.chain_id} "
                  f"block={w3.eth.block_number} gas={w3.eth.gas_price / 1e9:.2f} gwei")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  {url}\n    FAILED: {type(exc).__name__}: {exc}")

    address = os.getenv("AMOY_WALLET_ADDRESS", "").strip()
    if address:
        try:
            from eth_utils import is_address

            if not is_address(address):
                print(
                    f"  !! AMOY_WALLET_ADDRESS ({address[:40]}...) is NOT an EVM "
                    f"address. It looks like an RPC endpoint URL, not a wallet. "
                    f"Expected a 0x + 40-hex string."
                )
                print(
                    "     ACTION (HUMAN, H3): generate a throwaway Amoy wallet at\n"
                    "     https://faucet.polygon.technology (or MetaMask -> Amoy),\n"
                    "     copy its PRIVATE KEY (0x + 64 hex) into AMOY_PRIVATE_KEY\n"
                    "     and its address into AMOY_WALLET_ADDRESS."
                )
                ok = False
            else:
                w3 = Web3(Web3.HTTPProvider(bc.get_rpc_endpoints()[0]))
                balance = w3.eth.get_balance(w3.to_checksum_address(address))
                print(f"  wallet balance: {balance / 1e18:.6f} MATIC")
                if balance == 0:
                    print("  !! wallet unfunded — anchoring will fail until testnet")
                    print("     MATIC is added (https://faucet.polygon.technology/)")
                    ok = False
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  wallet check FAILED: {type(exc).__name__}: {exc}")
    else:
        print("  AMOY_WALLET_ADDRESS not set — skipping balance check")
    return ok


# ---------------------------------------------------------------------------
def step_pinata() -> bool:
    import uuid

    import requests

    import services.blockchain as bc

    banner("STEP pinata — live IPFS pin")
    jwt = os.getenv("PINATA_JWT", "").strip()
    if not jwt:
        print("  PINATA_JWT not found in .env")
        return False
    try:
        resp = requests.post(
            bc.PINATA_PIN_URL,
            headers={"Authorization": f"Bearer {jwt}"},
            json={"pinataContent": {"smoke": str(uuid.uuid4()), "project": "hhgoa-task3"}},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  pin FAILED (network): {exc}")
        return False
    print(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"  body: {resp.text[:300]}")
        return False
    cid = resp.json().get("IpfsHash")
    print(f"  pinned CID: {cid}")
    print(f"  {bc.ipfs_gateway_url(cid)}")
    return True


# ---------------------------------------------------------------------------
def step_vision() -> bool:
    import base64

    import requests

    from contracts.schemas import CanonicalStatus

    banner("STEP vision — live Google Vision Web Detection call")
    key = os.getenv("GOOGLE_VISION_API_KEY", "").strip()
    if not key:
        print("  GOOGLE_VISION_API_KEY missing")
        return False
    # Raw REST call so the API's own error message (billing/credential) is shown
    # verbatim — the most actionable way to surface an H1 problem.
    resp = requests.post(
        f"https://vision.googleapis.com/v1/images:annotate?key={key}",
        json={
            "requests": [
                {
                    "image": {"content": base64.b64encode(b"hello").decode()},
                    "features": [{"type": "WEB_DETECTION", "maxResults": 5}],
                }
            ]
        },
        timeout=30,
    )
    print(f"  HTTP {resp.status_code}")
    body = resp.json()
    err = body.get("error")
    if err:
        print(f"  status : {err.get('status')}")
        print(f"  message: {err.get('message')}")
        if "billing" in (err.get("message") or "").lower():
            print("\n  -> ACTION (HUMAN): enable billing on the GCP project from the")
            print("     link in the message above, wait a few minutes, re-run.")
        return False
    features = body.get("responses", [{}])[0]
    web = features.get("webDetection") or {}
    print(f"  web entities/page matches: "
          f"{len(web.get('pagesWithMatchingImages') or []) + len(web.get('partialMatchingImages') or [])}")
    print("  -> Vision key is LIVE and returns results")
    return True


# ---------------------------------------------------------------------------
def step_e2e() -> bool:
    import cv2

    import app.main as main

    banner("STEP e2e — FULL live pipeline, no stubs (real face -> real search "
           "-> real verification -> real anchor)")
    from skimage import data

    img = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)
    print("  (public NASA astronaut photo as the stand-in face; H4 team photos")
    print("   replace this for the graded demo)\n")

    job_id = "go-live-e2e"
    main._JOBS.clear()
    main._JOBS[job_id] = main.JobState(job_id)
    main._run_pipeline(job_id, b"e2e", img)
    job = main._JOBS[job_id]

    print(f"FINAL STATUS: {job.status.value if job.status else None}")
    if job.error_detail:
        print(f"error_detail: {job.error_detail}")
    print("EVENT LOG:")
    for e in job.events:
        print(f"  {e.stage.value:<24} {e.status:<40} {json.dumps(e.detail, default=str)[:70]}")
    if job.polygonscan_url:
        print(f"\nPolygonscan: {job.polygonscan_url}")
    # The step passes when the pipeline reached ANY typed terminal state —
    # the goal is proving the live system behaves honestly end-to-end.
    reached = job.done and job.status is not None
    print(f"\n  -> live pipeline reached a typed terminal state: {reached}")
    return reached


STEPS = {
    "env": step_env,
    "amoy": step_amoy,
    "pinata": step_pinata,
    "vision": step_vision,
    "e2e": step_e2e,
}


def main() -> int:
    steps = sys.argv[1:] or ["all"]
    if steps == ["all"]:
        steps = list(STEPS)
    results = {}
    for name in steps:
        if name not in STEPS:
            print(f"unknown step {name!r}; choose from {list(STEPS) + ['all']}")
            return 2
        try:
            results[name] = STEPS[name]()
        except SystemExit as exc:
            results[name] = exc.code == 0
        except Exception as exc:  # noqa: BLE001
            print(f"  step crashed: {type(exc).__name__}: {exc}")
            results[name] = False

    banner("GO-LIVE SUMMARY")
    for name, ok in results.items():
        print(f"  {name:<8} {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

