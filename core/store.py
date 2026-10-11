"""The archive files: ZIP containers of members, written crash-safe and read back before they count.

Members
    A baseline member is an ordinary ZIP entry (Deflate, or stored when Deflate would not shrink it), so any
    ZIP tool extracts the real file. A smart member is stored uncompressed under `<name>.sa` and holds the
    strategy's output; its ZIP comment says how to restore it (strategy, format version, original SHA-256
    and size), so an archive can be restored without the database. Folders keep their empty directories.

Writing (I1)
    Appending to an existing archive first saves its central directory and offset (`.guard` + `.tail`); any
    failure - or a crash, via recover() at startup - puts the file back exactly as it was. A new archive is
    written as `.partial` and renamed into place. The bytes of a baseline member are hashed while they are
    written: if the source changed after it was decided, the write is refused. After writing, every new
    member is read back from the archive and compared before the write counts.

Extracting (I1, I7)
    Every target path comes from paths.safe_target(); a member is restored into a temporary file next to its
    target, compared with its original SHA-256, and only then renamed into place.
"""
import concurrent.futures
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass

from core import paths, strategies
from core.catalog import EMPTY_DIGEST
from core.strategies.base import StrategyError, StreamingStrategy

SMART_SUFFIX = ".sa"
BASELINE_METHODS = ("deflate", "stored")
ZIP_WRITE_CHUNK = 8 * 1024          # zipfile's own write size; the baseline measured by the pipeline assumes it
_READ_CHUNK = 1024 * 1024
_ZIP64_THRESHOLD = 0x7FFF0000
EXTRACT_WORKERS = 4                 # measured: the system's cost per new file overlaps up to ~4 threads


class StoreError(Exception):
    """An archive could not be written or read as expected; the archive is left as it was."""


class SourceChanged(StoreError):
    """The source's bytes no longer match the hash that was decided on."""


class RestoreError(StoreError):
    """A member could not be restored to its exact original bytes."""


@dataclass(frozen=True)
class PlannedMember:
    path_in_archive: str             # "<uid>/<name>" for a file, "<uid>/<folder>/<rel>" inside a folder (dirs end in "/")
    relative_path: str               # the path under the destination when restored ("<name>" or "<folder>/<rel>")
    kind: str                        # "file" or "dir"
    method: str                      # "deflate", "stored" or a strategy name
    method_version: int = 0
    source_path: str | None = None   # the original file
    stored_path: str | None = None   # the strategy's output, for a smart member
    original_sha256: str = EMPTY_DIGEST
    original_size: int = 0
    mtime: float | None = None
    stored_bytes: bytes | None = None  # the strategy's output in memory, instead of stored_path (a small file)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_READ_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def zip_name(member):
    """The entry name inside the ZIP for a planned member or a catalog member row."""
    if isinstance(member, PlannedMember):
        path, kind, method = member.path_in_archive, member.kind, member.method
    else:
        return member["path_in_archive"]
    if kind == "dir":
        return path.rstrip("/") + "/"
    return path + SMART_SUFFIX if method not in BASELINE_METHODS else path


def _date_time(mtime):
    t = time.localtime(mtime if mtime and mtime > 315532800 else time.time())  # ZIP cannot hold dates before 1980
    return max(t[:6], (1980, 1, 1, 0, 0, 0))


# --- writing -------------------------------------------------------------------------------------------

def write_members(zip_path, planned):
    """Adds the planned members to zip_path (creating it if needed). Returns one catalog record per member.
    On any failure the archive is exactly as before and StoreError (or SourceChanged) is raised."""
    if not planned:
        return []
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    if os.path.exists(zip_path):
        return _append(zip_path, planned)
    partial = zip_path + ".partial"
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as z:
            records = [_write_one(z, m) for m in planned]
        _read_back(partial, records)
        _sync(partial)
        os.replace(partial, zip_path)
        return records
    except BaseException:
        _silent_remove(partial)
        raise


def _append(zip_path, planned):
    guard, tail = zip_path + ".guard", zip_path + ".tail"
    try:
        with zipfile.ZipFile(zip_path) as z:
            start_dir = z.start_dir
    except (zipfile.BadZipFile, OSError) as exc:
        raise StoreError(f"{zip_path} cannot be opened for appending: {exc}") from exc
    with open(zip_path, "rb") as f:
        f.seek(start_dir)
        saved_tail = f.read()
    _write_durably(tail, saved_tail)
    _write_durably(guard, json.dumps({"start_dir": start_dir, "tail_size": len(saved_tail)}).encode())
    try:
        existing = _names(zip_path)
        clash = [zip_name(m) for m in planned if zip_name(m) in existing]
        if clash:
            raise StoreError(f"members already in the archive: {clash[:3]}")
        with zipfile.ZipFile(zip_path, "a", allowZip64=True) as z:
            records = [_write_one(z, m) for m in planned]
        _read_back(zip_path, records)
        _sync(zip_path)  # the guard goes only once the new bytes are on the disk
    except BaseException:
        rollback(zip_path)
        raise
    _silent_remove(guard)
    _silent_remove(tail)
    return records


def _write_one(z, m):
    info = zipfile.ZipInfo(zip_name(m), date_time=_date_time(m.mtime))
    record = {"path_in_archive": info.filename, "relative_path": m.relative_path, "kind": m.kind, "method": m.method,
              "method_version": m.method_version, "original_size": m.original_size,
              "original_sha256": m.original_sha256}
    if m.kind == "dir":
        info.external_attr = 0o40775 << 16 | 0x10
        z.writestr(info, b"")
        return {**record, "original_size": 0, "original_sha256": EMPTY_DIGEST, "stored_size": 0,
                "stored_sha256": EMPTY_DIGEST, "crc32": 0}

    if m.method in BASELINE_METHODS:
        info.compress_type = zipfile.ZIP_DEFLATED if m.method == "deflate" else zipfile.ZIP_STORED
        source, expect = m.source_path, m.original_sha256
    else:
        info.compress_type = zipfile.ZIP_STORED
        info.comment = json.dumps({"s": m.method, "v": m.method_version, "sha256": m.original_sha256,
                                   "size": m.original_size, "mtime": m.mtime}, separators=(",", ":")).encode()
        source, expect = m.stored_path, None
        if m.stored_bytes is not None:
            source = io.BytesIO(m.stored_bytes)
    info.file_size = len(m.stored_bytes) if isinstance(source, io.BytesIO) else os.path.getsize(source)
    written = hashlib.sha256()
    with (source if isinstance(source, io.BytesIO) else open(source, "rb")) as src, \
            z.open(info, "w", force_zip64=info.file_size > _ZIP64_THRESHOLD) as dst:
        while chunk := src.read(ZIP_WRITE_CHUNK):
            written.update(chunk)
            dst.write(chunk)
    digest = written.hexdigest()
    if expect is not None and digest != expect:
        raise SourceChanged(f"{m.relative_path} changed while it was being archived")
    return {**record, "stored_size": info.compress_size, "stored_sha256": digest if m.method not in BASELINE_METHODS
            else _stored_sha_of_baseline(digest), "crc32": info.CRC}


def _stored_sha_of_baseline(original_digest):
    # For a baseline member the "stored bytes" that matter are the original bytes (the ZIP layer is the
    # codec); reading the member back yields them, so their hash is the stored hash.
    return original_digest


def _read_back(zip_path, records):
    """Reads every new member back out of the archive and compares it with what was meant to be written."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            for r in records:
                if r["kind"] == "dir":
                    z.getinfo(r["path_in_archive"])
                    continue
                h = hashlib.sha256()
                with z.open(r["path_in_archive"]) as f:
                    while chunk := f.read(_READ_CHUNK):
                        h.update(chunk)
                if h.hexdigest() != r["stored_sha256"]:
                    raise StoreError(f"{r['path_in_archive']} reads back differently from what was written")
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError, EOFError) as exc:
        raise StoreError(f"written archive cannot be read back: {exc}") from exc


def rollback(zip_path):
    """Puts an archive back as it was before an interrupted append (no-op without a guard)."""
    guard, tail = zip_path + ".guard", zip_path + ".tail"
    if not os.path.exists(guard):
        _silent_remove(tail)
        return False
    with open(guard, encoding="utf-8") as f:
        state = json.load(f)
    with open(tail, "rb") as f:
        saved_tail = f.read()
    if len(saved_tail) != state["tail_size"]:
        raise StoreError(f"the saved tail of {zip_path} is incomplete; the archive needs attention")
    with open(zip_path, "r+b") as f:
        f.seek(state["start_dir"])
        f.write(saved_tail)
        f.truncate(state["start_dir"] + len(saved_tail))
        f.flush()
        os.fsync(f.fileno())
    _silent_remove(guard)
    _silent_remove(tail)
    return True


def recover(archives_dir):
    """At startup: roll back interrupted appends and remove unfinished new archives. Returns what it touched."""
    touched = []
    if not os.path.isdir(archives_dir):
        return touched
    for folder, _dirs, files in os.walk(archives_dir):
        for name in files:
            path = os.path.join(folder, name)
            if name.endswith(".guard"):
                if rollback(path[: -len(".guard")]):
                    touched.append(path[: -len(".guard")])
            elif name.endswith(".partial") or name.endswith(".compacting"):
                _silent_remove(path)
                touched.append(path)
            elif name.endswith(".tail") and not os.path.exists(path[: -len(".tail")] + ".guard"):
                _silent_remove(path)
    return touched


def _names(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return set(z.namelist())


def _write_durably(path, data):
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _sync(path):
    """Has the file's bytes reach the disk. A file takes the place of another, or loses its guard, only
    after this - otherwise a power cut right after could leave the new name with content that was never written."""
    with open(path, "r+b") as f:
        os.fsync(f.fileno())


def _silent_remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


# --- extracting ----------------------------------------------------------------------------------------

def _restore_in_process(strategy, stored_path, out_dir, original_size):
    return strategy.restore(stored_path, out_dir, strategy.spec.timeout_s(original_size))


def _streams_in_process(strategy, size):
    spec = strategy.spec
    return isinstance(strategy, StreamingStrategy) and spec.in_process_safe and size <= spec.max_in_process_bytes


def extract_members(zip_path, members, destination, work_root=None, restorer=None, streams=None,
                    workers=EXTRACT_WORKERS):
    """Restores catalog member rows under `destination` (each at its relative_path). Every file is verified
    against its original SHA-256 before it appears under its name. `restorer(strategy, stored, out_dir, size)`
    runs a smart member's restore (the API passes Executor.restore, so it is killable); by default it runs
    in-process. A smart member whose codec may run here (`streams(strategy, size)`) is decoded straight from
    the archive into its file instead - no temporary files. Files are restored by `workers` threads: the
    time per small file is mostly the system opening the new file, which overlaps well. Returns the
    restored paths."""
    restorer = restorer or _restore_in_process
    streams = streams or _streams_in_process
    targets = [(m, paths.safe_target(destination, m["relative_path"])) for m in members]  # refuse before writing anything
    try:
        for member, target in targets:
            os.makedirs(target if member["kind"] == "dir" else os.path.dirname(target), exist_ok=True)
    except OSError as exc:
        raise RestoreError(f"cannot restore from {zip_path}: {exc}") from exc
    files = [(m, t) for m, t in targets if m["kind"] != "dir"]
    local, opened, failed = threading.local(), [], threading.Event()
    opened_lock = threading.Lock()

    def one(job):
        member, target = job
        if failed.is_set():
            return None
        try:
            z = getattr(local, "zip", None)
            if z is None:
                z = local.zip = zipfile.ZipFile(zip_path)
                with opened_lock:
                    opened.append(z)
            _extract_file(z, member, target, work_root, restorer, streams)
            return target
        except (zipfile.BadZipFile, KeyError, OSError, EOFError) as exc:
            failed.set()
            raise RestoreError(f"cannot restore from {zip_path}: {exc}") from exc
        except BaseException:
            failed.set()
            raise

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(files))),
                                                   thread_name_prefix="restore") as pool:
            futures = [pool.submit(one, job) for job in files]
            done = [f.exception() for f in futures]
        errors = [e for e in done if e is not None]
        if errors:
            raise errors[0]
        return [f.result() for f in futures]
    finally:
        for z in opened:
            z.close()


def _extract_file(z, member, target, work_root, restorer, streams=_streams_in_process):
    part = target + ".restoring"
    try:
        if member["method"] in BASELINE_METHODS:
            h = hashlib.sha256()
            with z.open(member["path_in_archive"]) as src, open(part, "wb") as dst:
                while chunk := src.read(_READ_CHUNK):
                    h.update(chunk)
                    dst.write(chunk)
            digest = h.hexdigest()
        else:
            digest = _restore_smart(z, member, part, work_root, restorer, streams)
        if digest != member["original_sha256"]:
            raise RestoreError(f"{member['relative_path']} did not restore to its original bytes")
        os.replace(part, target)
    finally:
        _silent_remove(part)


def _restore_smart(z, member, part, work_root, restorer, streams=_streams_in_process):
    strategy = strategies.get(member["method"])
    if strategy.spec.version != member["method_version"]:
        raise RestoreError(f"{member['relative_path']} was stored with {member['method']} format "
                           f"{member['method_version']}, this build reads {strategy.spec.version}")
    ok, reason = strategy.available()
    if not ok:
        raise RestoreError(f"{member['relative_path']} needs {member['method']}: {reason}")
    if isinstance(strategy, StreamingStrategy) and streams(strategy, member["original_size"]):
        h = hashlib.sha256()
        try:
            with z.open(member["path_in_archive"]) as src, open(part, "wb") as dst:
                for piece in strategy.decode(iter(lambda: src.read(_READ_CHUNK), b"")):
                    h.update(piece)
                    dst.write(piece)
        except (StrategyError, zipfile.BadZipFile, EOFError, RuntimeError) as exc:
            raise RestoreError(f"{member['relative_path']}: {type(exc).__name__}: {exc}") from exc
        return h.hexdigest()
    work = tempfile.mkdtemp(prefix="restore_", dir=work_root)
    try:
        stored = os.path.join(work, "stored")
        with z.open(member["path_in_archive"]) as src, open(stored, "wb") as dst:
            shutil.copyfileobj(src, dst, _READ_CHUNK)
        out_dir = os.path.join(work, "out")
        os.mkdir(out_dir)
        try:
            restored = restorer(strategy, stored, out_dir, member["original_size"])
        except Exception as exc:  # noqa: BLE001 - any strategy failure is a restore failure
            raise RestoreError(f"{member['relative_path']}: {type(exc).__name__}: {exc}") from exc
        digest = sha256_file(restored)
        shutil.move(restored, part)
        return digest
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- verifying, sealing, compacting --------------------------------------------------------------------

def verify_archive(zip_path, members, sealed_sha256=None, expected_size=None, known_names=None):
    """VERIFIED, CHANGED (members differ from the catalog, the file is not the size recorded after the last
    write, it holds entries the catalog does not know, or a sealed archive's file hash changed), CORRUPTED
    (unreadable, or a member's bytes no longer match) or MISSING."""
    if not os.path.isfile(zip_path):
        return "MISSING"
    if expected_size is not None and os.path.getsize(zip_path) != expected_size:
        return "CHANGED"  # e.g. data appended after the archive: zipfile itself reads past it without a word
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = {i.filename: i for i in z.infolist()}
            if known_names is not None and set(infos) - set(known_names):
                return "CHANGED"
            for m in members:
                info = infos.get(m["path_in_archive"])
                if info is None:
                    return "CHANGED"
                if m["kind"] == "dir":
                    continue
                if info.CRC != m["crc32"] or info.compress_size != m["stored_size"]:
                    return "CHANGED"
                h = hashlib.sha256()
                with z.open(info) as f:
                    while chunk := f.read(_READ_CHUNK):
                        h.update(chunk)
                if h.hexdigest() != m["stored_sha256"]:
                    return "CORRUPTED"
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, zipfile.LargeZipFile):
        return "CORRUPTED"
    except Exception:  # noqa: BLE001 - zlib errors from a damaged Deflate stream surface as various types
        return "CORRUPTED"
    if sealed_sha256 is not None and sha256_file(zip_path) != sealed_sha256:
        return "CHANGED"
    return "VERIFIED"


def compact(zip_path, members):
    """Rewrites the archive with only the given (live) members, reading each back before the new file
    replaces the old one. Baseline members are re-deflated the same way, so sizes and CRCs stay as recorded;
    smart members are copied byte for byte. Returns updated records keyed by path_in_archive."""
    temp = zip_path + ".compacting"
    try:
        with zipfile.ZipFile(zip_path) as old, zipfile.ZipFile(temp, "w", allowZip64=True) as new:
            for m in members:
                info = old.getinfo(m["path_in_archive"])
                clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                clone.compress_type, clone.comment, clone.external_attr = info.compress_type, info.comment, info.external_attr
                clone.file_size = info.file_size
                with old.open(info) as src, new.open(clone, "w", force_zip64=info.file_size > _ZIP64_THRESHOLD) as dst:
                    while chunk := src.read(ZIP_WRITE_CHUNK):
                        dst.write(chunk)
        updated = {}
        with zipfile.ZipFile(temp) as z:
            for m in members:
                info = z.getinfo(m["path_in_archive"])
                updated[m["path_in_archive"]] = {"stored_size": info.compress_size, "crc32": info.CRC}
        if verify_archive(temp, [{**m, **updated[m["path_in_archive"]]} for m in members]) != "VERIFIED":
            raise StoreError("the compacted archive did not verify")
        _sync(temp)
        os.replace(temp, zip_path)
        return updated
    except BaseException:
        _silent_remove(temp)
        raise


def write_manifest(zip_path, category, members, digest, state, file_sha256=None):
    """A human- and tool-readable listing next to the archive, written atomically."""
    manifest_path = os.path.splitext(zip_path)[0] + ".manifest.json"
    data = {
        "manifest_version": 2, "archive_name": os.path.basename(zip_path), "category": category,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "state": state, "members_digest": digest,
        "archive_size": os.path.getsize(zip_path) if os.path.exists(zip_path) else 0, "archive_sha256": file_sha256,
        "files": [{k: m[k] for k in ("path_in_archive", "relative_path", "kind", "method", "method_version",
                                      "original_size", "stored_size", "original_sha256", "stored_sha256")} for m in members],
    }
    temp = manifest_path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, manifest_path)
    return manifest_path
