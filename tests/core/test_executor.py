"""core.executor - running one candidate under its limits, in process or in a worker."""
import concurrent.futures
import os
import time

import pytest

from core import candidate, content, strategies
from core.executor import Executor, RestoreFailed
from core.scope import OperationScope
from support import corpus, fakes
from support.execution import live_children, pid_from


FAULTS = {
    "raises": (fakes.Raising, candidate.ERROR),
    "wrong_restore": (fakes.WrongRestore, candidate.MISMATCH),
    "missing_output": (fakes.MissingOutput, candidate.ERROR),
    "outside_work_dir": (fakes.OutsideWorkDir, candidate.ERROR),
    "crashes": (fakes.Crashing, candidate.CRASHED),
    "hangs": (fakes.Hanging, candidate.TIMEOUT),
}


@pytest.mark.parametrize("in_process", [True, False], ids=["thread", "worker"])
def test_a_good_candidate_is_verified_and_kept_inside_the_scope(in_process, src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(fakes.Fake(in_process=in_process), src, info)
        assert outcome.verified and outcome.where == ("thread" if in_process else "worker"), outcome
        assert os.path.commonpath([outcome.stored_path, scope.root]) == scope.root
        assert open(outcome.stored_path, "rb").read() == open(src, "rb").read()
        root = scope.root
    assert not os.path.exists(root)


# Only a worker can survive a crash or be killed, so the executor never runs those two in-process.
FAULT_CASES = [(fault, in_process) for fault in FAULTS for in_process in (True, False)
               if not (in_process and fault in ("crashes", "hangs"))]


@pytest.mark.parametrize("fault, in_process", FAULT_CASES,
                         ids=[f"{f}-{'thread' if p else 'worker'}" for f, p in FAULT_CASES])
def test_a_failing_candidate_becomes_a_status_and_leaves_nothing(fault, in_process, src, info, tmp_path):
    cls, expected = FAULTS[fault]
    marker = str(tmp_path / "ran")
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        start = time.monotonic()
        outcome = Executor(scope).run(cls(name=f"f_{fault}", in_process=in_process, marker=marker), src, info)
        elapsed = time.monotonic() - start
        pid_from(marker)
        assert outcome.status == expected and outcome.stored_path is None, outcome
        assert os.listdir(scope.root) == [], f"a failed candidate left {os.listdir(scope.root)}"
        if fault == "crashes":
            assert elapsed < fakes.TIMEOUT_S, f"a crash took {elapsed:.1f}s to notice - it waited for the timeout"
        if fault == "hangs":
            assert elapsed < fakes.TIMEOUT_S + 3, f"took {elapsed:.1f}s with a {fakes.TIMEOUT_S}s timeout"
    assert live_children() == []


def test_parallel_candidates_never_get_each_others_results(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        ex = Executor(scope, pool_size=3)
        tags = [f"t{i}" for i in range(15)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda t: ex.run(fakes.Tagged(t), src, info), tags))
        for tag, outcome in zip(tags, outcomes):
            assert outcome.verified, outcome
            assert open(outcome.stored_path, "rb").read().endswith(f"|{tag}".encode()), f"{tag} got another result"


def test_unavailable_strategies_never_run(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(fakes.Unavailable(), src, info)
    assert outcome.status == candidate.UNAVAILABLE and "tool missing" in outcome.detail



class _AvailabilityCheckFails(fakes.Fake):
    def available(self):
        raise PermissionError("tools folder not readable")


def test_a_strategy_whose_availability_check_fails_is_unavailable_not_an_error(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(_AvailabilityCheckFails(), src, info)
    assert outcome.status == candidate.UNAVAILABLE
    assert outcome.detail == "PermissionError: tools folder not readable"


def test_a_restore_in_a_cancelled_operation_is_refused(src, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        scope.cancel()
        with pytest.raises(RestoreFailed, match="operation cancelled"):
            Executor(scope).restore(fakes.Fake(), src, str(tmp_path / "out"), os.path.getsize(src))


def test_large_inputs_go_to_a_worker_even_for_in_process_safe_strategies(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(fakes.Fake(in_process=True, max_bytes=info.size - 1), src, info)
    assert outcome.verified and outcome.where == "worker"


@pytest.mark.parametrize("in_process", [True, False], ids=["thread", "worker"])
def test_real_strategies_verify_through_the_executor(in_process, tmp_path):
    samples = corpus.build(str(tmp_path / "corpus"))
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        ex = Executor(scope, allow_in_process=in_process)
        for sample_name, name in (("text.log", "zstd"), ("program.exe", "lzma_x86"), ("text.csv", "brotli"),
                                  ("large_text.txt", "zstd_ldm"), ("image.jpg", "jpeg_xl"), ("image.bmp", "bmp_jxl")):
            strategy = strategies.get(name)
            if not strategy.available()[0]:
                continue
            outcome = ex.run(strategy, samples[sample_name].path, content.classify(samples[sample_name].path))
            assert outcome.verified, (sample_name, name, outcome)
