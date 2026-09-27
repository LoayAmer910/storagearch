"""Shared helpers for the GUI tests on the new core."""


def wait_idle(qtbot, win, timeout=15000):
    qtbot.waitUntil(lambda: not win.is_busy(), timeout=timeout)


def workspace_of(name, original_path_suffix=None):
    """The workspace copy of a checked-out item, read from the catalog. The new core gives every item its
    own workspace folder (the old flat layout let two items with the same name overwrite each other)."""
    from core import api as main
    for row in main.search(name):
        if row[1] == name and (original_path_suffix is None or row[4].replace("\\", "/").endswith(original_path_suffix)):
            return main.get_item(row[0])[11]
    raise AssertionError(f"no checked-out item {name!r}")
