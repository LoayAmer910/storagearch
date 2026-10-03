"""core.pipeline - deciding how to store one file: the baseline, the candidates, the winner."""
import io
import os
import shutil
import zipfile

import pytest

from core import executor as executor_module, strategies
from core.content import ContentKind
from core.executor import Executor
from core.pipeline import scan, DEFLATE, Pipeline, STORED
from core.scope import OperationCancelled
from core.strategies.base import CHUNK
from support import fakes
from support.api import read
from support.files import files_under, sample_text


GENERIC = [strategies.get(n) for n in ("zstd", "zstd_ldm", "zlib", "lzma", "lzma_x86", "brotli")]


def _pipeline(scope, route):
    return Pipeline(Executor(scope), route if callable(route) else (lambda info: route))


def _zip_member_size(path, compression):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        z.write(path, "m")
    with zipfile.ZipFile(buf) as z:
        return z.infolist()[0].compress_size


TEXT_CODECS = [strategies.get(n) for n in ("brotli", "zstd", "lzma")]


def test_baseline_size_is_exactly_what_zipfile_writes(samples):
    for sample in samples.values():
        info, method, size = scan(sample.path)
        deflated = _zip_member_size(sample.path, zipfile.ZIP_DEFLATED)
        if method == DEFLATE:
            assert size == deflated < info.size, sample.name
        else:
            assert method == STORED and size == info.size <= deflated, sample.name


def test_incompressible_data_uses_stored_as_its_baseline(samples):
    assert scan(samples["large_random.bin"].path)[1] == STORED
    assert scan(samples["large_text.txt"].path)[1] == DEFLATE


def test_files_below_the_limit_never_run_candidates(samples, scope, tmp_path):
    marker = str(tmp_path / "ran")
    decision = _pipeline(scope, [fakes.Fake(marker=marker)]).decide(samples["boundary_4095.dat"].path)
    assert not decision.smart and decision.candidates == () and not os.path.exists(marker)
    empty = _pipeline(scope, [fakes.Fake(marker=marker)]).decide(samples["empty.txt"].path)
    assert empty.info.kind is ContentKind.EMPTY and empty.method == STORED and empty.stored_size == 0


def test_the_first_size_worth_trying_runs_candidates(samples, scope):
    decision = _pipeline(scope, GENERIC).decide(samples["boundary_4096.dat"].path)
    assert decision.candidates, "4096 bytes is worth trying"


def test_failing_candidates_fall_back_to_the_baseline(samples, scope, tmp_path):
    route = [fakes.Raising(name="r"), fakes.Crashing(name="c"), fakes.WrongRestore(name="w"),
             fakes.MissingOutput(name="m"), fakes.Hanging(name="h")]
    decision = _pipeline(scope, route).decide(samples["large_text.txt"].path)
    assert decision.method == DEFLATE and not decision.smart
    assert {o.name: o.status for o in decision.candidates} == {
        "r": "error", "c": "crashed", "w": "mismatch", "m": "error", "h": "timeout"}


def test_a_candidate_that_lies_about_its_size_never_wins(samples, scope):
    class TinyButWrong(fakes.WrongRestore):
        def compress(self, src_path, work_dir, time_limit_s=None):
            out = os.path.join(work_dir, "stored.bin")
            open(out, "wb").write(b"tiny")
            return out
    decision = _pipeline(scope, [TinyButWrong(name="liar", in_process=True), strategies.get("zstd")]).decide(
        samples["large_text.txt"].path)
    assert decision.method == "zstd", decision.method


def test_a_candidate_that_changes_the_source_is_not_accepted(samples, scope, tmp_path):
    src = tmp_path / "moving.txt"
    shutil.copyfile(samples["large_text.txt"].path, src)

    class Mutating(fakes.Fake):
        def compress(self, src_path, work_dir, time_limit_s=None):
            with open(src_path, "ab") as f:
                f.write(b"changed while it was being archived\n")
            return super().compress(src_path, work_dir)
    decision = _pipeline(scope, [Mutating(name="mut", in_process=True)]).decide(str(src))
    assert not decision.smart and decision.candidates[0].status == "mismatch"


def test_the_smallest_accepted_candidate_wins_and_only_its_file_survives(samples, scope):
    decision = _pipeline(scope, GENERIC).decide(samples["large_text.txt"].path)
    verified = [o for o in decision.candidates if o.verified]
    accepted = [o for o in verified if decision.verdicts[o.name].accepted]
    assert decision.smart and decision.stored_size == min(o.stored_size for o in accepted)
    assert os.path.getsize(decision.stored_path) == decision.stored_size
    survivors = [os.path.join(dp, f) for dp, _d, fs in os.walk(scope.root) for f in fs]
    assert survivors == [decision.stored_path], survivors


def test_a_decision_is_never_worse_than_the_baseline(samples, scope):
    pipeline = _pipeline(scope, GENERIC)
    for sample in samples.values():
        d = pipeline.decide(sample.path)
        assert d.stored_size <= d.baseline_size, sample.name
        if d.smart:
            assert d.stored_size < d.baseline_size and d.verdicts[d.method].accepted, sample.name
            if d.stored_bytes is not None:  # a small file, tried in memory
                assert d.stored_path is None and len(d.stored_bytes) == d.stored_size, sample.name
            else:
                assert os.path.getsize(d.stored_path) == d.stored_size, sample.name


def test_same_bytes_under_another_name_get_the_same_decision(samples, scope, tmp_path):
    pipeline = _pipeline(scope, lambda info: GENERIC if info.kind in (ContentKind.TEXT, ContentKind.EXECUTABLE) else GENERIC[:1])
    for name in ("text.csv", "program.exe", "image.bmp", "doc.pdf"):
        results = set()
        for i, other in enumerate(("a.csv", "a.jpg", "a", "a.exe")):
            folder = tmp_path / f"{name}_{i}"
            folder.mkdir()
            shutil.copyfile(samples[name].path, folder / other)
            d = pipeline.decide(str(folder / other))
            results.add((d.method, d.stored_size, d.baseline_size, d.info))
        assert len(results) == 1, (name, results)


def test_a_cancelled_scope_decides_nothing(samples, scope):
    scope.cancel()
    with pytest.raises(OperationCancelled):
        _pipeline(scope, GENERIC).decide(samples["text.log"].path)



def test_a_file_deleted_after_it_was_scanned_falls_back_to_the_baseline(scope, tmp_path):
    src = tmp_path / "small.txt"
    src.write_bytes(sample_text(200_000))
    scanned = scan(str(src))
    src.unlink()  # e.g. removed by another program between the duplicate check and the decision
    decision = Pipeline(Executor(scope), lambda info: TEXT_CODECS).decide(str(src), scanned)
    assert not decision.smart and decision.method == scanned[1]
    assert all(not o.verified for o in decision.candidates)


def test_a_small_file_is_decided_in_memory_without_a_temporary_file(scope, tmp_path):
    src = tmp_path / "small.txt"
    src.write_bytes(sample_text(200_000))
    decision = Pipeline(Executor(scope), lambda info: TEXT_CODECS).decide(str(src))
    assert decision.smart and decision.stored_path is None
    assert len(decision.stored_bytes) == decision.stored_size
    assert {o.where for o in decision.candidates} == {"memory"}
    assert all(o.stored_bytes is None for o in decision.candidates)  # only the winner's bytes are kept
    assert files_under(scope.root) == []
    winner = strategies.get(decision.method)
    assert b"".join(winner.decode([decision.stored_bytes])) == src.read_bytes()


def test_a_file_larger_than_one_chunk_is_still_tried_through_files(scope, tmp_path):
    src = tmp_path / "big.txt"
    src.write_bytes(sample_text(CHUNK + 50_000))
    decision = Pipeline(Executor(scope), lambda info: TEXT_CODECS).decide(str(src))
    assert decision.smart and decision.stored_bytes is None
    assert os.path.getsize(decision.stored_path) == decision.stored_size
    assert "memory" not in {o.where for o in decision.candidates}


def test_past_the_memory_budget_a_winner_goes_to_a_file(scope, tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "MEMORY_BUDGET_BYTES", 10)
    src = tmp_path / "small.txt"
    src.write_bytes(sample_text(200_000))
    decision = Pipeline(Executor(scope), lambda info: TEXT_CODECS).decide(str(src))
    assert decision.smart and decision.stored_bytes is None
    assert os.path.getsize(decision.stored_path) == decision.stored_size
    assert b"".join(strategies.get(decision.method).decode([read(decision.stored_path)])) == src.read_bytes()
