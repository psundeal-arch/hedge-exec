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

from .http import SourceError, request
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
    def place(self, side: str, symbol: str, qty_str: str, limit_px: str | None = None) -> dict:
        body = {"client_order_id": str(uuid.uuid4()), "side": side, "symbol": symbol}
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
