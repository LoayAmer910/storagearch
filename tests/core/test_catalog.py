"""core.catalog - the catalog. Hashes are enforced by the schema; an item and its members are written together
or not at all; a sealed archive always has its file hash; the members digest follows the live members
only; a database from the old version is refused and left untouched; search treats % and _ literally;
the row shapes the GUI reads are unchanged; concurrent writers do not lose rows."""
import concurrent.futures
import hashlib
import sqlite3

import pytest

from core import catalog
from core.catalog import Catalog, LegacyDatabaseError


H = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731


@pytest.fixture
def cat(tmp_path):
    return Catalog(str(tmp_path / "db" / "smartarchive.db"))


def _member(path, content="x", kind="file", method="deflate"):
    return {"path_in_archive": path, "relative_path": path.split("/", 1)[-1], "kind": kind, "method": method,
            "method_version": 0, "original_size": len(content), "stored_size": len(content),
            "original_sha256": H(content), "stored_sha256": H(content + "!"), "crc32": 0}


def _item(uid, name="a.txt", content="x", file_type="file"):
    return {"file_type": file_type, "file_name": name, "original_path": f"C:/src/{name}",
            "path_in_archive": f"{uid}/{name}", "original_size": len(content), "compressed_size": len(content),
            "original_hash": H(content), "archive_hash": H(content + "!"), "uid": uid}


def _archive(cat, name="Docs"):
    cid = cat.add_category(name)
    return cat.add_archive(cid, f"C:/data/{name}_001.zip")


def test_schema_version_and_reopen(tmp_path, cat):
    Catalog(cat.db_path)
    with sqlite3.connect(cat.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == catalog.SCHEMA_VERSION


def test_an_old_version_database_is_refused_and_not_modified(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, file_name TEXT)")
        db.execute("INSERT INTO items (file_name) VALUES ('precious.txt')")
    before = path.read_bytes()
    with pytest.raises(LegacyDatabaseError):
        Catalog(str(path))
    assert path.read_bytes() == before


# Hebrew on purpose: non-ASCII category names must work
@pytest.mark.parametrize("folder", ["C#Projects", "100% sure", "what?", "שם עם רווח"])
def test_databases_under_unusual_folder_names_open_and_reopen(tmp_path, folder):
    try:
        (tmp_path / folder).mkdir()
    except OSError:
        pytest.skip("the file system does not allow this name")
    path = str(tmp_path / folder / "smartarchive.db")
    first = Catalog(path)
    first.add_category("Docs")
    assert Catalog(path).category_by_name("Docs") is not None


def test_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "new.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(LegacyDatabaseError):
        Catalog(str(path))


@pytest.mark.parametrize("name,size", [("", 10), ("   ", 10), (None, 10), ("ok", -1), ("ok", "abc"), ("ok", 1.5), ("ok", None)])
def test_invalid_categories_are_refused(cat, name, size):
    assert cat.add_category(name, max_archive_size=size) is None


def test_category_folders_are_fixed_and_unique(cat):
    sanitize = lambda n: n.replace("/", "_")  # noqa: E731
    a = cat.add_category("a/b", folder_name_for=sanitize)
    b = cat.add_category("a_b", folder_name_for=sanitize)
    c = cat.add_category("A_B", folder_name_for=sanitize)
    folders = {cat.category_by_id(i)[6] for i in (a, b, c)}
    assert len(folders) == 3, folders  # would have shared one folder before (and differ only by case)
    assert cat.add_category("a/b") is None
    assert cat.update_category(a, "renamed")
    assert cat.category_by_id(a)[1] == "renamed" and cat.category_by_id(a)[6] == "a_b"  # folder never follows the name
    assert not cat.update_category(a, "a_b")  # name taken
    assert not cat.update_category(9999, "x")


def test_delete_category_states(cat):
    empty = cat.add_category("Empty")
    full = cat.add_category("Full")
    cat.add_archive(full, "C:/d/Full_001.zip")
    assert cat.delete_category(empty) == "DELETED"
    assert cat.delete_category(full) == "NOT_EMPTY"
    assert cat.delete_category(empty) == "NOT_FOUND"


def test_hashes_are_mandatory_by_schema(cat):
    aid = _archive(cat)
    bad = _item("u1")
    bad["original_hash"] = None
    with pytest.raises(sqlite3.IntegrityError):
        cat.add_item(aid, bad, [_member("u1/a.txt")])
    bad["original_hash"] = "short"
    with pytest.raises(sqlite3.IntegrityError):
        cat.add_item(aid, bad, [_member("u1/a.txt")])


def test_an_item_and_its_members_are_written_together_or_not_at_all(cat):
    aid = _archive(cat)
    digest_before = cat.archive_state(aid)["members_digest"]
    broken = _member("u1/b.txt")
    broken["stored_sha256"] = "not a hash"
    with pytest.raises(sqlite3.IntegrityError):
        cat.add_item(aid, _item("u1"), [_member("u1/a.txt"), broken])
    assert cat.search_items("") == [] and cat.members_of_archive(aid) == []
    assert cat.archive_state(aid)["members_digest"] == digest_before


def test_a_sealed_archive_must_have_its_file_hash(cat):
    aid = _archive(cat)
    with pytest.raises(sqlite3.IntegrityError):
        with cat.transaction() as db:
            db.execute("UPDATE archives SET state = 'sealed' WHERE id = ?", (aid,))
    cat.seal_archive(aid, H("zip"))
    assert cat.archive_state(aid)["state"] == "sealed"


def test_members_digest_follows_live_members_and_not_insertion_order(tmp_path):
    first, second = Catalog(str(tmp_path / "1.db")), Catalog(str(tmp_path / "2.db"))
    a1, a2 = _archive(first), _archive(second)
    first.add_item(a1, _item("u1"), [_member("u1/a.txt", "1"), _member("u1/b.txt", "2")])
    second.add_item(a2, _item("u1"), [_member("u1/b.txt", "2"), _member("u1/a.txt", "1")])
    assert first.archive_state(a1)["members_digest"] == second.archive_state(a2)["members_digest"] != catalog.EMPTY_DIGEST


def test_checkin_replacement_marks_old_members_dead(cat):
    aid = _archive(cat)
    item_id = cat.add_item(aid, _item("u1", content="old"), [_member("u1/a.txt", "old")])
    digest_old = cat.archive_state(aid)["members_digest"]
    new = _item("u1", content="new")
    new["path_in_archive"] = "u2/a.txt"
    cat.replace_item_content(item_id, new, [_member("u2/a.txt", "newer content")])
    state = cat.archive_state(aid)
    assert state["dead_bytes"] == 3 and state["members_digest"] != digest_old
    assert [m["path_in_archive"] for m in cat.members_of_item(item_id)] == ["u2/a.txt"]
    assert len(cat.members_of_item(item_id, live_only=False)) == 2
    assert cat.item_by_id(item_id)[8] == H("new")
    cat.forget_dead_members(aid)
    assert cat.archive_state(aid)["dead_bytes"] == 0 and len(cat.members_of_archive(aid, live_only=False)) == 1


def test_search_treats_wildcards_literally(cat):
    aid = _archive(cat)
    for i, name in enumerate(("100% done.txt", "100x done.txt", "a_b.log", "axb.log")):
        cat.add_item(aid, _item(f"u{i}", name, content=name), [_member(f"u{i}/{name}", name)])
    assert [r[1] for r in cat.search_items("100%")] == ["100% done.txt"]
    assert [r[1] for r in cat.search_items("a_b")] == ["a_b.log"]
    assert [r[1] for r in cat.search_items("", extension=".log")] == ["a_b.log", "axb.log"]
    assert cat.search_items("", category_name="Nope") == []


def test_row_shapes_the_gui_reads_are_unchanged(cat):
    aid = _archive(cat)
    item_id = cat.add_item(aid, _item("u1"), [_member("u1/a.txt")])
    cat.set_item_status(item_id, "CHECKED_OUT", "C:/ws/a.txt")
    assert len(cat.list_categories()[0]) == 7
    assert len(cat.archive_by_id(aid)) == 12 and len(cat.list_archives()[0]) == 8
    item = cat.item_by_id(item_id)
    assert len(item) == 16 and item[2] == "file" and item[3] == "a.txt" and item[10] == "CHECKED_OUT"
    assert len(cat.search_items("a")[0]) == 11 and len(cat.find_item_by_hash(H("x"))) == 10
    assert len(cat.checked_out_items()[0]) == 5
    assert set(cat.dashboard_summary()) == {"category_count", "item_count", "total_original_size",
                                            "total_compressed_size", "total_saved", "checked_out_count", "integrity_counts"}
    assert not cat.set_item_status(item_id, "NOT_A_STATUS")


def test_concurrent_writers_do_not_lose_rows(cat):
    aid = _archive(cat)

    def add(i):
        return cat.add_item(aid, _item(f"c{i:03d}", f"f{i}.txt", str(i)), [_member(f"c{i:03d}/f{i}.txt", str(i))])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(add, range(80)))
    assert len(set(ids)) == 80 and len(cat.members_of_archive(aid)) == 80
    assert cat.archive_by_id(aid)[5] == 80


def test_checkin_can_move_an_item_to_another_archive(cat):
    cid = cat.add_category("Docs")
    old_aid = cat.add_archive(cid, "C:/d/Docs_001.zip")
    new_aid = cat.add_archive(cid, "C:/d/Docs_002.zip")
    item_id = cat.add_item(old_aid, _item("u1", content="old"), [_member("u1/a.txt", "old")])
    moved = _item("u1", content="new")
    moved["path_in_archive"] = "u2/a.txt"
    cat.replace_item_content(item_id, moved, [_member("u2/a.txt", "new")], new_archive_id=new_aid)
    assert cat.item_by_id(item_id)[1] == new_aid
    assert cat.archive_by_id(old_aid)[5] == 0 and cat.archive_by_id(new_aid)[5] == 1
    assert cat.archive_state(old_aid)["dead_bytes"] == 3 and cat.members_of_archive(old_aid) == []
    assert cat.archive_state(old_aid)["members_digest"] == catalog.EMPTY_DIGEST


def test_adding_the_same_archive_file_twice_returns_the_same_row(cat):
    cid = cat.add_category("Docs")
    first = cat.add_archive(cid, "C:/data/Docs_001.zip")
    assert cat.add_archive(cid, "C:/data/Docs_001.zip") == first
    assert len(cat.archives_of_category(cid)) == 1


def test_an_unknown_integrity_status_is_refused_and_nothing_changes(cat):
    aid = _archive(cat)
    before = cat.archive_by_id(aid)
    assert cat.set_archive_status(aid, "MAYBE") is False
    assert cat.archive_by_id(aid) == before


def test_a_damaged_archive_is_marked_damaged_and_corrupted(cat):
    aid = _archive(cat)
    cat.mark_archive_damaged(aid)
    assert cat.archive_state(aid)["state"] == "damaged"
    assert [row[6] for row in cat.list_archives() if row[0] == aid] == ["CORRUPTED"]


def test_kept_members_cannot_move_to_another_archive(cat):
    aid = _archive(cat)
    item_id = cat.add_item(aid, _item("u1"), [_member("u1/a.txt")])
    other = cat.add_archive(cat.category_by_name("Docs")[0], "C:/data/Docs_002.zip")
    with pytest.raises(ValueError, match="cannot move to another archive"):
        cat.replace_item_content(item_id, _item("u1"), [_member("u1/b.txt")], new_archive_id=other, kept=["u1/a.txt"])
    assert [m["path_in_archive"] for m in cat.members_of_item(item_id)] == ["u1/a.txt"]  # nothing changed


def test_only_a_live_member_of_the_item_can_be_kept(cat):
    aid = _archive(cat)
    item_id = cat.add_item(aid, _item("u1"), [_member("u1/a.txt")])
    with pytest.raises(ValueError, match="not a live member"):
        cat.replace_item_content(item_id, _item("u1"), [_member("u1/b.txt")], kept=["u1/not_there.txt"])
    assert [m["path_in_archive"] for m in cat.members_of_item(item_id)] == ["u1/a.txt"]
