"""Single source of truth for everything that must be fixed at build time
per plan §23 ("Environment is fixed at build time... A Development Build
cannot accidentally send to Beta. There is no runtime/user-facing switch.")

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
APP_VERSION = "0.1.0-beta"
BUILD_VERSION = "beta9-cloud-connect-20260821"

BASE_URL = "https://storagearch-api-beta.storagearch-worker-public.workers.dev"

# The PUBLIC half of this channel's Ed25519 signing keypair only (plan §29:
# "The EXE contains only public keys") - safe to embed in source/commit
# either way. The matching private key is a Cloudflare Worker secret and
# must never appear anywhere in this repository, in either channel's value.
SIGNING_PUBLIC_KEY_SPKI_B64 = "MCowBQYDK2VwAyEAeUQJe7gCafz6vsvlvH5wq9FIEUCAChWXiGnFSHLVI70="
