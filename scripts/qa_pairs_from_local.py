"""scripts/qa_pairs_from_local.py — §9 pair matrix from a local folder.

The §9 same-person / different-person minimums (5-10 / 10+) need 2+ photos per
person. This builds that matrix from whatever usable images already exist
locally (e.g. the gitignored ``test imgs/`` folder) with the real buffalo_l
model, and reports the honest numbers. Put 2+ photos per person in a folder,
name them ``personA-1.jpg``, ``personA-2.jpg``, ``personB-1.jpg`` ... and run:

    .venv\\Scripts\\python.exe scripts\\qa_pairs_from_local.py \"test imgs\"
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

MIN_SAME = 5
MIN_DIFF = 10


def person_label(stem: str) -> str:
    label = re.sub(r"[_\s(]*-?\d{1,3}\s*\)?$", "", stem)
    return label.strip().lower() or stem.lower()


def main() -> int:
    from services import vision as vision_service

    import os

    photos_dir = Path(os.environ.get("QA_DIR", sys.argv[1] if len(sys.argv) > 1 else "test imgs"))
    if not photos_dir.is_dir():
        print(f"missing folder: {photos_dir}")
        return 2

    images = sorted(
        p for p in photos_dir.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if len(images) < 2:
        print("need at least 2 images for a pair matrix")
        return 2

    detections: dict[str, tuple[str, list[float], float]] = {}
    print(f"folder: {photos_dir}")
    for p in images:
        img = cv2.imread(str(p))
        if img is None:
            print(f"  ! {p.name}: unreadable")
            continue
        out = vision_service.detect_and_encode(img)
        if out.status == vision_service.VisionStatus.OK and out.embedding:
            detections[p.name] = (person_label(p.stem), out.embedding, out.quality_score)
        else:
            print(f"  !! {p.name}: {out.status.value} — skipped")

    names = list(detections)
    if len(names) < 2:
        print("\nnot enough usable single-face images for pairs")
        return 2

    for n in names:
        label, _emb, q = detections[n]
        print(f"  ok  {n:<28} person={label:<12} quality={q:.3f}")

    print(f"\n{'pair':<34} {'label':<10} {'cosine':>8}  {'zone':<10} verdict")
    rows: list[tuple[str, str, str, float, str]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a_name, b_name = names[i], names[j]
            a_label, a_emb, _ = detections[a_name]
            b_label, b_emb, _ = detections[b_name]
            va, vb = np.asarray(a_emb), np.asarray(b_emb)
            sim = float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb)))
            same = a_label == b_label
            zone = "HIGH" if sim >= 0.48 else ("UNCERTAIN" if sim >= 0.35 else "LOW")
            verdict = "match" if same else "no_match"
            rows.append((a_name, b_name if b_name == a_name else b_name, zone, sim, verdict))
            print(f"  {a_name[:13]}+{b_name[:13]:<19} "
                  f"{'same' if same else 'diff':<10} {sim:>8.4f}  "
                  f"{zone:<10} {verdict}")

    same_rows = [r for r in rows if r[4] == "match"]
    diff_rows = [r for r in rows if r[4] == "no_match"]
    correct = sum(
        1 for r in rows
        if (r[4] == "match" and r[2] in ("HIGH", "UNCERTAIN"))
        or (r[4] == "no_match" and r[2] == "LOW")
    )
    print("\n" + "=" * 72)
    print(f"SAME-PERSON pairs:     {len(same_rows)}  (min {MIN_SAME}-10 per §9)")
    print(f"DIFFERENT-PERSON pairs: {len(diff_rows)}  (min {MIN_DIFF}+ per §9)")
    if same_rows:
        print(f"same-person mean cosine: {np.mean([r[3] for r in same_rows]):.4f}  "
              f"(zone {max(r[2] for r in same_rows)})")
    if diff_rows:
        print(f"different-person max cosine: {max(r[3] for r in diff_rows):.4f}")
    print(f"classification agreement (match->HIGH/UNCERTAIN, no_match->LOW): "
          f"{correct}/{len(rows)}")
    print("NOTE: evidence for README + defense pack. If thresholds under-perform,")
    print("propose a revised threshold from THIS matrix — do not quietly adjust")
    print("code to make numbers look good.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())