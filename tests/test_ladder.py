"""用假交易所测执行引擎。不联网、不下单。"""
import pytest

from hedgebot.book import Book
from hedgebot.http import SourceError
from hedgebot.ladder import Ladder, Params
from hedgebot.trade_api import OrderState


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeSpot:
    """挂单每被查询一次就成交 step 个，直到挂满；step=0 表示永远不成交。"""
    name = "coinbase"
    tick, lot, min_qty = 0.01, 0.001, 0.001

    def __init__(self, step, bid=100.0, ask=100.1):
        self.step = step
        self.bid, self.ask = bid, ask
        self.orders = {}
        self.placed = []
        self.takes = []

    def book(self):
        return Book(bids=((self.bid, 1e6),), asks=((self.ask, 1e6),))

    def place_post_only(self, side, qty, px):
        oid = f"o{len(self.orders)}"
        self.orders[oid] = {"qty": qty, "px": px, "filled": 0.0, "open": True, "side": side}
        self.placed.append((side, qty, px))
        return oid

    def status(self, oid):
        o = self.orders[oid]
        if o["open"] and self.step:
            o["filled"] = min(o["qty"], o["filled"] + self.step)
            if o["filled"] >= o["qty"] - 1e-12:
                o["open"] = False
        f = o["filled"]
        return OrderState(f, o["px"] if f else None, f * o["px"] * 0.0015, o["open"],
                          "OPEN" if o["open"] else "FILLED")

    def cancel(self, oid):
        self.orders[oid]["open"] = False

    def take(self, side, qty, limit_px):
        px = self.ask if side == "buy" else self.bid
        self.takes.append((side, qty))
        return OrderState(qty, px, qty * px * 0.003, False, "FILLED")


class FakePerp:
    name = "cde"

    def __init__(self, fail=False):
        self.fail = fail
        self.takes = []

    def book(self):
        return Book(bids=((100.2, 1000),), asks=((100.3, 1000),))

    def take(self, side, qty, limit_px):
        if self.fail:
            raise SourceError("保证金不足")
        self.takes.append((side, qty))
        px = 100.2 if side == "sell" else 100.3
        return OrderState(qty, px, qty * 0.12, False, "FILLED")


def run(spot, perp, action="open", contracts=3, **kw):
    clk = Clock()
    unhedged = []

    def on_fill(**f):
        # 每次空单跟进之后，没对冲的现货必须少于一张
        if f["venue"] == "cde":
            unhedged.append(ld.r.residual(5))

    p = Params(poll_s=1, reprice_min_s=2, max_wait_s=kw.pop("max_wait_s", 100), **kw)
    ld = Ladder(spot, perp, action=action, contracts=contracts, contract_size=5, params=p,
                on_fill=on_fill, clock=clk.now, sleep=clk.sleep, say=lambda m: None)
    return ld.run(), unhedged


def test_open_hedges_each_contract_as_spot_fills():
    spot, perp = FakeSpot(step=4), FakePerp()
    r, unhedged = run(spot, perp)
    assert r.status == "done"
    assert r.spot_qty == pytest.approx(15) and r.contracts == 3
    assert r.residual(5) == pytest.approx(0)
    # 4 → 8（对冲 1 张）→ 12（再 1 张）→ 15（最后 1 张）：每张单独跟进
    assert perp.takes == [("sell", 1), ("sell", 1), ("sell", 1)]
    # 每次跟进后没对冲的都不足一张合约
    assert max(unhedged) < 5 - 1e-9
    # 价差 0.1 > 1.5 tick → 比买一高一档挂
    assert spot.placed[0] == ("buy", 15, pytest.approx(100.01))


def test_timeout_complete_buys_rest_and_hedges():
    spot, perp = FakeSpot(step=0), FakePerp()
    r, _ = run(spot, perp, max_wait_s=10)
    assert r.status == "done"
    assert spot.takes == [("buy", 15)]
    assert r.contracts == 3 and r.residual(5) == pytest.approx(0)


def test_timeout_trim_sells_back_fraction():
    spot, perp = FakeSpot(step=7), FakePerp()
    spot.step_limit = 7

    # 第一次查询成交 7 个，之后不再成交
    orig = spot.status
    calls = {"n": 0}

    def status(oid):
        calls["n"] += 1
        if calls["n"] > 1:
            spot.step = 0
        return orig(oid)
    spot.status = status
    r, _ = run(spot, perp, max_wait_s=10, finish="trim")
    assert r.status == "trimmed"
    assert r.contracts == 1
    assert spot.takes == [("sell", pytest.approx(2))]      # 7 - 5 = 2 个零头退掉
    assert r.residual(5) == pytest.approx(0)


def test_hedge_failure_aborts_and_cancels_spot():
    spot, perp = FakeSpot(step=6), FakePerp(fail=True)
    r, _ = run(spot, perp)
    assert r.status == "aborted"
    assert r.contracts == 0
    assert all(not o["open"] for o in spot.orders.values())   # 挂单撤掉了
    assert r.spot_qty > 0                                     # 已成交的现货成了裸腿，如实报告


def test_close_flips_sides():
    spot, perp = FakeSpot(step=5), FakePerp()
    r, _ = run(spot, perp, action="close", contracts=2)
    assert r.status == "done"
    assert spot.placed[0][0] == "sell" and spot.placed[0][2] == pytest.approx(100.09)
    assert perp.takes == [("buy", 1), ("buy", 1)]


def test_reprice_when_outbid():
    spot, perp = FakeSpot(step=0), FakePerp()
    clk = Clock()
    ld = Ladder(spot, perp, action="open", contracts=1, contract_size=5,
                params=Params(poll_s=1, reprice_min_s=2, max_wait_s=6),
                clock=clk.now, sleep=clk.sleep, say=lambda m: None)
    ld._place()
    spot.bid = 100.05                       # 有人出价更高
    clk.sleep(3)
    assert ld._stale()
    ld._cancel()
    ld._place()
    assert spot.placed[-1][2] == pytest.approx(100.06)
