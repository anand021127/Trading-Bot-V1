"""Regression tests for the CI repo-hygiene / secret scan.

Bug fixed: the tracked-file scan matched ``\\.env\\.`` and therefore rejected
the safe template ``.env.example``; the packaging check flagged any name
containing ``.env``; and the credential-literal scan used ``grep -L``
(files WITHOUT a match). The scan itself must stay strict.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("repo_hygiene_check", ROOT / "scripts" / "repo_hygiene_check.py")
hyg = importlib.util.module_from_spec(_spec)
sys.modules["repo_hygiene_check"] = hyg
_spec.loader.exec_module(hyg)


@pytest.mark.parametrize("path", [
    ".env.example", "frontend/.env.example", "deploy/.env.example",
    "id_rsa.pub", "package.json", "frontend/package-lock.json", "tsconfig.json",
    "backend/config/settings.py", "README.md", "backend/tests/test_x.py",
])
def test_safe_files_are_allowed(path):
    assert hyg.classify(path) is None, path


@pytest.mark.parametrize("path", [
    ".env", "backend/.env", ".env.local", ".env.production", ".env.development",
    ".env.staging", ".env.test", ".env.prod.local", "prod.env", ".ENV", ".Env.Local",
    "upstox_token.json", "data/upstox_token.json", "access_token.json", "credentials.json",
    "client_secret.json", "service_account.json",
    "data/trading_bot.db", "x.sqlite", "x.sqlite3", "trading_bot.db-wal",
    "logs/api.log", "a/b/errors.log", "server.pem", "priv.key", "cert.p12", "id_rsa", "id_ed25519",
])
def test_secret_like_files_are_rejected(path):
    assert hyg.classify(path), f"{path} must be rejected"


def test_only_exact_env_example_is_allowed_not_all_env_star():
    assert hyg.ALLOWED_TEMPLATES == frozenset({".env.example"})
    # look-alikes of the allowed name must NOT slip through
    for sneaky in (".env.example.local", ".env.example.bak", ".env.examples", "x.env.example.env"):
        assert hyg.classify(sneaky), sneaky


def test_archive_mode_rejects_forbidden_directories():
    for p in ("node_modules/x/index.js", "frontend/dist/index.html", ".git/config",
              "logs/x.txt", "backend/__pycache__/a.txt"):
        assert hyg.classify(p, archive=True), p
    assert hyg.classify("backend/api/main.py", archive=True) is None
    assert hyg.classify(".env.example", archive=True) is None


def test_content_scan_detects_credentials_and_jwt_but_not_placeholders():
    real = 'UPSTOX_ACCESS_TOKEN = "' + "A" * 32 + '"'
    assert hyg.scan_text("x.py", real)
    assert hyg.scan_text("x.py", 'OPENAI_API_KEY="sk-' + "b" * 30 + '"')
    jwt = "eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c" * 20
    assert hyg.scan_text("x.txt", jwt)
    assert hyg.scan_text("k.txt", "-----BEGIN RSA PRIVATE KEY-----\nabc")
    # placeholders / env lookups / short values are fine
    assert not hyg.scan_text(".env.example", "UPSTOX_ACCESS_TOKEN=\nOPENAI_API_KEY=\n")
    assert not hyg.scan_text("x.py", 'os.environ.get("UPSTOX_ACCESS_TOKEN", "")')
    assert not hyg.scan_text("x.py", 'UPSTOX_ACCESS_TOKEN = "your_token_here"')


def test_repo_env_example_itself_passes_and_is_placeholder_only():
    f = ROOT / ".env.example"
    assert f.exists()
    assert hyg.classify(".env.example") is None
    assert not hyg.scan_text(".env.example", f.read_text(encoding="utf-8"))


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def test_cli_tracked_mode_end_to_end_in_a_scratch_repo():
    with tempfile.TemporaryDirectory() as d:
        _git("init", "-q", cwd=d)
        _git("config", "user.email", "t@t", cwd=d)
        _git("config", "user.name", "t", cwd=d)
        (Path(d) / ".env.example").write_text("UPSTOX_ACCESS_TOKEN=\n")
        (Path(d) / "app.py").write_text("print('hi')\n")
        _git("add", "-A", cwd=d)
        ok = subprocess.run([sys.executable, str(ROOT / "scripts/repo_hygiene_check.py"),
                             "--repo", d, "--tracked"], capture_output=True, text=True)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        for bad in (".env", ".env.local", ".env.production", "data.db", "run.log", "k.pem"):
            (Path(d) / bad).write_text("x\n")
            _git("add", "-f", bad, cwd=d)
            r = subprocess.run([sys.executable, str(ROOT / "scripts/repo_hygiene_check.py"),
                                "--repo", d, "--tracked"], capture_output=True, text=True)
            assert r.returncode == 1 and bad in r.stdout, (bad, r.stdout)
            _git("rm", "-q", "-f", "--cached", bad, cwd=d)
            (Path(d) / bad).unlink()


def test_cli_zip_mode_allows_env_example_and_rejects_real_secrets():
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "good.zip"
        with zipfile.ZipFile(good, "w") as z:
            z.writestr("P/.env.example", "UPSTOX_ACCESS_TOKEN=\n")
            z.writestr("P/backend/app.py", "x=1\n")
        r = subprocess.run([sys.executable, str(ROOT / "scripts/repo_hygiene_check.py"),
                            "--zip", str(good)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        bad = Path(d) / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("P/.env", "UPSTOX_ACCESS_TOKEN=abc\n")
            z.writestr("P/.env.production", "x\n")
            z.writestr("P/logs/api.log", "x\n")
        r = subprocess.run([sys.executable, str(ROOT / "scripts/repo_hygiene_check.py"),
                            "--zip", str(bad)], capture_output=True, text=True)
        assert r.returncode == 1
        assert "P/.env:" in r.stdout and "P/.env.production" in r.stdout and "api.log" in r.stdout


def test_ci_workflow_uses_the_script_and_cannot_regress_to_broken_patterns():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts/repo_hygiene_check.py --tracked" in ci
    assert "scripts/repo_hygiene_check.py --zip" in ci
    # the three original defects
    assert r"\.env\." not in ci                       # blanket .env.* regex
    assert "grep -rEIL" not in ci and " -L " not in ci  # inverted grep
    assert "grep -v '^.env.example$'" not in ci
    assert "forbidden = ('.env'" not in ci            # substring packaging rule
    # the scan must NOT be disabled / advisory
    assert "continue-on-error" not in ci
    assert "|| true" not in ci.split("repo-hygiene:")[1].split("Merge-conflict")[-1]


def test_release_scripts_use_the_shared_classifier():
    for name in ("make_final_zip.py", "audit_final_zip.py"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "repo_hygiene_check" in text and "_hygiene.classify" in text, name
