"""Robust buffalo_l download: clean previous bad artifacts, download with
retries, verify zip CRC before extracting, then load the model."""
import shutil
import time
import zipfile
from pathlib import Path

import requests

sys_path = r"C:\Users\home\OneDrive\Documents\hhgoaa\hhgoa task-3"
if sys_path not in sys.path:
    import sys
    sys.path.insert(0, sys_path)

ROOT = Path(r"C:\Users\home\.insightface\models")
ZIP = ROOT / "buffalo_l.zip"
DEST = ROOT / "buffalo_l"
URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"

ROOT.mkdir(parents=True, exist_ok=True)

for attempt in range(1, 6):
    print(f"=== attempt {attempt}: cleaning + downloading ===", flush=True)
    shutil.rmtree(DEST, ignore_errors=True)
    if ZIP.exists():
        ZIP.unlink()
    try:
        with requests.get(URL, stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp = ZIP.with_suffix(".zip.part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            tmp.replace(ZIP)
        print("download done, verifying CRC...", flush=True)
        with zipfile.ZipFile(ZIP) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise IOError(f"corrupt member in zip: {bad}")
            zf.extractall(ROOT)
        print("extract OK", flush=True)
        break
    except Exception as e:  # noqa: BLE001
        print(f"attempt {attempt} failed: {e}", flush=True)
        time.sleep(3)
else:
    raise SystemExit("all download attempts failed")

from insightface.app import FaceAnalysis  # noqa: E402

app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
app.prepare(ctx_id=-1, det_size=(640, 640))
print("MODEL READY", flush=True)
