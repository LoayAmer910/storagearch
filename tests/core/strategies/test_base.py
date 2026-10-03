"""core.strategies.base - the strategy contract: no strategy can be given a file name."""
import inspect

from core import strategies


def test_no_strategy_can_receive_a_file_name():
    for s in strategies.REGISTRY.values():
        assert list(inspect.signature(s.compress).parameters) == ["src_path", "work_dir", "time_limit_s"], s.name
        assert list(inspect.signature(s.restore).parameters) == ["stored_path", "work_dir", "time_limit_s"], s.name
