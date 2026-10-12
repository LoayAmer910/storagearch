"""StorageArch core: decides how to store each file, stores it, restores it and proves the restore.

The public entry point for the GUI
is core.api; everything else is internal.
"""

# The one version number of the app. The cloud client reports the same number (local_cloud/build_channel.py),
# the exe file carries it in its details (StorageArch.spec), and a release tag must be "v" + this number
# (tests/test_version.py and the release workflow check all three).
CORE_VERSION = "1.0.2"
