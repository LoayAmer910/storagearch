"""StorageArch local Cloud client (C5-C7).

Additive, isolated from the core archive/Smart-compression engines - nothing
in this package is imported by archive_manager.py, archive_manager_smart.py,
smart_compression.py, or database_manager.py, and nothing in those modules
is required to import this package either (main.py is the only bridge, at
the orchestration boundary, per STORAGEARCH_BETA_CLOUD_PLAN.md).

Every public entry point in this package is designed to never raise into a
caller performing a real local operation - Cloud failure must never change
a local Archive/Verify/Checkout/Checkin result. Internal errors are caught,
logged via `print` only (no local Cloud logging table - plan does not
require one for the client), and treated as "Cloud did nothing this time".
"""
