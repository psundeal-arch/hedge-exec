"""hedge exec 排程器：假交易所 + 注入时钟，不联网、不下单。"""
import datetime as dt

import pytest

from hedgebot.book import Book
from hedgebot.exec_sched import Job, JobParams, Scheduler, Store, bad_time, parse_when
from hedgebot.exec_signal import Ref
from hedgebot.trade_api import OrderState

T0 = dt.datetime(2026, 10, 8, 10, 5, tzinfo=dt.timezone.utc).timestamp()   # 周四 10:05 UTC


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Spot:
    """挂单每查一次成交 step；take 按对手价全成交。"""
    name = "coinbase"
    tick, lot, min_qty = 0.01, 0.001, 0.001

    def __init__(self, step=1e9, bid=100.0, ask=100.02):
        self.step, self.bid, self.ask = step, bid, ask
        self.orders, self.takes, self.placed = {}, [], []

    def book(self):
        return Book(bids=((self.bid, 1e6),), asks=((self.ask, 1e6),))

    def place_post_only(self, side, qty, px):
        oid = f"o{len(self.orders)}"
        self.orders[oid] = {"qty": qty, "px": px, "filled": 0.0, "open": True}
        self.placed.append((side, qty, px))
        return oid

    def status(self, oid):
        o = self.orders[oid]
        if o["open"] and self.step:
            o["filled"] = min(o["qty"], o["filled"] + self.step)
            if o["filled"] >= o["qty"] - 1e-12:
                o["open"] = False
        f = o["filled"]
        return OrderState(f, o["px"] if f else None, f * o["px"] * 0.0015, o["open"], "OPEN" if o["open"] else "FILLED")

    def cancel(self, oid):
        self.orders[oid]["open"] = False

    def take(self, side, qty, limit_px):
        px = self.ask if side == "buy" else self.bid
        self.takes.append((side, qty, limit_px))
        return OrderState(qty, px, qty * px * 0.003, False, "FILLED")


class Perp:
    name = "cde"

    def __init__(self, bid=100.2, ask=100.22):
        self.bid, self.ask, self.takes = bid, ask, []

    def book(self):
        return Book(bids=((self.bid, 1000),), asks=((self.ask, 1000),))

    def take(self, side, qty, limit_px):
        self.takes.append((side, qty))
        px = self.bid if side == "sell" else self.ask
        return OrderState(qty, px, qty * 0.11, False, "FILLED")


def mk(kind, total, *, hours=4.0, spot=None, perp=None, ref=None, store=None, **pk):
    clk = Clock()
    job = Job(kind=kind, symbol="ZEC", venue="coinbase", total=total, start=clk.now(), end=clk.now() + hours * 3600,
              params=JobParams(**pk), id=1)
    spot = spot or Spot()
    sent = []
    sch = Scheduler(job, spot, perp, contract_size=1.0, ref_fn=lambda: ref or Ref(clk.now(), 100.0, 1.0, None),
                    store=store, notify=lambda t, m, p=0: sent.append((t, p)), clock=clk.now, sleep=clk.sleep,
                    say=lambda m: None, poll_s=60.0)
    return sch, job, spot, clk, sent


def test_spot_buy_follows_schedule_and_completes():
    sch, job, spot, clk, sent = mk("spot_buy", 4.0)
    sch.run()
    assert job.status == "done" and job.state.done == pytest.approx(4.0)
    assert len(spot.placed) == 4 and not spot.takes            # 全部挂单成交，没有吃单
    # 4 份分布在 4 小时里，不是一开始就买完
    assert clk.now() - T0 > 2.5 * 3600


def test_dip_buys_ahead_of_schedule():
    ref = Ref(T0, 101.0, 0.5, None)                            # 现价 100 比 VWAP 101 低 ~2 个 ATR
    sch, job, spot, clk, sent = mk("spot_buy", 4.0, ref=ref)
    sch.refresh_ref()
    act, q, why = sch.decide(clk.now())
    assert act == "go" and "逢低" in why


def test_no_chase_when_price_high_unless_urgent():
    ref = Ref(T0, 99.0, 0.5, None)                             # 现价比 VWAP 高 ~2 个 ATR
    sch, job, spot, clk, sent = mk("spot_buy", 4.0, ref=ref)
    sch.refresh_ref()
    clk.t = T0 + 0.3 * 4 * 3600                                # 落后一份：本该买，但价格高
    assert sch.decide(clk.now())[0] == "wait"
    clk.t = T0 + 0.9 * 4 * 3600                                # 临近截止：很急，照买
    assert sch.decide(clk.now())[0] == "go"


def test_deadline_completes_with_taker_or_cancels():
    sch, job, spot, clk, sent = mk("spot_buy", 2.0, spot=Spot(step=0), hours=1.0)
    sch.run()
    assert job.status == "done" and spot.takes and job.state.done == pytest.approx(2.0)
    sch2, job2, spot2, *_ = mk("spot_buy", 2.0, spot=Spot(step=0), hours=1.0, finish="cancel")
    sch2.run()
    assert job2.status == "partial" and not spot2.takes


def test_price_limit_pauses_and_notifies():
    sch, job, spot, clk, sent = mk("spot_buy", 1.0, limit=99.0, hours=0.5, finish="cancel")
    sch.run()
    assert job.state.done == 0 and any("暂停" in t for t, _ in sent)


def test_bad_time_waits():
    assert bad_time(dt.datetime(2026, 10, 8, 10, 59, tzinfo=dt.timezone.utc).timestamp())
    assert bad_time(dt.datetime(2026, 10, 9, 20, 45, tzinfo=dt.timezone.utc).timestamp())   # 周五闭市前
    assert not bad_time(T0)


def test_hedge_open_uses_ladder_and_basis_gate():
    perp = Perp(bid=100.0, ask=100.02)                          # 永续不比现货贵：基差 ≈ 0
    ref = Ref(T0, 100.0, 1.0, 10.0)                             # 近 24h 中位数 +10bp
    sch, job, spot, clk, sent = mk("hedge_open", 2.0, perp=perp, ref=ref)
    sch.refresh_ref()
    clk.t = T0 + 0.6 * 4 * 3600
    act, _, why = sch.decide(clk.now())
    assert act == "wait" and "基差" in why                      # 不够贵，等
    perp.bid, perp.ask = 100.3, 100.32                          # 永续贵 +30bp
    assert sch.decide(clk.now())[0] == "go"
    sch.run()
    assert job.status == "done" and job.state.done == 2 and sum(q for _, q in perp.takes) == 2


def test_resume_hedges_unhedged_spot():
    perp = Perp()
    sch, job, spot, clk, sent = mk("hedge_open", 3.0, perp=perp)
    job.status = "running"
    job.state.spot_qty, job.state.done = 2.0, 1.0               # 现货买了 2 张的量，只对冲了 1 张
    sch.resume()
    assert job.state.done == 2 and perp.takes == [("sell", 1)]


def test_store_roundtrip_and_cancel(tmp_path):
    st = Store(tmp_path / "live.db")
    sch, job, spot, clk, sent = mk("spot_buy", 4.0, store=st)
    job.id = None
    st.add(job)
    st.db.execute("update exec_jobs set status='cancel_requested' where id=?", (job.id,))
    st.db.commit()
    sch.run()
    assert job.status == "cancelled"
    j2 = st.get(job.id)
    assert j2.status == "cancelled" and j2.params.min_slices == 4


def test_parse_when():
    assert parse_when("4h", T0) == T0 + 4 * 3600
    assert parse_when("90m", T0) == T0 + 5400
    t = parse_when("21:00", T0)
    assert dt.datetime.fromtimestamp(t, dt.timezone.utc).hour == 21 and t > T0
    assert parse_when("09:00", T0) > T0 + 20 * 3600              # 已过 → 明天


def test_hedge_child_failure_stops_with_error():
    from hedgebot.http import SourceError

    class BadPerp(Perp):
        def take(self, side, qty, limit_px):
            raise SourceError("保证金不足")
    sch, job, spot, clk, sent = mk("hedge_open", 2.0, perp=BadPerp(), ref=Ref(T0, 100.0, 1.0, -50.0))
    sch.run()
    assert job.status == "error" and job.state.done == 0
    assert any(p == 1 for _, p in sent)                         # 高优先级通知


def test_cli_rejects_grid_only(monkeypatch):
    from types import SimpleNamespace
    from hedgebot import exec_cli
    cfg = SimpleNamespace(grid_only={"SUI", "XRP"}, live=SimpleNamespace(clip_usd=2000.0))
    args = SimpleNamespace(action="open", symbol="sui", start=None, by="4h", dry=True, limit=None, basis_min=None,
                           finish="complete", dip_k=0.5, chase_k=0.5, slices=4, contracts=1, venue="coinbase")
    monkeypatch.setattr(exec_cli, "load_exec_keys", lambda: None)
    with pytest.raises(SystemExit, match="grid_only"):
        exec_cli.submit(cfg, args)


class RisingSpot(Spot):
    """挂单永远不成交，每查一次盘口价格涨 5bp（单边上涨）。"""
    def book(self):
        self.bid *= 1.0005
        self.ask = self.bid + 0.02
        return super().book()


def test_ladder_child_gives_up_when_price_runs_away():
    spot, perp = RisingSpot(step=0), Perp()
    sch, job, _, clk, sent = mk("hedge_open", 2.0, spot=spot, perp=perp)
    clk.t = T0 + 0.6 * 4 * 3600
    sch.execute(1, 240.0)
    assert job.state.done == 0 and not perp.takes and not spot.takes          # 放弃，不追不吃
    assert job.state.cool_until > clk.now() and sch.decide(clk.now())[2].startswith("上一份追价")
    first, last = spot.placed[0][2], spot.placed[-1][2]
    assert last <= first * 1.0015 + 0.01


def test_spot_child_gives_up_when_price_runs_away():
    spot = RisingSpot(step=0)
    sch, job, _, clk, sent = mk("spot_buy", 4.0, spot=spot)
    sch.execute(1.0, 240.0)
    assert job.state.done == 0 and not spot.takes and job.state.cool_until > clk.now()
    assert spot.placed[-1][2] <= spot.placed[0][2] * 1.0015 + 0.01


def test_notifications_tagged_paper_or_live():
    sch, job, spot, clk, sent = mk("spot_buy", 1.0, hours=1.0)
    job.mode = "paper"
    sch2 = Scheduler(job, spot, None, ref_fn=lambda: Ref(clk.now(), 100.0, 1.0, None),
                     notify=lambda t, m, p=0: sent.append((t, p)), clock=clk.now, sleep=clk.sleep, say=lambda m: None)
    sch2.run()
    assert sent and all(t.startswith("【模拟】") for t, _ in sent)
    sch3, job3, *_ , sent3 = mk("spot_buy", 1.0, hours=1.0)
    sch3.run()
    assert sent3 and all(t.startswith("【实盘】") for t, _ in sent3)


# ---------------------------------------------------------------- 审查 C1：结果不明的单子
class AmbiguousSpot(Spot):
    """第一次挂单：交易所其实挂上并【整块成交】了，但请求超时（OrderUnknown，只有客户端订单号）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.first = True
        self.exchange_filled = 0.0

    def place_post_only(self, side, qty, px):
        from hedgebot.trade_api import OrderUnknown
        oid = super().place_post_only(side, qty, px)
        self.orders[oid]["cid"] = f"c-{oid}"
        if self.first:
            self.first = False
            o = self.orders[oid]
            o["filled"], o["open"] = qty, False
            self.exchange_filled += qty
            raise OrderUnknown("挂单请求超时", leg=self.name, side=side, qty=qty, cid=f"c-{oid}")
        return oid

    def status(self, oid):
        st = super().status(oid)
        return st

    def find(self, u):
        return u.oid or next((k for k, o in self.orders.items() if o.get("cid") == u.cid), None)


def test_ambiguous_spot_child_is_reconciled_once():
    sch, job, spot, clk, sent = mk("spot_buy", 4.0, spot=AmbiguousSpot())
    sch.run()
    assert job.status == "done"
    # 超时那一单按实际成交补记了，后面没有把它当「没成交」再买一遍
    total_bought = sum(o["filled"] for o in spot.orders.values())
    assert total_bought == pytest.approx(4.0)
    assert job.state.done == pytest.approx(4.0)


class LostSpot(Spot):
    """挂单请求超时，而且订单列表里永远查不到：对不上账。"""

    def place_post_only(self, side, qty, px):
        from hedgebot.trade_api import OrderUnknown
        raise OrderUnknown("挂单请求超时", leg=self.name, side=side, qty=qty, cid="lost")

    def find(self, u):
        return None


def test_unreconciled_job_errors_and_pages_without_retry():
    sch, job, spot, clk, sent = mk("spot_buy", 4.0, spot=LostSpot())
    sch.run()
    assert job.status == "error"
    assert any("对不上账" in t and p == 1 for t, p in sent)   # 高优先级传呼
    assert "lost" in job.state.log[-1]                        # 报出客户端订单号
