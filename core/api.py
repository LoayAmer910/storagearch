"""The public API of the core - the functions the GUI calls, with the same names, arguments, return values
and row shapes as the old `main` module, so the GUI only changes its imports.

Every archive / checkin runs inside one OperationScope (all temporary files and worker processes are gone
when it returns), decides each file by content through the pipeline, writes crash-safe through the store,
and records the result in the catalog only after the archive was read back. Cloud events are handed to
core.events after the local result is final and can never change it.
"""
import concurrent.futures
import contextlib
import logging
import os
import re
import shutil
import sqlite3
import threading
import time

try:
    import msvcrt
except ImportError:  # not Windows: no lock between instances
    msvcrt = None

from core import catalog as catalog_module
from core import config, events, folders, paths, router, store
from core.catalog import EMPTY_DIGEST, Catalog, LegacyDatabaseError
from core.executor import Executor
from core.paths import CloudPlaceholderError, ExtractionRefused, InvalidPathError, SourceInUseError
from core.pipeline import Pipeline, scan as scan_file
from core.scope import OperationCancelled, OperationScope, sweep_stale
from core.store import PlannedMember

log = logging.getLogger("storagearch")

BASELINE_NAME = "baseline_storagearch_zip"
DEFAULT_MAX_ARCHIVE_SIZE = catalog_module.DEFAULT_MAX_ARCHIVE_SIZE
COMPACT_DEAD_RATIO = 0.25
_SPACE_MARGIN = 16 * 1024 * 1024
_LOG_LIMIT = 1024 * 1024
_SOURCE_ACTIONS = ("keep", "recycle", "delete")
_SAVE_ROUNDS = 3                    # a source that keeps changing is saved this often before it is left alone
_write_lock = threading.RLock()     # archive files and catalog writes: one writer at a time
_catalog = None
_catalog_lock = threading.Lock()

__all__ = [
    "initialize", "create_category", "update_category", "delete_category", "get_category", "list_categories",
    "list_archives", "get_item", "get_archive", "search", "find_duplicate", "dashboard_summary",
    "archive_file", "archive_file_result", "archive_folder", "archive_folder_result", "checkout", "checkout_result",
    "cancel_current_operation", "operation_progress", "OperationCancelled",
    "checkin", "checkin_result",
    "items_pending_return", "handle_pending_checkouts_on_exit", "verify_archive_by_id", "verify_category",
    "verify_all", "is_protected_path", "get_storage_locations", "set_storage_location", "reset_workspace_location",
    "calculate_file_hash", "calculate_folder_hash", "validate_source_path", "data_root", "shutdown_cloud",
    "CloudPlaceholderError", "InvalidPathError", "SourceInUseError", "LegacyDatabaseError", "AlreadyRunningError",
    "cloud_available", "cloud_is_registered", "cloud_register", "cloud_disconnect", "cloud_telemetry_enabled",
    "cloud_set_telemetry_enabled", "cloud_submit_feedback", "cloud_check_for_updates", "cloud_report_crash",
]

# the cloud facade, unchanged for the GUI
cloud_available = events.cloud_available
cloud_is_registered = events.cloud_is_registered
cloud_register = events.cloud_register
cloud_disconnect = events.cloud_disconnect
cloud_telemetry_enabled = events.cloud_telemetry_enabled
cloud_set_telemetry_enabled = events.cloud_set_telemetry_enabled
cloud_submit_feedback = events.cloud_submit_feedback
cloud_check_for_updates = events.cloud_check_for_updates
cloud_report_crash = events.cloud_report_crash


# --- setup ---------------------------------------------------------------------------------------------

def _follow_data_folder():
    """Before anything reads a path from the catalog: the data folder may have been moved since it was written."""
    default_workspace = config.workspace_dir() if config.current()["workspace_root"] is None else None
    try:
        moved = _catalog.follow_data_folder(config.archives_dir(), default_workspace)
    except sqlite3.Error as exc:
        log.error("the paths in the catalog could not be brought up to date: %s", exc)
        return
    if moved:
        log.warning("the data folder was moved or copied; %d path(s) in the catalog now point to %s", moved, config.data_root())


class AlreadyRunningError(OSError):
    """Another StorageArch is working on this data folder."""


_instance_lock = None               # (lock file path, open file): held for as long as this process uses the folder


def _lock_data_folder(root):
    """One StorageArch per data folder. The write lock and the repair of interrupted writes at startup only
    know this process: a second one would take a write that is still running for one that was interrupted
    and undo it. The lock is a locked byte of a file, so Windows frees it by itself when the process ends,
    however it ends. Raises AlreadyRunningError when another process holds it."""
    global _instance_lock
    path = os.path.join(root, "storagearch.lock")
    if msvcrt is None or (_instance_lock is not None and os.path.normcase(_instance_lock[0]) == os.path.normcase(path)):
        return
    handle = open(path, "a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise AlreadyRunningError(f"StorageArch is already running on the data folder {root}") from None
    previous, _instance_lock = _instance_lock, (path, handle)
    if previous is not None:
        previous[1].close()


def initialize():
    """Loads the config, creates the data folders, takes the data folder for this process, opens the catalog,
    repairs interrupted writes and removes temporary folders of processes that died. Returns the database path."""
    global _catalog
    config.load()
    for folder in (config.archives_dir(), config.workspace_dir(), config.temp_dir(), config.log_dir()):
        os.makedirs(folder, exist_ok=True)
    _lock_data_folder(config.data_root())
    with _catalog_lock:
        _catalog = Catalog(config.db_path())
    _follow_data_folder()
    repaired = store.recover(config.archives_dir(), recorded=_catalog.holds_members)
    if repaired:
        log.warning("repaired interrupted writes: %s", repaired)
    for archive_id, zip_path in _catalog.archives_without_content():
        if not os.path.exists(zip_path):  # what a first write that never completed left behind
            _catalog.delete_archive_without_content(archive_id)
    sweep_stale(config.temp_dir())
    _log_to(config.log_dir())
    events.start()
    return config.db_path()


class _LogFile(logging.Handler):
    """Writes warnings and errors to a file in the data folder. The file is opened for each line and closed
    again - problems are rare - so nothing ever holds it and the data folder can be moved or removed. A full
    file starts again and one old copy is kept. Writing the log never fails an operation."""

    def __init__(self, folder):
        super().__init__(logging.WARNING)
        self.path = os.path.join(folder, "storagearch.log")
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record):
        try:
            line = self.format(record) + "\n"
            if os.path.exists(self.path) and os.path.getsize(self.path) + len(line) > _LOG_LIMIT:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                f.write(line)
        except Exception:  # noqa: BLE001 - a log that cannot be written must not break anything
            pass


def _log_to(folder):
    for handler in [h for h in log.handlers if isinstance(h, _LogFile)]:
        log.removeHandler(handler)
    log.addHandler(_LogFile(folder))


def _cat():
    if _catalog is None or _catalog.db_path != os.path.abspath(config.db_path()):
        initialize()
    return _catalog


def data_root():
    return config.data_root()


def shutdown_cloud(timeout=1.0):
    events.shutdown(timeout)


# --- categories and lookups ----------------------------------------------------------------------------

_MAX_FOLDER_NAME = 60


def _folder_name_for(name):
    """The category's folder: a safe, short name (a 500-character category name used to become a folder name
    Windows cannot create - found by the GUI journey test). The catalog adds a suffix when it is taken."""
    return paths.bare_name(paths.bare_name(name, fallback="category")[:_MAX_FOLDER_NAME], fallback="category")


def create_category(category_name, description=None, color=None, max_archive_size=DEFAULT_MAX_ARCHIVE_SIZE):
    if not isinstance(category_name, str) or not category_name.strip():
        return False
    cat = _cat()
    cat.add_category(category_name, description, color, max_archive_size, folder_name_for=_folder_name_for)
    row = cat.category_by_name(category_name)
    if row is None:
        return False
    os.makedirs(os.path.join(config.archives_dir(), row.folder_name), exist_ok=True)
    return True


def update_category(category_id, name, description=None, color=None, max_archive_size=DEFAULT_MAX_ARCHIVE_SIZE):
    return _cat().update_category(category_id, name, description, color, max_archive_size)


def delete_category(category_id):
    cat = _cat()
    row = cat.category_by_id(category_id)
    result = cat.delete_category(category_id)
    if result == "DELETED" and row is not None:
        try:
            os.rmdir(os.path.join(config.archives_dir(), row.folder_name))
        except OSError:
            pass
    return result


def get_category(category_id):
    return _cat().category_by_id(category_id)


def list_categories():
    return _cat().list_categories()


def list_archives():
    return _cat().list_archives()


def get_item(item_id):
    return _cat().item_by_id(item_id)


def get_archive(archive_id):
    return _cat().archive_by_id(archive_id)


def search(search_text, category_name=None, extension=None):
    return _cat().search_items(search_text, category_name, extension)


def dashboard_summary():
    return _cat().dashboard_summary()


def items_pending_return():
    return _cat().checked_out_items()


def handle_pending_checkouts_on_exit():
    return items_pending_return()


def find_duplicate(content_hash):
    row = _cat().find_item_by_hash(content_hash)
    if row is None:
        return None
    return _duplicate_info(row, _verify_item(row.id))


def _duplicate_info(row, status):
    return {"item_name": row.file_name, "category": row.category_name, "archive": row.archive_path,
            "path_in_archive": row.path_in_archive, "status": status}


def _verify_item(item_id):
    """The item's own members, plus the checks on the whole file that cost nothing (size, unknown entries)."""
    cat = _cat()
    archive_id = cat.item_by_id(item_id)[1]
    row = cat.archive_by_id(archive_id)
    status = _verify_archive_row(archive_id, row, members=cat.members_of_item(item_id))
    cat.set_archive_status(archive_id, status)
    return status


def _verify_archive_row(archive_id, row, members=None):
    cat = _cat()
    state = cat.archive_state(archive_id)
    sealed = state["zip_hash"] if state["state"] == "sealed" else None
    known = [m["path_in_archive"] for m in cat.members_of_archive(archive_id, live_only=False)]
    status = store.verify_archive(row.file_path, cat.members_of_archive(archive_id) if members is None else members,
                                  sealed, expected_size=row.current_size, known_names=known)
    if status == "CHANGED" and _repair_open_archive(archive_id):
        return _verify_archive_row(archive_id, cat.archive_by_id(archive_id), members)
    return status


def _repair_open_archive(archive_id):
    """Makes good what an interrupted write or compaction left in an open archive: entries the catalog never
    recorded, or a recorded size that fell behind. Only when the file is a whole ZIP with nothing after it
    and every recorded member in it is intact - anything else stays reported as it is. A sealed archive is
    never touched. Returns whether something was repaired."""
    cat = _cat()
    with _write_lock:
        row, state = cat.archive_by_id(archive_id), cat.archive_state(archive_id)
        if row is None or state["state"] != "open" or not os.path.exists(row.file_path):
            return False
        live = cat.members_of_archive(archive_id)
        if not store.ends_cleanly(row.file_path) or store.verify_archive(row.file_path, live) != "VERIFIED":
            return False
        known = {m["path_in_archive"] for m in cat.members_of_archive(archive_id, live_only=False)}
        unknown = sorted(store.entry_names(row.file_path) - known)
        if not unknown and os.path.getsize(row.file_path) == row.current_size:
            return False
        category = cat.category_by_id(row.category_id)
        try:
            if unknown:
                log.warning("%s held %d entries the catalog never recorded (an interrupted write); removed: %s",
                            row.file_path, len(unknown), unknown[:5])
                _compact(archive_id, category)
            else:
                log.warning("the recorded size of %s had fallen behind the file; corrected", row.file_path)
                _refresh_old_archive(archive_id, category)
        except (store.StoreError, OSError, sqlite3.Error) as exc:
            log.error("%s could not be repaired: %s", row.file_path, exc)
            return False
        return True


# --- paths and hashes the GUI checks before archiving -------------------------------------------------

def validate_source_path(path):
    return paths.validate_source_path(path)


def is_protected_path(path):
    return paths.is_protected(path)


def _is_protected_source(path):
    return paths.is_protected(path, extra_roots=(config.data_root(), config.workspace_dir()))


def calculate_file_hash(file_path):
    if not os.path.isfile(file_path):
        return None
    with _operation():
        _raise_if_cancelled()
        try:
            digest = store.sha256_file(file_path)
        except OSError as exc:
            raise _read_error(file_path, exc) from exc
        _raise_if_cancelled()  # a cancel that came while the file was read must not be lost with this operation
        return digest


def calculate_folder_hash(folder_path):
    if not os.path.isdir(folder_path):
        return None
    with _operation():
        return folders.folder_hash(folder_path, calculate_file_hash)


def _read_error(path, exc):
    if paths.is_cloud_placeholder(path):
        return CloudPlaceholderError(f"'{path}' is a OneDrive/cloud file that has not been downloaded to this device")
    ok, reason = paths.validate_source_path(path)
    if ok and isinstance(exc, PermissionError):  # locked by another program (sharing violation) or no read access
        return SourceInUseError(path, exc.strerror or exc)
    return InvalidPathError(f"Cannot read '{path}': {reason if not ok else (exc.strerror or exc)}")


# --- storage locations ---------------------------------------------------------------------------------

def get_storage_locations():
    return config.load()


def set_storage_location(data_root=None, workspace_root=None):
    if workspace_root is not None and (paths.is_protected(workspace_root) or items_pending_return()):
        return False
    if data_root is not None and paths.is_protected(data_root):
        return False
    previous = config.current()
    try:
        config.save(data_root=data_root, workspace_root=workspace_root)
        initialize()
    except (LegacyDatabaseError, OSError, sqlite3.Error) as exc:  # a location the app cannot use: keep the old one
        log.warning("cannot switch the storage location: %s", exc)
        config.save(data_root=previous["data_root"], workspace_root=previous["workspace_root"] or "")
        initialize()
        return False
    return True


def reset_workspace_location():
    if items_pending_return():
        return False
    config.save(workspace_root="")
    initialize()
    return True


# --- archiving -----------------------------------------------------------------------------------------

def _fail(reason, message):
    return {"success": False, "reason": reason, "message": message}


# --- cancelling and progress ---------------------------------------------------------------------------
#
# An archive, a check-out, a check-in and the hashing before an archive can be cancelled from another thread
# (the window's Cancel button). The rule: a cancel that arrives before the item is recorded in the catalog
# leaves nothing behind - no item, no archive entry, no temporary file - and the source is not touched. From
# the moment the item is recorded the save stands; a cancel then only keeps the source of an archive from
# being removed, and a check-in is simply finished.

_op_lock = threading.Lock()
_op_cancel = threading.Event()
_op = {"running": 0, "final": False, "progress": None, "scopes": set()}
_CANCELLED_MESSAGE = "The operation was cancelled. Nothing was saved."
_KEPT_AFTER_CANCEL = "\nThe source was kept because the operation was cancelled."


@contextlib.contextmanager
def _operation():
    """Marks one public operation as running, for cancel_current_operation() and operation_progress()."""
    with _op_lock:
        if _op["running"] == 0:
            _op_cancel.clear()
            _op["final"] = False
        _op["running"] += 1
    try:
        yield
    finally:
        with _op_lock:
            _op["running"] -= 1
            if _op["running"] == 0:
                _op_cancel.clear()
                _op.update(final=False, progress=None)


@contextlib.contextmanager
def _scope(kind):
    """An OperationScope that a cancel reaches: its worker processes are stopped at once."""
    with OperationScope(kind, temp_root=config.temp_dir()) as scope:
        with _op_lock:
            _op["scopes"].add(scope)
            if _op_cancel.is_set() and not _op["final"]:
                scope.cancel()
        try:
            yield scope
        finally:
            with _op_lock:
                _op["scopes"].discard(scope)


def cancel_current_operation():
    """Asks the running operation to stop. False when nothing is running or it is too late to stop it."""
    with _op_lock:
        if _op["running"] == 0 or _op["final"]:
            return False
        _op_cancel.set()
        for scope in _op["scopes"]:
            scope.cancel()
    return True


def operation_progress():
    """{"phase", "done", "total"} of the running operation, or None when nothing is running or counted."""
    with _op_lock:
        return dict(_op["progress"]) if _op["running"] and _op["progress"] else None


def _cancelled():
    return _op_cancel.is_set() and not _op["final"]


def _raise_if_cancelled():
    if _cancelled():
        raise OperationCancelled("cancelled")


def _too_late_to_cancel():
    """From here the operation is finished whatever happens: a cancel is refused."""
    with _op_lock:
        _op["final"] = True


def _phase(name, total):
    with _op_lock:
        _op["progress"] = {"phase": name, "done": 0, "total": total}


def _step():
    with _op_lock:
        if _op["progress"]:
            _op["progress"]["done"] += 1


def _guarded_operation(fn, *args):
    try:
        with _operation():
            return fn(*args)
    except OperationCancelled:
        return _fail("CANCELLED", _CANCELLED_MESSAGE)
    except store.SourceChanged as exc:
        return _fail("SOURCE_CHANGED", str(exc))
    except (store.StoreError, ExtractionRefused) as exc:
        log.error("write failed: %s", exc)
        return _fail("WRITE_FAILED", "The item could not be written to the archive")
    except sqlite3.Error as exc:
        log.error("database error: %s", exc)
        return _fail("DATABASE_ERROR", "A database error occurred while archiving")
    except PermissionError:
        return _fail("PERMISSION_ERROR", "Permission denied while archiving")
    except SourceInUseError as exc:
        return dict(_fail("SOURCE_IN_USE", str(exc)), path=exc.path)
    except InvalidPathError as exc:
        return _fail("INVALID_PATH", str(exc))
    except OSError as exc:
        log.error("filesystem error: %s", exc)
        return _fail("OS_ERROR", "A filesystem error occurred while archiving")
    except Exception as exc:  # noqa: BLE001 - the GUI must get a result, never a traceback
        log.exception("unexpected error")
        return _fail("UNEXPECTED_ERROR", f"An unexpected error occurred while archiving: {type(exc).__name__}")


def _check_request(path, category_name, source_action, want_dir):
    if not isinstance(path, str) or not path.strip():
        return _fail("INVALID_PATH", "Invalid folder path" if want_dir else "Invalid file path")
    if not isinstance(category_name, str) or not category_name.strip():
        return _fail("INVALID_CATEGORY", "Invalid category name")
    if source_action not in _SOURCE_ACTIONS:
        return _fail("INVALID_SOURCE_ACTION", "Invalid source action")
    if _is_protected_source(path):
        return _fail("PROTECTED_PATH", "This location is protected and cannot be archived")
    if not os.path.exists(path) and paths.too_long(os.path.abspath(path), folder=want_dir):
        return _fail("INVALID_PATH", f"Cannot archive '{path}': {paths.too_long(os.path.abspath(path), folder=want_dir)}. "
                                     "Move it to a shorter location and try again.")
    if want_dir and not os.path.isdir(path):
        return _fail("SOURCE_MISSING", "Folder does not exist")
    if not want_dir and not os.path.isfile(path):
        return _fail("SOURCE_MISSING", "File does not exist")
    return None


def _category_row(category_name):
    cat = _cat()
    row = cat.category_by_name(category_name)
    if row is None:
        create_category(category_name)
        row = cat.category_by_name(category_name)
    return row


_SEQ = re.compile(r"_(\d+)\.zip$", re.IGNORECASE)


def _choose_archive(category, incoming_bytes):
    """An open archive of the category with room for the incoming bytes, else a new one."""
    cat = _cat()
    category_id, max_size, folder = category.id, category.max_archive_size, category.folder_name
    for archive_id, zip_path, _size in cat.open_archives(category_id):
        actual = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
        if actual == 0 or actual + incoming_bytes <= max_size:
            return archive_id, zip_path
        _seal(archive_id, zip_path, category)  # full: its content is final from now on
    folder_path = os.path.join(config.archives_dir(), folder)
    os.makedirs(folder_path, exist_ok=True)
    used = {int(m.group(1)) for a in cat.archives_of_category(category_id) if (m := _SEQ.search(a.file_name))}
    used |= {int(m.group(1)) for f in os.listdir(folder_path) if (m := _SEQ.search(f))}
    seq = max(used, default=0) + 1
    zip_path = os.path.join(folder_path, f"{folder}_{seq:03d}.zip")
    return cat.add_archive(category_id, zip_path), zip_path


def _free_at(path):
    """Free bytes on the drive that holds `path`; a path that does not exist yet is on the drive of its
    nearest existing folder."""
    path = os.path.abspath(path)
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return shutil.disk_usage(path).free


def _enough_space(needed, where=None, temp=None):
    """Room for `needed` bytes on the drive of `where` (default: the archives) and for `temp` bytes (default:
    as many) on the drive of the temporary folder, where compressed and restored files wait. A drive that
    does not answer has no room."""
    try:
        return (_free_at(where or config.archives_dir()) >= needed + _SPACE_MARGIN
                and _free_at(config.temp_dir()) >= (needed if temp is None else temp) + _SPACE_MARGIN)
    except OSError:
        return False


def _plain_size(scanned):
    """What the scanned files take as a plain zip - known from the scan, before any compression: the upper
    limit of what they will take in the archive, since a smarter method is kept only when it is smaller."""
    return sum(s[2] for s in scanned)


def _seal(archive_id, zip_path, category):
    cat = _cat()
    file_sha = store.sha256_file(zip_path)
    cat.seal_archive(archive_id, file_sha)
    state = cat.archive_state(archive_id)
    manifest = store.write_manifest(zip_path, category.name, cat.members_of_archive(archive_id), state["members_digest"],
                                    "sealed", file_sha)
    cat.set_archive_file_facts(archive_id, os.path.getsize(zip_path), manifest)


@contextlib.contextmanager
def _recorded_write(archive_id, zip_path):
    """The members written inside the block and their catalog rows count together or not at all: on any
    error the archive file is put back as it was (and after a crash, at the next start). A first write that
    fails leaves no archive row behind."""
    try:
        with store.deferred(zip_path):
            yield
    except BaseException:
        if not os.path.exists(zip_path):
            try:
                _cat().delete_archive_without_content(archive_id)
            except sqlite3.Error as exc:
                log.warning("the empty archive row %s could not be removed: %s", archive_id, exc)
        raise


def _after_recorded(archive_id, zip_path, category, old_archive_id=None):
    """What follows a recorded write: the listing and status of the archive, sealing a full archive, and - for
    a new version of an item - the listing and compaction of the old archive. The item is saved by now, so a
    failure here is logged and made good at the next write, verification or start; it never reports a saved
    item as failed."""
    try:
        _after_write(archive_id, zip_path, category)
        if old_archive_id is not None:
            if archive_id != old_archive_id:
                _refresh_old_archive(old_archive_id, category)
            _compact_if_worth_it(old_archive_id, category)
    except Exception:  # noqa: BLE001
        log.exception("a step after saving to %s failed; the item itself is saved", zip_path)


def _after_write(archive_id, zip_path, category):
    """Updates the archive's facts and manifest; seals it once it reached the category's size limit."""
    cat = _cat()
    size = os.path.getsize(zip_path)
    members = cat.members_of_archive(archive_id)
    state = cat.archive_state(archive_id)
    cat.set_archive_status(archive_id, "VERIFIED")
    if state["state"] == "open" and size >= category.max_archive_size > 0:
        _seal(archive_id, zip_path, category)
        return
    manifest = store.write_manifest(zip_path, category.name, members, state["members_digest"], state["state"], state["zip_hash"])
    cat.set_archive_file_facts(archive_id, size, manifest)


def _planned_from_decision(decision, source, member_path, relative_path):
    return PlannedMember(member_path, relative_path, "file", decision.method, decision.strategy_version, source,
                         decision.stored_path, decision.info.sha256, decision.info.size, _mtime(source),
                         decision.stored_bytes)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _new_pipeline(scope):
    disabled = events.disabled_strategies()
    route = (lambda info: [s for s in router.route(info) if s.name not in disabled]) if disabled else router.route
    return Pipeline(Executor(scope), route)


def _state_of(path):
    """What a quick look at a file or a folder shows: every entry in it with its kind, size and modification
    time. It is taken before the content is read, so anything that changes afterwards makes it differ."""
    def facts(p):
        st = os.stat(p)
        return st.st_size, st.st_mtime_ns
    if not os.path.isdir(path):
        return {"": ("file", *facts(path))}
    entries, skipped = folders.scan(path)
    state = {e.rel_path: (e.kind, *(facts(e.full_path) if e.kind == "file" else ())) for e in entries}
    state.update({s.rel_path: (s.reason,) for s in skipped})
    return state


def _still(path, state):
    try:
        return _state_of(path) == state
    except (OSError, InvalidPathError):
        return False


def _remove_if_still(path, state, recycle=False):
    """Removes a file or a folder only while it still is what `state` describes. Returns "removed", "changed"
    or "failed". A permanent delete first moves it aside under another name - from then on nothing can be
    added to it by its path - and looks at it once more; whatever is not deleted gets its name back."""
    if not _still(path, state):
        return "changed"
    aside = None
    try:
        if recycle:
            from send2trash import send2trash
            send2trash(path)
            return "removed"
        aside = _move_aside(path)
        if aside and not _still(aside, state):
            return "changed"
        target = aside or path
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        return "removed"
    except (OSError, ImportError) as exc:
        log.warning("%s could not be removed: %s", path, exc)
        return "failed"
    finally:
        if aside and os.path.lexists(aside):
            try:
                os.rename(aside, path)
            except OSError as exc:
                log.error("%s could not get its name back and was left as %s: %s", path, aside, exc)


def _move_aside(path):
    """The new path, or None when it cannot be moved (held open, name too long) - it is then removed in place."""
    aside = f"{os.path.normpath(path)}.del-{Catalog.new_uid()[:6]}"
    try:
        os.rename(path, aside)
    except OSError:
        return None
    return aside


def _remove_once_saved(item_id, path, state, recycle=False, strict=False):
    """`path` was saved as the item when it looked like `state`, and is now to be removed. Nothing is removed
    unless a fresh look shows exactly what was saved: what was added or changed meanwhile is saved first, and
    a source that still changes after _SAVE_ROUNDS saves stays where it is. Returns (outcome, stats) -
    "removed", "failed" (it could not be removed) or "kept", and the sizes of the last extra save, if any."""
    stats = None
    for attempt in range(_SAVE_ROUNDS):
        outcome = _remove_if_still(path, state, recycle)
        if outcome != "changed":
            return outcome, stats
        if attempt == _SAVE_ROUNDS - 1:
            break
        try:
            saved = _save_again(item_id, path, strict)
        except Exception:  # noqa: BLE001 - the item keeps the version saved last and the source stays
            log.exception("saving what changed in %s failed", path)
            saved = None
        if saved is None:
            break
        state, stats = saved[0], saved[1] or stats
    return "kept", stats


def _save_again(item_id, path, strict):
    """Saves what `path` holds now as the item's content. Returns (the state that was saved, its sizes - None
    when the item already holds this content), or None when it cannot be saved. `strict`: a folder with
    entries that are skipped (links, cloud files) is not saved, as when it is archived."""
    item = _cat().item_by_id(item_id)
    state = _state_of(path)
    if item.file_type == "folder":
        entries, skipped = folders.scan(path)
        if skipped and strict:
            return None
        scanned = _scan_many(entries)
        new_hash = _folder_digest(entries, scanned)
    else:
        entries, scanned = None, scan_file(path)
        new_hash = scanned[0].sha256
    if new_hash == item.original_hash:
        return state, None
    reason, stats = _store_new_version(item_id, path, entries, scanned, new_hash)
    return None if reason else (state, stats)


def _remove_source_unless_cancelled(result, item_id, path, state, action):
    """The item is saved. A cancel that came after that keeps the source where it is."""
    if not _cancelled():
        try:
            _remove_source(result, item_id, path, state, action)
            return
        except OperationCancelled:  # cancelled while a change made during the archive was being saved again
            pass
    result["message"] += _KEPT_AFTER_CANCEL


def _remove_source(result, item_id, path, state, action):
    """Applies the source action to a source that was archived, and adds to the result what came of it."""
    if _is_protected_source(path):
        outcome, stats = "failed", None
    else:
        outcome, stats = _remove_once_saved(item_id, path, state, recycle=action == "recycle", strict=True)
    if stats:
        result.update(original_size=stats["original"], compressed_size=stats["stored"],
                      savings=stats["original"] - stats["stored"])
        result["message"] += "\nChanges made to the source while it was being archived were saved too."
    if outcome == "failed":
        result["message"] += " (the source could not be removed)"
    elif outcome == "kept":
        result["message"] += "\nThe source was kept because it was still changing while it was being archived."


def _duplicate_result(row):
    status = _verify_item(row.id)
    if status == "VERIFIED":
        return {"success": True, "reason": "DUPLICATE", "message": "Duplicate content already exists - not saved again",
                "duplicate": _duplicate_info(row, status)}
    return _fail("DUPLICATE_UNTRUSTED", f"A matching record exists but its archive could not be verified "
                                         f"(status: {status}); archiving stopped for safety")


def archive_file_result(file_path, category_name, source_action="keep"):
    start = time.perf_counter()
    result = _check_request(file_path, category_name, source_action, want_dir=False) or \
        _guarded_operation(_archive_file, file_path, category_name, source_action)
    _report_archive(result, file_path, time.perf_counter() - start)
    return result


def archive_file(file_path, category_name, source_action="keep"):
    return archive_file_result(file_path, category_name, source_action)["success"]


def _archive_file(file_path, category_name, source_action):
    try:
        state = _state_of(file_path) if source_action != "keep" else None
        scanned = scan_file(file_path)
    except OSError as exc:
        raise _read_error(file_path, exc) from exc
    info = scanned[0]
    duplicate = _cat().find_item_by_hash(info.sha256)
    if duplicate is not None:
        return _duplicate_result(duplicate)
    category = _category_row(category_name)
    name = paths.bare_name(os.path.basename(file_path))
    if not _enough_space(_plain_size([scanned])):
        return _fail("INSUFFICIENT_SPACE", "Not enough free space to archive file")
    with _scope("archive_file") as scope:
        _phase("Compressing", 1)
        decision = _new_pipeline(scope).decide(file_path, scanned)
        if not _enough_space(decision.stored_size):
            return _fail("INSUFFICIENT_SPACE", "Not enough free space to archive file")
        uid = Catalog.new_uid()
        with _write_lock:
            archive_id, zip_path = _choose_archive(category, decision.stored_size)
            with _recorded_write(archive_id, zip_path):
                _raise_if_cancelled()  # the last moment a cancel leaves everything as it was
                records = store.write_members(zip_path, [_planned_from_decision(decision, file_path, f"{uid}/{name}", name)])
                item = {"file_type": "file", "file_name": name, "original_path": file_path, "path_in_archive": f"{uid}/{name}",
                        "original_size": info.size, "compressed_size": records[0]["stored_size"],
                        "original_hash": info.sha256, "archive_hash": records[0]["stored_sha256"],
                        "strategy": decision.method if decision.smart else None, "uid": uid}
                item_id = _cat().add_item(archive_id, item, records, archive_size=os.path.getsize(zip_path))
            _after_recorded(archive_id, zip_path, category)
    stored = records[0]["stored_size"]
    result = {"success": True, "reason": "OK", "message": "Archived and verified successfully",
              "original_size": info.size, "compressed_size": stored, "savings": info.size - stored,
              "smart_compression_used": decision.smart, "strategy": decision.method if decision.smart else BASELINE_NAME}
    if source_action != "keep":
        _remove_source_unless_cancelled(result, item_id, file_path, state, source_action)
    return result


def archive_folder_result(folder_path, category_name, source_action="keep"):
    start = time.perf_counter()
    result = _check_request(folder_path, category_name, source_action, want_dir=True) or \
        _guarded_operation(_archive_folder, folder_path, category_name, source_action)
    _report_archive(result, folder_path, time.perf_counter() - start, folder=True)
    return result


def archive_folder(folder_path, category_name, source_action="keep"):
    return archive_folder_result(folder_path, category_name, source_action)["success"]


def _scan_many(entries):
    files = [e for e in entries if e.kind == "file"]

    def one(e):
        _raise_if_cancelled()
        try:
            return e, scan_file(e.full_path)
        except OSError as exc:
            raise _read_error(e.full_path, exc) from exc
        finally:
            _step()
    _phase("Reading", len(files))
    workers = max(1, min(8, len(files)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as pool:
        return dict(pool.map(one, files))


def _decide_many(scope, scanned):
    pipeline = _new_pipeline(scope)
    workers = max(1, min(4 * (os.cpu_count() or 2), 32, len(scanned)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="decide") as pool:
        def one(e, s):
            try:
                return pipeline.decide(e.full_path, s)
            finally:
                _step()
        _phase("Compressing", len(scanned))
        futures = {e: pool.submit(one, e, s) for e, s in scanned.items()}
        return {e: f.result() for e, f in futures.items()}


def _folder_digest(entries, scanned):
    return folders.digest((e.rel_path, e.kind, scanned[e][0].sha256 if e.kind == "file" else EMPTY_DIGEST) for e in entries)


def _folder_plan(uid_prefix, root_name, entries, decisions, kept=None):
    """The members to write for a folder - all of them, or (at checkin) those not `kept` from its old version."""
    kept = kept or {}
    planned = [] if None in kept else [PlannedMember(f"{uid_prefix}/{root_name}/", root_name, "dir", "stored")]
    for e in entries:
        if e in kept:
            continue
        member = f"{uid_prefix}/{root_name}/{e.rel_path}"
        relative = f"{root_name}/{e.rel_path}"
        if e.kind == "dir":
            planned.append(PlannedMember(member + "/", relative, "dir", "stored"))
        else:
            planned.append(_planned_from_decision(decisions[e], e.full_path, member, relative))
    return planned


def _unchanged_members(old_members, root_name, entries, scanned):
    """The old members a checked-in folder can keep as they are - same relative path, same kind and, for a
    file, the same SHA-256 and size - keyed by entry (None for the folder itself). Their stored bytes are
    already in the archive, chosen for exactly this content, so they are neither compressed nor written again."""
    by_relative = {m["relative_path"]: m for m in old_members}
    kept = {}
    root = by_relative.get(root_name)
    if root is not None and root["kind"] == "dir":
        kept[None] = root
    for e in entries:
        old = by_relative.get(f"{root_name}/{e.rel_path}")
        if old is None or old["kind"] != e.kind:
            continue
        if e.kind == "dir" or (old["original_sha256"] == scanned[e][0].sha256 and old["original_size"] == scanned[e][0].size):
            kept[e] = old
    return kept


def _records_digest(records):
    return folders.digest((r["path_in_archive"], r["kind"], r["stored_sha256"]) for r in records)


def _archive_folder(folder_path, category_name, source_action):
    state = _state_of(folder_path) if source_action != "keep" else None
    entries, skipped = folders.scan(folder_path)
    scanned = _scan_many(entries)
    folder_hash = _folder_digest(entries, scanned)
    duplicate = _cat().find_item_by_hash(folder_hash)
    if duplicate is not None:
        return _duplicate_result(duplicate)
    category = _category_row(category_name)
    root_name = paths.bare_name(os.path.basename(os.path.normpath(folder_path)), fallback="folder")
    if not _enough_space(_plain_size(scanned.values())):
        return _fail("INSUFFICIENT_SPACE", "Not enough free space to archive folder")
    with _scope("archive_folder") as scope:
        started = {e: time.perf_counter() for e in scanned}
        decisions = _decide_many(scope, scanned)
        stored_total = sum(d.stored_size for d in decisions.values())
        if not _enough_space(stored_total):
            return _fail("INSUFFICIENT_SPACE", "Not enough free space to archive folder")
        uid = Catalog.new_uid()
        with _write_lock:
            archive_id, zip_path = _choose_archive(category, stored_total)
            with _recorded_write(archive_id, zip_path):
                _raise_if_cancelled()  # the last moment a cancel leaves everything as it was
                records = store.write_members(zip_path, _folder_plan(uid, root_name, entries, decisions))
                original_size = sum(d.info.size for d in decisions.values())
                compressed = sum(r["stored_size"] for r in records)
                item = {"file_type": "folder", "file_name": root_name, "original_path": folder_path,
                        "path_in_archive": f"{uid}/{root_name}/", "original_size": original_size, "compressed_size": compressed,
                        "original_hash": folder_hash, "archive_hash": _records_digest(records), "strategy": None, "uid": uid}
                item_id = _cat().add_item(archive_id, item, records, archive_size=os.path.getsize(zip_path))
            _after_recorded(archive_id, zip_path, category)
    by_path = {r["relative_path"]: r for r in records}
    file_results = [{
        "path_in_archive": by_path[f"{root_name}/{e.rel_path}"]["path_in_archive"],
        "file_type": events.file_type_label(e.rel_path), "strategy": d.method if d.smart else None,
        "original_size": d.info.size, "stored_size": by_path[f"{root_name}/{e.rel_path}"]["stored_size"],
        "processing_ms": int((time.perf_counter() - started[e]) * 1000)} for e, d in decisions.items()]
    skipped_cloud = [s.full_path for s in skipped if s.reason == "cloud"]
    message = "Archived and verified successfully"
    if skipped:
        message += f"\n{len(skipped)} item(s) were skipped (cloud files not on this device, or links)."
    result = {"success": True, "reason": "OK", "message": message, "original_size": original_size,
              "compressed_size": compressed, "savings": original_size - compressed, "file_results": file_results,
              "skipped_cloud_files": skipped_cloud, "skipped_links": [s.full_path for s in skipped if s.reason == "link"]}
    if source_action != "keep":
        if skipped:
            result["message"] += "\nThe source was kept because not everything in it could be archived."
        else:
            _remove_source_unless_cancelled(result, item_id, folder_path, state, source_action)
    return result


# --- checkout / checkin --------------------------------------------------------------------------------

def checkout(item_id, destination=None):
    return checkout_result(item_id, destination)["path"]


def checkout_result(item_id, destination=None):
    """Like checkout, with the reason: {"success", "path", "reason"}."""
    start = time.perf_counter()
    with _operation():
        path, reason = _checkout(item_id, destination)
    events.report_operation({"event_type": "checkout_completed" if path else "checkout_failed", "operation_type": "checkout",
                             "result": "success" if path else "failure", "duration_ms": _ms(start),
                             "error_category": reason, "file_count": 1})
    return {"success": path is not None, "path": path, "reason": reason}


def _longest_target(dest, members):
    """The longest path a checkout of `members` into `dest` creates, and whether it is a folder."""
    longest = max(members, key=lambda m: len(m["relative_path"]))
    return os.path.join(os.path.abspath(dest), *longest["relative_path"].split("/")), longest["kind"] == "dir"


def _checkout(item_id, destination):
    cat = _cat()
    item = cat.item_by_id(item_id)
    if item is None:
        return None, "ITEM_NOT_FOUND"
    if item.status == "CHECKED_OUT":
        return (os.path.normpath(item.workspace_path) if item.workspace_path else None), None
    zip_path = cat.archive_path_of_item(item_id)
    members = cat.members_of_item(item_id)
    if not zip_path or not members:
        return None, "ARCHIVE_NOT_FOUND"
    root = members[0]["relative_path"].split("/", 1)[0]
    dest = destination or os.path.join(config.workspace_dir(), cat.item_uid(item_id))
    try:
        target = paths.safe_target(dest, root)
    except ExtractionRefused:
        return None, "SMART_RESTORE_ERROR"
    if os.path.exists(target):
        return None, "TARGET_EXISTS"  # never overwrite what is already there
    sizes = [m["original_size"] or 0 for m in members]
    if not _enough_space(sum(sizes), where=dest, temp=max(sizes)):
        return None, "INSUFFICIENT_SPACE"
    try:
        with _scope("checkout") as scope:
            executor = Executor(scope)
            _phase("Restoring", sum(1 for m in members if m["kind"] != "dir"))
            store.extract_members(zip_path, members, dest, work_root=scope.root, restorer=executor.restore,
                                  streams=executor.runs_in_process, stop=_cancelled, on_file=_step)
    except (store.RestoreError, ExtractionRefused) as exc:
        if _cancelled():
            # the target did not exist before this checkout (checked above): what is there is this checkout's
            for leftover in (target, target + ".restoring"):
                if os.path.isdir(leftover):
                    shutil.rmtree(leftover, ignore_errors=True)
                elif os.path.exists(leftover):
                    with contextlib.suppress(OSError):
                        os.remove(leftover)
            if not destination:
                _remove_empty_parent(target)
            return None, "CANCELLED"
        log.error("checkout of item %s failed: %s", item_id, exc)
        if os.path.isdir(target) and not destination:
            shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc.__cause__, OSError) and paths.too_long(*_longest_target(dest, members)):
            return None, "PATH_TOO_LONG"  # Windows refused the path: say so, the archive itself is fine
        return None, "SMART_RESTORE_ERROR"
    cat.set_item_status(item_id, "CHECKED_OUT", workspace_path=target)
    return target, None


def checkin(item_id, remove_workspace_copy=True):
    return checkin_result(item_id, remove_workspace_copy)["success"]


def checkin_result(item_id, remove_workspace_copy=True):
    """Like checkin, with the reason: {"success", "reason"}. A success can carry a reason too -
    WORKSPACE_COPY_MISSING means the checked-out copy was gone and the item went back to its archived version."""
    start = time.perf_counter()
    try:
        with _operation():
            ok, reason, stats = _checkin(item_id, remove_workspace_copy)
    except OperationCancelled:  # before the new version was recorded: the item and its copy are as they were
        ok, reason, stats = False, "CANCELLED", None
    except Exception as exc:  # noqa: BLE001 - a failed checkin keeps the workspace copy and reports failure
        log.exception("checkin of item %s failed", item_id)
        ok, reason, stats = False, type(exc).__name__, None
    event = {"event_type": "checkin_completed" if ok else "checkin_failed", "operation_type": "checkin",
             "result": "success" if ok else "failure", "duration_ms": _ms(start),
             "error_category": None if ok else reason, "file_count": 1,
             "original_bytes_total": (stats or {}).get("original"), "stored_bytes_total": (stats or {}).get("stored")}
    events.report_operation(event)
    return {"success": ok, "reason": reason}


def _checkin(item_id, remove_workspace_copy):
    cat = _cat()
    item = cat.item_by_id(item_id)
    if item is None:
        return False, "ITEM_NOT_FOUND", None
    if item.status != "CHECKED_OUT":
        return False, "NOT_CHECKED_OUT", None
    workspace = item.workspace_path
    if not workspace or not os.path.exists(workspace):
        # the checked-out copy is gone (deleted by the user): the archived version is untouched, so the item goes
        # back to it - otherwise it could neither be checked in nor checked out again. The caller tells the user.
        cat.set_item_status(item_id, "ARCHIVED")
        return True, "WORKSPACE_COPY_MISSING", None
    removable = remove_workspace_copy and _is_removable_copy(workspace)
    state = _state_of(workspace) if removable else None
    if item.file_type == "folder":
        entries, _skipped = folders.scan(workspace)
        scanned = _scan_many(entries)
        new_hash = _folder_digest(entries, scanned)
    else:
        entries, scanned = None, scan_file(workspace)
        new_hash = scanned[0].sha256
    unchanged = new_hash == item.original_hash
    stats = None
    if not unchanged:
        reason, stats = _store_new_version(item_id, workspace, entries, scanned, new_hash)
        if reason:
            return False, reason, None
    _too_late_to_cancel()  # the content is saved: what is left is finished, never left half done
    if removable:
        outcome, later = _remove_once_saved(item_id, workspace, state)
        stats = later or stats
        if outcome == "kept":
            # still being written to: what was saved so far stays saved, and the copy stays checked out
            cat.set_item_status(item_id, "CHECKED_OUT", workspace_path=workspace)
            return False, "WORKSPACE_STILL_CHANGING", None
        if outcome == "removed":
            _remove_empty_parent(workspace)
    if unchanged:
        cat.set_item_status(item_id, "ARCHIVED")
    return True, None, stats


def _store_new_version(item_id, path, entries, scanned, new_hash):
    """Replaces the item's content by what was scanned at `path` - a folder's `entries` and their scans, or
    (entries None) one file's scan. Returns (reason, stats): the reason it was not stored, or None and the sizes."""
    cat = _cat()
    item = cat.item_by_id(item_id)
    is_folder = entries is not None
    category = cat.category_by_id(cat.archive_by_id(item.archive_id).category_id)
    old_archive_id = item.archive_id
    uid = f"{cat.item_uid(item_id)}.{Catalog.new_uid()[:6]}"
    root_name = paths.bare_name(os.path.basename(os.path.normpath(path)), fallback="folder" if is_folder else "item")
    kept = {}
    with _scope("checkin") as scope:
        if is_folder:
            if cat.archive_state(old_archive_id)["state"] == "open":
                kept = _unchanged_members(cat.members_of_item(item_id), root_name, entries, scanned)
            changed = {e: s for e, s in scanned.items() if e not in kept}
            if not _enough_space(_plain_size(changed.values())):
                return "INSUFFICIENT_SPACE", None
            decisions = _decide_many(scope, changed)
            stored_total = sum(d.stored_size for d in decisions.values())
        else:
            if not _enough_space(_plain_size([scanned])):
                return "INSUFFICIENT_SPACE", None
            decision = _new_pipeline(scope).decide(path, scanned)
            stored_total = decision.stored_size
        if not _enough_space(stored_total):
            return "INSUFFICIENT_SPACE", None
        with _write_lock:
            archive_id, zip_path = _checkin_archive(old_archive_id, category, stored_total)
            if kept and archive_id != old_archive_id:
                # the item moves to another archive (its own filled up): unchanged files are written there too
                rest = _decide_many(scope, {e: scanned[e] for e in kept if e is not None and e.kind == "file"})
                if not _enough_space(sum(d.stored_size for d in rest.values())):
                    return "INSUFFICIENT_SPACE", None
                decisions.update(rest)
                kept = {}
            if is_folder:
                planned = _folder_plan(uid, root_name, entries, decisions, kept)
                original = sum(d.info.size for d in decisions.values()) + sum(
                    m["original_size"] for m in kept.values() if m["kind"] == "file")
            else:
                planned = [_planned_from_decision(decision, path, f"{uid}/{root_name}", root_name)]
                original = decision.info.size
            with _recorded_write(archive_id, zip_path):
                _raise_if_cancelled()  # the last moment a cancel leaves everything as it was
                records = store.write_members(zip_path, planned)
                content = [*kept.values(), *records]
                stored = sum(r["stored_size"] for r in content)
                new_item = {"path_in_archive": f"{uid}/{root_name}" + ("/" if is_folder else ""), "original_size": original,
                            "compressed_size": stored, "original_hash": new_hash,
                            "archive_hash": _records_digest(content) if is_folder else records[0]["stored_sha256"],
                            "strategy": None if is_folder or not decision.smart else decision.method}
                cat.replace_item_content(item_id, new_item, records, new_archive_id=archive_id,
                                         kept=[m["path_in_archive"] for m in kept.values()],
                                         archive_size=os.path.getsize(zip_path) if os.path.exists(zip_path) else None)
            _after_recorded(archive_id, zip_path, category, old_archive_id)
    return None, {"original": original, "stored": stored}


def _checkin_archive(old_archive_id, category, incoming):
    cat = _cat()
    state = cat.archive_state(old_archive_id)
    row = cat.archive_by_id(old_archive_id)
    if state["state"] == "open" and os.path.getsize(row.file_path) + incoming <= category.max_archive_size:
        return old_archive_id, row.file_path
    return _choose_archive(category, incoming)


def _refresh_old_archive(archive_id, category):
    cat = _cat()
    row = cat.archive_by_id(archive_id)
    state = cat.archive_state(archive_id)
    manifest = store.write_manifest(row.file_path, category.name, cat.members_of_archive(archive_id), state["members_digest"],
                                    state["state"], state["zip_hash"])
    cat.set_archive_file_facts(archive_id, os.path.getsize(row.file_path), manifest)


def _compact_if_worth_it(archive_id, category):
    """Rewrites an open archive without its dead members once they are a quarter of it (the old version
    never reclaimed the space of replaced items)."""
    cat = _cat()
    state = cat.archive_state(archive_id)
    row = cat.archive_by_id(archive_id)
    if state["state"] != "open" or not os.path.exists(row.file_path):
        return
    if state["dead_bytes"] < COMPACT_DEAD_RATIO * max(os.path.getsize(row.file_path), 1):
        return
    _compact(archive_id, category)


def _compact(archive_id, category):
    cat = _cat()
    row = cat.archive_by_id(archive_id)
    facts = store.compact(row.file_path, cat.members_of_archive(archive_id))
    cat.record_compaction(archive_id, facts, os.path.getsize(row.file_path))
    _refresh_old_archive(archive_id, category)


def _is_removable_copy(workspace):
    """A checked-out copy is removed only inside the workspace folder, never anywhere a tampered row points."""
    return paths.is_under(workspace, config.workspace_dir()) and not paths.is_protected(workspace)


def _remove_empty_parent(workspace):
    parent = os.path.dirname(workspace)
    try:
        if paths.is_under(parent, config.workspace_dir()) and os.path.normcase(parent) != os.path.normcase(config.workspace_dir()):
            os.rmdir(parent)
    except OSError:
        pass


# --- verification --------------------------------------------------------------------------------------

def verify_archive_by_id(archive_id):
    start = time.perf_counter()
    cat = _cat()
    row = cat.archive_by_id(archive_id)
    if row is None:
        status = "MISSING"
    else:
        status = _verify_archive_row(archive_id, row)
        cat.set_archive_status(archive_id, status)
    events.report_operation({"event_type": "verify_completed" if status == "VERIFIED" else "verify_failed",
                             "operation_type": "verify", "result": "success" if status == "VERIFIED" else "failure",
                             "duration_ms": _ms(start), "error_category": None if status == "VERIFIED" else status})
    return status


def verify_category(category_name):
    cat = _cat()
    row = cat.category_by_name(category_name)
    if row is None:
        return {}
    return {a.file_name: verify_archive_by_id(a.id) for a in cat.archives_of_category(row.id)}


def verify_all():
    return {c.name: verify_category(c.name) for c in list_categories()}


# --- telemetry -----------------------------------------------------------------------------------------

def _ms(start):
    return int((time.perf_counter() - start) * 1000)


def _report_archive(result, path, seconds, folder=False):
    ok = bool(result.get("success")) and result.get("reason") == "OK"
    duration = int(seconds * 1000)
    metrics = None
    if ok and not folder:
        metrics = [{"file_type": events.file_type_label(path), "strategy": result["strategy"], "file_count": 1,
                    "original_bytes": result["original_size"], "stored_bytes": result["compressed_size"],
                    "saved_bytes": max(result["savings"], 0),
                    "avg_saving_percent": result["savings"] / result["original_size"] * 100 if result["original_size"] else None,
                    "avg_processing_ms": duration, "min_processing_ms": duration, "max_processing_ms": duration}]
    elif ok:
        groups = {}
        for r in result.get("file_results") or []:
            groups.setdefault((r["file_type"], r["strategy"] or BASELINE_NAME), []).append(r)
        metrics = [{"file_type": ft, "strategy": st, "file_count": len(rs),
                    "original_bytes": sum(r["original_size"] for r in rs), "stored_bytes": sum(r["stored_size"] for r in rs),
                    "saved_bytes": max(sum(r["original_size"] - r["stored_size"] for r in rs), 0)}
                   for (ft, st), rs in groups.items()]
    event = {"event_type": "archive_completed" if result.get("success") else "archive_failed", "operation_type": "archive",
             "result": "success" if result.get("success") else "failure", "duration_ms": duration,
             "error_category": None if result.get("success") else result.get("reason"),
             "original_bytes_total": result.get("original_size"), "stored_bytes_total": result.get("compressed_size"),
             "saved_bytes_total": result.get("savings")}
    if ok and result.get("original_size"):
        event["saving_percent"] = result["savings"] / result["original_size"] * 100
    if not folder:
        event["file_count"] = 1
    events.report_operation(event, metrics)
