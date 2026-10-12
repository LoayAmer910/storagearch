"""StorageArch local Cloud client.

Additive, isolated from the core - nothing in this package is imported by
the core's modules, and nothing in them is required to import this package
either (core/events.py is the only bridge).

Every public entry point in this package is designed to never raise into a
caller performing a real local operation - Cloud failure must never change
a local Archive/Verify/Checkout/Checkin result. Internal errors are caught,
logged via `print` only (no local Cloud logging table - the design does not
require one for the client), and treated as "Cloud did nothing this time".
"""
