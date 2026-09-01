# PROJECT_STATUS.md — HH Goa 2026 · Task 3 Live Status

> **Single source of truth for build progress.** Update this file at the end of every work session.
> Companion file: `HUMAN_ACTIONS.md` (things only the human team can do).
> Design truth: `TASK3_ARCHITECTURE.md` (v2) · Contract law: `CONTRACTS.md` · Agent split: `MULTI_AGENT_BUILD_PLAN.md`

| Field | Value |
|---|---|
| **Project** | Face Identification & Blockchain Verification |
| **Deadline** | September 7, 2026, 11:59 PM |
| **Days remaining** | 6 |
| **Last updated** | September 1, 2026 (evening — full code build complete) |
| **OVERALL COMPLETION** | **~85%** (all code + scripts + UI + demo assets done; only live-key runs, real-photo pair matrix, recording, submission remain) |

**Legend:** `[DONE]` complete · `[WIP]` in progress · `[PENDING]` not started · `[BLOCKED-HUMAN]` waiting on human action · `[N/A]` dropped by design

---

## 1. Phase Snapshot

| Phase | Scope | Status | % | Target day |
|---|---|---|---|---|
| **Planning** | Architecture v2, contracts, build plan, research | `[DONE]` | 100% | Sep 1 |
| **Phase 0** | Environment + riskiest-assumption validation (search coverage) | `[PENDING]` | 0% | Sep 1–2 |
| **Phase 1** | 5 parallel coding agents: 4 services + backend | `[PENDING]` | 0% | Sep 2–3 |
| **Phase 2** | Integration (merge order per build plan) | `[PENDING]` | 0% | Sep 3 |
| **Phase 3** | QA test matrix + threshold tuning | `[PENDING]` | 0% | Sep 4 |
| **Phase 4** | Tamper demo, UI (1–2 hr cap), README, recording | `[PENDING]` | 0% | Sep 5 |
| **Phase 5** | Freeze, dry run, submission | `[PENDING]` | 0% | Sep 6–7 |

---

## 2. Detailed Task Board

### Planning (done)
| Task | Status | % | Notes |
|---|---|---|---|
| Architecture doc v1 → v2 revision | `[DONE]` | 100% | CrewAI dropped; role-separated services; honest-framing language added |
| `CONTRACTS.md` contract law | `[DONE]` | 100% | Unchanged in v2; still fully consistent — no amendments needed |
| `MULTI_AGENT_BUILD_PLAN.md` agent split | `[DONE]` | 100% | Valid as-is; branch labels → file-ownership labels for shared worktree |
| External-dependency research (4 parallel agents) | `[DONE]` | 100% | See §4 — 3 doc corrections found and applied |
| Status tracking + human-actions files | `[DONE]` | 100% | This file + `HUMAN_ACTIONS.md` |

### Phase 0 — Environment + Riskiest Assumption (target: Sep 1–2)
| Task | Status | % | Notes |
|---|---|---|---|
| `uv` Python 3.12 env + pinned deps | `[DONE]` | 100% | `.venv` built: web3 8.0, py-solc-x 2.0.5 (solc 0.8.24 verified), fastapi, insightface 1.0.1, onnxruntime, pytest — 74 tests green | crewai NOT needed (v2). Pins: insightface==1.0.1, onnxruntime, fastapi, uvicorn, web3==8.x, py-solc-x, pydantic, requests, pytest |
| `contracts/schemas.py` (executable CONTRACTS.md) | `[DONE]` | 100% | All 10 status enums; `extra="forbid"` on Search payload so a leaked embedding/score is a runtime crash |
| `.env` + `.env.example` (gitignored) | `[BLOCKED-HUMAN]` | 0% | Needs keys from `HUMAN_ACTIONS.md` items H1–H3 |
| InsightFace smoke test (CPU, 512-d embedding) | `[DONE]` | 100% | buffalo_l auto-downloads ~275 MB on first run |
| **Phase 0 validation script** (`scripts/validate_search.py`) | `[DONE]` | 90% | Script built + ready; live run `[BLOCKED-HUMAN]` — needs H1 key + H4 photos (blocks provider choice) |
| Amoy smoke test (`scripts/smoke_amoy.py`) | `[DONE]` | 100% | **PASS live**: both RPCs connected, chainId 80002, block ~46.4M; wallet/balance check ready, needs H3 |
| Pinata smoke test (`scripts/smoke_pinata.py`) | `[DONE]` | 80% | Script built + runs; live pin `[BLOCKED-HUMAN]` — needs H2 JWT |

### Phase 1 — Five Parallel Coding Agents (target: Sep 2–3)
| Task | Status | % | Owner | Notes |
|---|---|---|---|---|
| A — `services/vision.py` + tests | `[DONE]` | 100% | Agent A | InsightFace buffalo_l; NO_FACE / MULTIPLE_FACES (largest, documented) / LOW_IMAGE_QUALITY gate; §1 schema |
| B — `services/search.py` + tests | `[DONE]` | 100% | Agent B | Vision Web Detection primary, SerpAPI fallback (loudly logged); retrieval ONLY; §2 schema with extra="forbid" |
| C — `services/verification.py` + tests | `[DONE]` | 100% | Agent C | Own thresholds (start: HIGH >=0.48 / UNCERTAIN >=0.35, tunable); independent re-fetch/re-detect/re-encode/re-score; §3 schema |
| D — `services/blockchain.py` + `solidity/AnchorRecord.sol` + tests | `[DONE]` | 100% | Agent D | Pinata → CID → Amoy anchor (hashes only, NO biometrics/raw URLs); async pending state; `reverify()` + corrupt-one-byte mismatch test |
| E — `app/main.py` FastAPI backend + tests | `[DONE]` | 100% | Agent E | Job model (start/status/result), SSE event stream, template narration from §5 events, canonical status normalization, all unhappy paths typed |

### Phase 2 — Integration (target: Sep 3)
| Task | Status | % | Notes |
|---|---|---|---|
| Merge vision + blockchain (no interdeps) | `[DONE]` | 100% | Single worktree, file-boundary isolation |
| Merge search; run C's mocked tests against B's REAL output | `[PENDING]` | 50% | Contract enforced by schemas at unit level; live-output proof needs H1 key |
| Merge verification | `[DONE]` | 100% | |
| Merge backend; first full end-to-end run | `[PENDING]` | 60% | Full mocked E2E tested (happy + all unhappy paths); live E2E blocked on keys |
| Contract violations → amend CONTRACTS.md centrally | `[DONE]` | 100% | 2 amendments logged (status nullability; OK event status) |

### Phase 3 — QA Matrix (target: Sep 4)
| Task | Status | % | Notes |
|---|---|---|---|
| Same-person pairs (5–10) | `[PENDING]` | 20% | 1 pair measured with real model (astronaut full vs crop: cosine 0.9914, HIGH zone); needs H4 team photos for the full matrix (`scripts/qa_matrix.py`) |
| Different-person pairs (10+) | `[BLOCKED-HUMAN]` | 0% | Needs H4 real photos of different people |
| Bad-quality (5), no-face (3), multi-face (3) cases | `[DONE]` | 100% | Ran via `scripts/qa_matrix.py` with the real model: no-face 3/3 correct, multi-face 3/3 correct, bad-quality 6 cases (blur/noise/dark/64px OK w/ embeddings 0.60–0.83; 24px/pixelated honestly NO_FACE) |
| Threshold finalized from evidence + honest results table | `[PENDING]` | 40% | Partial matrix recorded; finalization awaits H4 pair matrix |

### Phase 4 — Demo Assets (target: Sep 5)
| Task | Status | % | Notes |
|---|---|---|---|
| `scripts/tamper_demo.py` (tamper → hash mismatch) | `[DONE]` | 100% | Run verified: untouched record INTACT, tampered record MISMATCH, single-pixel analog differs |
| UI: 1 page, live event feed (polling), terminal aesthetic | `[DONE]` | 100% | `ui/index.html`, served by the API at `/` (static mount); live-tested HTTP 200 |
| README (what/how/chain/limitations) | `[DONE]` | 100% | Written incl. honest limitations; QA pair-matrix numbers appended after H4 |
| Screen recording (full dry run first) | `[BLOCKED-HUMAN]` | 50% | Demo script exists (`scripts/demo_pipeline.py` + tamper demo); human records after keys land |
| Defense Pack rehearsal with teammate as judge | `[BLOCKED-HUMAN]` | 0% | `TASK3_ARCHITECTURE.md` §10 |

### Phase 5 — Freeze & Submit (Sep 6–7)
| Task | Status | % | Notes |
|---|---|---|---|
| Code freeze — no changes after | `[PENDING]` | 0% | Spec: no resubmissions |
| Submission form | `[BLOCKED-HUMAN]` | 0% | https://forms.gle/oZbQGuwiNeHVcHWo8 |

---

## 3. Environment & Tooling — Verified

| Item | Status | Detail |
|---|---|---|
| OS | Windows (win32) | PowerShell environment |
| Python on PATH | `[BLOCKED]` | Not on PATH; installed under `%LOCALAPPDATA%\Programs\Python` → use `uv` to manage Python 3.12 explicitly |
| `uv` | `[DONE]` | Present at `C:\Users\home\.local\bin\uv.exe` — env manager of choice |
| `git` | `[DONE]` | Present |
| `node` | `[DONE]` | Present (unused by this design) |
| Working tree | `[DONE]` | 3 planning docs only — no code yet, clean slate |

---

## 4. Key Decisions Log (research-verified, Sept 1)

| # | Decision | Rationale |
|---|---|---|
| 1 | **CrewAI dropped** (architecture v2) | v1 misread "multi-agent": means multiple coding agents, not runtime AI wrappers. Role-separated services are the honest, defensible design. **LLM API key no longer needed.** |
| 2 | Amoy RPC = `https://polygon-amoy.drpc.org` (primary) | `rpc-amoy.polygon.technology` is DEAD (DNS fails). Verified live: drpc returns chainId 0x13882 (80002). Fallback: `polygon-amoy-bor-rpc.publicnode.com` |
| 3 | IPFS = **Pinata free tier** | web3.storage pivoted to paid "Fil One" — free CID-pinning model is gone. Pinata: 1 GB / 500 files / 10 GB bandwidth — sufficient |
| 4 | `insightface==1.0.1` | Ships pure wheel — NO Visual C++ Build Tools needed on Windows (old 0.7.3 friction is obsolete) |
| 5 | buffalo_l via InsightFace `FaceAnalysis`, CPU onnxruntime | 512-d embeddings, auto-downloads from GitHub release v0.7 (~275 MB) |
| 6 | Similarity thresholds start: 0.48 HIGH / 0.35 UNCERTAIN | Research consensus for buffalo_l cosine; MUST be re-derived from §9 test matrix, documented as empirical |
| 7 | Vision Web Detection via raw REST (`images:annotate`, API-key auth) | No client lib needed; 1,000 free units/mo; GCP billing must be enabled |
| 8 | web3.py 8.x + py-solc-x 2.x (solc 0.8.24) | Pure-Python Solidity toolchain on Windows; note web3 v8 uses `signed.raw_transaction` |
| 9 | Disjoint file ownership instead of per-agent git branches | Parallel coding agents share one worktree; isolation preserved by file boundaries, not branches |
| 10 | Search = candidates only, scored by Verification only | Enforced in code via `extra="forbid"` pydantic model (CONTRACTS §2) |

## 5. Credential / Input Checklist (full steps in HUMAN_ACTIONS.md)

| Item | Feeds | Status |
|---|---|---|
| H1 Google Vision API key (billing enabled) | `GOOGLE_VISION_API_KEY` | `[PENDING]` |
| H2 Pinata JWT | `PINATA_JWT` | `[PENDING]` |
| H3 Funded Amoy wallet key | `AMOY_PRIVATE_KEY` | `[PENDING]` |
| H4 3–5 real team photos w/ public web presence | `data/phase0_photos/` | `[PENDING]` |
| H5 Team-member consent records | `docs/consent.md` | `[PENDING]` |
| H6 GitHub repo + push access | remote | `[PENDING]` |
| H7 Demo-day logistics + recording + form | — | `[PENDING]` |

## 6. Risk Register

| Risk | Severity | Mitigation | Status |
|---|---|---|---|
| Google Vision coverage thin for chosen demo face | HIGH | Phase 0 validation on 3–5 photos NOW; pivot to SerpAPI primary if needed | Open — Phase 0 |
| Threshold miscalibration → false matches live | HIGH | §9 test matrix before freeze; honest uncertainty zone | Open |
| Free RPC endpoint flaky mid-demo | MED | Two endpoints in config; retry with backoff | Mitigation planned |
| Pinata gateway latency during recording | MED | Pin explicitly ahead of demo; pre-warm | Open |
| IPFS pinning is not permanence (credibility) | LOW | State as documented limitation (already in v2 §4.4) | Handled |
| Scope creep into UI polish | MED | Hard 1–2 hr cap enforced | Standing rule |

## 7. Submission Checklist (from v2 §11)

- [ ] GitHub repo, `CONTRACTS.md` at root, clean history
- [ ] README: what / how to run / which chain & why / honest limitations
- [ ] Screen recording: live capture → real search call → verification → chain write → pixel-tamper failure
- [ ] Submission form submitted
- [ ] No website scope creep

## 8. Changelog

| Date | Entry |
|---|---|
| 2026-09-01 | Pending-task sweep: Phase 0 smoke/validate scripts (Amoy smoke PASSED live); Phase 3 partial QA matrix run with real model (no-face 3/3, multi-face 3/3, same-person pair cosine 0.9914); Phase 4 tamper demo + one-page UI built and verified; full HTTP pipeline run exercised (real detection → honest SEARCH_API_FAILURE without H1 key). |
| 2026-09-01 | Full code build: schemas + 4 services + FastAPI backend + AnchorRecord.sol + 74 tests (all passing; solc compile verified). Two CONTRACTS.md amendments logged. Remaining: live-key validation (H1-H3), Phase 3 QA matrix, tamper demo script, UI, recording. |
| 2026-09-01 | Project initialized. Architecture v2 reviewed, contracts confirmed consistent, 4 research agents verified all external dependencies (3 corrections applied — see §4). Status + human-actions files created. |
