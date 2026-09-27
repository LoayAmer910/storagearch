"""The registry of strategies. A name here is what gets recorded with every stored member, so a name is
never reused for a different format (bump StrategySpec.version instead)."""
from core.strategies.base import Strategy, StrategyError, StrategySpec
from core.strategies.codecs import (
    BrotliStrategy, DeflateStrategy, LzmaStrategy, LzmaX86Strategy, ZlibStrategy, ZstdLongRangeStrategy, ZstdStrategy,
)
from core.strategies.jpeg_xl import BmpJxlStrategy, JpegXlStrategy

BASELINE = "deflate"

REGISTRY = {s.name: s for s in (
    DeflateStrategy(), ZstdStrategy(), ZstdLongRangeStrategy(), ZlibStrategy(), LzmaStrategy(), LzmaX86Strategy(),
    BrotliStrategy(), JpegXlStrategy(), BmpJxlStrategy(),
)}


def get(name):
    try:
        return REGISTRY[name]
    except KeyError:
        raise StrategyError(f"unknown strategy {name!r}") from None


__all__ = ["BASELINE", "REGISTRY", "Strategy", "StrategyError", "StrategySpec", "get"]
