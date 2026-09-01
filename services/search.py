"""SearchService — reverse-image retrieval ONLY (CONTRACTS.md §2).

This service's single public entry point is ``search(image_bytes) -> SearchOutput``.
It performs *retrieval only*: it never scores, ranks-by-similarity, or judges
matches. Its output is a flat candidate list handed to the backend, which owns
the VerificationService and the final verdict statuses.

Primary provider: Google Cloud Vision API Web Detection, called via raw REST
(``requests``) so the outbound call is fully visible in the call log (anti-
hardcoding evidence for the demo). Fallback provider: SerpAPI reverse-image —
used ONLY if SERPAPI_KEY is set AND the primary failed after retries.

Status semantics (see CONTRACTS.md §2 note):
  * success + zero candidates             -> NO_SEARCH_RESULTS
  * failure after retries (all providers) -> SEARCH_API_FAILURE
  * success + candidates                  -> SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH
    NOTE: this name is a contract naming limitation — it means "candidates
    returned, verification NOT yet performed"; the backend owns the final
    verdict status (e.g. SEARCH_SUCCESS_MATCH_VERIFIED). Flagged as a possible
    CONTRACTS amendment.

No secrets in code: GOOGLE_VISION_API_KEY / SERPAPI_KEY are read from the
environment via os.getenv + python-dotenv (see HUMAN_ACTIONS.md H1).
"""
from __future__ import annotations

import base64
import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from contracts.schemas import (
    CanonicalStatus,
    SearchCandidate,
    SearchOutput,
    SourceType,
    assert_no_scoring_fields,
)

load_dotenv()

__all__ = [
    "SearchConfigError",
    "SOCIAL_DOMAINS",
    "VISION_CALL_LOG",
    "search",
    "get_call_log",
    "clear_call_log",
]

VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3  # retries after the initial attempt (total attempts = 4)
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)  # exponential backoff
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})

SOCIAL_DOMAINS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "vk.com",
    "threads.net",
)


class SearchConfigError(RuntimeError):
    """Raised when required provider configuration (API keys) is missing."""


# Module-level evidence log: one entry per outbound provider call, holding the
# request timestamp plus a top-level response summary. The backend reads this
# as proof that a *live* API call actually happened (anti-hardcoding demo).
VISION_CALL_LOG: list[dict[str, Any]] = []


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_call_log() -> list[dict[str, Any]]:
    """Return a shallow copy of the outbound-call evidence log."""
    return list(VISION_CALL_LOG)


def clear_call_log() -> None:
    VISION_CALL_LOG.clear()


def _log_call(entry: dict[str, Any]) -> None:
    entry.setdefault("timestamp", _utc_now_iso())
    VISION_CALL_LOG.append(entry)
    print(
        f"[SearchService] {entry.get('provider', 'unknown')} call at "
        f"{entry['timestamp']} -> {entry.get('summary', 'no summary')}"
    )


def _domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _source_type_for(url: str) -> SourceType:
    domain = _domain_of(url)
    for social in SOCIAL_DOMAINS:
        if domain == social or domain.endswith("." + social):
            return SourceType.SOCIAL
    return SourceType.WEB


def _candidate_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]



def _build_output(
    candidates: list[SearchCandidate],
    status: CanonicalStatus,
    meta: dict[str, Any],
) -> SearchOutput:
    payload: dict[str, Any] = {
        "candidates": [c.model_dump() for c in candidates],
        "status": status,
    }
    # CONTRACTS.md §2 runtime guard: reject any scoring/verification leakage
    # (embedding, similarity_score, confidence, match_decision, ...) before the
    # payload is ever allowed to become a SearchOutput.
    assert_no_scoring_fields(payload)
    meta["status"] = status.value
    meta["candidate_count"] = len(candidates)
    meta["summary"] = meta.get("summary", f"{status.value} candidates={len(candidates)}")
    _log_call(meta)
    return SearchOutput(**payload)


def _vision_web_detection(
    image_bytes: bytes, api_key: str, meta: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Call Vision Web Detection. Returns webDetection dict on success, else None.

    Retries on network errors / HTTP 5xx / 429 with exponential backoff.
    """
    body = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [{"type": "WEB_DETECTION", "maxResults": 50}],
            }
        ]
    }
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{VISION_ENDPOINT}?key={api_key}",
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            meta.setdefault("errors", []).append(
                {"attempt": attempt + 1, "error": f"network: {exc.__class__.__name__}"}
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAYS_SECONDS[attempt])
            continue

        if resp.status_code in RETRYABLE_HTTP_STATUS:
            meta.setdefault("errors", []).append(
                {"attempt": attempt + 1, "error": f"http_{resp.status_code}"}
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAYS_SECONDS[attempt])
            continue

        if resp.status_code != 200:
            meta.setdefault("errors", []).append(
                {"attempt": attempt + 1, "error": f"http_{resp.status_code}"}
            )
            return None  # non-retryable client error (e.g. 400/403)

        data = resp.json()
        meta["summary"] = _vision_summary(data)
        if "error" in data:
            meta.setdefault("errors", []).append(
                {"attempt": attempt + 1, "error": f"api_error: {data['error'].get('status', '?')}"}
            )
            return None
        responses = data.get("responses") or []
        if not responses:
            meta.setdefault("errors", []).append(
                {"attempt": attempt + 1, "error": "empty_responses"}
            )
            return None
        web_detection = responses[0].get("webDetection")
        if web_detection is None:
            return {}  # HTTP success but no webDetection at all -> zero candidates
        if "error" in web_detection:
            meta.setdefault("errors", []).append(
                {
                    "attempt": attempt + 1,
                    "error": f"webDetection_error: {web_detection['error'].get('status', '?')}",
                }
            )
            return None
        return web_detection
    return None


def _vision_summary(data: dict[str, Any]) -> str:
    """Top-level response summary stored as outbound-call evidence."""
    if "error" in data:
        return f"error:{data['error'].get('status', 'unknown')}"
    responses = data.get("responses") or []
    if not responses:
        return "empty_responses"
    wd = responses[0].get("webDetection") or {}
    counts = {
        key: len(wd.get(key) or [])
        for key in (
            "pagesWithMatchingImages",
            "fullMatchingImages",
            "partialMatchingImages",
            "visuallySimilarImages",
        )
    }
    return f"webDetection({counts})"



def _parse_web_detection(web_detection: dict[str, Any]) -> list[SearchCandidate]:
    """Parse pages first, then partial matches, then visually similar; dedupe by URL.

    NO scoring, ranking, or similarity judgment happens here — insertion order
    is provider order, not a relevance ranking.
    """
    full_thumbnails = web_detection.get("fullMatchingImages") or []
    thumbnail_url = full_thumbnails[0].get("url") if full_thumbnails else None

    page_urls: list[str] = []
    seen: set[str] = set()
    for entry in web_detection.get("pagesWithMatchingImages") or []:
        url = entry.get("url")
        if url and url not in seen:
            seen.add(url)
            page_urls.append(url)

    ordered_urls = list(page_urls)
    for section in (
        web_detection.get("partialMatchingImages") or [],
        web_detection.get("visuallySimilarImages") or [],
    ):
        for entry in section:
            url = entry.get("url")
            if url and url not in seen:
                seen.add(url)
                ordered_urls.append(url)

    page_set = set(page_urls)
    candidates: list[SearchCandidate] = []
    for url in ordered_urls:
        candidates.append(
            SearchCandidate(
                candidate_id=_candidate_id(url),
                candidate_url=url,
                source_type=_source_type_for(url),
                # Thumbnail only for page candidates (CONTRACTS §2 note).
                thumbnail_url=thumbnail_url if url in page_set else None,
            )
        )
    return candidates


def _parse_flat_urls(urls: list[str]) -> list[SearchCandidate]:
    return [
        SearchCandidate(
            candidate_id=_candidate_id(url),
            candidate_url=url,
            source_type=_source_type_for(url),
            thumbnail_url=None,
        )
        for url in urls
    ]


def _success_status() -> CanonicalStatus:
    # CONTRACT naming limitation: this status means "candidates returned,
    # verification NOT yet performed" — the backend owns the final verdict
    # status (SEARCH_SUCCESS_MATCH_VERIFIED). SearchService never verifies.
    return CanonicalStatus.SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH


def _serpapi_fallback(
    image_url: str, serpapi_key: str, meta: dict[str, Any]
) -> SearchOutput:
    """SerpAPI reverse-image fallback. Requires an image URL (not bytes)."""
    try:
        resp = requests.get(
            SERPAPI_ENDPOINT,
            params={
                "engine": "google_reverse_image",
                "image_url": image_url,
                "api_key": serpapi_key,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        meta["fallback"] = {"used": True, "provider": "serpapi", "error": str(exc)}
        return _build_output([], CanonicalStatus.SEARCH_API_FAILURE, meta)

    ordered_urls: list[str] = []
    seen: set[str] = set()
    for item in data.get("image_results") or []:
        url = item.get("original") or item.get("link")
        if url and url not in seen:
            seen.add(url)
            ordered_urls.append(url)
    meta["summary"] = f"serpapi image_results={len(data.get('image_results') or [])}"
    meta["fallback"] = {"used": True, "provider": "serpapi", "error": None}
    return _build_output(_parse_flat_urls(ordered_urls), _success_status(), meta)



def search(image_bytes: bytes, *, image_url: Optional[str] = None) -> SearchOutput:
    """Reverse-image retrieval: candidates only, no scoring or verification.

    Args:
        image_bytes: raw query image bytes (sent base64 to Vision Web Detection).
        image_url: optional public URL for the SerpAPI fallback — SerpAPI does
            not accept raw bytes, so without this the fallback is unavailable.

    Returns:
        SearchOutput with a deduped candidate list and one of:
          * SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH — candidates returned,
            verification NOT yet performed (backend owns the verdict status)
          * NO_SEARCH_RESULTS — provider call succeeded, zero candidates
          * SEARCH_API_FAILURE — all providers failed after retries

    Raises:
        SearchConfigError: if GOOGLE_VISION_API_KEY is not configured.
    """
    api_key = os.getenv("GOOGLE_VISION_API_KEY")
    if not api_key:
        raise SearchConfigError(
            "GOOGLE_VISION_API_KEY is not set — set GOOGLE_VISION_API_KEY in .env "
            "— see HUMAN_ACTIONS.md H1"
        )

    meta: dict[str, Any] = {
        "request_timestamp": _utc_now_iso(),
        "provider": "google_vision",
        "endpoint": VISION_ENDPOINT,
        "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_retries": MAX_RETRIES,
        "errors": [],
    }
    web_detection = _vision_web_detection(image_bytes, api_key, meta)

    if web_detection is not None:
        meta["fallback"] = {"used": False, "provider": "serpapi", "error": None}
        candidates = _parse_web_detection(web_detection)
        if not candidates:
            return _build_output([], CanonicalStatus.NO_SEARCH_RESULTS, meta)
        return _build_output(candidates, _success_status(), meta)

    # Primary failed after retries — try the fallback only if configured.
    serpapi_key = os.getenv("SERPAPI_KEY")
    if not serpapi_key:
        meta["summary"] = "vision_failed_no_fallback_configured"
        meta["fallback"] = {"used": False, "provider": "serpapi", "error": "SERPAPI_KEY not set"}
        return _build_output([], CanonicalStatus.SEARCH_API_FAILURE, meta)

    if not image_url:
        message = (
            "FALLBACK UNAVAILABLE — SerpAPI needs an image URL "
            "(only raw bytes were provided); returning SEARCH_API_FAILURE"
        )
        print(f"[SearchService] {message}")
        meta["summary"] = message
        meta["fallback"] = {
            "used": False,
            "provider": "serpapi",
            "error": "SerpAPI accepts an image URL, not image bytes",
        }
        return _build_output([], CanonicalStatus.SEARCH_API_FAILURE, meta)

    used_message = "FALLBACK USED — live Vision API unreachable"
    print(f"[SearchService] {used_message}")
    meta["summary"] = used_message
    return _serpapi_fallback(image_url, serpapi_key, meta)



