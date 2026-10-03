"""core.strategies.codecs - every codec restores byte-exact, refuses damaged input, and gives the
same stored bytes from a file and from memory."""
import concurrent.futures
import hashlib
import shutil

import pytest

from core import strategies
from core.strategies.base import CHUNK, MIB, StrategyError, StreamingStrategy
from core.strategies.codecs import _lzma_filters, BrotliStrategy, LARGE_FILE_BYTES, ZstdStrategy
from support.api import read
from support.files import sample_text
from support.strategies import read_file, round_trip, GENERIC


WITH_CHECKSUM = ("zstd", "zstd_ldm", "zlib", "lzma", "lzma_x86")


STREAMING = [s for s in strategies.REGISTRY.values() if isinstance(s, StreamingStrategy)]


@pytest.mark.parametrize("name", GENERIC)
def test_generic_codec_restores_every_corpus_file_exactly(name, samples, tmp_path):
    strategy = strategies.get(name)
    for i, sample in enumerate(samples.values()):
        before = hashlib.sha256(read_file(sample.path)).hexdigest()
        _stored, restored = round_trip(strategy, sample.path, str(tmp_path / str(i)))
        assert restored == read_file(sample.path), f"{name} changed {sample.name}"
        assert hashlib.sha256(read_file(sample.path)).hexdigest() == before, f"{name} modified its source {sample.name}"


@pytest.mark.parametrize("name", GENERIC)
def test_stored_bytes_do_not_depend_on_the_name(name, samples, tmp_path):
    strategy = strategies.get(name)
    for sample_name in ("text.log", "program.exe", "image.bmp", "random.bin"):
        src = samples[sample_name].path
        outputs = set()
        # Hebrew on purpose: non-ASCII, right-to-left names must archive and restore exactly
        for j, other in enumerate(("a.txt", "b.jpg", "-dash.bin", "שם.dat", "noext")):
            folder = tmp_path / f"{sample_name}_{j}"
            folder.mkdir()
            shutil.copyfile(src, folder / other)
            stored, _ = round_trip(strategy, str(folder / other), str(folder / "w"))
            outputs.add(stored)
        assert len(outputs) == 1, f"{name}: stored bytes of {sample_name} differ by name"


@pytest.mark.parametrize("name", GENERIC)
def test_truncated_or_padded_stored_data_is_refused(name, samples, tmp_path):
    strategy = strategies.get(name)
    src = samples["large_text.txt"].path
    cw = tmp_path / "c"
    cw.mkdir()
    stored = read_file(strategy.compress(src, str(cw)))
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
    stored = bytearray(read_file(strategy.compress(src, str(cw))))
    stored[len(stored) // 2] ^= 0x40
    path = tmp_path / "flipped.bin"
    path.write_bytes(bytes(stored))
    rw = tmp_path / "r"
    rw.mkdir()
    try:
        restored = read_file(strategy.restore(str(path), str(rw)))
    except StrategyError:
        return
    assert name not in WITH_CHECKSUM, f"{name} has a checksum but accepted a damaged stream"
    assert restored != read_file(src), "a damaged stream restored to the original - the flip was not in the data"


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
        return name, sample.name, round_trip(strategies.get(name), sample.path, str(tmp_path / f"p{i}"))[1] == read_file(sample.path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda args: one(*args), [(i, n, s) for i, (n, s) in enumerate(jobs)]))
    assert [r for r in results if not r[2]] == []


@pytest.mark.parametrize("strategy", STREAMING, ids=lambda s: s.name)
@pytest.mark.parametrize("size", [0, 5, 4096, 300_000, CHUNK])
def test_a_codec_gives_the_same_stored_bytes_from_memory_as_from_a_file(strategy, size, tmp_path):
    data = sample_text(size)
    src = tmp_path / "src"
    src.write_bytes(data)
    work = tmp_path / "w"
    work.mkdir()
    from_file = read(strategy.compress(str(src), str(work)))
    from_memory = b"".join(strategy.encode([data] if data else [], len(data)))
    assert from_memory == from_file
    assert b"".join(strategy.decode([from_memory])) == data


@pytest.mark.parametrize("strategy", STREAMING, ids=lambda s: s.name)
def test_a_codec_refuses_a_cut_or_padded_stream_in_memory(strategy):
    stored = b"".join(strategy.encode([sample_text(50_000)], 50_000))
    for bad in ([stored[:-7]], [stored + b"extra bytes"], [stored, b"extra bytes"]):  # the last: extra data in a later chunk
        with pytest.raises(StrategyError):
            b"".join(strategy.decode(bad))
