"""The local catalog (SQLite): categories, archives, items and - new - one row per stored ZIP member.

Hashes are mandatory by schema: every member has its original and stored SHA-256, every item its
original and archive hash, and every archive a members digest (a SHA-256 over its live member rows,
refreshed in the same transaction that changes them). A sealed archive must have its whole-file hash.
An item and its members are written in one transaction, so a failure never leaves half an item.

The row shapes the GUI reads by index (categories, archives, items, search results) are kept exactly as
before. A database written by the old version is recognized and refused, never modified (import tool).
"""
import hashlib
import os
import sqlite3
import threading
import urllib.parse
import uuid
from contextlib import contextmanager

SCHEMA_VERSION = 2
ITEM_STATUSES = {"ARCHIVED", "CHECKED_OUT", "ARCHIVING", "CHECKING_IN", "ERROR", "MISSING"}
ARCHIVE_STATUSES = {"VERIFIED", "CHANGED", "CORRUPTED", "MISSING"}
ARCHIVE_STATES = {"open", "sealed", "damaged"}
DEFAULT_MAX_ARCHIVE_SIZE = 2147483648

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    description TEXT,
    color TEXT,
    max_archive_size INTEGER NOT NULL DEFAULT {DEFAULT_MAX_ARCHIVE_SIZE} CHECK (max_archive_size >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    folder_name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    current_size INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    zip_hash TEXT CHECK (zip_hash IS NULL OR length(zip_hash) = 64),
    manifest_path TEXT,
    integrity_status TEXT NOT NULL DEFAULT 'VERIFIED',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    last_verified_at TEXT,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'sealed', 'damaged')),
    members_digest TEXT NOT NULL CHECK (length(members_digest) = 64),
    dead_bytes INTEGER NOT NULL DEFAULT 0,
    CHECK (state <> 'sealed' OR zip_hash IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    file_type TEXT NOT NULL CHECK (file_type IN ('file', 'folder')),
    file_name TEXT NOT NULL,
    original_path TEXT NOT NULL,
    path_in_archive TEXT NOT NULL,
    original_size INTEGER NOT NULL CHECK (original_size >= 0),
    compressed_size INTEGER NOT NULL CHECK (compressed_size >= 0),
    original_hash TEXT NOT NULL CHECK (length(original_hash) = 64),
    archive_hash TEXT NOT NULL CHECK (length(archive_hash) = 64),
    status TEXT NOT NULL DEFAULT 'ARCHIVED',
    workspace_path TEXT,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checked_out_at TEXT,
    checked_in_at TEXT,
    strategy TEXT,
    uid TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_items_original_hash ON items (original_hash);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    path_in_archive TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
    method TEXT NOT NULL,
    method_version INTEGER NOT NULL DEFAULT 0,
    original_size INTEGER NOT NULL CHECK (original_size >= 0),
    stored_size INTEGER NOT NULL CHECK (stored_size >= 0),
    original_sha256 TEXT NOT NULL CHECK (length(original_sha256) = 64),
    stored_sha256 TEXT NOT NULL CHECK (length(stored_sha256) = 64),
    crc32 INTEGER NOT NULL,
    live INTEGER NOT NULL DEFAULT 1 CHECK (live IN (0, 1)),
    UNIQUE (archive_id, path_in_archive)
);
CREATE INDEX IF NOT EXISTS idx_members_item ON members (item_id);
"""

_CATEGORY_COLUMNS = "id, name, description, color, max_archive_size, created_at, folder_name"
_ARCHIVE_COLUMNS = ("id, category_id, file_name, file_path, current_size, item_count, zip_hash, manifest_path, "
                    "integrity_status, created_at, updated_at, last_verified_at")
_ITEM_COLUMNS = ("id, archive_id, file_type, file_name, original_path, path_in_archive, original_size, "
                 "compressed_size, original_hash, archive_hash, status, workspace_path, archived_at, "
                 "checked_out_at, checked_in_at, strategy")
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class LegacyDatabaseError(RuntimeError):
    """The database was written by the old version; it is not opened for writing."""


def _norm(path):
    return None if path is None else os.path.abspath(path).replace("\\", "/")


def _escape_like(text):
    return str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Catalog:
    def __init__(self, db_path):
        self.db_path = os.path.abspath(db_path)
        self._write_lock = threading.RLock()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if os.path.exists(self.db_path):
            self._refuse_foreign_schema()
        with self._connect() as db:
            db.executescript(_SCHEMA)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _refuse_foreign_schema(self):
        """Checked through a read-only connection, before anything (even a journal-mode change, which
        rewrites the file header) can touch a database this version does not own."""
        uri = "file:" + urllib.parse.quote(self.db_path.replace("\\", "/"), safe="/:") + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            db.close()
        if version == 0 and tables & {"items", "archives", "categories"}:
            raise LegacyDatabaseError(f"{self.db_path} was written by the previous version")
        if version > SCHEMA_VERSION:
            raise LegacyDatabaseError(f"{self.db_path} has a newer schema ({version})")

    @contextmanager
    def _connect(self):
        # Autocommit mode: transactions are opened explicitly (BEGIN IMMEDIATE) and nowhere else.
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA journal_mode = WAL")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self._write_lock, self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _one(self, sql, args=()):
        with self._connect() as db:
            return db.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        with self._connect() as db:
            return db.execute(sql, args).fetchall()

    # --- categories ----------------------------------------------------------------------------------

    def add_category(self, name, description=None, color=None, max_archive_size=DEFAULT_MAX_ARCHIVE_SIZE,
                     folder_name_for=None):
        """Returns the new id, or None if the name exists or is invalid. The folder name is fixed here, once,
        and made unique (two names that sanitize alike no longer share a folder)."""
        if not isinstance(name, str) or not name.strip() or not _non_negative_int(max_archive_size):
            return None
        base = (folder_name_for or (lambda n: n))(name)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM categories WHERE name = ?", (name,)).fetchone():
                return None
            folder, n = base, 1
            while db.execute("SELECT 1 FROM categories WHERE folder_name = ?", (folder,)).fetchone():
                n += 1
                folder = f"{base}_{n}"
            cur = db.execute(
                "INSERT INTO categories (name, description, color, max_archive_size, folder_name) VALUES (?, ?, ?, ?, ?)",
                (name, description, color, int(max_archive_size), folder))
            return cur.lastrowid

    def category_by_name(self, name):
        return self._one(f"SELECT {_CATEGORY_COLUMNS} FROM categories WHERE name = ?", (name,))

    def category_by_id(self, category_id):
        return self._one(f"SELECT {_CATEGORY_COLUMNS} FROM categories WHERE id = ?", (category_id,))

    def list_categories(self):
        return self._all(f"SELECT {_CATEGORY_COLUMNS} FROM categories ORDER BY name")

    def update_category(self, category_id, name, description=None, color=None, max_archive_size=DEFAULT_MAX_ARCHIVE_SIZE):
        if not isinstance(name, str) or not name.strip() or not _non_negative_int(max_archive_size):
            return False
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
                return False
            if db.execute("SELECT 1 FROM categories WHERE name = ? AND id <> ?", (name, category_id)).fetchone():
                return False
            db.execute("UPDATE categories SET name = ?, description = ?, color = ?, max_archive_size = ? WHERE id = ?",
                       (name, description, color, int(max_archive_size), category_id))
            return True

    def delete_category(self, category_id):
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
                return "NOT_FOUND"
            if db.execute("SELECT COUNT(*) FROM archives WHERE category_id = ?", (category_id,)).fetchone()[0]:
                return "NOT_EMPTY"
            db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
            return "DELETED"

    # --- archives ------------------------------------------------------------------------------------

    def add_archive(self, category_id, zip_path):
        with self.transaction() as db:
            existing = db.execute("SELECT id FROM archives WHERE file_path = ?", (_norm(zip_path),)).fetchone()
            if existing:
                return existing[0]
            cur = db.execute("INSERT INTO archives (category_id, file_name, file_path, members_digest) VALUES (?, ?, ?, ?)",
                             (category_id, os.path.basename(zip_path), _norm(zip_path), EMPTY_DIGEST))
            return cur.lastrowid

    def archive_by_id(self, archive_id):
        return self._one(f"SELECT {_ARCHIVE_COLUMNS} FROM archives WHERE id = ?", (archive_id,))

    def archive_state(self, archive_id):
        row = self._one("SELECT state, members_digest, dead_bytes, zip_hash FROM archives WHERE id = ?", (archive_id,))
        return None if row is None else {"state": row[0], "members_digest": row[1], "dead_bytes": row[2], "zip_hash": row[3]}

    def archives_of_category(self, category_id):
        return self._all(f"SELECT {_ARCHIVE_COLUMNS} FROM archives WHERE category_id = ? ORDER BY file_name", (category_id,))

    def list_archives(self):
        return self._all(
            "SELECT archives.id, categories.name, archives.file_name, archives.file_path, archives.current_size, "
            "archives.item_count, archives.integrity_status, archives.last_verified_at FROM archives "
            "JOIN categories ON archives.category_id = categories.id ORDER BY categories.name, archives.file_name")

    def open_archives(self, category_id):
        return self._all("SELECT id, file_path, current_size FROM archives WHERE category_id = ? AND state = 'open' "
                         "ORDER BY file_name", (category_id,))

    def set_archive_status(self, archive_id, status):
        if status not in ARCHIVE_STATUSES:
            return False
        with self.transaction() as db:
            db.execute("UPDATE archives SET integrity_status = ?, last_verified_at = CURRENT_TIMESTAMP WHERE id = ?",
                       (status, archive_id))
        return True

    def set_archive_file_facts(self, archive_id, current_size, manifest_path):
        with self.transaction() as db:
            db.execute("UPDATE archives SET current_size = ?, manifest_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                       (current_size, _norm(manifest_path), archive_id))

    def seal_archive(self, archive_id, file_sha256):
        with self.transaction() as db:
            db.execute("UPDATE archives SET state = 'sealed', zip_hash = ? WHERE id = ?", (file_sha256, archive_id))

    def mark_archive_damaged(self, archive_id):
        with self.transaction() as db:
            db.execute("UPDATE archives SET state = 'damaged', integrity_status = 'CORRUPTED' WHERE id = ?", (archive_id,))

    # --- items and members ---------------------------------------------------------------------------

    @staticmethod
    def new_uid():
        return uuid.uuid4().hex[:12]

    def add_item(self, archive_id, item, members):
        """Writes one item and all its members in a single transaction and refreshes the archive digest.
        `item` is a dict of item columns; `members` a list of member dicts. Returns the item id."""
        with self.transaction() as db:
            cur = db.execute(
                "INSERT INTO items (archive_id, file_type, file_name, original_path, path_in_archive, original_size, "
                "compressed_size, original_hash, archive_hash, status, workspace_path, strategy, uid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ARCHIVED', NULL, ?, ?)",
                (archive_id, item["file_type"], item["file_name"], _norm(item["original_path"]), item["path_in_archive"],
                 item["original_size"], item["compressed_size"], item["original_hash"], item["archive_hash"],
                 item.get("strategy"), item["uid"]))
            item_id = cur.lastrowid
            self._insert_members(db, item_id, archive_id, members)
            self._refresh_archive(db, archive_id)
            return item_id

    def replace_item_content(self, item_id, item, members, new_archive_id=None, kept=()):
        """Checkin of changed content: the old members become dead (their bytes stay in the ZIP until it is
        compacted), the new ones are added - in `new_archive_id` when the item moves to another archive (its
        old one is sealed or full) - and the item row is updated, all in one transaction. `kept` names
        (path_in_archive) old members whose content did not change: they stay live as part of the new
        content - only possible while the item stays in its archive."""
        kept = set(kept)
        with self.transaction() as db:
            old_archive_id = db.execute("SELECT archive_id FROM items WHERE id = ?", (item_id,)).fetchone()[0]
            archive_id = new_archive_id or old_archive_id
            if kept and archive_id != old_archive_id:
                raise ValueError("kept members cannot move to another archive")
            old = db.execute("SELECT id, path_in_archive, stored_size FROM members WHERE item_id = ? AND live = 1",
                             (item_id,)).fetchall()
            if not kept <= {path for _id, path, _size in old}:
                raise ValueError("a kept member is not a live member of this item")
            gone = [(member_id, size) for member_id, path, size in old if path not in kept]
            db.executemany("UPDATE members SET live = 0 WHERE id = ?", [(member_id,) for member_id, _size in gone])
            db.execute("UPDATE archives SET dead_bytes = dead_bytes + ? WHERE id = ?",
                       (sum(size for _id, size in gone), old_archive_id))
            db.execute(
                "UPDATE items SET archive_id = ?, path_in_archive = ?, original_size = ?, compressed_size = ?, "
                "original_hash = ?, archive_hash = ?, strategy = ?, status = 'ARCHIVED', workspace_path = NULL, "
                "checked_in_at = CURRENT_TIMESTAMP WHERE id = ?",
                (archive_id, item["path_in_archive"], item["original_size"], item["compressed_size"],
                 item["original_hash"], item["archive_hash"], item.get("strategy"), item_id))
            self._insert_members(db, item_id, archive_id, members)
            self._refresh_archive(db, archive_id)
            if old_archive_id != archive_id:
                self._refresh_archive(db, old_archive_id)

    def update_member_facts(self, archive_id, facts):
        """After compaction: new stored sizes/CRCs keyed by path_in_archive (live members only)."""
        with self.transaction() as db:
            for path, f in facts.items():
                db.execute("UPDATE members SET stored_size = ?, crc32 = ? WHERE archive_id = ? AND path_in_archive = ? "
                           "AND live = 1", (f["stored_size"], f["crc32"], archive_id, path))
            self._refresh_archive(db, archive_id)

    def _insert_members(self, db, item_id, archive_id, members):
        for m in members:
            db.execute(
                "INSERT INTO members (item_id, archive_id, path_in_archive, relative_path, kind, method, method_version, "
                "original_size, stored_size, original_sha256, stored_sha256, crc32) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, archive_id, m["path_in_archive"], m["relative_path"], m["kind"], m["method"],
                 m.get("method_version", 0), m["original_size"], m["stored_size"], m["original_sha256"],
                 m["stored_sha256"], m["crc32"]))

    def _refresh_archive(self, db, archive_id):
        rows = db.execute(
            "SELECT path_in_archive, kind, method, method_version, original_size, stored_size, original_sha256, "
            "stored_sha256 FROM members WHERE archive_id = ? AND live = 1 ORDER BY path_in_archive", (archive_id,)).fetchall()
        count = db.execute("SELECT COUNT(*) FROM items WHERE archive_id = ?", (archive_id,)).fetchone()[0]
        db.execute("UPDATE archives SET members_digest = ?, item_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                   (members_digest(rows), count, archive_id))

    def members_of_item(self, item_id, live_only=True):
        cond = "AND live = 1" if live_only else ""
        return [dict(zip(_MEMBER_KEYS, r)) for r in self._all(
            f"SELECT {', '.join(_MEMBER_KEYS)} FROM members WHERE item_id = ? {cond} ORDER BY path_in_archive", (item_id,))]

    def members_of_archive(self, archive_id, live_only=True):
        cond = "AND live = 1" if live_only else ""
        return [dict(zip(_MEMBER_KEYS, r)) for r in self._all(
            f"SELECT {', '.join(_MEMBER_KEYS)} FROM members WHERE archive_id = ? {cond} ORDER BY path_in_archive", (archive_id,))]

    def forget_dead_members(self, archive_id):
        with self.transaction() as db:
            db.execute("DELETE FROM members WHERE archive_id = ? AND live = 0", (archive_id,))
            db.execute("UPDATE archives SET dead_bytes = 0 WHERE id = ?", (archive_id,))

    def item_by_id(self, item_id):
        return self._one(f"SELECT {_ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,))

    def item_uid(self, item_id):
        row = self._one("SELECT uid FROM items WHERE id = ?", (item_id,))
        return row[0] if row else None

    def archive_path_of_item(self, item_id):
        row = self._one("SELECT archives.file_path FROM items JOIN archives ON items.archive_id = archives.id WHERE items.id = ?",
                        (item_id,))
        return row[0] if row else None

    def find_item_by_hash(self, original_hash):
        return self._one(
            "SELECT items.id, items.file_name, items.file_type, items.path_in_archive, items.original_path, archives.id, "
            "archives.file_name, archives.file_path, categories.name, items.status FROM items "
            "JOIN archives ON items.archive_id = archives.id JOIN categories ON archives.category_id = categories.id "
            "WHERE items.original_hash = ? ORDER BY items.id LIMIT 1", (original_hash,))

    def search_items(self, text, category_name=None, extension=None):
        query = ("SELECT items.id, items.file_name, items.file_type, items.path_in_archive, items.original_path, "
                 "items.original_size, items.compressed_size, items.status, categories.name, archives.file_name, "
                 "archives.file_path FROM items JOIN archives ON items.archive_id = archives.id "
                 "JOIN categories ON archives.category_id = categories.id WHERE items.file_name LIKE ? ESCAPE '\\'")
        params = [f"%{_escape_like(text or '')}%"]
        if category_name:
            query += " AND categories.name = ?"
            params.append(category_name)
        if extension:
            query += " AND items.file_name LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(extension)}")
        return self._all(query + " ORDER BY items.id", tuple(params))

    def set_item_status(self, item_id, status, workspace_path=None):
        if status not in ITEM_STATUSES:
            return False
        with self.transaction() as db:
            cur = db.execute(
                "UPDATE items SET status = ?, workspace_path = ?, "
                "checked_out_at = CASE WHEN ? = 'CHECKED_OUT' THEN CURRENT_TIMESTAMP ELSE checked_out_at END, "
                "checked_in_at = CASE WHEN ? = 'ARCHIVED' THEN CURRENT_TIMESTAMP ELSE checked_in_at END WHERE id = ?",
                (status, _norm(workspace_path), status, status, item_id))
            return cur.rowcount == 1

    def checked_out_items(self):
        return self._all("SELECT id, file_name, file_type, path_in_archive, workspace_path FROM items "
                         "WHERE status = 'CHECKED_OUT' ORDER BY id")

    def dashboard_summary(self):
        with self._connect() as db:
            categories = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            original, compressed = db.execute(
                "SELECT COALESCE(SUM(original_size), 0), COALESCE(SUM(compressed_size), 0) FROM items").fetchone()
            checked_out = db.execute("SELECT COUNT(*) FROM items WHERE status = 'CHECKED_OUT'").fetchone()[0]
            integrity = dict(db.execute("SELECT integrity_status, COUNT(*) FROM archives GROUP BY integrity_status").fetchall())
        return {
            "category_count": categories, "item_count": items, "total_original_size": original,
            "total_compressed_size": compressed, "total_saved": original - compressed, "checked_out_count": checked_out,
            "integrity_counts": {s: integrity.get(s, 0) for s in ARCHIVE_STATUSES},
        }


_MEMBER_KEYS = ("id", "item_id", "archive_id", "path_in_archive", "relative_path", "kind", "method", "method_version",
                "original_size", "stored_size", "original_sha256", "stored_sha256", "crc32", "live")


def members_digest(rows):
    """SHA-256 over the canonical, length-prefixed encoding of member rows (sorted by path)."""
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: r[0]):
        for value in row:
            data = str(value).encode("utf-8")
            h.update(len(data).to_bytes(8, "big"))
            h.update(data)
    return h.hexdigest()


def _non_negative_int(value):
    try:
        return int(value) >= 0 and float(value) == int(value)
    except (TypeError, ValueError):
        return False
