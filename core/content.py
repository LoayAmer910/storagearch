"""What a file contains, decided from its bytes only.

This is the one place that looks at file contents to classify them. Nothing here reads the file name:
the same bytes under any name give the same ContentInfo. Everything downstream (routing, strategies)
receives a ContentInfo, never a name.
"""
import hashlib
import struct
import zipfile
from compression import zstd
from dataclasses import dataclass
from enum import Enum

SAMPLE_BYTES = 64 * 1024
PROBE_BYTES = 1024 * 1024
_READ_CHUNK = 1024 * 1024


class ContentKind(Enum):
    EMPTY = "empty"
    TEXT = "text"
    JPEG = "jpeg"
    PNG = "png"
    GIF = "gif"
    TIFF = "tiff"
    BMP = "bmp"
    WEBP = "webp"
    AVIF = "avif"
    HEIC = "heic"
    VIDEO = "video"
    PDF = "pdf"
    SQLITE = "sqlite"
    COMPRESSED = "compressed"          # gzip, xz, bzip2, zstd, 7z, rar: already-compressed streams
    ZIP = "zip"                        # a ZIP that is not an Office document (plain zip, jar, apk, odf...)
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    EXECUTABLE = "executable"          # PE (exe/dll), ELF, Mach-O
    BINARY = "binary"                  # nothing recognized, not text


@dataclass(frozen=True)
class ContentInfo:
    kind: ContentKind
    size: int
    sha256: str
    detail: str = ""                   # a sub-type worth logging ("pe", "elf", "utf-16", "mp4"...), never a name
    probe_saving: float = 0.0          # % that fast zstd (level 1) saves on the first 1 MiB: a cheap hint of
                                       # whether compressing is worth trying at all


def classify(path, on_chunk=None):
    """Reads the file once: SHA-256 of all of it, classification from its first bytes (and, for a ZIP,
    its central directory). `on_chunk`, if given, sees every chunk of that same read (so a caller can
    measure something else without reading the file twice). Raises OSError if the file cannot be read."""
    digest = hashlib.sha256()
    head = b""
    probe = bytearray()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_READ_CHUNK)
            if not chunk:
                break
            if len(head) < SAMPLE_BYTES:
                head += chunk[:SAMPLE_BYTES - len(head)]
            if len(probe) < PROBE_BYTES:
                probe += chunk[:PROBE_BYTES - len(probe)]
            digest.update(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
            size += len(chunk)
    kind, detail = detect(head, size)
    if kind is ContentKind.ZIP:
        kind, detail = _office_or_zip(path)
    return ContentInfo(kind=kind, size=size, sha256=digest.hexdigest(), detail=detail, probe_saving=probe_saving(bytes(probe)))


def probe_saving(sample):
    """Percent saved by zstd level 1 on the sample, rounded to 0.1 (so the value is stable)."""
    if not sample:
        return 0.0
    return round(max(0.0, (1 - len(zstd.compress(sample, level=1)) / len(sample)) * 100), 1)


def detect(head, size):
    """Classifies from the first bytes. Returns (kind, detail). A ZIP is returned as ZIP here; classify()
    then looks inside it."""
    if size == 0:
        return ContentKind.EMPTY, ""

    strong = _strong_signature(head, size)
    if strong is not None:
        return strong

    text_detail = _text_encoding(head, complete=size <= len(head))
    if text_detail is not None:
        return ContentKind.TEXT, text_detail

    if head.startswith(b"MZ") and size >= 64:
        return ContentKind.EXECUTABLE, "dos"  # an MZ header without a PE part: still a program image
    return ContentKind.BINARY, ""


# --- signatures --------------------------------------------------------------------------------------

_COMPRESSED_MAGIC = (
    (b"\x1f\x8b", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"(\xb5/\xfd", "zstd"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
)
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}


def _strong_signature(head, size):
    """Signatures specific enough that no text file starts with them, each checked with enough structure
    that a coincidence is very unlikely."""
    if head.startswith(b"\xff\xd8\xff"):
        return ContentKind.JPEG, ""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ContentKind.PNG, ""
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ContentKind.GIF, ""
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ContentKind.TIFF, ""
    if head.startswith(b"RIFF") and len(head) >= 12:
        form = head[8:12]
        if form == b"WEBP":
            return ContentKind.WEBP, ""
        if form in (b"AVI ", b"AVIX"):
            return ContentKind.VIDEO, "avi"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _AVIF_BRANDS:
            return ContentKind.AVIF, ""
        if brand in _HEIF_BRANDS:
            return ContentKind.HEIC, ""
        return ContentKind.VIDEO, "iso-bmff"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ContentKind.VIDEO, "matroska"
    if head.startswith(b"%PDF-"):
        return ContentKind.PDF, ""
    if head.startswith(b"SQLite format 3\x00"):
        return ContentKind.SQLITE, ""
    for magic, detail in _COMPRESSED_MAGIC:
        if head.startswith(magic):
            return ContentKind.COMPRESSED, detail
    if head[:3] == b"BZh" and len(head) >= 4 and head[3:4] in b"123456789" and head[4:10] == b"1AY&SY":
        return ContentKind.COMPRESSED, "bzip2"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return ContentKind.ZIP, ""
    if head.startswith(b"\x7fELF"):
        return ContentKind.EXECUTABLE, "elf"
    if head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        return ContentKind.EXECUTABLE, "mach-o"
    if head.startswith(b"MZ") and _has_pe_header(head):
        return ContentKind.EXECUTABLE, "pe"
    if head.startswith(b"BM") and _plausible_bmp(head, size):
        return ContentKind.BMP, ""
    return None


def _has_pe_header(head):
    if len(head) < 64:
        return False
    (pe_offset,) = struct.unpack_from("<I", head, 0x3C)
    return 64 <= pe_offset <= len(head) - 4 and head[pe_offset:pe_offset + 4] == b"PE\x00\x00"


_BMP_INFO_SIZES = {12, 40, 52, 56, 64, 108, 124}


def _plausible_bmp(head, size):
    """'BM' alone is common in text ("BMW..."); a real BMP also has a known info-header size, a pixel
    offset inside the file, and non-zero dimensions."""
    if len(head) < 26:
        return False
    pixel_offset, info_size = struct.unpack_from("<II", head, 10)
    if info_size not in _BMP_INFO_SIZES or not 14 + info_size <= pixel_offset <= size:
        return False
    if info_size == 12:
        width, height = struct.unpack_from("<HH", head, 18)
    else:
        width, height = struct.unpack_from("<ii", head, 18)
    return width > 0 and height != 0


# --- text --------------------------------------------------------------------------------------------

_ALLOWED_CONTROL = {0x09, 0x0A, 0x0C, 0x0D, 0x1B}


def _text_encoding(head, complete):
    """Returns an encoding label if the sample is text, else None. `complete` = the sample is the whole
    file (so a multi-byte character cut at the end of the sample is not an excuse)."""
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16" if _decodes(head[2:], "utf-16-le" if head[0] == 0xFF else "utf-16-be", complete) else None
    body = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
    if b"\x00" in body:
        return None
    try:
        text = body.decode("utf-8") if complete else _decode_prefix(body)
    except UnicodeDecodeError:
        # Not UTF-8: an 8-bit legacy encoding is still text if control characters are rare.
        controls = sum(1 for b in body if b < 0x20 and b not in _ALLOWED_CONTROL)
        return "8-bit" if controls <= len(body) // 100 else None
    controls = sum(1 for ch in text if ord(ch) < 0x20 and ord(ch) not in _ALLOWED_CONTROL)
    return "utf-8" if controls <= len(text) // 100 else None


def _decode_prefix(body):
    """UTF-8 decode that tolerates only a character cut by the end of the sample (at most 3 bytes)."""
    for cut in range(0, 4):
        try:
            return body[:len(body) - cut].decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.start < len(body) - 4:
                raise
    raise UnicodeDecodeError("utf-8", body, 0, len(body), "invalid")


def _decodes(data, encoding, complete):
    if not complete:
        data = data[:len(data) - (len(data) % 2)]
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return False
    controls = sum(1 for ch in text if ord(ch) < 0x20 and ord(ch) not in _ALLOWED_CONTROL)
    return controls <= len(text) // 100


# --- ZIP containers ----------------------------------------------------------------------------------

_OFFICE_MARKERS = (
    ("word/document.xml", ContentKind.DOCX),
    ("xl/workbook.xml", ContentKind.XLSX),
    ("ppt/presentation.xml", ContentKind.PPTX),
)


def _office_or_zip(path):
    """An Office document is a ZIP; its own member names tell which one - no file name needed."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError, EOFError):
        return ContentKind.ZIP, "unreadable"
    if "[Content_Types].xml" in names:
        for marker, kind in _OFFICE_MARKERS:
            if marker in names:
                return kind, ""
    return ContentKind.ZIP, ""
