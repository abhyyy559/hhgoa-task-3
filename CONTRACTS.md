# CONTRACTS.md — Single Source of Truth

Every coding agent building a piece of this system builds against **this file**, not against another agent's implementation. If a service you're building needs something not defined here, stop and flag it rather than guessing — a guessed contract is how parallel work turns into integration hell on day 5.

Enforce these with pydantic models using `extra="forbid"` wherever a contract says a field is excluded — a leaking field should be a validation error at runtime, not something caught in code review after the fact.

---

## Canonical Status Enum (used identically everywhere — backend, event log, README, defense pack)

```
NO_FACE_DETECTED
MULTIPLE_FACES_DETECTED
LOW_IMAGE_QUALITY
SEARCH_SUCCESS_MATCH_VERIFIED
SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH
NO_SEARCH_RESULTS
SEARCH_API_FAILURE
VERIFICATION_FAILED
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
      "thumbnail_url": "string"
    }
  ],
  "status": "SEARCH_SUCCESS_MATCH_VERIFIED | SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH | NO_SEARCH_RESULTS | SEARCH_API_FAILURE"
}
```

**Explicitly FORBIDDEN fields in this payload — a pydantic model with `extra="forbid"` must reject any of these if present:**
`embedding`, `similarity_score`, `confidence`, `match_decision`, or any other field implying SearchService scored or judged the match. SearchService's job is retrieval, not verification — the schema enforces this, not just the agent building it.

Note: despite the field names above using "SEARCH_SUCCESS_MATCH_VERIFIED", SearchService itself never verifies anything — that status naming is reserved for the *backend's* post-verification state, not something SearchService is allowed to emit. SearchService should only ever emit `NO_SEARCH_RESULTS` or `SEARCH_API_FAILURE` as terminal states, or return candidates for downstream verification. (If your implementation has SearchService emit `SEARCH_SUCCESS_MATCH_VERIFIED` directly, that's the independence violation this file exists to prevent — stop and fix it.)

---

## 3. VerificationService Input

Receives **only** `candidate_id` + `candidate_url` (+ `thumbnail_url` if useful for its own independent fetch) from the candidate list above, plus the original query embedding held separately by the backend (§1) — never routed through SearchService.

## VerificationService Output

```json
{
  "candidate_id": "string",
  "independent_similarity_score": 0.0,
  "zone": "HIGH | UNCERTAIN | LOW",
  "decision": "candidate_match | uncertain | no_match",
  "reason": "string — human-readable, this is what gets read aloud to a judge"
}
```

---

## 4. Canonical Record — BlockchainService Input (minimized, no biometric data)

```json
{
  "record_version": "1.0",
  "record_id": "uuid",
  "content_hash": "sha256 of this JSON object (minus this field) canonicalized",
  "content_cid": "string | null",
  "source_reference_hash": "sha256(candidate_url) — never the raw URL",
  "verification_result": "candidate_match | uncertain | no_match",
  "verification_timestamp": "ISO8601",
  "pipeline_version": "string"
}
```

**Explicitly EXCLUDED from this record and from anything written on-chain:** face embeddings, raw source URLs, raw image bytes, any submitter PII beyond what the demo strictly needs.

---

## 5. Event Log (Data Lineage) — one line per pipeline stage, every service emits these

```json
{
  "job_id": "string",
  "stage": "face_detected | query_sent | candidates_returned | candidate_selected | verification_run | verification_result | record_built | blockchain_tx_submitted | blockchain_confirmed | reverification_run",
  "timestamp": "ISO8601",
  "status": "one of the canonical status enum values, or a decision value from §3",
  "detail": { "...": "stage-specific structured fields, e.g. candidate_id, similarity, tx_hash" }
}
```

---

## 6. API Surface (owned by the Backend agent, all other services are internal modules called by it)

```
POST /api/pipeline/start       → { job_id }
GET  /api/pipeline/{job_id}/status  → { stage, status }
GET  /api/pipeline/{job_id}/result  → { final structured result incl. tx_hash, verification chain }
```

---

## Amendment rule

If a contract here needs to change mid-build, the agent proposing the change must state it explicitly (not silently deviate), and it needs to be reflected here before any *other* agent's already-in-progress work is assumed compatible with it. Treat this file as versioned truth, not a suggestion.

---

## Amendment log

- **2026-09-01 — §6 status endpoint nullability.** `GET /api/pipeline/{job_id}/status` may return `"stage": null, "status": null` when the job has been accepted but the background pipeline has not yet emitted its first event. Rationale: inventing a stage/status for work that hasn't run would put a fake state into the data-lineage log, and blocking the start request would violate the async job model (§6). Reflected in `contracts/schemas.py::PipelineStatusResponse` (`stage`/`status` now `Optional`).
- **2026-09-01 — §5 event status vocabulary for the `face_detected` stage.** When the query face is cleanly accepted, the `face_detected` event's `status` is the VisionService §1 status value `"OK"` (the canonical §6 enum has no "accepted" state, and reusing a search/verification status there would be a lie). The same applies to `query_sent` (the outbound search call was made; its outcome is the next event). All other vision outcomes (`NO_FACE_DETECTED`, `MULTIPLE_FACES_DETECTED`, `LOW_IMAGE_QUALITY`) are canonical enum values already.
