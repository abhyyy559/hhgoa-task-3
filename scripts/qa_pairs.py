"""scripts/qa_pairs.py — the §9 same/different-person pair matrix (real model).

Person labels are taken from the FILENAME: the label is everything before
the trailing run of digits / "-N" / " (N)" (e.g. ``alice-1.jpg`` and
``alice-2.jpg`` -> same person "alice"; ``bob-1.jpg`` -> different person).

Since verification itself operates per-candidate and the query embedding is
always the "true" embedding of one image, here the same logic is used
pairwise: detect+encode both images with the real buffalo_l model, take the
cosine similarity of the two embeddings, and bucket it into the §3 zones.

Usage:
    Put team photos in data/phase0_photos/  (HUMAN_ACTIONS H4), then:
    .venv\\Scripts\\python.exe scripts\\qa_pairs.py

Output is the honest results table for the README "known limitations" —
numbers as-measured, not as-wished.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
PHOTOS_DIR = PROJECT_ROOT / "data" / "phase0_photos"


def person_label(stem: str) -> str:
    label = re.sub(r"[_\s(]*-?\d{1,3}\s*\)?$", "", stem)
    return label.strip().lower() or stem.lower()


def main() -> int:
    from services import vision as vision_service

    if not PHOTOS_DIR.is_dir():
        print(f"missing {PHOTOS_DIR} — see HUMAN_ACTIONS.md H4")
        return 2

    images = sorted(
        p for p in PHOTOS_DIR.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if len(images) < 2:
        print("need at least 2 photos for a pair matrix")
        return 2

    # --- detect + encode every image once -----------------------------------
    detections: dict[str, tuple[str, list[float], float]] = {}
    for p in images:
        img = cv2.imread(str(p))
        if img is None:
            print(f"  ! {p.name}: unreadable")
            continue
        out = vision_service.detect_and_encode(img)
        if out.status == vision_service.VisionStatus.OK and out.embedding:
            detections[p.name] = (person_label(p.stem), out.embedding, out.quality_score)
            print(f"  ok  {p.name:<28} person={person_label(p.stem):<12} "
                  f"quality={out.quality_score:.3f}")
        else:
            print(f"  !!  {p.name}: {out.status.value} — skipped")

    names = list(detections)
    if len(names) < 2:
        print("\nnot enough usable single-face images for pairs")
        return 2

    # --- pairwise similarity + bucketing -------------------------------------
    print(f"\n{'pair':<34} {'label':<10} {'cosine':>8}  {'zone':<10} verdict")
    rows: list[tuple[str, str, str, float, str]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a_name = names[i]
            b_name = names[j]
            a_label, a_emb, _a_q = detections[a_name]
            b_label, b_emb, _b_q = detections[b_name]
            a, b = np.asarray(a_emb), np.asarray(b_emb)
            sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
            same = a_label == b_label
            if sim >= 0.48:
                zone = "HIGH"
            elif sim >= 0.35:
                zone = "UNCERTAIN"
            else:
                zone = "LOW"
            verdict = "match" if same else "no_match"
            rows.append((a_name, b_name, zone, sim, verdict))
            print(f"  {a_name[:14]}+{b_name[:14]:<18} "
                  f"{'same' if same else 'diff':<10} {sim:>8.4f}  "
                  f"{zone:<10} {verdict}")

    # --- honest summary vs §9 minimums ---------------------------------------
    same_rows = [r for r in rows if r[4] == "match"]
    diff_rows = [r for r in rows if r[4] == "no_match"]
    correct = sum(
        1 for r in rows
        if (r[4] == "match" and r[2] in ("HIGH", "UNCERTAIN"))
        or (r[4] == "no_match" and r[2] == "LOW")
    )
    print("\n" + "=" * 72)
    print(f"SAME-PERSON pairs:     {len(same_rows)}  (min 5-10 per §9)")
    print(f"DIFFERENT-PERSON pairs: {len(diff_rows)}  (min 10+ per §9)")
    if same_rows:
        mean_same = float(np.mean([r[3] for r in same_rows]))
        print(f"same-person mean cosine: {mean_same:.4f}  "
              f"(threshold zone {max(r[2] for r in same_rows)})")
    if diff_rows:
        print(f"different-person max cosine: {max(r[3] for r in diff_rows):.4f}")
    print(f"classification agreement (match->HIGH/UNCERTAIN, "
          f"no_match->LOW): {correct}/{len(rows)}")
    print("\nNOTE: this is the evidence for the README + defense pack. If the")
    print("thresholds under-perform, propose a revised threshold from THIS")
    print("matrix — do not quietly adjust the code to make numbers look good.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())