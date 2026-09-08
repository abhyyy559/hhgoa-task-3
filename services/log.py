"""Single log format for the whole pipeline: ``hhgoa | <stage> | <message>``.

One-line, timestamp-free (uvicorn already timestamps), no secrets — callers
must never pass keys, tokens, embeddings, or image bytes here.
"""
from __future__ import annotations


def log(stage: str, message: str) -> None:
    print(f"hhgoa | {stage:<7s} | {message}", flush=True)
