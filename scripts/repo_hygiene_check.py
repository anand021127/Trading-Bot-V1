#!/usr/bin/env python3
"""Repo hygiene + secret scan — single implementation used by CI and tests.

Why a script instead of inline shell/grep in ci.yml
---------------------------------------------------
The previous inline checks were wrong in three ways:
  1. tracked-file regex ``\\.env\\.`` matched ``.env.example`` (a safe template);
  2. the packaging rule flagged any file whose name merely CONTAINS ``.env``;
  3. the credential-literal scan used ``grep -L`` (files WITHOUT a match).

Rules (deliberately narrow, never a blanket ``.env.*`` allowance):
  ALLOWED template files (exact basenames)  : ``.env.example``
  REJECTED                                  : ``.env`` and every other ``.env.*``
                                              (.env.local/.production/.development/
                                              .staging/.test ...), ``*.env``,
                                              token/credential JSON, SQLite/DB
                                              artifacts, ``*.log``, PEM/private
                                              keys (``*.pem``, ``*.key``, ``*.p12``,
                                              ``id_rsa`` ...).
To allow another template file, add its EXACT basename to ALLOWED_TEMPLATES.

Usage:
  python scripts/repo_hygiene_check.py --tracked            # git ls-files + content scan
  python scripts/repo_hygiene_check.py --zip release.zip    # deliverable archive
  python scripts/repo_hygiene_check.py --paths a b c        # ad-hoc
Exit code 0 = clean, 1 = violations (listed), 2 = usage/IO error.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import zipfile
from pathlib import PurePosixPath
from typing import Iterable, List, Optional, Tuple

# Exact basenames that are explicitly safe placeholders-only templates.
ALLOWED_TEMPLATES = frozenset({".env.example"})

_DB_EXT = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal",
           ".sqlite-wal", ".sqlite-shm")
_KEY_EXT = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
_KEY_BASENAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_TOKEN_JSON = re.compile(
    r"(?i)(^|[_\-.])(access[_\-]?)?token[^/]*\.json$|"
    r"(^|[_\-.])credentials?[^/]*\.json$|"
    r"^client[_\-]?secret[^/]*\.json$|^service[_\-]?account[^/]*\.json$|"
    r"^upstox_token.*\.json$"
)
# package-lock/tsconfig etc. can never match _TOKEN_JSON, but keep lockfiles
# explicit so a future ``*token*`` package name cannot trip it.
_NEVER_TOKEN_JSON = frozenset({"package.json", "package-lock.json", "tsconfig.json",
                               "tsconfig.node.json", "vercel.json"})

# Directories that must never ship in a DELIVERABLE archive (zip mode only —
# tracked-file mode is governed by git, and .gitignore handles these).
_ARCHIVE_FORBIDDEN_DIRS = (".git", "node_modules", "__pycache__", ".pytest_cache",
                           "logs", "dist")

CRED_LITERAL = re.compile(
    r"(UPSTOX_ACCESS_TOKEN|UPSTOX_CLIENT_SECRET|UPSTOX_API_SECRET|OPENAI_API_KEY|"
    r"COPILOT_AI_API_KEY|ANTHROPIC_API_KEY)\s*=\s*[\"'][A-Za-z0-9_\-.]{20,}[\"']"
)
JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_\-]{15,}\.eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,}")
PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")

_TEXT_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff", ".woff2",
                  ".ttf", ".eot", ".zip", ".gz", ".pdf", ".xlsx", ".pyc")
_CONTENT_SKIP_BASENAMES = frozenset({"package-lock.json", "repo_hygiene_check.py",
                                     "test_repo_hygiene.py"})


def classify(path: str, *, archive: bool = False) -> Optional[str]:
    """Return a violation reason for ``path`` or None if it is acceptable."""
    p = PurePosixPath(path.replace("\\", "/").lstrip("./") if path.startswith("./") else path.replace("\\", "/"))
    parts = [x for x in p.parts if x not in ("", ".")]
    if not parts:
        return None
    base = parts[-1]
    low = base.lower()

    if archive:
        for d in parts[:-1]:
            if d in _ARCHIVE_FORBIDDEN_DIRS:
                return f"forbidden directory in deliverable: {d}/"

    if base in ALLOWED_TEMPLATES:
        return None
    if low == ".env" or low.startswith(".env.") or low.endswith(".env"):
        return "environment file (only .env.example is allowed)"
    if low.endswith(_DB_EXT):
        return "database artifact"
    if low.endswith(".log"):
        return "log file"
    if low.endswith(_KEY_EXT):
        return "private key / certificate material"
    if any(low == k or (low.startswith(k) and not low.endswith(".pub")) for k in _KEY_BASENAMES):
        return "SSH private key"
    if low.endswith(".json") and low not in _NEVER_TOKEN_JSON and _TOKEN_JSON.search(low):
        return "token / credential JSON"
    return None


def scan_paths(paths: Iterable[str], *, archive: bool = False) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for path in paths:
        why = classify(path, archive=archive)
        if why:
            out.append((path, why))
    return out


def scan_text(path: str, text: str) -> List[Tuple[str, str]]:
    base = os.path.basename(path)
    if base in _CONTENT_SKIP_BASENAMES or base.lower().endswith(_TEXT_SKIP_EXT):
        return []
    hits: List[Tuple[str, str]] = []
    if CRED_LITERAL.search(text):
        hits.append((path, "hard-coded credential literal"))
    if JWT_SHAPE.search(text):
        hits.append((path, "JWT-shaped token"))
    if PRIVATE_KEY_BLOCK.search(text):
        hits.append((path, "private key block"))
    return hits


def _git_tracked(repo: str) -> List[str]:
    res = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True, check=True)
    return [x for x in res.stdout.decode("utf-8", "replace").split("\0") if x]


def _scan_files_content(repo: str, files: Iterable[str]) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for rel in files:
        full = os.path.join(repo, rel)
        if not os.path.isfile(full) or os.path.getsize(full) > 5_000_000:
            continue
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        hits.extend(scan_text(rel, raw.decode("utf-8", "replace")))
    return hits


def _report(title: str, violations: List[Tuple[str, str]]) -> int:
    if violations:
        print(f"{title}: FAILED")
        for path, why in violations:
            print(f"  - {path}: {why}")
        return 1
    print(f"{title}: OK")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tracked", action="store_true", help="scan `git ls-files` (names + contents)")
    g.add_argument("--zip", metavar="ZIP", help="scan a deliverable archive (names + contents)")
    g.add_argument("--paths", nargs="+", metavar="PATH", help="classify names only")
    a = ap.parse_args(argv)
    rc = 0
    try:
        if a.tracked:
            files = _git_tracked(a.repo)
            rc |= _report("tracked-file name scan", scan_paths(files))
            rc |= _report("tracked-file content scan", _scan_files_content(a.repo, files))
        elif a.zip:
            with zipfile.ZipFile(a.zip) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                rc |= _report("archive name scan", scan_paths(names, archive=True))
                content: List[Tuple[str, str]] = []
                for n in names:
                    info = zf.getinfo(n)
                    if info.file_size > 5_000_000:
                        continue
                    raw = zf.read(n)
                    if b"\0" in raw[:4096]:
                        continue
                    content.extend(scan_text(n, raw.decode("utf-8", "replace")))
                rc |= _report("archive content scan", content)
        else:
            rc |= _report("path scan", scan_paths(a.paths))
    except (subprocess.CalledProcessError, OSError, zipfile.BadZipFile) as exc:
        print(f"hygiene check could not run: {exc}", file=sys.stderr)
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
