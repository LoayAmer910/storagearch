"""core.router - routing by content (I2). Every kind has a row with its evidence; the route depends only on
the ContentInfo; the probe keeps raw data inside "compressed" kinds reachable and skips random bytes;
end to end, the same bytes under any name get the same decision - including the 20 cases the old engine
got wrong (text, JPEG, BMP, PDF, ZIP, GZ, EXE, random data under another extension)."""
import inspect
import shutil
import zipfile

import pytest
from PIL import Image

from core import content, router, strategies
from core.content import ContentKind as K
from core.executor import Executor
from core.pipeline import Pipeline
from core.scope import OperationScope
from support import corpus


JXL = strategies.get("jpeg_xl").available()[0]


OLD_I2_KINDS = ("text.log", "text.csv", "text.json", "text.py", "text.md", "image.png", "image.gif", "image.tiff",
                "image.bmp", "image.jpg", "doc.pdf", "bundle.zip", "bundle.gz", "program.exe", "random.bin",
                "large_text.txt", "office.docx", "db.sqlite")


def _names(info):
    return [s.name for s in router.route(info)]


def test_every_kind_has_a_row_with_evidence_and_known_strategies():
    assert set(router.TABLE) == set(K)
    for kind, row in router.TABLE.items():
        assert row.evidence.strip(), kind
        for name in row.candidates:
            strategies.get(name)


def test_the_router_takes_nothing_but_the_content():
    assert list(inspect.signature(router.route).parameters) == ["info"]


@pytest.mark.parametrize("name,expected", [
    ("text.log", ["brotli", "zstd", "lzma"]),
    ("large_text.txt", ["lzma"]),
    ("program.exe", ["lzma_x86"]),
    ("image.jpg", ["jpeg_xl", "zstd"]),
    ("image.bmp", ["bmp_jxl", "zstd"]),
    ("image.png", []),
    ("image.gif", []),
    ("random.bin", []),
    ("large_random.bin", []),
    ("bundle.gz", []),
    ("empty.txt", []),
    ("doc.pdf", ["zstd", "lzma"]),
    ("db.sqlite", ["zstd", "lzma"]),
])
def test_routes_for_the_corpus(name, expected, samples):
    assert _names(content.classify(samples[name].path)) == expected


def test_the_probe_finds_raw_data_inside_compressed_kinds(tmp_path):
    raw_tiff = tmp_path / "raw.tiff"
    Image.new("RGB", (256, 256), (40, 80, 120)).save(raw_tiff, format="TIFF")  # uncompressed, flat
    stored_zip = tmp_path / "stored.zip"
    with zipfile.ZipFile(stored_zip, "w", zipfile.ZIP_STORED) as z:
        z.writestr("big.txt", corpus.lines(5000, "stored line {i} {w}\n").decode())
    for path, kind in ((raw_tiff, K.TIFF), (stored_zip, K.ZIP)):
        info = content.classify(str(path))
        assert info.kind is kind and info.probe_saving > 50 and _names(info) == ["zstd"], info


def test_huge_files_drop_the_slow_strategies_but_keep_a_fast_one():
    def info(kind, size, probe=50.0):
        return content.ContentInfo(kind=kind, size=size, sha256="0" * 64, probe_saving=probe)
    big = router.HEAVY_LIMIT_BYTES + 1
    assert _names(info(K.TEXT, big)) == ["zstd_ldm", "zstd"]
    assert _names(info(K.TEXT, 40 * router.MIB)) == ["zstd_ldm", "zstd"]  # beyond LZMA's limit
    assert _names(info(K.TEXT, 10 * router.MIB)) == ["lzma"]
    assert _names(info(K.TEXT, 1 * router.MIB)) == ["brotli", "zstd", "lzma"]
    assert _names(info(K.EXECUTABLE, 100 * router.MIB)) == ["zstd"]
    assert _names(info(K.EXECUTABLE, big)) == ["zstd"]
    assert _names(info(K.JPEG, big)) == ["jpeg_xl", "zstd"]
    assert _names(info(K.BMP, 64 * router.MIB, probe=1.0)) == ["zstd"]    # noisy large bitmap: no JPEG XL
    assert _names(info(K.BMP, 64 * router.MIB, probe=40.0)) == ["bmp_jxl", "zstd"]


def test_same_bytes_under_any_name_get_the_same_decision(samples, tmp_path):
    differing = {}
    with OperationScope(temp_root=str(tmp_path / "s")) as scope:
        pipeline = Pipeline(Executor(scope), router.route)
        for name in OLD_I2_KINDS:
            results = {}
            for ext in ("", "txt", "jpg", "dat", "png", "exe"):
                folder = tmp_path / f"{name}_{ext or 'none'}"
                folder.mkdir()
                target = folder / ("sample" + (f".{ext}" if ext else ""))
                shutil.copyfile(samples[name].path, target)
                d = pipeline.decide(str(target))
                results[ext] = (d.method, d.stored_size)
            if len(set(results.values())) != 1:
                differing[name] = results
    assert differing == {}, differing


def test_decisions_on_the_corpus_are_sensible(samples, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "s")) as scope:
        pipeline = Pipeline(Executor(scope), router.route)
        got = {n: pipeline.decide(samples[n].path) for n in ("text.log", "program.exe", "image.png", "random.bin",
                                                             "large_text.txt", "image.jpg")}
    assert got["text.log"].smart and got["large_text.txt"].smart and got["program.exe"].smart
    assert not got["image.png"].smart and got["image.png"].candidates == ()
    assert got["random.bin"].method == "stored" and got["random.bin"].candidates == ()
    if JXL:
        assert got["image.jpg"].method == "jpeg_xl"


def test_the_same_file_gets_the_same_decision_under_load(samples, tmp_path):
    """Busy threads compete for the CPU while one file is decided again and again: the choice must not move
    (it did in the old policy, which judged candidates by measured speed)."""
    import threading
    stop = threading.Event()

    def burn():
        x = 0
        while not stop.is_set():
            x = (x * 31 + 7) % 1000003
    burners = [threading.Thread(target=burn, daemon=True) for _ in range(8)]
    for t in burners:
        t.start()
    try:
        with OperationScope(temp_root=str(tmp_path / "s")) as scope:
            pipeline = Pipeline(Executor(scope), router.route)
            for name in ("image.jpg", "text.csv", "program.exe", "image.bmp"):
                seen = {(d.method, d.stored_size) for d in (pipeline.decide(samples[name].path) for _ in range(4))}
                assert len(seen) == 1, (name, seen)
    finally:
        stop.set()
