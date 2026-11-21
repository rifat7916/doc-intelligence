#!/usr/bin/env python3
"""Check in about 10 seconds that YOUR Gemini key, model and embedding model work with this project.

  python scripts/check_gemini.py        (after: pip install -r backend/requirements.txt, key in .env)
  docker compose run --rm -v ./scripts:/srv/scripts worker python /srv/scripts/check_gemini.py

It runs the same code the application uses and prints what to change when something is wrong.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" if (ROOT / "backend").is_dir() else ROOT

SAMPLE = """Northwind Traders Ltd
12 Harbor Road, Chattogram 4100
Invoice No: CHK-2026-001
Date: 14/03/2026
Bill To: Care Guide Services
Description   Qty   Unit Price   Amount
Wheelchair rental (monthly)   2   85.00   170.00
Nitrile gloves, box of 100   4   12.50   50.00
Subtotal   220.00
VAT (10%)   22.00
Total Due   242.00
Amounts in USD"""
EXPECTED = {"invoice_number": "CHK-2026-001", "supplier_name": "Northwind Traders Ltd", "total": 242.0, "currency": "USD"}


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                os.environ.setdefault(m[1], m[2].strip("'\""))


def hint_for(reason: str) -> str:
    r = reason.lower()
    if "api key" in r or "403" in r or "401" in r or "permission" in r:
        return "The key was rejected. Create a new key at https://aistudio.google.com/apikey and put it in .env (GEMINI_API_KEY=...)."
    if "429" in r or "quota" in r or "limit" in r or "exhaust" in r:
        return ("Quota or rate limit. Wait a minute and run again. If it says the limit is 0, your key may not include this model: "
                "set GEMINI_MODEL in .env to a model listed above.")
    if "404" in r or "not found" in r or "not supported" in r:
        return "The model name is not available for your key. Set GEMINI_MODEL in .env to one of the models listed above."
    if "timed out" in r or "connect" in r or "network" in r or "resolve" in r:
        return "Network problem: check your internet connection, proxy or firewall."
    return "See the message above. If it is unclear, run the check again and look at the model list."


def main() -> int:
    load_env(ROOT / ".env")
    sys.path.insert(0, str(BACKEND))
    from app.config import get_settings
    from app.embeddings import GeminiEmbedder
    from app.extraction import llm
    from app.extraction.segment import Segment
    from app.extraction.service import extract_segment
    from app.processing.types import PageText

    settings = get_settings()
    problems: list[str] = []
    print("Gemini check\n")

    print("[1/4] API key")
    if not settings.gemini_api_key:
        print("  FAIL  GEMINI_API_KEY is empty.\n        Get a free key at https://aistudio.google.com/apikey and put it in .env")
        return 1
    key = settings.gemini_api_key
    print(f"  OK    key found ({key[:4]}...{key[-2:]}, {len(key)} characters)")

    print("\n[2/4] Models your key can use")
    gen_models: list[str] = []
    embed_models: list[str] = []
    try:
        for m in llm.get_client(settings).models.list():
            name = str(getattr(m, "name", "")).replace("models/", "")
            actions = list(getattr(m, "supported_actions", None) or [])
            if "generateContent" in actions or not actions:
                gen_models.append(name)
            if "embedContent" in actions:
                embed_models.append(name)
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN  could not list models ({type(exc).__name__}: {str(exc)[:160]})")
        print(f"        {hint_for(str(exc))}")
    if gen_models:
        usable = [m for m in settings.gemini_models if m in gen_models]
        for m in settings.gemini_models:
            print(f"  {'OK  ' if m in usable else 'MISS'}  {m}" + ("" if m in usable else "   (not available for this key)"))
        if not usable:
            suggestions = sorted([m for m in gen_models if "flash" in m and "image" not in m and "tts" not in m], reverse=True)[:8]
            problems.append("none of the configured models is available")
            print("        Set GEMINI_MODEL in .env to one of: " + (", ".join(suggestions) or "(no flash model listed)"))

    print("\n[3/4] Extraction on a sample invoice")
    settings.extractor = "llm"
    t0 = time.time()
    inv = extract_segment(Segment([PageText(1, SAMPLE, "digital")]), settings)
    secs = time.time() - t0
    if inv.source != "gemini":
        reason = next((n for n in inv.notes if n.startswith("LLM_FALLBACK")), "unknown error")
        problems.append("extraction through Gemini failed")
        print(f"  FAIL  Gemini was not used ({reason[:220]})\n        {hint_for(reason)}")
    else:
        got = {"invoice_number": inv.invoice_number, "supplier_name": inv.supplier_name,
               "total": float(inv.total) if inv.total is not None else None, "currency": inv.currency}
        print(f"  OK    answered in {secs:.1f} s using model: {inv.model}")
        for k, v in EXPECTED.items():
            ok = got[k] == v
            print(f"        {'ok ' if ok else 'DIFF'}  {k}: expected {v!r}, got {got[k]!r}")
            if not ok:
                problems.append(f"unexpected value for {k}")
        print(f"        line items found: {len(inv.line_items)} (expected 2)")

    print("\n[4/4] Embeddings for semantic search")
    try:
        emb = GeminiEmbedder(settings)
        vec = emb.embed_query("wheelchair rental")
        ok = len(vec) == settings.embed_dim
        print(f"  {'OK  ' if ok else 'FAIL'}  model {settings.gemini_embed_model}: vector of {len(vec)} numbers (expected {settings.embed_dim})")
        if not ok:
            problems.append("embedding size does not match EMBED_DIM")
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN  embeddings failed ({type(exc).__name__}: {str(exc)[:160]})")
        print("        Search still works with the offline fallback. To use another model set GEMINI_EMBED_MODEL "
              + (f"(your key lists: {', '.join(embed_models[:5])})" if embed_models else "in .env") + ", or set EMBEDDER=hash.")

    print()
    if problems:
        print("NOT READY: " + "; ".join(problems))
        return 1
    print("READY: Gemini works. The app will now use it for extraction and search.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
