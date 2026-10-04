"""Imports the data of the old version into the new core.

    python scripts/import_old_data.py OLD_DATA_ROOT [TARGET_DIR]

OLD_DATA_ROOT is the old `SmartArchiveData` folder (its smartarchive.db and archives). TARGET_DIR is where
the new installation's config lives (default: the current folder); the new data goes where that config
points. The old data is only read - opened read-only, never written, never moved.

Every old item is rebuilt from its archive without the old code (the old stored formats are decoded here
with the standard libraries and the JPEG XL tools), compared with the SHA-256 the old catalog recorded,
and only then archived by the new core into a category of the same name. An item that does not rebuild to
its recorded hash - or uses a format that cannot be decoded here - is reported as damaged and skipped; the
rest continue. A JSON report lists what was imported and what was not.
"""
import contextlib
import hashlib
import json
import lzma
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
import zipfile
import zlib
from compression import zstd

import brotli

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core import api, paths, tools  # noqa: E402

_SMART_PREFIX = b"smart:"


class NotImportable(Exception):
    pass


# --- the old stored formats (knowledge of the old version; none of its code) ---------------------------

def _zstd(data):
    return zstd.decompress(data, options={zstd.DecompressionParameter.window_log_max: 31})


def _jxl_to_jpeg(data, work):
    src, out = os.path.join(work, "old.jxl"), os.path.join(work, "restored.jpg")
    with open(src, "wb") as f:
        f.write(data)
    tools.run("djxl", [src, out], paths=(0, 1))
    with open(out, "rb") as f:
        return f.read()


def _bmp_jxl(data, work):
    """Old layout: <u32 header length><BMP header><JPEG XL of the pixels as top-down RGB>."""
    header_len = int.from_bytes(data[:4], "little")
    header, jxl = data[4:4 + header_len], data[4 + header_len:]
    src, out = os.path.join(work, "old_bmp.jxl"), os.path.join(work, "pixels.ppm")
    with open(src, "wb") as f:
        f.write(jxl)
    tools.run("djxl", [src, out], paths=(0, 1))
    with open(out, "rb") as f:
        ppm = f.read()
    width, height, pixels = _read_p6(ppm)
    row = width * 3
    rows = [pixels[r * row:(r + 1) * row] for r in range(height)]
    out_rows = bytearray()
    for r in reversed(rows):  # back to bottom-up, BGR
        swapped = bytearray(r)
        swapped[0::3], swapped[2::3] = r[2::3], r[0::3]
        out_rows += swapped
    return header + bytes(out_rows)


def _read_p6(data):
    tokens, pos = [], 2
    while len(tokens) < 3:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        start = pos
        while not data[pos:pos + 1].isspace():
            pos += 1
        tokens.append(int(data[start:pos]))
    width, height, maxval = tokens
    if not data.startswith(b"P6") or maxval != 255:
        raise NotImportable("unexpected decoded BMP pixels")
    return width, height, data[pos + 1:]


DECODERS = {
    "generic_zstd": lambda d, w: _zstd(d),
    "zstd_15": lambda d, w: _zstd(d),
    "zstd_long_range": lambda d, w: _zstd(d),
    "zstd_long_range_v2": lambda d, w: _zstd(d),
    "generic_lzma": lambda d, w: lzma.decompress(d),
    "generic_zlib": lambda d, w: zlib.decompress(d),
    "generic_brotli": lambda d, w: brotli.decompress(d),
    "brotli_q9": lambda d, w: brotli.decompress(d),
    "lzma_x86": lambda d, w: lzma.decompress(d[4:], format=lzma.FORMAT_RAW,
                                             filters=[{"id": lzma.FILTER_X86}, {"id": lzma.FILTER_LZMA2, "preset": 9}]),
    "jpeg_xl_lossless_jpeg": _jxl_to_jpeg,
    "bmp_jxl_lossless": _bmp_jxl,
}


def _member_bytes(z, info, work):
    comment = info.comment or b""
    raw = z.read(info)
    if not comment.startswith(_SMART_PREFIX):
        return raw
    try:
        _tag, strategy, expected, size = comment.decode("utf-8").split(":", 3)
    except (ValueError, UnicodeDecodeError):
        return raw  # the old version treated a malformed tag as a plain member too
    decoder = DECODERS.get(strategy)
    if decoder is None:
        raise NotImportable(f"{info.filename}: stored with {strategy}, which cannot be decoded here")
    data = decoder(raw, work)
    if hashlib.sha256(data).hexdigest() != expected or len(data) != int(size):
        raise NotImportable(f"{info.filename}: does not decode to its recorded bytes")
    return data


def _old_folder_hash(folder):
    """The old version's folder hash: sorted walk, relative path then file hash, concatenated."""
    entries = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        files.sort()
        for name in files:
            full = os.path.join(root, name)
            entries.append((os.path.relpath(full, folder).replace(os.sep, "/"), full))
    h = hashlib.sha256()
    for rel, full in entries:
        with open(full, "rb") as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
        h.update(rel.encode("utf-8"))
        h.update(file_hash.encode("utf-8"))
    return h.hexdigest()


# --- import ---------------------------------------------------------------------------------------------

def _open_read_only(db_path):
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path).replace("\\", "/"), safe="/:") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _resolve(old_root, stored_path):
    """Old archive paths were stored relative to the old working folder; find the file under OLD_DATA_ROOT."""
    stored = stored_path.replace("\\", "/")
    if os.path.isabs(stored) and os.path.isfile(stored):
        return stored
    marker = "/archives/"
    if marker in "/" + stored:
        tail = ("/" + stored).split(marker, 1)[1]
        candidate = os.path.join(old_root, "archives", *tail.split("/"))
        if os.path.isfile(candidate):
            return candidate
    return None


def import_old(old_root, target_dir=None):
    old_root = os.path.abspath(old_root)
    db_path = os.path.join(old_root, "smartarchive.db")
    if not os.path.isfile(db_path):
        raise SystemExit(f"no smartarchive.db in {old_root}")
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
        os.chdir(target_dir)
    api.initialize()
    if paths.is_under(api.data_root(), old_root) or paths.is_under(old_root, api.data_root()):
        raise SystemExit("the new data folder must not be inside the old one (or the other way round)")

    report = {"imported": [], "duplicates": [], "damaged": [], "checked_out_at_import": []}
    with contextlib.closing(_open_read_only(db_path)) as db:  # `with` on a connection alone does not close it
        categories = db.execute("SELECT id, name, description, color, max_archive_size FROM categories").fetchall()
        for _cid, name, description, color, max_size in categories:
            api.create_category(name, description, color, max_size)
        rows = db.execute(
            "SELECT items.id, items.file_type, items.file_name, items.path_in_archive, items.original_hash, "
            "items.status, archives.file_path, categories.name FROM items "
            "JOIN archives ON items.archive_id = archives.id JOIN categories ON archives.category_id = categories.id "
            "ORDER BY items.id").fetchall()

    for item_id, kind, name, member_path, expected, status, zip_stored, category in rows:
        label = {"old_item_id": item_id, "name": name, "category": category}
        if status == "CHECKED_OUT":
            report["checked_out_at_import"].append(label)  # the archived version is imported; edits stay where they are
        zip_path = _resolve(old_root, zip_stored)
        # outside the data folder: the core refuses to archive anything from inside its own data
        work = tempfile.mkdtemp(prefix="storagearch_import_")
        try:
            if zip_path is None:
                raise NotImportable("its archive file is missing")
            target = _rebuild(zip_path, kind, name, member_path, work)
            if kind == "folder":
                got = _old_folder_hash(target)
            else:
                with open(target, "rb") as f:
                    got = hashlib.sha256(f.read()).hexdigest()
            if got != expected:
                raise NotImportable("rebuilt content does not match the hash the old catalog recorded")
            result = (api.archive_folder_result if kind == "folder" else api.archive_file_result)(target, category)
            if not result.get("success"):
                raise NotImportable(f"the new core refused it: {result.get('reason')} {result.get('message')}")
            (report["duplicates"] if result["reason"] == "DUPLICATE" else report["imported"]).append(label)
        except (NotImportable, zipfile.BadZipFile, OSError, tools.ToolError, lzma.LZMAError, zlib.error,
                zstd.ZstdError, brotli.error, KeyError, ValueError, EOFError) as exc:
            report["damaged"].append({**label, "reason": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return report


def _rebuild(zip_path, kind, name, member_path, work):
    """Restores one old item into `work` under its own (safe) name; returns the path."""
    out_root = os.path.join(work, "item")
    os.makedirs(out_root)
    safe_name = paths.bare_name(name, fallback="item")
    normalized = member_path.rstrip("/")
    with zipfile.ZipFile(zip_path) as z:
        if kind == "folder":
            base = os.path.join(out_root, safe_name)
            os.makedirs(base)
            for info in z.infolist():
                if not info.filename.startswith(normalized + "/") or info.is_dir():
                    continue
                rel = info.filename[len(normalized) + 1:]
                target = paths.safe_target(base, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(_member_bytes(z, info, work))
            return base
        target = os.path.join(out_root, safe_name)
        with open(target, "wb") as f:
            f.write(_member_bytes(z, z.getinfo(member_path), work))
        return target


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    report = import_old(argv[1], argv[2] if len(argv) > 2 else None)
    out = os.path.join(api.data_root(), "logs", "import_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"imported {len(report['imported'])}, duplicates {len(report['duplicates'])}, "
          f"damaged {len(report['damaged'])}; report: {out}")


if __name__ == "__main__":
    main(sys.argv)
