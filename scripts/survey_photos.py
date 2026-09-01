"""Survey the user-uploaded test images: detect faces, report status/quality.

Used to decide whether these can seed the §9 pair matrix. Does not modify
anything and never commits the images.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2

from services import vision as vision_service

d = Path("test imgs")
if not d.is_dir():
    print("no test imgs dir")
    raise SystemExit(0)

for p in sorted(d.iterdir()):
    if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        continue
    img = cv2.imread(str(p))
    if img is None:
        print(f"{p.name}: unreadable")
        continue
    out = vision_service.detect_and_encode(img)
    embed = "yes" if out.embedding else "no"
    print(f"{p.name:<32} {out.status.value:<22} quality={out.quality_score:.3f} "
          f"bbox={out.bbox} embedding={embed}")