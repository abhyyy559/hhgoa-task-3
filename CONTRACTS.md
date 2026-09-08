# CONTRACTS.md — Single Source of Truth (v3)

Every coding agent building a piece of this system builds against **this file**, not against another agent's implementation. If a service you're building needs something not defined here, stop and flag it rather than guessing — a guessed contract is how parallel work turns into integration hell on day 5.

Enforce these with pydantic models using `extra="forbid"` wherever a contract says a field is excluded — a leaking field should be a validation error at runtime, not something caught in code review after the fact.

---

## Status Enum — split by layer (only the owning layer may emit its values)

```
# Vision-level
NO_FACE_DETECTED
MULTIPLE_FACES_DETECTED
LOW_IMAGE_QUALITY

# Search-level — SearchService may ONLY emit these, never a "verified"/"match" status
SEARCH_RESULTS_FOUND
NO_SEARCH_RESULTS
SEARCH_API_FAILURE

# Pipeline-level — only the backend may emit these, only after VerificationService has run
PIPELINE_MATCH_VERIFIED
PIPELINE_NO_CONFIDENT_MATCH
PIPELINE_ERROR

# Blockchain-level
BLOCKCHAIN_FAILURE
BLOCKCHAIN_CONFIRMED
```

---

## 1. VisionService Output

```json
{
  "face_id": "uuid",
  "embedding": [0.0123, "... 512 floats total"],
  "bbox": [0, 0, 0, 0],
  "quality_score": 0.0,
  "status": "OK | NO_FACE_DETECTED | MULTIPLE_FACES_DETECTED | LOW_IMAGE_QUALITY"
}
```

The `embedding` here is the **query** embedding. It is held by the backend/VerificationService for later independent comparison. **It is never sent to SearchService.**

---

## 2. SearchService Output — STRICT, enforced by schema validator

```json
{
  "candidates": [
    {
      "candidate_id": "string",
      "candidate_url": "string",
      "source_type": "web | social",
      "thumbnail_url": "string",
      "is_social_domain": true
    }
  ],
  "status": "SEARCH_RESULTS_FOUND | NO_SEARCH_RESULTS | SEARCH_API_FAILURE"
}
```

**Ensemble sourcing, finalized providers:** SerpAPI (Google Lens engine via image-upload endpoint, primary — free tier, 250/month, no card required) is the primary and sufficient source on its own; SerpAPI's Yandex engine is an optional second leg on the same account/quota for face-match diversity; Google Vision Web Detection is an optional upgrade if GCP billing gets sorted, not a dependency. `found_via` records which provider surfaced each candidate. Bing (retired Aug 2025), Google Custom Search API (closing Jan 2027, closed to new signups), and DuckDuckGo (no genuine reverse-image capability) are excluded; do not add them as `found_via` values.

**`is_social_domain`:** true if `candidate_url`'s domain matches the allowlist (instagram.com, x.com, twitter.com, facebook.com, linkedin.com, reddit.com, tiktok.com, threads.net, pinterest.com). A pipeline result only counts as satisfying the task's "matching social media post" requirement if at least one returned candidate has `is_social_domain: true` — a non-social web match is supplementary, not sufficient on its own.

**Explicitly FORBIDDEN fields — a pydantic model with `extra="forbid"` must reject any of these if present:**
`embedding`, `similarity_score`, `confidence`, `match_decision`, or any other field implying SearchService scored or judged the match. SearchService's job is retrieval and tagging (social-domain only — a cheap string match, not a judgment call about identity), never verification.

**SearchService can never emit a `PIPELINE_*` status.** If your implementation has SearchService emit anything resembling "verified" or "match confirmed," that's the independence violation this file exists to prevent — stop and fix it.

---

## 3. VerificationService Input

Receives, per candidate: `candidate_id`, `candidate_url`, `thumbnail_url`, `is_social_domain` — plus the original query embedding, held separately by the backend (§1/§3), never routed through SearchService.

**Primary independent fetch target is `thumbnail_url`**, not `candidate_url` directly — most social platforms block or login-wall unauthenticated scraping of the live page, so correctness cannot depend on that succeeding. `candidate_url` may still be fetched best-effort for supplementary metadata (username, caption text), but nothing required depends on it working.

**Multi-face handling:** if the fetched thumbnail contains more than one detected face, compute similarity against every detected face and take the maximum — do not assume the target is the largest or most-centered face (that heuristic applies to the query image in VisionService, not to an arbitrary candidate image).

## VerificationService Output

```json
{
  "candidate_id": "string",
  "independent_similarity_score": 0.0,
  "zone": "HIGH | UNCERTAIN | LOW",
  "decision": "candidate_match | uncertain | no_match",
  "faces_checked_in_candidate": 1,
  "reason": "string — human-readable, this is what gets read aloud to a judge"
}
```

**On `uncertain`:** the backend retries verification against the next-ranked candidate (cap at top 5) before finalizing `PIPELINE_NO_CONFIDENT_MATCH`. `uncertain` is not a terminal state on the first candidate — it's a signal to keep going.

---

## 4. Canonical Record — BlockchainService Input

```json
{
  "record_version": "1.0",
  "record_id": "uuid",
  "content_hash": "sha256 of this object (minus this field), canonicalized per the spec below",
  "content_cid": "string | null",
  "source_reference_hash": "sha256(candidate_url)",
  "query_embedding_hash": "sha256(rounded query embedding) | null — optional, see note",
  "verification_result": "candidate_match | uncertain | no_match",
  "verification_timestamp": "ISO8601",
  "pipeline_version": "string"
}
```

**Canonicalization spec (mandatory, not optional):** `json.dumps(record, sort_keys=True, separators=(',', ':'))` before hashing. Anyone re-verifying independently must use this exact serialization, or their recomputed hash won't match for reasons that have nothing to do with tampering. Document this in the README.

**`query_embedding_hash`:** round the embedding to 4 decimal places before hashing, specifically so the hash is reproducible across runs (raw floats are not always bit-identical across hardware). One-way, non-invertible, and gives an audit link between "this specific face" and "this specific record" without ever putting anything reversible to a face on a public chain.

**Excluded from this record and from anything on-chain:** raw face embeddings, raw source URLs (only their hash), raw image bytes, submitter PII beyond what the demo strictly needs.

**Where the real URL actually lives:** the chain stores only `source_reference_hash`. The real `source_url` is retained off-chain in the Job/Event Store, keyed by `record_id` — this is what makes re-verification (re-fetch → recompute → compare) actually possible. Don't let this be implicit; a coding agent building the re-verification flow needs to know explicitly to look the URL up via `record_id`, not assume it's derivable from the chain.

---

## 5. Event Log (Data Lineage)

```json
{
  "job_id": "string",
  "stage": "face_detected | query_sent | candidates_returned | candidate_selected | verification_run | verification_result | candidate_retry | record_built | blockchain_tx_submitted | blockchain_confirmed | reverification_run",
  "timestamp": "ISO8601",
  "status": "one of the layered status enum values, or a decision value from §3",
  "detail": { "...": "stage-specific fields, e.g. candidate_id, similarity, tx_hash, found_via" }
}
```

`candidate_retry` is new in v3 — emitted whenever an `uncertain` result triggers a retry against the next candidate, so the event log shows the full attempt history, not just the final one.

---

## 6. API Surface

```
POST /api/pipeline/start       → { job_id }
GET  /api/pipeline/{job_id}/status  → { stage, status }
GET  /api/pipeline/{job_id}/result  → { final structured result incl. tx_hash, verification chain, real source_url }
```

---

## Amendment rule

Unchanged: propose changes explicitly, update this file before any other agent's in-progress work is assumed compatible with it. Versioned truth, not a suggestion.

---

## Amendment log

- **2026-09-01 — §6 status endpoint nullability.** `GET /api/pipeline/{job_id}/status` may return `"stage": null, "status": null` when the job has been accepted but the background pipeline has not yet emitted its first event. Rationale: inventing a stage/status for work that hasn't run would put a fake state into the data-lineage log, and blocking the start request would violate the async job model (§6). Reflected in `contracts/schemas.py::PipelineStatusResponse` (`stage`/`status` now `Optional`).
- **2026-09-01 — §5 event status vocabulary for the `face_detected` stage.** When the query face is cleanly accepted, the `face_detected` event's `status` is the VisionService §1 status value `"OK"` (the canonical §6 enum has no "accepted" state, and reusing a search/verification status there would be a lie). The same applies to `query_sent` (the outbound search call was made; its outcome is the next event). All other vision outcomes (`NO_FACE_DETECTED`, `MULTIPLE_FACES_DETECTED`, `LOW_IMAGE_QUALITY`) are canonical enum values already.
- **2026-09-01 — §2 SearchOutput `is_social_domain`.** Every candidate now carries `is_social_domain: bool` against a social-domain allowlist. A pipeline result only counts toward a verified match if at least one candidate has `is_social_domain: true`. This closes the gap where v2 could return a non-social web match and claim it satisfied the "matching social media post" requirement.
- **2026-09-01 — §3 VerificationService thumbnail-first fetch.** Primary independent fetch target is `thumbnail_url`, not `candidate_url`. Most social platforms block/unauthenticated-scrape the live page, so correctness cannot depend on that succeeding. The thumbnail is independently retrievable from the search API result; fetching + re-detecting + re-encoding it independently is still a fully independent computation with zero access to Search's score.
- **2026-09-01 — §3 uncertain retry.** On `uncertain` classification, retry against the next-ranked candidate (up to top 5) before finalizing `no_match`. `uncertain` is not a dead end — it signals to keep going.
- **2026-09-01 — §4 `query_embedding_hash`.** New optional field: sha256 of the query embedding rounded to 4 decimal places, for reproducibility across hardware runs without putting anything invertible on-chain.
- **2026-09-01 — §4 canonicalization spec.** `json.dumps(record, sort_keys=True, separators=(',', ':'))` before computing SHA-256. Documented so anyone re-verifying independently uses the same rule.
- **2026-09-01 — §4 real URL off-chain.** The chain stores only `source_reference_hash`. The real `source_url` is retained off-chain in the Job/Event Store, keyed by `record_id`. Re-verification looks it up via `record_id`, re-fetches, and recomputes the hash against the on-chain value. This closes the re-verification demo gap where only a hash was stored and the real URL was implied rather than explicit.
- **2026-09-01 — §5 `candidate_retry` event.** New event stage emitted when uncertain triggers a retry against the next candidate, so the data-lineage log records the full attempt history.
- **2026-09-07 — §2 social gate, enrolled-gallery exception.** The `is_social_domain` requirement applies to open-web matches. Verified matches from the enrolled gallery (`candidate_url` containing `/api/gallery/`) ARE allowed to anchor — the `record_built` event labels `match_source: enrolled_gallery | social_web | web` so a judge can tell gallery-demo proof from open-web-social proof. Rationale: team demo subjects may not have indexed public photos; blocking gallery matches would make the enrolled directory unusable while DDG/Vision are flaky. The graded social-post demo still requires `is_social_domain: true`.
- **2026-09-07 — §2 candidate resolver tiers.** Vision candidates are ordered full_match → partial_match → page → visually_similar with `match_type` provenance per candidate. Pages carry NO borrowed thumbnail (a page's og:image is routinely unrelated) — image matches carry their own URL as thumbnail. Dedupe across tiers, first tier wins.
- **2026-09-07 — §2 `web_entities`.** SearchOutput carries Google's web-entity guesses (description + score, e.g. celebrity names) as retrieval metadata, surfaced in the result as `web_entities` ("Google suggests"). Never a verification judgment.
- **2026-09-07 — §3 UNVERIFIED vs scored rejection.** `faces_checked_in_candidate == 0` with `no_match` means UNVERIFIED (no face ever scored — image unavailable/indecodable/faceless), prefixed `UNVERIFIED — ` in the reason. A genuine rejection always has faces_checked >= 1 and a real similarity. The pipeline emits `candidate_retry` for UNVERIFIED and keeps going; the UI labels it distinctly from NO_MATCH.
- **2026-09-07 — §3 cheapest-first evaluation + bounded parallel fetch.** Candidates are ranked gallery-local → direct image tiers → social pages → other pages → text-smoke last (cost ordering, not judgment; stable within tiers). Image downloads within one candidate fetch concurrently (max 3 workers); scoring stays sequential so HIGH short-circuit and attempt history are unchanged.
- **2026-09-07 — §3 metadata parses the head slice only.** Title/meta/JSON-LD live in `<head>`; regex-scanning multi-MB script-heavy bodies stalled runs (measured 1.5 s → 0.05 s on a 5 MB page with identical output). Image-URL harvesting still scans the full page. Decision + per-step timings are logged so any future stall is locatable to one line.
- **2026-09-07 — §3 same-photo vs same-face evidence label.** Scores are always face-only (ArcFace embedding of the cropped aligned face — outfit/background never enter it). Each scored hit additionally carries `same_photo` (dHash Hamming ≤ 8 → repost of the same picture) so the lineup distinguishes SAME PHOTO from SAME FACE · NEW PHOTO, the face-level proof. Lookalike fallback remains the UNCERTAIN zone (0.35–0.48, deferred to a human).
- **2026-09-07 — §3 bounded fetch (slow-drip fix).** A plain per-read timeout does not bound total transfer — one page trickled for 232 s live. Pages: 2 MB cap + 25 s wall clock; images: 15 MB cap + 30 s wall clock; overruns are UNVERIFIED outcomes, never hangs. Measured metadata parse unchanged.
- **2026-09-07 — §6 resolved-identity track.** The result carries `resolved_identity`: public_figure (dominant web entity + name-matching profile root = likely official; fan/aggregator patterns demoted; blue-checks never claimed) or low_presence (common person: face-verified hits only). Retrieval ranking, not a verdict — computed for match AND no-match runs.
- **2026-09-07 — §3 thresholds env-configurable.** `VERIFICATION_ACCEPT_THRESHOLD` / `VERIFICATION_REVIEW_THRESHOLD` override the 0.48/0.35 defaults (clamped [0,1], malformed falls back). Defaults unchanged; behavior identical unless set.
- **2026-09-07 — §5 latency is measured, not estimated.** `latency_ms` (face/search/collect/verify/record/chain/reverify/total) is derived from event-log timestamps; unreached stages are absent, never zero-filled. Retrieval/verification/chain stay separate.
- **2026-09-07 — §6 officiality vocabulary (fixed set).** `officially_linked` (name-matching profile + face-verified hit + 2+ corroborations), `strongly_supported`, `unverified`, `unknown`. `platform_verified` is intentionally never emitted — blue-checks are invisible unauthenticated, and emitting it would be fabrication.
</content>