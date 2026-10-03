"""core.strategies.jpeg_xl - JPEG XL for JPEG and BMP: exact restores, refusals, no dependence on names."""
import os
import shutil
import struct

import pytest
from PIL import Image

from core import strategies, tools
from core.strategies import StrategyError
from core.strategies.jpeg_xl import _read_pnm
from support import corpus
from support.strategies import read_file, round_trip


JXL_AVAILABLE = tools.available("cjxl", "djxl")[0]


needs_jxl = pytest.mark.skipif(not JXL_AVAILABLE, reason="cjxl/djxl not found (set STORAGEARCH_JXL_TOOLS_DIR)")


def _bmp(tmp_path, name, image, mutate=None):
    path = tmp_path / name
    image.save(path, format="BMP")
    if mutate:
        data = bytearray(path.read_bytes())
        mutate(data)
        path.write_bytes(bytes(data))
    return str(path)


def _set_height(data, value):
    struct.pack_into("<i", data, 22, value)


def _nonzero_padding(data):
    offset = struct.unpack_from("<I", data, 10)[0]
    data[offset + 33 * 3] = 0x7F  # the first row's padding byte (33 px * 3 bytes = 99, stride 100)


@needs_jxl
@pytest.mark.parametrize("sample_name", ["image.jpg", "image_progressive.jpg"])
def test_jpeg_xl_restores_exactly_under_any_name(sample_name, samples, tmp_path):
    jxl = strategies.get("jpeg_xl")
    src = samples[sample_name].path
    outputs = set()
    # Hebrew on purpose: non-ASCII, right-to-left names must archive and restore exactly
    for j, other in enumerate(("photo.jpg", "photo.txt", "photo", "-dash.png", "תמונה.JPEG")):
        folder = tmp_path / str(j)
        folder.mkdir()
        shutil.copyfile(src, folder / other)
        stored, restored = round_trip(jxl, str(folder / other), str(folder / "w"))
        assert restored == read_file(src), f"restore under the name {other!r} differs"
        outputs.add(stored)
    assert len(outputs) == 1
    assert len(outputs.pop()) < len(read_file(src)), "JPEG XL should be smaller than the JPEG"


@needs_jxl
def test_jpeg_xl_restore_does_not_depend_on_the_stored_file_name(samples, tmp_path):
    jxl = strategies.get("jpeg_xl")
    (tmp_path / "c").mkdir()
    stored = jxl.compress(samples["image.jpg"].path, str(tmp_path / "c"))
    renamed = tmp_path / "weird name.bin"
    shutil.copyfile(stored, renamed)
    (tmp_path / "r").mkdir()
    assert read_file(jxl.restore(str(renamed), str(tmp_path / "r"))) == read_file(samples["image.jpg"].path)


@needs_jxl
def test_a_relative_path_starting_with_a_dash_is_not_an_option(samples, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shutil.copyfile(samples["image.jpg"].path, tmp_path / "-v.jpg")
    (tmp_path / "w").mkdir()
    stored = strategies.get("jpeg_xl").compress("-v.jpg", "w")
    assert os.path.getsize(stored) > 0


@needs_jxl
@pytest.mark.parametrize("sample_name", ["image.png", "text.log", "truncated.jpg", "random.bin"])
def test_jpeg_xl_refuses_what_it_cannot_keep_exact(sample_name, samples, tmp_path):
    with pytest.raises((StrategyError, tools.ToolError)):
        strategies.get("jpeg_xl").compress(samples[sample_name].path, str(tmp_path))


@needs_jxl
@pytest.mark.parametrize("case", ["24bit", "odd_width_padding", "rgba_varying_alpha", "rgba_opaque", "top_down", "trailing_bytes"])
def test_bmp_jxl_restores_exactly(case, tmp_path):
    img = corpus.photo(33 if case == "odd_width_padding" else 48, 20, 11)
    if case == "rgba_varying_alpha":
        img = img.convert("RGBA")
        img.putalpha(Image.linear_gradient("L").resize(img.size))
    elif case == "rgba_opaque":
        img = img.convert("RGBA")
    mutate = {
        "top_down": lambda d: _set_height(d, -20),
        "trailing_bytes": lambda d: d.extend(b"TRAILER-DATA" * 5),
    }.get(case)
    src = _bmp(tmp_path, f"{case}.bmp", img, mutate)
    stored, restored = round_trip(strategies.get("bmp_jxl"), src, str(tmp_path / "w"))
    assert restored == read_file(src)


@needs_jxl
@pytest.mark.parametrize("case", ["nonzero_padding", "paletted", "rle", "pixels_past_eof", "not_bmp", "old_header",
                                  "zero_width"])
def test_bmp_jxl_refuses_layouts_it_cannot_keep_exact(case, tmp_path, samples):
    if case == "nonzero_padding":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(33, 20, 12), _nonzero_padding)
    elif case == "paletted":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(32, 20, 13).convert("P"))
    elif case == "rle":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(32, 20, 14), lambda d: struct.pack_into("<I", d, 30, 1))
    elif case == "pixels_past_eof":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(32, 20, 15), lambda d: d.__delitem__(slice(-100, None)))
    elif case == "old_header":  # a 12-byte BITMAPCOREHEADER (OS/2)
        src = _bmp(tmp_path, "p.bmp", corpus.photo(32, 20, 16), lambda d: struct.pack_into("<I", d, 14, 12))
    elif case == "zero_width":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(32, 20, 17), lambda d: struct.pack_into("<i", d, 18, 0))
    else:
        src = samples["image.png"].path
    (tmp_path / "w").mkdir()
    with pytest.raises(StrategyError):
        strategies.get("bmp_jxl").compress(src, str(tmp_path / "w"))


@pytest.mark.parametrize("data", [b"", b"too short", b"X" * 64], ids=["empty", "short", "foreign"])
def test_restoring_something_that_is_not_a_stored_bmp_is_refused(data, tmp_path):
    stored = tmp_path / "stored.bin"
    stored.write_bytes(data)
    with pytest.raises(StrategyError):
        strategies.get("bmp_jxl").restore(str(stored), str(tmp_path))


# --- reading the image file djxl writes ----------------------------------------------------------------

def test_a_decoded_image_with_a_comment_in_its_header_is_read():
    assert _read_pnm(b"P6\n# written by djxl\n2 1\n255\n" + bytes(6)) == (2, 1, 3, bytearray(6))


def test_a_decoded_image_with_transparency_is_read():
    header = b"P7\nWIDTH 1\nHEIGHT 2\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"
    assert _read_pnm(header + bytes(8)) == (1, 2, 4, bytearray(8))


@pytest.mark.parametrize("data, problem", [
    (b"P7\nWIDTH 1\nHEIGHT 1\n" + bytes(4), "bad PAM header"),
    (b"P5\n1 1\n255\n\x00", "unexpected decoded image format"),
    (b"P6\n1 1\n65535\n" + bytes(6), "maxval 65535"),
    (b"P6\n2 2\n255\n" + bytes(5), "wrong size"),
])
def test_a_decoded_image_that_is_not_what_was_stored_is_refused(data, problem):
    with pytest.raises(StrategyError, match=problem):
        _read_pnm(data)
