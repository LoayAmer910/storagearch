"""Smoke test for the new core - runs first (file name sorts first; run with -x).

1. The package imports from this repository, not from somewhere else on sys.path.
2. Independence: no module under core/ imports the old engine or Qt, checked statically on the source
   (every import statement, including ones inside functions) and dynamically (what importing the
   whole package actually loads).
"""
import ast
import importlib
import os
import pkgutil
import sys

import core

CORE_DIR = os.path.dirname(os.path.abspath(core.__file__))
REPO_ROOT = os.path.dirname(CORE_DIR)

OLD_ENGINE_MODULES = {
    "main", "archive_manager", "archive_manager_smart", "smart_compression", "database_manager",
    "storage_config", "file_analysis", "strategy_registry", "policy", "selector", "resource_scheduler",
    "strategies", "concurrency_overlay", "gui", "app", "video_optimizer", "local_cloud",
}
FORBIDDEN_ROOTS = OLD_ENGINE_MODULES | {"PySide6", "tests"}
# Only these core modules may talk to the kept cloud client (after a local result is final).
CLOUD_ALLOWED = {"core.api", "core.events"}


def _core_modules():
    yield "core"
    for info in pkgutil.walk_packages([CORE_DIR], prefix="core."):
        yield info.name


def _imported_roots(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0], node.lineno


def test_core_is_imported_from_this_repository():
    assert CORE_DIR == os.path.join(REPO_ROOT, "core"), CORE_DIR
    assert os.path.isfile(os.path.join(REPO_ROOT, "tests_core", "test_00_smoke.py"))


def test_no_core_source_imports_the_old_engine_or_qt():
    offenders = []
    for folder, _dirs, files in os.walk(CORE_DIR):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            module = "core." + os.path.relpath(path, CORE_DIR)[:-3].replace(os.sep, ".").removesuffix(".__init__")
            for root, line in _imported_roots(path):
                if root == "local_cloud" and module in CLOUD_ALLOWED:
                    continue
                if root in FORBIDDEN_ROOTS:
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{line} imports {root}")
    assert offenders == [], offenders


def test_the_import_check_sees_every_import_form(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import os\n"
        "import smart_compression\n"
        "from archive_manager import checkout_item\n"
        "def later():\n"
        "    import concurrency_overlay.integration_layer\n"
        "    from PySide6 import QtCore\n"
        "from . import sibling\n",
        encoding="utf-8",
    )
    roots = {root for root, _line in _imported_roots(str(planted))}
    assert {"smart_compression", "archive_manager", "concurrency_overlay", "PySide6"} <= roots
    assert "sibling" not in roots and "" not in roots


def test_every_third_party_import_is_declared_in_requirements():
    """A package that is only installed on the developer's machine (cryptography once was) silently disables
    the cloud client on a clean install - the app catches the import error on purpose."""
    import importlib.metadata as metadata
    import re

    with open(os.path.join(REPO_ROOT, "requirements.txt"), encoding="utf-8") as f:
        declared = {re.split(r"[<>=!~;\[ ]", line.strip())[0].lower() for line in f
                    if line.strip() and not line.startswith("#")}
    distributions = metadata.packages_distributions()
    local = {"core", "gui", "local_cloud", "app"}
    missing = []
    for folder in ("core", "gui", "local_cloud"):
        for root_dir, _dirs, files in os.walk(os.path.join(REPO_ROOT, folder)):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root_dir, name)
                for root, line in _imported_roots(path):
                    if root in sys.stdlib_module_names or root in local:
                        continue
                    dists = {d.lower() for d in distributions.get(root, [root])}
                    if not dists & declared:
                        missing.append(f"{os.path.relpath(path, REPO_ROOT)}:{line} imports {root}")
    assert missing == [], missing


def test_the_cloud_client_imports():
    from core import events
    assert events.connect(), "local_cloud must be importable (is a dependency missing?)"


def test_importing_every_core_module_loads_nothing_from_the_old_engine():
    before = set(sys.modules)
    for name in _core_modules():
        importlib.import_module(name)
    loaded = {m.split(".")[0] for m in set(sys.modules) - before}
    assert loaded & (FORBIDDEN_ROOTS - {"tests"}) == set(), loaded & FORBIDDEN_ROOTS
    for module in list(sys.modules.values()):
        origin = getattr(module, "__file__", None) or ""
        assert "beta9_src" not in origin, f"{module.__name__} was loaded from the old engine: {origin}"
