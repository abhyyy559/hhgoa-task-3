"""Inspect .env / .env.example WITHOUT printing any secret values."""
import sys
from pathlib import Path

for name in (".env", ".env.example"):
    p = Path(__file__).resolve().parent.parent / name
    print(f"{name}: {'exists' if p.exists() else 'MISSING'}")
    if not p.exists():
        continue
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        key, _, val = ln.partition("=")
        state = f"SET ({len(val.strip())} chars)" if val.strip() else "empty"
        print(f"  {key.strip():<24} {state}")
