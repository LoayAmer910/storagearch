# StorageArch

A Windows desktop archiver that chooses the best lossless compression for each file by what the file
contains, and stores nothing it has not restored and verified byte for byte.

## Requirements

- Windows, Python 3.14+
- `pip install -r requirements.txt` (development and tests: `requirements-dev.txt`)
- Optional: the JPEG XL tools `cjxl.exe` / `djxl.exe` in a `tools` folder next to the code, or in the folder
  named by `STORAGEARCH_JXL_TOOLS_DIR`. Without them, JPEG and BMP files use the general compressors.

## Run

    python app.py

## Tests

    python -m pytest -x tests_core

`tests_core/test_00_smoke.py` runs first. Two parts compare with, or import from, the previous version and
run only when `STORAGEARCH_OLD_ENGINE_DIR` points at a checkout of it: `tests_core/test_09_import.py` and
`tests_core/differential/run_differential.py`.

## Importing data from the previous version

    python scripts/import_old_data.py OLD_DATA_ROOT [TARGET_DIR]

The old data is only read, never modified.
