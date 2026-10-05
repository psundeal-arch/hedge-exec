"""hedge exec：用户定量、程序挑时机的日内执行算法（2026-10-05）。

母单（对冲加 / 减仓、或纯现货买 / 卖）在一个时间窗里分批做完：
- 进度线：目标完成量 = 已过时间占比 × 总量；落后加快、超前放慢（最少分 min_slices 份）。
- 逢低 / 逢高：买方向价格 ≤ 当日 VWAP − dip_k × ATR → 可以超前多做；≥ VWAP + chase_k × ATR → 不追（除非很急）。
- 对冲基差：加对冲要 永续溢价 ≥ basis_min（默认近 24 小时中位数），减对冲要 ≤；很急时放宽。
- 坏时段：CDE 整点资金费结算前后 2 分钟、周五 20:30–22:00 UTC（闭市前后）不下新子单。
- 价格保护：超过 limit（买）/ 低于 limit（卖）暂停并通知，不追。
- 截止：剩余按 finish = complete 吃单补齐（带保护价）/ cancel 撤掉剩余。
子单：对冲复用 Ladder（现货挂单 + 每满一张跟 CDE 吃单，带回滚）；纯现货 Coinbase 挂单跟价、超时不追（留给下一份），
只吃单的场所（Robinhood）用带保护价的吃单。

状态在 live.db 的 exec_jobs 表（每个任务一条 runs 记录，成交照常写 fills），进程重启后接着做。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import exec_quality
from .http import SourceError
from .ladder import EPS, Ladder, Params, Unreconciled, settle
from .trade_api import OrderUnknown, PostOnlyRejected, round_down

EPS = 1e-9
FRONT = 0.85                            # 进度线在窗口的这个比例处到 100%
COOL_S = 300.0                          # 子单追价放弃后的冷却
BUY_KINDS = ("hedge_open", "spot_buy")

EXEC_SCHEMA = """
create table if not exists exec_jobs(
    id integer primary key, created text, kind text, symbol text, venue text, mode text,
    total real, start real, end_ real, params text, status text, state text, run_id integer);
"""


@dataclass
class JobParams:
    limit: float | None = None          # 价格保护：买不高于 / 卖不低于
    basis_min_bp: float | None = None   # 对冲：加仓要求基差 ≥（减仓要求 ≤）；None = 近 24h 中位数
    finish: str = "complete"            # complete / cancel
    dip_k: float = 0.5                  # 逢低：偏离 VWAP 超过 dip_k 个 ATR 可超前
    chase_k: float = 0.5                # 不追：偏离超过 chase_k 个 ATR 暂停（除非很急）
    min_slices: int = 4
    child_wait_s: float = 240.0         # 每份挂单最多等多久
    clip_usd: float = 2000.0
    reprice_cap_bps: float = 15.0       # 每份子单挂单价最多比首次挂价追这么多（很急时不限）


@dataclass
class JobState:
    done: float = 0.0                   # 已完成：对冲 = 张，现货 = 币
    spot_qty: float = 0.0
    spot_cost: float = 0.0
    spot_fees: float = 0.0
    perp_cost: float = 0.0
    perp_fees: float = 0.0
    arrival: float | None = None        # 开始时现货中价
    arrival_basis_bp: float | None = None
    child_oid: str | None = None        # 纯现货子单（重启时撤掉并补记成交）
    child: dict = field(default_factory=dict)
    progress_sent: int = 0              # 已发过的进度通知（25% 一档）
    cool_until: float = 0.0             # 子单因追价上限放弃后，冷却到这个时间再下新子单
    paused: str = ""                    # 当前暂停原因（只在变化时通知）
    log: list[str] = field(default_factory=list)


@dataclass
class Job:
    kind: str                           # hedge_open / hedge_close / spot_buy / spot_sell
    symbol: str
    venue: str                          # 现货场所：coinbase / robinhood
    total: float
    start: float
    end: float
    params: JobParams = field(default_factory=JobParams)
    mode: str = "live"                  # live / paper
    id: int | None = None
    status: str = "queued"              # queued / running / done / partial / cancelled / error
    state: JobState = field(default_factory=JobState)
    run_id: int | None = None

    @property
    def buy(self) -> bool:
        return self.kind in BUY_KINDS

    @property
    def hedge(self) -> bool:
        return self.kind.startswith("hedge")


# ---------------------------------------------------------------- 存储
class Store:
    def __init__(self, path):
        from .live import SCHEMA
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA + EXEC_SCHEMA)
        for col in ("mode", "perp_venue"):
            try:
                self.db.execute(f"alter table runs add column {col} text")
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _now() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def add(self, job: Job) -> Job:
        job.id = self.db.execute(
            "insert into exec_jobs(created,kind,symbol,venue,mode,total,start,end_,params,status,state) "
            "values(?,?,?,?,?,?,?,?,?,?,?)",
            (self._now(), job.kind, job.symbol, job.venue, job.mode, job.total, job.start, job.end,
             json.dumps(asdict(job.params)), job.status, json.dumps(asdict(job.state)))).lastrowid
        self.db.commit()
        return job

    def save(self, job: Job) -> None:
        # 终端发来的 cancel_requested 不能被运行中的保存覆盖掉（循环下一轮读到它才停）
        self.db.execute("update exec_jobs set status=case when status='cancel_requested' and ?='running' "
                        "then status else ? end, state=?, run_id=? where id=?",
                        (job.status, job.status, json.dumps(asdict(job.state), ensure_ascii=False), job.run_id, job.id))
        self.db.commit()

    def get(self, jid: int) -> Job | None:
        r = self.db.execute("select id,kind,symbol,venue,mode,total,start,end_,params,status,state,run_id "
                            "from exec_jobs where id=?", (jid,)).fetchone()
        return self._row(r) if r else None

    def _row(self, r) -> Job:
        st = JobState(**json.loads(r[10]))
        return Job(kind=r[1], symbol=r[2], venue=r[3], mode=r[4], total=r[5], start=r[6], end=r[7],
                   params=JobParams(**json.loads(r[8])), status=r[9], state=st, id=r[0], run_id=r[11])

    def list(self, statuses=None) -> list[Job]:
        q = "select id,kind,symbol,venue,mode,total,start,end_,params,status,state,run_id from exec_jobs"
        rows = self.db.execute(q + " order by id").fetchall()
        jobs = [self._row(r) for r in rows]
        return [j for j in jobs if not statuses or j.status in statuses]

    def status_of(self, jid: int) -> str:
        r = self.db.execute("select status from exec_jobs where id=?", (jid,)).fetchone()
        return r[0] if r else ""

    def new_run(self, job: Job) -> int:
        rid = self.db.execute("insert into runs(started,symbol,action,spot_venue,contracts,status,mode,perp_venue)"
                              " values(?,?,?,?,?,?,?,?)",
                              (self._now(), job.symbol, f"exec-{job.kind}", job.venue, int(job.total) if job.hedge else 0,
                               "running", job.mode, "cde" if job.hedge else "none")).lastrowid
        self.db.commit()
        return rid

    def fill(self, run_id: int, **f) -> None:
        self.db.execute("insert into fills(run_id,ts,venue,side,qty,price,fee,order_id,kind) values(?,?,?,?,?,?,?,?,?)",
                        (run_id, self._now(), f["venue"], f["side"], f["qty"], f["price"], f["fee"],
                         f.get("order_id"), f.get("kind")))
        self.db.commit()

    def finish_run(self, job: Job, summary: dict) -> None:
        self.db.execute("update runs set ended=?, status=?, summary=? where id=?",
                        (self._now(), job.status, json.dumps(summary, ensure_ascii=False), job.run_id))
        self.db.commit()


# ---------------------------------------------------------------- 时段
def bad_time(now: float) -> str | None:
    t = time.gmtime(now)
    m = t.tm_min
    if m >= 58 or m < 2:
        return "整点资金费结算前后"
    if t.tm_wday == 4 and ((t.tm_hour == 20 and m >= 30) or t.tm_hour == 21):
        return "周五 CDE 闭市前后"
    return None


# ---------------------------------------------------------------- 排程器
class Scheduler:
    def __init__(self, job: Job, spot, perp=None, *, contract_size: float = 1.0, ref_fn: Callable,
                 store: Store | None = None, notify: Callable = lambda *a, **k: None,
                 clock=time.time, sleep=time.sleep, say=print, poll_s: float = 15.0,
                 escalation_bps=(30.0, 100.0, 300.0)):
        self.j, self.spot, self.perp, self.cs = job, spot, perp, contract_size
        self.ref_fn, self.store = ref_fn, store
        tag = "【模拟】" if job.mode == "paper" else "【实盘】"   # 通知必须一眼分清演练和真实成交
        self.notify = lambda t, m, p=0: notify(f"{tag}{t}", f"{tag}{m}", p)
        self.clock, self.sleep, self._say, self.poll_s = clock, sleep, say, poll_s
        self.esc = tuple(escalation_bps)
        self.ref = None
        self.ref_ts = 0.0
        self.stop = False

    # ------------------------------------------------------------ 工具
    def say(self, msg: str) -> None:
        line = time.strftime("%m-%d %H:%M:%S", time.gmtime(self.clock())) + " " + msg
        self.j.state.log = (self.j.state.log + [line])[-200:]
        self._say(line)

    def save(self) -> None:
        if self.store:
            self.store.save(self.j)

    def on_fill(self, **f) -> None:
        if self.store and self.j.run_id:
            self.store.fill(self.j.run_id, **f)

    @property
    def unit(self) -> float:
        """最小单位：对冲 = 1 张；现货 = 交易所数量精度。"""
        return 1.0 if self.j.hedge else self.spot.lot

    @property
    def slice(self) -> float:
        j = self.j
        mid = self.spot.book().mid or 0
        if j.hedge:
            per = max(1, math.ceil(j.total / j.params.min_slices))
            cap = max(1, math.floor(j.params.clip_usd / (self.cs * mid))) if mid else per
            return float(min(per, cap))
        per = j.total / j.params.min_slices
        if mid:
            per = min(per, j.params.clip_usd / mid)
        return max(round_down(per, self.spot.lot), self.spot.min_qty)

    def remaining(self) -> float:
        return max(0.0, self.j.total - self.j.state.done)

    def refresh_ref(self) -> None:
        if self.ref is None or self.clock() - self.ref_ts > 300:
            try:
                self.ref = self.ref_fn()
                self.ref_ts = self.clock()
            except Exception as e:                    # noqa: BLE001 —— 参考数据取不到：按纯进度线执行
                self.say(f"参考数据（VWAP/ATR/基差）取不到：{e}，这一轮按进度线执行")

    def basis_bp(self) -> float | None:
        if not self.perp:
            return None
        pb, sb = self.perp.book(), self.spot.book()
        if not (pb.mid and sb.mid):
            return None
        b = (pb.mid / sb.mid - 1) * 1e4
        return b if abs(b) < 1000 else None           # 超过 10%：永续按"千枚"等单位报价，不可比，不用基差闸门

    # ------------------------------------------------------------ 决策
    def decide(self, now: float) -> tuple[str, float, str]:
        """→ (go / wait / finish / done, 数量, 原因)"""
        j, p = self.j, self.j.params
        if self.remaining() < self.unit * 0.5 or self.remaining() < (0 if j.hedge else self.spot.min_qty):
            return "done", 0.0, "已完成"
        if now >= j.end:
            return "finish", self.remaining(), "到截止时间"
        span = max(1.0, j.end - j.start)
        frac = min(1.0, max(0.0, (now - j.start) / span))
        target = j.total * min(1.0, frac / FRONT)   # 进度线在窗口 85% 处走完，最后一份也有时间挂单
        sl = min(self.slice, self.remaining())
        behind = target - j.state.done
        urgent = (j.end - now) < 0.15 * span or behind >= 2 * sl
        px = self.spot.book().mid
        if px is None:
            return "wait", 0.0, "现货盘口是空的"
        if p.limit and ((j.buy and px > p.limit) or (not j.buy and px < p.limit)):
            return "wait", 0.0, f"价格保护：现价 {px:g} {'高于' if j.buy else '低于'}上限 {p.limit:g}"
        bt = bad_time(now)
        if bt:
            return "wait", 0.0, bt
        if now < j.state.cool_until and not urgent:
            return "wait", 0.0, "上一份追价放弃，冷却中"
        ref = self.ref
        if j.hedge:
            b = self.basis_bp()
            need = p.basis_min_bp if p.basis_min_bp is not None else (ref.basis_med_bp if ref else None)
            if b is not None and need is not None and not urgent:
                if j.kind == "hedge_open" and b < need:
                    return "wait", 0.0, f"基差 {b:+.1f}bp < 要求 {need:+.1f}bp（加对冲要等永续偏贵）"
                if j.kind == "hedge_close" and b > need:
                    return "wait", 0.0, f"基差 {b:+.1f}bp > 要求 {need:+.1f}bp（减对冲要等永续偏便宜）"
        dev = None
        if ref and ref.vwap and ref.atr_pct:
            dev = (px / ref.vwap - 1) * 100 / ref.atr_pct * (1 if j.buy else -1)   # 正 = 对我们不利
        if dev is not None and dev <= -p.dip_k and j.state.done < target + 2 * sl:
            return "go", sl, f"逢{'低' if j.buy else '高'}：偏离 VWAP {dev:+.2f} 个 ATR"
        if behind >= sl - EPS:
            if dev is not None and dev >= p.chase_k and not urgent:
                return "wait", 0.0, f"{'高位不追' if j.buy else '低位不砸'}：偏离 VWAP {dev:+.2f} 个 ATR"
            return "go", sl, "按进度" + ("（很急）" if urgent else "")
        return "wait", 0.0, "进度已到"

    # ------------------------------------------------------------ 子单
    def _ladder(self, n: int, wait_s: float, cap: float | None = None) -> bool:
        j = self.j
        s = j.state
        base = (s.done, s.spot_qty, s.spot_cost, s.spot_fees, s.perp_cost, s.perp_fees)

        def fold(ld) -> None:
            """子单进度【当场】并进任务状态并落盘（H2）。

            原来要等 ld.run() 返回才并 —— 中途被 SIGKILL，挂着的现货单没人认领，
            已成交的现货/空单也不在状态里：恢复后 done 偏小会多做，
            已成交未对冲的现货也不会被补对冲。"""
            r = ld.r
            (s.done, s.spot_qty, s.spot_cost, s.spot_fees, s.perp_cost, s.perp_fees) = (
                base[0] + r.contracts, base[1] + r.spot_qty, base[2] + r.spot_cost,
                base[3] + r.spot_fees, base[4] + r.perp_cost, base[5] + r.perp_fees)
            c = ld.cur
            s.child = ({"ladder_oid": c["oid"], "side": ld.spot_side, "booked": c["filled"],
                        "cost": c["cost"], "fee": c["fee"]} if c else {})
            self.save()

        ld = Ladder(self.spot, self.perp, action="open" if j.kind == "hedge_open" else "close",
                    contracts=n, contract_size=self.cs, on_fill=self.on_fill, on_change=fold,
                    clock=self.clock, sleep=self.sleep, say=lambda m: self.say("  " + m),
                    params=Params(poll_s=2.0, reprice_min_s=4.0, max_wait_s=wait_s, finish="complete",
                                  clip_usd=j.params.clip_usd, hedge_escalation_bps=self.esc, reprice_cap_bps=cap))
        r = ld.run()
        fold(ld)        # 正常结束时 ld.cur 已是 None；撤单失败/对不上账则留着挂单号供核对
        if r.status == "skipped":
            s.cool_until = self.clock() + COOL_S
        self.save()
        if r.status not in ("done", "trimmed", "skipped"):
            j.status = "error"
            msg = f"对冲子单 {r.status}：{r.error or '空单跟不上'}；已完成 {s.done:g}/{j.total:g} 张"
            self.say("✗ " + msg)
            self.notify(f"hedge exec #{j.id} {j.symbol} 出错", msg + "。先 hedge account 核对两腿", 1)
            return False
        return True

    def _spot_record(self, side: str, qty: float, px: float, fee: float, oid, kind: str) -> None:
        s = self.j.state
        s.done += qty
        s.spot_qty += qty
        s.spot_cost += qty * px
        s.spot_fees += fee
        self.on_fill(venue=self.spot.name, side=side, qty=qty, price=px, fee=fee, order_id=oid, kind=kind)
        self.save()

    def _spot_take(self, q: float, cap_bps: float) -> None:
        side = "buy" if self.j.buy else "sell"
        book = self.spot.book()
        ref = book.best_ask if side == "buy" else book.best_bid
        if ref is None:
            return
        limit = ref * (1 + cap_bps / 1e4) if side == "buy" else ref * (1 - cap_bps / 1e4)
        if self.j.params.limit:
            limit = min(limit, self.j.params.limit) if side == "buy" else max(limit, self.j.params.limit)
        q = round_down(q, self.spot.lot)
        if q < self.spot.min_qty:
            return
        try:
            st = self.spot.take(side, q, limit)
        except OrderUnknown as u:                    # 结果不明：对账，不重试
            st, _ = settle(self.spot, u, sleep=self.sleep, say=self.say)
        if st.filled > EPS:
            self._spot_record(side, st.filled, st.avg, st.fee, None, "taker")
            self.say(f"  现货吃单 {side} {st.filled:g} @ {st.avg:g}（保护价 {limit:g}）")

    def _spot_child(self, q: float, wait_s: float, cap: float | None = None) -> None:
        """纯现货一份：挂单在最优价，被抢到前面就改价；超时撤掉，没成交的留给下一份。"""
        side = "buy" if self.j.buy else "sell"
        if getattr(self.spot, "taker_only", False):
            return self._spot_take(q, 30.0)
        t0 = self.clock()
        s = self.j.state
        first = None
        while self.clock() - t0 < wait_s and not self.stop:
            left = round_down(q - s.child.get("filled", 0.0), self.spot.lot)
            if left < self.spot.min_qty:
                break
            if not s.child_oid:
                book = self.spot.book()
                px = book.best_bid if side == "buy" else book.best_ask
                if px is None:
                    break
                if first is None:
                    first = px
                elif cap is not None and (px > first * (1 + cap / 1e4) if side == "buy" else px < first * (1 - cap / 1e4)):
                    self.say(f"  价格跑出追价上限 {cap:g}bp，这一份剩余放弃")
                    s.cool_until = self.clock() + COOL_S
                    break
                try:
                    oid = self.spot.place_post_only(side, left, px)
                except PostOnlyRejected:
                    self.sleep(1.0)
                    continue
                except OrderUnknown as u:
                    # 挂单请求没拿到回复：可能挂上了。对账 → 撤掉 → 按实际成交补记
                    st, oid = settle(self.spot, u, sleep=self.sleep, say=self.say)
                    if st.filled > EPS:
                        s.child["filled"] = s.child.get("filled", 0.0) + st.filled
                        self._spot_record(side, st.filled, st.avg, st.fee, oid, "maker")
                    self.save()
                    continue
                s.child_oid = oid
                s.child = {"px": px, "filled": s.child.get("filled", 0.0), "seen": 0.0, "cost": 0.0,
                           "fee": 0.0, "at": self.clock()}
                self.save()
                self.say(f"  现货挂{'买' if side == 'buy' else '卖'} {left:g} @ {px:g}")
            self.sleep(2.0)
            self._sync_child()
            if s.child_oid:
                book = self.spot.book()
                best = book.best_bid if side == "buy" else book.best_ask
                moved = best is not None and ((side == "buy" and best > s.child["px"] + self.spot.tick / 2)
                                              or (side == "sell" and best < s.child["px"] - self.spot.tick / 2))
                if moved and self.clock() - s.child["at"] >= 4.0:
                    self._cancel_child()
        self._cancel_child()
        s.child = {}
        self.save()

    def _sync_child(self) -> None:
        s = self.j.state
        if not s.child_oid:
            return
        try:
            st = self.spot.status(s.child_oid)
            s.child["fails"] = 0
        except SourceError as e:
            # 偶发查询失败：子单还挂着，下一轮再查；连续查不到才对账（对账会撤单）
            s.child["fails"] = s.child.get("fails", 0) + 1
            if s.child["fails"] < 5:
                self.say(f"  查子单状态出错（第 {s.child['fails']} 次）：{e}")
                return
            st, _ = settle(self.spot, OrderUnknown(
                f"子单 {s.child_oid} 连续查不到状态：{e}", leg=self.spot.name,
                side="buy" if self.j.buy else "sell", qty=0.0, oid=s.child_oid),
                sleep=self.sleep, say=self.say)
        d = st.filled - s.child.get("seen", 0.0)
        if d > EPS:
            cost = st.filled * (st.avg or 0)
            dc, df = cost - s.child.get("cost", 0.0), st.fee - s.child.get("fee", 0.0)
            s.child.update(seen=st.filled, cost=cost, fee=st.fee, filled=s.child.get("filled", 0.0) + d)
            self._spot_record("buy" if self.j.buy else "sell", d, dc / d, df, s.child_oid, "maker")
            self.say(f"  现货成交 +{d:g} @ {dc / d:g}（累计 {s.done:g}/{self.j.total:g}）")
        if not st.open:
            s.child_oid = None
            s.child.update(seen=0.0, cost=0.0, fee=0.0)

    def _cancel_child(self) -> None:
        s = self.j.state
        if not s.child_oid:
            return
        try:
            self.spot.cancel(s.child_oid)
            for _ in range(20):
                st = self.spot.status(s.child_oid)
                if not st.open:
                    break
                self.sleep(0.5)
        except SourceError as e:
            # 撤单/查询出错：交给对账（会再撤一次并取终态），对不上就整个任务停下
            settle(self.spot, OrderUnknown(f"撤子单 {s.child_oid} 出错：{e}", leg=self.spot.name,
                                           side="buy" if self.j.buy else "sell", qty=0.0,
                                           oid=s.child_oid), sleep=self.sleep, say=self.say)
        self._sync_child()
        s.child_oid = None
        self.save()

    def execute(self, q: float, wait_s: float, urgent: bool = False) -> bool:
        cap = None if urgent else self.j.params.reprice_cap_bps
        if self.j.hedge:
            return self._ladder(int(round(q)), wait_s, cap)
        self._spot_child(q, wait_s, cap)
        return True

    # ------------------------------------------------------------ 收尾 / 通知 / 主循环
    def progress(self) -> None:
        j = self.j
        pct = int(j.state.done / j.total * 100) if j.total else 100
        step = min(pct // 25, 3)
        if step > j.state.progress_sent:
            j.state.progress_sent = step
            self.notify(f"hedge exec #{j.id} {j.symbol} {pct}%", self.summary_line(), -1)

    def summary_line(self) -> str:
        s, j = self.j.state, self.j
        avg = s.spot_cost / s.spot_qty if s.spot_qty > EPS else None
        out = f"{j.kind} {j.symbol} 完成 {s.done:g}/{j.total:g}"
        if avg:
            out += f"，现货均价 {avg:g}"
            if s.arrival:
                bp = (avg / s.arrival - 1) * 1e4 * (1 if j.buy else -1)
                out += f"（比开始时中价 {s.arrival:g} {'贵' if bp > 0 else '便宜'} {abs(bp):.1f}bp）"
        if j.hedge and s.done and s.spot_qty > EPS:
            pavg = s.perp_cost / (s.done * self.cs)
            out += f"，永续均价 {pavg:g}，成交基差 {(pavg / avg - 1) * 1e4:+.1f}bp"
        out += f"，手续费 ${s.spot_fees + s.perp_fees:.2f}"
        return out

    def finish(self) -> None:
        j = self.j
        rem = self.remaining()
        if rem >= self.unit * 0.5 and j.params.finish == "complete":
            self.say(f"截止：剩余 {rem:g} 吃单补齐")
            if j.hedge:
                if not self._ladder(int(round(rem)), 30.0):
                    return
            else:
                self._spot_take(rem, 100.0)              # 现货补齐最多比对手价差 1%
        rem = self.remaining()
        j.status = "done" if rem < max(self.unit * 0.5, 0 if j.hedge else self.spot.min_qty) else "partial"
        self.save()

    def run(self) -> Job:
        """对不上账（Unreconciled）→ 任务直接 error + 传呼，【不交给 worker 重试】：
        worker 的重试会走 resume → 再下单，正是 C1 要堵的那条路。"""
        try:
            return self._run()
        except Unreconciled as e:
            j = self.j
            j.status = "error"
            j.state.log.append(f"对不上账，停止：{e}")
            self.save()
            self.notify(f"hedge exec #{j.id} {j.symbol} 对不上账，已停止",
                        f"{e}。不会自动重试；先 hedge account 和交易所页面核对", 1)
            return j

    def _run(self) -> Job:
        j = self.j
        if j.status == "queued":
            j.status = "running"
            if self.store and not j.run_id:
                j.run_id = self.store.new_run(j)
            j.state.arrival = self.spot.book().mid
            j.state.arrival_basis_bp = self.basis_bp()
            self.save()
            self.notify(f"hedge exec #{j.id} 开始", f"{j.kind} {j.symbol} {j.total:g}"
                        f"，截止 {time.strftime('%m-%d %H:%M UTC', time.gmtime(j.end))}", -1)
        else:
            self.resume()
        while not self.stop and j.status == "running":
            if self.store and self.store.status_of(j.id) == "cancel_requested":
                self._cancel_child()
                j.status = "cancelled"
                self.say("收到取消请求，停止（已成交部分保留）")
                break
            now = self.clock()
            self.refresh_ref()
            act, q, why = self.decide(now)
            if why != j.state.paused and act == "wait" and why.startswith("价格保护"):
                self.notify(f"hedge exec #{j.id} {j.symbol} 暂停", why, 0)
            j.state.paused = why if act == "wait" else ""
            if act == "done":
                j.status = "done"
                break
            if act == "finish":
                self.finish()
                break
            if act == "go":
                self.say(f"下一份 {q:g}：{why}")
                if not self.execute(q, j.params.child_wait_s, urgent="很急" in why):
                    break
                self.progress()
                continue
            self.save()
            self.sleep(self.poll_s)
        self.save()
        if j.status in ("done", "partial", "cancelled", "error"):
            self.report()
        return j

    def resume(self) -> None:
        """进程重启后接着做：撤掉挂着的现货子单并补记成交；对冲则把已成交未对冲的现货补对冲。"""
        s = self.j.state
        self.say("恢复任务")
        if s.child_oid:
            try:
                self._cancel_child()
            except Exception as e:                    # noqa: BLE001
                self.say(f"撤旧子单失败：{e}")
        if s.child.get("ladder_oid"):
            self._settle_ladder_child()
        if self.j.hedge and self.perp:
            # 现货可能停在半张（子单被打断时）：补齐到整张再对冲，否则零头会留在两腿之间，
            # 下一份子单又从 0 开始买，现货就多出来了。补齐不超过任务总张数。
            tgt = max(0, min(math.ceil(s.spot_qty / self.cs - EPS), int(round(self.j.total))))
            ld = Ladder(self.spot, self.perp, action="open" if self.j.kind == "hedge_open" else "close",
                        contracts=tgt, contract_size=self.cs, on_fill=self.on_fill, clock=self.clock,
                        sleep=self.sleep, say=lambda m: self.say("  " + m),
                        params=Params(hedge_escalation_bps=self.esc, finish="complete"))
            r = ld.r
            r.spot_qty, r.spot_cost, r.spot_fees = s.spot_qty, s.spot_cost, s.spot_fees
            r.contracts, r.perp_cost, r.perp_fees = int(round(s.done)), s.perp_cost, s.perp_fees
            short = ld.target_qty - r.spot_qty
            need = ld._need()
            if need > 0 or short > self.spot.lot / 2:
                self.say(f"现货已成交未对冲：差 {need} 张空单" + (f"，现货零头补 {short:g}" if short > self.spot.lot / 2 else ""))
                ld._finish()
                (s.done, s.spot_qty, s.spot_cost, s.spot_fees, s.perp_cost, s.perp_fees) = (
                    r.contracts, r.spot_qty, r.spot_cost, r.spot_fees, r.perp_cost, r.perp_fees)
        self.save()

    def _settle_ladder_child(self) -> None:
        """对冲子单被打断时挂着的现货单（H2）：对账 → 撤掉 → 把没记上的成交补记。

        补记之后由 resume 后半段统一补对冲。查不清就抛 Unreconciled，
        run() 会把任务置 error 并传呼 —— 不在未知仓位上继续下单。"""
        s = self.j.state
        c = s.child
        oid, side = c["ladder_oid"], c.get("side", "buy" if self.j.buy else "sell")
        self.say(f"对冲子单留下的现货挂单 {oid}：对账并撤单")
        st, oid = settle(self.spot, OrderUnknown(f"进程重启前挂着的现货单 {oid}", leg=self.spot.name,
                                                 side=side, qty=0.0, oid=oid), sleep=self.sleep, say=self.say)
        d = st.filled - c.get("booked", 0.0)
        if d > EPS:
            cost = st.filled * (st.avg or 0)
            dc, df = cost - c.get("cost", 0.0), st.fee - c.get("fee", 0.0)
            s.spot_qty += d
            s.spot_cost += dc
            s.spot_fees += df
            self.on_fill(venue=self.spot.name, side=side, qty=d, price=dc / d, fee=df, order_id=oid, kind="maker")
            self.say(f"  重启前没记上的成交 +{d:g} @ {dc / d:g}，补记")
        s.child = {}
        self.save()

    def report(self) -> None:
        j = self.j
        line = f"#{j.id} {j.status}：" + self.summary_line()
        if self.store and j.run_id:
            s = j.state
            self.store.finish_run(j, {"done": s.done, "total": j.total, "spot_qty": s.spot_qty,
                                      "spot_avg": s.spot_cost / s.spot_qty if s.spot_qty > EPS else None,
                                      "arrival": s.arrival, "arrival_basis_bp": s.arrival_basis_bp,
                                      "fees": s.spot_fees + s.perp_fees,
                                      "perp_avg": s.perp_cost / (s.done * self.cs) if j.hedge and s.done else None,
                                      **exec_quality.measure(j, self.cs)})
            # 同标的的滚动值附在通知里：执行质量的退化是渐进的，单笔看不出来
            try:
                roll = exec_quality.rolling_line(exec_quality.history(self.store.db, symbol=j.symbol, mode=j.mode))
                if roll:
                    line += f"\n{j.symbol} {roll}"
            except Exception as e:                    # noqa: BLE001 —— 统计出错不能拦住结束通知
                self.say(f"执行质量统计出错：{e}")
        self.say(line)
        pr = 1 if j.status == "error" else (0 if j.status == "partial" else -1)
        self.notify(f"hedge exec #{j.id} {j.symbol} {j.status}", line, pr)


# ---------------------------------------------------------------- 时间
def parse_when(s: str, now: float) -> float:
    """'4h' / '90m' 相对现在；'21:00' 今天 UTC（已过则明天）；ISO 时间（带时区）原样。"""
    s = s.strip()
    if s in ("now", ""):
        return now
    if s[-1] in "hm" and s[:-1].replace(".", "").isdigit():
        return now + float(s[:-1]) * (3600 if s[-1] == "h" else 60)
    if len(s) <= 5 and ":" in s:
        hh, mm = (int(x) for x in s.split(":"))
        d = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=hh, minute=mm, second=0, microsecond=0)
        t = d.timestamp()
        return t if t > now else t + 86400
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
