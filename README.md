# StorageArch

[![Tests](https://github.com/LoayAmer910/storagearch/actions/workflows/tests.yml/badge.svg)](https://github.com/LoayAmer910/storagearch/actions/workflows/tests.yml)

A Windows desktop archiver that chooses the best lossless compression for each file by what the file
contains, and stores nothing it has not restored and verified byte for byte.

![The Items tab](assets/screenshot.png)

## How it works

1. **Read once.** Each file is read a single time: its real type is taken from its bytes (not its name),
   its SHA-256 is computed, and the size of a plain ZIP (Deflate) member is measured - the baseline.
2. **Try what fits.** A router picks the candidates for that type - Brotli, zstd or LZMA for text (by
   size), LZMA with the x86 filter for EXE/DLL, JPEG XL for JPEG and BMP; already-compressed formats
   (PNG, video, ZIP) get only a cheap probe. Every candidate is compressed **and restored**, and the
   restored bytes must match the original SHA-256.
3. **Keep it only if it pays.** A verified candidate wins only if it beats the ZIP baseline by at least
   3 % or 1 MiB; otherwise the file is stored as a normal ZIP member.

Archives are standard ZIP files with a manifest next to them. Items can be checked out to a workspace,
edited and checked in (a folder writes only the files that changed), and every archive can be verified
at any time.

## Results

A 1.70 GiB folder of real files (photos, bitmaps, code, logs, CSV/JSON/XML, DLLs/EXEs, a SQLite
database, audio, video, documents - 2,329 files):

| | Size | Time |
|---|---:|---:|
| Original | 1,744 MiB | |
| ZIP (7-Zip, normal) | 489 MiB | 17 s |
| ZIP (7-Zip, ultra) | 480 MiB | 142 s |
| **StorageArch** | **406 MiB (-76.7 %)** | 85 s |
| 7z (7-Zip, normal) | 366 MiB | 98 s |

21.7 % smaller than ZIP, while every file stays individually addressable inside a standard ZIP. By type,
against ZIP: BMP -55.8 %, JavaScript -31.8 %, Python extension modules -30.3 %, DLLs -24.3 %, JPEG -22.2 %,
JSON/XML -17.2 %. No file was stored larger than its ZIP baseline, and every restore was identical to the
original.

## The optional cloud part

The app works fully offline. The cloud client is optional and off by default: it lets the compression rules be
improved from anonymous usage statistics (which method won for each file type, and by how much), and lets a
compression method found to be faulty be switched off remotely through a signed setting - it can only remove a
method, never add one - without shipping a new release. File names, paths and file contents are never sent.

## Requirements

- Windows, Python 3.14+
- `pip install -r requirements.txt` (development and tests: `requirements-dev.txt`)
- Optional: the JPEG XL tools `cjxl.exe` / `djxl.exe` (from [libjxl](https://github.com/libjxl/libjxl/releases))
  in a `tools` folder next to the code, or in the folder named by `STORAGEARCH_JXL_TOOLS_DIR`. Without
  them, JPEG and BMP files use the general compressors.

## Run

    python app.py

## Build the Windows app (EXE)

    pip install pyinstaller
    python -m PyInstaller StorageArch.spec --noconfirm

The single-file app is written to `dist/StorageArch.exe`; the JPEG XL tools are bundled when they are in
`tools`.

## Tests

    python -m pytest tests/test_smoke.py
    python -m pytest

The tests are built in six layers, each catching a different kind of failure:

1. **Smoke** - runs first, in a second: the right code is loaded and nothing depends on the previous version.
2. **Module by module** - every module of the core, the window and the cloud client, error paths included.
3. **The eight rules** - each design rule (I1-I8) checked directly through the core's API.
4. **Fault injection** - compressors that crash, hang or return wrong bytes; disks and databases that fail.
5. **User journeys** - the real window, mistakes included, checking after every step that nothing was lost.
6. **Against the previous version** - the same real files through both versions, and importing old data.

All tests live in `tests/`, one file per source module, in folders that mirror the code:

| Folder | What it checks |
|---|---|
| `tests/core/` | each core module - content detection, codecs, routing, the selection policy, the decision pipeline, the archive format and crash recovery, the catalog, workers and timeouts |
| `tests/core/api/` | the API the window calls, by area: categories, locations, archiving, check out and check in |
| `tests/gui/` | each part of the window, driven like a user - including mistakes: locked files, deleted copies, odd names, busy clicks |
| `tests/local_cloud/` | each module of the optional cloud client, against a local stand-in server: consent, signatures, queues, retries |
| `tests/scripts/` | importing data from the previous version, from old-format data the tests build themselves |
| `tests/contracts/` | the eight rules the whole system keeps (I1-I8), checked through the API |
| `tests/e2e/` | whole journeys: through the real window, and with the cloud client against a local stand-in server |
| `tests/support/` | shared helpers and fixtures: the generated sample files, fake strategies, GUI drivers |

The sample files are generated by the tests themselves (`tests/support/corpus.py`); nothing large is stored in
the repository. GitHub Actions runs the smoke tests and then the full suite on Windows for every push.

A few checks import from, or compare with, the previous version itself and run only when
`STORAGEARCH_OLD_ENGINE_DIR` points at a checkout of it: the tests marked `needs_old_engine` in
`tests/scripts/test_import_old_data.py`, and `tests/differential/run_differential.py`.

## Importing data from the previous version

    python scripts/import_old_data.py OLD_DATA_ROOT [TARGET_DIR]

The old data is only read, never modified.

## License

[MIT](LICENSE)
