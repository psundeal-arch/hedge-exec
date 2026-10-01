"""压力场景：价格剧变、簿子被砸空、接口出错时，执行引擎的行为。

每个场景都检查三件事：最终两边是否对齐（或已回到零敞口）、运行中的最大裸敞口、
状态码是否如实反映发生了什么。
"""
import pytest

from hedgebot.book import Book
from hedgebot.http import SourceError
from hedgebot.ladder import Ladder, Params
from hedgebot.trade_api import OrderState

CS = 5.0          # 每张 5 个币（LTC 风格），价格 ~100 → 一张 ≈ $500


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Spot:
    """可编排的现货腿。fill_all：挂单在第一次查询时整块成交（被砸穿）。"""
    name, tick, lot, min_qty = "coinbase", 0.01, 0.001, 0.001

    def __init__(self, *, fill_all=True, reject_first=0, status_error_at=None, take_fill=1.0):
        self.fill_all = fill_all
        self.reject_first = reject_first
        self.status_error_at = status_error_at
        self.take_fill = take_fill            # 吃单能成交的比例（0 = 簿子空了）
        self.orders, self.placed, self.takes = {}, [], []
        self.status_calls = 0

    def book(self):
        return Book(bids=((100.0, 1e6),), asks=((100.1, 1e6),))

    def place_post_only(self, side, qty, px):
        oid = f"o{len(self.orders)}"
        rejected = len(self.placed) < self.reject_first
        self.orders[oid] = {"qty": qty, "px": px, "filled": 0.0, "open": not rejected,
                            "reason": "post-only" if rejected else ""}
        self.placed.append((side, qty, px))
        return oid

    def status(self, oid):
        self.status_calls += 1
        if self.status_error_at and self.status_calls == self.status_error_at:
            raise SourceError("HTTP 503")
        o = self.orders[oid]
        if o["open"] and self.fill_all:
            o["filled"], o["open"] = o["qty"], False
        f = o["filled"]
        return OrderState(f, o["px"] if f else None, 0.0, o["open"],
                          "FILLED" if f else ("OPEN" if o["open"] else "CANCELLED"), o["reason"])

    def cancel(self, oid):
        self.orders[oid]["open"] = False

    def take(self, side, qty, limit_px):
        got = qty * self.take_fill
        self.takes.append((side, got))
        return OrderState(got, 100.0 if got else None, 0.0, False, "FILLED" if got else "CANCELLED")


class Perp:
    """可编排的永续腿。book 是函数，可以模拟被砸穿的簿子。"""
    name = "cde"

    def __init__(self, book=None, *, error=False, max_per_take=None):
        self._book = book or (lambda: Book(bids=((100.2, 1000),), asks=((100.3, 1000),)))
        self.error = error
        self.max_per_take = max_per_take
        self.takes = []

    def book(self):
        return self._book()

    def take(self, side, qty, limit_px):
        if self.error:
            raise SourceError("INSUFFICIENT_MARGIN")
        got = min(qty, self.max_per_take or qty)
        self.takes.append((side, got, limit_px))
        return OrderState(got, 100.2, 0.0, False, "FILLED" if got == qty else "PARTIAL")


def go(spot, perp, contracts=3, sleep=None, **kw):
    clk = Clock()
    p = Params(poll_s=1, reprice_min_s=2, max_wait_s=kw.pop("max_wait_s", 100), **kw)
    ld = Ladder(spot, perp, action="open", contracts=contracts, contract_size=CS, params=p,
                clock=clk.now, sleep=sleep or clk.sleep, say=lambda m: None)
    return ld.run()


def test_thin_book_escalates_instead_of_giving_up():
    """现货整块成交后 CDE 簿子被砸薄：吃 3 张均价偏离 130bp —— 升到 300bp 档成交，不放弃。"""
    thin = lambda: Book(bids=((100.2, 1), (99.5, 1), (97.0, 100)), asks=((100.3, 100),))  # noqa: E731
    spot, perp = Spot(), Perp(thin)
    r = go(spot, perp)
    assert r.status == "done" and r.contracts == 3
    assert r.residual(CS) == pytest.approx(0)
    assert len(perp.takes) == 1
    assert perp.takes[0][2] == pytest.approx(100.2 * (1 - 0.03))     # 300bp 档的保护价
    assert any("放宽一档" in m for m in r.log)


def test_clip_caps_unhedged_when_order_is_swept():
    """10 张、每块 $1100（2 张，一张约 $500）：每块都被一口吃掉，最大裸敞口也只有一块。"""
    spot, perp = Spot(), Perp()
    r = go(spot, perp, contracts=10, clip_usd=1100)
    assert r.status == "done" and r.contracts == 10
    assert len(spot.placed) == 5
    assert r.max_unhedged <= 2 * CS + 1e-9
    # 对照：不分块时整单被吃，裸敞口是全部 10 张
    r2 = go(Spot(), Perp(), contracts=10, clip_usd=1e9)
    assert r2.max_unhedged == pytest.approx(10 * CS)


def test_partial_fills_on_cde_keep_going():
    """CDE 每次只成交 1 张：同一轮里逐档继续吃，最终补齐。"""
    spot, perp = Spot(), Perp(max_per_take=1)
    r = go(spot, perp)
    assert r.status == "done" and r.contracts == 3


def test_cde_halt_unwinds_spot():
    """CDE 簿子空了（熔断/停牌）：连续硬失败 → 把已买的现货卖回去，回到零敞口。"""
    empty = lambda: Book(bids=(), asks=())  # noqa: E731
    spot, perp = Spot(), Perp(empty)
    r = go(spot, perp)
    assert r.status == "unwound"
    assert r.contracts == 0 and r.spot_qty == pytest.approx(0)
    assert spot.takes[-1][0] == "sell"


def test_margin_rejection_unwinds_spot():
    spot, perp = Spot(), Perp(error=True)
    r = go(spot, perp)
    assert r.status == "unwound" and r.spot_qty == pytest.approx(0)


def test_unwind_failure_reports_aborted():
    """CDE 下不去，现货也卖不回去：如实报告 aborted，裸腿数量留在结果里。"""
    spot, perp = Spot(take_fill=0.0), Perp(error=True)
    r = go(spot, perp)
    assert r.status == "aborted"
    assert r.residual(CS) == pytest.approx(15)


def test_api_error_takes_safe_exit():
    """查挂单时接口 503：撤单、对冲已成交的整张、状态 error —— 不是直接崩溃。"""
    spot = Spot(fill_all=False, status_error_at=1)
    perp = Perp()
    r = go(spot, perp)
    assert r.status == "error" and "503" in r.error
    assert all(not o["open"] for o in spot.orders.values())


def test_post_only_reject_storm_still_completes():
    spot, perp = Spot(reject_first=3), Perp()
    r = go(spot, perp)
    assert r.status == "done" and r.contracts == 3
    assert len(spot.placed) == 4


def test_ctrl_c_hedges_what_filled():
    """第一块成交后按 Ctrl-C：撤单，已成交的整张对冲掉。"""
    spot, perp = Spot(), Perp()
    clk = Clock()
    calls = {"n": 0}

    def sleep(s):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        clk.sleep(s)
    ld = Ladder(spot, perp, action="open", contracts=10, contract_size=CS,
                params=Params(poll_s=1, clip_usd=1000), clock=clk.now, sleep=sleep,
                say=lambda m: None)
    r = ld.run()
    assert r.status == "interrupted"
    assert 0 < r.contracts < 10
    assert r.residual(CS) == pytest.approx(0)
