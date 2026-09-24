"""Universal test runner that executes both unittest.TestCase and bare pytest test_* functions.

Correctly activates generator fixtures (including autouse) so setup/teardown
run around each bare test — critical for isolation of global state such as
te.settings.mode used by position-recovery tests.
"""
import inspect
import os
import sys
import tempfile
import traceback
import unittest
from typing import Any, Dict, List, Optional, Tuple

# Offline/paper isolation MUST be set before any backend import (same as pytest.py).
# SQLite on tmpfs avoids FUSE-backed project-dir disk I/O errors under load.
os.environ.setdefault("PYTEST_RUNNING", "1")
os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")
os.environ.setdefault("OFFLINE", "1")
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
os.environ.setdefault("UPSTOX_ORDER_PRODUCT", "I")
os.environ.setdefault("PAPER_ALLOW_TEST_SIGNAL", "0")
if os.environ.get("ALLOW_LIVE_UPSTOX") != "1":
    os.environ["ALLOW_LIVE_UPSTOX"] = "0"
    os.environ["UPSTOX_ACCESS_TOKEN"] = ""
_shm = "/dev/shm" if os.path.isdir("/dev/shm") else "/tmp"
os.environ.setdefault(
    "DATABASE_PATH",
    os.path.join(_shm, f"run_all_tests_{os.getpid()}.db"),
)
_empty_opt = os.path.join(_shm, "empty_options_cache")
os.makedirs(_empty_opt, exist_ok=True)
os.environ.setdefault("HISTORICAL_OPTIONS_CACHE_DIR", _empty_opt)

# Reset in-memory verified token so prior process state cannot leak into the suite.
try:
    from backend.broker.token_resolver import clear_verified_runtime_token as _clear_vrt
    _clear_vrt()
except Exception:
    pass

from unittest.mock import patch, MagicMock

# Ensure root is in path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class MonkeyPatch:
    def __init__(self):
        self._patches = []

    def setattr(self, target: Any, name: str = None, value: Any = None):
        if value is None and isinstance(target, str):
            # target is a dotted path; name is the replacement value
            p = patch(target, name)
            p.start()
            self._patches.append(p)
        else:
            orig = getattr(target, name, None)
            setattr(target, name, value)
            self._patches.append((target, name, orig))

    def setenv(self, name: str, value: str):
        p = patch.dict(os.environ, {name: value})
        p.start()
        self._patches.append(p)

    def undo(self):
        for item in reversed(self._patches):
            if hasattr(item, "stop"):
                item.stop()
            elif isinstance(item, tuple) and len(item) == 3:
                tgt, name, orig = item
                if orig is None:
                    try:
                        delattr(tgt, name)
                    except AttributeError:
                        pass
                else:
                    setattr(tgt, name, orig)
        self._patches.clear()


def _start_fixture(fixture_fn: callable) -> Tuple[Any, Optional[Any]]:
    """Run a fixture function. Returns (value, generator_or_None for teardown)."""
    if inspect.isgeneratorfunction(fixture_fn):
        gen = fixture_fn()
        try:
            val = next(gen)
        except StopIteration as exc:
            val = exc.value
        return val, gen
    return fixture_fn(), None


def _teardown_generator(gen: Any) -> None:
    if gen is None:
        return
    try:
        next(gen)
    except StopIteration:
        pass
    except Exception:
        pass


def resolve_fixtures(fn: callable, mod: Any) -> Tuple[Dict[str, Any], List[Any], Optional[MonkeyPatch]]:
    """Resolve fixtures for a bare test function.

    Returns (kwargs, active_generators_to_teardown, monkeypatch_or_None).
    Autouse generator fixtures are advanced past yield so setup runs, and
    generators are returned for post-test teardown.
    """
    sig = inspect.signature(fn)
    kwargs: Dict[str, Any] = {}
    active_gens: List[Any] = []
    mp: Optional[MonkeyPatch] = None

    # Autouse fixtures first — must actually run setup (next() on generators)
    for attr_name, attr_val in inspect.getmembers(mod, inspect.isfunction):
        if not (getattr(attr_val, "_is_fixture", False) and getattr(attr_val, "_autouse", False)):
            continue
        try:
            _val, gen = _start_fixture(attr_val)
            if gen is not None:
                active_gens.append(gen)
        except Exception:
            traceback.print_exc()

    for param_name in sig.parameters:
        if param_name == "tmp_path":
            kwargs["tmp_path"] = tempfile.mkdtemp()
        elif param_name == "monkeypatch":
            mp = MonkeyPatch()
            kwargs["monkeypatch"] = mp
        elif hasattr(mod, param_name) and getattr(getattr(mod, param_name), "_is_fixture", False):
            fixture_fn = getattr(mod, param_name)
            try:
                val, gen = _start_fixture(fixture_fn)
                kwargs[param_name] = val
                if gen is not None:
                    active_gens.append(gen)
            except Exception:
                traceback.print_exc()
                kwargs[param_name] = MagicMock()
        elif hasattr(mod, param_name) and callable(getattr(mod, param_name)):
            kwargs[param_name] = getattr(mod, param_name)()
        else:
            kwargs[param_name] = MagicMock()

    return kwargs, active_gens, mp


def discover_and_run_all() -> bool:
    test_dir = os.path.join(ROOT_DIR, "backend", "tests")
    test_files = sorted(
        [f[:-3] for f in os.listdir(test_dir) if f.startswith("test_") and f.endswith(".py")]
    )

    suite = unittest.TestSuite()
    bare_function_tests: List[Tuple[str, callable, Any]] = []

    for mod_name in test_files:
        full_mod_name = f"backend.tests.{mod_name}"
        try:
            mod = __import__(full_mod_name, fromlist=["*"])
        except Exception as e:
            print(f"FAILED TO IMPORT {full_mod_name}: {e}")
            continue

        for attr_name, attr_val in inspect.getmembers(mod, inspect.isclass):
            if issubclass(attr_val, unittest.TestCase) and attr_val.__module__ == full_mod_name:
                suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(attr_val))

        for attr_name, attr_val in inspect.getmembers(mod, inspect.isfunction):
            if attr_name.startswith("test_") and attr_val.__module__ == full_mod_name:
                bare_function_tests.append((f"{mod_name}.{attr_name}", attr_val, mod))

    print(
        f"Found {suite.countTestCases()} TestCase tests and "
        f"{len(bare_function_tests)} standalone test functions."
    )

    class _IsolatingResult(unittest.TextTestResult):
        """Clear verified runtime token before each test to prevent cross-module leakage."""

        def startTest(self, test):
            try:
                from backend.broker.token_resolver import clear_verified_runtime_token
                clear_verified_runtime_token()
            except Exception:
                pass
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            try:
                from backend.broker.token_resolver import clear_verified_runtime_token
                clear_verified_runtime_token()
            except Exception:
                pass

    runner = unittest.TextTestRunner(verbosity=1, resultclass=_IsolatingResult)
    result = runner.run(suite)

    bare_passed = 0
    bare_failed = 0
    for name, fn, mod in bare_function_tests:
        active_gens: List[Any] = []
        mp: Optional[MonkeyPatch] = None
        try:
            kwargs, active_gens, mp = resolve_fixtures(fn, mod)
            fn(**kwargs)
            bare_passed += 1
        except Exception as e:
            bare_failed += 1
            tb = traceback.format_exc()
            print(f"FAIL bare test {name}: {e!r}")
            print(tb)
        finally:
            # Teardown generator fixtures in reverse order (pytest-like)
            for gen in reversed(active_gens):
                _teardown_generator(gen)
            if mp is not None:
                mp.undo()

    total_run = result.testsRun + len(bare_function_tests)
    total_failures = len(result.failures) + len(result.errors) + bare_failed

    print(f"\n==================================================")
    print(
        f"TOTAL TESTS: {total_run} | PASSED: {total_run - total_failures} | "
        f"FAILED: {total_failures}"
    )
    print(f"==================================================")
    return total_failures == 0


if __name__ == "__main__":
    success = discover_and_run_all()
    sys.exit(0 if success else 1)
