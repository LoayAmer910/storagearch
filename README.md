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

    python -m pytest -x tests_core

About 550 tests: the codecs, the router and the selection policy, the archive format and crash recovery,
the API, whole workflows, folders of many small files, the GUI driven like a user (including mistakes:
locked files, deleted copies, odd names, busy clicks), and the cloud client against a local stand-in
server. `tests_core/test_00_smoke.py` runs first. GitHub Actions runs the smoke tests and then the full
suite on Windows for every push.

Two parts compare with, or import from, the previous version and run only when
`STORAGEARCH_OLD_ENGINE_DIR` points at a checkout of it: `tests_core/test_09_import.py` and
`tests_core/differential/run_differential.py`.

## Importing data from the previous version

    python scripts/import_old_data.py OLD_DATA_ROOT [TARGET_DIR]

The old data is only read, never modified.

## License

[MIT](LICENSE)
