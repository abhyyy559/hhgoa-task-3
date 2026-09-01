import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2
from skimage import data

cv2.imwrite("data/demo_face.png", cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR))
print("demo image saved: data/demo_face.png")
