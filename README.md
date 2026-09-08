# HH Goa 2026 — Task 3: Face Identification & Blockchain Verification

A face-identification pipeline that searches the live web for where a person's
photo appears, independently verifies the match, and anchors a tamper-evident
fingerprint of the result on the Polygon Amoy testchain — with a full,
inspectable data-lineage event log of every decision the pipeline made.

Design truth: `TASK3_ARCHITECTURE.md` (v2) · Contract law: `CONTRACTS.md`
(executed by `contracts/schemas.py`) · Build split: `MULTI_AGENT_BUILD_PLAN.md`
· Live status: `PROJECT_STATUS.md`

## What it does

```
image → VisionService        detect face, 512-d embedding (buffalo_l, CPU)
      → SearchService        Google Vision Web Detection (SerpAPI fallback) —
                             retrieval ONLY: candidates, no scoring (schema-enforced)
      → VerificationService  independent re-fetch / re-detect / re-encode / re-score
      → BlockchainService    canonical record → SHA-256 → Pinata IPFS → Amoy anchor
      → re-verification      rebuild hash vs. chain (tamper-evidence demo)
```

Three deliberate design points a reviewer should check first:

1. **Search can't judge.** `SearchOutput` is a pydantic model with
   `extra="forbid"` — if a similarity score, embedding, or match decision ever
   leaks into the search payload, it is a runtime validation error, not a
   code-review catch. Verification re-does everything from scratch.
2. **No silent successes.** Every failure mode — no face, low quality, no
   search results, search API failure, uncertain similarity, rejected match,
   chain-write failure — is a typed, visible terminal state. The status enum
   is canonical across backend, event log, and README.
3. **The chain proves integrity, not identity.** What is anchored: a content
   hash, an IPFS CID, and `sha256(source_url)` (never the raw URL, never any
   biometric data). What it proves: this fingerprint was anchored at this
   time and hasn't changed since. What it does NOT prove: that the post
   belongs to that person.

## Run it

```powershell
# 1. Environment (Python 3.12, deps already pinned in the venv)
.venv\Scripts\python.exe -m pip list   # web3 8.x, fastapi, insightface 1.0.1, py-solc-x 2.x, ...

# 2. Configure — copy .env.example to .env and fill in (see HUMAN_ACTIONS.md):
#    GOOGLE_VISION_API_KEY   (H1)  live web search
#    PINATA_JWT              (H2)  IPFS pinning
#    AMOY_PRIVATE_KEY        (H3)  funded Amoy testnet wallet (throwaway!)
#    SERPAPI_KEY             (primary web search — SerpAPI Lens + Yandex, free tier 250/mo, no card)

# 3. Start the API
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# 4. Start a pipeline job and watch it finish (live SSE narration in the UI)
curl -F "image=@photo.jpg" http://127.0.0.1:8000/api/pipeline/start
#    → {"job_id": "..."}
curl http://127.0.0.1:8000/api/pipeline/<job_id>/status
curl http://127.0.0.1:8000/api/pipeline/<job_id>/result   # + /events for the full lineage
curl http://127.0.0.1:8000/api/pipeline/<job_id>/events/stream   # SSE live feed (UI uses this)

# 5. Deploy the on-chain contract once (optional — auto-deploys on first anchor)
.venv\Scripts\python.exe scripts\deploy_contract.py
#    → writes AMOY_CONTRACT_ADDRESS to .env + prints the Polygonscan link

# 6. Tests (114: vision, search, verification, blockchain, backend incl. SSE/retry/delete) + `scripts/eval_matrix.py` threshold evidence
.venv\Scripts\python.exe -m pytest -q
```

The first vision call needs the `buffalo_l` model pack (~275 MB) —
`scripts/bootstrap_model.py` pre-downloads it.

## API surface

```
POST /api/pipeline/start              → { job_id }          (multipart image upload)
GET  /api/pipeline/{job_id}/status    → { stage, status }
GET  /api/pipeline/{job_id}/result    → final structured result + event log
GET  /api/pipeline/{job_id}/events    → full §5 event log (JSON)
GET  /api/pipeline/{job_id}/events/stream → SSE live narration feed (UI)
DELETE /api/pipeline/{job_id}         → free a finished job (registry hygiene)
GET  /api/health                      → liveness + readiness checks
GET  /                               → terminal-style live UI
```

Blockchain anchoring retries transient RPC failures with exponential backoff
and a **fresh nonce** on every attempt, then surfaces a typed
`BLOCKCHAIN_FAILURE` — it never claims `BLOCKCHAIN_CONFIRMED` without a real
receipt. The in-memory job registry is bounded (200 jobs, 2h TTL) so a long
demo never grows memory unboundedly.

## Which chain, and why

**Polygon Amoy testnet** (chainId 80002): an EVM chain with free testnet MATIC,
real transactions visible on Polygonscan, and a pure-Python toolchain
(web3.py 8.x + py-solc-x, solc 0.8.24) that needs no Visual C++ Build Tools on
Windows. The `AnchorRecord` contract (`solidity/AnchorRecord.sol`) stores only:
content hash, IPFS CID, `sha256(source_url)`, verification decision label, and
provenance metadata. Re-anchoring the same `recordId` reverts — records cannot
be silently overwritten. RPC endpoints: drpc primary, publicnode fallback.

## Known limitations (stated honestly)

- **Search coverage is whatever the provider indexes have.** SerpAPI Lens
  (primary) + Yandex leg + Vision (if billing enabled). Some faces simply
  have no web presence — that's `NO_SEARCH_RESULTS`, a real terminal state,
  not an error. Coverage per demo subject must be validated before demo day
  (have them post a clear public photo days ahead).
- **Thresholds 0.48 / 0.35 are measured, with a stated limit.**
  `scripts/eval_matrix.py` (real model, Sept 7): same-person **2/2 matched
  (0.9186 both)**, different-person **25/25 cleanly rejected**, max impostor
  score **0.07** — 5× margin below the 0.35 review threshold. Honest limit:
  only 2 genuine same-person pairs so far; add team/friend pairs (target
  5–10) before claiming generality. We selected these numbers from this
  evidence, not from literature alone.
- **Verification is an independent execution path, not statistical
  independence.** Separate fetch, separate detection, separate encoding,
  separate scoring — but the same underlying model. We call it that
  precisely rather than overclaiming.
- **IPFS pinning is not permanence.** Pinata's free tier retains what we pin;
  that is not a guarantee. Documented, not hidden.
- **Unreachable images are UNVERIFIED, never scored rejections.** Candidate
  pages that 404, login-wall, or yield no detectable face return `no_match`
  with `faces_checked == 0` and an `UNVERIFIED — …` reason (plus a
  `candidate_retry` event) — "could not compare" is never presented as
  "compared and rejected." Only scored faces (faces_checked ≥ 1) count as
  genuine rejections.
- **No website.** One terminal-style UI page at most (hard 1–2 h cap).

## Live validation status (Sept 7, 2026)

| Check | Result |
|---|---|
| SerpAPI Lens open-web search | **PASS** — 59 candidates / 19 social on a stranger photo; top match verified 0.9116 |
| Pinata IPFS pin (H2) | **PASS** — live CIDs pinned (`QmZVmtJ8…`, `QmXiLDTv…`) |
| Amoy RPC connectivity (H3) | **PASS** — both endpoints, chainId 80002 |
| Amoy wallet funds (H3) | **BLOCKED-HUMAN** — ~0.0079 MATIC vs ~0.0128 needed; top up at the Amoy faucet, verify with `scripts\go_live.py amoy`, then re-upload for the recording run |
| Google Vision Web Detection (H1) | **BLOCKED** — key present but API answers HTTP 403 (billing/API not enabled); deprioritized, Lens covers the need |
| Enrolled-gallery match | **PASS** — different-photo same-person 0.9186 → record → pin → anchor attempted (funds only blocker) |
| Honest rejection | **PASS** — stranger vs gallery 0.05 / −0.04 → `PIPELINE_NO_CONFIDENT_MATCH`, no fake match |
| §9 pair matrix (real model) | `scripts/eval_matrix.py`: same-person 2/2, different-person 25/25, max impostor 0.07 — supports 0.48/0.35; add pairs to reach 5–10 same-person minimum |

Any step: `.venv\Scripts\python.exe scripts\go_live.py <env|amoy|pinata|vision|e2e|all>` — the `all` target runs the entire live pipeline (real face → real search → real verification → Pinata CID → Amoy anchor) and prints the Polygonscan link.

## Repo layout

```
contracts/schemas.py      executable CONTRACTS.md — pydantic models, extra="forbid"
services/vision.py        face detect + encode (InsightFace buffalo_l, CPU)
services/search.py        reverse-image retrieval (SerpAPI Lens primary + Yandex leg, Vision optional; resolver tiers full→partial→page→similar)
services/verification.py  independent re-fetch + re-encode + re-score
services/blockchain.py    canonical record, Pinata pin, Amoy anchoring, re-verification
app/main.py               FastAPI job manager + §5 data-lineage event log (SSE)
solidity/AnchorRecord.sol minimal anchoring contract (solc 0.8.24)
scripts/deploy_contract.py deploy AnchorRecord once + persist address to .env
scripts/go_live.py        run every remaining live validation step
scripts/tamper_demo.py    pixel/field-tamper → hash mismatch demo
scripts/qa_pairs*.py      §9 same/different-person pair matrix (real model)
docs/consent.md           participant consent record (HUMAN_ACTIONS H5)
tests/                    90+ tests: conformance + every unhappy path + SSE/retry/delete
ui/index.html             terminal-style live UI (SSE narration)
```
