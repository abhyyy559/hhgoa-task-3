# HUMAN_ACTIONS.md — What Only YOU Can Do

> Everything here is something the coding agents **cannot do for you** — accounts, keys, physical photos, consent, and human judgment calls.
> Items are ordered by urgency. **Phase 0 is blocked without H1–H4.**
> When you complete an item, tick the box and put the value where indicated (never paste secrets into chat or commit them — they go in `.env`, which is gitignored).

---

## CRITICAL — Blocks Phase 0 (do these first, ~30–45 min total)

### H1. Google Cloud Vision API key — `[ ]`
**Why:** SearchService's primary provider (real reverse image search). Free tier = 1,000 lookups/month — plenty, but **billing must be enabled on the GCP project even for the free tier**.
**Steps:**
1. Go to https://console.cloud.google.com → create (or pick) a project.
2. Billing → enable billing on that project (a card is required; Vision's free 1,000 units/month means you will not be charged at our usage).
3. APIs & Services → Library → search **Cloud Vision API** → **Enable**.
4. APIs & Services → Credentials → **Create credentials → API key**.
5. (Recommended) Restrict the key to the Vision API only.
**Provide:** put the key in `.env` as `GOOGLE_VISION_API_KEY=...`

### H2. Pinata account + API JWT — `[ ]`
**Why:** IPFS content-addressed storage for the on-chain record's `content_cid`. (web3.storage's free tier no longer exists — Pinata is the verified choice.)
**Steps:**
1. Sign up free at https://pinata.cloud (free tier: 1 GB storage, 500 files — sufficient).
2. API Keys → **New key** (Admin permissions) → copy the **JWT** (the long `eyJ...` string).
**Provide:** `.env` → `PINATA_JWT=...` and your gateway if you set one: `PINATA_GATEWAY=<yoursubdomain>.mypinata.cloud`

### H3. Funded Polygon Amoy testnet wallet — `[ ]`
**Why:** Signs the on-chain anchor transactions. Testnet POL is free.
**Steps:**
1. Install MetaMask (browser extension) → create wallet.
2. Add the **Polygon Amoy** network manually if not listed: RPC `https://polygon-amoy.drpc.org` · Chain ID `80002` · Symbol `POL` · Explorer `https://amoy.polygonscan.com`.
3. Copy your wallet address, go to the faucet: https://faucet.polygon.technology → choose **Amoy** → paste address → request test POL. (If the official faucet is congested, Alchemy/QuickNode Amoy faucets also work.)
4. In MetaMask: account details → **Export private key** (this is a throwaway testnet wallet — never reuse a real one).
**Provide:** `.env` → `AMOY_PRIVATE_KEY=...` and `AMOY_WALLET_ADDRESS=...` (RPC endpoints are already decided, no action needed).

### H4. Real photos of a team member with a public web/social presence — `[ ]`
**Why:** THE riskiest assumption in the whole project is "Google Vision Web Detection returns usable candidates for a real face." Phase 0 tests this on real data **today**, not on demo day. Without these photos, nothing proceeds.
**What I need:**
- 3–5 photos of **one or more team members who have photos on public web pages or public social profiles** (Instagram/LinkedIn/X public posts, a public blog, etc.).
- The URLs of the pages where those photos publicly appear (so Phase 0 can verify the API's returned URLs are correct and fetchable).
- Plain JPG/PNG, face clearly visible, decent lighting. Put them in `data/phase0_photos/` (I'll create the folder) or just drop them in the project root and tell me.
**Judgment call you own:** pick a person whose face actually appears on the public web. If nobody on the team qualifies, tell me NOW — the demo strategy needs to change (e.g., use a consenting public figure's photo offline, or lean on SerpAPI), and that's a conversation we need before building.

---

## REQUIRED — By Phase 0/1 (Day 1–2)

### H5. Written team-member consent — `[ ]`
**Why:** The demo runs face search on a team member's own face. A documented consent record is both the ethical minimum and a DPDP-ready control a judge may ask about.
**Steps:** Create one line per person: *"I, <full name>, consent to my likeness being used for face detection, web search, and blockchain anchoring in the HH Goa 2026 Task 3 demo, on this date."* → save as `docs/consent.md` (I can scaffold it; you fill and sign it).

### H6. GitHub repo + push access — `[ ]`
**Why:** Submission requires a GitHub repo with `CONTRACTS.md` at root.
**Steps:**
1. Create an empty repo (no auto-README, so our docs push cleanly).
2. Make sure `git push` works from this machine (your credentials/SSH are set up).
**Provide:** the repo URL; I'll set the remote and handle commits.

---

## LATER — Phase 4–5 (Day 5–7)

### H7. SerpAPI key (OPTIONAL fallback) — `[ ]`
**Why:** Documented secondary search provider if the primary fails after retries — and the pivot target if Phase 0 shows Google Vision coverage is thin for your face.
**Steps:** Only needed if Phase 0 results are weak OR you want belt-and-suspenders: sign up at https://serpapi.com (free tier exists) → copy API key → `.env` → `SERPAPI_KEY=...`
**Defer this until Phase 0 reports.**

### H8. Demo-day logistics — `[ ]`
- Working camera on the demo device (photo is captured LIVE in front of judges — that's the anti-hardcoding proof).
- Stable internet backup plan (phone hotspot).
- A dry-run of the full flow the day before recording.
- Choose the on-camera presenter = the consenting person from H4/H5.

### H9. Screen recording — `[ ]`
Flow to record (I'll produce a shot-by-shot script): live face capture → real search call visible in the event log → independent verification → blockchain anchor → Polygonscan link → re-verification → pixel-tamper failure case.

### H10. Defense Pack rehearsal — `[ ]`
Rehearse `TASK3_ARCHITECTURE.md` §10 out loud with a teammate playing judge. The two answers that must be word-perfect: "independent execution path, not statistical independence" and "blockchain proves integrity, not identity."

### H11. Submission form — `[ ]`
https://forms.gle/oZbQGuwiNeHVcHWo8 — fill it AFTER the repo and recording are final. Spec says no resubmissions.

---

## Things you do NOT need anymore (changed by architecture v2 + research)

| Previously planned | Why it's gone |
|---|---|
| LLM API key (OpenAI/Anthropic/etc.) | CrewAI was dropped in v2 — the pipeline is deterministic services; no runtime LLM calls |
| Visual Studio C++ Build Tools | `insightface==1.0.1` ships a pure wheel; no compiler needed |
| web3.storage account | Service pivoted to paid product; Pinata replaces it |
| Node.js / Hardhat / Foundry for Solidity | py-solc-x compiles in pure Python on Windows |

---

## Quick reference — what `.env` should contain when you're done with H1–H3

```env
GOOGLE_VISION_API_KEY=AIza...          # H1
PINATA_JWT=eyJ...                       # H2
AMOY_PRIVATE_KEY=0x...                  # H3 (testnet wallet ONLY)
AMOY_WALLET_ADDRESS=0x...
# Optional / deferred:
SERPAPI_KEY=...                         # H7 — only if Phase 0 says so
```
`.env` will be gitignored; `.env.example` (no real values) will be committed.
