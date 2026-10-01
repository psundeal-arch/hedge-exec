"""订单簿与「吃单成交均价」。两个交易所共用。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Book:
    """bids 从高到低、asks 从低到高，每档 (价格, 数量)。

    数量的单位随交易所：Kraken 是币，CDE 是【张】。
    """
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if not (self.bids and self.asks):
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2


def walk(levels, qty: float) -> tuple[float | None, float]:
    """按档位吃 qty，返回 (成交均价, 实际成交量)。深度不够时成交量 < qty。"""
    left, cost = qty, 0.0
    for px, sz in levels:
        take = min(left, sz)
        cost += take * px
        left -= take
        if left <= 1e-12:
            left = 0.0
            break
    filled = qty - left
    return (cost / filled if filled > 0 else None), filled
