#!/usr/bin/env python3
"""Build the labelled evaluation dataset (synthetic invoices with known ground truth).

    python eval/generate_dataset.py [--out eval/dataset] [--seed 42]
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.devtools.synth import build_dataset  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "eval" / "dataset"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    records = build_dataset(args.out, args.seed)
    kinds: dict[str, int] = {}
    for r in records:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    size = sum(os.path.getsize(os.path.join(args.out, r["file"])) for r in records)
    print(f"{len(records)} files, {sum(len(r['invoices']) for r in records)} labelled invoices, "
          f"{size / 1e6:.1f} MB -> {args.out}\n{kinds}")
