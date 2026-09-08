"""scripts/reseed_gallery.py — replace the demo gallery with real-face entries.

The initial demo gallery was seeded with hand-drawn cartoon avatars
(``cv2.circle`` head + dot eyes). InsightFace correctly refuses to detect a
face in a cartoon, so every gallery candidate returned NO_FACE_DETECTED during
independent verification and no match was ever possible — a broken demo.

This one-shot maintenance script rewrites the gallery index and its images with
public-domain photos that contain REAL, detectable faces (skimage samples), so
the end-to-end pipeline can demonstrate a genuine MATCH -> on-chain anchor.

It clears ONLY the auto-generated demo directory and re-seeds it. Run it once:

    .venv\\Scripts\\python.exe scripts\\reseed_gallery.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    import cv2

    from services import gallery, vision as vis

    gallery._ensure_gallery_dirs()

    # Remove the auto-seeded demo entries and their image files by name (the
    # ids/filenames come from the current index — no wildcards).
    members = gallery.list_enrolled_members()
    print(f"clearing {len(members)} existing demo entr(y/ies) ...")
    images_dir = gallery.GALLERY_DIR / "images"
    for m in members:
        img_path = images_dir / m.image_filename
        if img_path.exists():
            img_path.unlink()
            print(f"  removed {img_path.name}")
    if gallery.GALLERY_INDEX_FILE.exists():
        gallery.GALLERY_INDEX_FILE.unlink()
        print(f"  removed {gallery.GALLERY_INDEX_FILE.name}")

    # Re-seed with real faces.
    gallery.seed_default_demo_identities()

    print("\nverifying seeded faces are detectable:")
    ok = True
    for m in gallery.list_enrolled_members():
        img = cv2.imread(str(images_dir / m.image_filename))
        detected = vis.detect_and_encode(img)
        status = detected.status.value
        if status != "OK":
            ok = False
        print(f"  {m.member_id}  {m.full_name:<16} -> {status}")

    print(f"\n  -> gallery re-seeded with detectable real faces: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
