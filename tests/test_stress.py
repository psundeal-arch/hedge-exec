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


def test_one_status_error_is_tolerated():
    """查挂单时偶发一次 503：挂单还在交易所，下一轮再查 —— 不再整笔停下。

    （C1 之前这里会直接走安全出口；那会按【过期的】成交量去对冲，正是审查指出的
    「现货腿同一个毛病」。现在一次查询失败只是抖动，连续查不到才转入对账。）
    """
    spot = Spot(fill_all=True, status_error_at=1)
    perp = Perp()
    r = go(spot, perp)
    assert r.status == "done" and r.contracts == 3
    assert r.residual(CS) == pytest.approx(0)


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


def test_session_risk_window():
    import datetime as dt
    from hedgebot.live import session_risk
    now = dt.datetime(2026, 10, 1, 20, 50, tzinfo=dt.timezone.utc)
    p = {"close_time": "2026-10-01T21:00:00Z",
         "maintenance": {"start_time": "2026-10-24T10:00:00Z", "end_time": "2026-10-24T19:00:00Z"}}
    assert "切换" in session_risk(p, now, 900)                       # 10 分钟后切换，窗口 15 分钟
    assert session_risk(p, now - dt.timedelta(hours=1), 900) is None
    during = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.timezone.utc)
    assert "维护" in session_risk({"maintenance": p["maintenance"]}, during, 900)
    before = dt.datetime(2026, 10, 24, 9, 50, tzinfo=dt.timezone.utc)
    assert "维护" in session_risk({"maintenance": p["maintenance"]}, before, 900)


def test_market_step_sends_no_limit():
    """升级到最后一档时永续发真正的市价单（limit=None），而不是一个很远的限价。"""
    deep_only_far = lambda: Book(bids=((100.2, 0.5), (60.0, 100)), asks=((100.3, 100),))  # noqa: E731
    spot, perp = Spot(), Perp(deep_only_far)
    r = go(spot, perp, contracts=1)
    assert r.contracts == 1
    assert perp.takes[-1][2] is None


class SyncRejectSpot(Spot):
    """Coinbase 式的同步拒单：下单请求直接报错，不生成订单。"""

    def __init__(self, n_reject, **kw):
        super().__init__(**kw)
        self.n_reject = n_reject

    def place_post_only(self, side, qty, px):
        from hedgebot.trade_api import PostOnlyRejected
        if self.n_reject > 0:
            self.n_reject -= 1
            raise PostOnlyRejected("会变成吃单，被拒")
        return super().place_post_only(side, qty, px)


def test_sync_post_only_reject_retries():
    """2026-10-01 实盘踩到：Coinbase 同步拒只挂单，原来被当成意外错误整笔停下。"""
    spot, perp = SyncRejectSpot(2), Perp()
    r = go(spot, perp)
    assert r.status == "done" and r.contracts == 3
    assert sum("被拒" in m for m in r.log) == 2


def test_endless_post_only_rejects_stop_safely():
    spot, perp = SyncRejectSpot(10**6), Perp()
    r = go(spot, perp, max_post_rejects=5)
    assert r.status == "error" and "单边" in r.error
    assert r.spot_qty == 0 and r.contracts == 0


def test_venue_pacing_floor_overrides_config():
    """Kraken 腿自带 3 秒查单 / 15 秒重挂的下限，配置里写得再小也不会更快。"""
    class SlowVenue(Spot):
        min_poll_s, min_reprice_s = 3.0, 15.0
    ld = Ladder(SlowVenue(), Perp(), action="open", contracts=1, contract_size=CS,
                params=Params(poll_s=1, reprice_min_s=2), say=lambda m: None)
    assert ld.p.poll_s == 3.0 and ld.p.reprice_min_s == 15.0


def test_kraken_rate_limit_retries_with_fresh_nonce(monkeypatch):
    import hedgebot.trade_api as ta
    calls = []

    def fake_request(method, url, label, **kw):
        calls.append(kw["data"]["nonce"])
        if len(calls) == 1:
            return {"error": ["EAPI:Rate limit exceeded"]}
        return {"error": [], "result": {"ok": 1}}
    monkeypatch.setattr(ta, "request", fake_request)
    monkeypatch.setattr(ta.time, "sleep", lambda s: None)
    auth = ta._KrakenAuth("k", "c2VjcmV0")
    assert auth.call("/0/private/QueryOrders", {"txid": "x"}, "t") == {"ok": 1}
    assert len(calls) == 2 and calls[1] > calls[0]


class TakerSpot(Spot):
    """Robinhood 式只吃单的现货腿。take 每次按要求数量全部成交。"""
    taker_only = True

    def place_post_only(self, *a, **k):
        raise AssertionError("只吃单的腿不应挂单")


def test_taker_only_leg_hedges_one_contract_at_a_time():
    spot, perp = TakerSpot(), Perp()
    unhedged = []
    clk = Clock()
    ld = Ladder(spot, perp, action="open", contracts=3, contract_size=CS, params=Params(poll_s=1),
                clock=clk.now, sleep=clk.sleep, say=lambda m: None,
                on_fill=lambda **f: unhedged.append(ld.r.residual(CS)) if f["venue"] == "cde" else None)
    r = ld.run()
    assert r.status == "done" and r.contracts == 3 and r.spot_qty == pytest.approx(15)
    assert spot.takes == [("buy", 5), ("buy", 5), ("buy", 5)]       # 逐张
    # 原来这行是「X == [...] or len(...) == 3」，前半句拿被测列表自己的值比，后半句只查长度（审查 M9）
    assert [(side, q) for side, q, _ in perp.takes] == [("sell", 1)] * 3, perp.takes   # 逐张、每次 1 张
    assert all(lim is not None for _, _, lim in perp.takes), "第一档就成交，应带保护价而不是市价"
    assert r.max_unhedged <= CS + 1e-9 and max(unhedged) < 1e-9


def test_taker_only_close_and_unwind():
    r = go(TakerSpot(), Perp(error=True), contracts=2)
    assert r.status == "unwound" and r.spot_qty == pytest.approx(0)
    spot = TakerSpot()
    clk = Clock()
    r = Ladder(spot, Perp(), action="close", contracts=2, contract_size=CS, params=Params(poll_s=1),
               clock=clk.now, sleep=clk.sleep, say=lambda m: None).run()
    assert r.status == "done" and spot.takes == [("sell", 5), ("sell", 5)]
