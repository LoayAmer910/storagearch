"""General-purpose codecs: Deflate (the baseline), zstd, zstd with long-distance matching, zlib, xz/LZMA,
xz/LZMA with the x86 branch filter, and Brotli. All stream in chunks, so memory does not grow with the
file, and all detect a truncated or damaged stored stream instead of returning short output.

Levels are the ones measured on the old version (knowledge, not code): zstd 19 below 15 MiB and 9 from
15 MiB (255 MB file: same ratio, 35x faster); zstd long-range at level 12 with a 128 MiB window
(41.7 MB text: level 19 over 10 s, level 12 about 2 s); LZMA preset 9, 6 from 15 MiB; Brotli 11, 9 from
4 MiB. New here: the LZMA dictionary is sized to the file, which gives the same output as preset 9 with
far less memory on small files.
"""
import lzma
import os
import zlib
from compression import zstd

import brotli

from core.strategies.base import MIB, Strategy, StrategyError, StrategySpec

CHUNK = MIB
LARGE_FILE_BYTES = 15 * MIB
_ZSTD_LDM_WINDOW_LOG = 27


def _chunks(path):
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                return
            yield chunk


def _size(path):
    return os.path.getsize(path)


def _spec(name, weight, base, per_mib, max_in_process=64 * MIB, version=1):
    return StrategySpec(name=name, version=version, weight=weight, in_process_safe=True,
                        max_in_process_bytes=max_in_process, base_timeout_s=base, seconds_per_mib=per_mib)


class DeflateStrategy(Strategy):
    """Raw Deflate at zlib's default level - exactly what a plain ZIP member costs. The baseline every
    other strategy is measured against."""
    spec = _spec("deflate", "light", 30, 0.5)

    def compress(self, src_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "stored.deflate")
        c = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
        with open(out, "wb") as f:
            for chunk in _chunks(src_path):
                f.write(c.compress(chunk))
            f.write(c.flush())
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        return _zlib_restore(stored_path, work_dir, wbits=-15)


class ZlibStrategy(Strategy):
    spec = _spec("zlib", "light", 30, 0.5)

    def compress(self, src_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "stored.zlib")
        c = zlib.compressobj(9, zlib.DEFLATED, 15)
        with open(out, "wb") as f:
            for chunk in _chunks(src_path):
                f.write(c.compress(chunk))
            f.write(c.flush())
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        return _zlib_restore(stored_path, work_dir, wbits=15)


def _zlib_restore(stored_path, work_dir, wbits):
    out = os.path.join(work_dir, "restored.bin")
    d = zlib.decompressobj(wbits)
    try:
        with open(out, "wb") as f:
            for chunk in _chunks(stored_path):
                f.write(d.decompress(chunk))
                if d.unused_data:
                    raise StrategyError("trailing data after the compressed stream")
            f.write(d.flush())
    except zlib.error as exc:
        raise StrategyError(f"damaged stream: {exc}") from exc
    if not d.eof:
        raise StrategyError("truncated stream")
    return out


class ZstdStrategy(Strategy):
    spec = _spec("zstd", "light", 30, 1.0)

    @staticmethod
    def level_for(size):
        return 9 if size >= LARGE_FILE_BYTES else 19

    def _options(self, size):
        return {zstd.CompressionParameter.compression_level: self.level_for(size),
                zstd.CompressionParameter.checksum_flag: 1}

    def compress(self, src_path, work_dir, time_limit_s=None):
        # An explicit compressor, not zstd.open(): flush() always ends a complete frame, so even an empty
        # input gives a stream that restores (zstd.open wrote nothing at all for an empty file).
        out = os.path.join(work_dir, "stored.zst")
        c = zstd.ZstdCompressor(options=self._options(_size(src_path)))
        with open(out, "wb") as f:
            for chunk in _chunks(src_path):
                f.write(c.compress(chunk))
            f.write(c.flush())
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        return _single_stream_restore(zstd.ZstdDecompressor(options=self._decompress_options()),
                                      stored_path, work_dir, zstd.ZstdError)

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


class LzmaStrategy(Strategy):
    spec = _spec("lzma", "heavy", 30, 2.0, max_in_process=16 * MIB)
    x86 = False

    def compress(self, src_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "stored.xz")
        c = lzma.LZMACompressor(format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64,
                                filters=_lzma_filters(_size(src_path), self.x86))
        with open(out, "wb") as f:
            for chunk in _chunks(src_path):
                f.write(c.compress(chunk))
            f.write(c.flush())
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        # One xz stream, nothing after it (lzma.open silently ignored trailing data).
        return _single_stream_restore(lzma.LZMADecompressor(format=lzma.FORMAT_XZ), stored_path, work_dir,
                                      lzma.LZMAError)


class LzmaX86Strategy(LzmaStrategy):
    """LZMA with the x86 branch-call filter: measured 3-5 % smaller than plain LZMA on real EXE/DLL files."""
    spec = _spec("lzma_x86", "heavy", 30, 2.0, max_in_process=16 * MIB)
    x86 = True


def _single_stream_restore(d, stored_path, work_dir, errors):
    """Decompresses exactly one stream: refuses a truncated stream and anything after its end."""
    out = os.path.join(work_dir, "restored.bin")
    try:
        with open(out, "wb") as f:
            for chunk in _chunks(stored_path):
                if d.eof:
                    raise StrategyError("trailing data after the compressed stream")
                f.write(d.decompress(chunk))
                if d.eof and d.unused_data:
                    raise StrategyError("trailing data after the compressed stream")
    except errors as exc:
        raise StrategyError(f"damaged stream: {exc}") from exc
    if not d.eof:
        raise StrategyError("truncated stream")
    return out


class BrotliStrategy(Strategy):
    spec = _spec("brotli", "heavy", 30, 3.0, max_in_process=16 * MIB)

    @staticmethod
    def quality_for(size):
        return 9 if size >= 4 * MIB else 11

    def compress(self, src_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "stored.br")
        c = brotli.Compressor(quality=self.quality_for(_size(src_path)), lgwin=24)
        with open(out, "wb") as f:
            for chunk in _chunks(src_path):
                f.write(c.process(chunk))
            f.write(c.finish())
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "restored.bin")
        d = brotli.Decompressor()
        try:
            with open(out, "wb") as f:
                for chunk in _chunks(stored_path):
                    if d.is_finished():
                        raise StrategyError("trailing data after the compressed stream")
                    f.write(d.process(chunk))
        except brotli.error as exc:
            raise StrategyError(f"damaged stream: {exc}") from exc
        if not d.is_finished():
            raise StrategyError("truncated stream")
        return out
