"""A deterministic corpus for the core tests, built from scratch: one file of every kind the core
distinguishes, plus the traps (text that starts like a binary signature, truncated and corrupt files,
size boundaries). build() returns {name: Sample}; the expected kind is part of each sample."""
import gzip
import io
import bz2
import lzma
import os
import random
import sqlite3
import struct
import zipfile
from dataclasses import dataclass
from compression import zstd

from PIL import Image

from core.content import ContentKind as K

_WORDS = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa")


@dataclass(frozen=True)
class Sample:
    name: str
    path: str
    kind: K
    compressible: bool       # whether a good general-purpose compressor should beat plain Deflate on it


def lines(n, template, seed=7):
    rng = random.Random(seed)
    return "".join(template.format(i=i, w=rng.choice(_WORDS), n=rng.randrange(1000)) for i in range(n)).encode()


def photo(width, height, seed):
    rng = random.Random(seed)
    img = Image.new("RGB", (width, height))
    px = img.load()
    for x in range(width):
        for y in range(height):
            g = rng.randrange(-8, 9)
            px[x, y] = (max(0, min(255, x * 255 // width + g)), max(0, min(255, y * 255 // height + g)),
                        max(0, min(255, (x + y) * 255 // (width + height) + g)))
    return img


def image_bytes(img, fmt, **kwargs):
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def ooxml(root_file):
    return zip_bytes({
        "[Content_Types].xml": "<Types/>" * 50,
        root_file: lines(400, "<p id='{i}'>{w} {n}</p>\n").decode(),
        "docProps/core.xml": "<core/>" * 30,
    })


def pe_executable():
    dos = bytearray(b"MZ" + b"\x90\x00" * 29)
    dos += b"\x00" * (0x3C - len(dos)) + struct.pack("<I", 0x80)
    dos += b"\x00" * (0x80 - len(dos)) + b"PE\x00\x00" + b"\x4c\x01" + b"\x00" * 18
    code = struct.pack("<I", 0xE8) * 9000 + bytes(range(256)) * 60
    return bytes(dos) + code


def sqlite_bytes(folder):
    path = os.path.join(folder, "_tmp.sqlite")
    db = sqlite3.connect(path)
    db.execute("create table t (id integer primary key, name text, value integer)")
    db.executemany("insert into t (name, value) values (?, ?)", [(_WORDS[i % 10] * 3, i) for i in range(3000)])
    db.commit()
    db.close()
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    return data


def build(root):
    os.makedirs(root, exist_ok=True)
    samples = {}

    def add(name, data, kind, compressible):
        path = os.path.join(root, name)
        with open(path, "wb") as f:
            f.write(data)
        samples[name] = Sample(name, path, kind, compressible)

    add("text.log", lines(600, "2026-09-26 12:00:{n:02d} INFO request {i} path=/api/{w}/{n} status=200\n"), K.TEXT, True)
    add("text.csv", b"id,name,region,amount\n" + lines(900, "{i},{w},{w},{n}.50\n"), K.TEXT, True)
    add("text.json", b"[" + b",".join(b'{"id": %d, "tag": "%s"}' % (i, _WORDS[i % 10].encode()) for i in range(700)) + b"]", K.TEXT, True)
    add("text.py", lines(400, "def function_{i}(value):\n    return value + {n}  # {w}\n\n"), K.TEXT, True)
    add("text.md", lines(500, "## Section {i}\n\nSome {w} text about item {n}.\n\n"), K.TEXT, True)
    # Hebrew on purpose: UTF-8 text in a non-Latin script must still be recognized as text
    add("text_utf8.txt", ("שורה {} בעברית עם מילים חוזרות ונשנות\n" * 400).encode("utf-8"), K.TEXT, True)
    add("text_utf16.txt", "﻿".encode("utf-16-le") + ("line of utf-16 text\r\n" * 300).encode("utf-16-le"), K.TEXT, True)
    add("text_latin1.txt", ("caf\xe9 cr\xe8me br\xfbl\xe9e number {}\n" * 300).encode("latin-1"), K.TEXT, True)
    add("trap_bm_text.txt", b"BMW service report\n" + lines(300, "car {i} {w} {n}\n"), K.TEXT, True)
    add("trap_mz_text.txt", b"MZ notes: " + lines(300, "entry {i} {w} {n}\n"), K.TEXT, True)
    add("trap_pk_text.txt", b"PK is short for primary key\n" + lines(300, "row {i} {w}\n"), K.TEXT, True)
    add("image.png", image_bytes(photo(160, 120, 1), "PNG"), K.PNG, False)
    add("image.gif", image_bytes(photo(120, 90, 2), "GIF"), K.GIF, False)
    add("image.tiff", image_bytes(photo(128, 96, 3), "TIFF"), K.TIFF, True)
    add("image.bmp", image_bytes(photo(200, 150, 4), "BMP"), K.BMP, True)
    add("image_rgba.bmp", image_bytes(photo(64, 48, 5).convert("RGBA"), "BMP"), K.BMP, True)
    add("image_odd_width.bmp", image_bytes(photo(33, 20, 6), "BMP"), K.BMP, True)
    add("image.jpg", image_bytes(photo(320, 240, 7), "JPEG", quality=90), K.JPEG, False)
    add("image_progressive.jpg", image_bytes(photo(160, 120, 8), "JPEG", quality=85, progressive=True), K.JPEG, False)
    add("image.webp", image_bytes(photo(120, 90, 9), "WEBP", lossless=True), K.WEBP, False)
    add("video.mp4", b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + random.Random(1).randbytes(20000), K.VIDEO, False)
    add("video.mkv", b"\x1a\x45\xdf\xa3" + random.Random(2).randbytes(20000), K.VIDEO, False)
    add("video.avi", b"RIFF\x00\x00\x00\x00AVI LIST" + random.Random(3).randbytes(20000), K.VIDEO, False)
    add("image.avif", b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00" + random.Random(4).randbytes(9000), K.AVIF, False)
    add("image.heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + random.Random(5).randbytes(9000), K.HEIC, False)
    add("doc.pdf", b"%PDF-1.4\n" + lines(500, "BT /F1 12 Tf ({w} {n} {i}) Tj ET\n") + b"\n%%EOF\n", K.PDF, True)
    add("db.sqlite", sqlite_bytes(root), K.SQLITE, True)
    add("bundle.zip", zip_bytes({"a.txt": lines(800, "{w} {n}\n")}), K.ZIP, False)
    add("bundle.jar", zip_bytes({"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n", "A.class": b"\xca\xfe\xba\xbe" * 50}), K.ZIP, False)
    add("corrupt.zip", b"PK\x03\x04" + random.Random(6).randbytes(5000), K.ZIP, False)
    add("bundle.gz", gzip.compress(lines(800, "{w} {n} {i}\n")), K.COMPRESSED, False)
    add("bundle.xz", lzma.compress(lines(800, "{w} {n} {i}\n")), K.COMPRESSED, False)
    add("bundle.bz2", bz2.compress(lines(800, "{w} {n} {i}\n")), K.COMPRESSED, False)
    add("bundle.zst", zstd.compress(lines(800, "{w} {n} {i}\n")), K.COMPRESSED, False)
    add("office.docx", ooxml("word/document.xml"), K.DOCX, False)
    add("office.xlsx", ooxml("xl/workbook.xml"), K.XLSX, False)
    add("office.pptx", ooxml("ppt/presentation.xml"), K.PPTX, False)
    add("program.exe", pe_executable(), K.EXECUTABLE, True)
    add("program.elf", b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + struct.pack("<I", 0xE8) * 5000, K.EXECUTABLE, True)
    add("dos.com", b"MZ" + bytes(range(256)) * 40, K.EXECUTABLE, True)
    add("random.bin", random.Random(5).randbytes(24000), K.BINARY, False)
    add("empty.txt", b"", K.EMPTY, False)
    add("one_byte.txt", b"x", K.TEXT, False)
    add("truncated.jpg", b"\xff\xd8\xff", K.JPEG, False)
    add("boundary_4095.dat", (b"abcdefghij" * 410)[:4095], K.TEXT, True)
    add("boundary_4096.dat", (b"abcdefghij" * 410)[:4096], K.TEXT, True)
    add("large_text.txt", lines(30000, "record {i} {w} {n} some repeated payload text\n"), K.TEXT, True)
    add("large_random.bin", random.Random(9).randbytes(1_500_000), K.BINARY, False)
    return samples
