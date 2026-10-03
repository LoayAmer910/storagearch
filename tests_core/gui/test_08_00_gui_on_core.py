"""The GUI runs on the core: every name it reads from `main` (core.api), `archive_manager` (core.api) and
`storage_config` (core.config) exists there, and it imports nothing but the core, Qt and the standard library."""
import ast
import os
import re

from core import api, config

GUI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "gui")
BOUND = {"main": api, "archive_manager": api, "storage_config": config}
ALLOWED_ROOTS = {"core", "gui", "PySide6", "os", "sys", "json", "threading", "time", "traceback", "functools", "queue",
                 "logging", "datetime", "pathlib", "shiboken6", "collections", "typing", "dataclasses", "multiprocessing",
                 "gc"}


def _gui_sources():
    for name in os.listdir(GUI_DIR):
        if name.endswith(".py"):
            yield name, open(os.path.join(GUI_DIR, name), encoding="utf-8").read()


def test_every_name_the_gui_uses_exists_on_the_core():
    missing = []
    for name, source in _gui_sources():
        for module, attr in re.findall(r"\b(main|archive_manager|storage_config)\.([A-Za-z_]\w*)", source):
            if not hasattr(BOUND[module], attr):
                missing.append(f"{name}: {module}.{attr}")
    assert missing == [], missing


def test_the_gui_imports_only_the_core_qt_and_the_standard_library():
    unexpected = []
    for name, source in _gui_sources():
        for node in ast.walk(ast.parse(source)):
            roots = []
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            unexpected += [f"{name}: {r}" for r in roots if r not in ALLOWED_ROOTS]
    assert unexpected == [], unexpected
