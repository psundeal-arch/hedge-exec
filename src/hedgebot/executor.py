"""两腿执行：先下永续，再下现货；第二腿失败就回滚第一腿。

模拟盘与实盘共用这套流程，区别只在 Venue：PaperVenue 按订单簿吃单成交，
将来的实盘 Venue 换成真下单。

为什么永续先下：CDE 有闭市时段、只能整张、深度也薄，是更容易失败的一腿。
它失败时现货还没动，什么都不用撤；反过来先买了现货再发现永续下不了单，
就得把现货卖回去，白付两次手续费和价差。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .book import Book, walk


class ExecError(RuntimeError):
    """单腿下单失败（整笔没成交）。"""


class LegError(ExecError):
    """两腿没能一起成交。fills 是实际发生了的成交，包括回滚那笔。

    naked=True 表示回滚也失败了 —— 账上有一条没对冲的腿，必须人工处理。
    """
    def __init__(self, msg: str, fills: list, naked: bool):
        super().__init__(msg)
        self.fills = fills
        self.naked = naked


@dataclass(frozen=True)
class Fill:
    venue: str
    side: str          # buy / sell
    qty: float         # 现货是币，永续是张
    price: float
    fee: float         # 美元
    ts: str


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class PaperVenue:
    """按订单簿快照吃单成交。簿子在一笔交易内不变（不模拟自己的冲击被补单）。"""

    def __init__(self, name: str, book: Book, fee_rate: float, multiplier: float = 1.0,
                 available: bool = True, reason: str = ""):
        self.name = name
        self.book = book
        self.fee_rate = fee_rate
        self.multiplier = multiplier       # 永续：每张多少币；现货：1
        self.available = available
        self.reason = reason

    def market(self, side: str, qty: float) -> Fill:
        if not self.available:
            raise ExecError(f"{self.name} 不可交易：{self.reason}")
        levels = self.book.asks if side == "buy" else self.book.bids
        avg, filled = walk(levels, qty)
        if avg is None or filled + 1e-9 < qty:
            raise ExecError(f"{self.name} 深度不够：要 {qty:g}，只有 {filled:g}")
        fee = avg * qty * self.multiplier * self.fee_rate
        return Fill(self.name, side, qty, avg, fee, _now())


def check_slippage(book: Book, side: str, qty: float, max_bps: float, label: str) -> float:
    """下单前预检：成交均价偏离中间价超限就整笔不做。返回预估滑点（基点）。"""
    mid = book.mid
    if mid is None:
        raise ExecError(f"{label} 订单簿是空的（闭市？）")
    avg, filled = walk(book.asks if side == "buy" else book.bids, qty)
    if avg is None or filled + 1e-9 < qty:
        raise ExecError(f"{label} 深度不够：要 {qty:g}，只有 {filled:g}")
    bps = abs(avg / mid - 1) * 1e4
    if bps > max_bps:
        raise ExecError(f"{label} 预估滑点 {bps:.1f}bp 超过上限 {max_bps:g}bp")
    return bps


def _flip(side: str) -> str:
    return "sell" if side == "buy" else "buy"


def run_pair(first, first_side: str, first_qty: float,
             second, second_side: str, second_qty: float) -> tuple[Fill, Fill]:
    """依次成交两腿。第二腿失败 → 反向平掉第一腿，再抛 LegError。"""
    f1 = first.market(first_side, first_qty)    # 失败直接抛 ExecError，什么都没发生
    try:
        f2 = second.market(second_side, second_qty)
    except ExecError as e:
        try:
            undo = first.market(_flip(first_side), first_qty)
        except ExecError as e2:
            raise LegError(f"第二腿失败（{e}），回滚第一腿也失败（{e2}）——"
                           f"{first.name} 上有一条没对冲的仓位，需要人工处理",
                           fills=[f1], naked=True) from e2
        raise LegError(f"第二腿失败，已回滚第一腿：{e}", fills=[f1, undo],
                       naked=False) from e
    return f1, f2
