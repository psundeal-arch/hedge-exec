"""现货挂单 + 空单按成交跟进。

开仓（open）：现货挂买单（post-only，吃挂单费率），每累计成交满一张合约的量，
立刻吃单卖一张 CDE。平仓（close）方向全反：现货挂卖单，成交满一张就买平一张。

现货挂单的处理：
- 【分块挂】每次最多挂 clip_usd 美元（至少一张合约的量）。整块被一口吃掉时，
  没对冲的也只有这一块 —— 暴跌时买单往往正是这样被砸穿的（逆向选择）
- 价格：价差够宽就比最优价好一档（成为新的最优价，成交更快），否则排在最优价
- 被别人抢到前面（最优价越过了我们）且距上次挂单 ≥ reprice_min_s → 撤单重挂
- 被交易所以 post-only 拒掉（下单那一刻价格变了，会变成吃单）→ 下一轮按新价重挂
- 超过 max_wait_s 还没全部成交 → 撤单，按 finish 收尾：
    complete：剩余现货吃单补齐，再跟进空单（两边一定都成交到目标）
    trim：    不补了，把凑不满一张的现货零头吃单退掉，两边按已完成的张数对齐

空单跟进【只升级、不放弃】：
- 价格因素（簿子薄、滑点超限、只成交了一部分）不算失败，当场把限价放宽一档重试：
  hedge_escalation_bps（默认 30 → 100 → 300 基点）用完后直接市价。
  越是暴跌越必须对冲 —— 在这里放弃，就是在最坏的时刻留下裸腿。
- 只有硬错误（接口报错、保证金不足、停牌、簿子空了）才计失败；连续
  max_hedge_failures 次 → 撤现货挂单，把【没对冲的现货反向吃单退掉】回到零敞口
  （status = unwound）。退也退不掉才是 aborted —— 那时账上有裸腿，必须人工处理。

任何未预料的异常（接口超时、限频、交易所 5xx）走同一个安全出口：撤现货挂单 →
对冲已成交的整张 → 报告剩余敞口（status = error）。Ctrl-C 也一样（interrupted）。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from .book import walk
from .http import SourceError
from .trade_api import OrderState, PostOnlyRejected, round_down, round_up

EPS = 1e-9
MARKET_BPS = 2000.0           # 「市价档」：永续发真正的市价单；现货仍带 20% 保护价


class HedgeStuck(RuntimeError):
    """空单跟进连续硬失败 —— 现货腿已经成交的部分没有对冲。"""


@dataclass
class Params:
    poll_s: float = 2.0
    reprice_min_s: float = 4.0
    max_wait_s: float = 600.0
    finish: str = "complete"          # complete | trim
    max_hedge_failures: int = 3
    max_post_rejects: int = 30        # 连续被「只挂单」拒这么多次才停手（行情在单边狂奔）
    clip_usd: float = 2000.0          # 现货每块最多挂多少美元（至少一张合约的量）
    hedge_escalation_bps: tuple[float, ...] = (30.0, 100.0, 300.0)
    # 追价上限（hedge exec 用；None = 不限，hedge live 的原行为）：挂单价最多比第一次挂的价差这么多 bp。
    # 到上限时一点没成交 → 放弃这一份（status=skipped）；成交了一部分 → 不再改价，挂着等到超时
    reprice_cap_bps: float | None = None


@dataclass
class Result:
    status: str = "running"           # done / trimmed / unwound / aborted / error / interrupted
    spot_qty: float = 0.0             # 现货净成交量（开仓=净买入，平仓=净卖出）
    spot_cost: float = 0.0            # 净成交金额
    spot_fees: float = 0.0
    contracts: int = 0                # 永续成交张数
    perp_cost: float = 0.0            # 张数 × 乘数 × 均价
    perp_fees: float = 0.0
    spot_orders: int = 0              # 挂过几次单（含重挂）
    max_unhedged: float = 0.0         # 运行中出现过的最大未对冲现货（币）
    error: str = ""
    log: list[str] = field(default_factory=list)

    @property
    def spot_avg(self):
        return self.spot_cost / self.spot_qty if self.spot_qty > EPS else None

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
        # 场所自带的节奏下限（Kraken 限频与撤单罚分）优先于配置
        from dataclasses import replace
        params = replace(params,
                         poll_s=max(params.poll_s, getattr(spot, "min_poll_s", 0.0)),
                         reprice_min_s=max(params.reprice_min_s, getattr(spot, "min_reprice_s", 0.0)))
        self.p = params
        self.on_fill = on_fill or (lambda **kw: None)
        self.clock, self.sleep, self._say = clock, sleep, say
        self.r = Result()
        self.cur: dict | None = None          # 当前挂着的现货单
        self.hard_failures = 0
        self.post_rejects = 0
        self.first_px: float | None = None

    # ------------------------------------------------------------ 工具
    def say(self, msg: str) -> None:
        self.r.log.append(msg)
        self._say(msg)

    @staticmethod
    def _ref(book, side: str) -> float | None:
        """side 方向吃单时的对手价。"""
        return book.best_ask if side == "buy" else book.best_bid

    @staticmethod
    def _limit(ref: float, side: str, bps: float) -> float:
        k = bps / 1e4
        return ref * (1 + k) if side == "buy" else ref * (1 - k)

    def _track(self) -> None:
        self.r.max_unhedged = max(self.r.max_unhedged, abs(self.r.residual(self.cs)))

    # ------------------------------------------------------------ 现货挂单
    def _clip_qty(self, px: float) -> float:
        """一块挂多少：clip_usd 折成币，取整到整张合约，至少一张。"""
        n = max(1, math.floor(self.p.clip_usd / (self.cs * px)))
        return n * self.cs

    def _place(self) -> str:
        """挂一块。返回 placed / rejected（只挂单被拒，稍后重挂）/ small（剩余量太小）。"""
        book = self.spot.book()
        bid, ask = book.best_bid, book.best_ask
        if bid is None or ask is None:
            raise SourceError(f"{self.spot.name} 订单簿是空的")
        rem = self.target_qty - self.r.spot_qty
        rem = round_down(min(rem, self._clip_qty((bid + ask) / 2)), self.spot.lot)
        if rem < self.spot.min_qty:
            self.cur = None
            return "small"
        px = self._quote(bid, ask)
        if self._beyond_cap(px):
            self.cur = None
            return "capped"
        if self.first_px is None:
            self.first_px = px
        try:
            oid = self.spot.place_post_only(self.spot_side, rem, px)
        except PostOnlyRejected:
            self.post_rejects += 1
            self.say(f"  挂{'买' if self.spot_side == 'buy' else '卖'} {rem:g} @ {px:g} 会变成吃单被拒"
                     f"（连续第 {self.post_rejects} 次），按新价重挂")
            if self.post_rejects >= self.p.max_post_rejects:
                raise SourceError(f"连续 {self.post_rejects} 次只挂单被拒，行情单边太快，停手")
            self.cur = None
            return "rejected"
        self.post_rejects = 0
        self.r.spot_orders += 1
        self.cur = {"oid": oid, "px": px, "filled": 0.0, "cost": 0.0, "fee": 0.0,
                    "at": self.clock()}
        self.say(f"现货挂{'买' if self.spot_side == 'buy' else '卖'} {rem:g} @ {px:g}"
                 f"（买一 {bid:g} / 卖一 {ask:g}）")
        return "placed"

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
            self._track()
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

    def _quote(self, bid: float, ask: float) -> float:
        t = self.spot.tick
        if self.spot_side == "buy":
            return round_down(bid + t if ask - bid > 1.5 * t else bid, t)
        return round_up(ask - t if ask - bid > 1.5 * t else ask, t)

    def _beyond_cap(self, px: float) -> bool:
        cap = self.p.reprice_cap_bps
        if cap is None or self.first_px is None:
            return False
        lim = self.first_px * (1 + cap / 1e4) if self.spot_side == "buy" else self.first_px * (1 - cap / 1e4)
        return px > lim if self.spot_side == "buy" else px < lim

    def _stale(self) -> bool:
        if self.clock() - self.cur["at"] < self.p.reprice_min_s:
            return False
        book = self.spot.book()
        if self.spot_side == "buy":
            return book.best_bid is not None and book.best_bid > self.cur["px"] + self.spot.tick / 2
        return book.best_ask is not None and book.best_ask < self.cur["px"] - self.spot.tick / 2

    # ------------------------------------------------------------ 空单跟进
    def _need(self) -> int:
        return math.floor(self.r.spot_qty / self.cs + EPS) - self.r.contracts

    def _hedge(self) -> None:
        """把能对冲的整张都对冲掉。价格问题当场升级限价，硬错误才计失败。"""
        steps = list(self.p.hedge_escalation_bps) + [MARKET_BPS]
        for i, bps in enumerate(steps):
            want = self._need()
            if want <= 0:
                self.hard_failures = 0
                return
            book = self.perp.book()
            ref = self._ref(book, self.perp_side)
            if ref is None:
                return self._hedge_failed("CDE 订单簿是空的（熔断/停牌？）")
            if bps < MARKET_BPS:
                levels = book.bids if self.perp_side == "sell" else book.asks
                avg, depth = walk(levels, want)
                if avg is None or depth + EPS < want or abs(avg / ref - 1) * 1e4 > bps:
                    self.say(f"  CDE 吃 {want} 张超出 {bps:g}bp（深度 {depth:g}），放宽一档")
                    continue
            try:
                limit = None if bps >= MARKET_BPS else self._limit(ref, self.perp_side, bps)
                st = self.perp.take(self.perp_side, want, limit)
            except SourceError as e:
                return self._hedge_failed(str(e))
            got = int(round(st.filled))
            if got > 0:
                self.r.contracts += got
                self.r.perp_cost += got * self.cs * (st.avg or 0)
                self.r.perp_fees += st.fee
                self._track()
                self.on_fill(venue=self.perp.name, side=self.perp_side, qty=got, price=st.avg,
                             fee=st.fee, order_id=None, kind="taker")
                self.say(f"  空单{'卖出' if self.perp_side == 'sell' else '买平'} {got} 张 @ {st.avg:g}"
                         f"（累计 {self.r.contracts}/{self.target_contracts}"
                         f"{f'，{bps:g}bp 档' if i else ''}）")
            if got < want:
                self.say(f"  CDE 只成交 {got}/{want} 张（{st.status} {st.reason}），放宽一档")
        if self._need() > 0:
            self._hedge_failed("市价档也没成交完")
        else:
            self.hard_failures = 0

    def _hedge_failed(self, why: str) -> None:
        self.hard_failures += 1
        self.say(f"  ✗ 空单跟进失败（第 {self.hard_failures}/{self.p.max_hedge_failures} 次）：{why}")
        if self.hard_failures >= self.p.max_hedge_failures:
            raise HedgeStuck(why)

    def _hedge_all(self) -> None:
        """把能对冲的整张都对冲完（收尾用）。硬失败攒够次数会抛 HedgeStuck。"""
        while self._need() > 0:
            self._hedge()
            if self._need() > 0:
                self.sleep(self.p.poll_s)

    # ------------------------------------------------------------ 现货吃单（收尾 / 回滚）
    def _spot_take(self, side: str, qty: float, why: str) -> None:
        """现货吃单，限价逐档放宽（与空单同一套档位）。"""
        taker_only = getattr(self.spot, "taker_only", False)
        for bps in list(self.p.hedge_escalation_bps) + [MARKET_BPS]:
            qty = round_down(qty, self.spot.lot)
            if qty < self.spot.min_qty:
                if qty > EPS:
                    self.say(f"  {why}：{qty:g} 低于最小下单量 {self.spot.min_qty:g}，留作零头")
                return
            ref = self._ref(self.spot.book(), side)
            if ref is None:
                continue
            # 只吃单的场所（Robinhood）最后一档发真正的市价单：20% 外的限价可能被拒
            limit = None if (taker_only and bps >= MARKET_BPS) else self._limit(ref, side, bps)
            st = self.spot.take(side, qty, limit)
            if st.filled > EPS:
                sign = 1 if side == self.spot_side else -1
                self.r.spot_qty += sign * st.filled
                self.r.spot_cost += sign * st.filled * st.avg
                self.r.spot_fees += st.fee
                self._track()
                self.on_fill(venue=self.spot.name, side=side, qty=st.filled, price=st.avg,
                             fee=st.fee, order_id=None, kind="taker")
                self.say(f"  {why}：现货吃单 {side} {st.filled:g} @ {st.avg:g}")
            qty -= st.filled
            if qty < self.spot.lot / 2:
                return
        self.say(f"  {why}：各档吃单后还差 {qty:g}")

    def _unwind(self) -> None:
        """空单跟不上时，把没对冲的现货反向退掉，回到零敞口。"""
        extra = self.r.residual(self.cs)
        if extra > self.spot.lot / 2:
            back = "sell" if self.spot_side == "buy" else "buy"
            try:
                self._spot_take(back, extra, "回滚现货")
            except Exception as e:      # noqa: BLE001
                self.say(f"✗ 回滚现货失败：{e}")
        left = self.r.residual(self.cs)
        self.r.status = "unwound" if abs(left) < max(self.spot.min_qty, self.spot.lot) else "aborted"

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
    def _loop_taker(self) -> None:
        """只吃单的现货腿（Robinhood v2）：每次吃一张合约的量，成交后立刻跟进空单。

        单腿风险 = 一张合约 × 一次下单往返的几秒。比挂单流程短得多：
        挂单要等别人来成交，等待期间价格怎么走都有可能。
        """
        stalls = 0
        while self.target_qty - self.r.spot_qty >= self.spot.lot / 2:
            self._hedge()
            before = self.r.spot_qty
            step = min(self.target_qty - self.r.spot_qty, self.cs)
            self._spot_take(self.spot_side, step, f"第 {self.r.contracts + 1} 张")
            self.r.spot_orders += 1
            self._hedge()
            if self.r.spot_qty - before < self.spot.lot / 2:
                stalls += 1
                if stalls >= 3:
                    raise SourceError("现货连续 3 次吃单都没成交")
                self.sleep(self.p.poll_s)
            else:
                stalls = 0
        self._hedge_all()
        self.r.status = "done"

    def _loop(self) -> None:
        if getattr(self.spot, "taker_only", False):
            return self._loop_taker()
        start = self.clock()
        while True:
            self._hedge()
            if self.target_qty - self.r.spot_qty < self.spot.lot / 2:
                break
            timed_out = self.clock() - start > self.p.max_wait_s
            if self.cur is None:
                if timed_out:
                    self.say("挂单超时，进入收尾")
                    break
                res = self._place()
                if res == "small":            # 剩余量低于最小下单量
                    break
                if res == "capped":           # 价格跑出追价上限（只在挂过一次之后发生）
                    if self.r.spot_qty < EPS:
                        self.say(f"  价格跑出追价上限 {self.p.reprice_cap_bps:g}bp，一点没成交，放弃这一份")
                        self.r.status = "skipped"
                        return
                    self.say(f"  价格跑出追价上限 {self.p.reprice_cap_bps:g}bp，已成交部分进入收尾")
                    break
                if res == "rejected":         # 等一下让簿子稳定，再按新价挂
                    self.sleep(min(self.p.poll_s, 0.5))
                    continue
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
                    b = self.spot.book()
                    if (self.r.spot_qty > EPS and b.best_bid and b.best_ask
                            and self._beyond_cap(self._quote(b.best_bid, b.best_ask))):
                        self.sleep(self.p.poll_s)  # 已成交一部分且到追价上限：不改价，挂着等到超时
                        continue
                    self.say("  被抢到前面，撤单重挂")
                    self._cancel()
                    continue
            self.sleep(self.p.poll_s)
        self._finish()

    def run(self) -> Result:
        try:
            self._loop()
        except HedgeStuck as e:
            self.say(f"✗ 空单连续跟不上，停手并回滚现货：{e}")
            self._safe_cancel()
            self._unwind()
        except KeyboardInterrupt:
            self.say("收到 Ctrl-C：撤现货挂单，对冲已成交的整张部分")
            self._emergency("interrupted")
        except Exception as e:          # noqa: BLE001 —— 安全出口：什么异常都先把挂单撤了
            self.r.error = f"{type(e).__name__}: {e}"
            self.say(f"✗ 意外错误：{self.r.error}。撤现货挂单，对冲已成交的整张部分")
            self._emergency("error")
        return self.r

    def _emergency(self, status: str) -> None:
        self._safe_cancel()
        try:
            self._hedge_all()
            self.r.status = status
        except HedgeStuck as e:
            self.say(f"✗ 对冲失败：{e}，回滚现货")
            self._unwind()
        except Exception as e:          # noqa: BLE001
            self.say(f"✗ 对冲时又出错：{e}")
            self.r.status = "aborted"

    def _safe_cancel(self) -> None:
        if not self.cur:
            return
        try:
            self._cancel()
        except Exception as e:      # noqa: BLE001 —— 撤单失败也要把结果报出来
            self.say(f"✗ 撤现货挂单失败：{e} —— 去交易所页面手动撤单 {self.cur['oid']}")
