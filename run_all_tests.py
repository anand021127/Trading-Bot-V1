"""Universal test runner that executes both unittest.TestCase and bare pytest test_* functions."""
import inspect
import os
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Tuple
from unittest.mock import patch, MagicMock

# Ensure root is in path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class MonkeyPatch:
    def __init__(self):
        self._patches = []

    def setattr(self, target: Any, name: str, value: Any = None):
        if value is None and isinstance(target, str):
            # target is a dotted path
            mod_path, attr_name = target.rsplit(".", 1)
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
                    delattr(tgt, name)
                else:
                    setattr(tgt, name, orig)
        self._patches.clear()


def resolve_fixtures(fn: callable, mod: Any) -> Dict[str, Any]:
    sig = inspect.signature(fn)
    kwargs = {}
    
    # Check autouse fixtures in module
    for attr_name, attr_val in inspect.getmembers(mod, inspect.isfunction):
        if getattr(attr_val, "_is_fixture", False) and getattr(attr_val, "_autouse", False):
            try:
                attr_val()
            except Exception:
                pass

    for param_name in sig.parameters:
        if param_name == "tmp_path":
            kwargs["tmp_path"] = tempfile.mkdtemp()
        elif param_name == "monkeypatch":
            kwargs["monkeypatch"] = MonkeyPatch()
        elif hasattr(mod, param_name) and getattr(getattr(mod, param_name), "_is_fixture", False):
            fixture_fn = getattr(mod, param_name)
            # Check if generator fixture
            if inspect.isgeneratorfunction(fixture_fn):
                gen = fixture_fn()
                val = next(gen)
                kwargs[param_name] = val
            else:
                kwargs[param_name] = fixture_fn()
        elif hasattr(mod, param_name) and callable(getattr(mod, param_name)):
            kwargs[param_name] = getattr(mod, param_name)()
        else:
            # dummy mock
            kwargs[param_name] = MagicMock()

    return kwargs


def discover_and_run_all() -> bool:
    test_dir = os.path.join(ROOT_DIR, "backend", "tests")
    test_files = sorted([f[:-3] for f in os.listdir(test_dir) if f.startswith("test_") and f.endswith(".py")])

    suite = unittest.TestSuite()
    bare_function_tests: List[Tuple[str, callable, Any]] = []

    for mod_name in test_files:
        full_mod_name = f"backend.tests.{mod_name}"
        try:
            mod = __import__(full_mod_name, fromlist=["*"])
        except Exception as e:
            print(f"FAILED TO IMPORT {full_mod_name}: {e}")
            continue

        # Find TestCase classes
        for attr_name, attr_val in inspect.getmembers(mod, inspect.isclass):
            if issubclass(attr_val, unittest.TestCase) and attr_val.__module__ == full_mod_name:
                suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(attr_val))

        # Find bare test_* functions
        for attr_name, attr_val in inspect.getmembers(mod, inspect.isfunction):
            if attr_name.startswith("test_") and attr_val.__module__ == full_mod_name:
                bare_function_tests.append((f"{mod_name}.{attr_name}", attr_val, mod))

    print(f"Found {suite.countTestCases()} TestCase tests and {len(bare_function_tests)} standalone test functions.")

    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)

    bare_passed = 0
    bare_failed = 0
    for name, fn, mod in bare_function_tests:
        mp = None
        try:
            kwargs = resolve_fixtures(fn, mod)
            for k, v in kwargs.items():
                if isinstance(v, MonkeyPatch):
                    mp = v
            fn(**kwargs)
            bare_passed += 1
        except Exception as e:
            bare_failed += 1
            print(f"FAIL bare test {name}: {e}")
        finally:
            if mp:
                mp.undo()

    total_run = result.testsRun + len(bare_function_tests)
    total_failures = len(result.failures) + len(result.errors) + bare_failed

    print(f"\n==================================================")
    print(f"TOTAL TESTS: {total_run} | PASSED: {total_run - total_failures} | FAILED: {total_failures}")
    print(f"==================================================")
    return total_failures == 0


if __name__ == "__main__":
    success = discover_and_run_all()
    sys.exit(0 if success else 1)
