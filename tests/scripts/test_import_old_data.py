"""scripts/import_old_data.py - importing the old version's data. The tests build data in the old format themselves
(an old catalog and its ZIP archives, in every old codec), without the old code: every item then restores from the
new core exactly as the original; a damaged old item is reported and skipped while the rest import; the old data is
never modified."""
import hashlib
import json
import lzma
import os
import sqlite3
import zipfile
import zlib
from compression import zstd

import brotli
import pytest

from core import api, tools
from scripts import import_old_data
from support import corpus
from support.api import read


def _snapshot(root):
    out = {}
    for base, _d, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(read(p)).hexdigest()
    return out


_X86 = [{"id": lzma.FILTER_X86}, {"id": lzma.FILTER_LZMA2, "preset": 9}]
OLD_ENCODERS = {
    "generic_zstd": zstd.compress,
    "generic_lzma": lzma.compress,
    "generic_zlib": zlib.compress,
    "brotli_q9": brotli.compress,
    "lzma_x86": lambda d: b"\0\0\0\0" + lzma.compress(d, format=lzma.FORMAT_RAW, filters=_X86),
}


class OldData:
    """An old SmartArchiveData folder: its catalog (the columns the importer reads) and its ZIP archives."""

    def __init__(self, root):
        self.root = root
        (root / "archives").mkdir(parents=True)
        self.db = sqlite3.connect(root / "smartarchive.db")
        self.db.executescript(
            "CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT, description TEXT, color TEXT, max_archive_size INTEGER);"
            "CREATE TABLE archives (id INTEGER PRIMARY KEY, category_id INTEGER, file_path TEXT);"
            "CREATE TABLE items (id INTEGER PRIMARY KEY, archive_id INTEGER, file_type TEXT, file_name TEXT, "
            "path_in_archive TEXT, original_hash TEXT, status TEXT);"
            "INSERT INTO categories VALUES (1, 'Old Docs', 'from the old version', '#123456', 50000000);"
            "INSERT INTO archives VALUES (1, 1, 'SmartArchiveData/archives/Old Docs/Old Docs_001.zip');")
        self.zip_path = root / "archives" / "Old Docs" / "Old Docs_001.zip"
        self.zip_path.parent.mkdir()
        self.members = []  # (name, bytes, comment)

    def add_file(self, name, data, strategy=None, comment=None, status="ARCHIVED", recorded_hash=None):
        member = f"u{len(self.members)}/{name}"
        stored = OLD_ENCODERS[strategy](data) if strategy else data
        if comment is None and strategy:
            comment = f"smart:{strategy}:{hashlib.sha256(data).hexdigest()}:{len(data)}"
        self.members.append((member, stored, comment))
        self.db.execute("INSERT INTO items (archive_id, file_type, file_name, path_in_archive, original_hash, status) "
                        "VALUES (1, 'file', ?, ?, ?, ?)", (name, member, recorded_hash or hashlib.sha256(data).hexdigest(), status))

    def add_folder(self, name, files):
        member = f"u{len(self.members)}/{name}"
        for rel, data in files.items():
            self.members.append((f"{member}/{rel}", data, None))
        h = hashlib.sha256()
        for rel in sorted(files):
            h.update(rel.encode())
            h.update(hashlib.sha256(files[rel]).hexdigest().encode())
        self.db.execute("INSERT INTO items (archive_id, file_type, file_name, path_in_archive, original_hash, status) "
                        "VALUES (1, 'folder', ?, ?, ?, 'ARCHIVED')", (name, member, h.hexdigest()))

    def write(self):
        self.db.commit()
        self.db.close()
        with zipfile.ZipFile(self.zip_path, "w") as z:
            for name, data, comment in self.members:
                info = zipfile.ZipInfo(name)
                if comment:
                    info.comment = comment.encode()
                z.writestr(info, data)
        return str(self.root)


@pytest.fixture
def old(tmp_path):
    return OldData(tmp_path / "old" / "SmartArchiveData")


@pytest.fixture
def new_home(tmp_path, monkeypatch):
    home = tmp_path / "new"
    home.mkdir()
    monkeypatch.chdir(home)  # the importer changes into it; this puts the old working folder back afterwards
    return str(home)


def _content(name):
    item = api.search(name)[0]
    return read(api.checkout(item[0], destination=str(os.path.join(os.getcwd(), "restored", name))))


@pytest.mark.parametrize("strategy", sorted(OLD_ENCODERS))
def test_an_item_in_each_old_codec_comes_back_exactly(old, new_home, strategy):
    data = corpus.lines(3000, "row {i} {w} {n}\n")
    old.add_file("report.txt", data, strategy)
    report = import_old_data.import_old(old.write(), new_home)
    assert [i["name"] for i in report["imported"]] == ["report.txt"] and report["damaged"] == []
    assert _content("report.txt") == data
    assert [c[1] for c in api.list_categories()] == ["Old Docs"]


def test_a_folder_and_a_plain_member_import_and_the_old_data_is_untouched(old, new_home):
    old.add_file("plain.txt", b"stored without a smart codec")
    old.add_file("tagged_badly.txt", b"a malformed tag means a plain member", comment="smart:broken")
    old.add_folder("project", {"a.txt": b"one", "sub/b.txt": b"two"})
    root = old.write()
    before = _snapshot(root)
    report = import_old_data.import_old(root, new_home)
    assert sorted(i["name"] for i in report["imported"]) == ["plain.txt", "project", "tagged_badly.txt"]
    assert _snapshot(root) == before, "the old data must only be read"
    restored = api.checkout(api.search("project")[0][0])
    assert read(os.path.join(restored, "sub", "b.txt")) == b"two" and read(os.path.join(restored, "a.txt")) == b"one"


def test_damaged_old_items_are_reported_and_the_rest_import(old, new_home):
    data = b"the original bytes" * 100
    old.add_file("good.txt", data, "generic_zlib")
    old.add_file("unknown_codec.txt", data, comment=f"smart:fancy_codec:{hashlib.sha256(data).hexdigest()}:{len(data)}")
    old.add_file("wrong_bytes.txt", zlib.compress(b"other"), comment=f"smart:generic_zlib:{hashlib.sha256(data).hexdigest()}:{len(data)}")
    old.add_file("wrong_catalog_hash.txt", b"plain", recorded_hash="0" * 64)
    report = import_old_data.import_old(old.write(), new_home)
    assert [i["name"] for i in report["imported"]] == ["good.txt"]
    reasons = {d["name"]: d["reason"] for d in report["damaged"]}
    assert "cannot be decoded here" in reasons["unknown_codec.txt"]
    assert "does not decode to its recorded bytes" in reasons["wrong_bytes.txt"]
    assert "does not match the hash the old catalog recorded" in reasons["wrong_catalog_hash.txt"]


def test_an_item_whose_archive_is_missing_is_reported(old, new_home):
    old.add_file("a.txt", b"x")
    root = old.write()
    os.remove(old.zip_path)
    report = import_old_data.import_old(root, new_home)
    assert report["damaged"][0]["reason"] == "NotImportable: its archive file is missing"


def test_a_checked_out_old_item_is_imported_and_listed(old, new_home):
    old.add_file("edited.txt", b"archived version", status="CHECKED_OUT")
    report = import_old_data.import_old(old.write(), new_home)
    assert [i["name"] for i in report["checked_out_at_import"]] == ["edited.txt"]
    assert [i["name"] for i in report["imported"]] == ["edited.txt"]


def test_the_same_content_twice_is_imported_once(old, new_home):
    old.add_file("first.txt", b"same")
    old.add_file("second.txt", b"same")
    report = import_old_data.import_old(old.write(), new_home)
    assert [i["name"] for i in report["imported"]] == ["first.txt"]
    assert [i["name"] for i in report["duplicates"]] == ["second.txt"]


def test_an_old_folder_without_a_catalog_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="no smartarchive.db"):
        import_old_data.import_old(str(tmp_path))


def test_the_command_line_writes_a_report_and_explains_its_use(old, new_home, capsys):
    old.add_file("a.txt", b"x")
    import_old_data.main(["import_old_data.py", old.write(), new_home])
    out = capsys.readouterr().out
    assert out.startswith("imported 1, duplicates 0, damaged 0; report: ")
    with open(os.path.join(api.data_root(), "logs", "import_report.json"), encoding="utf-8") as f:
        report = json.load(f)
    assert [i["name"] for i in report["imported"]] == ["a.txt"]
    with pytest.raises(SystemExit):
        import_old_data.main(["import_old_data.py"])


def test_an_absolute_archive_path_from_the_old_catalog_is_used_as_is(old, tmp_path):
    old.add_file("a.txt", b"x")
    root = old.write()
    assert os.path.normpath(import_old_data._resolve(root, str(old.zip_path))) == str(old.zip_path)
    assert import_old_data._resolve(root, "elsewhere/not_an_archive.zip") is None


def test_old_decoded_bmp_pixels_in_an_unexpected_format_are_refused():
    with pytest.raises(import_old_data.NotImportable):
        import_old_data._read_p6(b"P6\n# comment\n1 1\n65535\n" + bytes(6))
    assert import_old_data._read_p6(b"P6\n# by djxl\n1 1\n255\n" + bytes(3)) == (1, 1, bytes(3))


def test_the_new_data_may_not_be_inside_the_old_folder(old):
    old.add_file("a.txt", b"x")
    root = old.write()
    with pytest.raises(SystemExit, match="must not be inside the old one"):
        import_old_data.import_old(root, os.path.join(root, "new_install"))


# --- the old image formats need the JPEG XL tools --------------------------------------------------------

needs_jxl = pytest.mark.skipif(not tools.available("cjxl", "djxl")[0], reason="cjxl/djxl not found")


def _cjxl(src_bytes, suffix, tmp_path, *options):
    src, out = tmp_path / f"in{suffix}", tmp_path / "out.jxl"
    src.write_bytes(src_bytes)
    tools.run("cjxl", [str(src), str(out), *options], paths=(0, 1))
    return out.read_bytes()


@needs_jxl
def test_an_old_jpeg_stored_as_jpeg_xl_comes_back_exactly(old, new_home, tmp_path):
    import io
    buffer = io.BytesIO()
    corpus.photo(64, 48, 3).save(buffer, format="JPEG", quality=90)
    jpeg = buffer.getvalue()
    stored = _cjxl(jpeg, ".jpg", tmp_path, "--lossless_jpeg=1")
    old.members.append(("u0/photo.jpg", stored,
                        f"smart:jpeg_xl_lossless_jpeg:{hashlib.sha256(jpeg).hexdigest()}:{len(jpeg)}"))
    old.db.execute("INSERT INTO items (archive_id, file_type, file_name, path_in_archive, original_hash, status) "
                   "VALUES (1, 'file', 'photo.jpg', 'u0/photo.jpg', ?, 'ARCHIVED')", (hashlib.sha256(jpeg).hexdigest(),))
    report = import_old_data.import_old(old.write(), new_home)
    assert [i["name"] for i in report["imported"]] == ["photo.jpg"], report["damaged"]
    assert read(api.checkout(api.search("photo.jpg")[0][0])) == jpeg


@needs_jxl
def test_an_old_bmp_stored_as_header_plus_jpeg_xl_pixels_comes_back_exactly(old, new_home, tmp_path):
    import io
    buffer = io.BytesIO()
    corpus.photo(32, 20, 4).save(buffer, format="BMP")  # 24-bit, bottom-up BGR rows of 96 bytes (no padding)
    bmp = buffer.getvalue()
    offset = int.from_bytes(bmp[10:14], "little")
    header, rows = bmp[:offset], [bmp[offset + r * 96: offset + (r + 1) * 96] for r in range(20)]
    rgb_top_down = bytearray()
    for row in reversed(rows):
        swapped = bytearray(row)
        swapped[0::3], swapped[2::3] = row[2::3], row[0::3]
        rgb_top_down += swapped
    jxl = _cjxl(b"P6\n32 20\n255\n" + bytes(rgb_top_down), ".ppm", tmp_path, "-d", "0")
    stored = len(header).to_bytes(4, "little") + header + jxl
    old.members.append(("u0/scan.bmp", stored, f"smart:bmp_jxl_lossless:{hashlib.sha256(bmp).hexdigest()}:{len(bmp)}"))
    old.db.execute("INSERT INTO items (archive_id, file_type, file_name, path_in_archive, original_hash, status) "
                   "VALUES (1, 'file', 'scan.bmp', 'u0/scan.bmp', ?, 'ARCHIVED')", (hashlib.sha256(bmp).hexdigest(),))
    report = import_old_data.import_old(old.write(), new_home)
    assert [i["name"] for i in report["imported"]] == ["scan.bmp"], report["damaged"]
    assert read(api.checkout(api.search("scan.bmp")[0][0])) == bmp
