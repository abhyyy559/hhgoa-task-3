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

# 4. Start a pipeline job and watch it finish
curl -F "image=@photo.jpg" http://127.0.0.1:8000/api/pipeline/start
#    → {"job_id": "..."}
curl http://127.0.0.1:8000/api/pipeline/<job_id>/status
curl http://127.0.0.1:8000/api/pipeline/<job_id>/result   # + /events for the full lineage

# 5. Tests (74: vision, search, verification, blockchain, backend)
.venv\Scripts\python.exe -m pytest -q
```

The first vision call needs the `buffalo_l` model pack (~275 MB) —
`scripts/bootstrap_model.py` pre-downloads it.

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

## Repo layout

```
contracts/schemas.py     executable CONTRACTS.md — pydantic models, extra="forbid"
services/vision.py       face detect + encode (InsightFace buffalo_l, CPU)
services/search.py       reverse-image retrieval (Google Vision / SerpAPI fallback)
services/verification.py independent re-fetch + re-encode + re-score
services/blockchain.py   canonical record, Pinata pin, Amoy anchoring, re-verification
app/main.py              FastAPI job manager + §5 data-lineage event log
solidity/AnchorRecord.sol  minimal anchoring contract (solc 0.8.24)
tests/                   74 tests: contract conformance + every unhappy path
```
