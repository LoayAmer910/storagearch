"""Step 2 - strategies. Every strategy restores byte-exact, never depends on a file name, writes only
inside its work dir, leaves the source untouched, and never returns wrong bytes silently from a damaged
stored stream where the format has a checksum. The JPEG XL strategies refuse inputs they cannot keep
exact instead of guessing."""
import concurrent.futures
import hashlib
import inspect
import os
import shutil
import struct

import pytest
from PIL import Image

import corpus
from core import strategies, tools
from core.strategies import StrategyError
from core.strategies.base import MIB
from core.strategies.codecs import LARGE_FILE_BYTES, BrotliStrategy, ZstdStrategy, _lzma_filters

GENERIC = ("deflate", "zstd", "zstd_ldm", "zlib", "lzma", "lzma_x86", "brotli")
WITH_CHECKSUM = ("zstd", "zstd_ldm", "zlib", "lzma", "lzma_x86")
JXL_AVAILABLE = tools.available("cjxl", "djxl")[0]
needs_jxl = pytest.mark.skipif(not JXL_AVAILABLE, reason="cjxl/djxl not found (set STORAGEARCH_JXL_TOOLS_DIR)")


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    return corpus.build(str(tmp_path_factory.mktemp("corpus")))


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _round_trip(strategy, src, base):
    """compress in one work dir, restore in another; returns (stored bytes, restored bytes)."""
    cw, rw = os.path.join(base, "c"), os.path.join(base, "r")
    os.makedirs(cw)
    os.makedirs(rw)
    stored = strategy.compress(src, cw)
    assert os.path.dirname(os.path.abspath(stored)) == os.path.abspath(cw), f"{strategy.name} wrote outside its work dir"
    restored = strategy.restore(stored, rw)
    assert os.path.dirname(os.path.abspath(restored)) == os.path.abspath(rw), f"{strategy.name} restored outside its work dir"
    return _read(stored), _read(restored)


def test_registry_is_consistent():
    assert set(strategies.REGISTRY) == set(GENERIC) | {"jpeg_xl", "bmp_jxl"}
    for name, s in strategies.REGISTRY.items():
        assert s.name == name and s.spec.version >= 1 and s.spec.weight in ("light", "heavy")
        assert 0 < s.spec.timeout_s(0) < s.spec.timeout_s(100 * MIB)
    with pytest.raises(StrategyError):
        strategies.get("no_such_strategy")


def test_no_strategy_can_receive_a_file_name():
    for s in strategies.REGISTRY.values():
        assert list(inspect.signature(s.compress).parameters) == ["src_path", "work_dir", "time_limit_s"], s.name
        assert list(inspect.signature(s.restore).parameters) == ["stored_path", "work_dir", "time_limit_s"], s.name


@pytest.mark.parametrize("name", GENERIC)
def test_generic_codec_restores_every_corpus_file_exactly(name, samples, tmp_path):
    strategy = strategies.get(name)
    for i, sample in enumerate(samples.values()):
        before = hashlib.sha256(_read(sample.path)).hexdigest()
        _stored, restored = _round_trip(strategy, sample.path, str(tmp_path / str(i)))
        assert restored == _read(sample.path), f"{name} changed {sample.name}"
        assert hashlib.sha256(_read(sample.path)).hexdigest() == before, f"{name} modified its source {sample.name}"


@pytest.mark.parametrize("name", GENERIC)
def test_stored_bytes_do_not_depend_on_the_name(name, samples, tmp_path):
    strategy = strategies.get(name)
    for sample_name in ("text.log", "program.exe", "image.bmp", "random.bin"):
        src = samples[sample_name].path
        outputs = set()
        for j, other in enumerate(("a.txt", "b.jpg", "-dash.bin", "שם.dat", "noext")):
            folder = tmp_path / f"{sample_name}_{j}"
            folder.mkdir()
            shutil.copyfile(src, folder / other)
            stored, _ = _round_trip(strategy, str(folder / other), str(folder / "w"))
            outputs.add(stored)
        assert len(outputs) == 1, f"{name}: stored bytes of {sample_name} differ by name"


@pytest.mark.parametrize("name", GENERIC)
def test_truncated_or_padded_stored_data_is_refused(name, samples, tmp_path):
    strategy = strategies.get(name)
    src = samples["large_text.txt"].path
    cw = tmp_path / "c"
    cw.mkdir()
    stored = _read(strategy.compress(src, str(cw)))
    for label, bad in (("truncated", stored[: len(stored) // 2]), ("trailing garbage", stored + b"garbage!" * 4)):
        path = tmp_path / f"{label}.bin"
        path.write_bytes(bad)
        rw = tmp_path / f"r_{label.replace(' ', '_')}"
        rw.mkdir()
        with pytest.raises(StrategyError):
            strategy.restore(str(path), str(rw))


@pytest.mark.parametrize("name", GENERIC)
def test_a_flipped_byte_is_detected_or_changes_the_output(name, samples, tmp_path):
    strategy = strategies.get(name)
    src = samples["large_text.txt"].path
    cw = tmp_path / "c"
    cw.mkdir()
    stored = bytearray(_read(strategy.compress(src, str(cw))))
    stored[len(stored) // 2] ^= 0x40
    path = tmp_path / "flipped.bin"
    path.write_bytes(bytes(stored))
    rw = tmp_path / "r"
    rw.mkdir()
    try:
        restored = _read(strategy.restore(str(path), str(rw)))
    except StrategyError:
        return
    assert name not in WITH_CHECKSUM, f"{name} has a checksum but accepted a damaged stream"
    assert restored != _read(src), "a damaged stream restored to the original - the flip was not in the data"


def test_level_choices_follow_the_measured_boundaries():
    assert ZstdStrategy.level_for(LARGE_FILE_BYTES - 1) == 19 and ZstdStrategy.level_for(LARGE_FILE_BYTES) == 9
    assert BrotliStrategy.quality_for(4 * MIB - 1) == 11 and BrotliStrategy.quality_for(4 * MIB) == 9
    assert _lzma_filters(10_000, x86=False)[-1]["dict_size"] == 1 << 16  # the floor, not 64 MiB
    huge = _lzma_filters(10 * 1024 * MIB, x86=True)
    assert huge[0]["id"] == __import__("lzma").FILTER_X86 and huge[-1]["dict_size"] == 64 * MIB and huge[-1]["preset"] == 6
    for size in (1, 70_000, 5 * MIB, 20 * MIB):
        assert _lzma_filters(size, False)[-1]["dict_size"] >= min(size, 64 * MIB)


def test_codecs_run_in_parallel_without_interfering(samples, tmp_path):
    jobs = [(name, sample) for name in GENERIC for sample in samples.values() if sample.kind.value != "empty"][:120]

    def one(i, name, sample):
        return name, sample.name, _round_trip(strategies.get(name), sample.path, str(tmp_path / f"p{i}"))[1] == _read(sample.path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda args: one(*args), [(i, n, s) for i, (n, s) in enumerate(jobs)]))
    assert [r for r in results if not r[2]] == []


# --- JPEG XL ------------------------------------------------------------------------------------------

@needs_jxl
@pytest.mark.parametrize("sample_name", ["image.jpg", "image_progressive.jpg"])
def test_jpeg_xl_restores_exactly_under_any_name(sample_name, samples, tmp_path):
    jxl = strategies.get("jpeg_xl")
    src = samples[sample_name].path
    outputs = set()
    for j, other in enumerate(("photo.jpg", "photo.txt", "photo", "-dash.png", "תמונה.JPEG")):
        folder = tmp_path / str(j)
        folder.mkdir()
        shutil.copyfile(src, folder / other)
        stored, restored = _round_trip(jxl, str(folder / other), str(folder / "w"))
        assert restored == _read(src), f"restore under the name {other!r} differs"
        outputs.add(stored)
    assert len(outputs) == 1
    assert len(outputs.pop()) < len(_read(src)), "JPEG XL should be smaller than the JPEG"


@needs_jxl
def test_jpeg_xl_restore_does_not_depend_on_the_stored_file_name(samples, tmp_path):
    jxl = strategies.get("jpeg_xl")
    (tmp_path / "c").mkdir()
    stored = jxl.compress(samples["image.jpg"].path, str(tmp_path / "c"))
    renamed = tmp_path / "weird name.bin"
    shutil.copyfile(stored, renamed)
    (tmp_path / "r").mkdir()
    assert _read(jxl.restore(str(renamed), str(tmp_path / "r"))) == _read(samples["image.jpg"].path)


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
    stored, restored = _round_trip(strategies.get("bmp_jxl"), src, str(tmp_path / "w"))
    assert restored == _read(src)


@needs_jxl
@pytest.mark.parametrize("case", ["nonzero_padding", "paletted", "rle", "pixels_past_eof", "not_bmp"])
def test_bmp_jxl_refuses_layouts_it_cannot_keep_exact(case, tmp_path, samples):
    if case == "nonzero_padding":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(33, 20, 12), _nonzero_padding)
    elif case == "paletted":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(32, 20, 13).convert("P"))
    elif case == "rle":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(32, 20, 14), lambda d: struct.pack_into("<I", d, 30, 1))
    elif case == "pixels_past_eof":
        src = _bmp(tmp_path, "p.bmp", corpus.photo(32, 20, 15), lambda d: d.__delitem__(slice(-100, None)))
    else:
        src = samples["image.png"].path
    (tmp_path / "w").mkdir()
    with pytest.raises(StrategyError):
        strategies.get("bmp_jxl").compress(src, str(tmp_path / "w"))


def test_missing_tools_are_reported_not_crashed(monkeypatch, tmp_path, samples):
    monkeypatch.setenv("STORAGEARCH_JXL_TOOLS_DIR", str(tmp_path / "empty"))
    for name in ("jpeg_xl", "bmp_jxl"):
        ok, reason = strategies.get(name).available()
        assert not ok and "cjxl" in reason
    (tmp_path / "w").mkdir()
    with pytest.raises(tools.ToolError):
        strategies.get("jpeg_xl").compress(samples["image.jpg"].path, str(tmp_path / "w"))
