"""模拟腿：盯真实行情，只把「下单」换成模拟。给 hedge live --paper 用。

和 trade_api 的真实腿接口一致，所以 Ladder 引擎一行不改 —— 模拟跑的就是实盘那套逻辑。

挂单（post-only）什么时候算成交：
- 下单那一刻会吃单（买价 ≥ 卖一）→ 像交易所一样拒掉
- 挂单之后出现了【更优】的成交（买单：成交价 < 挂价）→ 按挂价成交，数量取那些成交的量。
  卖方要吃到更低的价，必然先吃掉我们
- 挂价比当时的最优价好一档（价位上只有我们）→ 正好【等于】挂价的成交也算我们的
- 只是排在已有最优价后面 → 同价成交不算（前面还有人排队），偏保守
- 订单簿已经越过挂价（卖一 ≤ 买价）→ 剩余全部按挂价成交

吃单：按真实订单簿逐档吃到 limit_px 为止，簿子不够就部分成交（和 IOC 一样）。
"""
from __future__ import annotations

import datetime as dt
import itertools
import time

from . import coinbase, kraken
from .book import walk
from .http import request
from .trade_api import OrderState

_ids = itertools.count(1)


def coinbase_trades(product_id: str) -> list[tuple[str, float, float, float]]:
    """[(成交号, 时间戳秒, 价格, 数量)]，最近 100 笔。"""
    body = request("GET", f"https://{coinbase.HOST}{coinbase.PRODUCTS}/{product_id}/ticker",
                   f"Coinbase 成交 {product_id}", params={"limit": 100})
    out = []
    for t in body.get("trades") or []:
        ts = dt.datetime.fromisoformat(t["time"].replace("Z", "+00:00")).timestamp()
        out.append((t["trade_id"], ts, float(t["price"]), float(t["size"])))
    return out


def kraken_trades(pair: str) -> list[tuple[str, float, float, float]]:
    res = kraken._result(request("GET", f"{kraken.API}/0/public/Trades", f"Kraken 成交 {pair}",
                                 params={"pair": pair, "count": 200}), f"Kraken 成交 {pair}")
    rows = next(v for k, v in res.items() if k != "last")
    return [(str(r[6]), float(r[2]), float(r[0]), float(r[1])) for r in rows]


class PaperSpotLeg:
    def __init__(self, real, *, maker_fee: float, taker_fee: float, trades_fn, now=time.time):
        self.real = real
        self.name = real.name
        self.tick, self.lot, self.min_qty = real.tick, real.lot, real.min_qty
        # 节奏下限跟真实腿一致，模拟出来的耗时/挂单次数才有参考价值
        self.taker_only = getattr(real, "taker_only", False)   # Robinhood：逐张吃单
        self.min_poll_s = getattr(real, "min_poll_s", 0.0)
        self.min_reprice_s = getattr(real, "min_reprice_s", 0.0)
        self.maker_fee, self.taker_fee = maker_fee, taker_fee
        self.trades_fn = trades_fn
        self.now = now
        self.orders: dict[str, dict] = {}

    def book(self):
        return self.real.book()

    def place_post_only(self, side: str, qty: float, px: float) -> str:
        b = self.book()
        oid = f"paper-{next(_ids)}"
        crosses = (side == "buy" and b.best_ask is not None and px >= b.best_ask) or \
                  (side == "sell" and b.best_bid is not None and px <= b.best_bid)
        improved = (side == "buy" and b.best_bid is not None and px > b.best_bid) or \
                   (side == "sell" and b.best_ask is not None and px < b.best_ask)
        self.orders[oid] = {"side": side, "qty": qty, "px": px, "filled": 0.0,
                            "open": not crosses, "t0": self.now(), "improved": improved,
                            "seen": set(), "reason": "post-only 会吃单，被拒" if crosses else ""}
        return oid

    def _better(self, o: dict, price: float) -> bool:
        """这笔成交价是否意味着我们的挂单会被吃到。"""
        if o["side"] == "buy":
            return price < o["px"] or (o["improved"] and price == o["px"])
        return price > o["px"] or (o["improved"] and price == o["px"])

    def status(self, oid: str) -> OrderState:
        o = self.orders[oid]
        if o["open"]:
            for tid, ts, price, size in self.trades_fn():
                if tid in o["seen"] or ts < o["t0"]:
                    continue
                o["seen"].add(tid)
                if self._better(o, price):
                    o["filled"] = min(o["qty"], o["filled"] + size)
            b = self.book()
            crossed = (o["side"] == "buy" and b.best_ask is not None and b.best_ask <= o["px"]) or \
                      (o["side"] == "sell" and b.best_bid is not None and b.best_bid >= o["px"])
            if crossed:
                o["filled"] = o["qty"]
            if o["filled"] >= o["qty"] - self.lot / 2:
                o["filled"], o["open"] = o["qty"], False
        f = o["filled"]
        return OrderState(f, o["px"] if f else None, f * o["px"] * self.maker_fee, o["open"],
                          "OPEN" if o["open"] else ("FILLED" if f >= o["qty"] else "CANCELLED"),
                          o["reason"])

    def cancel(self, oid: str) -> None:
        self.orders[oid]["open"] = False

    def take(self, side: str, qty: float, limit_px: float) -> OrderState:
        return _take(self.book(), side, qty, limit_px, mult=1.0,
                     fee_rate=self.taker_fee, per_unit=0.0)

    def preview(self, side: str, qty: float) -> dict:
        return {"ok": True, "errors": [], "warnings": [], "fee": None, "raw": "paper"}


class PaperPerpLeg:
    def __init__(self, real, *, contract_size: float, taker_fee: float, per_contract: float):
        self.real = real
        self.name = real.name
        self.tick, self.lot, self.min_qty = real.tick, real.lot, real.min_qty
        self.cs, self.taker_fee, self.per_contract = contract_size, taker_fee, per_contract

    def book(self):
        return self.real.book()

    def take(self, side: str, qty: float, limit_px: float) -> OrderState:
        # CDE 实盘用市价 IOC；这里同样按簿子吃，但不超过 limit_px —— 和实盘前的滑点把关一致
        return _take(self.book(), side, qty, limit_px, mult=self.cs,
                     fee_rate=self.taker_fee, per_unit=self.per_contract)

    def preview(self, side: str, qty: float) -> dict:
        return {"ok": True, "errors": [], "warnings": [], "fee": None, "raw": "paper"}


def _take(book, side: str, qty: float, limit_px: float, *, mult: float,
          fee_rate: float, per_unit: float) -> OrderState:
    if limit_px is None:                                 # 市价
        levels = list(book.asks if side == "buy" else book.bids)
    elif side == "buy":
        levels = [(p, s) for p, s in book.asks if p <= limit_px]
    else:
        levels = [(p, s) for p, s in book.bids if p >= limit_px]
    avg, filled = walk(levels, qty)
    fee = (avg or 0) * filled * mult * fee_rate + filled * per_unit
    return OrderState(filled, avg, fee, False, "FILLED" if filled >= qty - 1e-9 else "PARTIAL")
