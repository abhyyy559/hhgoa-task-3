"""Evaluation matrix (§9): same-person vs different-person pairs, real model.

Offline evidence for the 0.48 / 0.35 thresholds — no network, no chain.
Prints a markdown table + TPR/FPR summary. Same-person pairs must be
GENUINE (different photos of one person); identical-byte pairs are flagged
trivial and excluded from the rates.

Usage:
    .venv\\Scripts\\python.exe scripts\\eval_matrix.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services import vision as V  # noqa: E402
from services.verification import ACCEPT_THRESHOLD, REVIEW_THRESHOLD  # noqa: E402

# (label, path, person_id) — person_id groups same-person pairs.
# Gallery entries load dynamically from the index (filenames are content ids).
def _gallery_pairs() -> list[tuple[str, Path, str]]:
    import json as _json

    idx = _json.loads((PROJECT_ROOT / "data" / "gallery" / "gallery_index.json").read_text())
    out = []
    for m in idx.get("members", []):
        pid = "aarav" if "aarav" in m.get("full_name", "").lower() else m.get("member_id", "?")
        out.append((f"gallery-{m.get('member_id', '?')[:8]}", PROJECT_ROOT / "data" / "gallery" / "images" / m["image_filename"], pid))
    return out


PAIRS: list[tuple[str, Path, str]] = [
    *_gallery_pairs(),
    ("alice-1", PROJECT_ROOT / "test imgs" / "alice-1.jpg", "aarav"),
    ("alice-2", PROJECT_ROOT / "test imgs" / "alice-2.jpg", "aarav"),
    ("122947622", PROJECT_ROOT / "test imgs" / "122947622.webp", "u122947622"),
    ("127128498", PROJECT_ROOT / "test imgs" / "127128498.webp", "u127128498"),
    ("3cc5de47", PROJECT_ROOT / "test imgs" / "3cc5de4726db854981eb5207ae65f02c.webp", "u3cc5de47"),
    ("virat", PROJECT_ROOT / "test imgs" / "virat-kohli-wallpaper-4k.webp", "virat"),
]


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def encode(p: Path):
    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, "UNREADABLE"
    out = V.detect_and_encode(img)
    return (out.embedding, out.status.value) if out.embedding else (None, out.status.value)


def cos(a, b) -> float:
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    d = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / d) if d else 0.0


def main() -> None:
    embs: dict[str, list[float]] = {}
    for label, path, _pid in PAIRS:
        if not path.exists():
            print(f"SKIP {label}: missing {path}")
            continue
        emb, status = encode(path)
        print(f"detect {label:15s} {status}")
        if emb:
            embs[label] = emb

    labels = [l for l, _, _ in PAIRS if l in embs]
    hashes = {l: file_hash(p) for l, p, _ in PAIRS if l in embs}
    pid = {l: p for l, _, p in PAIRS}

    print("\n| pair | relation | similarity | decision |")
    print("|---|---|---|---|")
    same, same_ok, diff, diff_ok, trivial = 0, 0, 0, 0, 0
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            a, b = labels[i], labels[j]
            s = cos(embs[a], embs[b])
            if hashes[a] == hashes[b]:
                trivial += 1
                print(f"| {a} <-> {b} | identical-bytes (trivial, excluded) | {s:.4f} | - |")
                continue
            if pid[a] == pid[b]:
                same += 1
                ok = s >= ACCEPT_THRESHOLD
                same_ok += ok
                print(f"| {a} <-> {b} | SAME person | {s:.4f} | {'MATCH [ok]' if ok else 'MISS [FAIL]'} |")
            else:
                diff += 1
                ok = s < REVIEW_THRESHOLD
                diff_ok += ok
                flag = "REJECT [ok]" if ok else ("REVIEW?" if s < ACCEPT_THRESHOLD else "FALSE MATCH [FAIL]")
                print(f"| {a} <-> {b} | different | {s:.4f} | {flag} |")

    print(f"\nSame-person: {same_ok}/{same} matched (TPR {(same_ok / same if same else 0):.2f})")
    print(f"Different-person: {diff_ok}/{diff} cleanly rejected "
          f"(FPR {(1 - diff_ok / diff if diff else 0):.2f} at accept {ACCEPT_THRESHOLD})")
    print(f"Identical-byte trivial pairs excluded: {trivial}")
    if same < 5:
        print(f"HONEST LIMIT: only {same} genuine same-person pair(s) - add team/friend "
              f"photo pairs (target 5-10) before claiming threshold generality.")


if __name__ == "__main__":
    main()
