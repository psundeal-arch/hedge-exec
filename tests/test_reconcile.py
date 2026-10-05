"""代码审查 C1：结果不明的单子必须对账，不能当失败重试。

假交易所把「下单」和「查状态」分开：下单就真的在交易所成交（记在 exchange_*），
查状态可以被编排成报错。断言的是【交易所上的真实仓位】和程序记账一致。
"""
import pytest

from hedgebot.book import Book
from hedgebot.http import SourceError, TransientError
from hedgebot.ladder import Ladder, Params
from hedgebot.trade_api import OrderState, OrderUnknown

CS = 5.0


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Spot:
    """挂单第一次查询即整块成交。"""
    name, tick, lot, min_qty = "coinbase", 0.01, 0.001, 0.001

    def __init__(self, ambiguous_place=False):
        self.ambiguous_place = ambiguous_place
        self.orders = {}
        self.exchange_bought = 0.0

    def book(self):
        return Book(bids=((100.0, 1e6),), asks=((100.1, 1e6),))

    def place_post_only(self, side, qty, px):
        oid = f"o{len(self.orders)}"
        self.orders[oid] = {"qty": qty, "px": px, "filled": qty, "open": False, "cid": f"c{oid}"}
        self.exchange_bought += qty                       # 交易所上真的成交了
        if self.ambiguous_place:
            self.ambiguous_place = False
            raise OrderUnknown("挂单请求超时", leg=self.name, side=side, qty=qty, cid=f"c{oid}")
        return oid

    def find(self, u):
        if u.oid:
            return u.oid
        return next((k for k, o in self.orders.items() if o["cid"] == u.cid), None)

    def status(self, oid):
        o = self.orders[oid]
        return OrderState(o["filled"], o["px"], 0.0, o["open"], "FILLED")

    def cancel(self, oid):
        self.orders[oid]["open"] = False

    def take(self, side, qty, limit_px):
        self.exchange_bought += qty if side == "buy" else -qty
        return OrderState(qty, 100.0, 0.0, False, "FILLED")


class Perp:
    """take 在交易所真的成交，然后按编排让「查状态」报错。"""
    name = "cde"

    def __init__(self, *, unknown_takes=0, status_ok=True):
        self.unknown_takes = unknown_takes
        self.status_ok = status_ok
        self.orders = {}
        self.exchange_sold = 0
        self.take_calls = 0

    def book(self):
        return Book(bids=((100.2, 1000),), asks=((100.3, 1000),))

    def take(self, side, qty, limit_px):
        self.take_calls += 1
        oid = f"p{len(self.orders)}"
        self.orders[oid] = qty
        self.exchange_sold += qty                         # 已经成交了
        if self.unknown_takes > 0:
            self.unknown_takes -= 1
            raise OrderUnknown(f"cde 订单 {oid} 查询状态出错（HTTP 429）",
                               leg=self.name, side=side, qty=qty, oid=oid)
        return OrderState(qty, 100.2, 0.0, False, "FILLED")

    def find(self, u):
        return u.oid

    def status(self, oid):
        if not self.status_ok:
            raise TransientError("HTTP 503")
        return OrderState(self.orders[oid], 100.2, 0.0, False, "FILLED")

    def cancel(self, oid):
        pass


def run(spot, perp, contracts=3, **kw):
    clk = Clock()
    ld = Ladder(spot, perp, action="open", contracts=contracts, contract_size=CS,
                params=Params(poll_s=1, reconcile_wait_s=1, **kw),
                clock=clk.now, sleep=clk.sleep, say=lambda m: None)
    return ld.run()


def test_unseen_fill_is_reconciled_not_retried():
    """审查 C1 的复现场景：IOC 在交易所成交了、但查询报错。

    修复前：当失败重试 3 次 → 交易所 9 张裸空、程序报 unwound 零敞口。
    修复后：对账查到已成交 3 张，按实际记账，不再下第二单。
    """
    spot, perp = Spot(), Perp(unknown_takes=1)
    r = run(spot, perp, contracts=3)
    assert perp.take_calls == 1
    assert perp.exchange_sold == 3 == r.contracts
    assert r.status == "done"
    assert spot.exchange_bought == pytest.approx(15) == pytest.approx(r.spot_qty)


def test_unreconcilable_order_stops_everything():
    """查不清的单子：状态 unknown，之后不再下任何单（不对冲、不回滚）。"""
    spot, perp = Spot(), Perp(unknown_takes=1, status_ok=False)
    r = run(spot, perp, contracts=3)
    assert r.status == "unknown"
    assert "p0" in r.error                                # 报出订单号去交易所核对
    assert perp.take_calls == 1                           # 没有重试
    assert perp.exchange_sold == 3                        # 交易所真实状态：只有那一单


def test_ambiguous_post_only_is_found_by_client_id():
    """挂单请求超时，但单子其实挂上并成交了：按客户端订单号找到，整笔补记。"""
    spot, perp = Spot(ambiguous_place=True), Perp()
    r = run(spot, perp, contracts=3)
    assert r.status == "done"
    assert spot.exchange_bought == pytest.approx(r.spot_qty) == pytest.approx(15)
    assert perp.exchange_sold == r.contracts == 3


def test_coinbase_leg_marks_post_timeout_as_unknown(monkeypatch):
    """CoinbaseLeg：下单请求超时 → OrderUnknown 带客户端订单号；查询出错 → 带订单号。"""
    import json
    import hedgebot.trade_api as ta
    sent = {}

    class Auth:
        def call(self, method, path, label, body=None, params=None):
            if method == "POST":
                sent["cid"] = body["client_order_id"]
                raise TransientError("ReadTimeout")
            raise AssertionError("不应查询")

    leg = object.__new__(ta.CoinbaseLeg)
    leg.auth, leg.product_id, leg.is_perp, leg.name = Auth(), "ZEC-20DEC30-CDE", True, "cde"
    leg.unit, leg.tick, leg.lot, leg.min_qty = 1.0, 0.05, 1.0, 1.0
    with pytest.raises(OrderUnknown) as ei:
        leg.take("sell", 1, None)
    assert ei.value.cid == sent["cid"] and ei.value.oid is None

    class Auth2:
        def call(self, method, path, label, body=None, params=None):
            if method == "POST":
                return {"success": True, "success_response": {"order_id": "X1"}}
            raise TransientError("HTTP 429")
    leg.auth = Auth2()
    with pytest.raises(OrderUnknown) as ei:
        leg.take("sell", 1, None)
    assert ei.value.oid == "X1"


def test_coinbase_find_by_client_id():
    import hedgebot.trade_api as ta

    class Auth:
        def call(self, method, path, label, body=None, params=None):
            assert path.endswith("/orders/historical/batch")
            return {"orders": [{"order_id": "A", "client_order_id": "x"},
                               {"order_id": "B", "client_order_id": "want"}]}
    leg = object.__new__(ta.CoinbaseLeg)
    leg.auth, leg.product_id, leg.name = Auth(), "ZEC-USD", "coinbase"
    u = OrderUnknown("t", leg="coinbase", side="buy", qty=1, cid="want")
    assert leg.find(u) == "B"
    assert leg.find(OrderUnknown("t", leg="coinbase", side="buy", qty=1, cid="nope")) is None


def test_http_distinguishes_rejection_from_unknown(monkeypatch):
    import requests
    import hedgebot.http as h

    class R:
        def __init__(self, code):
            self.status_code, self.text, self.headers = code, "bad", {}

    monkeypatch.setattr(h.time, "sleep", lambda s: None)
    monkeypatch.setattr(h.requests, "request", lambda *a, **k: R(400))
    with pytest.raises(SourceError) as ei:
        h.request("POST", "u", "t", attempts=1)
    assert not isinstance(ei.value, TransientError)       # 明确拒绝：什么都没发生

    def boom(*a, **k):
        raise requests.Timeout("read timed out")
    monkeypatch.setattr(h.requests, "request", boom)
    with pytest.raises(TransientError):                   # 结果不明
        h.request("POST", "u", "t", attempts=1)
