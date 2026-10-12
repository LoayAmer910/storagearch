# Security

## Reporting a problem

Please report a security problem privately, not in a public issue: open the repository's **Security** tab on
GitHub and choose **Report a vulnerability**. The report is visible only to the maintainer.

A useful report says which version it is about (the number in the file's Properties > Details, or the release
tag), what you did, what happened, and what you expected.

Only the latest release is maintained; a fix is published as a new release.

## What the app does with your files

- Everything is local. Archives, the catalog and the workspace are in the `SmartArchiveData` folder next to
  `StorageArch.exe` (or in the folder you chose in Settings). The app works fully offline.
- A file counts as archived only after it was read back from the archive and matched its original SHA-256.
  A source is removed only when you asked for it, only after that check, and only if it did not change in
  the meantime.
- Nothing is extracted outside the folder it is meant for: names stored in an archive that point elsewhere
  (`..`, absolute or drive paths, reserved device names) are refused.
- Archives are not encrypted. Anyone who can read the archive files can read their content - keep them on a
  drive you trust, or inside an encrypted volume.

## The optional cloud part

It is off until you connect it and turn it on yourself. When it is on, the app sends anonymous usage statistics
only: which compression method won for which file type, sizes and durations. File names, paths, file contents,
hashes of your files, your user name and your computer's name are never sent. Before registration the app makes
no network request at all.

## Checking a download

Each release carries `StorageArch.exe.sha256`. To compare, in PowerShell:

    Get-FileHash StorageArch.exe -Algorithm SHA256

The file is not code-signed, so Windows SmartScreen may warn on first run. The release is built by GitHub
Actions from the tagged source, with pinned dependencies, after the full test suite and a test of the packaged
build itself.
