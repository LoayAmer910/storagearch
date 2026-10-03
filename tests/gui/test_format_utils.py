"""gui.format_utils - sizes as the window shows them, and the archive size choices."""
import pytest

from gui.format_utils import ARCHIVE_SIZE_PRESETS, format_bytes


@pytest.mark.parametrize("size, shown", [
    (None, "-"),
    (0, "0 B"),
    (1023, "1023 B"),
    (1024, "1.00 KB"),
    (1536, "1.50 KB"),
    (5 * 1024 ** 2, "5.00 MB"),
    (2 * 1024 ** 3, "2.00 GB"),
    (3 * 1024 ** 4, "3.00 TB"),
    (2048 * 1024 ** 4, "2048.00 TB"),  # TB is the largest unit, so huge sizes stay in TB
])
def test_sizes_are_shown_in_the_largest_fitting_unit(size, shown):
    assert format_bytes(size) == shown


def test_each_archive_size_choice_is_labelled_with_its_own_size():
    assert [label for label, _ in ARCHIVE_SIZE_PRESETS] == ["1 GB", "2 GB", "5 GB", "10 GB"]
    assert all(format_bytes(size) == label.replace(" GB", ".00 GB") for label, size in ARCHIVE_SIZE_PRESETS)
