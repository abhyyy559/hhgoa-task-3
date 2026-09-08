"""SearchService — reverse-image retrieval ONLY (CONTRACTS.md §2 v3).

This service's single public entry point is ``search(image_bytes) -> SearchOutput``.
It performs *retrieval only*: it never scores, ranks-by-similarity, or judges
matches. Its output is a flat candidate list handed to the backend, which owns
the VerificationService and the final verdict statuses.

Provider decision (v3, finalized):
- Primary: SerpAPI Google Lens via image-upload endpoint (POST /image,
  multipart, 500KB cap, returns image_id — no public hosting required).
  Free tier, 250/month, no card. Sufficient on its own.
- Optional second leg: SerpAPI Yandex engine (same account/quota) for
  face-match diversity.
- Optional upgrade: Google Cloud Vision Web Detection (if GCP billing sorted),
  called via raw REST so the outbound call is visible in the call log.
- Explicitly excluded: Bing (retired Aug 2025), Google Custom Search
  (closing Jan 2027), DuckDuckGo (no genuine reverse-image capability —
  kept only as explicit 'duckduckgo' engine smoke test, never in default path).

Status semantics (CONTRACTS.md §2 v3 — SearchService may ONLY emit these):
  * success + zero candidates             -> NO_SEARCH_RESULTS
  * failure after retries (all providers) -> SEARCH_API_FAILURE
  * success + candidates                  -> SEARCH_RESULTS_FOUND
    (means "candidates returned, verification NOT yet performed";
    backend owns PIPELINE_* verdicts after VerificationService runs).

No secrets in code: SERPAPI_KEY / GOOGLE_VISION_API_KEY from env.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests
from dotenv import load_dotenv

from contracts.schemas import (
    CanonicalStatus,
    SearchCandidate,
    SearchOutput,
    SourceType,
    assert_no_scoring_fields,
)
from services.log import log as _log

# override=True: .env is authoritative; a stale OS env var must not shadow it.
load_dotenv(override=True)

__all__ = [
    "SearchConfigError",
    "SOCIAL_DOMAINS",
    "VISION_CALL_LOG",
    "search",
    "search_google_vision",
    "search_serpapi",
    "search_serpapi_lens",
    "search_serpapi_yandex",
    "search_duckduckgo",
    "search_federated_web",
    "rank_candidates",
    "resolve_identity",
    "search_enrolled_gallery",
    "federated_search",
    "get_call_log",
    "clear_call_log",
]

VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_IMAGE_ENDPOINT = "https://serpapi.com/image"
DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html/"
SERPAPI_LENS_MAX_BYTES = 500 * 1024
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3  # retries after the initial attempt (total attempts = 4)
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)  # exponential backoff
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})

# Honest, descriptive User-Agent (see services/verification.py for rationale).
DEFAULT_HEADERS = {
    "User-Agent": (
        "hhgoa-task3/1.0 (student identity-verification pipeline; "
        "public code, contact via project GitHub repo)"
    ),
}

SOCIAL_DOMAINS = (
    "instagram.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "linkedin.com",
    "reddit.com",
    "tiktok.com",
    "threads.net",
    "pinterest.com",
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
    _log("search", f"{entry.get('provider', 'unknown')} -> {entry.get('summary', 'no summary')}")


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
    *,
    web_entities: Optional[list[dict[str, Any]]] = None,
) -> SearchOutput:
    payload: dict[str, Any] = {
        "candidates": [c.model_dump() for c in candidates],
        "status": status,
        "web_entities": web_entities or [],
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
                headers=DEFAULT_HEADERS,
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


def _parse_web_entities(web_detection: dict[str, Any], *, top_n: int = 5) -> list[dict[str, Any]]:
    """Extract Google's web-entity guesses (e.g. celebrity names) — retrieval
    metadata only, never a verification judgment. Each entry: {description, score}."""
    entities: list[dict[str, Any]] = []
    for entry in web_detection.get("webEntities") or []:
        desc = entry.get("description")
        if not desc:
            continue
        try:
            score = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        entities.append({"description": str(desc)[:200], "score": score})
    entities.sort(key=lambda e: e["score"], reverse=True)
    return entities[:top_n]


def _parse_web_detection(
    web_detection: dict[str, Any], *, found_via: str = "google_vision"
) -> list[SearchCandidate]:
    """Candidate resolver (v3): actual matching IMAGE URLs first, pages last.

    Tier order (insertion order is provider order, not a relevance ranking):
      1. fullMatchingImages  — Google considers these fully matching (strongest)
      2. partialMatchingImages — cropped/partial versions
      3. pagesWithMatchingImages — page URL kept for metadata/context ONLY;
         no borrowed thumbnail (a page's og:image is often unrelated, an ad,
         or a login placeholder — comparing against it is the classic
         wrong-image failure)
      4. visuallySimilarImages — shared visual features, weakest evidence

    Dedupe by URL across tiers (first tier wins). NO scoring here.
    """
    candidates: list[SearchCandidate] = []
    seen: set[str] = set()

    def _add(url: str, tier: str, thumbnail: Optional[str]) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        candidates.append(
            SearchCandidate(
                candidate_id=_candidate_id(url),
                candidate_url=url,
                source_type=_source_type_for(url),
                thumbnail_url=thumbnail,
                is_social_domain=_source_type_for(url) == SourceType.SOCIAL,
                found_via=found_via,
                match_type=tier,
            )
        )

    # Tier 1: full image matches — the image URL IS the evidence.
    for entry in web_detection.get("fullMatchingImages") or []:
        url = entry.get("url")
        _add(url, "full_match", url)
    # Tier 2: partial/callback image matches.
    for entry in web_detection.get("partialMatchingImages") or []:
        url = entry.get("url")
        _add(url, "partial_match", url)
    # Tier 3: matching pages — metadata/context only, no borrowed thumbnail.
    for entry in web_detection.get("pagesWithMatchingImages") or []:
        _add(entry.get("url"), "page", None)
    # Tier 4: visually similar — weak supporting evidence only.
    for entry in web_detection.get("visuallySimilarImages") or []:
        url = entry.get("url")
        _add(url, "visually_similar", url)

    return candidates


def _parse_flat_urls(
    urls: list[str],
    *,
    found_via: Optional[str] = "serpapi_lens",
    thumbnails: Optional[dict[str, str]] = None,
) -> list[SearchCandidate]:
    thumbs = thumbnails or {}
    return [
        SearchCandidate(
            candidate_id=_candidate_id(url),
            candidate_url=url,
            source_type=_source_type_for(url),
            thumbnail_url=thumbs.get(url),
            is_social_domain=_source_type_for(url) == SourceType.SOCIAL,
            found_via=found_via,
        )
        for url in urls
    ]


def _success_status() -> CanonicalStatus:
    # v3: SEARCH_RESULTS_FOUND means "candidates returned, verification NOT
    # yet performed" — backend owns PIPELINE_* verdicts. Search never verifies.
    return CanonicalStatus.SEARCH_RESULTS_FOUND


#: URL/title tokens marking fan or aggregator pages (demoted, never official).
FAN_TOKENS = frozenset({
    "fan", "fans", "fanpage", "fanclub", "fcedits", "edits", "edit",
    "parody", "tribute", "fanmade", "update", "updates", "army", "fc",
})
#: URL path fragments marking aggregator/non-profile pages (demoted).
AGGREGATOR_PATHS = ("/public/", "/explore/", "/pulse/", "/watch", "fbid=", "/reel/", "/status/")


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _entity_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(t) >= 3}


def resolve_identity(
    lineup: list[dict[str, Any]],
    web_entities: list[dict[str, Any]],
) -> dict[str, Any]:
    """Best-effort identity resolution from retrieval evidence (v3).

    Celebrity track: when one entity dominates corroborations across
    candidates (entity mentions + title/username matches), resolve the name
    and rank the OFFICIAL profile — profile-root URL whose handle matches
    the entity name — above fan pages (demoted by fan tokens) and
    aggregators (demoted by path shape). No blue-check is ever claimed:
    "likely official" means name-match + profile-shape + corroboration.

    Common-person track: with no dominant entity, resolve only from
    face-verified hits (decision == candidate_match); otherwise report
    low_presence with the evidence listed. A 1-like public post IS
    resolvable the moment Lens returns it — engagement never enters scoring.

    Pure function (no network/model). Returns a ResolvedIdentity dict.
    """
    entries = [e for e in (lineup or []) if isinstance(e, dict)]
    entities: dict[str, float] = {}
    for ent in web_entities or []:
        if not isinstance(ent, dict):
            continue
        desc = str(ent.get("description", "")).strip()
        if not desc:
            continue
        try:
            sc = float(ent.get("score", 0.0))
        except (TypeError, ValueError):
            sc = 0.0
        if sc > entities.get(desc, -1.0):
            entities[desc] = sc

    def _profile_handle(url: str) -> Optional[str]:
        try:
            host = (urlparse(url).hostname or "").lower()
            parts = [p for p in urlparse(url).path.split("/") if p]
        except Exception:
            return None
        if not parts:
            return None
        if any(d in host for d in (
            "instagram.com", "x.com", "twitter.com", "tiktok.com",
            "threads.net", "pinterest.com",
        )):
            if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9_.]{2,30}", parts[0]):
                return "@" + parts[0]
            return None
        if "facebook.com" in host:
            if len(parts) == 1 and parts[0] not in ("photo", "photo.php", "people", "pages", "watch", "public", "share"):
                return "@" + parts[0]
            return None
        if "linkedin.com" in host:
            if parts[0] in ("in", "company") and len(parts) == 2:
                return "@" + parts[1]
            return None
        if "reddit.com" in host:
            if parts[0] in ("u", "user") and len(parts) == 2:
                return "@" + parts[1]
            return None
        return None

    scored: list[dict[str, Any]] = []
    for e in entries:
        url = str(e.get("candidate_url", ""))
        title = str(e.get("title") or "")
        blob = (url + " " + title).lower()
        handle = _profile_handle(url)
        is_fan = any(t in blob for t in FAN_TOKENS)
        is_agg = any(p in url for p in AGGREGATOR_PATHS)
        matched_ents: list[str] = []
        for name in entities:
            toks = _entity_tokens(name)
            if not toks:
                continue
            hn = _norm_name(handle or "")
            if any(t in hn or hn in t.replace(" ", "") for t in toks if hn):
                matched_ents.append(name)
            elif any(t in title.lower() for t in toks):
                matched_ents.append(name)
        score = 0
        signals: list[str] = []
        if handle:
            score += 2
            signals.append(f"profile-url {handle}")
        for name in matched_ents:
            score += 3
            signals.append(f"name-match '{name}'")
        if e.get("is_social_domain"):
            score += 1
            signals.append("social-domain")
        if e.get("decision") == "candidate_match":
            score += 2
            signals.append(f"face-verified {float(e.get('similarity') or 0):.2f}")
        if is_fan:
            score -= 3
            signals.append("fan-pattern demoted")
        if is_agg:
            score -= 2
            signals.append("aggregator demoted")
        scored.append({
            "entry": e, "handle": handle, "score": score,
            "signals": signals, "is_fan": is_fan, "matched": matched_ents,
        })

    # Dominant entity = corroborated by 2+ independent entries.
    corroboration: dict[str, int] = {}
    for s in scored:
        for name in s["matched"]:
            corroboration[name] = corroboration.get(name, 0) + 1
    dominant = next((n for n, c in sorted(corroboration.items(), key=lambda kv: -kv[1]) if c >= 2), None)
    if dominant is None and entities:
        top = max(entities.items(), key=lambda kv: kv[1])
        if top[1] >= 0.5:
            dominant = top[0]

    if dominant:
        cands = sorted(
            (s for s in scored if dominant in s["matched"] and s["handle"] and not s["is_fan"]),
            key=lambda s: -s["score"],
        )
        verified_hit = any(
            s["entry"].get("decision") == "candidate_match" for s in scored if dominant in s["matched"]
        )
        if cands:
            best = cands[0]
            conf = "high" if (best["score"] >= 6 and verified_hit) else "medium"
            verified_corrob = sum(
                1 for s in scored
                if dominant in s["matched"] and s["entry"].get("decision") == "candidate_match"
            )
            if conf == "high" and verified_corrob >= 2:
                officiality = "officially_linked"
            elif conf == "high":
                officiality = "strongly_supported"
            elif conf == "medium":
                officiality = "unverified"
            else:
                officiality = "unknown"
            return {
                "name": dominant,
                "kind": "public_figure",
                "handle": best["handle"],
                "profile_url": str(best["entry"].get("candidate_url")),
                "confidence": conf,
                "officiality": officiality,
                "signals": [f"dominant entity ({corroboration.get(dominant, 1)} corroborations, {verified_corrob} face-verified)"] + best["signals"],
                "alternates": [
                    {"handle": s["handle"], "profile_url": str(s["entry"].get("candidate_url")), "score": s["score"]}
                    for s in cands[1:3]
                ],
            }
        return {
            "name": dominant, "kind": "public_figure", "handle": None,
            "profile_url": None, "confidence": "low", "officiality": "unverified",
            "signals": [f"dominant entity ({corroboration.get(dominant, 1)} corroborations), no clean profile URL found"],
            "alternates": [],
        }

    # Common-person track: face-verified hits only.
    hits = sorted(
        (s for s in scored if s["entry"].get("decision") == "candidate_match"),
        key=lambda s: -float(s["entry"].get("similarity") or 0),
    )
    if hits:
        best = hits[0]
        e = best["entry"]
        return {
            "name": e.get("title") or e.get("profile_username"),
            "kind": "low_presence",
            "handle": e.get("profile_username") or best["handle"],
            "profile_url": str(e.get("candidate_url")),
            "confidence": "medium" if len(hits) >= 2 else "low",
            "officiality": "unverified",
            "signals": [f"{len(hits)} face-verified hit(s), no dominant web entity"] + best["signals"],
            "alternates": [
                {"handle": s["handle"], "profile_url": str(s["entry"].get("candidate_url")), "score": s["score"]}
                for s in hits[1:3]
            ],
        }
    return {
        "name": None, "kind": "low_presence", "handle": None,
        "profile_url": None, "confidence": "low", "officiality": "unknown",
        "signals": [f"{len(entries)} candidate(s) evaluated, no face-verified hit, no dominant entity"],
        "alternates": [],
    }


def rank_candidates(candidates: list[SearchCandidate]) -> list[SearchCandidate]:
    """Cheap pre-verification ranking (no network, no model, no scoring).

    Orders candidates so the cheapest-to-verify, highest-signal evidence is
    tried first: local gallery (fast, labeled) → direct image tiers
    (full/partial/similar — one download, no page scrape) → social pages →
    other pages → text-smoke leftovers (DDG has no image evidence at all).
    Stable sort: provider order is preserved within each tier. This is
    cost-ordering, not a match judgment — verification still decides.
    """
    def _tier(c: SearchCandidate) -> int:
        url = c.candidate_url or ""
        if "/api/gallery/" in url:
            return 0
        mt = getattr(c, "match_type", None)
        if mt == "full_match":
            return 1
        if mt == "partial_match":
            return 2
        if mt == "visually_similar":
            return 3
        if (c.found_via or "") in ("serpapi_lens", "serpapi_yandex") and not mt:
            return 3  # SerpAPI flat image results behave like image tiers
        if mt == "page":
            return 4 if c.is_social_domain else 5
        if c.found_via == "google_vision" and not mt:
            return 4
        return 6  # text-smoke / unknown provenance last

    return sorted(candidates, key=_tier)


def _serpapi_parse_results(
    data: dict[str, Any], *, found_via: str
) -> tuple[list[str], dict[str, str]]:
    """Parse SerpAPI image_results / lens_results into URLs + thumbnails.

    Supports both google_reverse_image (image_results) and google_lens /
    yandex (visual_matches / image_results with thumbnail). No scoring.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    thumbs: dict[str, str] = {}
    for section in (
        data.get("image_results") or [],
        data.get("visual_matches") or [],
        data.get("images_with_matching_images") or [],
    ):
        for item in section:
            if not isinstance(item, dict):
                continue
            url = item.get("original") or item.get("link") or item.get("source")
            if url and url not in seen:
                seen.add(url)
                ordered.append(url)
                thumb = item.get("thumbnail") or item.get("thumbnail_url")
                if thumb:
                    thumbs[url] = thumb
    return ordered, thumbs


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
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        meta["fallback"] = {"used": True, "provider": "serpapi", "error": str(exc)}
        return _build_output([], CanonicalStatus.SEARCH_API_FAILURE, meta)

    ordered_urls, thumbs = _serpapi_parse_results(data, found_via="serpapi_lens")
    meta["summary"] = f"serpapi image_results={len(ordered_urls)}"
    meta["fallback"] = {"used": True, "provider": "serpapi", "error": None}
    return _build_output(
        _parse_flat_urls(ordered_urls, found_via="serpapi_lens", thumbnails=thumbs),
        _success_status(),
        meta,
    )


def _serpapi_upload_image(image_bytes: bytes, serpapi_key: str, meta: dict[str, Any]) -> Optional[str]:
    """Upload image bytes to SerpAPI /image endpoint (v3 primary, 500KB cap).

    Returns image_id on success, else None. No public hosting required.
    """
    if len(image_bytes) > SERPAPI_LENS_MAX_BYTES:
        meta.setdefault("errors", []).append(
            f"serpapi_lens_image_too_large_{len(image_bytes)}_gt_{SERPAPI_LENS_MAX_BYTES}"
        )
        return None
    try:
        resp = requests.post(
            SERPAPI_IMAGE_ENDPOINT,
            files={"image": ("query.jpg", image_bytes, "image/jpeg")},
            data={"api_key": serpapi_key},
            headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            meta.setdefault("errors", []).append(f"serpapi_image_http_{resp.status_code}")
            return None
        data = resp.json()
        image_id = data.get("image_id") or data.get("imageId")
        if not image_id:
            meta.setdefault("errors", []).append("serpapi_image_no_image_id")
            return None
        return str(image_id)
    except (requests.RequestException, ValueError) as exc:
        meta.setdefault("errors", []).append(f"serpapi_image_error_{type(exc).__name__}")
        return None


def _serpapi_lens_search(
    image_bytes: bytes, serpapi_key: str, meta: dict[str, Any], *, engine: str = "google_lens"
) -> SearchOutput:
    """SerpAPI Lens/Yandex via image-upload (v3 primary). No image URL needed."""
    found_via = "serpapi_lens" if engine == "google_lens" else "serpapi_yandex"
    image_id = _serpapi_upload_image(image_bytes, serpapi_key, meta)
    if not image_id:
        return _build_output([], CanonicalStatus.SEARCH_API_FAILURE, meta)
    try:
        resp = requests.get(
            SERPAPI_ENDPOINT,
            params={"engine": engine, "image_id": image_id, "api_key": serpapi_key},
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        meta.setdefault("errors", []).append(f"{found_via}_search_{type(exc).__name__}")
        return _build_output([], CanonicalStatus.SEARCH_API_FAILURE, meta)
    ordered_urls, thumbs = _serpapi_parse_results(data, found_via=found_via)
    if not ordered_urls:
        return _build_output([], CanonicalStatus.NO_SEARCH_RESULTS, meta)
    meta["summary"] = f"{found_via} image_id={image_id[:8]} results={len(ordered_urls)}"
    return _build_output(
        _parse_flat_urls(ordered_urls, found_via=found_via, thumbnails=thumbs),
        _success_status(),
        meta,
    )


def _duckduckgo_scrape_urls(query: str, meta: dict[str, Any]) -> list[str]:
    """Scrape web candidate URLs from DuckDuckGo without API key or billing."""
    urls: list[str] = []
    seen: set[str] = set()
    try:
        resp = requests.post(
            DUCKDUCKGO_ENDPOINT,
            data={"q": query, "b": ""},
            headers={
                **DEFAULT_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            # Extract links formatted like /l/?uddg=https%3A%2F%2F... or direct URLs
            raw_matches = re.findall(r'href="([^"]+)"', resp.text)
            for m in raw_matches:
                target = None
                if "uddg=" in m:
                    parsed = urlparse(m)
                    qs = parse_qs(parsed.query)
                    if "uddg" in qs:
                        target = qs["uddg"][0]
                elif m.startswith("http://") or m.startswith("https://"):
                    if "duckduckgo.com" not in m:
                        target = m
                if target and target not in seen:
                    # Basic filter to ensure legitimate target domains
                    parsed_target = urlparse(target)
                    if parsed_target.scheme in ("http", "https") and "." in (parsed_target.hostname or ""):
                        seen.add(target)
                        urls.append(target)
            meta["summary"] = f"duckduckgo found {len(urls)} URLs for query '{query}'"
        else:
            meta.setdefault("errors", []).append(f"duckduckgo_status_{resp.status_code}")
    except Exception as exc:
        meta.setdefault("errors", []).append(f"duckduckgo_error: {str(exc)}")
    return urls[:25]


def _build_social_query(base_query: str) -> str:
    """Build a query focused on social media / professional profiles."""
    social_sites = " OR ".join([f"site:{d}" for d in SOCIAL_DOMAINS[:8]])
    return f"{base_query} ({social_sites})"


def search_google_vision(
    image_bytes: bytes,
    *,
    image_url: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> SearchOutput:
    """Google Cloud Vision Web Detection (primary provider)."""
    api_key = os.getenv("GOOGLE_VISION_API_KEY")
    if not api_key:
        raise SearchConfigError(
            "GOOGLE_VISION_API_KEY is not set — set GOOGLE_VISION_API_KEY in .env "
            "— see HUMAN_ACTIONS.md H1"
        )

    if meta is None:
        meta = {
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
        candidates = _parse_web_detection(web_detection, found_via="google_vision")
        entities = _parse_web_entities(web_detection)
        meta["web_entities"] = entities
        if not candidates:
            return _build_output([], CanonicalStatus.NO_SEARCH_RESULTS, meta, web_entities=entities)
        return _build_output(candidates, _success_status(), meta, web_entities=entities)

    # Primary failed after retries — try SerpAPI fallback if configured.
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
        _log("search", message)
        meta["summary"] = message
        meta["fallback"] = {
            "used": False,
            "provider": "serpapi",
            "error": "SerpAPI accepts an image URL, not image bytes",
        }
        return _build_output([], CanonicalStatus.SEARCH_API_FAILURE, meta)

    used_message = "FALLBACK USED — live Vision API unreachable"
    _log("search", used_message)
    meta["summary"] = used_message
    return _serpapi_fallback(image_url, serpapi_key, meta)


def search_serpapi(
    image_url: str,
    *,
    meta: Optional[dict[str, Any]] = None,
) -> SearchOutput:
    """SerpAPI reverse-image search (requires public image URL, legacy path)."""
    serpapi_key = os.getenv("SERPAPI_KEY")
    if not serpapi_key:
        raise SearchConfigError("SERPAPI_KEY is not set")

    if meta is None:
        meta = {
            "request_timestamp": _utc_now_iso(),
            "provider": "serpapi",
            "endpoint": SERPAPI_ENDPOINT,
            "errors": [],
        }
    return _serpapi_fallback(image_url, serpapi_key, meta)


def search_serpapi_lens(
    image_bytes: bytes,
    *,
    meta: Optional[dict[str, Any]] = None,
) -> SearchOutput:
    """SerpAPI Google Lens via image-upload (v3 primary, no URL needed)."""
    serpapi_key = os.getenv("SERPAPI_KEY")
    if not serpapi_key:
        raise SearchConfigError("SERPAPI_KEY is not set — see HUMAN_ACTIONS.md H7")
    if meta is None:
        meta = {
            "request_timestamp": _utc_now_iso(),
            "provider": "serpapi_lens",
            "endpoint": SERPAPI_IMAGE_ENDPOINT,
            "errors": [],
        }
    return _serpapi_lens_search(image_bytes, serpapi_key, meta, engine="google_lens")


def search_serpapi_yandex(
    image_bytes: bytes,
    *,
    meta: Optional[dict[str, Any]] = None,
) -> SearchOutput:
    """SerpAPI Yandex engine via image-upload (v3 optional second leg)."""
    serpapi_key = os.getenv("SERPAPI_KEY")
    if not serpapi_key:
        raise SearchConfigError("SERPAPI_KEY is not set")
    if meta is None:
        meta = {
            "request_timestamp": _utc_now_iso(),
            "provider": "serpapi_yandex",
            "endpoint": SERPAPI_IMAGE_ENDPOINT,
            "errors": [],
        }
    return _serpapi_lens_search(image_bytes, serpapi_key, meta, engine="yandex")


def search_duckduckgo(
    image_bytes_or_query: bytes | str = "",
    *,
    query: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> SearchOutput:
    """Execute free web search candidate retrieval via DuckDuckGo."""
    if meta is None:
        meta = {
            "request_timestamp": _utc_now_iso(),
            "provider": "duckduckgo",
            "endpoint": DUCKDUCKGO_ENDPOINT,
            "errors": [],
        }
    actual_query = query or (image_bytes_or_query if isinstance(image_bytes_or_query, str) else "")
    # Enhance query for social media profiles
    if actual_query and not any(site in actual_query for site in ["site:", "linkedin", "github", "twitter"]):
        actual_query = _build_social_query(actual_query)
    urls = _duckduckgo_scrape_urls(actual_query, meta) if actual_query else []
    # NOTE (v3): DuckDuckGo has no genuine reverse-image capability. This is
    # a text-query connectivity smoke test only — found_via=None so it is
    # never mistaken for a real image-to-image provider.
    candidates = _parse_flat_urls(urls, found_via=None)
    status = _success_status() if candidates else CanonicalStatus.NO_SEARCH_RESULTS
    return _build_output(candidates, status, meta)


def search_federated_web(
    image_bytes: bytes,
    *,
    image_url: Optional[str] = None,
    query: Optional[str] = None,
) -> SearchOutput:
    """Federated multi-engine retrieval (v3): SerpAPI Lens + Yandex + Vision.

    Executes multi-source candidate discovery, deduplicating candidates by URL
    while strictly complying with zero-scoring contract boundaries.
    NO gallery / enrolled identities — pure open web search.
    DuckDuckGo is NOT included (no genuine reverse-image capability).
    """
    meta: dict[str, Any] = {
        "request_timestamp": _utc_now_iso(),
        "provider": "federated_web",
        "engines": [],
        "errors": [],
    }

    all_candidates: list[SearchCandidate] = []
    seen_urls: set[str] = set()
    any_provider_ok = False
    any_provider_failed = False

    merged_entities: dict[str, float] = {}

    def _merge(result: SearchOutput, label: str) -> None:
        nonlocal any_provider_ok, any_provider_failed
        if result.status == CanonicalStatus.SEARCH_API_FAILURE:
            any_provider_failed = True
            meta["engines"].append(f"{label}(failed)")
            return
        any_provider_ok = True
        meta["engines"].append(f"{label}({len(result.candidates)})")
        for c in result.candidates:
            if c.candidate_url not in seen_urls:
                seen_urls.add(c.candidate_url)
                all_candidates.append(c)
        for ent in result.web_entities or []:
            desc = str(ent.get("description", ""))[:200]
            try:
                sc = float(ent.get("score", 0.0))
            except (TypeError, ValueError):
                sc = 0.0
            if desc and sc > merged_entities.get(desc, -1.0):
                merged_entities[desc] = sc

    # 1. SerpAPI Lens primary (if key available, works with raw bytes)
    serpapi_key = os.getenv("SERPAPI_KEY")
    if serpapi_key:
        try:
            lens_meta: dict[str, Any] = {"errors": []}
            lens_result = _serpapi_lens_search(image_bytes, serpapi_key, lens_meta, engine="google_lens")
            _merge(lens_result, "serpapi_lens")
        except Exception as exc:
            any_provider_failed = True
            meta["engines"].append(f"serpapi_lens_err({exc})")
        # 1b. SerpAPI Yandex diversity leg (same account/quota) — different
        # index, often catches faces Lens misses (low-presence people).
        # Skipped when lens already returned plenty, to save quota.
        if len(all_candidates) < 10:
            try:
                y_meta: dict[str, Any] = {"errors": []}
                y_result = _serpapi_lens_search(image_bytes, serpapi_key, y_meta, engine="yandex")
                _merge(y_result, "serpapi_yandex")
            except Exception as exc:
                any_provider_failed = True
                meta["engines"].append(f"serpapi_yandex_err({exc})")

    # 2. Google Vision Web Detection (optional upgrade, if key available)
    api_key = os.getenv("GOOGLE_VISION_API_KEY")
    if api_key:
        vision_meta: dict[str, Any] = {
            "request_timestamp": _utc_now_iso(),
            "provider": "google_vision",
            "endpoint": VISION_ENDPOINT,
            "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "max_retries": MAX_RETRIES,
            "errors": [],
        }
        try:
            wd = _vision_web_detection(image_bytes, api_key, vision_meta)
            if wd:
                v_cands = _parse_web_detection(wd, found_via="google_vision")
                v_ents = _parse_web_entities(wd)
                _merge(
                    SearchOutput(
                        candidates=v_cands,
                        status=_success_status(),
                        web_entities=v_ents,
                    ),
                    "google_vision",
                )
            else:
                any_provider_failed = True
                meta["engines"].append("google_vision(failed_or_empty)")
        except Exception as exc:
            any_provider_failed = True
            meta["engines"].append(f"google_vision_err({exc})")

    # 3. SerpAPI legacy URL fallback (if key + image URL, for compat)
    if serpapi_key and image_url:
        try:
            serpapi_meta: dict[str, Any] = {"errors": []}
            serpapi_result = _serpapi_fallback(image_url, serpapi_key, serpapi_meta)
            _merge(serpapi_result, "serpapi_url")
        except Exception as exc:
            any_provider_failed = True
            meta["engines"].append(f"serpapi_url_err({exc})")

    if all_candidates:
        status = _success_status()
    elif any_provider_ok:
        status = CanonicalStatus.NO_SEARCH_RESULTS
    elif any_provider_failed:
        status = CanonicalStatus.SEARCH_API_FAILURE
    else:
        status = CanonicalStatus.NO_SEARCH_RESULTS
    meta["summary"] = f"federated_web total_candidates={len(all_candidates)} from {','.join(meta['engines'])}"
    entities = sorted(
        ({"description": d, "score": s} for d, s in merged_entities.items()),
        key=lambda e: e["score"],
        reverse=True,
    )[:5]
    meta["web_entities"] = entities
    return _build_output(all_candidates, status, meta, web_entities=entities)


def search(
    image_bytes: bytes,
    *,
    image_url: Optional[str] = None,
    engine: str = "auto",
    query: Optional[str] = None,
    base_url: str = "http://127.0.0.1:8000",
) -> SearchOutput:
    """Reverse-image retrieval: candidates only, no scoring or verification.

    Supports multiple search backends (v3):
      - 'auto': SerpAPI Lens primary if SERPAPI_KEY set, else Vision if key set,
        else raise SearchConfigError. 'auto' string from env also maps here.
      - 'federated_web': Lens + Yandex-optional + Vision ensemble
      - 'google_vision': Google Cloud Vision Web Detection only (optional upgrade)
      - 'serpapi_lens': SerpAPI Google Lens via image-upload (primary, bytes OK)
      - 'serpapi_yandex': SerpAPI Yandex via image-upload (diversity leg)
      - 'serpapi': legacy SerpAPI reverse-image (requires image_url, compat)
      - 'duckduckgo': text-query smoke test ONLY (no real reverse-image)
      - 'gallery': Local Enrolled Identity Directory (DEMO ONLY)

    Args:
        image_bytes: raw query image bytes.
        image_url: optional public URL for legacy SerpAPI URL fallback.
        engine: search engine provider to use.
        query: optional text search query for text-assisted engines.
        base_url: host base URL for gallery engine.

    Returns:
        SearchOutput with deduped candidates and one of:
          * SEARCH_RESULTS_FOUND — candidates returned, verification NOT yet done
          * NO_SEARCH_RESULTS — provider OK, zero candidates
          * SEARCH_API_FAILURE — all providers failed after retries

    Raises:
        SearchConfigError: if required API keys are not configured.
    """
    raw_pref = engine if engine != "auto" else os.getenv("SEARCH_PROVIDER", "auto")
    # Normalize: "auto" env value means automatic selection, not federated.
    if raw_pref == "auto":
        if os.getenv("SERPAPI_KEY"):
            return search_serpapi_lens(image_bytes)
        if os.getenv("GOOGLE_VISION_API_KEY"):
            return search_google_vision(image_bytes, image_url=image_url)
        raise SearchConfigError(
            "No search provider configured — set SERPAPI_KEY (primary, H7) or "
            "GOOGLE_VISION_API_KEY in .env — see HUMAN_ACTIONS.md H1"
        )
    provider_pref = raw_pref

    # DEPRECATED: gallery engine kept for backward compatibility only
    if provider_pref == "gallery":
        from services.gallery import search_gallery_candidates
        meta = {
            "request_timestamp": _utc_now_iso(),
            "provider": "enrolled_gallery",
            "endpoint": f"{base_url.rstrip('/')}/api/gallery",
            "errors": [],
        }
        candidates = search_gallery_candidates(base_url=base_url)
        # Gallery candidates are local — found_via stays None per v3.
        for c in candidates:
            if c.found_via not in ("google_vision", "serpapi_lens", "serpapi_yandex", None):
                c.found_via = None
        status = _success_status() if candidates else CanonicalStatus.NO_SEARCH_RESULTS
        meta["summary"] = f"gallery returned {len(candidates)} candidate profiles (DEPRECATED)"
        return _build_output(candidates, status, meta)

    if provider_pref == "google_vision":
        return search_google_vision(image_bytes, image_url=image_url)

    if provider_pref in ("serpapi_lens", "serpapi-lens", "lens"):
        return search_serpapi_lens(image_bytes)

    if provider_pref in ("serpapi_yandex", "yandex"):
        return search_serpapi_yandex(image_bytes)

    if provider_pref == "serpapi":
        if image_url:
            return search_serpapi(image_url)
        # v3: bytes work via Lens upload — prefer Lens over raising.
        if os.getenv("SERPAPI_KEY"):
            return search_serpapi_lens(image_bytes)
        raise SearchConfigError("SerpAPI requires image_url parameter")

    if provider_pref == "duckduckgo":
        return search_duckduckgo(query=query or "person profile")

    if provider_pref == "federated_web":
        return search_federated_web(
            image_bytes,
            image_url=image_url,
            query=query,
        )

    # Default to federated web search
    return search_federated_web(
        image_bytes,
        image_url=image_url,
        query=query,
    )


def search_enrolled_gallery(
    base_url: str = "http://127.0.0.1:8000",
) -> SearchOutput:
    """Backward-compat wrapper for enrolled gallery (used by older tests).

    Returns SearchOutput with SEARCH_RESULTS_FOUND when members exist.
    """
    from services.gallery import search_gallery_candidates

    meta: dict[str, Any] = {
        "request_timestamp": _utc_now_iso(),
        "provider": "enrolled_gallery",
        "endpoint": f"{base_url.rstrip('/')}/api/gallery",
        "errors": [],
    }
    candidates = search_gallery_candidates(base_url=base_url)
    status = _success_status() if candidates else CanonicalStatus.NO_SEARCH_RESULTS
    meta["summary"] = f"gallery returned {len(candidates)} candidate profiles"
    return _build_output(candidates, status, meta)


def federated_search(
    image_bytes: bytes,
    *,
    image_url: Optional[str] = None,
    query: Optional[str] = None,
    base_url: str = "http://127.0.0.1:8000",
) -> SearchOutput:
    """Backward-compat wrapper: gallery + federated web merged (older tests).

    Merges enrolled-gallery candidates with federated web results, deduped
    by URL, without any scoring.
    """
    from services.gallery import search_gallery_candidates

    gallery_cands = search_gallery_candidates(base_url=base_url)
    try:
        web_out = search_federated_web(image_bytes, image_url=image_url, query=query)
        web_cands = web_out.candidates
    except SearchConfigError:
        web_cands = []
    seen: set[str] = set()
    merged: list[SearchCandidate] = []
    for c in list(gallery_cands) + list(web_cands):
        if c.candidate_url not in seen:
            seen.add(c.candidate_url)
            merged.append(c)
    meta: dict[str, Any] = {
        "request_timestamp": _utc_now_iso(),
        "provider": "federated_gallery_web",
        "engines": [f"gallery({len(gallery_cands)})", f"web({len(web_cands)})"],
        "errors": [],
    }
    status = _success_status() if merged else CanonicalStatus.NO_SEARCH_RESULTS
    meta["summary"] = f"federated_gallery_web total={len(merged)}"
    return _build_output(merged, status, meta)