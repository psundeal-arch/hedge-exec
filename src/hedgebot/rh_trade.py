"""Robinhood 加密货币下单（Crypto Trading API v2，「按费率档」那一版）。

v1 与 v2 的区别（官方文档）：v1 由做市商报价，价差藏在价格里；v2 走合作交易所，
另收按 30 天成交量分档的手续费。2026-10-02 实测 DOGE 3190 枚：
    v1 做市商报价  买卖价差 0.95%       总价 $306.77
    v2 交易所路由  价差 0.007% + 费 0.15%  总价 $304.31
账户 30 天量 $5.3 万时 v2 费率 0.15%（接口 accounts 的 fee_tier_status 里读得到）。
v2 目前一律按吃单费率收（maker/taker 还在灰度），所以市价单和限价单费率一样。
key 权限要勾「Place crypto orders with fee tiers (v2)」。v2 的下单/查单都要带
account_number 查询参数，且它是签名原文 path 的一部分。

签名要点：签名原文里的 body 必须与【实际发送的字节】完全一致。官方示例的签名向量
是拿 Python dict 的 str() 拼出来的（tests 里验证过），真实请求若这样签、却发 JSON，
服务器验签必失败。所以这里先 json.dumps 成字符串，签它、发它，同一份。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import uuid

from .http import SourceError, TransientError, request
from .robinhood import BASE, sign

OPEN_STATES = ("open", "pending", "partially_filled", "queued")


class RHTrader:
    def __init__(self, api_key: str, secret_b64: str):
        self.api_key, self.secret = api_key, secret_b64
        self._acct: dict | None = None

    def _call(self, method: str, path: str, label: str, body: dict | None = None):
        raw = json.dumps(body) if body is not None else ""
        ts = int(time.time())
        headers = {"x-api-key": self.api_key, "x-timestamp": str(ts),
                   "x-signature": sign(self.api_key, self.secret, ts, path, method, raw),
                   "Content-Type": "application/json; charset=utf-8"}
        # attempts=1：时间戳 30 秒过期；下单更不能重发（client_order_id 能去重，但别依赖）
        return request(method, BASE + path, label, attempts=1, headers=headers,
                       data=raw if body is not None else None)

    # ---------------------------------------------------------------- 查询
    def account(self) -> dict:
        """account_number / buying_power / fee_tier_status（费率、30 天量、下一档门槛）。"""
        if self._acct is None:
            res = self._call("GET", "/api/v2/crypto/trading/accounts/", "Robinhood 账户")
            accts = [a for a in res.get("results") or [] if a.get("status") == "active"]
            if not accts:
                raise SourceError("Robinhood 没有可用的加密货币账户")
            self._acct = accts[0]
        return self._acct

    def _acct_q(self) -> str:
        return urllib.parse.urlencode({"account_number": self.account()["account_number"]})

    def pair(self, symbol: str) -> dict:
        q = urllib.parse.urlencode({"symbol": symbol})
        res = self._call("GET", f"/api/v2/crypto/trading/trading_pairs/?{q}",
                         f"Robinhood 交易对 {symbol}").get("results") or []
        if not res:
            raise SourceError(f"Robinhood 没有 {symbol}")
        p = res[0]
        if not p.get("is_api_tradable", True):
            raise SourceError(f"{symbol} 不支持 API 交易")
        # v2 的下限是【金额】min_order_amount（美元），不是数量
        return {"min_usd": float(p.get("min_order_amount") or 0),
                "max": float(p["max_order_size"]),
                "step": float(p["asset_increment"]), "tick": float(p["quote_increment"]),
                "status": p.get("status")}

    def quote(self, symbol: str, side: str, qty: float) -> dict:
        """side: buy → ask 报价，sell → bid 报价。返回成交价、中价、价差、手续费率与金额。"""
        bb = (self._call("GET", f"/api/v2/crypto/marketdata/best_bid_ask/?"
                         f"{urllib.parse.urlencode({'symbol': symbol})}",
                         f"Robinhood 盘口 {symbol}").get("results") or [{}])[0]
        bid, ask = float(bb.get("bid") or 0), float(bb.get("ask") or 0)
        q = urllib.parse.urlencode({"symbol": symbol, "side": "ask" if side == "buy" else "bid",
                                    "quantity": f"{qty:g}"})
        res = self._call("GET", f"/api/v2/crypto/trading/estimated_price/?{q}",
                         f"Robinhood 报价 {symbol}").get("results") or []
        if not res:
            raise SourceError(f"Robinhood 报价为空 {symbol}")
        r = res[0]
        px = float(r.get("ask" if side == "buy" else "bid") or 0)
        mid = (bid + ask) / 2 if bid and ask else px
        return {"price": px, "mid": mid,
                "spread": (ask - bid) / mid if bid and ask else None,
                "fee_ratio": r.get("fee_ratio"), "fee": r.get("est_fee"),
                "total": r.get("est_total_cost") or r.get("est_total_credit")}

    def order(self, oid: str) -> dict:
        return self._call("GET", f"/api/v2/crypto/trading/orders/{oid}/?{self._acct_q()}",
                          f"Robinhood 订单 {oid}")

    def cancel(self, oid: str) -> None:
        self._call("POST", f"/api/v2/crypto/trading/orders/{oid}/cancel/", f"Robinhood 撤单 {oid}")

    # ---------------------------------------------------------------- 下单
    def place(self, side: str, symbol: str, qty_str: str, limit_px: str | None = None,
              client_order_id: str | None = None) -> dict:
        body = {"client_order_id": client_order_id or str(uuid.uuid4()), "side": side,
                "symbol": symbol}
        if limit_px is None:
            body |= {"type": "market", "market_order_config": {"asset_quantity": qty_str}}
        else:
            body |= {"type": "limit", "limit_order_config": {
                "asset_quantity": qty_str, "limit_price": limit_px, "time_in_force": "gtc"}}
        return self._call("POST", f"/api/v2/crypto/trading/orders/?{self._acct_q()}",
                          f"Robinhood {side} {symbol} {qty_str}", body)

    def wait(self, oid: str, timeout_s: float = 60) -> dict:
        """轮询到终态。市价单通常几秒内成交；超时返回最后一次查到的状态。"""
        t0 = time.monotonic()
        while True:
            o = self.order(oid)
            if o.get("state") not in OPEN_STATES or time.monotonic() - t0 > timeout_s:
                return o
            time.sleep(1.5)


def fmt_step(x: float, step: float) -> str:
    """按交易对的数量/价格步长向下取整并格式化，不留浮点尾巴。"""
    import math
    n = math.floor(x / step + 1e-9) * step
    d = max(0, -int(math.floor(math.log10(step) + 1e-9)))
    return f"{n:.{d}f}"


class RobinhoodLeg:
    """Ladder 的现货腿（Robinhood v2）。只做吃单：v2 现在一律按吃单费率收，
    挂单省不了钱，反而要排队、撤单、承担更久的单腿风险。

    Ladder 看到 taker_only 就走逐张吃单的流程（见 ladder._loop_taker）。
    Robinhood 的限价单只有 GTC，没有 IOC —— 所以「带保护价的吃单」实现为：
    下限价单 → 等几秒 → 没成交完就撤 → 取最终成交量。
    """
    taker_only = True
    min_poll_s = 0.0
    min_reprice_s = 0.0

    def __init__(self, trader: RHTrader, symbol: str):
        self.t = trader
        self.symbol = symbol
        self.name = "robinhood"
        p = trader.pair(symbol)
        self.lot, self.tick = p["step"], p["tick"]
        self.min_usd = p["min_usd"]
        self.min_qty = p["step"]

    def book(self):
        from .book import Book
        bb = (self.t._call("GET", "/api/v2/crypto/marketdata/best_bid_ask/?"
                           + urllib.parse.urlencode({"symbol": self.symbol}),
                           f"Robinhood 盘口 {self.symbol}").get("results") or [{}])[0]
        bid, ask = float(bb.get("bid") or 0), float(bb.get("ask") or 0)
        # Robinhood 只给一档（跨合作交易所的最优价），没有深度 —— 数量填一个大数，
        # 深度由下单后实际成交量说话
        return Book(bids=((bid, 1e12),) if bid else (), asks=((ask, 1e12),) if ask else ())

    def _state(self, o: dict):
        from .trade_api import OrderState
        filled = float(o.get("filled_asset_quantity") or 0)
        avg = float(o.get("average_price") or 0) or None
        return OrderState(filled, avg, float(o.get("fee_charged") or 0),
                          o.get("state") in OPEN_STATES, o.get("state") or "")

    def take(self, side: str, qty: float, limit_px: float | None):
        from .trade_api import OrderUnknown
        qty_str = fmt_step(qty, self.lot)
        px_str = None
        if limit_px is not None:
            # 保护价朝更容易成交的方向取整：买单向上、卖单向下
            import math
            n = (math.ceil if side == "buy" else math.floor)(limit_px / self.tick - 1e-9 if side == "buy"
                                                            else limit_px / self.tick + 1e-9)
            px_str = fmt_step(n * self.tick, self.tick)
        cid, since = str(uuid.uuid4()), time.time()
        try:
            o = self.t.place(side, self.symbol, qty_str, px_str, client_order_id=cid)
        except TransientError as e:
            raise OrderUnknown(f"robinhood {side} {qty_str}：下单请求结果不明（{e}）",
                               leg=self.name, side=side, qty=qty, cid=cid, since=since) from e
        oid = o.get("id")
        try:
            o = self.t.wait(oid, timeout_s=10)
            if o.get("state") in OPEN_STATES:
                self.t.cancel(oid)
                o = self.t.wait(oid, timeout_s=10)
        except SourceError as e:
            raise OrderUnknown(f"robinhood 订单 {oid} 已提交，查询/撤单出错（{e}）",
                               leg=self.name, side=side, qty=qty, oid=oid) from e
        return self._state(o)

    def find(self, u) -> str | None:
        """对账：有 oid 直接用；否则在下单时刻之后的订单列表里按 client_order_id 找。"""
        if u.oid:
            return u.oid
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(u.since - 300))
        q = urllib.parse.urlencode({"account_number": self.t.account()["account_number"],
                                    "symbol": self.symbol, "created_at_start": start})
        res = self.t._call("GET", f"/api/v2/crypto/trading/orders/?{q}", "Robinhood 订单列表")
        for o in res.get("results") or []:
            if o.get("client_order_id") == u.cid:
                return o.get("id")
        return None

    def preview(self, side: str, qty: float) -> dict:
        try:
            q = self.t.quote(self.symbol, side, qty)
            return {"ok": True, "errors": [], "warnings": [], "fee": q.get("fee"), "raw": q}
        except SourceError as e:
            return {"ok": False, "errors": [str(e)], "warnings": [], "fee": None, "raw": None}

    # Ladder 的挂单接口：taker_only 时不会被调用
    def place_post_only(self, *a, **k):
        raise NotImplementedError("Robinhood 腿只做吃单")

    # status / cancel：对账（ladder._settle）要用
    def status(self, oid: str):
        return self._state(self.t.order(oid))

    def cancel(self, oid: str) -> None:
        self.t.cancel(oid)


def rh_available(trader: RHTrader) -> dict[str, float]:
    """USD 购买力 + 各币可卖数量（与 kraken_available / coinbase_available 同形状）。"""
    from .robinhood import holdings
    out = {"USD": float(trader.account().get("buying_power") or 0)}
    # 可卖数量，不是总量：质押中的币卖不了（平仓预检据此拦截）
    out.update(holdings(trader.api_key, trader.secret, available=True))
    return out
