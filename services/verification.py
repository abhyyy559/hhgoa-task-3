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
import urllib.parse
from typing import Optional, Tuple

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
# Thresholds (tunable constants — see module docstring)
# ---------------------------------------------------------------------------
#: Cosine similarity at or above which we call it a ``candidate_match`` /
#: ``HIGH``. 0.48 sits in the range commonly reported in the InsightFace /
#: ArcFace literature as high-confidence for ``buffalo_l`` cosine distance.
#: Empirical starting point, tunable — NOT proven-optimal for this dataset.
ACCEPT_THRESHOLD: float = 0.48

#: Cosine similarity at or above which we defer to a human (``uncertain`` /
#: ``UNCERTAIN``) instead of rejecting. Below 0.35 ArcFace cosine scores are
#: broadly considered indistinguishable from lookalikes. Empirical, tunable.
REVIEW_THRESHOLD: float = 0.35

#: Per-HTTP-request timeout in seconds (contract: 20).
HTTP_TIMEOUT_S: float = 20.0

#: Maximum number of candidate images tried for a usable face.
MAX_IMAGE_ATTEMPTS: int = 3

# ---------------------------------------------------------------------------
# Helpers (monkeypatch seams for tests; each is independently testable)
# ---------------------------------------------------------------------------
class PageFetchError(Exception):
    """Raised by ``_fetch_page`` when the candidate page cannot be fetched."""


def _fetch_page(url: str) -> Tuple[int, str]:
    """GET the candidate page; return ``(status_code, html_text)``.

    Raises ``PageFetchError`` on network failure or non-200 status. Kept as a
    separate raising helper so ``verify()`` can convert it into a decision.
    """
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:  # network / DNS / timeout
        raise PageFetchError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise PageFetchError(f"HTTP {resp.status_code}")
    return resp.status_code, resp.text


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


def _download_image(url: str) -> Optional[np.ndarray]:
    """Download and decode an image URL into a BGR ndarray, or ``None``."""
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        buf = np.frombuffer(resp.content, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except (requests.RequestException, cv2.error):
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


def _no_match(candidate_id: str, reason: str) -> VerificationOutput:
    """Every candidate-side failure is an *outcome*, never an exception."""
    return VerificationOutput(
        candidate_id=candidate_id,
        independent_similarity_score=0.0,
        zone=Zone.LOW,
        decision=VerificationDecision.NO_MATCH,
        reason=reason,
    )


def _classify(
    candidate_id: str,
    similarity: float,
    origin: str,
    *,
    multiple_faces: bool = False,
) -> VerificationOutput:
    """Map an independently computed similarity into the §3 decision zones."""
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
    if multiple_faces:
        reason += (
            " Note: the candidate image contained multiple faces; the "
            "primary (largest/most-centered) face was used."
        )
    return VerificationOutput(
        candidate_id=candidate_id,
        independent_similarity_score=float(similarity),
        zone=zone,
        decision=decision,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def verify(vinput: VerificationInput) -> VerificationOutput:
    """Independently verify one candidate against the query embedding.

    Full from-scratch pass: fetch the candidate page ourselves, extract its
    images, download them, run our own detection/encoding call, and score the
    result against ``vinput.query_embedding`` with plain cosine similarity.
    The query embedding arrived from the backend (CONTRACTS.md §1/§3) — never
    routed through SearchService, whose schema forbids carrying it.

    Never raises on candidate-side failure: unreachable pages, dead images,
    and faceless images are all ``no_match`` outcomes with an explanatory
    reason — the judge needs a decision either way. Only genuinely broken
    input (schema violations) raises. A missing local model pack re-raises
    ``VisionModelNotReadyError``: that is a server-side problem, not a
    verification outcome, and faking a ``no_match`` for it would be a silent
    misrepresentation.
    """
    candidate_id = vinput.candidate_id

    try:
        _, html_text = _fetch_page(vinput.candidate_url)
    except PageFetchError as exc:
        return _no_match(
            candidate_id, f"Candidate page unreachable — independent fetch failed ({exc})."
        )

    image_candidates = _extract_image_urls(
        html_text, vinput.candidate_url, vinput.thumbnail_url
    )
    if not image_candidates:
        return _no_match(
            candidate_id,
            "Candidate page was reachable but contains no face-image "
            "candidates to independently verify.",
        )

    # Deferred import: importing this module must stay model-free; the heavy
    # insightface/onnxruntime imports happen on the first model call only.
    from services import vision as _vision  # noqa: PLC0415

    tried = 0
    for origin, url in image_candidates[:MAX_IMAGE_ATTEMPTS]:
        tried += 1
        image = _download_image(url)
        if image is None:
            continue  # dead image URL — try the next candidate image
        try:
            detected = _vision.detect_and_encode(image)
        except _vision.VisionModelNotReadyError:
            raise  # server-side problem — surface it, never fake a decision

        if detected.status == VisionStatus.NO_FACE_DETECTED:
            continue  # this image has no face — try the next one
        if detected.status == VisionStatus.LOW_IMAGE_QUALITY:
            continue  # too weak to score honestly — try the next one
        if detected.embedding is None:
            continue

        similarity = _cosine_similarity(vinput.query_embedding, detected.embedding)
        return _classify(
            candidate_id,
            similarity,
            origin,
            multiple_faces=(detected.status == VisionStatus.MULTIPLE_FACES_DETECTED),
        )

    return _no_match(
        candidate_id,
        f"No usable face image could be extracted from the candidate page "
        f"(tried {tried} image(s)); no independent score could be computed.",
    )

