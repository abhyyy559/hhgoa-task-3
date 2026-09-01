"""scripts/validate_search.py — Phase 0: validate the riskiest assumption.

Calls the REAL Google Vision Web Detection API on 3-5 photos of team members
from data/phase0_photos/ and logs the raw result per photo, so the choice of
primary search provider is made on evidence, not hope (MULTI_AGENT_BUILD_PLAN
Phase 0). Run BEFORE committing to Google Vision as primary — if coverage is
thin, pivot to SerpAPI while it's still cheap.

Usage:
    1. Drop 3-5 real photos into data/phase0_photos/  (HUMAN_ACTIONS H4)
    2. Set GOOGLE_VISION_API_KEY in .env               (HUMAN_ACTIONS H1)
    3. .venv\\Scripts\\python.exe scripts\\validate_search.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PHOTOS_DIR = PROJECT_ROOT / "data" / "phase0_photos"
RESULTS_PATH = PROJECT_ROOT / "data" / "phase0_search_results.json"


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    import os

    if not os.getenv("GOOGLE_VISION_API_KEY"):
        print("=" * 72)
        print("PHASE 0 SEARCH VALIDATION — blocked on credentials")
        print("=" * 72)
        print("  GOOGLE_VISION_API_KEY is not set (HUMAN_ACTIONS.md H1).")
        print("  1. GCP console -> enable Cloud Vision API -> billing on -> API key")
        print("  2. paste into .env")
        print("  3. put 3-5 real team photos in data/phase0_photos/ (H4)")
        print("  4. re-run this script")
        print("\nRESULT: BLOCKED-HUMAN")
        return 2

    if not PHOTOS_DIR.is_dir() or not any(PHOTOS_DIR.iterdir()):
        print(f"No photos found in {PHOTOS_DIR} — see HUMAN_ACTIONS.md H4.")
        print("\nRESULT: BLOCKED-HUMAN")
        return 2

    from services.search import get_call_log, search

    print("=" * 72)
    print("PHASE 0 SEARCH VALIDATION — real Vision Web Detection calls")
    print("=" * 72)
    results = []
    for photo in sorted(PHOTOS_DIR.glob("*")):
        if photo.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        try:
            out = search(photo.read_bytes(), image_url=None)
        except Exception as exc:  # noqa: BLE001
            print(f"\n{photo.name}: EXCEPTION {type(exc).__name__}: {exc}")
            results.append({"photo": photo.name, "error": str(exc)})
            continue
        entry = {
            "photo": photo.name,
            "status": out.status.value,
            "candidate_count": len(out.candidates),
            "candidate_urls": [c.candidate_url for c in out.candidates[:10]],
        }
        results.append(entry)
        print(f"\n{photo.name}: {out.status.value} candidates={len(out.candidates)}")
        for url in entry["candidate_urls"][:5]:
            print(f"    {url}")

    print(f"\ncall log entries: {len(get_call_log())}")
    RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"raw results written to {RESULTS_PATH}")
    print("\nDecision rule: >=3 photos with real candidate pages -> keep Google")
    print("Vision primary; thin coverage -> pivot to SerpAPI primary (H7).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
