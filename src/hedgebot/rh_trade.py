"""Robinhood 加密货币下单（Crypto Trading API v1，「不带费率档」那一版）。

v1 与 v2 的区别（官方文档）：v1 由做市商报价，价格里含买卖价差、不另收手续费；
v2 走合作交易所、按费率档收手续费。这里用 v1 —— 与只读的 holdings 同一版本，
key 权限勾「Place crypto orders without fee tiers (v1)」即可。

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
        return self._call("GET", "/api/v1/crypto/trading/accounts/", "Robinhood 账户")

    def pair(self, symbol: str) -> dict:
        q = urllib.parse.urlencode({"symbol": symbol})
        res = self._call("GET", f"/api/v1/crypto/trading/trading_pairs/?{q}",
                         f"Robinhood 交易对 {symbol}").get("results") or []
        if not res:
            raise SourceError(f"Robinhood 没有 {symbol}")
        p = res[0]
        return {"min": float(p["min_order_size"]), "max": float(p["max_order_size"]),
                "step": float(p["asset_increment"]), "tick": float(p["quote_increment"]),
                "status": p.get("status")}

    def quote(self, symbol: str, side: str, qty: float) -> dict:
        """side: buy → 取 ask 报价，sell → 取 bid 报价。价格含 Robinhood 的价差。"""
        q = urllib.parse.urlencode({"symbol": symbol, "side": "ask" if side == "buy" else "bid",
                                    "quantity": f"{qty:g}"})
        res = self._call("GET", f"/api/v1/crypto/marketdata/estimated_price/?{q}",
                         f"Robinhood 报价 {symbol}").get("results") or []
        if not res:
            raise SourceError(f"Robinhood 报价为空 {symbol}")
        r = res[0]
        px = float(r.get("price") or 0)
        key = "ask_inclusive_of_buy_spread" if side == "buy" else "bid_inclusive_of_sell_spread"
        spread = r.get("buy_spread" if side == "buy" else "sell_spread")
        return {"price": float(r.get(key) or px), "mid": px,
                "spread": float(spread) if spread not in (None, "") else None}

    def order(self, oid: str) -> dict:
        return self._call("GET", f"/api/v1/crypto/trading/orders/{oid}/", f"Robinhood 订单 {oid}")

    def cancel(self, oid: str) -> None:
        self._call("POST", f"/api/v1/crypto/trading/orders/{oid}/cancel/", f"Robinhood 撤单 {oid}")

    # ---------------------------------------------------------------- 下单
    def place(self, side: str, symbol: str, qty_str: str, limit_px: str | None = None) -> dict:
        body = {"client_order_id": str(uuid.uuid4()), "side": side, "symbol": symbol}
        if limit_px is None:
            body |= {"type": "market", "market_order_config": {"asset_quantity": qty_str}}
        else:
            body |= {"type": "limit", "limit_order_config": {
                "asset_quantity": qty_str, "limit_price": limit_px, "time_in_force": "gtc"}}
        return self._call("POST", "/api/v1/crypto/trading/orders/",
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
