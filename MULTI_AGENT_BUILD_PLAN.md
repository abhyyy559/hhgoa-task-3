# Multi-Agent Build Plan — Task 3

How to split this build across multiple coding agents working in parallel, so it finishes fast without turning into integration hell. `TASK3_ARCHITECTURE.md` is the design; `CONTRACTS.md` is the law every agent below is bound by — paste it into every agent's context before anything else.

---

## Phase 0 — Validate the riskiest assumption FIRST (single agent, ~30-60 min, blocks nothing else from starting but must be watched closely)

Before splitting into parallel work, one agent (or you, manually — this is fast) answers the reviewer's hardest question directly: **can Google Vision Web Detection reliably return usable candidates for real photos of your team members?** Call the API on 3-5 real photos, log the raw response, confirm it returns actual URLs you can fetch. If this fails or coverage is thin, that's the moment to pivot to SerpAPI as primary — cheap to discover now, expensive to discover on day 5.

This doesn't block Phases 1A-1D below from starting immediately in parallel — Vision and Blockchain work don't depend on the outcome. But don't let Search/Verification integration proceed past a mocked stub until Phase 0 has reported back.

---

## Phase 1 — Four agents in parallel, each owns one service, each works on its own branch

Every agent below gets: `TASK3_ARCHITECTURE.md`, `CONTRACTS.md`, and **only its own section** from this file — not the others. That's deliberate: an agent that can't see how another service is implemented is exactly what makes the independence contract real instead of accidental. Communication between them happens through `CONTRACTS.md` only.

### Agent A — VisionService
**Branch:** `vision-service`
**Prompt:**
> Build `services/vision.py` implementing face detection and encoding per `CONTRACTS.md` §1. Use InsightFace (buffalo_l model), fall back to DeepFace if InsightFace has install issues in this environment — document which you used and why in a module docstring. Handle every state explicitly: no face detected, multiple faces detected (auto-select largest/most-centered, document this as a deliberate choice), and a quality gate below which you return `LOW_IMAGE_QUALITY` rather than a bad embedding. Write unit tests against at least 3 real test images covering: clean single face, no face, multiple faces. Output must validate against the exact schema in `CONTRACTS.md` §1 — write a pydantic model for it and a test that a valid detection actually passes that model.

### Agent B — SearchService
**Branch:** `search-service`
**Prompt:**
> Build `services/search.py` implementing web/social search per `CONTRACTS.md` §2. Primary: Google Cloud Vision API Web Detection. Secondary/fallback: SerpAPI reverse image search, used only if the primary fails after retries — log which path was used, loudly, every time. Your output schema is non-negotiable: use a pydantic model with `extra="forbid"` matching `CONTRACTS.md` §2 exactly, so it is a runtime error, not a code-review catch, if you accidentally include an embedding, similarity score, or match decision in what you return. You are retrieval only — do not score or judge candidates, that's a different service's job. Run Phase 0's validation script first if it hasn't been run yet, and report the real result rate before building further. Handle `NO_SEARCH_RESULTS` and `SEARCH_API_FAILURE` as real, distinct, tested terminal states.

### Agent C — VerificationService
**Branch:** `verification-service`
**Prompt:**
> Build `services/verification.py` per `CONTRACTS.md` §3. You receive ONLY `candidate_id` + `candidate_url` (+ optionally `thumbnail_url`) — if the input you're given contains an embedding, similarity score, or decision field, that's a contract violation upstream; reject it, don't use it. Independently: fetch the candidate URL yourself, extract any face image present, run your own detection+encoding (you may import the same underlying model as VisionService, but call it fresh — do not accept a pre-computed embedding for the candidate from anywhere), and compute similarity against the original query embedding (which the backend will pass you separately, per `CONTRACTS.md` §1). Classify into HIGH/UNCERTAIN/LOW zones. You do not know what threshold Agent A or Agent B used for anything — pick your own ACCEPT_THRESHOLD and REVIEW_THRESHOLD, document your reasoning, and leave them easily tunable constants, not magic numbers. Build against a **mocked** candidate list conforming to `CONTRACTS.md` §2 until SearchService is ready to integrate — don't wait on Agent B.

### Agent D — BlockchainService
**Branch:** `blockchain-service`
**Prompt:**
> Build `services/blockchain.py` per `CONTRACTS.md` §4. Target Polygon Amoy testnet (chain ID 80002, ~2s blocks). Write a minimal Solidity contract that stores exactly the fields in `CONTRACTS.md` §4 — nothing more, no biometric data, no raw URLs (hash them first). Implement: upload content to IPFS (web3.storage or Pinata, document which and note the pinning-≠-permanence limitation explicitly in a comment and later in the README), submit the anchor transaction, and a `reverify(record_id)` function that rebuilds the hash from source and compares against the on-chain record. Write a test that deliberately corrupts one byte of test content and confirms `reverify` correctly reports a mismatch — this is your part of the tamper-evidence demo, make it reliable, not just demoable once. Treat blockchain submission as async from the start — return a pending state immediately, don't block on confirmation.

---

## Phase 2 — Backend/Integration agent (starts in parallel with Phase 1, integrates as pieces land)

### Agent E — Backend & Data Lineage
**Branch:** `backend-orchestrator`
**Prompt:**
> Build the FastAPI service per `CONTRACTS.md` §6: `POST /api/pipeline/start`, `GET /api/pipeline/{job_id}/status`, `GET /api/pipeline/{job_id}/result`. This must be a real job model — the pipeline takes several seconds, a blocking request is a bug, not a simplification. Wire in the four services as they land on their branches (build against stubs conforming to `CONTRACTS.md` in the meantime — don't idle waiting for Agents A-D). Implement the event log per `CONTRACTS.md` §5 — every stage transition emits a structured event, streamable over SSE/WebSocket for the UI to consume live later. Own the canonical status enum — every service's output gets normalized into it at the boundary, so nothing downstream has to know each service's internal error vocabulary. Write tests for every unhappy path explicitly: no face, no search results, verification rejects, blockchain write fails — each must produce a real, typed, visible failure state, never a silent success.

---

## Phase 3 — Integration (you, or one designated agent, not all of them at once)

Once Agents A-E each pass their own contract-conformance tests on their own branch:
1. Merge `vision-service` and `blockchain-service` first — no interdependencies, lowest risk.
2. Merge `search-service`, run Agent C's mocked verification tests against Agent B's *real* output — this is the moment that proves the contract actually held. If it doesn't validate cleanly, that's a contract bug to fix in `CONTRACTS.md`, not a reason to loosen the schema informally.
3. Merge `verification-service`.
4. Merge `backend-orchestrator` last, run the full end-to-end pipeline for the first time as one system.
5. Run the test matrix from `TASK3_ARCHITECTURE.md` §9 against the fully integrated system, not just each service in isolation.

---

## Phase 4 — QA sweep agent (once integrated)

### Agent F — Testing
**Branch:** `qa-sweep`
**Prompt:**
> Run the full test matrix from `TASK3_ARCHITECTURE.md` §9 against the integrated pipeline: 5-10 same-person pairs, 10+ different-people pairs, 5 bad-quality cases, 3 no-face cases, 3 multiple-faces cases. Record actual results, not expected ones — if the threshold from Phase 1 performs badly against this matrix, report that honestly and propose a revised threshold with the new evidence, don't quietly patch it to make the numbers look better. Output a short results table for the README's "known limitations" section.

---

## Phase 5 — Last (deliberately, per architecture doc's priority order)

One agent or you directly: minimal UI (1-2 hrs cap), README (what/how/which chain/limitations — pull directly from Agent F's results and each service's documented decisions), screen recording, and a rehearsal of the Defense Pack in `TASK3_ARCHITECTURE.md` §10 with a teammate playing judge.

---

## Coordination notes

- **`CONTRACTS.md` is read-only truth for agents A-D.** Only Agent E (backend) or you should be authorized to propose changes to it, and only with the amendment note at the bottom of that file — a contract silently drifting between two agents' branches is exactly how this kind of parallel build usually fails.
- **Give each agent only its own section of this file**, not the whole document — this is what keeps Verification honest about not knowing Search's internals, not just politeness.
- **Don't let Agent C or Agent E idle waiting on Agent B.** Mocked data conforming to the contract is enough to build and test against; real integration happens in Phase 3.
- If any agent's actual output doesn't validate against `CONTRACTS.md`, that agent stops and the contract gets fixed centrally — no agent should informally special-case around another's shape.
