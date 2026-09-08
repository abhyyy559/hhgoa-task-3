"""VerificationService — the independent cross-check (CONTRACTS.md §3).

Public API
----------
- ``verify(vinput: VerificationInput) -> VerificationOutput``: given a search
  candidate's coordinates plus the original query embedding (held separately
  by the backend, NEVER routed through SearchService), run a from-scratch,
  zero-shared-state re-verification and return the §3 payload.

Upstream contract-violation guard
---------------------------------
``VerificationInput`` is a pydantic model with ``extra="forbid"``. If any
upstream stage ever leaks a scoring field (``embedding``,
``similarity_score``, ``confidence``, ``match_decision``, ...) into the
VerificationService input — i.e. SearchService starts judging instead of
retrieving — construction raises a ``pydantic.ValidationError`` at runtime
instead of silently accepting a compromised pipeline. VerificationService
must never receive a pre-computed similarity from anywhere.

Independence guarantees (deliberate design decisions)
----------------------------------------------------
- **Fresh model call.** The query embedding that arrives in ``vinput`` is
  compared against an embedding we compute *ourselves* right now by calling
  ``services.vision.load_face_app()`` (imported lazily inside the extraction
  helper — importing this module performs no heavy work and touches no
  model). No pre-computed candidate embedding is accepted from any source;
  if one appears in the input the ``extra="forbid"`` schema rejects it.
- **Independent fetch path.** The candidate page is fetched with ``requests``
  (timeout 20 s) via our own ``_fetch_page`` helper — not via SearchService's
  HTTP layer — and image URLs are re-extracted from the raw HTML with the
  stdlib ``html.parser`` (``<img src>`` tags plus the ``og:image`` meta tag),
  with ``thumbnail_url`` kept as a fallback candidate image. Up to 3
  candidate images are tried until one downloads and decodes
  (``cv2.imdecode``).
- **Never raises on candidate-side failure.** An unreachable source page,
  a failed image download, or a page/image with no detectable face are all
  *verification outcomes* (``decision=no_match``, ``zone=LOW`` with an
  explanatory reason), not exceptions — the judge needs a decision either
  way. Only truly broken input (schema violations) raises.

Thresholds
----------
``ACCEPT_THRESHOLD`` / ``REVIEW_THRESHOLD`` are module constants with the
rationale documented inline: they follow the research consensus for
InsightFace ``buffalo_l`` ArcFace cosine similarity but are empirical and
tunable, NOT proven-optimal for this dataset.
"""

from __future__ import annotations

import html.parser
import os
import re
import time
import urllib.parse
from typing import Any, Optional, Tuple

import cv2
import numpy as np
import requests

from contracts.schemas import (
    VerificationDecision,
    VerificationInput,
    VerificationOutput,
    VisionStatus,
    Zone,
)

# ---------------------------------------------------------------------------
# Thresholds (env-configurable, documented defaults — see module docstring)
# ---------------------------------------------------------------------------
def _threshold_from_env(name: str, default: float) -> float:
    """Read a decision threshold from the environment.

    Falls back to the documented default on missing/malformed values and
    clamps to [0.0, 1.0] — a typo must never silently invert the zones.
    """
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(1.0, max(0.0, value))


#: Cosine similarity at or above which we call it a ``candidate_match`` /
#: ``HIGH``. Default 0.48 sits in the range commonly reported in the
#: InsightFace / ArcFace literature as high-confidence for ``buffalo_l``
#: cosine distance. Measured support: eval_matrix.py (same-person 0.9186,
#: different-person max 0.07). Override: VERIFICATION_ACCEPT_THRESHOLD.
ACCEPT_THRESHOLD: float = _threshold_from_env("VERIFICATION_ACCEPT_THRESHOLD", 0.48)

#: Cosine similarity at or above which we defer to a human (``uncertain`` /
#: ``UNCERTAIN``) instead of rejecting. Below the default 0.35 ArcFace cosine
#: scores are broadly considered indistinguishable from lookalikes.
#: Override: VERIFICATION_REVIEW_THRESHOLD.
REVIEW_THRESHOLD: float = _threshold_from_env("VERIFICATION_REVIEW_THRESHOLD", 0.35)

#: Per-HTTP-request timeout in seconds (contract: 20).
HTTP_TIMEOUT_S: float = 20.0

#: Maximum number of candidate images tried for a usable face.
MAX_IMAGE_ATTEMPTS: int = 3

#: Honest, descriptive User-Agent for outbound fetches. CDNs and social sites
#: frequently 403 the default ``python-requests`` UA; sending a real identifier
#: is both more reliable and more honest than impersonating a browser.
USER_AGENT = (
    "hhgoa-task3/1.0 (student identity-verification pipeline; "
    "public code, contact via project GitHub repo)"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json,image/*,*/*;q=0.8",
}

# ---------------------------------------------------------------------------
# Metadata Extraction
# ---------------------------------------------------------------------------
def _meta_content(html_text: str, attr: str, name: str) -> Optional[str]:
    """Extract <meta attr=name content=...> value (og:, twitter:, name=)."""
    pat = (
        r'<meta\s+[^>]*' + attr + r'\s*=\s*["\']' + re.escape(name) + r'["\']'
        r'[^>]*content\s*=\s*["\'](.*?)["\']'
        r'|<meta\s+[^>]*content\s*=\s*["\'](.*?)["\']'
        r'[^>]*' + attr + r'\s*=\s*["\']' + re.escape(name) + r'["\']'
    )
    m = re.search(pat, html_text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    val = m.group(1) if m.group(1) is not None else m.group(2)
    return val.strip() if val and val.strip() else None


def _username_from_url(url: str) -> Optional[str]:
    """Best-effort profile username from known social URL patterns."""
    try:
        from urllib.parse import urlparse as _up

        host = (_up(url).hostname or "").lower()
        parts = [p for p in _up(url).path.split("/") if p and p not in ("p", "reel", "post", "status")]
        if not parts:
            return None
        first = parts[0]
        if any(d in host for d in ("instagram.com", "x.com", "twitter.com", "tiktok.com", "threads.net", "pinterest.com", "github.com")):
            if re.fullmatch(r"[A-Za-z0-9_.]{1,30}", first):
                return "@" + first.lstrip("@")
        if "facebook.com" in host and first not in ("photo", "photo.php", "people", "pages", "watch"):
            return "@" + first
        if "linkedin.com" in host and parts[0] in ("in", "company") and len(parts) > 1:
            return "@" + parts[1]
        if "reddit.com" in host and parts[0] in ("u", "user") and len(parts) > 1:
            return "@" + parts[1]
    except Exception:
        return None
    return None


def _extract_metadata(html_text: str, *, page_url: str = "") -> dict[str, Any]:
    """Scrape identity metadata from candidate HTML page (best-effort).

    Keeps legacy keys (title, description, og_description, potential_handles)
    and adds: og_title, twitter_* author tags, canonical URL, JSON-LD
    author/name, and profile_username guessed from the page URL pattern.
    Never raises — returns {} when nothing found (login-walled pages).
    """
    metadata: dict[str, Any] = {}
    try:
        title_match = re.search(r"<title>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
        if title_match and title_match.group(1).strip():
            metadata["title"] = title_match.group(1).strip()

        for attr, key, out in (
            ("name", "description", "description"),
            ("property", "og:title", "og_title"),
            ("property", "og:description", "og_description"),
            ("name", "twitter:title", "twitter_title"),
            ("name", "twitter:description", "twitter_description"),
            ("name", "twitter:site", "twitter_site"),
            ("name", "twitter:creator", "twitter_creator"),
            ("property", "article:author", "article_author"),
        ):
            val = _meta_content(html_text, attr, key)
            if val:
                metadata[out] = val

        canon = re.search(
            r'<link\s+[^>]*rel\s*=\s*["\']canonical["\'][^>]*href\s*=\s*["\'](.*?)["\']'
            r'|<link\s+[^>]*href\s*=\s*["\'](.*?)["\'][^>]*rel\s*=\s*["\']canonical["\']',
            html_text,
            re.IGNORECASE,
        )
        if canon:
            href = canon.group(1) or canon.group(2)
            if href:
                metadata["canonical_url"] = href.strip()

        # JSON-LD author / name (schema.org Person/Organization blocks).
        for m in re.finditer(
            r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html_text,
            re.IGNORECASE | re.DOTALL,
        ):
            blob = m.group(1).strip()[:4000]
            try:
                import json as _json

                data = _json.loads(blob)
            except Exception:
                continue
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                author = obj.get("author")
                if isinstance(author, dict) and author.get("name"):
                    metadata.setdefault("author", str(author["name"]))
                elif isinstance(author, str) and author:
                    metadata.setdefault("author", author)
                if obj.get("name") and "name" not in metadata:
                    metadata["name"] = str(obj["name"])[:200]

        handles = re.findall(r"@([a-zA-Z0-9_]{1,30})", html_text)
        if handles:
            # Stoplist: @-tokens from inline CSS/JS/JSON-LD/font blobs that
            # are never social handles (seen live: @context @media @font …).
            junk = frozenset({
                "context", "graph", "type", "id", "media", "supports",
                "keyframes", "charset", "font", "fontface", "wordpress",
                "import", "mediaquery", "container", "root", "host",
                "slot", "part", "theme", "light", "dark", "rtl", "ltr",
                "mediafeature", "supportsquery",
            })
            seen: set[str] = set()
            ordered: list[str] = []
            for h in handles:
                hl = h.lower()
                if hl in seen or hl in junk or hl.startswith(("font", "media", "wp")):
                    continue
                seen.add(hl)
                ordered.append(h)
            if ordered:
                metadata["potential_handles"] = ordered[:20]

        if page_url:
            uname = _username_from_url(page_url)
            if uname:
                metadata.setdefault("profile_username", uname)
                hs = metadata.get("potential_handles", [])
                if uname.lstrip("@") not in hs:
                    metadata["potential_handles"] = [uname.lstrip("@")] + hs[:19]
    except Exception:
        pass
    return metadata

# ---------------------------------------------------------------------------
# Helpers (monkeypatch seams for tests; each is independently testable)
# ---------------------------------------------------------------------------
class PageFetchError(Exception):
    """Raised by ``_fetch_page`` when the candidate page cannot be fetched."""


def _bounded_get(url: str, *, max_bytes: int, deadline_s: float) -> bytes:
    """GET with an overall wall-clock deadline + size cap (anti-slow-drip).

    A single ``timeout=`` only bounds inactivity between bytes — a server
    trickling bytes (observed: 232 s for one page) slips past it. Streaming
    with an explicit deadline and byte cap bounds the TOTAL instead.
    Raises ``PageFetchError`` on any failure, non-200 status, or cap breach.
    """
    try:
        resp = requests.get(
            url, headers=DEFAULT_HEADERS, timeout=(5.0, 10.0), stream=True
        )
    except requests.RequestException as exc:  # network / DNS / connect timeout
        raise PageFetchError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise PageFetchError(f"HTTP {resp.status_code}")
    chunks: list[bytes] = []
    total = 0
    start = time.time()
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if time.time() - start > deadline_s:
                raise PageFetchError(f"fetch exceeded {deadline_s:.0f}s wall clock")
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise PageFetchError(f"payload exceeded {max_bytes // 1024}KB cap")
            chunks.append(chunk)
    except requests.RequestException as exc:
        raise PageFetchError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return b"".join(chunks)


def _fetch_page(url: str) -> Tuple[int, str]:
    """GET the candidate page; return ``(status_code, html_text)``.

    Raises ``PageFetchError`` on network failure, non-200 status, slow-drip
    overrun, or oversize payload. Kept as a separate raising helper so
    ``verify()`` can convert it into a decision.
    """
    raw = _bounded_get(url, max_bytes=2 * 1024 * 1024, deadline_s=25.0)
    try:
        return 200, raw.decode("utf-8", errors="replace")
    except Exception as exc:
        raise PageFetchError(f"decode failed: {exc}") from exc


class _ImageURLCollector(html.parser.HTMLParser):
    """Stdlib-only collector of ``<img src>`` and ``og:image`` meta URLs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[Tuple[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[Tuple[str, Optional[str]]]
    ) -> None:
        d = {k: (v or "") for k, v in attrs}
        if tag == "img":
            src = d.get("src") or d.get("data-src")
            if src:
                self.found.append(("img tag", src))
        elif tag == "meta" and d.get("property", "").lower() == "og:image":
            if d.get("content"):
                self.found.append(("og:image", d["content"]))


def _extract_image_urls(
    html_text: str, candidate_url: str, thumbnail_url: Optional[str]
) -> list[Tuple[str, str]]:
    """Extract ``(origin, absolute_url)`` image candidates from a page.

    ``origin`` is a short label used in the human-readable reason
    ("og:image", "img tag", "thumbnail"). Relative URLs are resolved against
    ``candidate_url``; non-http(s) schemes (e.g. ``data:``) are dropped.
    ``thumbnail_url`` is appended as a fallback candidate image.
    """
    parser = _ImageURLCollector()
    parser.feed(html_text)
    candidates: list[Tuple[str, str]] = []
    for origin, raw in parser.found:
        absolute = urllib.parse.urljoin(candidate_url, raw.strip())
        if absolute.startswith(("http://", "https://")):
            candidates.append((origin, absolute))
    if thumbnail_url and thumbnail_url.startswith(("http://", "https://")):
        candidates.append(("thumbnail", thumbnail_url))
    # De-duplicate, preserving order (first extraction wins).
    seen: set[str] = set()
    unique: list[Tuple[str, str]] = []
    for origin, url in candidates:
        if url not in seen:
            seen.add(url)
            unique.append((origin, url))
    return unique


#: Minimum image dimension (px) for a candidate image to be worth face
#: detection. Below this there cannot be a usable face — skip it as
#: invalid rather than scoring noise. Thumbnails from real providers and
#: the gallery are far larger; tiny placeholders/1px trackers land here.
MIN_IMAGE_DIM_PX: int = 24

#: Maximum candidate image payload (bytes) — protects the pipeline from
#: downloading multi-hundred-MB originals during a live demo.
MAX_IMAGE_BYTES: int = 15 * 1024 * 1024


def _download_image(url: str) -> Optional[np.ndarray]:
    """Download, validate, and decode an image URL into BGR, or ``None``.

    Validation gate (never score garbage): HTTP 200, non-empty payload
    within size cap and wall-clock deadline, successful decode, minimum
    dimensions. Returns None for login placeholders, tracker pixels,
    slow-drip hosts, and corrupt payloads.
    """
    try:
        content = _bounded_get(url, max_bytes=MAX_IMAGE_BYTES, deadline_s=30.0)
        if not content:
            return None
        buf = np.frombuffer(content, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None or img.ndim != 3 or img.shape[2] != 3:
            return None
        h, w = int(img.shape[0]), int(img.shape[1])
        if min(h, w) < MIN_IMAGE_DIM_PX:
            return None
        return img
    except (PageFetchError, requests.RequestException, cv2.error):
        return None

# ---------------------------------------------------------------------------
# Scoring + decision (pure functions — unit-testable without network/model)
# ---------------------------------------------------------------------------
def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity between two equal-length vectors."""
    va = np.asarray(a, dtype=np.float64).reshape(-1)
    vb = np.asarray(b, dtype=np.float64).reshape(-1)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _no_match(
    candidate_id: str, reason: str, *, faces_checked: int = 0
) -> VerificationOutput:
    """Candidate-side failure as an *outcome*, never an exception.

    When faces_checked == 0 no face was ever scored — the candidate is
    UNVERIFIED (image unavailable/indecodable/faceless), NOT a genuine
    similarity failure. The reason carries the UNVERIFIED prefix so judges
    and the event log never mistake "could not compare" for "compared and
    rejected". A scored rejection always has faces_checked >= 1.
    """
    prefix = "UNVERIFIED — " if faces_checked == 0 else ""
    return VerificationOutput(
        candidate_id=candidate_id,
        independent_similarity_score=0.0,
        zone=Zone.LOW,
        decision=VerificationDecision.NO_MATCH,
        reason=prefix + reason,
        faces_checked_in_candidate=faces_checked,
    )


def _classify(
    candidate_id: str,
    similarity: float,
    origin: str,
    extracted_metadata: dict[str, Any],
    *,
    faces_checked: int = 1,
    same_photo: bool = False,
) -> VerificationOutput:
    """Map an independently computed similarity into the §3 decision zones."""
    multiple = faces_checked > 1
    if similarity >= ACCEPT_THRESHOLD:
        zone = Zone.HIGH
        decision = VerificationDecision.CANDIDATE_MATCH
        reason = (
            f"Candidate image ({origin}) was re-fetched, re-detected and "
            f"re-encoded from scratch: cosine similarity {similarity:.3f} is "
            f"at or above the accept threshold {ACCEPT_THRESHOLD:.2f}."
        )
    elif similarity >= REVIEW_THRESHOLD:
        zone = Zone.UNCERTAIN
        decision = VerificationDecision.UNCERTAIN
        reason = (
            f"Candidate image ({origin}) scored cosine similarity "
            f"{similarity:.3f} — between the review threshold "
            f"{REVIEW_THRESHOLD:.2f} and the accept threshold "
            f"{ACCEPT_THRESHOLD:.2f}. Deferred to a human instead of "
            f"claiming a match."
        )
    else:
        zone = Zone.LOW
        decision = VerificationDecision.NO_MATCH
        reason = (
            f"Candidate image ({origin}) scored cosine similarity "
            f"{similarity:.3f}, below the review threshold "
            f"{REVIEW_THRESHOLD:.2f} — indistinguishable from a lookalike."
        )
    if multiple:
        reason += (
            f" Note: the candidate image contained multiple faces "
            f"({faces_checked} checked); similarity is the maximum across "
            f"all faces (v3 max-score)."
        )
    if same_photo:
        reason += (
            " Evidence: SAME photo reposted (near-identical image hash) — "
            "the score itself is still face-only."
        )
    elif decision is not VerificationDecision.NO_MATCH:
        reason += (
            " Evidence: DIFFERENT photo, same face (face-level match, "
            "not a repost)."
        )
    return VerificationOutput(
        candidate_id=candidate_id,
        independent_similarity_score=float(similarity),
        zone=zone,
        decision=decision,
        reason=reason,
        faces_checked_in_candidate=int(faces_checked),
        same_photo=bool(same_photo),
        extracted_metadata=extracted_metadata,
    )


# ---------------------------------------------------------------------------
# Multi-face max-score helper (v3)
# ---------------------------------------------------------------------------
def _is_same_photo(image: Any, query_phash: Optional[int]) -> bool:
    """True if the candidate image is (near-)identical to the query photo.

    dHash Hamming distance within threshold → repost/rescale/recompress of
    the same picture. None query hash → False (check skipped, not assumed).
    Never raises: a hashing failure simply reports False.
    """
    if query_phash is None:
        return False
    try:
        from services import vision as _vision  # noqa: PLC0415

        dist = _vision.phash_distance(_vision.phash_bgr(image), int(query_phash))
        return dist <= _vision.SAME_PHOTO_HAMMING_THRESHOLD
    except Exception:
        return False


def _score_image_multi(
    image: Any,
    query_embedding: list[float],
    query_phash: Optional[int] = None,
) -> tuple[Optional[float], int, bool]:
    """Score one image against the query, max over ALL faces (v3).

    Returns (best_similarity_or_None, faces_checked, same_photo). Uses
    vision.encode_all_faces for true max-score; falls back to the legacy
    detect_and_encode single-primary path so existing unit-test mocks
    (which patch detect_and_encode) keep working.
    Raises VisionModelNotReadyError when the pack is missing (server problem).
    """
    from services import vision as _vision  # noqa: PLC0415

    same = _is_same_photo(image, query_phash)
    # Preferred v3 path: all faces, max-score.
    try:
        all_faces = _vision.encode_all_faces(image)
    except _vision.VisionModelNotReadyError:
        raise
    except Exception:
        all_faces = []
    if all_faces:
        sims = [
            _cosine_similarity(query_embedding, f.embedding)
            for f in all_faces
            if f.embedding is not None
        ]
        if sims:
            return max(sims), len(sims), same
    # Legacy fallback (test-mock compatible): single primary face.
    try:
        detected = _vision.detect_and_encode(image)
    except _vision.VisionModelNotReadyError:
        raise
    if detected.status in (
        VisionStatus.NO_FACE_DETECTED,
        VisionStatus.LOW_IMAGE_QUALITY,
    ):
        return None, 0, False
    if detected.embedding is None:
        return None, 0, False
    sim = _cosine_similarity(query_embedding, detected.embedding)
    faces_n = 2 if detected.status == VisionStatus.MULTIPLE_FACES_DETECTED else 1
    return sim, faces_n, same


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def verify(vinput: VerificationInput) -> VerificationOutput:
    """Independently verify one candidate against the query embedding.

    v3 thumbnail-first (CONTRACTS.md §3): the primary independent fetch
    target is ``thumbnail_url`` as an *image* (download + re-detect +
    re-encode, max-score over all faces). Most social platforms block
    unauthenticated live-page scraping, so correctness cannot depend on
    that succeeding. ``candidate_url`` page fetch remains as best-effort
    enrichment (metadata + additional image candidates).

    Never raises on candidate-side failure: unreachable pages, dead images,
    and faceless images are all ``no_match`` outcomes. Only genuinely broken
    input (schema violations) or missing model pack raises.
    """
    from services import vision as _vision  # noqa: PLC0415

    candidate_id = vinput.candidate_id

    from services.log import log as _log  # noqa: PLC0415

    # ---- Primary: thumbnail_url as image (v3) ---------------------------
    if vinput.thumbnail_url:
        _log("verify", f"{candidate_id}: thumbnail download ...")
        thumb_img = _download_image(vinput.thumbnail_url)
        if thumb_img is not None:
            _log("verify", f"{candidate_id}: thumbnail {thumb_img.shape[1]}x{thumb_img.shape[0]}, scoring ...")
            _t0 = time.time()
            try:
                best, n_faces, thumb_same = _score_image_multi(
                    thumb_img, vinput.query_embedding, vinput.query_phash
                )
            except _vision.VisionModelNotReadyError:
                raise
            _log("verify", f"{candidate_id}: thumbnail scored in {time.time() - _t0:.1f}s")
            if best is not None:
                # Best-effort metadata from live page (never required).
                try:
                    _t0 = time.time()
                    _, html_text = _fetch_page(vinput.candidate_url)
                    # Parse the head slice only: title/meta/JSON-LD all live
                    # in <head> (first ~200KB). Regex-scanning multi-MB bodies
                    # can stall for minutes on script-heavy pages.
                    _t1 = time.time()
                    extracted = _extract_metadata(html_text[:200_000], page_url=vinput.candidate_url)
                    _log("verify", f"{candidate_id}: page {time.time() - _t0:.1f}s, metadata {time.time() - _t1:.1f}s")
                except PageFetchError:
                    extracted = {}
                    uname = _username_from_url(vinput.candidate_url)
                    if uname:
                        extracted = {"profile_username": uname, "potential_handles": [uname.lstrip("@")]}
                out = _classify(
                    candidate_id,
                    best,
                    "thumbnail",
                    extracted,
                    faces_checked=n_faces,
                    same_photo=thumb_same,
                )
                _log("verify", f"{candidate_id}: decision={out.decision.value} sim={out.independent_similarity_score:.3f}")
                return out
            # Thumbnail had no usable face — fall through to page path.

    # ---- Fallback: live page for metadata + image candidates ------------
    _log("verify", f"{candidate_id}: fetching page {vinput.candidate_url[:80]} ...")
    try:
        _t0 = time.time()
        _, html_text = _fetch_page(vinput.candidate_url)
        _log("verify", f"{candidate_id}: page fetched in {time.time() - _t0:.1f}s")
    except PageFetchError as exc:
        # If thumbnail already tried and failed, report both.
        if vinput.thumbnail_url:
            return _no_match(
                candidate_id,
                f"Candidate thumbnail had no usable face and page unreachable "
                f"— independent fetch failed ({exc}).",
            )
        return _no_match(
            candidate_id,
            f"Candidate page unreachable — independent fetch failed ({exc}).",
        )

    _t0 = time.time()
    extracted_metadata = _extract_metadata(html_text[:200_000], page_url=vinput.candidate_url)
    _log("verify", f"{candidate_id}: metadata parsed in {time.time() - _t0:.1f}s")

    image_candidates = _extract_image_urls(
        html_text, vinput.candidate_url, vinput.thumbnail_url
    )
    if not image_candidates:
        return _no_match(
            candidate_id,
            "Candidate page was reachable but contains no face-image "
            "candidates to independently verify.",
        )

    targets = image_candidates[:MAX_IMAGE_ATTEMPTS]
    # Bounded parallel fetch (max 3 workers): downloads overlap instead of
    # stacking 20 s timeouts sequentially. Scoring stays sequential in
    # candidate order below, so HIGH short-circuit + attempt history are
    # unchanged. requests + cv2.imdecode are thread-safe for separate calls.
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    _log("verify", f"{candidate_id}: fetching {len(targets)} image(s), up to 3 workers")
    with ThreadPoolExecutor(max_workers=MAX_IMAGE_ATTEMPTS) as _pool:
        fetched = list(_pool.map(_download_image, [u for _, u in targets]))

    tried = 0
    best_overall: Optional[float] = None
    best_origin = ""
    best_faces = 1
    best_same_photo = False
    for (origin, url), image in zip(targets, fetched):
        tried += 1
        if image is None:
            _log("verify", f"{candidate_id}: try {tried}/{len(targets)} ({origin}) no image, next")
            continue
        try:
            sim, n_faces, same = _score_image_multi(image, vinput.query_embedding, vinput.query_phash)
        except _vision.VisionModelNotReadyError:
            raise
        if sim is None:
            _log("verify", f"{candidate_id}: try {tried}/{len(targets)} ({origin}) no face, next")
            continue
        _log("verify", f"{candidate_id}: try {tried}/{len(targets)} ({origin}) sim={sim:.3f} faces={n_faces}")
        if best_overall is None or sim > best_overall:
            best_overall = sim
            best_origin = origin
            best_faces = n_faces
            best_same_photo = same
        # HIGH short-circuit: already a confident match, no need to try more.
        if sim >= ACCEPT_THRESHOLD:
            break

    if best_overall is not None:
        out = _classify(
            candidate_id,
            best_overall,
            best_origin,
            extracted_metadata,
            faces_checked=best_faces,
            same_photo=best_same_photo,
        )
        _log("verify", f"{candidate_id}: decision={out.decision.value} sim={out.independent_similarity_score:.3f}")
        return out

    return _no_match(
        candidate_id,
        f"No usable face image could be extracted from the candidate page "
        f"(tried {tried} image(s)); no independent score could be computed.",
    )
