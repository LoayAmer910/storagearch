"""Helpers shared by several test files."""
import os


GENERIC = ("deflate", "zstd", "zstd_ldm", "zlib", "lzma", "lzma_x86", "brotli")


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def round_trip(strategy, src, base):
    """compress in one work dir, restore in another; returns (stored bytes, restored bytes)."""
    cw, rw = os.path.join(base, "c"), os.path.join(base, "r")
    os.makedirs(cw)
    os.makedirs(rw)
    stored = strategy.compress(src, cw)
    assert os.path.dirname(os.path.abspath(stored)) == os.path.abspath(cw), f"{strategy.name} wrote outside its work dir"
    restored = strategy.restore(stored, rw)
    assert os.path.dirname(os.path.abspath(restored)) == os.path.abspath(rw), f"{strategy.name} restored outside its work dir"
    return read_file(stored), read_file(restored)
