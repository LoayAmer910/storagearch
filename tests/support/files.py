"""Helpers shared by several test files."""
import ctypes
import os
import random
from contextlib import contextmanager
from ctypes import wintypes

from support import corpus


def write_lines_file(folder, name, lines=3000, seed=0):
    path = os.path.join(folder, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(corpus.lines(lines, f"line {{i}} {seed} {{w}} {{n}}\n", seed=seed))
    return path


def sample_text(size, seed=1):
    data = corpus.lines(size // 20 + 10, f"row {{i}} {seed} {{w}} {{n}}\n", seed=seed)
    return data[:size]


def files_under(root):
    return [os.path.join(d, f) for d, _s, fs in os.walk(root) for f in fs]


def code_folder(root, count=60, seed=0):
    rnd = random.Random(seed)
    for i in range(count):
        path = os.path.join(root, f"pkg{i % 4}", f"mod{i}.py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(sample_text(rnd.randint(5_000, 120_000), seed=seed * 1000 + i))
    return root


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


@contextmanager
def locked(path):
    """Opens the file the way another program holding it exclusively would (no sharing at all)."""
    handle = _kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, share 0
    assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    try:
        yield
    finally:
        _kernel32.CloseHandle(handle)
