"""The only bridge between the core and the optional cloud client (`local_cloud`, kept as it is).

Local first (I8): an event is handed over only after the local result is committed, it is built and sent
on a background thread, and nothing that happens there - an exception, a hang, a missing package - can
change or delay a local result. Payloads carry counts, sizes, durations, method names and a file-type
label; never a file name, a path or file contents. When the package is missing, every function here is
a harmless no-op that returns the "unavailable" value the GUI already understands.
"""
import queue
import re
import threading

_sender_module = _installation = _remote_config = _retention = _schemas = _telemetry = _updates = None
CLOUD_AVAILABLE = False


def connect():
    """Finds the cloud client (the local_cloud package next to the core). Called at startup; without the
    package the app simply runs with cloud features unavailable. Returns whether the cloud is available."""
    global _sender_module, _installation, _remote_config, _retention, _schemas, _telemetry, _updates, CLOUD_AVAILABLE
    if CLOUD_AVAILABLE:
        return True
    try:
        from local_cloud import background_sender, installation_service, remote_config_service, retention, schemas
        from local_cloud import telemetry_service, update_service
    except Exception:  # noqa: BLE001 - any import problem simply means "no cloud"
        return False
    _sender_module, _installation, _remote_config, _retention = background_sender, installation_service, remote_config_service, retention
    _schemas, _telemetry, _updates = schemas, telemetry_service, update_service
    CLOUD_AVAILABLE = True
    return True


connect()

_QUEUE_LIMIT = 1000
_queue = queue.Queue(maxsize=_QUEUE_LIMIT)
_worker = None
_worker_lock = threading.Lock()
_sender = None
_disabled = frozenset()
_FILE_TYPE = re.compile(r"^[a-z0-9]{1,10}$")


def file_type_label(name_or_ext):
    """A short, safe label (the extension) - or 'other'; never anything that could identify a file."""
    ext = str(name_or_ext or "").rsplit(".", 1)[-1].lower() if "." in str(name_or_ext or "") else ""
    return ext if _FILE_TYPE.match(ext) else ("unknown" if not ext else "other")


# --- the background lane -------------------------------------------------------------------------------

def _run(lane):
    while True:
        job = lane.get()
        try:
            if job is None:
                return
            job()
        except Exception:  # noqa: BLE001 - nothing from the cloud side may escape
            pass
        finally:
            lane.task_done()


def _submit(job):
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, args=(_queue,), name="cloud-events", daemon=True)
            _worker.start()
    try:
        _queue.put_nowait(job)
    except queue.Full:
        pass  # dropping telemetry is always acceptable; blocking the caller is not


def reset():
    """Starts a fresh lane, forgets cached remote config and closes the sender. A job still stuck on the old
    lane (a hanging cloud client) keeps its daemon thread but can no longer delay anything queued after this."""
    global _queue, _worker, _disabled, _sender
    with _worker_lock:
        old, _queue, _worker = _queue, queue.Queue(maxsize=_QUEUE_LIMIT), None
    _disabled = frozenset()
    sender, _sender = _sender, None  # its queues belong to the cloud home it was opened with
    if sender is not None:
        try:
            sender.shutdown(timeout=0.5)
        except Exception:  # noqa: BLE001
            pass
    try:
        old.put_nowait(None)  # lets the old worker end once it is free
    except queue.Full:
        pass


def flush(timeout=5.0):
    """Waits (bounded) until queued events are handled. For shutdown and tests."""
    done = threading.Event()
    _submit(done.set)
    return done.wait(timeout)


# --- sending -------------------------------------------------------------------------------------------

def _get_sender():
    global _sender
    if not CLOUD_AVAILABLE:
        return None
    if _sender is None:
        try:
            _sender = _sender_module.BackgroundSender(_installation.get_credentials)
            _sender.start()
        except Exception:  # noqa: BLE001
            _sender = None
    return _sender


def report_operation(event, metrics=None):
    """`event`: the kwargs of telemetry_service.record_operation_event, built from a committed result.
    `metrics`: optional list of dicts for schemas.build_smart_aggregate_row - built on the background lane,
    inside its guard (in the old version a failure there turned a successful archive into an error)."""
    if not CLOUD_AVAILABLE:
        return

    def job():
        rows = None
        if metrics:
            rows = [_schemas.build_smart_aggregate_row(**m) for m in metrics]
        _telemetry.record_operation_event(**dict(event, smart_metrics=rows, sender=_get_sender()))
    _submit(job)


def start():
    """At startup: app-started event, remote config refresh and queue retention - all in the background."""
    if not connect():
        return
    _submit(lambda: _telemetry.record_app_started(sender=_get_sender()))
    _submit(_remote_config.fetch_and_verify)
    _submit(_refresh_disabled)
    _submit(_retention.prune_expired_queues)


# Remote config (the admin dashboard) names strategies as the old version did; each old name disables the
# new strategy that does the same job, so a setting made there keeps working.
OLD_STRATEGY_NAMES = {
    "generic_zstd": "zstd", "zstd_15": "zstd", "zstd_long_range": "zstd_ldm", "zstd_long_range_v2": "zstd_ldm",
    "generic_lzma": "lzma", "lzma_x86": "lzma_x86", "generic_zlib": "zlib", "generic_brotli": "brotli",
    "brotli_q9": "brotli", "jpeg_xl_lossless_jpeg": "jpeg_xl", "bmp_jxl_lossless": "bmp_jxl",
}


def _refresh_disabled():
    global _disabled
    names = _remote_config.disabled_strategies_set()
    if isinstance(names, (set, frozenset, list, tuple)):
        _disabled = frozenset(OLD_STRATEGY_NAMES.get(n, n) for n in names if isinstance(n, str))


def disabled_strategies():
    """Strategy names remote config disables - it can only narrow the candidate list. Returns the last known
    value at once and refreshes it on the background lane: a slow or hanging cloud client must never sit on
    an archive's critical path (found by the I8 test: a 3 s hang there delayed every archive by 3 s)."""
    if not CLOUD_AVAILABLE:
        return frozenset()
    _submit(_refresh_disabled)
    return _disabled


def shutdown(timeout=1.0):
    global _sender
    flush(timeout)
    if _sender is not None:
        try:
            _sender.shutdown(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        _sender = None


# --- the GUI's cloud facade (same names and return values as before) -----------------------------------

def _guarded(default, fn, *args, **kwargs):
    if not CLOUD_AVAILABLE:
        return default
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return default


def cloud_available():
    return CLOUD_AVAILABLE


def cloud_is_registered():
    return _guarded(False, lambda: _installation.is_registered())


def cloud_register(beta_code):
    if not CLOUD_AVAILABLE:
        return False, "cloud_unavailable"
    return _guarded((False, "unexpected_error"), lambda: _installation.register(beta_code))


def cloud_disconnect():
    """True once the local connection is gone; False if it could not be removed (it is then left whole)."""
    return bool(_guarded(False, lambda: _installation.disconnect()))


def cloud_telemetry_enabled():
    return _guarded(False, lambda: _telemetry.is_consent_on())


def cloud_set_telemetry_enabled(enabled):
    _guarded(None, lambda: _telemetry.set_consent(enabled, sender=_get_sender()))


def cloud_submit_feedback(category, rating=None, feedback_text=None, include_diagnostics=False, beta_code=None):
    if not CLOUD_AVAILABLE:
        return False, "cloud_unavailable"

    def submit():
        from local_cloud import feedback_service
        snapshot = None
        if include_diagnostics:
            try:
                from local_cloud import system_info
                snapshot = {"windows_version": system_info.windows_version(), "architecture": system_info.architecture()}
            except Exception:  # noqa: BLE001
                snapshot = None
        return feedback_service.submit_feedback(category, rating=rating, feedback_text=feedback_text,
                                                include_diagnostics=include_diagnostics, diagnostic_snapshot=snapshot,
                                                beta_code=beta_code, sender=_get_sender())
    return _guarded((False, "unexpected_error"), submit)


def cloud_check_for_updates():
    return _guarded(None, lambda: _updates.check_latest_release())


def cloud_report_crash(exception, component=None, function_name=None, safe_code_location=None):
    def report():
        from local_cloud import crash_service
        crash_service.report_crash(exception, component=component, function_name=function_name,
                                   safe_code_location=safe_code_location, sender=_get_sender())
    _guarded(None, report)
