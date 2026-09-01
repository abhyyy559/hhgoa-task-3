"""Pre-download the InsightFace buffalo_l model pack so no agent blocks on it."""
import sys

sys.path.insert(0, r"C:\Users\home\OneDrive\Documents\hhgoaa\hhgoa task-3")

from insightface.app import FaceAnalysis  # noqa: E402

app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
app.prepare(ctx_id=-1, det_size=(640, 640))
print("MODEL READY")
