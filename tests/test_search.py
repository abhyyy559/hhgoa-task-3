"""Offline tests for SearchService (services/search.py) — CONTRACTS.md §2.

All provider HTTP calls are monkeypatched (no network access in tests).
Covers: candidate parsing/dedupe/source classification, status semantics,
retry behavior, the §2 scoring-field guard, extra="forbid" enforcement,
config errors, and the SerpAPI fallback gate.
"""
from __future__ import annotations

import hashlib
from typing import Any

import pytest
import requests
from pydantic import ValidationError

import services.search as search_module
from contracts.schemas import (
    CanonicalStatus,
    SearchOutput,
    assert_no_scoring_fields,
)
from services.search import (
    MAX_RETRIES,
    SearchConfigError,
    clear_call_log,
    get_call_log,
    search,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
FAKE_KEY = "fake-vision-key"


@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real keys, no real sleeps — fully offline + fast."""
    monkeypatch.setenv("GOOGLE_VISION_API_KEY", FAKE_KEY)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    monkeypatch.setattr(search_module.time, "sleep", lambda _s: None)
    clear_call_log()


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self) -> dict[str, Any]:
        if self._payload == {}:
            raise ValueError("no json body")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def vision_payload(web_detection: dict[str, Any]) -> dict[str, Any]:
    return {"responses": [{"webDetection": web_detection}]}


def sample_web_detection() -> dict[str, Any]:
    return {
        "fullMatchingImages": [{"url": "https://img.example.com/full/1.jpg"}],
        "pagesWithMatchingImages": [
            {"url": "https://www.facebook.com/photo?fbid=1", "pageTitle": "FB post"},
            {"url": "https://example.org/missing-person-post", "pageTitle": "News"},
            {"url": "https://www.facebook.com/photo?fbid=1"},  # duplicate URL
        ],
        "partialMatchingImages": [{"url": "https://img.example.com/partial/2.jpg"}],
        "visuallySimilarImages": [
            {"url": "https://img.example.com/partial/2.jpg"},  # duplicate URL
            {"url": "https://cdn.example.com/similar/3.jpg"},
        ],
    }



# ---------------------------------------------------------------------------
# 1. Success with candidates
# ---------------------------------------------------------------------------
def test_success_with_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(200, vision_payload(sample_web_detection()))

    monkeypatch.setattr(search_module.requests, "post", fake_post)

    out = search(b"\x89PNG-fake-bytes")

    # Exactly one outbound call, to the Vision endpoint, with the key in the URL.
    assert len(calls) == 1
    assert calls[0]["url"].startswith(
        "https://vision.googleapis.com/v1/images:annotate?key="
    )
    assert calls[0]["kwargs"]["timeout"] == 60
    body = calls[0]["kwargs"]["json"]
    assert body["requests"][0]["features"] == [{"type": "WEB_DETECTION", "maxResults": 50}]
    assert body["requests"][0]["image"]["content"]  # base64 image bytes present

    # Status semantics: candidates returned, verification NOT yet performed.
    assert out.status == CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH

    # Parsing: dedupe by URL, pages first, then partial, then visually similar.
    urls = [c.candidate_url for c in out.candidates]
    assert urls == [
        "https://www.facebook.com/photo?fbid=1",
        "https://example.org/missing-person-post",
        "https://img.example.com/partial/2.jpg",
        "https://cdn.example.com/similar/3.jpg",
    ]
    assert len(urls) == len(set(urls))  # dedupe happened

    first = out.candidates[0]
    assert first.candidate_id == hashlib.sha1(urls[0].encode()).hexdigest()[:12]
    assert first.source_type.value == "social"  # facebook.com domain
    assert first.thumbnail_url == "https://img.example.com/full/1.jpg"
    assert out.candidates[1].source_type.value == "web"  # example.org
    assert out.candidates[1].thumbnail_url == "https://img.example.com/full/1.jpg"  # page candidate
    assert out.candidates[2].source_type.value == "web"
    assert out.candidates[2].thumbnail_url is None  # partial match -> no thumbnail
    assert out.candidates[3].thumbnail_url is None  # visually similar -> no thumbnail

    # Schema-validates against the strict contract model.
    assert SearchOutput.model_validate(out.model_dump()) == out

    # Outbound-call evidence log was populated with timestamp + summary.
    log = get_call_log()
    assert len(log) == 1
    assert "T" in log[0]["request_timestamp"]
    assert "pagesWithMatchingImages" in log[0]["summary"]
    assert log[0]["fallback"]["used"] is False


# ---------------------------------------------------------------------------
# 2. Success with zero candidates
# ---------------------------------------------------------------------------
def test_empty_results_no_search_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_module.requests,
        "post",
        lambda *a, **k: FakeResponse(200, vision_payload({})),
    )
    out = search(b"\x89PNG-fake-bytes")
    assert out.status == CanonicalStatus.NO_SEARCH_RESULTS
    assert out.candidates == []
    assert SearchOutput.model_validate(out.model_dump()) == out



# ---------------------------------------------------------------------------
# 3. Repeated 5xx -> retries then SEARCH_API_FAILURE
# ---------------------------------------------------------------------------
def test_repeated_500s_search_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    sleeps: list[float] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url})
        return FakeResponse(500, {"error": {"code": 500, "status": "INTERNAL"}})

    monkeypatch.setattr(search_module.requests, "post", fake_post)
    monkeypatch.setattr(search_module.time, "sleep", sleeps.append)

    out = search(b"\x89PNG-fake-bytes")

    assert out.status == CanonicalStatus.SEARCH_API_FAILURE
    assert out.candidates == []
    # 1 initial attempt + 3 retries, with exponential backoff between them.
    assert len(calls) == MAX_RETRIES + 1
    assert sleeps == [1.0, 2.0, 4.0]
    log = get_call_log()
    assert log[0]["status"] == "SEARCH_API_FAILURE"
    assert log[0]["fallback"]["used"] is False


def test_network_errors_retry_then_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def flaky_post(url: str, **kwargs: Any) -> FakeResponse:
        attempts.append(1)
        if len(attempts) <= 2:
            raise requests.ConnectionError("transient")
        return FakeResponse(200, vision_payload(sample_web_detection()))

    monkeypatch.setattr(search_module.requests, "post", flaky_post)
    monkeypatch.setattr(search_module.time, "sleep", lambda _s: None)

    out = search(b"\x89PNG-fake-bytes")
    assert out.status == CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH
    assert len(out.candidates) == 4


# ---------------------------------------------------------------------------
# 4. CONTRACTS §2 guard — scoring-field leak is a runtime error
# ---------------------------------------------------------------------------
def test_scoring_field_leak_raises_value_error() -> None:
    leaking_payload = {
        "candidates": [],
        "status": CanonicalStatus.NO_SEARCH_RESULTS,
        "similarity_score": 0.97,  # FORBIDDEN
    }
    with pytest.raises(ValueError, match="§2 violation"):
        assert_no_scoring_fields(leaking_payload)


def test_search_never_emits_scoring_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_module.requests,
        "post",
        lambda *a, **k: FakeResponse(200, vision_payload(sample_web_detection())),
    )
    out = search(b"\x89PNG-fake-bytes")
    assert_no_scoring_fields(out.model_dump())
    dumped = out.model_dump()
    for forbidden in ("embedding", "similarity_score", "confidence", "match_decision"):
        assert forbidden not in dumped
        assert forbidden not in dumped["candidates"][0]


# ---------------------------------------------------------------------------
# 5. extra="forbid" — an embedding field cannot sneak into SearchOutput
# ---------------------------------------------------------------------------
def test_search_output_forbids_embedding_field() -> None:
    with pytest.raises(ValidationError):
        SearchOutput(
            candidates=[],
            status=CanonicalStatus.NO_SEARCH_RESULTS,
            embedding=[0.1] * 512,
        )


# ---------------------------------------------------------------------------
# 6. Config error when the key is missing
# ---------------------------------------------------------------------------
def test_missing_vision_key_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
    with pytest.raises(SearchConfigError, match="HUMAN_ACTIONS.md H1"):
        search(b"\x89PNG-fake-bytes")



# ---------------------------------------------------------------------------
# 7. SerpAPI fallback gate
# ---------------------------------------------------------------------------
def test_fallback_unavailable_without_image_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SERPAPI_KEY", "fake-serpapi-key")
    monkeypatch.setattr(
        search_module.requests,
        "post",
        lambda *a, **k: FakeResponse(503, {"error": {"status": "UNAVAILABLE"}}),
    )
    out = search(b"\x89PNG-fake-bytes")  # bytes only -> SerpAPI cannot be used

    assert out.status == CanonicalStatus.SEARCH_API_FAILURE
    assert out.candidates == []
    assert "FALLBACK UNAVAILABLE — SerpAPI needs an image URL" in capsys.readouterr().out
    log = get_call_log()
    assert log[0]["fallback"]["used"] is False
    assert "SerpAPI accepts an image URL, not image bytes" in log[0]["fallback"]["error"]


def test_fallback_used_when_image_url_provided(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SERPAPI_KEY", "fake-serpapi-key")
    monkeypatch.setattr(
        search_module.requests,
        "post",
        lambda *a, **k: FakeResponse(503, {"error": {"status": "UNAVAILABLE"}}),
    )
    monkeypatch.setattr(
        search_module.requests,
        "get",
        lambda *a, **k: FakeResponse(
            200,
            {
                "image_results": [
                    {"original": "https://news.example.org/repost"},
                    {"original": "https://vk.com/wall_1"},
                ]
            },
        ),
    )
    out = search(b"\x89PNG-fake-bytes", image_url="https://host/query.jpg")

    assert out.status == CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH
    assert [c.candidate_url for c in out.candidates] == [
        "https://news.example.org/repost",
        "https://vk.com/wall_1",
    ]
    assert out.candidates[1].source_type.value == "social"
    assert "FALLBACK USED — live Vision API unreachable" in capsys.readouterr().out
    log = get_call_log()
    assert log[0]["fallback"] == {"used": True, "provider": "serpapi", "error": None}


def test_no_fallback_when_serpapi_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_module.requests,
        "post",
        lambda *a, **k: FakeResponse(503, {"error": {"status": "UNAVAILABLE"}}),
    )

    def fail_get(*a: Any, **k: Any) -> None:
        raise AssertionError("SerpAPI must not be called without SERPAPI_KEY")

    monkeypatch.setattr(search_module.requests, "get", fail_get)
    out = search(b"\x89PNG-fake-bytes")
    assert out.status == CanonicalStatus.SEARCH_API_FAILURE



