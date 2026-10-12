StorageArch is a Windows archiver that picks the best lossless compression for each file by what it contains, and keeps nothing it has not restored and verified byte for byte.

**Download `StorageArch.exe` below and run it** - one file, no installation, no Python needed.

**First run:** the file is not code-signed, so Windows SmartScreen may show a blue "Windows protected your PC" window. Choose **More info**, then **Run anyway**.

The app keeps its data in a `SmartArchiveData` folder next to `StorageArch.exe` (wherever it is started from), so it is best to put the file in a folder of its own first. It works fully offline; the optional cloud part is off by default.

**What is new in 1.0.1**

- A source or a checked-out copy is removed only if it did not change while it was being saved; changes made in the meantime are saved too.
- A crash or a power cut in the middle of a write leaves the archive and the catalog consistent; verifying never removes anything: entries an old crash left in an archive are reported, and removed only if you say so.
- Only one copy of the app works on a data folder at a time.
- The data folder can be moved to another place or drive and is found again, and the data always sits next to the EXE file, whatever folder the app is started from.
- Long operations show their progress and can be cancelled; a cancelled operation leaves nothing half done.
- Free space is checked before the work starts, on the drive that is written to.
- A folder that holds something that cannot be read (a path that is too long, no permission) is not archived in part: the app says what is wrong.
- The window stays responsive while items are returned on closing and while it checks for updates.
- A log file is kept in the data folder, and the file's Properties show its version.
- No network request is made before you connect the optional cloud part.
- Data written by 1.0.0 opens in 1.0.1 as it is.

This build was made by GitHub Actions on a clean Windows machine from the tagged source: the full test suite ran first, then the packaged build itself archived, restored and verified sample files before it was attached here. `StorageArch.exe.sha256` holds its checksum.
