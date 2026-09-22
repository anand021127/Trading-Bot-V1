"""Lightweight pytest shim providing core pytest decorator and context manager utilities."""
from __future__ import annotations

import contextlib
import math
import os
import sys
from typing import Any, Callable, Optional
from unittest.mock import patch

# Offline/paper isolation MUST be set before any backend.api.main import.
# SQLite lives on tmpfs so FUSE-backed project dirs cannot stall the suite.
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
    os.path.join(_shm, f"pytest_trading_bot_{os.getpid()}.db"),
)
_empty_opt = os.path.join(_shm, "empty_options_cache")
os.makedirs(_empty_opt, exist_ok=True)
os.environ.setdefault("HISTORICAL_OPTIONS_CACHE_DIR", _empty_opt)


class RaisesContext:
    def __init__(self, expected_exc: Any, match: Optional[str] = None):
        self.expected_exc = expected_exc
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(f"DID NOT RAISE {self.expected_exc}")
        if not issubclass(exc_type, self.expected_exc):
            return False
        self.value = exc_val
        if self.match and self.match not in str(exc_val):
            raise AssertionError(f"Pattern '{self.match}' not found in '{str(exc_val)}'")
        return True


def raises(expected_exc: Any, match: Optional[str] = None) -> RaisesContext:
    return RaisesContext(expected_exc, match=match)


def fixture(fn: Optional[Callable] = None, scope: str = "function", autouse: bool = False) -> Any:
    def decorator(func: Callable) -> Callable:
        setattr(func, "_is_fixture", True)
        setattr(func, "_autouse", autouse)
        setattr(func, "_scope", scope)
        return func
    if fn is not None:
        return decorator(fn)
    return decorator


class Mark:
    def __getattr__(self, name: str) -> Any:
        def decorator(*args: Any, **kwargs: Any) -> Callable:
            def inner(func: Callable) -> Callable:
                return func
            if len(args) == 1 and callable(args[0]):
                return args[0]
            return inner
        return decorator


mark = Mark()


class MonkeyPatch:
    def __init__(self):
        self._patches = []

    def setattr(self, target: Any, name: str, value: Any = None):
        if value is None and isinstance(target, str):
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

    def delenv(self, name: str, raising: bool = True):
        if name in os.environ:
            orig = os.environ[name]
            del os.environ[name]
            self._patches.append(("env", name, orig))

    def undo(self):
        for item in reversed(self._patches):
            if hasattr(item, "stop"):
                item.stop()
            elif isinstance(item, tuple) and len(item) == 3:
                tgt, name, orig = item
                if tgt == "env":
                    if orig is not None:
                        os.environ[name] = orig
                else:
                    if orig is None:
                        if hasattr(tgt, name):
                            delattr(tgt, name)
                    else:
                        setattr(tgt, name, orig)
        self._patches.clear()


class Approx:
    def __init__(self, expected: float, rel: float = 1e-6, abs: float = 1e-12):
        self.expected = expected
        self.rel = rel
        self.abs = abs

    def __eq__(self, actual: Any) -> bool:
        if isinstance(actual, (int, float)):
            return math.isclose(actual, self.expected, rel_tol=self.rel, abs_tol=self.abs)
        return False

    def __repr__(self) -> str:
        return f"approx({self.expected} ± {self.abs})"


def approx(expected: float, rel: float = 1e-6, abs: float = 1e-12) -> Approx:
    return Approx(expected, rel=rel, abs=abs)


def skip(reason: str = ""):
    import unittest
    raise unittest.SkipTest(reason)



def _resolve_fixture(mod, name, cache, mp_holder):
    """Resolve a pytest-style fixture, including nested deps (e.g. monkeypatch)."""
    if name in cache:
        return cache[name]
    if name == "monkeypatch":
        mp = MonkeyPatch()
        mp_holder.append(mp)
        cache[name] = mp
        return mp
    if name == "tmp_path":
        import tempfile
        cache[name] = tempfile.mkdtemp()
        return cache[name]
    if name == "temp_log_dir" and not (hasattr(mod, "temp_log_dir") and getattr(getattr(mod, "temp_log_dir", None), "_is_fixture", False)):
        import tempfile, uuid
        from pathlib import Path
        d = Path(tempfile.gettempdir()) / f"test_logs_{uuid.uuid4().hex}"
        d.mkdir(parents=True, exist_ok=True)
        import os
        os.environ["LOGS_DIR"] = str(d)
        cache[name] = d
        return d
    fn = getattr(mod, name, None)
    if fn is None or not getattr(fn, "_is_fixture", False):
        raise KeyError(name)
    import inspect
    sig = inspect.signature(fn)
    kwargs = {}
    for p in sig.parameters:
        kwargs[p] = _resolve_fixture(mod, p, cache, mp_holder)
    val = fn(**kwargs)
    # handle generator fixtures
    if hasattr(val, "__iter__") and hasattr(val, "__next__"):
        try:
            val = next(val)
        except TypeError:
            pass
    if hasattr(val, "__next__") and not isinstance(val, (str, bytes, list, dict)):
        # generator
        try:
            val = next(iter([val])) if False else next(val)
        except Exception:
            pass
    # better generator handling
    import types
    if isinstance(val, types.GeneratorType):
        val = next(val)
    cache[name] = val
    return val



def ensure_test_deps() -> None:
    """Require FastAPI to already be installed; do not install at runtime."""
    try:
        import fastapi  # noqa: F401
        from fastapi.testclient import TestClient  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "FastAPI is required for the test suite. "
            "Install backend/requirements.txt first. "
            f"Import error: {exc}"
        ) from exc



def main(args: Optional[List[str]] = None) -> int:
    ensure_test_deps()
    import inspect
    import importlib.util
    import tempfile
    import unittest
    import uuid

    # Re-assert isolation in case a prior module mutated process env.
    os.environ["TRADING_BOT_OFFLINE_TESTS"] = "1"
    os.environ["OFFLINE"] = "1"
    os.environ["TRADING_MODE"] = os.environ.get("TRADING_MODE") or "paper"
    os.environ["TRADING_STRATEGY"] = os.environ.get("TRADING_STRATEGY") or "V8_D_PULLBACK_ATM"
    if os.environ.get("ALLOW_LIVE_UPSTOX") != "1":
        os.environ["ALLOW_LIVE_UPSTOX"] = "0"
        os.environ["UPSTOX_ACCESS_TOKEN"] = ""

    # Load conftest so fixtures/env apply even with this custom runner.
    _root = os.path.dirname(os.path.abspath(__file__))
    _conftest = os.path.join(_root, "backend", "tests", "conftest.py")
    if os.path.isfile(_conftest):
        _cspec = importlib.util.spec_from_file_location("conftest", _conftest)
        if _cspec and _cspec.loader:
            _cmod = importlib.util.module_from_spec(_cspec)
            sys.modules["conftest"] = _cmod
            _cspec.loader.exec_module(_cmod)
    
    if args is None:
        args = sys.argv[1:]
        
    test_paths = []
    for arg in args:
        if not arg.startswith("-"):
            test_paths.append(arg)
            
    if not test_paths:
        test_dir = os.path.join(os.path.dirname(__file__), "backend", "tests")
        if os.path.exists(test_dir):
            for f in sorted(os.listdir(test_dir)):
                if f.startswith("test_") and f.endswith(".py"):
                    test_paths.append(os.path.join(test_dir, f))

    passed = 0
    failed = 0
    skipped = 0

    for path in test_paths:
        if not os.path.exists(path):
            continue
        # Isolate SQLite per test module on tmpfs to avoid FUSE disk I/O stalls
        os.environ["DATABASE_PATH"] = os.path.join(
            _shm, f"pytest_{uuid.uuid4().hex}.db"
        )
        mod_name = os.path.basename(path).replace(".py", "")
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"FAILED TO LOAD {path}: {e}")
            failed += 1
            continue

        # Run unittest TestCases and plain Test* classes
        for attr_name, attr_val in inspect.getmembers(mod, inspect.isclass):
            if getattr(attr_val, "__module__", "") != mod_name:
                continue
            if issubclass(attr_val, unittest.TestCase):
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(attr_val)
                res = unittest.TextTestRunner(verbosity=0).run(suite)
                passed += res.testsRun - len(res.failures) - len(res.errors)
                failed += len(res.failures) + len(res.errors)
            elif attr_name.startswith("Test"):
                try:
                    instance = attr_val()
                except Exception as e:
                    print(f"FAILED TO INSTANTIATE {mod_name}.{attr_name}: {e}")
                    failed += 1
                    continue
                # Collect autouse fixtures from module
                autouse_fns = []
                for fname, fval in inspect.getmembers(mod, inspect.isfunction):
                    if getattr(fval, "_is_fixture", False) and getattr(fval, "_autouse", False):
                        autouse_fns.append(fval)

                for meth_name, meth_val in inspect.getmembers(instance, inspect.ismethod):
                    if meth_name.startswith("test_"):
                        try:
                            # pytest-compatible and unittest-compatible lifecycle
                            if hasattr(instance, "setup_method"):
                                instance.setup_method()
                            elif hasattr(instance, "setUp"):
                                instance.setUp()
                            # Run autouse fixtures (generator or plain)
                            for af in autouse_fns:
                                try:
                                    gen = af()
                                    if hasattr(gen, "__next__"):
                                        next(gen)
                                        # store for teardown if needed - best effort
                                        if not hasattr(instance, "_autouse_gens"):
                                            instance._autouse_gens = []
                                        instance._autouse_gens.append(gen)
                                except Exception as _af_err:
                                    pass
                            # Resolve fixtures for class methods (e.g. temp_log_dir)
                            sig = inspect.signature(meth_val)
                            kwargs = {}
                            cache = {}
                            mp_holder = []
                            for pname in sig.parameters:
                                if pname == "self":
                                    continue
                                try:
                                    kwargs[pname] = _resolve_fixture(mod, pname, cache, mp_holder)
                                except KeyError:
                                    pass
                            try:
                                meth_val(**kwargs) if kwargs else meth_val()
                                passed += 1
                                print(f"  PASSED: {mod_name}.{attr_name}.{meth_name}")
                            finally:
                                for mp in mp_holder:
                                    mp.undo()
                                if hasattr(instance, "teardown_method"):
                                    instance.teardown_method()
                                elif hasattr(instance, "tearDown"):
                                    instance.tearDown()
                        except unittest.SkipTest as st:
                            skipped += 1
                            print(f"  SKIPPED: {mod_name}.{attr_name}.{meth_name} ({st})")
                        except Exception as e:
                            failed += 1
                            print(f"  FAILED: {mod_name}.{attr_name}.{meth_name} - {e}")

        # Run standalone test_* functions
        for attr_name, attr_val in inspect.getmembers(mod, inspect.isfunction):
            if attr_name.startswith("test_") and getattr(attr_val, "__module__", "") == mod_name:
                # Resolve fixtures (including autouse)
                sig = inspect.signature(attr_val)
                kwargs = {}
                cache = {}
                mp_holder = []
                # autouse first
                for fname, fval in inspect.getmembers(mod, inspect.isfunction):
                    if getattr(fval, "_is_fixture", False) and getattr(fval, "_autouse", False):
                        try:
                            _resolve_fixture(mod, fname, cache, mp_holder)
                        except Exception:
                            pass
                for pname in sig.parameters:
                    try:
                        kwargs[pname] = _resolve_fixture(mod, pname, cache, mp_holder)
                    except KeyError:
                        pass
                try:
                    attr_val(**kwargs)
                    passed += 1
                    print(f"  PASSED: {mod_name}.{attr_name}")
                except unittest.SkipTest as st:
                    skipped += 1
                    print(f"  SKIPPED: {mod_name}.{attr_name} ({st})")
                except Exception as e:
                    failed += 1
                    print(f"  FAILED: {mod_name}.{attr_name} - {e}")
                finally:
                    for mp in mp_holder:
                        mp.undo()

    print(f"\n================ SUMMARY ================")
    print(f"PASSED: {passed} | FAILED: {failed} | SKIPPED: {skipped}")
    print(f"=========================================")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
