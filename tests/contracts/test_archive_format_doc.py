"""docs/ARCHIVE_FORMAT.md is true: a file is taken out of an archive with nothing but a ZIP reader, the comment
of its entry and the standard decoder the document names - no StorageArch code, no database."""
import hashlib
import json
import lzma
import os
import zipfile
import zlib
from compression import zstd

import brotli
import pytest

from core import api
from support.files import write_lines_file

DECODERS = {
    "zstd": zstd.decompress,
    "zstd_ldm": lambda data: zstd.decompress(data, options={zstd.DecompressionParameter.window_log_max: 27}),
    "lzma": lambda data: lzma.decompress(data, format=lzma.FORMAT_XZ),
    "lzma_x86": lambda data: lzma.decompress(data, format=lzma.FORMAT_XZ),
    "brotli": brotli.decompress,
    "zlib": zlib.decompress,
}


def _restore_by_the_document(zip_path, out_dir):
    """Every file of the archive, restored as the document says. Returns {name: (bytes, method)}."""
    restored = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            data = z.read(info)
            if not info.filename.endswith(".sa"):
                restored[info.filename] = (data, "plain")
                continue
            how = json.loads(info.comment)
            assert set(how) == {"s", "v", "sha256", "size", "mtime"}
            original = DECODERS[how["s"]](data)
            assert len(original) == how["size"] and hashlib.sha256(original).hexdigest() == how["sha256"]
            restored[info.filename[:-len(".sa")]] = (original, how["s"])
    return restored


@pytest.mark.parametrize("lines, expected_method", [(40, "plain"), (3000, None), (60000, "lzma")])
def test_a_file_is_restored_from_the_archive_by_the_document_alone(app, lines, expected_method):
    src = write_lines_file(app, "report.txt", lines)
    assert api.archive_file_result(src, "Docs")["success"]
    (found,) = api.search("report.txt")
    (name, (data, method)), = _restore_by_the_document(found.archive_path, app / "out").items()
    with open(src, "rb") as f:
        assert data == f.read()
    assert name.split("/", 1)[1] == "report.txt" and len(name.split("/", 1)[0]) == 12
    if expected_method:
        assert method == expected_method


def test_a_folder_keeps_its_layout_under_one_id_and_the_manifest_lists_it(app):
    folder = app / "project"
    for i in range(6):
        write_lines_file(folder, f"src/pkg{i % 2}/mod{i}.txt", 400 + 900 * i, seed=i)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    (found,) = api.search("project")
    restored = _restore_by_the_document(found.archive_path, app / "out")
    tops = {name.split("/", 1)[0] for name in restored}
    assert len(tops) == 1
    for name, (data, _method) in restored.items():
        with open(os.path.join(str(app), *name.split("/")[1:]), "rb") as f:
            assert data == f.read()
    with open(os.path.splitext(found.archive_path)[0] + ".manifest.json", encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["manifest_version"] == 2 and manifest["state"] in ("open", "sealed") and manifest["category"] == "Docs"
    files = [m for m in manifest["files"] if m["kind"] == "file"]
    assert len(files) == 6 and set(files[0]) == {"path_in_archive", "relative_path", "kind", "method", "method_version",
                                                 "original_size", "stored_size", "original_sha256", "stored_sha256"}


def test_every_method_the_core_can_write_is_named_in_the_document():
    from core import strategies
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs",
                           "ARCHIVE_FORMAT.md"), encoding="utf-8") as f:
        document = f.read()
    names = set(strategies.REGISTRY)
    assert names >= set(DECODERS) | {"jpeg_xl", "bmp_jxl"}
    missing = [n for n in names if n != "deflate" and f"`{n}`" not in document]
    assert missing == [], missing
