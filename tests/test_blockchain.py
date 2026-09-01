"""BlockchainService tests — CONTRACTS.md §4.

No live chain and no live Pinata: the web3 seam (``_get_rpc_w3``) and the
solidity artifact loader are faked; Pinata is tested through a monkeypatched
``requests.post``. The solc compilation test runs the real py-solc-x toolchain
and skips (rather than fails) if the solc 0.8.24 binary cannot be fetched in
this environment.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import services.blockchain as bc
from contracts.schemas import (
    CanonicalRecord,
    OnChainRecord,
    VerificationDecision,
    VerificationOutput,
)


def make_verification(decision: str = "candidate_match") -> VerificationOutput:
    return VerificationOutput(
        candidate_id="c-1",
        independent_similarity_score=0.83,
        zone="HIGH" if decision == "candidate_match" else "LOW",
        decision=VerificationDecision(decision),
        reason="test reason",
    )


def source_url() -> str:
    return "https://page.example.com/post"


# ---------------------------------------------------------------------------
# Canonical record (§4) — pure functions
# ---------------------------------------------------------------------------
def test_record_has_no_biometric_or_raw_url_data():
    record = blockchain_record()
    dumped = record.model_dump()
    # Minimized record only: no embeddings, no raw URLs, no image bytes.
    assert set(dumped) == {
        "record_version", "record_id", "content_hash", "content_cid",
        "source_reference_hash", "verification_result",
        "verification_timestamp", "pipeline_version",
    }
    assert "https://page.example.com/post" not in canonical_str(dumped)
    assert source_url() not in canonical_str(dumped)


def test_source_reference_hash_is_sha256_of_url():
    record = blockchain_record()
    import hashlib as _h

    assert record.source_reference_hash == _h.sha256(source_url().encode()).hexdigest()


def test_content_hash_is_reproducible():
    record = blockchain_record()
    assert bc.rebuild_content_hash(record.model_dump(mode="json")) == record.content_hash


def test_tampered_record_hash_no_longer_matches():
    record = blockchain_record()
    tampered = record.model_dump(mode="json")
    tampered["verification_result"] = "no_match"  # the demo's "one pixel" analog
    assert bc.rebuild_content_hash(tampered) != record.content_hash
    verdict = bc.verify_integrity(tampered, on_chain_from(record))
    assert verdict["intact"] is False


def test_untampered_record_is_intact():
    record = blockchain_record(with_cid="bafytest123")
    on_chain = on_chain_from(record)
    verdict = bc.verify_integrity(record.model_dump(mode="json"), on_chain)
    assert verdict["intact"] is True


def test_with_content_cid_recomputes_hash():
    record = blockchain_record()
    updated = bc.with_content_cid(record, "bafytest123")
    assert updated.content_cid == "bafytest123"
    assert updated.content_hash != record.content_hash
    assert bc.rebuild_content_hash(updated.model_dump(mode="json")) == updated.content_hash


# ---------------------------------------------------------------------------
# Pinata pinning — best-effort, never fatal
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_pin_to_ipfs_success(monkeypatch):
    monkeypatch.setenv("PINATA_JWT", "test-jwt")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization")
        seen["body"] = json
        return _FakeResponse(200, {"IpfsHash": "bafyfakeCID"})

    monkeypatch.setattr(bc.requests, "post", fake_post)
    cid = bc.pin_to_ipfs(blockchain_record())
    assert cid == "bafyfakeCID"
    assert seen["url"] == bc.PINATA_PIN_URL
    assert seen["auth"] == "Bearer test-jwt"
    assert "pinataContent" in seen["body"]


def test_pin_skips_without_jwt(monkeypatch):
    monkeypatch.delenv("PINATA_JWT", raising=False)
    assert bc.pin_to_ipfs(blockchain_record()) is None


def test_pin_http_failure_returns_none(monkeypatch):
    monkeypatch.setenv("PINATA_JWT", "test-jwt")
    monkeypatch.setattr(
        bc.requests, "post", lambda *a, **k: _FakeResponse(403, {}, "forbidden")
    )
    assert bc.pin_to_ipfs(blockchain_record()) is None


def test_pin_network_failure_returns_none(monkeypatch):
    monkeypatch.setenv("PINATA_JWT", "test-jwt")

    def boom(*a, **k):
        raise bc.requests.ConnectionError("no route to host")

    monkeypatch.setattr(bc.requests, "post", boom)
    assert bc.pin_to_ipfs(blockchain_record()) is None


# ---------------------------------------------------------------------------
# Shared helpers + fake web3 objects
# ---------------------------------------------------------------------------
def canonical_str(d: dict) -> str:
    return bc.canonical_json(d)


def blockchain_record(with_cid: str | None = None) -> CanonicalRecord:
    record = bc.build_canonical_record(make_verification(), source_url=source_url())
    if with_cid:
        record = bc.with_content_cid(record, with_cid)
    return record


def on_chain_from(record: CanonicalRecord) -> OnChainRecord:
    return OnChainRecord(
        record_id=record.record_id,
        content_hash=record.content_hash,
        content_cid=record.content_cid,
        source_reference_hash=record.source_reference_hash,
        verification_result=record.verification_result,
        verification_timestamp=record.verification_timestamp,
        tx_hash="0x" + "11" * 32,
        block_number=42,
        confirmed=True,
    )


_FakeResponse = _FakeResp  # alias used above


class _FakeSigned:
    raw_transaction = b"raw-signed-tx"


class _FakeAccount:
    address = "0x" + "ab" * 20

    def sign_transaction(self, tx):
        return _FakeSigned()


class _FakeContract:
    """Mimics the tiny surface anchor_record uses."""

    def __init__(self, receipt_status: int = 1):
        self._receipt_status = receipt_status
        self.functions = self

    # constructor() and anchorRecord(...) both return self, then
    # .build_transaction(...) is called on it.
    def constructor(self):
        return self

    def anchorRecord(self, *args):
        return self

    def build_transaction(self, tx: dict) -> dict:
        return tx

    def getRecord(self, record_id: bytes):
        rec = (
            bytes.fromhex("cd" * 32),
            "bafychain",
            b"\x00" * 32,
            "candidate_match",
            1756740000,
        )
        return SimpleNamespace(call=lambda: rec)


class _FakeEth:
    chain_id = 80002
    gas_price = 30_000_000_000

    def __init__(self, receipt_status: int = 1) -> None:
        self._receipt_status = receipt_status
        self.account = SimpleNamespace(from_key=lambda pk: _FakeAccount())
        self.sent: list[bytes] = []

    def get_transaction_count(self, address: str) -> int:
        return 7

    def contract(self, address=None, abi=None, bytecode=None):
        return _FakeContract()

    def send_raw_transaction(self, raw: bytes) -> bytes:
        self.sent.append(raw)
        return b"\x11" * 32

    def wait_for_transaction_receipt(self, tx_hash, timeout=None):
        return SimpleNamespace(
            status=self._receipt_status, blockNumber=42, contractAddress="0x" + "cd" * 20
        )


class _FakeW3:
    def __init__(self, receipt_status: int = 1) -> None:
        self.eth = _FakeEth(receipt_status)

    def is_connected(self) -> bool:
        return True

    def to_checksum_address(self, address: str) -> str:
        return address


@pytest.fixture
def fake_chain(monkeypatch):
    monkeypatch.setenv("AMOY_PRIVATE_KEY", "11" * 32)  # any 32-byte secp256k1 key
    monkeypatch.setattr(bc, "_load_artifacts", lambda: ([{"inputs": []}], "0x6080"))
    return monkeypatch


# ---------------------------------------------------------------------------
# anchor_record — deploy path, configured-address path, typed failures
# ---------------------------------------------------------------------------
def test_anchor_requires_private_key(monkeypatch):
    monkeypatch.delenv("AMOY_PRIVATE_KEY", raising=False)
    with pytest.raises(bc.BlockchainConfigError):
        bc.anchor_record(blockchain_record())


def test_anchor_deploys_when_no_contract_address(fake_chain, monkeypatch):
    monkeypatch.delenv("AMOY_CONTRACT_ADDRESS", raising=False)
    w3 = _FakeW3()
    fake_chain.setattr(bc, "_get_rpc_w3", lambda: w3)
    out = bc.anchor_record(blockchain_record(with_cid="bafyfakeCID"))
    # One deploy tx + one anchor tx both hit the fake chain.
    assert len(w3.eth.sent) == 2
    assert out.confirmed is True
    assert out.tx_hash == ("11" * 32)
    assert out.block_number == 42
    assert out.content_cid == "bafyfakeCID"


def test_anchor_uses_configured_contract_address(fake_chain, monkeypatch):
    monkeypatch.setenv("AMOY_CONTRACT_ADDRESS", "0x" + "cd" * 20)
    w3 = _FakeW3()
    fake_chain.setattr(bc, "_get_rpc_w3", lambda: w3)
    out = bc.anchor_record(blockchain_record())
    assert len(w3.eth.sent) == 1  # no deploy — anchor only
    assert out.confirmed is True


def test_anchor_reverted_tx_raises_write_error(fake_chain, monkeypatch):
    monkeypatch.setenv("AMOY_CONTRACT_ADDRESS", "0x" + "cd" * 20)
    w3 = _FakeW3(receipt_status=0)
    fake_chain.setattr(bc, "_get_rpc_w3", lambda: w3)
    with pytest.raises(bc.BlockchainWriteError):
        bc.anchor_record(blockchain_record())


def test_anchor_without_rpc_raises_write_error(fake_chain, monkeypatch):
    def _no_rpc():
        raise bc.BlockchainWriteError("All Amoy RPC endpoints failed")

    fake_chain.setattr(bc, "_get_rpc_w3", _no_rpc)
    with pytest.raises(bc.BlockchainWriteError):
        bc.anchor_record(blockchain_record())


def test_anchor_wraps_unexpected_errors_as_write_error(fake_chain, monkeypatch):
    monkeypatch.setenv("AMOY_CONTRACT_ADDRESS", "0x" + "cd" * 20)
    w3 = _FakeW3()
    w3.eth.get_transaction_count = lambda addr: (_ for _ in ()).throw(
        ValueError("nonce exploded")
    )
    fake_chain.setattr(bc, "_get_rpc_w3", lambda: w3)
    with pytest.raises(bc.BlockchainWriteError, match="nonce exploded"):
        bc.anchor_record(blockchain_record())


# ---------------------------------------------------------------------------
# Solidity toolchain — real compile, skipped if solc can't be fetched
# ---------------------------------------------------------------------------
def test_solidity_source_declares_contract():
    source = bc.SOL_SOURCE_PATH.read_text(encoding="utf-8")
    assert "contract AnchorRecord" in source
    assert "function anchorRecord" in source
    assert "function isIntact" in source
    assert "0.8.24" in source


def test_solc_compile_produces_abi_and_bytecode():
    try:
        abi, bytecode = bc.compile_contract()
    except Exception as exc:  # noqa: BLE001 — network-gated binary download
        pytest.skip(f"solc 0.8.24 unavailable in this environment: {exc}")
    assert isinstance(abi, list) and abi
    assert bytecode.startswith("60")  # EVM preamble PUSH1




