"""core.candidate - one candidate run: compress, restore, compare the SHA-256. Every failure of a strategy is a
result with a reason, never an exception for the caller."""
import hashlib
import os

from core import candidate
from support import fakes


class _RestoresOutsideItsFolder(fakes.Fake):
    def restore(self, stored_path, work_dir, time_limit_s=None):
        return stored_path  # the stored file itself, not a restored copy in the restore folder


class _RestoreFails(fakes.Fake):
    def restore(self, stored_path, work_dir, time_limit_s=None):
        raise OSError("disk read error")


class _EncodeFails:
    def encode(self, chunks, size):
        raise MemoryError("out of memory")


class _CorruptsInMemory:
    def encode(self, chunks, size):
        return [b"".join(chunks)]

    def decode(self, chunks):
        return [b"changed"]


class _CopiesInMemory:
    def encode(self, chunks, size):
        return [b"".join(chunks)]

    def decode(self, chunks):
        return list(chunks)


def test_a_good_candidate_is_verified_and_keeps_its_stored_file(src, tmp_path):
    result = candidate.run_candidate(fakes.Fake(), src, str(tmp_path), candidate.sha256_file(src))
    assert result["status"] == candidate.VERIFIED and os.path.isfile(result["stored_path"])
    assert result["stored_size"] == os.path.getsize(src)


def test_a_restore_that_does_not_give_back_the_original_is_a_mismatch(src, tmp_path):
    result = candidate.run_candidate(fakes.WrongRestore(), src, str(tmp_path), candidate.sha256_file(src))
    assert result["status"] == candidate.MISMATCH and result["stored_path"] is None


def test_a_restore_outside_its_own_folder_is_refused(src, tmp_path):
    result = candidate.run_candidate(_RestoresOutsideItsFolder(), src, str(tmp_path), candidate.sha256_file(src))
    assert result == {"status": candidate.ERROR, "detail": "restore did not return a file inside its work dir"}


def test_in_memory_a_good_candidate_is_verified_and_returns_its_bytes():
    data = b"hello " * 100
    result = candidate.run_candidate_in_memory(_CopiesInMemory(), data, hashlib.sha256(data).hexdigest())
    assert result["status"] == candidate.VERIFIED and result["stored_bytes"] == data


def test_in_memory_a_wrong_restore_is_a_mismatch():
    data = b"hello"
    result = candidate.run_candidate_in_memory(_CorruptsInMemory(), data, hashlib.sha256(data).hexdigest())
    assert result["status"] == candidate.MISMATCH and result["stored_bytes"] is None


def test_in_memory_a_failing_strategy_is_an_error_with_its_reason():
    result = candidate.run_candidate_in_memory(_EncodeFails(), b"data", "sha")
    assert result == {"status": candidate.ERROR, "detail": "MemoryError: out of memory"}


def test_restoring_a_stored_file_returns_the_restored_path(src, tmp_path):
    result = candidate.run_restore(fakes.Fake(), src, str(tmp_path))
    assert result["status"] == candidate.VERIFIED
    assert open(result["restored_path"], "rb").read() == open(src, "rb").read()


def test_a_restore_outside_the_target_folder_is_refused(src, tmp_path):
    result = candidate.run_restore(_RestoresOutsideItsFolder(), src, str(tmp_path / "out"))
    assert result == {"status": candidate.ERROR, "detail": "restore did not return a file inside its work dir"}


def test_a_restore_that_fails_is_an_error_with_its_reason(src, tmp_path):
    result = candidate.run_restore(_RestoreFails(), src, str(tmp_path))
    assert result == {"status": candidate.ERROR, "detail": "OSError: disk read error"}
