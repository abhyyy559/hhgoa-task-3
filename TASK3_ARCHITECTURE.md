# HH Goa 2026 — Task 3: Face Identification & Blockchain Verification
## Product Requirements & Architecture — v2 (revised after technical review)

**Deadline:** September 7, 2026, 11:59 PM · **Launched:** August 31, 2026

**What changed from v1, and why:** v1 wrapped every component in a CrewAI `Agent` object, which was a misreading — "multiple agents" meant multiple *coding agents* building this in parallel, not in-product AI agents. That misreading made the architecture weaker, not just different: a deterministic function wrapped in an Agent object is exactly the "wrapper library pretending to be architecture" a technical judge will call out. v2 drops CrewAI and adopts role-separated services instead — the same design intent (independence between components), stated precisely instead of decorated.

---

## 0. What actually went wrong last time, and how this design answers it

- **Overfitting** — v1 flagged this correctly but v2 goes further: an explicit result-state machine (§4.3) that never silently converts "no confident match" into "match found," plus a documented minimum test matrix (§9) that's evidence you tested beyond one demo image, not just a claim that you did.
- **No proper backend** — real FastAPI service, job-based (not blocking), explicit typed states for every failure mode, not just the happy path (§6).
- **Can't defend it to the evaluator** — a data-lineage event log (§7) gives you a literal, inspectable trail of every decision the pipeline made, and §10 is a rehearsed answer key including the two conceptual corrections a technical judge would otherwise catch you on (independence ≠ same-model-same-threshold; blockchain proves integrity, not identity).

---

## 1. The Thesis

The differentiator isn't an AI-agent framework — it's rigor a judge can verify: an independence contract for search/verification that's *enforced in code* (a schema validator that rejects a leaking payload, not just a design intention), a result-state machine that refuses to fake a happy ending, and a blockchain layer that's precise about what it actually proves. Palantir-style systems aren't impressive because every box has an agent name; they're impressive because data lineage, verification, and failure handling are painfully clear. That's the target.

---

## 2. Architecture

```
                       CLIENT
                          │
                          ▼
                 ┌────────────────┐
                 │  Consent Gate  │
                 └───────┬────────┘
                         │
                         ▼
                 ┌─────────────────┐
                 │  FastAPI Backend │
                 │ Pipeline Manager │
                 └────────┬─────────┘
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                   ▼
 ┌─────────────┐   ┌──────────────┐   ┌───────────────┐
 │VisionService│   │SearchService │   │ Job/Event Store│
 └──────┬──────┘   └──────┬───────┘   └────────────────┘
        │ embedding       │ candidate_url + candidate_id ONLY
        │ (kept, never    │ (see CONTRACTS.md — no score,
        │  sent to Search)│  no embedding, no decision)
        ▼                 ▼
                 ┌──────────────────┐
                 │VerificationService│
                 │ Re-fetch          │
                 │ Re-detect         │
                 │ Re-encode         │
                 │ Re-score          │
                 │ (own copy of the  │
                 │  face embedding,  │
                 │  independent call)│
                 └────────┬──────────┘
                          │
                 candidate_match / uncertain / no_match
                          ▼
                 ┌──────────────────┐
                 │ Record Builder    │
                 │ Canonical JSON    │
                 │ SHA-256 hash      │
                 └────────┬──────────┘
                          ▼
                 ┌──────────────────┐
                 │ BlockchainService │
                 └────────┬──────────┘
                          ▼
                    Transaction hash
                          ▼
                 ┌──────────────────┐
                 │  Re-verification  │
                 │  Rebuild hash      │
                 │  Compare on-chain  │
                 └──────────────────┘
```

Full data contracts between every arrow above live in `CONTRACTS.md` (repo root) — that file, not this document, is the literal source of truth each service is built and tested against.

---

## 3. Product Requirements (mapped to the task spec)

| # | Task requirement | How this design meets it |
|---|---|---|
| 1 | Face identification — detect and encode | VisionService, §4.1 |
| 2 | Real web/social search, not hardcoded | SearchService + live-capture demo protocol, §4.2 + §8 |
| 3 | Blockchain upload + re-verification demo | BlockchainService, §4.4 |
| 4 | No website required | UI capped at 1-2 hrs, §5 |
| 5 | GitHub repo + README | §11 |
| — | Independent verification, not self-trust | VerificationService, §4.3, enforced via CONTRACTS.md |
| — | Explainable / defensible pipeline | Data lineage event log, §7 |

---

## 4. Component Deep-Dive

### 4.1 VisionService — Face Detection & Encoding

InsightFace (buffalo_l), open-source, 512-d embedding, no per-call cost or rate limit to worry about mid-demo. DeepFace as fallback if install friction eats time.

**Explicit states, not a silent empty result:** `NO_FACE_DETECTED`, `MULTIPLE_FACES_DETECTED`, `LOW_IMAGE_QUALITY` (below a minimum-confidence gate) — each a typed status the backend must handle, not swallow.

### 4.2 SearchService — Real Web/Social Search

Google Cloud Vision API Web Detection, primary (verified live/current, 1,000 free units/month, returns real matching-page URLs). SerpAPI reverse-image wrapper as documented secondary. PimEyes/FaceCheck.ID deliberately excluded as primary — dedicated stranger-search engines carry real stalking-enablement risk; naming them in a comparison table if asked is fine, building around one isn't.

**Output contract is strict** (see `CONTRACTS.md`): `candidate_id`, `candidate_url`, `source_type`, `thumbnail_url` only. No embedding, similarity score, or match decision is permitted in this payload — enforced with a pydantic model using `extra="forbid"`, so a leaking field is a validation error at run time, not a code-review hope.

**Explicit result states**, never silently collapsed into a happy path:
- `SEARCH_SUCCESS` + candidates returned → proceed to verification
- `SEARCH_SUCCESS` + zero usable candidates → `NO_SEARCH_RESULTS`, a real terminal state, not retried into a fake match
- `SEARCH_API_FAILURE` → network/provider error, distinct from "found nothing"

### 4.3 VerificationService — Independent Cross-Check

Receives only `candidate_url`/`candidate_id`. Independently: fetches the live source, extracts any face image present, re-detects and re-encodes it (its own call to the same VisionService logic, but on the candidate's image, from scratch), computes similarity against the *original* query embedding (which VerificationService holds independently, not passed through SearchService), and classifies into a threshold zone:

```python
if similarity >= ACCEPT_THRESHOLD:
    decision = "candidate_match"
elif similarity >= REVIEW_THRESHOLD:
    decision = "uncertain"
else:
    decision = "no_match"
```

**Honest framing, adopted verbatim from review:** since both services would use the same underlying embedding model, call this an **independent execution path**, not a statistically independent system — that's the accurate claim and it's still a strong one. Thresholds are chosen empirically against the test matrix in §9, documented with the reasoning, never presented as scientifically settled.

### 4.4 BlockchainService — Notarization & Re-Verification

**Chain: Polygon Amoy testnet** — public, free faucet, ~2s block times (vs Sepolia's ~12s), full Polygonscan support for the "verify this transaction yourself, right now" moment. Root chain (Sepolia) retires Sept 30, 2026 — after the deadline, a non-issue for this build.

**On-chain record, minimized per privacy review — no biometric data anchored:**
```json
{
  "record_version": "1.0",
  "record_id": "uuid",
  "content_hash": "sha256 of canonical JSON",
  "content_cid": "ipfs CID or null",
  "source_reference_hash": "sha256(candidate_url) — not the raw URL",
  "verification_result": "candidate_match | uncertain | no_match",
  "verification_timestamp": "ISO8601",
  "pipeline_version": "string"
}
```
Face embeddings, raw source URLs, and raw image bytes are explicitly excluded from the on-chain payload — kept off-chain, referenced by hash only.

**Critical distinction to state precisely, every time this comes up:** the blockchain proves *"at this timestamp, this specific fingerprint was anchored immutably and hasn't been altered since."* It does **not** prove *"this post belongs to this person."* Conflating the two is the single easiest way to lose credibility with a technical judge — see §10.

**Re-verification demo:** rebuild the hash from the source, compare to the on-chain record — matches. Then edit one pixel of a saved copy and rebuild — hash no longer matches. State explicitly: *"this demonstrates content integrity, not facial identity — the image could still depict the same person even if untampered."*

**IPFS honesty:** pinning a CID does not guarantee permanence. Document this as a known limitation in the README rather than implying "on IPFS" means "forever."

### 4.5 Backend — Job Manager & Data Lineage

FastAPI, async job model (§6), and the event log described in §7. This is the component that used to be called "OrchestratorAgent" — same mechanism (structured events → readable trace), correctly renamed: it's a data-lineage/observability system, not an agent.

---

## 5. UI (1–2 hours, deliberately)

One page: capture/upload → live event-log stream (SSE/WebSocket) → final result + Polygonscan link. Terminal/log aesthetic — cheap to build, thematically correct for a transparency-first project.

---

## 6. Backend & Async Job Design

```
POST /api/pipeline/start        → { job_id }
GET  /api/pipeline/{job_id}/status  → current stage + status enum
GET  /api/pipeline/{job_id}/result  → final structured result
```

Blockchain submission is asynchronous — submit tx, return immediately, poll/stream for confirmation, update job status on confirm. Never block an HTTP request on a chain confirmation.

**Canonical status enum, used everywhere (backend, event log, README, defense pack — one vocabulary, not three):**
```
NO_FACE_DETECTED · MULTIPLE_FACES_DETECTED · LOW_IMAGE_QUALITY
SEARCH_SUCCESS_MATCH_VERIFIED · SEARCH_SUCCESS_NO_HIGH_CONFIDENCE_MATCH
NO_SEARCH_RESULTS · SEARCH_API_FAILURE
VERIFICATION_FAILED
BLOCKCHAIN_FAILURE · BLOCKCHAIN_CONFIRMED
```
Never claim `BLOCKCHAIN_CONFIRMED` if the transaction failed — retry with backoff, then surface `BLOCKCHAIN_FAILURE` as a real, visible terminal state.

---

## 7. Data Lineage (Event Log)

Every stage emits one event:
```json
{
  "job_id": "abc123",
  "stage": "verification_result",
  "timestamp": "2026-09-03T10:22:11Z",
  "status": "candidate_match",
  "detail": { "candidate_id": "c_02", "similarity": 0.83, "zone": "HIGH" }
}
```
Canonical stage list: `face_detected → query_sent → candidates_returned → candidate_selected → verification_run → verification_result → record_built → blockchain_tx_submitted → blockchain_confirmed → reverification_run`. This log is simultaneously debugging output, the UI's live feed, and the transcript you read from when a judge asks what happened at any given step.

---

## 8. Anti-Overfitting / Live-Demo Protocol

Capture the input photo live, in front of judges, of a consenting team member, on the demo device, at that moment — no lookup table could contain a photo that didn't exist five minutes earlier. Narrate it explicitly as you do it. Show the actual outbound SearchService API call in the event log as corroborating evidence.

---

## 9. Test Matrix (minimum, before any threshold is finalized)

| Case | Minimum count |
|---|---|
| Same person, different photos | 5–10 pairs |
| Different people | 10+ pairs |
| Bad lighting / low quality | 5 cases |
| No face in image | 3 cases |
| Multiple faces in image | 3 cases |

This is what makes the chosen threshold defensible as "empirically selected" rather than invented, and it's direct evidence against the overfitting criticism — proof you tested beyond the one demo image.

---

## 10. Evaluator Defense Pack

| Question | Answer |
|---|---|
| "Aren't these agents just Python functions?" | We use role-separated services, not an agent framework — the design decision that matters is the independence contract between search and verification, enforced by a schema validator that rejects a leaking payload, not the presence of an agent object. |
| "Is verification really independent?" | It's an independent execution path — separate fetch, separate detection, separate encoding, separate scoring, with zero access to search's internal state. Same underlying model, so we call it that precisely rather than overclaiming statistical independence. |
| "How do you know this isn't hardcoded?" | Live-captured this photo moments ago on this device — no lookup table could contain it. Here's the actual outbound API call in the event log, timestamped now. |
| "What does the blockchain actually prove?" | That this specific fingerprint was anchored immutably at this timestamp and hasn't changed since — not that the post belongs to this person. Those are different claims and we only make the first one. |
| "Prove tamper-evidence." | [Live pixel-edit demo.] Same content, one pixel changed, hash no longer matches. This shows content integrity, not facial identity — the image could still depict the same person even untampered. |
| "What's your match threshold and why?" | [State the number and the test matrix in §9 it came from.] Empirically selected against a same-person/different-person test batch, not asserted as scientifically proven. |
| "What happens with no match?" | [Show it live.] Explicit `NO_SEARCH_RESULTS` or `no_match` state — never silently converted into a happy path. |
| "Is the IPFS content permanent?" | No — pinning isn't a permanence guarantee, and we document that as a known limitation rather than overclaiming. |

---

## 11. Submission Checklist

- [ ] GitHub repo, `CONTRACTS.md` at root, clean history
- [ ] README: what it does, how to run it, which blockchain and why, known limitations stated honestly (search coverage depends on Google's index; threshold is empirical, not proven-optimal; IPFS pinning ≠ permanence)
- [ ] Screen recording: live face capture → real search call visible → independent verification → blockchain upload → re-verification incl. the pixel-tamper failure case
- [ ] Submission form: https://forms.gle/oZbQGuwiNeHVcHWo8
- [ ] No website — confirm no scope creep into frontend polish

---

## 12. Open Risks

1. Search coverage is unpredictable per face — validate with several real team-member photos before demo day (this is now Phase 0, see `MULTI_AGENT_BUILD_PLAN.md`).
2. InsightFace install friction on some machines — verify environment early.
3. Threshold is a judgment call from a small test matrix, not a proven-optimal number — state it that way, don't oversell it.
4. IPFS pinning provider choice affects demo reliability during the recording window — pin explicitly, don't assume default retention.
