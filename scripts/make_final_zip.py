"""FINAL PRODUCTION ZIP — Trading-Bot-V1-FINAL-PRODUCTION.zip (PHASE 5.3 §38).

Same safety pattern as the earlier phase ZIPs:
  - excludes .env / secrets / production DBs / logs / node_modules / dist /
    venvs / caches AND all prior phase deliverable ZIPs
    (Trading-Bot-V1-*, trading-bot-copilot-*)
  - includes deploy/systemd + deploy/nginx + docs + tests + migrations
  - CRC integrity check of every member (testzip)
  - secret value-pattern scan over all text members (fails on real credentials)
  - prints size, SHA256, file count
"""
from __future__ import annotations

import hashlib
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "Trading-Bot-V1-FINAL-PRODUCTION.zip"

EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", "dist", "venv", ".venv",
    "env", ".pytest_cache", ".mypy_cache", ".ruff_cache", "data",
    ".freebuff", "build", ".vercel", "coverage", ".next",
}
EXCLUDE_FILE_EXACT = {".env", "th.db", "trading_bot.db", "backtest_jobs.db"}
EXCLUDE_FILE_PREFIXES = (
    "Trading-Bot-V1-", "trading-bot-copilot-",
)
EXCLUDE_SUFFIXES = (
    ".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".lock",
    ".token", ".key", ".pem",
)
SECRET_PATTERNS = (
    (re.compile(r"UPSTOX_ACCESS_TOKEN=[A-Za-z0-9._\-]{20,}"), "upstox token value"),
    (re.compile(r"LTpk[A-Za-z0-9._\-]{20,}"), "upstox LTpk token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai-style key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\n\r][A-Za-z0-9+/=\n\r]{100,}"), "private key"),
    (re.compile(r"SMTP_PASSWORD=(?!\s*$)\S{8,}"), "smtp password value"),
    (re.compile(r"SECRET_KEY=(?!\s*$)(?!\{)\S{16,}"), "secret key value"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{30,}"), "bearer token literal"),
)
# Scanner scripts contain only these regex DEFINITIONS (self-match).
SELF_SCAN_SKIP = {
    "scripts/make_phase51_zip.py", "scripts/make_phase52_zip.py",
    "scripts/make_final_zip.py",
}


def excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    for p in rel.parts:
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
            zf.write(p, p.relative_to(ROOT).as_posix())

    with zipfile.ZipFile(OUT) as zf:
        bad = zf.testzip()
        assert bad is None, f"CRC check failed for member: {bad}"
        names = zf.namelist()

        hits: list[str] = []
        for name in names:
            if name in SELF_SCAN_SKIP:
                continue
            if name.endswith(".env"):
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
