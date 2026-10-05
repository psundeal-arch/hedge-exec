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


class Killed(BaseException):
    """模拟 SIGKILL：不是 Exception，Ladder 的安全出口拦不住，进程就这么没了。"""


class KillableSpot(Spot):
    """挂单每查一次成交 0.5；成交到 kill_at 的那一次查询直接「进程死亡」。"""

    def __init__(self, kill_at, **kw):
        super().__init__(step=0.5, **kw)
        self.kill_at = kill_at

    def status(self, oid):
        st = super().status(oid)
        if self.kill_at is not None and st.filled >= self.kill_at - 1e-12:
            raise Killed()
        return st


def test_kill_mid_hedge_child_resumes_without_orphan_or_overtrade(tmp_path):
    """H2：对冲子单中途被杀 —— 挂着的现货单要被认领撤掉，没记上的成交要补记，
    半张的零头补齐再对冲，最终两腿各正好 total，不多做。

    原来进度要等 ld.run() 返回才并进任务状态：被杀时状态里一张都没有，
    挂单号也没存，恢复后从 0 开始再买 2 张，交易所上还挂着一张孤儿单。"""
    st = Store(tmp_path / "live.db")
    spot, perp = KillableSpot(kill_at=1.5), Perp()
    sch, job, _, clk, sent = mk("hedge_open", 2.0, spot=spot, perp=perp, store=st, min_slices=1)
    st.add(job)
    clk.t = T0 + 0.9 * 4 * 3600                                 # 进度落后，马上开做
    with pytest.raises(Killed):
        sch.run()
    # 交易所实况：挂单成交了 1.5，空单对冲了 1 张，挂单还挂着
    assert spot.orders["o0"]["filled"] == 1.5 and spot.orders["o0"]["open"]
    assert sum(q for _, q in perp.takes) == 1

    # 「重启」：从库里读回任务，新的排程器接着跑
    spot.kill_at, spot.step = None, 0
    job2 = st.get(job.id)
    assert job2.state.done == 1 and job2.state.spot_qty == 1.0, "进度要当场落盘"
    assert job2.state.child.get("ladder_oid") == "o0", "挂单号要当场落盘"
    sch2 = Scheduler(job2, spot, perp, contract_size=1.0, ref_fn=lambda: Ref(clk.now(), 100.0, 1.0, None),
                     store=st, notify=lambda t, m, p=0: sent.append((t, p)), clock=clk.now, sleep=clk.sleep,
                     say=lambda m: None, poll_s=60.0)
    sch2.run()
    assert not spot.orders["o0"]["open"], "孤儿挂单要撤掉"
    assert job2.status == "done" and job2.state.done == 2
    bought = sum(o["filled"] for o in spot.orders.values()) + sum(q for _, q, _ in spot.takes)
    assert bought == pytest.approx(2.0), f"现货不能多买：{bought}"
    assert sum(q for _, q in perp.takes) == 2
    assert job2.state.spot_qty == pytest.approx(2.0) and not job2.state.child


class _StopLoop(BaseException):
    pass


def _worker_env(tmp_path, monkeypatch, build):
    import time as _t
    import types
    from hedgebot import exec_cli, live, notify
    st = Store(tmp_path / "live.db")
    sent = []
    monkeypatch.setattr(exec_cli, "_store", lambda cfg: st)
    monkeypatch.setattr(exec_cli, "load_exec_keys", lambda: None)
    monkeypatch.setattr(live, "build", build)
    monkeypatch.setattr(notify, "send", lambda t, m, p=0: sent.append((t, p)))
    monkeypatch.setattr(exec_cli.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop()))
    now = _t.time()
    for sym in ("ZEC", "LTC"):
        st.add(Job(kind="spot_buy", symbol=sym, venue="coinbase", total=1.0, start=now - 10,
                   end=now + 3600, mode="paper"))
    cfg = types.SimpleNamespace(live=types.SimpleNamespace(hedge_escalation_bps=(30.0, 100.0, 300.0)))
    return exec_cli, st, sent, cfg


def test_worker_config_error_fails_job_and_moves_on(tmp_path, monkeypatch):
    """H4：build() 抛 SystemExit（服务器没有 RH 交易 key）—— 原来 worker 整个退出、
    systemd 重启后又选中同一任务，无限循环、不传呼、后面的任务全被堵死。"""
    built = []

    def build(cfg, sym, venue, **kw):
        built.append(sym)
        if sym == "ZEC":
            raise SystemExit("缺 Robinhood 交易 key")
        raise _StopLoop()
    exec_cli, st, sent, cfg = _worker_env(tmp_path, monkeypatch, build)
    with pytest.raises(_StopLoop):
        exec_cli.worker(cfg, None)
    jobs = {j.symbol: j for j in st.list()}
    assert jobs["ZEC"].status == "error"
    assert any("启动失败" in t and p == 1 for t, p in sent), sent
    assert built == ["ZEC", "LTC"], "出错的任务置 error 后，队列要往下走"


def test_worker_sigterm_during_build_does_not_start_scheduler(tmp_path, monkeypatch):
    """H5：SIGTERM 落在 build() 期间 —— 原来新建的排程器 stop=False，照样开跑。"""
    import os
    import signal as sig
    from hedgebot import exec_cli as ec
    ran = []
    monkeypatch.setattr(ec.Scheduler, "run", lambda self: ran.append(self.j.id))

    def build(cfg, sym, venue, **kw):
        os.kill(os.getpid(), sig.SIGTERM)                       # restart 恰好落在这里
        return {"product_id": "ZEC-PERP", "contract_size": 1.0}, Spot(), None
    old = sig.getsignal(sig.SIGTERM), sig.getsignal(sig.SIGINT)
    try:
        exec_cli, st, sent, cfg = _worker_env(tmp_path, monkeypatch, build)
        exec_cli.worker(cfg, None)                              # 正常返回 = 停在安全点
    finally:
        sig.signal(sig.SIGTERM, old[0])
        sig.signal(sig.SIGINT, old[1])
    assert ran == [], "收到 SIGTERM 后不该再开跑新的排程器"
    assert all(j.status == "queued" for j in st.list()), "任务原样留着，重启后再做"


# --- 执行质量 ----------------------------------------------------------------------

def _done_job(kind, *, spot_qty, spot_cost, arrival, done=None, perp_cost=0.0, arrival_basis_bp=None,
              fees=(0.0, 0.0)):
    job = Job(kind=kind, symbol="ZEC", venue="coinbase", total=done or spot_qty, start=T0, end=T0 + 3600, id=1)
    s = job.state
    s.spot_qty, s.spot_cost, s.arrival, s.done = spot_qty, spot_cost, arrival, done if done is not None else spot_qty
    s.perp_cost, s.arrival_basis_bp = perp_cost, arrival_basis_bp
    s.spot_fees, s.perp_fees = fees
    return job


def test_quality_signs_are_costs():
    """正 = 比基准差。买贵了、卖便宜了、开仓基差变小、平仓基差变大，都是正。"""
    from hedgebot.exec_quality import measure
    q = measure(_done_job("spot_buy", spot_qty=1, spot_cost=100.1, arrival=100.0, fees=(0.15, 0)))
    assert q["arrival_bp"] == pytest.approx(10) and q["cost_bp"] == pytest.approx(10 + 0.15 / 100.1 * 1e4)
    q = measure(_done_job("spot_sell", spot_qty=1, spot_cost=99.9, arrival=100.0))
    assert q["arrival_bp"] == pytest.approx(10)
    # 开仓：开始时基差 +20bp，成交基差 +10bp（卖永续卖得不够贵）→ 成本 10bp
    q = measure(_done_job("hedge_open", spot_qty=1, spot_cost=100.0, arrival=100.0, done=1,
                          perp_cost=100.1, arrival_basis_bp=20.0))
    assert q["exec_basis_bp"] == pytest.approx(10) and q["basis_bp"] == pytest.approx(10)
    assert q["cost_bp"] == pytest.approx(10), "对冲看基差，不看到达价"
    # 平仓：开始时 +5bp，成交时 +15bp（买回永续买贵了）→ 成本 10bp
    q = measure(_done_job("hedge_close", spot_qty=1, spot_cost=100.0, arrival=100.0, done=1,
                          perp_cost=100.15, arrival_basis_bp=5.0))
    assert q["basis_bp"] == pytest.approx(10)
    assert measure(_done_job("spot_buy", spot_qty=0, spot_cost=0, arrival=100.0)) == {}


def test_quality_history_rolling_and_report_line(tmp_path):
    """结束通知要带同标的的滚动值；老任务（summary 没有新字段）按均价补算到达价滑点。"""
    import json
    from hedgebot import exec_quality as eq
    st = Store(tmp_path / "live.db")
    # 一条老格式的 run：只有 spot_avg / arrival
    st.db.execute("insert into runs(started,ended,symbol,action,status,mode,summary) values(?,?,?,?,?,?,?)",
                  ("2026-10-05T10:00:00+00:00", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "ZEC", "exec-spot_buy", "done", "paper",
                   json.dumps({"spot_qty": 1, "spot_avg": 100.2, "arrival": 100.0})))
    st.db.commit()
    rows = eq.history(st.db, symbol="ZEC", mode="paper")
    assert rows[0]["arrival_bp"] == pytest.approx(20)
    for bp in (0, 0, 0, 0, 0, 30):                               # 最近几笔变差
        q = {"spot_qty": 1, "notional": 100.0, "arrival_bp": float(bp), "fee_bp": 0.0, "cost_bp": float(bp)}
        st.db.execute("insert into runs(started,ended,symbol,action,status,mode,summary) values(?,?,?,?,?,?,?)",
                      ("x", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "ZEC",
                       "exec-spot_buy", "done", "paper", json.dumps(q)))
    st.db.commit()
    line = eq.rolling_line(eq.history(st.db, symbol="ZEC", mode="paper"))
    assert "最近 5 笔 +6.0bp" in line and "变差" in line, line

    sch, job, spot, clk, sent = mk("spot_buy", 1.0, store=st)
    job.mode = "paper"
    st.add(job)
    sch.run()
    assert job.status == "done"
    summary = json.loads(st.db.execute("select summary from runs where id=?", (job.run_id,)).fetchone()[0])
    assert "arrival_bp" in summary and "cost_bp" in summary
    assert "到达价滑点" not in summary                            # 是数，不是文字


# --- H6：--limit 是硬约束，子单里也是 ---------------------------------------------

class JumpingSpot(Spot):
    """挂单成交一半后价格跳到保护价之外，再也不回来。"""

    def __init__(self, **kw):
        super().__init__(step=0.5, **kw)
        self.n = 0

    def status(self, oid):
        st = super().status(oid)
        if st.filled >= 0.5 and self.bid < 105:
            self.bid, self.ask = 110.0, 110.02
            self.step = 0                                       # 价格跑了，旧价位上的挂单再没人成交
        return st


def test_hedge_child_respects_limit_hedges_whole_contracts_and_trims():
    """H6：对冲子单原来不知道 --limit，超时补齐会按逐档放宽一路追到 +20%。
    现在：现货超出保护价就不再买；已成交凑不满一张的零头退掉；空单只对冲整张。"""
    spot, perp = JumpingSpot(), Perp()
    sch, job, _, clk, sent = mk("hedge_open", 2.0, spot=spot, perp=perp, limit=101.0, min_slices=1)
    clk.t = T0 + 0.9 * 4 * 3600
    sch.execute(2, 60.0)
    buys = [(q, lim) for side, q, lim in spot.takes if side == "buy"]
    assert all(lim is not None and lim <= 101.0 for _, lim in buys), f"吃单越过了保护价：{buys}"
    assert not [px for side, _, px in spot.placed if side == "buy" and px > 101.0], "挂单越过了保护价"
    s = job.state
    assert s.spot_qty == pytest.approx(s.done), "现货必须正好等于已对冲的整张（零头退掉）"
    assert sum(q for side, q in perp.takes if side == "sell") == s.done


def test_spot_child_respects_limit_when_repricing():
    """纯现货子单里改价也不能越过 --limit。"""
    spot = JumpingSpot()
    sch, job, _, clk, sent = mk("spot_buy", 2.0, spot=spot, limit=101.0, min_slices=1)
    sch._spot_child(2.0, 120.0)
    assert not [px for side, _, px in spot.placed if px > 101.0]


def test_dry_close_preflight_does_not_read_paper_db(monkeypatch):
    """H1：exec --dry 不写 paper.db，所以演练平仓的预检不能去 paper.db 里找仓位。"""
    import types
    from hedgebot import live
    from hedgebot.config import load_config
    cfg = load_config()
    p = {"contract_size": 1.0, "product_id": "ZEC-PERP", "funding_rate": 1e-5, "session_open": True,
         "trading_disabled": False, "unit": 1.0, "margin_short_overnight": 0.99}
    monkeypatch.setattr(live, "build", lambda *a, **k: (p, Spot(bid=1300, ask=1300.2), Perp(1301, 1301.2),
                                                       None, lambda: {}, 0.0015, []))
    monkeypatch.setattr(live, "session_risk", lambda *a: None)
    called = []
    import hedgebot.paper as paper_mod
    monkeypatch.setattr(paper_mod, "Paper", lambda cfg: called.append(1) or types.SimpleNamespace(position=lambda s: None))
    ns = types.SimpleNamespace(symbol="ZEC", spot="coinbase", perp="cde", contracts=1, usd=None, action="close",
                               max_wait=300, paper=True, finish="complete", paper_position_check=False)
    plan = live.preflight(cfg, ns, need_trade=False)
    assert not called and not [b for b in plan["blockers"] if "模拟盘" in b], plan["blockers"]
    ns.paper_position_check = True                              # hedge live --paper 照旧检查
    assert [b for b in live.preflight(cfg, ns, need_trade=False)["blockers"] if "模拟盘" in b]
