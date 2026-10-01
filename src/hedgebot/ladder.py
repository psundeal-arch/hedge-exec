"""现货挂单 + 空单按成交跟进。

开仓（open）：现货挂买单（post-only，吃挂单费率），每累计成交满一张合约的量，
立刻吃单卖一张 CDE。平仓（close）方向全反：现货挂卖单，成交满一张就买平一张。

任意时刻没对冲的敞口 < 1 张合约（加上正在跟进的那一笔）。

现货挂单的处理：
- 价格：价差够宽就比最优价好一档（成为新的最优价，成交更快），否则排在最优价
- 被别人抢到前面（最优价越过了我们）且距上次挂单 ≥ reprice_min_s → 撤单重挂
- 被交易所以 post-only 拒掉（下单那一刻价格变了，会变成吃单）→ 下一轮按新价重挂
- 超过 max_wait_s 还没全部成交 → 撤单，按 finish 收尾：
    complete：剩余现货吃单补齐，再跟进空单（两边一定都成交到目标）
    trim：    不补了，把凑不满一张的现货零头吃单退掉，两边按已完成的张数对齐

空单跟进连续失败 max_hedge_failures 次 → 撤现货挂单、停手，报告裸敞口。
Ctrl-C → 撤现货挂单，把已成交的整张部分对冲完，再报告剩余零头。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from .book import walk
from .http import SourceError
from .trade_api import OrderState, round_down, round_up

EPS = 1e-9


class HedgeStuck(RuntimeError):
    """空单跟进连续失败 —— 现货腿已经成交的部分没有对冲。"""


@dataclass
class Params:
    poll_s: float = 2.0
    reprice_min_s: float = 4.0
    max_wait_s: float = 600.0
    taker_slippage_bps: float = 30.0
    finish: str = "complete"          # complete | trim
    max_hedge_failures: int = 3


@dataclass
class Result:
    status: str = "running"           # done / trimmed / aborted / interrupted
    spot_qty: float = 0.0             # 现货成交量（开仓=买入，平仓=卖出）
    spot_cost: float = 0.0            # 成交金额
    spot_fees: float = 0.0
    contracts: int = 0                # 永续成交张数
    perp_cost: float = 0.0            # 张数 × 乘数 × 均价
    perp_fees: float = 0.0
    spot_orders: int = 0              # 挂过几次单（含重挂）
    log: list[str] = field(default_factory=list)

    @property
    def spot_avg(self):
        return self.spot_cost / self.spot_qty if self.spot_qty else None

    def residual(self, cs: float) -> float:
        """现货比空单多出来的币数（未对冲的部分）。"""
        return self.spot_qty - self.contracts * cs


class Ladder:
    def __init__(self, spot, perp, *, action: str, contracts: int, contract_size: float,
                 params: Params, on_fill: Callable | None = None,
                 clock=time.monotonic, sleep=time.sleep, say=print):
        assert action in ("open", "close")
        self.spot, self.perp = spot, perp
        self.action = action
        self.spot_side = "buy" if action == "open" else "sell"
        self.perp_side = "sell" if action == "open" else "buy"
        self.target_contracts = contracts
        self.cs = contract_size
        self.target_qty = round_down(contracts * contract_size, spot.lot)
        self.p = params
        self.on_fill = on_fill or (lambda **kw: None)
        self.clock, self.sleep, self._say = clock, sleep, say
        self.r = Result()
        self.cur: dict | None = None          # 当前挂着的现货单
        self.hedge_failures = 0

    # ------------------------------------------------------------ 工具
    def say(self, msg: str) -> None:
        self.r.log.append(msg)
        self._say(msg)

    def _book_px(self, book, side: str) -> float | None:
        """side 方向吃单时的对手价。"""
        return book.best_ask if side == "buy" else book.best_bid

    def _limit(self, ref: float, side: str) -> float:
        k = self.p.taker_slippage_bps / 1e4
        return ref * (1 + k) if side == "buy" else ref * (1 - k)

    # ------------------------------------------------------------ 现货挂单
    def _place(self) -> None:
        rem = round_down(self.target_qty - self.r.spot_qty, self.spot.lot)
        if rem < self.spot.min_qty:
            self.cur = None
            return
        book = self.spot.book()
        bid, ask = book.best_bid, book.best_ask
        if bid is None or ask is None:
            raise SourceError(f"{self.spot.name} 订单簿是空的")
        t = self.spot.tick
        if self.spot_side == "buy":
            px = bid + t if ask - bid > 1.5 * t else bid
            px = round_down(px, t)
        else:
            px = ask - t if ask - bid > 1.5 * t else ask
            px = round_up(px, t)
        oid = self.spot.place_post_only(self.spot_side, rem, px)
        self.r.spot_orders += 1
        self.cur = {"oid": oid, "px": px, "filled": 0.0, "cost": 0.0, "fee": 0.0,
                    "at": self.clock()}
        self.say(f"现货挂{'买' if self.spot_side == 'buy' else '卖'} {rem:g} @ {px:g}"
                 f"（买一 {bid:g} / 卖一 {ask:g}）")

    def _sync(self) -> OrderState:
        """读当前挂单的累计成交，把【增量】记进结果。"""
        c = self.cur
        st = self.spot.status(c["oid"])
        d = st.filled - c["filled"]
        if d > EPS:
            cost = st.filled * (st.avg or 0)
            dc, df = cost - c["cost"], st.fee - c["fee"]
            self.r.spot_qty += d
            self.r.spot_cost += dc
            self.r.spot_fees += df
            c.update(filled=st.filled, cost=cost, fee=st.fee)
            self.on_fill(venue=self.spot.name, side=self.spot_side, qty=d, price=dc / d,
                         fee=df, order_id=c["oid"], kind="maker")
            self.say(f"  现货成交 +{d:g} @ {dc / d:g}（累计 {self.r.spot_qty:g}/{self.target_qty:g}）")
        return st

    def _cancel(self) -> None:
        """撤单并等到终态，把撤单前最后成交的部分也记上。"""
        if not self.cur:
            return
        self.spot.cancel(self.cur["oid"])
        for _ in range(20):
            st = self._sync()
            if not st.open:
                break
            self.sleep(0.5)
        else:
            raise SourceError(f"现货单 {self.cur['oid']} 撤单后 10 秒仍未到终态")
        self.cur = None

    def _stale(self) -> bool:
        if self.clock() - self.cur["at"] < self.p.reprice_min_s:
            return False
        book = self.spot.book()
        if self.spot_side == "buy":
            return book.best_bid is not None and book.best_bid > self.cur["px"] + self.spot.tick / 2
        return book.best_ask is not None and book.best_ask < self.cur["px"] - self.spot.tick / 2

    # ------------------------------------------------------------ 空单跟进
    def _hedge(self) -> None:
        want = math.floor(self.r.spot_qty / self.cs + EPS) - self.r.contracts
        if want <= 0:
            return
        book = self.perp.book()
        levels = book.bids if self.perp_side == "sell" else book.asks
        ref = self._book_px(book, self.perp_side)
        avg, depth = walk(levels, want)
        if ref is None or avg is None or depth + EPS < want:
            return self._hedge_failed(f"CDE 深度不够（要 {want} 张，簿子上 {depth:g}）")
        if abs(avg / ref - 1) * 1e4 > self.p.taker_slippage_bps:
            return self._hedge_failed(f"CDE 吃 {want} 张的滑点超过 {self.p.taker_slippage_bps:g}bp")
        try:
            st = self.perp.take(self.perp_side, want, self._limit(ref, self.perp_side))
        except SourceError as e:
            return self._hedge_failed(str(e))
        got = int(round(st.filled))
        if got > 0:
            self.r.contracts += got
            self.r.perp_cost += got * self.cs * (st.avg or 0)
            self.r.perp_fees += st.fee
            self.on_fill(venue=self.perp.name, side=self.perp_side, qty=got, price=st.avg,
                         fee=st.fee, order_id=None, kind="taker")
            self.say(f"  空单{'卖出' if self.perp_side == 'sell' else '买平'} {got} 张 @ {st.avg:g}"
                     f"（累计 {self.r.contracts}/{self.target_contracts}）")
        if got < want:
            self._hedge_failed(f"CDE 只成交 {got}/{want} 张（{st.status} {st.reason}）")
        else:
            self.hedge_failures = 0

    def _hedge_failed(self, why: str) -> None:
        self.hedge_failures += 1
        self.say(f"  ✗ 空单跟进失败（第 {self.hedge_failures} 次）：{why}")
        if self.hedge_failures >= self.p.max_hedge_failures:
            raise HedgeStuck(why)

    def _hedge_all(self) -> None:
        """把能对冲的整张都对冲完（收尾用）。"""
        while math.floor(self.r.spot_qty / self.cs + EPS) > self.r.contracts:
            self._hedge()
            if math.floor(self.r.spot_qty / self.cs + EPS) > self.r.contracts:
                self.sleep(self.p.poll_s)

    # ------------------------------------------------------------ 现货吃单（收尾）
    def _spot_take(self, side: str, qty: float, why: str) -> None:
        for _ in range(3):
            qty = round_down(qty, self.spot.lot)
            if qty < self.spot.min_qty:
                self.say(f"  {why}：{qty:g} 低于最小下单量 {self.spot.min_qty:g}，留作零头")
                return
            book = self.spot.book()
            ref = self._book_px(book, side)
            st = self.spot.take(side, qty, self._limit(ref, side))
            if st.filled > EPS:
                sign = 1 if side == self.spot_side else -1
                self.r.spot_qty += sign * st.filled
                self.r.spot_cost += sign * st.filled * st.avg
                self.r.spot_fees += st.fee
                self.on_fill(venue=self.spot.name, side=side, qty=st.filled, price=st.avg,
                             fee=st.fee, order_id=None, kind="taker")
                self.say(f"  {why}：现货吃单 {side} {st.filled:g} @ {st.avg:g}")
            qty -= st.filled
            if qty < self.spot.lot / 2:
                return
        self.say(f"  {why}：三次吃单后还差 {qty:g}")

    def _finish(self) -> None:
        rem = self.target_qty - self.r.spot_qty
        if rem > self.spot.lot / 2 and self.p.finish == "complete":
            self._spot_take(self.spot_side, rem, "超时补齐")
            self._hedge_all()
            self.r.status = "done"
        elif rem > self.spot.lot / 2:
            # trim：凑不满一张的现货零头退掉
            self._hedge_all()
            extra = self.r.residual(self.cs)
            if extra > self.spot.lot / 2:
                back = "sell" if self.spot_side == "buy" else "buy"
                self._spot_take(back, extra, "退掉零头")
            self.r.status = "trimmed"
        else:
            self._hedge_all()
            self.r.status = "done"

    # ------------------------------------------------------------ 主循环
    def run(self) -> Result:
        start = self.clock()
        try:
            while True:
                self._hedge()
                if self.target_qty - self.r.spot_qty < self.spot.lot / 2:
                    break
                timed_out = self.clock() - start > self.p.max_wait_s
                if self.cur is None:
                    if timed_out:
                        self.say("挂单超时，进入收尾")
                        break
                    self._place()
                    if self.cur is None:          # 剩余量低于最小下单量
                        break
                else:
                    st = self._sync()
                    if not st.open:
                        if st.filled < EPS and st.reason:
                            self.say(f"  挂单被拒/撤销：{st.status} {st.reason}，重挂")
                        self.cur = None
                        continue
                    if timed_out:
                        self.say("挂单超时，撤单进入收尾")
                        self._cancel()
                        break
                    if self._stale():
                        self.say("  被抢到前面，撤单重挂")
                        self._cancel()
                        continue
                self.sleep(self.p.poll_s)
            self._finish()
        except HedgeStuck as e:
            self.say(f"✗ 空单跟进连续失败，停手：{e}")
            self._safe_cancel()
            self.r.status = "aborted"
        except KeyboardInterrupt:
            self.say("收到 Ctrl-C：撤现货挂单，对冲已成交的整张部分")
            self._safe_cancel()
            try:
                self._hedge_all()
            except HedgeStuck as e:
                self.say(f"✗ 对冲失败：{e}")
            self.r.status = "interrupted"
        return self.r

    def _safe_cancel(self) -> None:
        try:
            self._cancel()
        except Exception as e:      # noqa: BLE001 —— 撤单失败也要把结果报出来
            self.say(f"✗ 撤现货挂单失败：{e} —— 去交易所页面手动撤单 {self.cur and self.cur['oid']}")
