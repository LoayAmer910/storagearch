"""Step 4 - policy and the decision pipeline (I1, I3, I4). The policy's boundaries are exact; the baseline
size is what zipfile really writes; a winner is verified and strictly smaller than the baseline; failing
candidates never break a decision; only the winner's stored file survives; the same bytes under another
name get the same decision."""
import io
import os
import shutil
import zipfile

import pytest

import corpus
import fakes
from core import strategies
from core.content import ContentKind
from core.executor import Executor
from core.pipeline import DEFLATE, STORED, Pipeline, scan
from core.policy import MIB, Policy
from core.scope import OperationCancelled, OperationScope

GENERIC = [strategies.get(n) for n in ("zstd", "zstd_ldm", "zlib", "lzma", "lzma_x86", "brotli")]
P = Policy()


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    return corpus.build(str(tmp_path_factory.mktemp("corpus")))


@pytest.fixture
def scope(tmp_path):
    with OperationScope(temp_root=str(tmp_path / "scopes")) as s:
        yield s


def _pipeline(scope, route):
    return Pipeline(Executor(scope), route if callable(route) else (lambda info: route))


# --- policy -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("baseline,stored,seconds,accepted,reason", [
    (1000, 1000, 0.1, False, "not smaller than the baseline"),
    (1000, 1001, 0.1, False, "not smaller than the baseline"),
    (1000, 971, 0.1, False, "saving too small"),             # 2.9 %
    (1000, 970, 0.1, True, "accepted"),                      # exactly 3 %
    (100 * MIB, 99 * MIB, 0.1, True, "accepted"),             # 1 MiB saved is enough on its own
    (100 * MIB, 99 * MIB + 1, 0.1, False, "saving too small"),
    (10 * MIB, 9 * MIB, 100.0, True, "accepted"),            # slow does not change a size-based decision
    (10 * MIB, 9 * MIB, 0.001, True, "accepted"),
    (0, 0, 0.0, False, "not smaller than the baseline"),
])
def test_policy_boundaries(baseline, stored, seconds, accepted, reason):
    verdict = P.judge(max(baseline, 1), baseline, stored, seconds, 0.0)
    assert (verdict.accepted, verdict.reason) == (accepted, reason), verdict


def test_policy_tiny_file_limit():
    assert not P.worth_trying(4095) and P.worth_trying(4096)


# --- scan / baseline ----------------------------------------------------------------------------------

def _zip_member_size(path, compression):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        z.write(path, "m")
    with zipfile.ZipFile(buf) as z:
        return z.infolist()[0].compress_size


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


# --- pipeline -----------------------------------------------------------------------------------------

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
            assert os.path.isfile(d.stored_path)


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
