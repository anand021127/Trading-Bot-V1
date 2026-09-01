"""Lightweight pytest shim providing core pytest decorator and context manager utilities."""
from __future__ import annotations

import contextlib
import math
import os
import sys
from typing import Any, Callable, Optional
from unittest.mock import patch


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


def main(args: Optional[List[str]] = None) -> int:
    import inspect
    import importlib.util
    import tempfile
    import unittest
    
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

        # Run unittest TestCases
        for attr_name, attr_val in inspect.getmembers(mod, inspect.isclass):
            if issubclass(attr_val, unittest.TestCase) and getattr(attr_val, "__module__", "") == mod_name:
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(attr_val)
                res = unittest.TextTestRunner(verbosity=0).run(suite)
                passed += res.testsRun - len(res.failures) - len(res.errors)
                failed += len(res.failures) + len(res.errors)

        # Run standalone test_* functions
        for attr_name, attr_val in inspect.getmembers(mod, inspect.isfunction):
            if attr_name.startswith("test_") and getattr(attr_val, "__module__", "") == mod_name:
                # Resolve fixtures
                sig = inspect.signature(attr_val)
                kwargs = {}
                mp = None
                for p in sig.parameters:
                    if p == "monkeypatch":
                        mp = MonkeyPatch()
                        kwargs[p] = mp
                    elif p == "tmp_path":
                        kwargs[p] = tempfile.mkdtemp()
                    elif hasattr(mod, p) and getattr(getattr(mod, p), "_is_fixture", False):
                        kwargs[p] = getattr(mod, p)()
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
                    if mp:
                        mp.undo()

    print(f"\n================ SUMMARY ================")
    print(f"PASSED: {passed} | FAILED: {failed} | SKIPPED: {skipped}")
    print(f"=========================================")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
