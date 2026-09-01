"""scripts/smoke_amoy.py — Phase 0 Amoy smoke test (read-only; no key needed).

Checks, in order: env config, both RPC endpoints (chainId 80002), wallet
balance if AMOY_WALLET_ADDRESS is set. Safe to run any time — it never signs
or sends anything.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    from web3 import Web3

    import services.blockchain as bc

    print("=" * 72)
    print("PHASE 0 SMOKE TEST — Polygon Amoy (chainId 80002)")
    print("=" * 72)

    ok = True
    for url in bc.get_rpc_endpoints():
        try:
            w3 = Web3(Web3.HTTPProvider(url))
            connected = w3.is_connected()
            chain_id = w3.eth.chain_id if connected else None
            latest = w3.eth.block_number if connected else None
            status = "OK" if connected and chain_id == 80002 else "WRONG CHAIN"
            if not connected or chain_id != 80002:
                ok = False
            print(f"  {url}\n    connected={connected} chainId={chain_id} "
                  f"latest_block={latest}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  {url}\n    FAILED: {type(exc).__name__}: {exc}")

    wallet = os.getenv("AMOY_WALLET_ADDRESS")
    if wallet:
        try:
            w3 = Web3(Web3.HTTPProvider(bc.get_rpc_endpoints()[0]))
            balance = w3.eth.get_balance(Web3.to_checksum_address(wallet))
            print(f"\n  wallet {wallet}\n    balance = {balance / 1e18:.6f} MATIC")
            if balance == 0:
                print("    !! balance is zero — fund it before anchoring (H3)")
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"\n  wallet check failed: {exc}")
            ok = False
    else:
        print("\n  AMOY_WALLET_ADDRESS not set — skipping balance check (HUMAN_ACTIONS H3)")

    key_set = bool(os.getenv("AMOY_PRIVATE_KEY"))
    print(f"\n  AMOY_PRIVATE_KEY configured: {key_set}")

    print("\nRESULT:", "PASS" if ok else "FAIL (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
