"""JPEG XL strategies (external cjxl/djxl tools).

jpeg_xl: lossless JPEG recompression. cjxl reads the JPEG by its content; djxl chooses the output format
from the output file's extension, so restore always writes a fixed `restored.jpg` inside the work dir -
never a name derived from the original file. That is what makes the restore name-independent.

bmp_jxl: the pixel array of an uncompressed 24- or 32-bit BMP is encoded losslessly as JPEG XL; the
bytes before and after it (headers, masks, trailing data) are kept verbatim, and zero row padding is
dropped and restored. Anything else (paletted, RLE, non-zero padding) is refused, never guessed.
"""
import os
import struct

from core import tools
from core.strategies.base import Strategy, StrategyError, StrategySpec

_JXL_SPEC = dict(version=1, weight="heavy", in_process_safe=True, max_in_process_bytes=1 << 40,
                 base_timeout_s=30, seconds_per_mib=4.0)  # the tool is a child process with its own timeout


# Each tool run gets two threads: many candidates run side by side, and cjxl's default (every core) made 16
# parallel runs fight over 256 threads - slow enough that the policy rejected JPEG XL under load while it
# won on the same file alone (found by the differential test on real JPEGs).
TOOL_THREADS = ["--num_threads=2"]


def _tool_timeout(time_limit_s):
    return time_limit_s if time_limit_s else tools.DEFAULT_TOOL_TIMEOUT_S


class JpegXlStrategy(Strategy):
    spec = StrategySpec(name="jpeg_xl", **_JXL_SPEC)

    def available(self):
        return tools.available("cjxl", "djxl")

    def compress(self, src_path, work_dir, time_limit_s=None):
        with open(src_path, "rb") as f:
            if f.read(3) != b"\xff\xd8\xff":
                raise StrategyError("not a JPEG")  # cjxl would otherwise encode other images lossy
        out = os.path.join(work_dir, "stored.jxl")
        tools.run("cjxl", [src_path, out, "--lossless_jpeg=1", *TOOL_THREADS], _tool_timeout(time_limit_s), paths=(0, 1))
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "restored.jpg")
        tools.run("djxl", [stored_path, out, *TOOL_THREADS], _tool_timeout(time_limit_s), paths=(0, 1))
        return out


# --- BMP ---------------------------------------------------------------------------------------------

_MAGIC = b"SABJ"
_LAYOUT = struct.Struct("<4sBIIIIBBBB")  # magic, version, header_len, trailer_len, width, rows, bytes/px, pad, alpha mode, alpha value
_ALPHA_NONE, _ALPHA_CONSTANT, _ALPHA_CHANNEL = 0, 1, 2


def _parse_bmp(data):
    if len(data) < 54 or data[:2] != b"BM":
        raise StrategyError("not a BMP")
    offset, info_size = struct.unpack_from("<II", data, 10)
    if info_size < 40:
        raise StrategyError("old-style BMP header not supported")
    width, height, _planes, bits, compression = struct.unpack_from("<iiHHI", data, 18)
    if bits not in (24, 32) or compression not in (0, 3) or (compression == 3 and bits != 32):
        raise StrategyError(f"unsupported BMP variant: {bits} bits, compression {compression}")
    if width <= 0 or height == 0:
        raise StrategyError("invalid BMP dimensions")
    rows, bpp = abs(height), bits // 8
    row_bytes = width * bpp
    stride = (row_bytes + 3) & ~3
    end = offset + stride * rows
    if offset < 14 + info_size or end > len(data):
        raise StrategyError("pixel array outside the file")
    return offset, end, width, rows, bpp, row_bytes, stride


class BmpJxlStrategy(Strategy):
    spec = StrategySpec(name="bmp_jxl", **_JXL_SPEC)

    def available(self):
        return tools.available("cjxl", "djxl")

    def compress(self, src_path, work_dir, time_limit_s=None):
        with open(src_path, "rb") as f:
            data = f.read()
        offset, end, width, rows, bpp, row_bytes, stride = _parse_bmp(data)
        pad = stride - row_bytes
        view = memoryview(data)
        pixels = bytearray()
        for r in range(rows):
            start = offset + r * stride
            if pad and any(view[start + row_bytes:start + stride]):
                raise StrategyError("non-zero row padding")
            pixels += view[start:start + row_bytes]

        alpha_mode, alpha_value, channels = _ALPHA_NONE, 0, 3
        if bpp == 4:
            alpha = pixels[3::4]
            if alpha.count(alpha[0]) == len(alpha):
                alpha_mode, alpha_value = _ALPHA_CONSTANT, alpha[0]
                del pixels[3::4]
            else:
                alpha_mode, channels = _ALPHA_CHANNEL, 4

        image = os.path.join(work_dir, "pixels.pam" if channels == 4 else "pixels.ppm")
        with open(image, "wb") as f:
            f.write(_pnm_header(width, rows, channels))
            f.write(pixels)
        encoded = os.path.join(work_dir, "pixels.jxl")
        tools.run("cjxl", [image, encoded, "-d", "0", "-e", "7", *TOOL_THREADS], _tool_timeout(time_limit_s), paths=(0, 1))
        os.remove(image)

        out = os.path.join(work_dir, "stored.bmpjxl")
        with open(out, "wb") as f, open(encoded, "rb") as jxl:
            f.write(_LAYOUT.pack(_MAGIC, 1, offset, len(data) - end, width, rows, bpp, pad, alpha_mode, alpha_value))
            f.write(view[:offset])
            f.write(view[end:])
            f.write(jxl.read())
        os.remove(encoded)
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        with open(stored_path, "rb") as f:
            data = f.read()
        if len(data) < _LAYOUT.size:
            raise StrategyError("stored data too short")
        magic, version, header_len, trailer_len, width, rows, bpp, pad, alpha_mode, alpha_value = _LAYOUT.unpack_from(data)
        if magic != _MAGIC or version != 1:
            raise StrategyError("not a stored BMP")
        pos = _LAYOUT.size
        header = data[pos:pos + header_len]
        trailer = data[pos + header_len:pos + header_len + trailer_len]
        encoded = os.path.join(work_dir, "pixels.jxl")
        with open(encoded, "wb") as f:
            f.write(data[pos + header_len + trailer_len:])
        channels = 4 if alpha_mode == _ALPHA_CHANNEL else 3
        decoded = os.path.join(work_dir, "pixels.pam" if channels == 4 else "pixels.ppm")
        tools.run("djxl", [encoded, decoded, *TOOL_THREADS], _tool_timeout(time_limit_s), paths=(0, 1))
        with open(decoded, "rb") as f:
            got_w, got_h, got_c, pixels = _read_pnm(f.read())
        if (got_w, got_h, got_c) != (width, rows, channels):
            raise StrategyError(f"decoded image is {got_w}x{got_h}x{got_c}, expected {width}x{rows}x{channels}")
        if alpha_mode == _ALPHA_CONSTANT:
            rgba = bytearray(width * rows * 4)
            for c in range(3):
                rgba[c::4] = pixels[c::3]
            rgba[3::4] = bytes([alpha_value]) * (width * rows)
            pixels = rgba

        row_bytes = width * bpp
        out = os.path.join(work_dir, "restored.bmp")
        with open(out, "wb") as f:
            f.write(header)
            padding = b"\x00" * pad
            view = memoryview(pixels)
            for r in range(rows):
                f.write(view[r * row_bytes:(r + 1) * row_bytes])
                f.write(padding)
            f.write(trailer)
        return out


def _pnm_header(width, height, channels):
    if channels == 3:
        return f"P6\n{width} {height}\n255\n".encode("ascii")
    return f"P7\nWIDTH {width}\nHEIGHT {height}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n".encode("ascii")


def _read_pnm(data):
    """Parses the P6 or P7 file djxl wrote. Returns (width, height, channels, pixel bytes)."""
    if data.startswith(b"P7"):
        end = data.find(b"ENDHDR\n")
        if end < 0:
            raise StrategyError("bad PAM header")
        fields = {}
        for line in data[2:end].decode("ascii").splitlines():
            parts = line.split()
            if len(parts) >= 2 and not line.startswith("#"):
                fields[parts[0]] = parts[1]
        width, height, depth, maxval = (int(fields.get(k, 0)) for k in ("WIDTH", "HEIGHT", "DEPTH", "MAXVAL"))
        body = data[end + len(b"ENDHDR\n"):]
    elif data.startswith(b"P6"):
        tokens, pos = [], 2
        while len(tokens) < 3:
            while pos < len(data) and data[pos:pos + 1].isspace():
                pos += 1
            if data[pos:pos + 1] == b"#":
                pos = data.index(b"\n", pos) + 1
                continue
            start = pos
            while pos < len(data) and not data[pos:pos + 1].isspace():
                pos += 1
            tokens.append(int(data[start:pos]))
        width, height, maxval = tokens
        depth = 3
        body = data[pos + 1:]
    else:
        raise StrategyError("unexpected decoded image format")
    if maxval != 255:
        raise StrategyError(f"decoded image has maxval {maxval}")
    if len(body) != width * height * depth:
        raise StrategyError("decoded pixel data has the wrong size")
    return width, height, depth, bytearray(body)
