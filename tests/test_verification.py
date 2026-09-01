"""VerificationService tests — CONTRACTS.md §3.

No live network, no model download: every seam (``_fetch_page``,
``_download_image``, and ``services.vision.detect_and_encode`` — imported
lazily inside ``verify``, so patching the module attribute reaches it) is
monkeypatched. Controlled-cosine embedding pairs make the threshold zones
exactly assertable.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from pydantic import ValidationError

import services.vision as vision
from contracts.schemas import (
    VerificationInput,
    VerificationOutput,
    VisionOutput,
    VisionStatus,
)
from services import verification as ver

PAGE_HTML = (
    "<html><head>"
    '<meta property="og:image" content="https://img.example.com/og.jpg">'
    "</head><body>"
    '<img src="https://img.example.com/first.jpg">'
    '<img src="/relative/second.jpg">'
    "</body></html>"
)


def make_pair(cosine_target: float) -> tuple[list[float], list[float]]:
    """Two 512-dim unit-ish vectors whose cosine similarity is exact."""
    q = np.zeros(512)
    q[0] = 1.0
    c = np.zeros(512)
    c[0] = cosine_target
    c[1] = math.sqrt(max(0.0, 1.0 - cosine_target**2))
    return [float(v) for v in q], [float(v) for v in c]


def fake_detect(
    status: VisionStatus = VisionStatus.OK,
    embedding: list[float] | None = None,
    results: list[VisionOutput] | None = None,
):
    """Build a detect_and_encode replacement, optionally returning a
    sequence of outputs across successive calls."""
    calls = {"n": 0}

    def _run(image_bgr):
        if results is not None:
            out = results[min(calls["n"], len(results) - 1)]
            calls["n"] += 1
            return out
        return VisionOutput(
            face_id="face-1",
            embedding=embedding,
            bbox=[0, 0, 10, 10],
            quality_score=0.2 if status == VisionStatus.LOW_IMAGE_QUALITY else 0.9,
            status=status,
        )

    return _run


def make_vinput(query: list[float], cid: str = "c-1") -> VerificationInput:
    return VerificationInput(
        candidate_id=cid,
        candidate_url="https://page.example.com/post",
        query_embedding=query,
    )


@pytest.fixture
def good_page(monkeypatch):
    monkeypatch.setattr(ver, "_fetch_page", lambda url: (200, PAGE_HTML))


@pytest.fixture
def downloadable_images(monkeypatch):
    monkeypatch.setattr(
        ver, "_download_image", lambda url: np.zeros((16, 16, 3), dtype=np.uint8)
    )


# ---------------------------------------------------------------------------
# Decision zones (exact, controlled cosine similarity)
# ---------------------------------------------------------------------------
def test_high_similarity_is_candidate_match(good_page, downloadable_images, monkeypatch):
    query, cand = make_pair(0.90)
    monkeypatch.setattr(vision, "detect_and_encode", fake_detect(embedding=cand))
    out = ver.verify(make_vinput(query))
    assert out.decision.value == "candidate_match"
    assert out.zone.value == "HIGH"
    assert out.independent_similarity_score == pytest.approx(0.90)
    assert "accept threshold" in out.reason


def test_mid_similarity_is_uncertain(good_page, downloadable_images, monkeypatch):
    query, cand = make_pair(0.40)
    monkeypatch.setattr(vision, "detect_and_encode", fake_detect(embedding=cand))
    out = ver.verify(make_vinput(query))
    assert out.decision.value == "uncertain"
    assert out.zone.value == "UNCERTAIN"
    assert out.independent_similarity_score == pytest.approx(0.40)


def test_low_similarity_is_no_match(good_page, downloadable_images, monkeypatch):
    query, cand = make_pair(0.10)
    monkeypatch.setattr(vision, "detect_and_encode", fake_detect(embedding=cand))
    out = ver.verify(make_vinput(query))
    assert out.decision.value == "no_match"
    assert out.zone.value == "LOW"
    assert out.independent_similarity_score == pytest.approx(0.10)


def test_reason_mentions_multiple_faces(good_page, downloadable_images, monkeypatch):
    query, cand = make_pair(0.9)
    monkeypatch.setattr(
        vision,
        "detect_and_encode",
        fake_detect(status=VisionStatus.MULTIPLE_FACES_DETECTED, embedding=cand),
    )
    out = ver.verify(make_vinput(query))
    assert out.decision.value == "candidate_match"
    assert "multiple faces" in out.reason


# ---------------------------------------------------------------------------
# Candidate-side failures are outcomes, never exceptions
# ---------------------------------------------------------------------------
def test_unreachable_page_is_no_match(monkeypatch):
    def _boom(url):
        raise ver.PageFetchError("HTTP 503")

    monkeypatch.setattr(ver, "_fetch_page", _boom)
    out = ver.verify(make_vinput(make_pair(0.9)[0]))
    assert out.decision.value == "no_match"
    assert out.zone.value == "LOW"
    assert "unreachable" in out.reason


def test_page_without_images_is_no_match(monkeypatch):
    monkeypatch.setattr(ver, "_fetch_page", lambda url: (200, "<html><body>hi</body></html>"))
    out = ver.verify(make_vinput(make_pair(0.9)[0]))
    assert out.decision.value == "no_match"
    assert "no face-image candidates" in out.reason


def test_dead_images_exhaust_attempts_is_no_match(good_page, monkeypatch):
    monkeypatch.setattr(ver, "_download_image", lambda url: None)
    out = ver.verify(make_vinput(make_pair(0.9)[0]))
    assert out.decision.value == "no_match"
    assert f"tried {ver.MAX_IMAGE_ATTEMPTS}" in out.reason


def test_faceless_image_then_good_image_succeeds(good_page, downloadable_images, monkeypatch):
    no_face = VisionOutput(
        face_id="f0", embedding=None, bbox=None, quality_score=0.0,
        status=VisionStatus.NO_FACE_DETECTED,
    )
    query, cand = make_pair(0.85)
    good = VisionOutput(
        face_id="f1", embedding=cand, bbox=[0, 0, 9, 9], quality_score=0.9,
        status=VisionStatus.OK,
    )
    monkeypatch.setattr(vision, "detect_and_encode", fake_detect(results=[no_face, good]))
    out = ver.verify(make_vinput(query))
    assert out.decision.value == "candidate_match"
    assert out.independent_similarity_score == pytest.approx(0.85)


def test_low_quality_image_is_skipped(good_page, downloadable_images, monkeypatch):
    weak = VisionOutput(
        face_id="f0", embedding=None, bbox=[0, 0, 4, 4], quality_score=0.2,
        status=VisionStatus.LOW_IMAGE_QUALITY,
    )
    query, cand = make_pair(0.7)
    good = VisionOutput(
        face_id="f1", embedding=cand, bbox=[0, 0, 9, 9], quality_score=0.9,
        status=VisionStatus.OK,
    )
    monkeypatch.setattr(vision, "detect_and_encode", fake_detect(results=[weak, good]))
    out = ver.verify(make_vinput(query))
    assert out.decision.value == "candidate_match"


def test_missing_model_pack_is_raised_not_faked(good_page, downloadable_images, monkeypatch):
    def _boom(image_bgr):
        raise vision.VisionModelNotReadyError("model pack missing")

    monkeypatch.setattr(vision, "detect_and_encode", _boom)
    with pytest.raises(vision.VisionModelNotReadyError):
        ver.verify(make_vinput(make_pair(0.9)[0]))


# ---------------------------------------------------------------------------
# Schema-level independence enforcement (extra="forbid")
# ---------------------------------------------------------------------------
def test_input_rejects_leaked_similarity_field():
    query, _cand = make_pair(0.9)
    payload = {
        "candidate_id": "c-1",
        "candidate_url": "https://page.example.com/post",
        "query_embedding": query,
        "similarity_score": 0.99,  # SearchService must never be able to send this
    }
    with pytest.raises(ValidationError):
        VerificationInput(**payload)


def test_input_rejects_wrong_embedding_length():
    with pytest.raises(ValidationError):
        make_vinput([0.1] * 511)


def test_similarity_helper():
    a, b = make_pair(0.5)
    assert ver._cosine_similarity(a, b) == pytest.approx(0.5)
    assert ver._cosine_similarity([0.0] * 512, [0.0] * 512) == 0.0


