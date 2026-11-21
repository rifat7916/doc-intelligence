#!/usr/bin/env python3
"""Stop secrets (API keys, tokens, private keys) from reaching a public repository.

  python scripts/check_secrets.py            # every file git would commit (tracked + new, minus .gitignore)
  python scripts/check_secrets.py --staged   # only what is staged right now (used by the pre-commit hook)
  python scripts/check_secrets.py --history  # the whole git history (run once before the first push)

Exit code 1 = something looks like a secret. Findings are masked: the secret itself is never printed.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STRICT = {  # formats that are secrets by themselves
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "OpenAI/Anthropic-style key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
# NAME = value, where NAME looks like a credential and value looks like a real secret
ASSIGN = re.compile(r"""(?ix)\b([A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*)\s*[:=]\s*["']?([^\s"'#,;)]{12,})""")
PLACEHOLDER = re.compile(r"(?i)change[-_ ]?me|your[-_ ]|<.*>|example|placeholder|x{4,}|\*{3,}|\$\{|\$\(|todo|dummy|not[-_ ]?set")
SKIP_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf", ".xlsx", ".xls", ".zip", ".woff", ".woff2", ".ico", ".gz", ".db"}
SKIP_NAMES = {"package-lock.json", "check_secrets.py"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "data", ".venv", ".pytest_cache"}
MAX_BYTES = 2_000_000


def mask(s: str) -> str:
    return f"{s[:4]}...{s[-2:]} ({len(s)} chars)" if len(s) > 8 else "***"


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return (line number, what, masked excerpt) for every suspicious line."""
    out = []
    for no, line in enumerate(text.splitlines(), start=1):
        for label, rx in STRICT.items():
            m = rx.search(line)
            if m:
                out.append((no, label, mask(m.group(0))))
        for m in ASSIGN.finditer(line):
            name, value = m.group(1), m.group(2)
            if PLACEHOLDER.search(value) or PLACEHOLDER.search(name):
                continue
            if line[max(0, m.start() - 2):m.start()] == "${" or value[0] in ":${":  # ${NAME:-default}, test ids, templates
                continue
            has_letter, has_digit = re.search(r"[A-Za-z]", value), re.search(r"\d", value)
            mixed_case = re.search(r"[a-z]", value) and re.search(r"[A-Z]", value)
            if (has_letter and has_digit) or (len(value) >= 32 and mixed_case):
                out.append((no, f"credential assigned to {name}", mask(value)))
    return out


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, errors="replace")


def in_git() -> bool:
    return (ROOT / ".git").exists() and _git("rev-parse", "--git-dir").returncode == 0


def collect_files(mode: str = "tree") -> list[str]:
    """Relative paths to scan. `tree` uses git when available (respects .gitignore), otherwise walks the folder."""
    if mode == "staged":
        res = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
        return [p for p in res.stdout.splitlines() if p]
    if in_git():
        res = _git("ls-files", "--cached", "--others", "--exclude-standard")
        return [p for p in res.stdout.splitlines() if p]
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f == ".env" or f.startswith(".env.") and f != ".env.example":  # git would ignore these
                continue
            files.append(str(Path(dirpath, f).relative_to(ROOT)))
    return files


def read(path: str, staged: bool) -> str | None:
    p = Path(path)
    if p.suffix.lower() in SKIP_EXT or p.name in SKIP_NAMES:
        return None
    try:
        if staged:
            res = subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True)
            raw = res.stdout
        else:
            if (ROOT / path).stat().st_size > MAX_BYTES:
                return None
            raw = (ROOT / path).read_bytes()
    except OSError:
        return None
    return None if b"\x00" in raw[:4096] else raw.decode("utf-8", errors="replace")


def scan_history() -> list[str]:
    res = _git("log", "--all", "-p", "--no-color", "--unified=0")
    findings, commit = [], "?"
    for line in res.stdout.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:8]
        elif line.startswith("+") and not line.startswith("+++"):
            for _, label, shown in scan_text(line[1:]):
                findings.append(f"commit {commit}: {label}: {shown}")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--history", action="store_true")
    args = ap.parse_args()
    problems: list[str] = []

    if args.history:
        if not in_git():
            print("Not a git repository yet: nothing to scan. Run `git init` and commit first.")
            return 0
        problems = scan_history()
        where = "git history"
    else:
        files = collect_files("staged" if args.staged else "tree")
        if in_git():
            tracked = _git("ls-files", "--cached").stdout.splitlines()
            if ".env" in tracked or (args.staged and ".env" in files):
                problems.append(".env: the .env file is tracked by git. Run `git rm --cached .env`")
        for path in files:
            text = read(path, args.staged)
            if text is None:
                continue
            problems += [f"{path}:{no}: {label}: {shown}" for no, label, shown in scan_text(text)]
        where = f"{len(files)} file(s)"

    if problems:
        print(f"STOP: possible secrets found while scanning {where}:\n")
        print("\n".join("  " + p for p in problems))
        print("\nRemove them (use .env, which is git-ignored). If a real key was ever committed or pushed, REVOKE it and create a new one:\n"
              "  https://aistudio.google.com/apikey  ->  delete the key, create another.")
        return 1
    print(f"OK: no secrets found in {where}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
