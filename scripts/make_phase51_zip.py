"""PHASE 5.1 final deliverable ZIP — Trading-Bot-V1-PHASE5.1-AI-TRADING-FINAL.zip.

Same safety pattern as the Phase 5 ZIP:
  - excludes secrets/databases/logs/caches/build artifacts
  - CRC integrity check of every member (testzip)
  - secret needle scan over all text members (fail if real credentials found)
  - prints size, SHA256, file count
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "Trading-Bot-V1-PHASE5.1-AI-TRADING-FINAL.zip"

EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", "dist", "venv", ".venv",
    "env", ".pytest_cache", ".mypy_cache", ".ruff_cache", "data",
    ".freebuff", "build", ".vercel", "coverage", ".next",
}
EXCLUDE_FILE_EXACT = {".env", "th.db", "trading_bot.db", "backtest_jobs.db"}
# Prior phase deliverables are NOT part of the 5.1 source payload — and the
# in-progress output zip must never be collected into itself.
EXCLUDE_FILE_PREFIXES = (
    "Trading-Bot-V1-", "trading-bot-copilot-",
)
EXCLUDE_SUFFIXES = (
    ".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".lock",
    ".token", ".key", ".pem",
)
# Files that may contain token-like names in examples/tests must not contain
# REAL credentials. Scanned with VALUE-shaped regexes (not naive substrings)
# so documentation placeholders like `UPSTOX_ACCESS_TOKEN=` (empty) or
# `<your_token>` pass, while an actual credential fails the build.
import re

SECRET_PATTERNS = (
    # A real Upstox access token: long dot/base64-ish value after the = sign
    (re.compile(r"UPSTOX_ACCESS_TOKEN=[A-Za-z0-9._\-]{20,}"), "upstox token value"),
    (re.compile(r"LTpk[A-Za-z0-9._\-]{20,}"), "upstox LTpk token"),
    # OpenAI-style keys (20+ chars after sk- so prose like 'risk-' never matches)
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai-style key"),
    # A real private key: header followed by a base64 body
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\n\r][A-Za-z0-9+/=\n\r]{100,}"), "private key"),
    (re.compile(r"SMTP_PASSWORD=(?!\s*$)\S{8,}"), "smtp password value"),
    (re.compile(r"SECRET_KEY=(?!\s*$)(?!\{)\S{16,}"), "secret key value"),
)


def excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    for p in parts:
        if p in EXCLUDE_DIRS:
            return True
    if path.name in EXCLUDE_FILE_EXACT:
        return True
    if path.name.startswith(EXCLUDE_FILE_PREFIXES) and path.suffix == ".zip":
        return True
    if path == OUT:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def main() -> None:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if not excluded(p):
                files.append(p)
    files.sort()

    src_bytes = sum(p.stat().st_size for p in files)
    print(f"collecting {len(files)} files, {src_bytes / 1024 / 1024:.1f} MB source")
    if src_bytes > 200 * 1024 * 1024:
        raise SystemExit("source set unexpectedly large — refusing to zip")

    if OUT.exists():
        OUT.unlink()
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            arc = p.relative_to(ROOT).as_posix()
            zf.write(p, arc)

    # ── integrity: CRC check of every member ─────────────────────────
    with zipfile.ZipFile(OUT) as zf:
        bad = zf.testzip()
        assert bad is None, f"CRC check failed for member: {bad}"
        names = zf.namelist()

        # ── secret value scan over text members ────────────────────
        hits: list[str] = []
        for name in names:
            if name == "scripts/make_phase51_zip.py":
                continue  # the scanner itself: contains only regex definitions
            if name.endswith((".env",)):
                hits.append(name)  # .env must never ship
                continue
            if name.endswith((".py", ".ts", ".tsx", ".md", ".json", ".txt",
                              ".yml", ".yaml", ".toml", ".cfg", ".ini", ".example", ".html")):
                try:
                    text = zf.read(name).decode("utf-8", errors="ignore")
                except Exception:
                    continue
                for pattern, label in SECRET_PATTERNS:
                    if pattern.search(text):
                        hits.append(f"{name}: {label}")
        assert not hits, f"SECRET SCAN FAILURES: {hits}"

    size = OUT.stat().st_size
    sha = hashlib.sha256(OUT.read_bytes()).hexdigest()
    print("ZIP:", OUT.name)
    print("files:", len(names))
    print("size_bytes:", size, f"({size / 1024 / 1024:.2f} MB)")
    print("sha256:", sha)
    print("crc_check: PASS (all members)")
    print("secret_scan: PASS (no real credentials; .env excluded)")
    print("created_utc:", datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
