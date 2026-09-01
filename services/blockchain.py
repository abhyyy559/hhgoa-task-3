"""BlockchainService — integrity anchoring on Polygon Amoy (CONTRACTS.md §4).

Public API
----------
- ``build_canonical_record(verification, *, source_url, pipeline_version)``:
    build the minimized §4 record (NO biometric data) and its content_hash —
    sha256 of the canonical JSON of the record minus the content_hash field.
- ``with_content_cid(record, cid)``: attach an IPFS CID and recompute the
    content hash (the CID is part of the hashed object).
- ``rebuild_content_hash(record_dict) -> str``: recompute a record hash —
    this is what makes the tamper-evidence re-verification demo possible.
- ``pin_to_ipfs(record) -> Optional[str]``: Pinata JSON pinning. Non-fatal by
    design (§4 allows ``content_cid: null``): a failure logs loudly and
    returns None so a demo-day Pinata hiccup cannot kill an on-chain anchor.
- ``anchor_record(record) -> OnChainRecord``: live Polygon Amoy write via
    web3.py 8.x (``signed.raw_transaction`` — v8 renamed this field). Typed
    failures only: ``BlockchainConfigError`` / ``BlockchainWriteError``.
    Two RPC endpoints (PROJECT_STATUS.md decision #2: drpc primary,
    publicnode fallback).
- ``read_onchain_record(record_id)`` / ``verify_integrity(record, on_chain)``:
    read-back and tamper-evidence re-verification.

What the chain proves — state it precisely every time (TASK3_ARCHITECTURE.md
§10): that this specific fingerprint was anchored at this timestamp and hasn't
changed since. It does NOT prove that the post belongs to the person.
Explicitly excluded from anything on-chain (§4): face embeddings, raw source
URLs, raw image bytes, submitter PII.

Configuration (env, see .env.example / HUMAN_ACTIONS.md):
    AMOY_PRIVATE_KEY, AMOY_WALLET_ADDRESS, AMOY_RPC_URL, AMOY_RPC_FALLBACK,
    AMOY_CONTRACT_ADDRESS (optional — deploys on first use when absent),
    PINATA_JWT, PINATA_GATEWAY.

No secrets in code; everything comes from the environment via os.getenv.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

import requests
from dotenv import load_dotenv

from contracts.schemas import CanonicalRecord, OnChainRecord, VerificationOutput

load_dotenv()

PIPELINE_VERSION = "1.0.0"

AMOY_CHAIN_ID = 80002  # Polygon Amoy testnet
DEFAULT_RPC_URL = "https://polygon-amoy.drpc.org"
DEFAULT_RPC_FALLBACK = "https://polygon-amoy-bor-rpc.publicnode.com"

PINATA_PIN_URL = "https://api.pinata.xyz/pinning/pinJSONToIPFS"
PINATA_TIMEOUT_S = 30

TX_WAIT_TIMEOUT_S = 180  # seconds to wait for a receipt before giving up
DEPLOY_GAS = 1_800_000
ANCHOR_GAS = 400_000

SOLC_VERSION = "0.8.24"
SOL_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent / "solidity" / "AnchorRecord.sol"
)


class BlockchainConfigError(RuntimeError):
    """Raised when chain/IPFS configuration (keys, endpoints) is missing."""


class BlockchainWriteError(RuntimeError):
    """Raised when an on-chain write fails after retries — a real, typed,
    visible BLOCKCHAIN_FAILURE, never a silent success."""


# ---------------------------------------------------------------------------
# Canonical record (§4) — pure functions, no network
# ---------------------------------------------------------------------------
def canonical_json(record: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, no whitespace — hash-stable."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rebuild_content_hash(record_dict: dict[str, Any]) -> str:
    """sha256 of the record JSON minus the content_hash field, canonicalized
    (CONTRACTS.md §4 definition)."""
    without_hash = {k: v for k, v in record_dict.items() if k != "content_hash"}
    return hashlib.sha256(canonical_json(without_hash).encode("utf-8")).hexdigest()


def build_canonical_record(
    verification: VerificationOutput,
    *,
    source_url: str,
    pipeline_version: str = PIPELINE_VERSION,
) -> CanonicalRecord:
    """Build the §4 record from a verification outcome.

    Only ``sha256(candidate_url)`` enters the record — never the raw URL, and
    nothing biometric. The record model itself (``extra="forbid"``) forbids
    any embedding or raw image data from sneaking in.
    """
    record: dict[str, Any] = {
        "record_version": "1.0",
        "record_id": str(uuid.uuid4()),
        "content_cid": None,
        "source_reference_hash": hashlib.sha256(source_url.encode("utf-8")).hexdigest(),
        "verification_result": verification.decision.value,
        "verification_timestamp": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": pipeline_version,
    }
    record["content_hash"] = rebuild_content_hash(record)
    return CanonicalRecord(**record)


def with_content_cid(record: CanonicalRecord, cid: str) -> CanonicalRecord:
    """Attach an IPFS CID and recompute the content hash (the CID is part of
    the hashed object, so anchoring must happen after this step)."""
    payload = record.model_dump(mode="json")
    payload["content_cid"] = cid
    payload["content_hash"] = rebuild_content_hash(payload)
    return CanonicalRecord(**payload)


def verify_integrity(
    record_dict: dict[str, Any], on_chain: OnChainRecord
) -> dict[str, Any]:
    """Tamper-evidence check: rebuild the hash and compare to the anchored one.

    This is the demo's re-verification step: an unmodified record is intact;
    change a single field (or the underlying content) and the rebuilt hash no
    longer matches what's on-chain. Demonstrates content integrity, NOT facial
    identity — say that sentence out loud during the demo (§10).
    """
    recomputed = rebuild_content_hash(record_dict)
    return {
        "intact": recomputed == on_chain.content_hash,
        "recomputed_content_hash": recomputed,
        "onchain_content_hash": on_chain.content_hash,
    }


# ---------------------------------------------------------------------------
# IPFS pinning (Pinata) — best-effort, non-fatal (§4: content_cid nullable)
# ---------------------------------------------------------------------------
def ipfs_gateway_url(cid: str) -> str:
    gateway = os.getenv("PINATA_GATEWAY", "gateway.pinata.cloud").rstrip("/")
    return f"https://{gateway}/ipfs/{cid}"


def pin_to_ipfs(record: CanonicalRecord) -> Optional[str]:
    """Pin the canonical record JSON to IPFS via Pinata; return the CID.

    Returns ``None`` (and logs loudly) when pinning is unconfigured or fails —
    a failed pin must never fail the anchor itself, because §4 defines
    ``content_cid`` as nullable but ``content_hash`` as mandatory.
    """
    jwt = os.getenv("PINATA_JWT")
    if not jwt:
        print(
            "[BlockchainService] IPFS pin skipped — PINATA_JWT not set "
            "(content_cid will be null; see HUMAN_ACTIONS.md H2)"
        )
        return None
    try:
        resp = requests.post(
            PINATA_PIN_URL,
            headers={"Authorization": f"Bearer {jwt}"},
            json={"pinataContent": record.model_dump(mode="json")},
            timeout=PINATA_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        print(f"[BlockchainService] IPFS pin FAILED (network): {exc}")
        return None
    if resp.status_code != 200:
        print(
            f"[BlockchainService] IPFS pin FAILED: HTTP {resp.status_code} "
            f"{resp.text[:200]}"
        )
        return None
    cid = resp.json().get("IpfsHash")
    if not cid:
        print("[BlockchainService] IPFS pin FAILED: no IpfsHash in response")
        return None
    print(f"[BlockchainService] IPFS pinned: {cid}")
    return str(cid)


# ---------------------------------------------------------------------------
# Solidity toolchain (py-solc-x — pure Python, no Visual C++ needed)
# ---------------------------------------------------------------------------
_ARTIFACTS: Optional[Tuple[list[dict[str, Any]], str]] = None
_ARTIFACTS_LOCK = threading.Lock()


def compile_contract() -> Tuple[list[dict[str, Any]], str]:
    """Compile ``solidity/AnchorRecord.sol`` with solc 0.8.24 (cached)."""
    global _ARTIFACTS
    if _ARTIFACTS is None:
        with _ARTIFACTS_LOCK:
            if _ARTIFACTS is None:
                import solcx  # deferred: keeps module import light

                installed = {str(v) for v in solcx.get_installed_solc_versions()}
                if SOLC_VERSION not in installed:
                    solcx.install_solc(SOLC_VERSION)
                solcx.set_solc_version(SOLC_VERSION)
                compiled = solcx.compile_source(
                    SOL_SOURCE_PATH.read_text(encoding="utf-8"),
                    output_values=["abi", "bin"],
                    optimize=True,
                    optimize_runs=200,
                )
                artifact = next(iter(compiled.values()))
                _ARTIFACTS = (artifact["abi"], artifact["bin"])
    return _ARTIFACTS


# ---------------------------------------------------------------------------
# Chain access (web3.py 8.x, two RPC endpoints, typed failures)
# ---------------------------------------------------------------------------
def get_rpc_endpoints() -> list[str]:
    primary = os.getenv("AMOY_RPC_URL", DEFAULT_RPC_URL)
    fallback = os.getenv("AMOY_RPC_FALLBACK", DEFAULT_RPC_FALLBACK)
    return [primary, fallback]


def _get_rpc_w3() -> Any:
    """Connect to Amoy: primary RPC first, then the fallback. Typed failure
    when neither is reachable — never a silent fake success."""
    from web3 import Web3  # deferred: keeps module import light

    errors: list[str] = []
    for url in get_rpc_endpoints():
        try:
            w3 = Web3(Web3.HTTPProvider(url))
            if w3.is_connected():
                chain_id = w3.eth.chain_id
                if chain_id != AMOY_CHAIN_ID:
                    raise BlockchainWriteError(
                        f"RPC {url} reports chainId {chain_id}, expected "
                        f"{AMOY_CHAIN_ID} (Amoy) — refusing to write"
                    )
                return w3
            errors.append(f"{url}: not connected")
        except BlockchainWriteError:
            raise
        except Exception as exc:  # noqa: BLE001 — any RPC failure is retryable
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise BlockchainWriteError("All Amoy RPC endpoints failed: " + "; ".join(errors))


def _to_bytes32(hex_string: str) -> bytes:
    """Render a uuid4 or 32-byte hex hash as an EVM bytes32 value."""
    raw = hex_string.removeprefix("0x").replace("-", "")
    try:
        data = bytes.fromhex(raw)
    except ValueError as exc:
        raise BlockchainConfigError(
            f"cannot interpret {hex_string!r} as bytes32"
        ) from exc
    if len(data) > 32:
        raise BlockchainConfigError(f"hash longer than 32 bytes: {hex_string!r}")
    return data.rjust(32, b"\x00")


def _load_artifacts() -> Tuple[list[dict[str, Any]], str]:
    return compile_contract()


def anchor_record(record: CanonicalRecord) -> OnChainRecord:
    """Anchor the canonical record on Polygon Amoy and wait for the receipt.

    Returns the §4 ``OnChainRecord`` with ``confirmed=True`` only after a real
    receipt with status 1. Any failure raises a typed error that the backend
    surfaces as ``BLOCKCHAIN_FAILURE`` — never a silent success, and never a
    ``BLOCKCHAIN_CONFIRMED`` claim without a receipt.
    """
    private_key = os.getenv("AMOY_PRIVATE_KEY")
    if not private_key:
        raise BlockchainConfigError(
            "AMOY_PRIVATE_KEY is not set — a funded Amoy wallet is required "
            "to anchor (HUMAN_ACTIONS.md H3)"
        )
    if not record.content_hash:
        raise BlockchainConfigError("record has no content_hash to anchor")

    try:
        abi, bytecode = _load_artifacts()
        w3 = _get_rpc_w3()
        account = w3.eth.account.from_key(private_key)
        chain_id = int(os.getenv("AMOY_CHAIN_ID", str(AMOY_CHAIN_ID)))
        contract_address = os.getenv("AMOY_CONTRACT_ADDRESS")

        if contract_address:
            contract = w3.eth.contract(
                address=w3.to_checksum_address(contract_address), abi=abi
            )
        else:
            # One-time deploy: the contract is tiny; deploying per-environment
            # keeps the demo self-contained without a manual migration step.
            contract = _deploy(w3, account, abi, bytecode, chain_id)

        nonce = w3.eth.get_transaction_count(account.address)
        tx = contract.functions.anchorRecord(
            _to_bytes32(record.record_id),
            _to_bytes32(record.content_hash),
            record.content_cid or "",
            _to_bytes32(record.source_reference_hash),
            record.verification_result,
        ).build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "gas": ANCHOR_GAS,
                "gasPrice": w3.eth.gas_price,
                "chainId": chain_id,
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=TX_WAIT_TIMEOUT_S
        )
        if receipt.status != 1:
            raise BlockchainWriteError("anchorRecord transaction reverted")
    except (BlockchainConfigError, BlockchainWriteError):
        raise
    except Exception as exc:  # noqa: BLE001 — every other failure is a write failure
        raise BlockchainWriteError(f"{type(exc).__name__}: {exc}") from exc

    return OnChainRecord(
        record_id=record.record_id,
        content_hash=record.content_hash,
        content_cid=record.content_cid,
        source_reference_hash=record.source_reference_hash,
        verification_result=record.verification_result,
        verification_timestamp=record.verification_timestamp,
        tx_hash=tx_hash.hex(),
        block_number=int(receipt.blockNumber),
        confirmed=True,
    )


def _deploy(w3: Any, account: Any, abi: list, bytecode: str, chain_id: int) -> Any:
    """Deploy AnchorRecord and return the live contract object."""
    nonce = w3.eth.get_transaction_count(account.address)
    deploy_tx = (
        w3.eth.contract(abi=abi, bytecode=bytecode)
        .constructor()
        .build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "gas": DEPLOY_GAS,
                "gasPrice": w3.eth.gas_price,
                "chainId": chain_id,
            }
        )
    )
    signed = account.sign_transaction(deploy_tx)
    deploy_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(
        deploy_hash, timeout=TX_WAIT_TIMEOUT_S
    )
    if receipt.status != 1:
        raise BlockchainWriteError("AnchorRecord deployment reverted")
    return w3.eth.contract(address=receipt.contractAddress, abi=abi)


def read_onchain_record(record_id: str) -> OnChainRecord:
    """Read back an anchored record by id (needs AMOY_CONTRACT_ADDRESS)."""
    contract_address = os.getenv("AMOY_CONTRACT_ADDRESS")
    if not contract_address:
        raise BlockchainConfigError(
            "AMOY_CONTRACT_ADDRESS is not set — anchor_record deploys "
            "automatically, or set the address in .env"
        )
    try:
        w3 = _get_rpc_w3()
        abi, _bytecode = _load_artifacts()
        contract = w3.eth.contract(
            address=w3.to_checksum_address(contract_address), abi=abi
        )
        rec = contract.functions.getRecord(_to_bytes32(record_id)).call()
    except (BlockchainConfigError, BlockchainWriteError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise BlockchainWriteError(f"{type(exc).__name__}: {exc}") from exc
    if rec[4] == 0:  # anchoredAt == 0 → this recordId was never anchored
        raise BlockchainWriteError(f"record {record_id} is not anchored on-chain")
    return OnChainRecord(
        record_id=record_id,
        content_hash="0x" + rec[0].hex(),
        content_cid=rec[1] or None,
        source_reference_hash="0x" + rec[2].hex(),
        verification_result=str(rec[3]),
        verification_timestamp="",
        block_number=None,
        confirmed=True,
    )



