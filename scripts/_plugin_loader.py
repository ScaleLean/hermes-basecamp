"""Load the plugin package from a source checkout."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_plugin() -> ModuleType:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "basecamp_plugin",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Basecamp plugin from {root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
