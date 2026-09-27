"""Step 6c - the archive files (I1, I3, I7). Written members read back exactly; a baseline member is exactly
the size the pipeline measured; a failed or crashed append leaves the archive byte-identical to before; a
source that changes while being written is refused; tampered bytes or tampered names never produce a file;
empty folders survive; compaction drops dead members without touching live ones."""
import hashlib
import json
import os
import zipfile

import pytest

import corpus
from core import strategies, store
from core.catalog import EMPTY_DIGEST
from core.paths import ExtractionRefused
from core.pipeline import scan
from core.store import PlannedMember, RestoreError, SourceChanged, StoreError


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    return corpus.build(str(tmp_path_factory.mktemp("corpus")))


def _baseline(path, uid="u1", rel=None):
    _info, method, _size = scan(path)
    rel = rel or os.path.basename(path)
    return PlannedMember(f"{uid}/{rel}", rel, "file", method, 0, path, None, _sha(path), os.path.getsize(path),
                         os.path.getmtime(path))


def _smart(path, tmp_path, uid="u2", name="zstd"):
    work = tmp_path / f"work_{uid}"
    work.mkdir()
    strategy = strategies.get(name)
    stored = strategy.compress(path, str(work))
    rel = os.path.basename(path)
    return PlannedMember(f"{uid}/{rel}", rel, "file", name, strategy.spec.version, path, stored, _sha(path),
                         os.path.getsize(path), None)


def _catalog_rows(records):
    return [dict(r, id=i, item_id=1, archive_id=1, live=1) for i, r in enumerate(records)]


def test_members_round_trip_and_baseline_matches_the_measured_size(samples, tmp_path):
    zip_path = str(tmp_path / "a" / "Docs_001.zip")
    planned = [_baseline(samples["large_text.txt"].path), _baseline(samples["large_random.bin"].path, "u3"),
               _smart(samples["text.log"].path, tmp_path), PlannedMember("u4/folder/empty/", "folder/empty", "dir", "stored")]
    records = store.write_members(zip_path, planned)
    assert records[0]["method"] == "deflate" and records[0]["stored_size"] == scan(samples["large_text.txt"].path)[2]
    assert records[1]["method"] == "stored" and records[1]["stored_size"] == os.path.getsize(samples["large_random.bin"].path)
    assert records[2]["path_in_archive"].endswith(store.SMART_SUFFIX)
    dest = tmp_path / "out"
    restored = store.extract_members(zip_path, _catalog_rows(records), str(dest))
    assert [_sha(p) for p in restored] == [m.original_sha256 for m in planned[:3]]
    assert (dest / "folder" / "empty").is_dir()
    assert store.verify_archive(zip_path, _catalog_rows(records)) == "VERIFIED"


def test_the_archive_is_a_normal_zip_and_describes_itself(samples, tmp_path):
    zip_path = str(tmp_path / "Docs_001.zip")
    planned = [_baseline(samples["text.csv"].path), _smart(samples["text.log"].path, tmp_path)]
    store.write_members(zip_path, planned)
    with zipfile.ZipFile(zip_path) as z:
        assert z.read("u1/text.csv") == open(samples["text.csv"].path, "rb").read()  # any ZIP tool gets the real file
        meta = json.loads(z.getinfo("u2/text.log" + store.SMART_SUFFIX).comment)
        assert meta["s"] == "zstd" and meta["sha256"] == planned[1].original_sha256


def test_appending_keeps_earlier_members(samples, tmp_path):
    zip_path = str(tmp_path / "Docs_001.zip")
    first = store.write_members(zip_path, [_baseline(samples["text.csv"].path)])
    second = store.write_members(zip_path, [_smart(samples["text.log"].path, tmp_path)])
    rows = _catalog_rows(first + second)
    assert store.verify_archive(zip_path, rows) == "VERIFIED"
    assert len(store.extract_members(zip_path, rows, str(tmp_path / "out"))) == 2


def test_a_source_that_changes_during_the_write_is_refused_and_leaves_the_archive_as_it_was(samples, tmp_path):
    zip_path = str(tmp_path / "Docs_001.zip")
    store.write_members(zip_path, [_baseline(samples["text.csv"].path)])
    before = _sha(zip_path)
    src = tmp_path / "moving.txt"
    src.write_bytes(open(samples["large_text.txt"].path, "rb").read())
    planned = _baseline(str(src), "u9")
    src.write_bytes(src.read_bytes() + b"edited after the decision\n")
    with pytest.raises(SourceChanged):
        store.write_members(zip_path, [planned])
    assert _sha(zip_path) == before
    assert not os.path.exists(zip_path + ".guard") and not os.path.exists(zip_path + ".tail")
    with pytest.raises(SourceChanged):
        store.write_members(str(tmp_path / "New_001.zip"), [planned])
    assert os.listdir(tmp_path).count("New_001.zip") == 0 and not os.path.exists(str(tmp_path / "New_001.zip.partial"))


def test_a_duplicate_member_name_is_refused_without_changes(samples, tmp_path):
    zip_path = str(tmp_path / "Docs_001.zip")
    store.write_members(zip_path, [_baseline(samples["text.csv"].path)])
    before = _sha(zip_path)
    with pytest.raises(StoreError):
        store.write_members(zip_path, [_baseline(samples["text.csv"].path)])
    assert _sha(zip_path) == before


def test_a_crash_in_the_middle_of_an_append_is_rolled_back_at_startup(samples, tmp_path):
    archives = tmp_path / "archives" / "Docs"
    archives.mkdir(parents=True)
    zip_path = str(archives / "Docs_001.zip")
    store.write_members(zip_path, [_baseline(samples["text.csv"].path)])
    before = _sha(zip_path)
    # the state a crash leaves: guard and tail saved, half-written data appended over the central directory
    with zipfile.ZipFile(zip_path) as z:
        start = z.start_dir
    tail = open(zip_path, "rb").read()[start:]
    open(zip_path + ".tail", "wb").write(tail)
    open(zip_path + ".guard", "w").write(json.dumps({"start_dir": start, "tail_size": len(tail)}))
    with open(zip_path, "r+b") as f:
        f.seek(start)
        f.write(b"PK\x03\x04 half written member" * 100)
    open(str(archives / "Docs_002.zip.partial"), "wb").write(b"unfinished")
    touched = store.recover(str(tmp_path / "archives"))
    assert _sha(zip_path) == before and zip_path in touched
    assert sorted(os.listdir(archives)) == ["Docs_001.zip"]
    zipfile.ZipFile(zip_path).testzip()


@pytest.mark.parametrize("bad", ["../escape.txt", "a/../../escape.txt", "C:/escape.txt", "a\\..\\escape.txt", "CON"])
def test_tampered_relative_paths_are_refused_before_anything_is_written(samples, tmp_path, bad):
    zip_path = str(tmp_path / "Docs_001.zip")
    rows = _catalog_rows(store.write_members(zip_path, [_baseline(samples["text.csv"].path),
                                                        _baseline(samples["text.md"].path, "u2")]))
    rows[1]["relative_path"] = bad
    dest = tmp_path / "dest" / "inner"
    with pytest.raises(ExtractionRefused):
        store.extract_members(zip_path, rows, str(dest))
    assert not dest.exists() or list(dest.iterdir()) == []
    assert not any("escape" in f for _d, _s, fs in os.walk(tmp_path) for f in fs)


def _flip_member_byte(zip_path, member_name):
    with zipfile.ZipFile(zip_path) as z:
        info = z.getinfo(member_name)
    data = bytearray(open(zip_path, "rb").read())
    offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra) + info.compress_size // 2
    data[offset] ^= 0x55
    open(zip_path, "wb").write(bytes(data))


@pytest.mark.parametrize("kind", ["smart", "baseline"])
def test_damaged_member_bytes_never_produce_a_file(samples, tmp_path, kind):
    zip_path = str(tmp_path / "Docs_001.zip")
    planned = _smart(samples["large_text.txt"].path, tmp_path) if kind == "smart" else _baseline(samples["large_text.txt"].path)
    rows = _catalog_rows(store.write_members(zip_path, [planned]))
    _flip_member_byte(zip_path, rows[0]["path_in_archive"])
    assert store.verify_archive(zip_path, rows) in ("CORRUPTED", "CHANGED")
    dest = tmp_path / "out"
    with pytest.raises(RestoreError):
        store.extract_members(zip_path, rows, str(dest))
    assert not any(f for _d, _s, f in os.walk(dest) if f), "a file (or a partial one) appeared after a failed restore"


def test_a_member_stored_with_another_format_version_is_refused(samples, tmp_path):
    zip_path = str(tmp_path / "Docs_001.zip")
    rows = _catalog_rows(store.write_members(zip_path, [_smart(samples["text.log"].path, tmp_path)]))
    rows[0]["method_version"] = 99
    with pytest.raises(RestoreError, match="format"):
        store.extract_members(zip_path, rows, str(tmp_path / "out"))


def test_verify_statuses(samples, tmp_path):
    zip_path = str(tmp_path / "Docs_001.zip")
    rows = _catalog_rows(store.write_members(zip_path, [_baseline(samples["text.csv"].path)]))
    assert store.verify_archive(str(tmp_path / "missing.zip"), rows) == "MISSING"
    assert store.verify_archive(zip_path, rows, sealed_sha256=_sha(zip_path)) == "VERIFIED"
    assert store.verify_archive(zip_path, rows, sealed_sha256="0" * 64) == "CHANGED"
    assert store.verify_archive(zip_path, rows + [dict(rows[0], path_in_archive="u1/gone.txt")]) == "CHANGED"
    open(zip_path, "wb").write(b"not a zip at all")
    assert store.verify_archive(zip_path, rows) == "CORRUPTED"


def test_compaction_drops_dead_members_and_keeps_live_ones_exact(samples, tmp_path):
    zip_path = str(tmp_path / "Docs_001.zip")
    dead = store.write_members(zip_path, [_baseline(samples["large_text.txt"].path, "old")])
    live = store.write_members(zip_path, [_baseline(samples["text.csv"].path, "new"), _smart(samples["text.log"].path, tmp_path, "s")])
    size_before = os.path.getsize(zip_path)
    updated = store.compact(zip_path, _catalog_rows(live))
    assert os.path.getsize(zip_path) < size_before - dead[0]["stored_size"] // 2
    for r in live:
        assert updated[r["path_in_archive"]] == {"stored_size": r["stored_size"], "crc32": r["crc32"]}
    assert len(store.extract_members(zip_path, _catalog_rows(live), str(tmp_path / "out"))) == 2
    with zipfile.ZipFile(zip_path) as z:
        assert not any(n.startswith("old/") for n in z.namelist())


def test_unicode_names_and_empty_directories_survive(tmp_path):
    src = tmp_path / "תיקייה"
    (src / "ריק").mkdir(parents=True)
    f = src / "קובץ.txt"
    f.write_bytes(corpus.lines(2000, "שורה {i} {w}\n"))
    zip_path = str(tmp_path / "Docs_001.zip")
    planned = [PlannedMember("u1/תיקייה/קובץ.txt", "תיקייה/קובץ.txt", "file", "deflate", 0, str(f), None, _sha(f), f.stat().st_size),
               PlannedMember("u1/תיקייה/ריק/", "תיקייה/ריק", "dir", "stored")]
    rows = _catalog_rows(store.write_members(zip_path, planned))
    out = tmp_path / "out"
    store.extract_members(zip_path, rows, str(out))
    assert (out / "תיקייה" / "ריק").is_dir() and _sha(out / "תיקייה" / "קובץ.txt") == _sha(f)
    assert rows[1]["original_sha256"] == EMPTY_DIGEST


def test_data_appended_after_the_archive_or_unknown_entries_are_noticed(samples, tmp_path):
    zip_path = str(tmp_path / "Docs_001.zip")
    rows = _catalog_rows(store.write_members(zip_path, [_baseline(samples["text.csv"].path)]))
    size = os.path.getsize(zip_path)
    names = [r["path_in_archive"] for r in rows]
    assert store.verify_archive(zip_path, rows, expected_size=size, known_names=names) == "VERIFIED"
    with open(zip_path, "ab") as f:
        f.write(b"garbage appended after the central directory")
    assert zipfile.ZipFile(zip_path).testzip() is None  # zipfile alone does not notice
    assert store.verify_archive(zip_path, rows, expected_size=size, known_names=names) == "CHANGED"
    other = str(tmp_path / "Docs_002.zip")
    rows2 = _catalog_rows(store.write_members(other, [_baseline(samples["text.csv"].path)]))
    with zipfile.ZipFile(other, "a") as z:
        z.writestr("smuggled.txt", "not from the catalog")
    assert store.verify_archive(other, rows2, known_names=[r["path_in_archive"] for r in rows2]) == "CHANGED"
