"""Post-creation audit of Trading-Bot-V1-FINAL-PRODUCTION.zip.

Verifies, from the ZIP bytes themselves (not the working tree):
  1. every required production file is present
  2. email/SMTP artifacts are ABSENT (module, config keys, test card)
  3. no .env / DB / log / token / prior-ZIP / analysis-output members
  4. ZoneInfo import present in the shipped diagnostics.py
  5. V8-D strategy file is byte-identical to git HEAD (frozen)
  6. live gate + entry-window + expiry-lifecycle + copilot context shipped
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZIP_PATH = ROOT / "Trading-Bot-V1-FINAL-PRODUCTION.zip"

REQUIRED = [
    "backend/api/routers/diagnostics.py",
    "backend/api/routers/alerts.py",
    "backend/config/settings.py",
    "backend/strategy/trading_engine.py",
    "backend/broker/instrument_master.py",
    "backend/backtest/engine.py",
    "backend/paper/market_scan_loop.py",
    "backend/copilot/context.py",
    "backend/copilot/strategy_context.py",
]
FORBIDDEN_EXACT = {
    "backend/notifications/email_alerts.py",
}
FORBIDDEN_MEMBER_PATTERNS = (".env", ".db", ".sqlite", ".log", ".pem", ".token", ".key",
                             "upstox_token", "Trading-Bot-V1-", "trading-bot-copilot-")
FORBIDDEN_DIR_PREFIXES = ("logs/", "analysis/", "data/", ".freebuff/", "node_modules/", "dist/")
EMAIL_TOKENS = ("smtplib", "EmailAlerts", "SMTP_SERVER", "SMTP_PASSWORD", "EMAIL_PASSWORD",
                "SENDER_EMAIL", "RECIPIENT_EMAIL", "email_alerts")
# Members that legitimately mention email/SMTP tokens:
#  - the removal-guard test (its smtplib use is the sentinel proving absence)
#  - this audit script and the zip builder (self-referential token lists only)
EMAIL_SCAN_SKIP = {
    "backend/tests/test_email_removal.py",
    "scripts/audit_final_zip.py",
    "scripts/make_final_zip.py",
}


def main() -> int:
    failures: list[str] = []
    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = set(zf.namelist())
        inf = {i.filename: i.file_size for i in zf.infolist()}

        # 1. required members
        for req in REQUIRED:
            if req not in names:
                failures.append(f"MISSING required member: {req}")

        # 2. email artifacts absent
        for bad in FORBIDDEN_EXACT:
            if bad in names:
                failures.append(f"FORBIDDEN member present: {bad}")
        for name in names:
            if name.endswith((".py", ".ts", ".tsx", ".json", ".yaml", ".yml", ".example",
                              ".md", ".html", ".cfg", ".ini", ".toml")):
                if name in EMAIL_SCAN_SKIP:
                    continue  # self-referential scanner / removal-guard test only
                text = zf.read(name).decode("utf-8", errors="ignore")
                for tok in EMAIL_TOKENS:
                    if tok in text:
                        failures.append(f"EMAIL TOKEN '{tok}' in member: {name}")
                        break

        # 3. no secrets / logs / DBs / prior ZIPs / analysis outputs
        for name in names:
            for pat in FORBIDDEN_MEMBER_PATTERNS:
                if pat in Path(name).name and not name.endswith(".example"):
                    failures.append(f"FORBIDDEN member pattern '{pat}': {name}")
            for pre in FORBIDDEN_DIR_PREFIXES:
                if name.startswith(pre):
                    failures.append(f"FORBIDDEN directory member: {name}")

        # 4. ZoneInfo import inside shipped diagnostics.py
        diag = zf.read("backend/api/routers/diagnostics.py").decode("utf-8")
        if "from zoneinfo import ZoneInfo" not in diag:
            failures.append("shipped diagnostics.py lacks ZoneInfo import")
        if '"email"' in diag:
            failures.append("shipped diagnostics.py still has an 'email' TEST_MAP entry")

        # 5. V8-D frozen: ZIP content == git HEAD content.
        # Windows checkouts store CRLF in the working tree while git blobs are
        # LF, so compare LINE-NORMALIZED bytes (content identity, not EOLs).
        head_blob = subprocess.run(
            ["git", "show", "HEAD:backend/strategy/strategies/v8d_strategy.py"],
            cwd=ROOT, capture_output=True, check=True).stdout
        zip_blob = zf.read("backend/strategy/strategies/v8d_strategy.py")
        zip_norm = zip_blob.replace(b"\r\n", b"\n")
        if hashlib.sha256(head_blob).hexdigest() != hashlib.sha256(zip_norm).hexdigest():
            failures.append("v8d_strategy.py inside ZIP differs from git HEAD (freeze broken)")

        # 6. invariants shipped
        engine = zf.read("backend/backtest/engine.py").decode("utf-8")
        checks = [
            ("daily counter reset", "trades_opened_today = 0" in engine),
            ("entry window 09:20/14:45 parity", "09:20" in engine and "14:45" in engine),
            ("expiry lifecycle gate", "LIFECYCLE FORCED CLOSE" in engine),
            ("live gate shipped", "backend/execution/live_gate.py" in names),
            ("strategy context shipped", "backend/copilot/strategy_context.py" in names),
            ("NIFTY50→NIFTY alias", "\"NIFTY50\": \"NIFTY\"" in
             zf.read("backend/broker/instrument_master.py").decode("utf-8")),
            ("telegram alerts intact", "backend/notifications/telegram_alerts.py" in names),
            ("run_all_tests shipped", "run_all_tests.py" in names),
        ]
        for label, ok in checks:
            if not ok:
                failures.append(f"INVARIANT FAIL: {label}")

        print(f"members: {len(names)}")
        print("required members present:", sum(1 for r in REQUIRED if r in names), "/", len(REQUIRED))
        print("v8d_strategy.py content sha256 (zip==HEAD, EOL-normalized):",
              hashlib.sha256(zip_norm).hexdigest()[:16], "…")
        print("largest members:", sorted(inf.items(), key=lambda kv: -kv[1])[:3])

    if failures:
        print("\nAUDIT FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nZIP AUDIT: ALL CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
