# HANDOFF BRIEF — HH Goa 2026 · Task 3 · Face Identification & Blockchain Verification

> Written Sept 7, 2026 (deadline day, 11:59 PM). For a technically strong friend joining cold.
> Design truth: `CONTRACTS.md` (v3). Run instructions: `README.md`. This file = where we stand + where we're stuck.

---

## 1. What this is (30 seconds)

Upload any face → detect + 512-d ArcFace embedding (InsightFace `buffalo_l`, CPU) → **real reverse-image web search** (SerpAPI Lens primary) → **independent** re-fetch/re-detect/re-score per candidate (never trusts search scores) → social-domain tagging → canonical record → IPFS pin (Pinata) → anchor on Polygon Amoy → tamper-evidence re-verification. Live event log streams every stage (SSE). No website required; minimal UI at `/`.

```
image → Vision → Search (retrieval ONLY) → Verification (independent)
      → Record → IPFS → Amoy → re-verify
```

**Non-negotiable laws** (`CONTRACTS.md`, enforced by pydantic `extra="forbid"`, not convention):
- Search never scores; only the backend emits match verdicts.
- `faces_checked == 0` + `no_match` = **UNVERIFIED** (no face ever scored), never a genuine rejection.
- Chain proves **integrity, not identity** (stores hashes only — no embeddings, URLs, or image bytes on-chain).

---

## 2. PROVEN WORKING (with evidence, all Sept 7)

| Claim | Evidence |
|---|---|
| Face engine accurate | Offline matrix, real model: same-person 0.919 (alice-2↔Aarav), different-person max ~0.07, identical 1.000 |
| Enrolled-gallery match → full chain path | Live run: 0.9186 HIGH → record → IPFS `QmXiLDTv…` → anchor attempted (blocked only by funds, §3.1) |
| Open-web face search works | Live SerpAPI Lens: **59 candidates, 19 social**, top match 0.9116 (IMDb image), IPFS `QmZVmtJ8…` pinned |
| Honest rejection works | Stranger photo vs gallery: 0.05 / −0.04 → `PIPELINE_NO_CONFIDENT_MATCH`, no fake match |
| Test suite green | **104 passed** (`pytest tests/ -q`): backend, search, verification, blockchain, gallery |
| UNVERIFIED vs scored rejection | Login-walled pages label `UNVERIFIED — …`, retry next candidate with `candidate_retry` events |
| Candidate resolver tiers | `full_match → partial_match → page → visually_similar` + `match_type` provenance; pages carry no borrowed thumbnails |
| Celebrity/name hints | Vision `web_entities` surfaced ("Google suggests"); SerpAPI path returns them when present |
| Result richness | `candidate_lineup` (URL, social tag, `found_via`, score, thumbnail, handles) rendered in UI digital-presence panel |

---

## 3. WHERE WE'RE STUCK (ranked)

### 3.1 RESOLVED — Amoy wallet funded (was the blocker)
- Faucet top-up landed: **0.1079 MATIC** (was 0.0079 vs ~0.0128 needed). Live proof: open-web match anchored at **block 46976822** (tx `bbfdcbf9…`), re-verification intact.
- Recording run: `test imgs/alice-2.jpg` for the fast deterministic path, or any indexed stranger photo for the full open-web story.

### 3.2 STRUCTURAL — Open-web recall depends on indexation, not code
- Findability = is the person's photo publicly indexed, not follower count. 200-follower public accounts CAN hit; private accounts are invisible to everyone, legally.
- Provider reality (all verified live): Google Vision key present but **HTTP 403** (billing/API not enabled — GCP console human action, deprioritized since Lens works); DuckDuckGo has **no reverse-image capability** (text junk, correctly rejected); Bing retired Aug 2025; Google Custom Search closed to new signups; PimEyes/Clearview-style excluded (EU/UK/AU fines/bans).
- Working path: **SerpAPI Lens** (key configured, 250 searches/mo free; each upload ≈ 1–3 calls) + **Yandex diversity leg** (same key, auto-skipped when Lens already returns ≥10). `SEARCH_PROVIDER=auto` in `.env`.
- Precondition for graded demo (v3 §8): confirm subject has discoverable public photos days ahead; have them post one publicly.
- Question for friends: any other *legal* face-index API with a free tier? If yes, it slots in as one `found_via` leg — the merge/dedupe layer already exists.

### 3.3 SLOW, not broken — stranger runs take minutes
- 59 candidates × image downloads (20 s timeout each) + model scoring on heavy pages. Bounded but slow; progress prints (`[VerificationService] …`) now narrate it in the server log.
- Question for friends: safe ways to cut this (parallel thumbnail fetch with cap? smaller `MAX_IMAGE_ATTEMPTS`? lower image timeout?) without weakening the independence claim.

### 3.4 SPARSE — social-page metadata is thin
- Instagram/X/LinkedIn login-wall scraping, so handles/bios come mostly from URL patterns + thumbnails, not page HTML (thumbnail-first design, deliberate).
- Question for friends: legal enrichment ideas (oEmbed endpoints? profile-URL username parsing already done)?

### 3.5 NOT YET DONE — demo-day items
- Threshold matrix §9: have 1 same-person pair (0.919) + diff max 0.07; need 5–10 same-person pairs for a defensible table (use team/friend photos).
- `docs/consent.md` (one line per demo subject), GitHub repo + push, screen recording (shot script: live capture → Lens call visible → social tag → verify → Polygonscan → pixel-tamper fail), submission form `forms.gle/oZbQGuwiNeHVcHWo8`.

---

## 4. ENV / CONFIG STATE (no secret values here — `.env` is gitignored)

| Key | State | Effect |
|---|---|---|
| `GOOGLE_VISION_API_KEY` | SET, live-tested **403** | Vision leg dead until billing/API enabled in GCP console |
| `SERPAPI_KEY` | SET, live-tested working | Lens primary + Yandex leg operational |
| `PINATA_JWT` | SET, pinning verified live | IPFS CIDs issuing (`Qm…` above) |
| `AMOY_PRIVATE_KEY` / `AMOY_WALLET_ADDRESS` | SET, throwaway testnet wallet, **unfunded** | §3.1 |
| `SEARCH_PROVIDER` | `auto` | Lens → Vision order; `federated_web` = max recall (Lens+Yandex+Vision) |
| Gallery | 2 enrolled (Aarav Sharma, Priya Nair) | Demo fallback when web empty; matches labeled `enrolled_gallery` |

---

## 5. HOW TO VERIFY ANY CLAIM HERE

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q          # 101 tests, ~40 s
.venv\Scripts\python.exe scripts\go_live.py amoy       # chain + balance
.venv\Scripts\python.exe -c "from services import search as S; out=S.search_serpapi_lens(open('test imgs/virat-kohli-wallpaper-4k.webp','rb').read()); print(out.status, len(out.candidates))"
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000   # UI at http://127.0.0.1:8000
```

Face-match matrix (offline, no pipeline): `C:\Users\home\AppData\Local\Temp\opencode\facematrix.py` (edit ROOT if moved).

**Reading the SSE log:** `similarity 0.000 + faces_checked 0` with `UNVERIFIED —` prefix = could not compare (keep going). Real score + `faces_checked ≥ 1` below 0.35 = genuine rejection. `match_source` on `record_built` tells gallery-demo apart from open-web-social proof.

---

## 6. KEY FILES

- `CONTRACTS.md` — v3 contract law + amendment log (read first)
- `app/main.py` — pipeline orchestration, job store (off-chain URL map), SSE
- `services/search.py` — Lens/Yandex/Vision ensemble, resolver tiers, social allowlist
- `services/verification.py` — thumbnail-first, multi-face max-score, UNVERIFIED semantics
- `services/blockchain.py` + `solidity/AnchorRecord.sol` — canonical hash spec, Amoy anchoring
- `contracts/schemas.py` — executable contracts (`extra="forbid"`)
- `tests/` — 101 tests · `test imgs/` — 6-image probe set (alice pair = same-person proof)
