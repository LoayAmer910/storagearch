"""Which strategies to try, from the content alone (I2): one table keyed by ContentKind, each row with
the measurement it rests on. The router never sees a file name - only a ContentInfo.

`probe` rows use ContentInfo.probe_saving (fast zstd on the first 1 MiB): an already-compressed file
(random-looking bytes) is skipped at no cost, while the same kind holding raw data (an uncompressed TIFF,
a ZIP of stored files) still gets a candidate.
"""
from dataclasses import dataclass

from core import strategies
from core.content import ContentKind as K

MIB = 1024 * 1024
HEAVY_LIMIT_BYTES = 256 * MIB   # above this, the slow strategies are left out (measured: they time out
                                # on large files without winning)
# Size limits per slow strategy: speed is decided here, before anything runs, so the choice stays the same
# for the same bytes. Measured compress+restore on real files, one run at a time: LZMA 0.9-1.0 MB/s and
# zstd level 19 about 1.1 MB/s on 13 MB CSVs (zstd drops to level 9 from 15 MiB on its own), Brotli at
# quality 9 about 6.5 MB/s. LZMA above 32 MiB would cost over half a minute per file for a few percent.
SIZE_LIMITS = {"lzma": 32 * MIB, "lzma_x86": 64 * MIB, "brotli": 128 * MIB}


@dataclass(frozen=True)
class Route:
    candidates: tuple              # strategy names, in preference order
    evidence: str
    min_probe_saving: float = 0.0  # try only if the probe saved at least this much (0 = always)


_TEXT = ("zstd_ldm", "brotli", "zstd", "lzma")
# Text is routed by size, from the differential test on real folders (2.4 GB): between 1 and 32 MiB, LZMA won
# every one of 103 files (35 XML, 28 SQL, 40 JSON exports, 6-15 MB each) - trying the other three there tripled
# the time for nothing. Below 1 MiB, Brotli (231), zstd (160) and LZMA (23) each won some of 400+ source files,
# and trying all three costs little. Above LZMA's size limit, the two fast zstd variants remain.
TEXT_BANDS = ((1 * 1024 * 1024, ("brotli", "zstd", "lzma")), (32 * 1024 * 1024, ("lzma",)), (None, ("zstd_ldm", "zstd")))
TABLE = {
    K.EMPTY: Route((), "nothing to compress"),
    K.TEXT: Route(_TEXT, "text, by size (TEXT_BANDS): Brotli/zstd/LZMA below 1 MiB, LZMA to 32 MiB, fast zstd "
                         "beyond; 57 real documents/code/data files -3.4 % vs ZIP"),
    K.EXECUTABLE: Route(("lzma_x86",), "x86 filter + LZMA: real 4.1 MB EXE 1,277,754 B vs 1,320,288 B plain "
                                       "LZMA; 726 KB DLL 266,156 B vs 272,428 B"),
    K.JPEG: Route(("jpeg_xl", "zstd"), "11 real JPEGs (34.9 MB): JPEG XL -17.2 %; zstd as the tool-free fallback"),
    K.BMP: Route(("bmp_jxl", "zstd"), "real BMPs: JPEG XL 3721 B on a flat graphic; noisy 39-50 MB bitmaps lost "
                                      "8.6 % - the probe rule keeps those to zstd", min_probe_saving=0.0),
    K.PNG: Route((), "1.3 GB pair: trying changed size by <0.1 % and cost 49.6 s -> 89.7 s"),
    K.GIF: Route((), "same measurement as PNG: already compressed"),
    K.TIFF: Route(("zstd",), "real TIFFs were already compressed (skip measured); raw ones are caught by the probe",
                  min_probe_saving=10.0),
    K.WEBP: Route(("zstd",), "already compressed; only a probe that finds slack gets a try", min_probe_saving=10.0),
    K.AVIF: Route(("zstd",), "already compressed; probe only", min_probe_saving=10.0),
    K.HEIC: Route(("zstd",), "already compressed; probe only", min_probe_saving=10.0),
    K.VIDEO: Route(("zstd",), "already compressed; probe only", min_probe_saving=10.0),
    K.COMPRESSED: Route(("zstd",), "gz/xz/bz2/zst/7z/rar: nothing beat ZIP; probe only", min_probe_saving=10.0),
    K.ZIP: Route(("zstd",), "ZIP containers: nothing beat ZIP; a ZIP of stored files is caught by the probe",
                 min_probe_saving=10.0),
    K.DOCX: Route(("zstd",), "OOXML: nothing beat ZIP (skip measured); probe only", min_probe_saving=10.0),
    K.XLSX: Route(("zstd",), "OOXML: nothing beat ZIP (skip measured); probe only", min_probe_saving=10.0),
    K.PPTX: Route(("zstd",), "OOXML: zstd was the only one that ever helped; probe only", min_probe_saving=5.0),
    K.PDF: Route(("zstd", "lzma"), "PDF: text streams compress; zstd, LZMA for the last few %"),
    K.SQLITE: Route(("zstd", "lzma"), "database pages are highly redundant"),
    K.BINARY: Route(("zstd",), "unknown binary: one cheap zstd attempt when the probe finds anything",
                    min_probe_saving=3.0),
}
_BMP_JXL_MIN_PROBE = 5.0   # measured: bitmaps whose first MiB zstd-1 could not shrink by 5 % lost with JPEG XL


def route(info):
    """The strategies to try for this content, in preference order."""
    row = TABLE[info.kind]
    if info.probe_saving < row.min_probe_saving:
        return []
    names = list(row.candidates)
    if info.kind is K.TEXT:
        names = list(next(names for limit, names in TEXT_BANDS if limit is None or info.size <= limit))
    if info.kind is K.BMP and info.size >= 32 * MIB and info.probe_saving < _BMP_JXL_MIN_PROBE:
        names.remove("bmp_jxl")
    limited = [n for n in names if info.size <= SIZE_LIMITS.get(n, info.size)]
    if names and not limited:
        limited = ["zstd"]  # a large file whose only candidates are slow still gets a fast try
    names = limited
    if info.size > HEAVY_LIMIT_BYTES and names:
        light = [n for n in names if strategies.get(n).spec.weight != "heavy" or n in ("jpeg_xl", "bmp_jxl")]
        names = light or ["zstd"]  # a huge EXE still gets a (fast) try
    return [strategies.get(n) for n in names]
