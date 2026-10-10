StorageArch is a Windows archiver that picks the best lossless compression for each file by what it contains, and keeps nothing it has not restored and verified byte for byte.

**Download `StorageArch.exe` below and run it** - one file, no installation, no Python needed.

**First run:** the file is not code-signed, so Windows SmartScreen may show a blue "Windows protected your PC" window. Choose **More info**, then **Run anyway**.

The app keeps its data in a `SmartArchiveData` folder next to where it is started, so it is best to put `StorageArch.exe` in a folder of its own first. It works fully offline; the optional cloud part is off by default.

This build was made by GitHub Actions on a clean Windows machine from the tagged source: the full test suite ran first, then the packaged build itself archived, restored and verified sample files before it was attached here. `StorageArch.exe.sha256` holds its checksum.
