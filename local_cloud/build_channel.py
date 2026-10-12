"""Single source of truth for everything that must be fixed at build time:
the environment a build talks to is decided here, and there is no runtime or
user-facing switch.

This is the ONLY module in local_cloud/ whose values differ between a
Development build and a Beta build - `local_state.py` (RELEASE_CHANNEL/
APP_VERSION/BUILD_VERSION), `cloud_client.py` (BASE_URL), and
`metadata_signing.py` (SIGNING_PUBLIC_KEY_SPKI_B64) all import from here
instead of hardcoding their own values, so a channel build only ever means
editing this one file, never touching application logic.

Building a Beta package: temporarily replace the four values below with
the real Beta ones (Beta Worker URL, Beta's Ed25519 PUBLIC key only - never
a private key or any secret), run the build, then revert this file back to
the Development values checked in here by default. There is still no
runtime switch - whatever this file said at PyInstaller freeze time is
permanently what that specific EXE talks to; nothing in the running app
ever reads an env var or a setting to pick a server.
"""

RELEASE_CHANNEL = "beta"
APP_VERSION = "1.0.2"       # the same number as core.CORE_VERSION (tests/test_version.py)
BUILD_VERSION = "1.0.2"

BASE_URL = "https://storagearch-api-beta.storagearch-worker-public.workers.dev"

# The PUBLIC half of this channel's Ed25519 signing keypair only (the EXE
# contains only public keys) - safe to embed in source/commit
# either way. The matching private key is a Cloudflare Worker secret and
# must never appear anywhere in this repository, in either channel's value.
SIGNING_PUBLIC_KEY_SPKI_B64 = "MCowBQYDK2VwAyEAeUQJe7gCafz6vsvlvH5wq9FIEUCAChWXiGnFSHLVI70="
