"""local_cloud.system_info - only coarse, privacy-safe facts about the PC, and "unknown" when they cannot be read."""
import platform

import pytest

from local_cloud import system_info

GIB = 1024 ** 3


def test_the_windows_version_and_architecture_are_reported():
    assert system_info.windows_version() == platform.platform()
    assert system_info.architecture() == platform.machine()


def test_the_computer_name_is_never_part_of_what_is_reported():
    reported = " ".join([system_info.windows_version(), system_info.architecture(), system_info.ram_bucket()])
    assert platform.node() not in reported


def _broken():
    raise OSError("not readable")


def test_facts_that_cannot_be_read_are_unknown(monkeypatch):
    monkeypatch.setattr(platform, "platform", _broken)
    monkeypatch.setattr(platform, "machine", lambda: "")
    assert system_info.windows_version() == "unknown"
    assert system_info.architecture() == "unknown"
    monkeypatch.setattr(platform, "machine", _broken)
    assert system_info.architecture() == "unknown"


def test_this_pc_has_a_memory_size_in_one_of_the_buckets():
    assert system_info._total_ram_bytes() > 0
    assert system_info.ram_bucket() in {"0-8gb", "8-16gb", "16-32gb", "32-64gb", "64gb_plus"}


@pytest.mark.parametrize("total, bucket", [
    (4 * GIB, "0-8gb"), (8 * GIB, "0-8gb"), (8 * GIB + 1, "8-16gb"), (16 * GIB, "8-16gb"),
    (24 * GIB, "16-32gb"), (64 * GIB, "32-64gb"), (128 * GIB, "64gb_plus"), (None, "unknown"),
])
def test_memory_is_reported_only_as_a_coarse_bucket(monkeypatch, total, bucket):
    monkeypatch.setattr(system_info, "_total_ram_bytes", lambda: total)
    assert system_info.ram_bucket() == bucket


def test_memory_that_cannot_be_read_is_unknown(monkeypatch):
    monkeypatch.setattr(system_info.ctypes, "windll", None)  # the call itself fails
    assert system_info._total_ram_bytes() is None
