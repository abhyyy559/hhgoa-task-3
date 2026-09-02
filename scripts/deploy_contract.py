"""scripts/deploy_contract.py — deploy AnchorRecord once and persist its address.

Deploys on Polygon Amoy using the funded wallet in ``.env``, then writes the
returned checksummed address into ``AMOY_CONTRACT_ADDRESS=...`` in ``.env``
(creating the file when missing) and prints the Polygonscan link.

Usage:
    .venv\\Scripts\\python.exe scripts\\deploy_contract.py

Run this ONCE per environment. Subsequent anchors reuse the same contract,
which keeps the demo cheap (no per-anchor deploy bytecode cost) and means
``read_onchain_record`` works for every anchored record.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")


def set_env(key: str, value: str) -> None:
    """Set/append one KEY=VALUE line in .env without touching other lines."""
    env_path = PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replaced = False
    out: list[str] = []
    for ln in lines:
        if "=" in ln and ln.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    print(f"  wrote {key} to {env_path}")


def main() -> int:
    import services.blockchain as bc

    existing = __import__("os").getenv("AMOY_CONTRACT_ADDRESS")
    if existing:
        print(f"AMOY_CONTRACT_ADDRESS already set: {existing}")
        print("  -> refuse to redeploy; delete the line first if you really"
              " want a fresh contract.")
        return 0

    print("Deploying AnchorRecord on Polygon Amoy ...")
    try:
        address = bc.deploy_anchor_contract()
    except (bc.BlockchainConfigError, bc.BlockchainWriteError) as exc:
        print(f"deploy failed: {exc}")
        return 1

    set_env("AMOY_CONTRACT_ADDRESS", address)
    print(f"contract address : {address}")
    print(f"polygonscan      : https://amoy.polygonscan.com/address/{address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())