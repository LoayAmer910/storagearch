"""The public API of the core - the functions the GUI calls, with the same names, arguments, return values
and row shapes as the old `main` module, so the GUI only changes its imports.

Every archive / checkin runs inside one OperationScope (all temporary files and worker processes are gone
when it returns), decides each file by content through the pipeline, writes crash-safe through the store,
and records the result in the catalog only after the archive was read back. Cloud events are handed to
core.events after the local result is final and can never change it.
"""
import concurrent.futures
import logging
import os
import re
import shutil
import sqlite3
import threading
import time

from core import catalog as catalog_module
from core import config, events, folders, paths, router, store
from core.catalog import EMPTY_DIGEST, Catalog, LegacyDatabaseError
from core.executor import Executor
from core.paths import CloudPlaceholderError, ExtractionRefused, InvalidPathError
from core.pipeline import Pipeline, scan as scan_file
from core.scope import OperationScope, sweep_stale
from core.store import PlannedMember

log = logging.getLogger("storagearch")

BASELINE_NAME = "baseline_storagearch_zip"
DEFAULT_MAX_ARCHIVE_SIZE = catalog_module.DEFAULT_MAX_ARCHIVE_SIZE
COMPACT_DEAD_RATIO = 0.25
_SPACE_MARGIN = 16 * 1024 * 1024
_SOURCE_ACTIONS = ("keep", "recycle", "delete")
_write_lock = threading.RLock()     # archive files and catalog writes: one writer at a time
_catalog = None
_catalog_lock = threading.Lock()

__all__ = [
    "initialize", "create_category", "update_category", "delete_category", "get_category", "list_categories",
    "list_archives", "get_item", "get_archive", "search", "find_duplicate", "dashboard_summary",
    "archive_file", "archive_file_result", "archive_folder", "archive_folder_result", "checkout", "checkin", "checkin_result",
    "items_pending_return", "handle_pending_checkouts_on_exit", "verify_archive_by_id", "verify_category",
    "verify_all", "is_protected_path", "get_storage_locations", "set_storage_location", "reset_workspace_location",
    "calculate_file_hash", "calculate_folder_hash", "validate_source_path", "data_root", "shutdown_cloud",
    "CloudPlaceholderError", "InvalidPathError", "LegacyDatabaseError",
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

def initialize():
    """Loads the config, creates the data folders, opens the catalog, repairs interrupted writes and
    removes temporary folders of processes that died. Returns the database path."""
    global _catalog
    config.load()
    for folder in (config.archives_dir(), config.workspace_dir(), config.temp_dir(), config.log_dir()):
        os.makedirs(folder, exist_ok=True)
    with _catalog_lock:
        _catalog = Catalog(config.db_path())
    repaired = store.recover(config.archives_dir())
    if repaired:
        log.warning("repaired interrupted writes: %s", repaired)
    sweep_stale(config.temp_dir())
    events.start()
    return config.db_path()


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
    os.makedirs(os.path.join(config.archives_dir(), row[6]), exist_ok=True)
    return True


def update_category(category_id, name, description=None, color=None, max_archive_size=DEFAULT_MAX_ARCHIVE_SIZE):
    return _cat().update_category(category_id, name, description, color, max_archive_size)


def delete_category(category_id):
    cat = _cat()
    row = cat.category_by_id(category_id)
    result = cat.delete_category(category_id)
    if result == "DELETED" and row is not None:
        try:
            os.rmdir(os.path.join(config.archives_dir(), row[6]))
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
    return _duplicate_info(row, _verify_item(row[0]))


def _duplicate_info(row, status):
    return {"item_name": row[1], "category": row[8], "archive": row[7], "path_in_archive": row[3], "status": status}


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
    return store.verify_archive(row[3], cat.members_of_archive(archive_id) if members is None else members,
                                sealed, expected_size=row[4], known_names=known)


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
    try:
        return store.sha256_file(file_path)
    except OSError as exc:
        raise _read_error(file_path, exc) from exc


def calculate_folder_hash(folder_path):
    if not os.path.isdir(folder_path):
        return None
    return folders.folder_hash(folder_path, calculate_file_hash)


def _read_error(path, exc):
    if paths.is_cloud_placeholder(path):
        return CloudPlaceholderError(f"'{path}' is a OneDrive/cloud file that has not been downloaded to this device")
    ok, reason = paths.validate_source_path(path)
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


def _guarded_operation(fn, *args):
    try:
        return fn(*args)
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
    category_id, max_size, folder = category[0], category[4], category[6]
    for archive_id, zip_path, _size in cat.open_archives(category_id):
        actual = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
        if actual == 0 or actual + incoming_bytes <= max_size:
            return archive_id, zip_path
        _seal(archive_id, zip_path, category)  # full: its content is final from now on
    folder_path = os.path.join(config.archives_dir(), folder)
    os.makedirs(folder_path, exist_ok=True)
    used = {int(m.group(1)) for a in cat.archives_of_category(category_id) if (m := _SEQ.search(a[2]))}
    used |= {int(m.group(1)) for f in os.listdir(folder_path) if (m := _SEQ.search(f))}
    seq = max(used, default=0) + 1
    zip_path = os.path.join(folder_path, f"{folder}_{seq:03d}.zip")
    return cat.add_archive(category_id, zip_path), zip_path


def _enough_space(needed):
    try:
        return shutil.disk_usage(config.data_root()).free >= needed + _SPACE_MARGIN
    except OSError:
        return False


def _seal(archive_id, zip_path, category):
    cat = _cat()
    file_sha = store.sha256_file(zip_path)
    cat.seal_archive(archive_id, file_sha)
    state = cat.archive_state(archive_id)
    manifest = store.write_manifest(zip_path, category[1], cat.members_of_archive(archive_id), state["members_digest"],
                                    "sealed", file_sha)
    cat.set_archive_file_facts(archive_id, os.path.getsize(zip_path), manifest)


def _after_write(archive_id, zip_path, category):
    """Updates the archive's facts and manifest; seals it once it reached the category's size limit."""
    cat = _cat()
    size = os.path.getsize(zip_path)
    members = cat.members_of_archive(archive_id)
    state = cat.archive_state(archive_id)
    cat.set_archive_status(archive_id, "VERIFIED")
    if state["state"] == "open" and size >= category[4] > 0:
        _seal(archive_id, zip_path, category)
        return
    manifest = store.write_manifest(zip_path, category[1], members, state["members_digest"], state["state"], state["zip_hash"])
    cat.set_archive_file_facts(archive_id, size, manifest)


def _planned_from_decision(decision, source, member_path, relative_path):
    return PlannedMember(member_path, relative_path, "file", decision.method, decision.strategy_version, source,
                         decision.stored_path, decision.info.sha256, decision.info.size, _mtime(source))


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _new_pipeline(scope):
    disabled = events.disabled_strategies()
    route = (lambda info: [s for s in router.route(info) if s.name not in disabled]) if disabled else router.route
    return Pipeline(Executor(scope), route)


def _apply_source_action(path, action):
    if action == "keep" or _is_protected_source(path):
        return action == "keep"
    try:
        if action == "recycle":
            from send2trash import send2trash
            send2trash(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return True
    except (OSError, ImportError) as exc:
        log.warning("source action %s failed: %s", action, exc)
        return False


def _duplicate_result(row):
    status = _verify_item(row[0])
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
        scanned = scan_file(file_path)
    except OSError as exc:
        raise _read_error(file_path, exc) from exc
    info = scanned[0]
    duplicate = _cat().find_item_by_hash(info.sha256)
    if duplicate is not None:
        return _duplicate_result(duplicate)
    category = _category_row(category_name)
    name = paths.bare_name(os.path.basename(file_path))
    with OperationScope("archive_file", temp_root=config.temp_dir()) as scope:
        decision = _new_pipeline(scope).decide(file_path, scanned)
        if not _enough_space(decision.stored_size):
            return _fail("INSUFFICIENT_SPACE", "Not enough free space to archive file")
        uid = Catalog.new_uid()
        with _write_lock:
            archive_id, zip_path = _choose_archive(category, decision.stored_size)
            records = store.write_members(zip_path, [_planned_from_decision(decision, file_path, f"{uid}/{name}", name)])
            item = {"file_type": "file", "file_name": name, "original_path": file_path, "path_in_archive": f"{uid}/{name}",
                    "original_size": info.size, "compressed_size": records[0]["stored_size"],
                    "original_hash": info.sha256, "archive_hash": records[0]["stored_sha256"],
                    "strategy": decision.method if decision.smart else None, "uid": uid}
            _cat().add_item(archive_id, item, records)
            _after_write(archive_id, zip_path, category)
    stored = records[0]["stored_size"]
    result = {"success": True, "reason": "OK", "message": "Archived and verified successfully",
              "original_size": info.size, "compressed_size": stored, "savings": info.size - stored,
              "smart_compression_used": decision.smart, "strategy": decision.method if decision.smart else BASELINE_NAME}
    if source_action != "keep" and not _apply_source_action(file_path, source_action):
        result["message"] += " (the source could not be removed)"
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
        try:
            return e, scan_file(e.full_path)
        except OSError as exc:
            raise _read_error(e.full_path, exc) from exc
    workers = max(1, min(8, len(files)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as pool:
        return dict(pool.map(one, files))


def _decide_many(scope, scanned):
    pipeline = _new_pipeline(scope)
    workers = max(1, min(4 * (os.cpu_count() or 2), 32, len(scanned)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="decide") as pool:
        futures = {e: pool.submit(pipeline.decide, e.full_path, s) for e, s in scanned.items()}
        return {e: f.result() for e, f in futures.items()}


def _folder_digest(entries, scanned):
    return folders.digest((e.rel_path, e.kind, scanned[e][0].sha256 if e.kind == "file" else EMPTY_DIGEST) for e in entries)


def _folder_plan(uid_prefix, root_name, entries, decisions):
    planned = [PlannedMember(f"{uid_prefix}/{root_name}/", root_name, "dir", "stored")]
    for e in entries:
        member = f"{uid_prefix}/{root_name}/{e.rel_path}"
        relative = f"{root_name}/{e.rel_path}"
        if e.kind == "dir":
            planned.append(PlannedMember(member + "/", relative, "dir", "stored"))
        else:
            planned.append(_planned_from_decision(decisions[e], e.full_path, member, relative))
    return planned


def _records_digest(records):
    return folders.digest((r["path_in_archive"], r["kind"], r["stored_sha256"]) for r in records)


def _archive_folder(folder_path, category_name, source_action):
    entries, skipped = folders.scan(folder_path)
    scanned = _scan_many(entries)
    folder_hash = _folder_digest(entries, scanned)
    duplicate = _cat().find_item_by_hash(folder_hash)
    if duplicate is not None:
        return _duplicate_result(duplicate)
    category = _category_row(category_name)
    root_name = paths.bare_name(os.path.basename(os.path.normpath(folder_path)), fallback="folder")
    with OperationScope("archive_folder", temp_root=config.temp_dir()) as scope:
        started = {e: time.perf_counter() for e in scanned}
        decisions = _decide_many(scope, scanned)
        stored_total = sum(d.stored_size for d in decisions.values())
        if not _enough_space(stored_total):
            return _fail("INSUFFICIENT_SPACE", "Not enough free space to archive folder")
        uid = Catalog.new_uid()
        with _write_lock:
            archive_id, zip_path = _choose_archive(category, stored_total)
            records = store.write_members(zip_path, _folder_plan(uid, root_name, entries, decisions))
            original_size = sum(d.info.size for d in decisions.values())
            compressed = sum(r["stored_size"] for r in records)
            item = {"file_type": "folder", "file_name": root_name, "original_path": folder_path,
                    "path_in_archive": f"{uid}/{root_name}/", "original_size": original_size, "compressed_size": compressed,
                    "original_hash": folder_hash, "archive_hash": _records_digest(records), "strategy": None, "uid": uid}
            _cat().add_item(archive_id, item, records)
            _after_write(archive_id, zip_path, category)
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
        elif not _apply_source_action(folder_path, source_action):
            result["message"] += " (the source could not be removed)"
    return result


# --- checkout / checkin --------------------------------------------------------------------------------

def checkout(item_id, destination=None):
    start = time.perf_counter()
    path, reason = _checkout(item_id, destination)
    events.report_operation({"event_type": "checkout_completed" if path else "checkout_failed", "operation_type": "checkout",
                             "result": "success" if path else "failure", "duration_ms": _ms(start),
                             "error_category": reason, "file_count": 1})
    return path


def _checkout(item_id, destination):
    cat = _cat()
    item = cat.item_by_id(item_id)
    if item is None:
        return None, "ITEM_NOT_FOUND"
    if item[10] == "CHECKED_OUT":
        return (os.path.normpath(item[11]) if item[11] else None), None
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
    if not _enough_space(sum(m["original_size"] for m in members)):
        return None, "INSUFFICIENT_SPACE"
    try:
        with OperationScope("checkout", temp_root=config.temp_dir()) as scope:
            store.extract_members(zip_path, members, dest, work_root=scope.root, restorer=Executor(scope).restore)
    except (store.RestoreError, ExtractionRefused) as exc:
        log.error("checkout of item %s failed: %s", item_id, exc)
        if os.path.isdir(target) and not destination:
            shutil.rmtree(target, ignore_errors=True)
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
        ok, reason, stats = _checkin(item_id, remove_workspace_copy)
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
    if item[10] != "CHECKED_OUT":
        return False, "NOT_CHECKED_OUT", None
    workspace = item[11]
    if not workspace or not os.path.exists(workspace):
        # the checked-out copy is gone (deleted by the user): the archived version is untouched, so the item goes
        # back to it - otherwise it could neither be checked in nor checked out again. The caller tells the user.
        cat.set_item_status(item_id, "ARCHIVED")
        return True, "WORKSPACE_COPY_MISSING", None
    is_folder = item[2] == "folder"
    if is_folder:
        entries, skipped = folders.scan(workspace)
        scanned = _scan_many(entries)
        new_hash = _folder_digest(entries, scanned)
    else:
        scanned_file = scan_file(workspace)
        new_hash = scanned_file[0].sha256

    if new_hash == item[8]:
        _remove_workspace_copy(workspace, remove_workspace_copy)
        cat.set_item_status(item_id, "ARCHIVED")
        return True, None, None

    category = cat.category_by_id(cat.archive_by_id(item[1])[1])
    old_archive_id = item[1]
    uid = f"{cat.item_uid(item_id)}.{Catalog.new_uid()[:6]}"
    root_name = paths.bare_name(os.path.basename(os.path.normpath(workspace)), fallback="folder" if is_folder else "item")
    with OperationScope("checkin", temp_root=config.temp_dir()) as scope:
        if is_folder:
            decisions = _decide_many(scope, scanned)
            stored_total = sum(d.stored_size for d in decisions.values())
        else:
            decision = _new_pipeline(scope).decide(workspace, scanned_file)
            stored_total = decision.stored_size
        if not _enough_space(stored_total):
            return False, "INSUFFICIENT_SPACE", None
        with _write_lock:
            archive_id, zip_path = _checkin_archive(old_archive_id, category, stored_total)
            if is_folder:
                planned = _folder_plan(uid, root_name, entries, decisions)
                original = sum(d.info.size for d in decisions.values())
            else:
                planned = [_planned_from_decision(decision, workspace, f"{uid}/{root_name}", root_name)]
                original = decision.info.size
            records = store.write_members(zip_path, planned)
            stored = sum(r["stored_size"] for r in records)
            new_item = {"path_in_archive": f"{uid}/{root_name}" + ("/" if is_folder else ""), "original_size": original,
                        "compressed_size": stored, "original_hash": new_hash,
                        "archive_hash": _records_digest(records) if is_folder else records[0]["stored_sha256"],
                        "strategy": None if is_folder or not decision.smart else decision.method}
            cat.replace_item_content(item_id, new_item, records, new_archive_id=archive_id)
            _after_write(archive_id, zip_path, category)
            if archive_id != old_archive_id:
                _refresh_old_archive(old_archive_id, category)
            _compact_if_worth_it(old_archive_id, category)
    _remove_workspace_copy(workspace, remove_workspace_copy)
    return True, None, {"original": original, "stored": stored}


def _checkin_archive(old_archive_id, category, incoming):
    cat = _cat()
    state = cat.archive_state(old_archive_id)
    row = cat.archive_by_id(old_archive_id)
    if state["state"] == "open" and os.path.getsize(row[3]) + incoming <= category[4]:
        return old_archive_id, row[3]
    return _choose_archive(category, incoming)


def _refresh_old_archive(archive_id, category):
    cat = _cat()
    row = cat.archive_by_id(archive_id)
    state = cat.archive_state(archive_id)
    manifest = store.write_manifest(row[3], category[1], cat.members_of_archive(archive_id), state["members_digest"],
                                    state["state"], state["zip_hash"])
    cat.set_archive_file_facts(archive_id, os.path.getsize(row[3]), manifest)


def _compact_if_worth_it(archive_id, category):
    """Rewrites an open archive without its dead members once they are a quarter of it (the old version
    never reclaimed the space of replaced items)."""
    cat = _cat()
    state = cat.archive_state(archive_id)
    row = cat.archive_by_id(archive_id)
    if state["state"] != "open" or not os.path.exists(row[3]):
        return
    if state["dead_bytes"] < COMPACT_DEAD_RATIO * max(os.path.getsize(row[3]), 1):
        return
    live = cat.members_of_archive(archive_id)
    facts = store.compact(row[3], live)
    cat.update_member_facts(archive_id, facts)
    cat.forget_dead_members(archive_id)
    _refresh_old_archive(archive_id, category)


def _remove_workspace_copy(workspace, remove):
    """Removes a checked-out copy - only inside the workspace folder, never anywhere a tampered row points."""
    if not remove or not paths.is_under(workspace, config.workspace_dir()) or paths.is_protected(workspace):
        return
    try:
        if os.path.isdir(workspace):
            shutil.rmtree(workspace)
        else:
            os.remove(workspace)
        parent = os.path.dirname(workspace)
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
    return {a[2]: verify_archive_by_id(a[0]) for a in cat.archives_of_category(row[0])}


def verify_all():
    return {c[1]: verify_category(c[1]) for c in list_categories()}


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
