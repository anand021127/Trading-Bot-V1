import importlib.util
import sys
from pathlib import Path
from types import ModuleType

repo_root = Path(__file__).resolve().parent.parent

if importlib.util.find_spec("backend") is None:
    backend_pkg = ModuleType("backend")
    if (repo_root / "backend").exists():
        backend_pkg.__path__ = [str(repo_root / "backend")]
    else:
        backend_pkg.__path__ = [str(repo_root)]
    sys.modules["backend"] = backend_pkg

from backend.api.main import app
