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
#    SERPAPI_KEY             (optional fallback)

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

# 6. Tests (90+: vision, search, verification, blockchain, backend incl. SSE/retry/delete)
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

- **Search coverage is whatever Google's index has.** Some faces simply have
  no web presence — that's `NO_SEARCH_RESULTS`, a real terminal state, not an
  error. Coverage per team member must be validated (Phase 0) before demo day.
- **The 0.48 / 0.35 thresholds are empirical starting points** from published
  buffalo_l cosine-similarity ranges, NOT proven-optimal for this dataset.
  They are being re-derived from the §9 test matrix (same-person /
  different-person / bad-quality batches) before freeze.
- **Verification is an independent execution path, not statistical
  independence.** Separate fetch, separate detection, separate encoding,
  separate scoring — but the same underlying model. We call it that
  precisely rather than overclaiming.
- **IPFS pinning is not permanence.** Pinata's free tier retains what we pin;
  that is not a guarantee. Documented, not hidden.
- **Web Detection URLs can decay.** Candidate pages fetched minutes after the
  search may 404; verification then returns `no_match` with an explanatory
  reason rather than a faked score.
- **No website.** One terminal-style UI page at most (hard 1–2 h cap).

## Live validation status (Sept 1, 2026)

| Check | Result |
|---|---|
| Pinata IPFS pin (H2) | **PASS** — live CID pinned via `api.pinata.cloud` (the correct host) |
| Amoy RPC connectivity (H3) | **PASS** — both endpoints, chainId 80002, gas ~30–60 gwei |
| Google Vision Web Detection (H1) | **BLOCKED-HUMAN** — key is valid but GCP billing is not enabled (`403 PERMISSION_DENIED`); enable billing on project `807377235294` then run `python scripts\go_live.py vision` |
| Amoy wallet (H3) | **BLOCKED-HUMAN** — `AMOY_PRIVATE_KEY`/`AMOY_WALLET_ADDRESS` in `.env` currently hold an RPC URL, not a wallet; generate a throwaway funded Amoy wallet, paste its key + address, then run `python scripts\go_live.py amoy e2e` |
| §9 pair matrix (real model) | 15/15 agreement — same-person cosine **0.9186** (HIGH), different-person max **0.0700** (LOW, 5× margin below the 0.35 review threshold); thresholds supported by this evidence but pair counts still below §9 minimums until team photos are added |

Any step: `.venv\Scripts\python.exe scripts\go_live.py <env|amoy|pinata|vision|e2e|all>` — the `all` target runs the entire live pipeline (real face → real search → real verification → Pinata CID → Amoy anchor) and prints the Polygonscan link.

## Repo layout

```
contracts/schemas.py      executable CONTRACTS.md — pydantic models, extra="forbid"
services/vision.py        face detect + encode (InsightFace buffalo_l, CPU)
services/search.py        reverse-image retrieval (Google Vision / SerpAPI fallback)
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
