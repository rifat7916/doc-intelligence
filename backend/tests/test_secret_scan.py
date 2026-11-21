"""The repository ships a secret scanner (scripts/check_secrets.py): make sure it works and the repo itself is clean."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts" / "check_secrets.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

FAKE_GOOGLE = "AIza" + "Sy" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q"[:33]  # built at run time: never a literal key in the repo


def test_detects_real_looking_keys_and_masks_them():
    text = f"client = Client(api_key='{FAKE_GOOGLE}')"
    found = cs.scan_text(text)
    assert found and found[0][1] == "Google API key"
    assert FAKE_GOOGLE not in repr(found)  # the secret itself is never shown


@pytest.mark.parametrize("line", [  # split into pieces so this test file does not look like a leak itself
    "GEMINI_API_" + "KEY=abc123def456ghi789jkl012mno",
    "pass" + 'word: "Zx9fK2mQ8wLp4Rt7"',
    "export API_TO" + "KEN=9f8e7d6c5b4a39281706f5e4d3c2b1a0",
])
def test_detects_credential_assignments(line):
    assert cs.scan_text(line)


@pytest.mark.parametrize("line", [
    "GEMINI_API_KEY=", "API_KEY=change-me-to-a-long-random-string", "API_KEY: ${API_KEY:-}",
    "GEMINI_API_KEY=<paste your key here>", "gemini_api_key: str = \"\"", "x_api_key: str | None = Header(default=None)",
    "proxy_set_header X-API-Key \"${API_KEY}\";", "password = your-password-here",
    "DATABASE_URL: postgresql://user:${POSTGRES_PASSWORD:-docintel}@db:5432/docintel",  # variable with a default
    "**Failures:** test_secret_scan::test_this_repository_contains_no_secrets",  # a pytest id, not a secret
    "SECRET_KEY_NAME = some_long_snake_case_identifier_without_digits",
])
def test_ignores_placeholders_and_code(line):
    assert cs.scan_text(line) == []


def test_private_key_block_and_github_token():
    assert cs.scan_text("-----BEGIN RSA " + "PRIVATE KEY-----")[0][1] == "Private key block"
    assert cs.scan_text("token ghp_" + "a" * 36)[0][1] == "GitHub token"


def test_binary_and_lock_files_are_skipped(tmp_path):
    (tmp_path / "x.png").write_bytes(b"\x89PNG" + b"AIza" + b"a" * 35)
    assert cs.read("x.png", False) is None and cs.read("package-lock.json", False) is None


def test_this_repository_contains_no_secrets():
    files = cs.collect_files("tree")
    assert len(files) > 50
    problems = []
    for path in files:
        text = cs.read(path, False)
        if text is not None:
            problems += [(path, no, label) for no, label, _ in cs.scan_text(text)]
    assert problems == []
    assert not (ROOT / ".env").exists() or ".env" not in [Path(f).name for f in files]
