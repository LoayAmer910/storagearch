"""General-purpose codecs: Deflate (the baseline), zstd, zstd with long-distance matching, zlib, xz/LZMA,
xz/LZMA with the x86 branch filter, and Brotli. All stream in chunks, so memory does not grow with the
file, and all detect a truncated or damaged stored stream instead of returning short output. Each is an
encode/decode pair over byte chunks (StreamingStrategy): the file form and the in-memory form are the same
code, so they give the same stored bytes.

Levels are the ones measured on the old version (knowledge, not code): zstd 19 below 15 MiB and 9 from
15 MiB (255 MB file: same ratio, 35x faster); zstd long-range at level 12 with a 128 MiB window
(41.7 MB text: level 19 over 10 s, level 12 about 2 s); LZMA preset 9, 6 from 15 MiB; Brotli 11, 9 from
4 MiB. New here: the LZMA dictionary is sized to the file, which gives the same output as preset 9 with
far less memory on small files.
"""
import lzma
import zlib
from compression import zstd

import brotli

from core.strategies.base import CHUNK, MIB, StrategyError, StrategySpec, StreamingStrategy, file_chunks  # noqa: F401

LARGE_FILE_BYTES = 15 * MIB
_ZSTD_LDM_WINDOW_LOG = 27


def _spec(name, weight, base, per_mib, max_in_process=64 * MIB, version=1):
    return StrategySpec(name=name, version=version, weight=weight, in_process_safe=True,
                        max_in_process_bytes=max_in_process, base_timeout_s=base, seconds_per_mib=per_mib)


def _encode_with(c, chunks, finish):
    for chunk in chunks:
        yield c.compress(chunk)
    yield finish()


class DeflateStrategy(StreamingStrategy):
    """Raw Deflate at zlib's default level - exactly what a plain ZIP member costs. The baseline every
    other strategy is measured against."""
    spec = _spec("deflate", "light", 30, 0.5)
    suffix = ".deflate"

    def encode(self, chunks, size):
        c = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
        return _encode_with(c, chunks, c.flush)

    def decode(self, chunks):
        return _zlib_decode(chunks, wbits=-15)


class ZlibStrategy(StreamingStrategy):
    spec = _spec("zlib", "light", 30, 0.5)
    suffix = ".zlib"

    def encode(self, chunks, size):
        c = zlib.compressobj(9, zlib.DEFLATED, 15)
        return _encode_with(c, chunks, c.flush)

    def decode(self, chunks):
        return _zlib_decode(chunks, wbits=15)


def _zlib_decode(chunks, wbits):
    d = zlib.decompressobj(wbits)
    try:
        for chunk in chunks:
            yield d.decompress(chunk)
            if d.unused_data:
                raise StrategyError("trailing data after the compressed stream")
        yield d.flush()
    except zlib.error as exc:
        raise StrategyError(f"damaged stream: {exc}") from exc
    if not d.eof:
        raise StrategyError("truncated stream")


class ZstdStrategy(StreamingStrategy):
    spec = _spec("zstd", "light", 30, 1.0)
    suffix = ".zst"

    @staticmethod
    def level_for(size):
        return 9 if size >= LARGE_FILE_BYTES else 19

    def _options(self, size):
        return {zstd.CompressionParameter.compression_level: self.level_for(size),
                zstd.CompressionParameter.checksum_flag: 1}

    def encode(self, chunks, size):
        # An explicit compressor, not zstd.open(): flush() always ends a complete frame, so even an empty
        # input gives a stream that restores (zstd.open wrote nothing at all for an empty file).
        c = zstd.ZstdCompressor(options=self._options(size))
        return _encode_with(c, chunks, c.flush)

    def decode(self, chunks):
        return _single_stream_decode(zstd.ZstdDecompressor(options=self._decompress_options()), chunks,
                                     zstd.ZstdError)

    def _decompress_options(self):
        return None


class ZstdLongRangeStrategy(ZstdStrategy):
    """zstd with long-distance matching and a 128 MiB window: finds repeats far apart (logs, dumps)."""
    spec = _spec("zstd_ldm", "light", 30, 0.5)

    @staticmethod
    def level_for(size):
        return 12

    def _options(self, size):
        return {zstd.CompressionParameter.compression_level: 12,
                zstd.CompressionParameter.enable_long_distance_matching: 1,
                zstd.CompressionParameter.window_log: _ZSTD_LDM_WINDOW_LOG,
                zstd.CompressionParameter.checksum_flag: 1}

    def _decompress_options(self):
        return {zstd.DecompressionParameter.window_log_max: _ZSTD_LDM_WINDOW_LOG}


def _lzma_filters(size, x86):
    preset = 6 if size >= LARGE_FILE_BYTES else 9
    dict_size = 1 << 16
    while dict_size < size and dict_size < 64 * MIB:
        dict_size <<= 1
    lzma2 = {"id": lzma.FILTER_LZMA2, "preset": preset, "dict_size": dict_size}
    return [{"id": lzma.FILTER_X86}, lzma2] if x86 else [lzma2]


class LzmaStrategy(StreamingStrategy):
    spec = _spec("lzma", "heavy", 30, 2.0, max_in_process=16 * MIB)
    suffix = ".xz"
    x86 = False

    def encode(self, chunks, size):
        c = lzma.LZMACompressor(format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, filters=_lzma_filters(size, self.x86))
        return _encode_with(c, chunks, c.flush)

    def decode(self, chunks):
        # One xz stream, nothing after it (lzma.open silently ignored trailing data).
        return _single_stream_decode(lzma.LZMADecompressor(format=lzma.FORMAT_XZ), chunks, lzma.LZMAError)


class LzmaX86Strategy(LzmaStrategy):
    """LZMA with the x86 branch-call filter: measured 3-5 % smaller than plain LZMA on real EXE/DLL files."""
    spec = _spec("lzma_x86", "heavy", 30, 2.0, max_in_process=16 * MIB)
    x86 = True


def _single_stream_decode(d, chunks, errors):
    """Decompresses exactly one stream: refuses a truncated stream and anything after its end."""
    try:
        for chunk in chunks:
            if d.eof:
                raise StrategyError("trailing data after the compressed stream")
            yield d.decompress(chunk)
            if d.eof and d.unused_data:
                raise StrategyError("trailing data after the compressed stream")
    except errors as exc:
        raise StrategyError(f"damaged stream: {exc}") from exc
    if not d.eof:
        raise StrategyError("truncated stream")


class BrotliStrategy(StreamingStrategy):
    spec = _spec("brotli", "heavy", 30, 3.0, max_in_process=16 * MIB)
    suffix = ".br"

    @staticmethod
    def quality_for(size):
        return 9 if size >= 4 * MIB else 11

    def encode(self, chunks, size):
        c = brotli.Compressor(quality=self.quality_for(size), lgwin=24)
        for chunk in chunks:
            yield c.process(chunk)
        yield c.finish()

    def decode(self, chunks):
        d = brotli.Decompressor()
        try:
            for chunk in chunks:
                if d.is_finished():
                    raise StrategyError("trailing data after the compressed stream")
                yield d.process(chunk)
        except brotli.error as exc:
            raise StrategyError(f"damaged stream: {exc}") from exc
        if not d.is_finished():
            raise StrategyError("truncated stream")
