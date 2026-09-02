# Consent Record (HUMAN_ACTIONS.md H5)

This file records informed, written consent from every person whose photo is
run through this pipeline. **No photo should be processed until its row is
signed.** This is a legal/ethical safeguard, not optional paperwork — a judge
will ask for it, and "we meant to get it" is not a defense.

## How to use

1. For each participant, fill in their name, the social/web handle the photo
   is expected to be found under, and the photo file used.
2. Have the participant sign (digital signature or scanned handwritten).
3. Record the date. Keep the signed originals; this file is the index.

| # | Participant name | Handle / web reference | Photo file | Consent signature | Date |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |

## What consent covers

By signing, each participant confirms they understand and agree that:

- Their photo will be sent to **Google Cloud Vision API Web Detection** to find
  public web pages where that photo appears.
- Candidate pages will be **independently fetched and re-verified** (a second
  face-detection + similarity pass, never trusting search's own judgment).
- A **content fingerprint** (SHA-256 hash) of the verification result — never
  the embedding, raw URL, or image bytes — will be anchored on the **Polygon
  Amoy testnet** and pinned to **Pinata IPFS**.
- The on-chain record proves only **content integrity** ("this fingerprint was
  anchored at this time and hasn't changed"), **not** that any post belongs to
  them.
- IPFS pinning is **not a permanence guarantee** (free tier retention).

## Removal

A participant may withdraw consent at any time. On request, delete their photo
from `data/phase0_photos/` (or `test imgs/`) and note the withdrawal above. Note:
an already-anchored on-chain hash cannot be deleted (it's immutable by design),
but it contains no biometric data and no raw URL — only a content fingerprint.

---

*This file is versioned with the repo. Git history is the audit trail.*
