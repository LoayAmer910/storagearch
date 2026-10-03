"""core.strategies - the registry of strategies."""
import pytest

from core import strategies
from core.strategies import StrategyError
from core.strategies.base import MIB
from support.strategies import GENERIC


def test_registry_is_consistent():
    assert set(strategies.REGISTRY) == set(GENERIC) | {"jpeg_xl", "bmp_jxl"}
    for name, s in strategies.REGISTRY.items():
        assert s.name == name and s.spec.version >= 1 and s.spec.weight in ("light", "heavy")
        assert 0 < s.spec.timeout_s(0) < s.spec.timeout_s(100 * MIB)
    with pytest.raises(StrategyError):
        strategies.get("no_such_strategy")
