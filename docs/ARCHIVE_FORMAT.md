# The archive format

An archive is an ordinary ZIP file (ZIP64 when it needs to be) with a manifest next to it. What follows is
enough to take any file out of an archive without StorageArch and without its database.

## Where things are

    SmartArchiveData/
      archives/<category folder>/<category>_001.zip            the archive
      archives/<category folder>/<category>_001.manifest.json  its listing
      smartarchive.db                                          the catalog (SQLite)
      workspace/                                               checked-out copies

Inside an archive every item has its own top folder, named by a random 12-character id (a later version of
the item, written by a check-in, adds a dot and six more characters):

    3f9c2a7b1d04/report.txt             a file item
    8a61e0c5d7f2/project/               a folder item, with its files and empty folders below it
    8a61e0c5d7f2/project/src/main.py

## The two kinds of member

**Plain members** are normal ZIP entries, compressed with Deflate, or stored as they are when Deflate would not
make them smaller. Any ZIP tool extracts the real file.

**Smart members** end in `.sa`. They hold the output of a stronger compression method and are stored in the
ZIP without further compression. The entry's ZIP comment is a small JSON object that says how to get the
original back:

    {"s": "lzma", "v": 1, "sha256": "<SHA-256 of the original file>", "size": 1234567, "mtime": 1760000000.0}

| field | meaning |
|---|---|
| `s` | the method (see the table below) |
| `v` | the version of that method's stored format |
| `sha256` | SHA-256 of the original file - compare after restoring |
| `size` | size of the original file in bytes |
| `mtime` | modification time of the original file (seconds since 1970) |

A ZIP tool extracts a smart member as `name.sa`; that file must then be decoded with the method named in `s`.

## The methods

| `s` | what the `.sa` bytes are | restore with |
|---|---|---|
| `zstd`, `zstd_ldm` | a Zstandard frame (`zstd_ldm` was written with long-distance matching and a 128 MiB window) | `zstd -d --long=27` |
| `lzma` | an XZ container | `xz -d` |
| `lzma_x86` | an XZ container with the x86 branch filter | `xz -d` |
| `brotli` | a Brotli stream | `brotli -d` |
| `zlib` | a zlib stream | any zlib tool |
| `jpeg_xl` | a JPEG XL file made from a JPEG, losslessly | `djxl file.jxl file.jpg` gives back the identical JPEG |
| `bmp_jxl` | a small header, the bitmap's own header and trailer, then a JPEG XL file of its pixels | StorageArch (the layout is in `core/strategies/jpeg_xl.py`) |

The exact parameters of each method are in `core/strategies/`. Whatever tool is used, the result is right only
if its SHA-256 equals the `sha256` of the comment.

## The manifest

`<archive>.manifest.json` lists every member of the archive, for people and tools:

    {
      "manifest_version": 2,
      "archive_name": "Docs_001.zip",
      "category": "Docs",
      "state": "open",                    "open" (still written to) or "sealed" (full; never changed again)
      "archive_sha256": null,             SHA-256 of the whole file, once it is sealed
      "files": [
        {"path_in_archive": "3f9c2a7b1d04/report.txt.sa", "relative_path": "report.txt", "kind": "file",
         "method": "lzma", "method_version": 1, "original_size": 1234567, "stored_size": 345678,
         "original_sha256": "...", "stored_sha256": "..."}
      ]
    }

The manifest is a convenience: the archive itself carries everything needed (the names, and the comment of
every smart member).

## What the format does not have

- No encryption and no password.
- No solid compression across files: every member can be taken out on its own.
- An open archive may hold entries of older versions of an item until it is tidied; the catalog and the
  manifest name the current ones.
