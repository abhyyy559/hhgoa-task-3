"""scripts/qa_matrix.py — Phase 3 QA matrix, machine-runnable subset.

Runs the REAL buffalo_l model over a generated matrix and records ACTUAL
results (TASK3_ARCHITECTURE.md §9):

  - no-face cases (3):        noise / gradient / blank canvases
  - multiple-faces cases (3): composites of a real detected face
  - bad-quality cases (5):    blurred / downscale-up / noisy / dark / pixelated
  - same-person pair (1):     full image vs. its own face crop

The same-person/different-person PAIR COUNTS (5-10 / 10+) still need real
team photos (HUMAN_ACTIONS H4) — those rows stay BLOCKED-HUMAN; do NOT treat
this partial matrix as threshold proof. Run:

    .venv\\Scripts\\python.exe scripts\\qa_matrix.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services import vision as vision_service  # noqa: E402


def bgr(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def load_base_with_face():
    """Real face from skimage's bundled astronaut photo (ships with the pkg)."""
    from skimage import data

    img = bgr(data.astronaut())
    out = vision_service.detect_and_encode(img)
    if out.status not in (vision_service.VisionStatus.OK, vision_service.VisionStatus.MULTIPLE_FACES_DETECTED) or out.embedding is None:
        raise SystemExit(f"astronaut face not detected: {out.status}")
    x1, y1, x2, y2 = out.bbox
    pad = 10
    face = img[max(0, y1 - pad): min(img.shape[0], y2 + pad),
               max(0, x1 - pad): min(img.shape[1], x2 + pad)]
    return img, face, out


def run_case(name: str, image: np.ndarray, results: list) -> None:
    try:
        out = vision_service.detect_and_encode(image)
        emb = "yes" if out.embedding else "no"
        print(f"  {name:<38} {out.status.value:<24} "
              f"quality={out.quality_score:.3f} embedding={emb} bbox={out.bbox}")
        results.append((name, out.status.value, out.quality_score, out.embedding))
    except Exception as exc:  # noqa: BLE001
        print(f"  {name:<38} EXCEPTION {type(exc).__name__}: {exc}")
        results.append((name, "EXCEPTION", 0.0, None))


def main() -> int:
    print("=" * 72)
    print("PHASE 3 QA MATRIX — real buffalo_l model, CPU")
    print("=" * 72)
    base, face_crop, base_out = load_base_with_face()
    print(f"\nbase image: {base.shape}, face bbox={base_out.bbox}, "
          f"quality={base_out.quality_score:.3f}")

    results: list = []
    print("\n-- no-face cases (expect NO_FACE_DETECTED) --")
    rng = np.random.default_rng(7)
    run_case("noise", rng.integers(0, 256, (480, 640, 3), dtype=np.uint8), results)
    run_case("blank", np.full((480, 640, 3), 128, np.uint8), results)
    grad_row = np.linspace(0, 255, 640, dtype=np.uint8)
    grad = np.broadcast_to(grad_row[None, :, None], (480, 640, 3)).copy()
    run_case("gradient", grad, results)

    print("\n-- multiple-faces cases (expect MULTIPLE_FACES_DETECTED) --")
    for i, (scale, pos) in enumerate([(1.0, (10, 10)), (0.6, (20, 400)), (0.8, (300, 60))]):
        canvas = base.copy()
        h, w = face_crop.shape[:2]
        small = cv2.resize(face_crop, (max(32, int(w * scale)), max(32, int(h * scale))))
        y0, x0 = pos
        region = canvas[y0: y0 + small.shape[0], x0: x0 + small.shape[1]]
        if region.shape[:2] == small.shape[:2]:
            canvas[y0: y0 + small.shape[0], x0: x0 + small.shape[1]] = small
        run_case(f"duplicated-face variant {i + 1}", canvas, results)

    print("\n-- bad-quality cases (expect OK or LOW_IMAGE_QUALITY; record honestly) --")
    run_case("gaussian-blur", cv2.GaussianBlur(base, (31, 31), 12), results)
    small = cv2.resize(base, (24, 24), interpolation=cv2.INTER_NEAREST)
    run_case("downscale-upscale-24px", cv2.resize(small, (base.shape[1], base.shape[0]),
                                                  interpolation=cv2.INTER_NEAREST), results)
    small64 = cv2.resize(base, (64, 64), interpolation=cv2.INTER_LINEAR)
    run_case("downscale-upscale-64px", cv2.resize(small64,
                                                  (base.shape[1], base.shape[0]),
                                                  interpolation=cv2.INTER_LINEAR), results)
    noisy = base.astype(np.int16) + rng.normal(0, 40, base.shape).astype(np.int32)
    run_case("heavy-noise", np.clip(noisy, 0, 255).astype(np.uint8), results)
    run_case("darkened", (base * 0.15).astype(np.uint8), results)
    pix = cv2.resize(base, (16, 16), interpolation=cv2.INTER_LINEAR)
    run_case("pixelated", cv2.resize(pix, (base.shape[1], base.shape[0]),
                                     interpolation=cv2.INTER_NEAREST), results)

    print("\n-- same-person pair (full image vs its own face crop) --")
    # Loosen the crop: expand the bbox by 40% on each side, then upscale so the
    # detector gets a big enough face to work with.
    x1, y1, x2, y2 = base_out.bbox
    ex = int((x2 - x1) * 0.4)
    ey = int((y2 - y1) * 0.4)
    loose = base[max(0, y1 - ey): min(base.shape[0], y2 + ey),
                 max(0, x1 - ex): min(base.shape[1], x2 + ex)]
    scale = 320 / max(loose.shape[:2])
    if scale > 1:
        loose = cv2.resize(loose, None, fx=scale, fy=scale)
    crop_out = vision_service.detect_and_encode(loose)
    if base_out.embedding and crop_out.embedding:
        a = np.asarray(base_out.embedding)
        b = np.asarray(crop_out.embedding)
        sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        print(f"  cosine(full, face-crop) = {sim:.4f}  "
              f"(zone: HIGH >= 0.48 / UNCERTAIN >= 0.35 / LOW below)")
        same_person_sims = [sim]
    else:
        print(f"  crop not usable: {crop_out.status.value}")
        same_person_sims = None

    print("\n" + "=" * 72)
    n_multi = sum(1 for r in results if r[1] == "MULTIPLE_FACES_DETECTED")
    n_none = sum(1 for r in results if r[1] == "NO_FACE_DETECTED")
    n_lq = sum(1 for r in results if r[1] == "LOW_IMAGE_QUALITY")
    n_ok = sum(1 for r in results if r[1] == "OK")
    print(f"SUMMARY: no_face={n_none}/3  multi_face={n_multi}/3  "
          f"bad_quality: OK={n_ok} LOW_IMAGE_QUALITY={n_lq} of 5")
    print("PAIR COUNTS: same-person 1 pair, different-person 0 pairs —")
    print("the §9 minimum (5-10 / 10+) needs real team photos (H4). This")
    print("matrix is PARTIAL evidence; do not finalize thresholds on it alone.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
