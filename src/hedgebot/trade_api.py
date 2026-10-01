"""真实下单的场所接口。ladder.py 只通过这里的「腿」下单。

每条腿实现同一组方法：
    book()                         -> Book（公开订单簿）
    place_post_only(side, qty, px) -> 订单号（只挂单；会吃单就被交易所拒掉）
    status(oid)                    -> OrderState（累计成交量、均价、手续费、是否还挂着）
    cancel(oid)
    take(side, qty, limit_px)      -> OrderState（立即成交，剩余撤销；等到终态才返回）
    preview(side, qty)             -> 不下单的预检（Coinbase preview / Kraken validate）

POST 一律不自动重试：下单请求超时时，单子可能已经下了，重发就是两单。
Coinbase 靠 client_order_id 去重，Kraken 没有 —— 所以失败直接抛给上层处理。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
import urllib.parse
import uuid
from dataclasses import dataclass

from . import coinbase, kraken
from .book import Book
from .http import SourceError, request


class PostOnlyRejected(SourceError):
    """只挂单被拒：下单那一瞬间价格变了，按这个价会变成吃单。重挂即可，不是故障。

    Coinbase 是在下单请求里【同步】拒（success=false，INVALID_LIMIT_PRICE_POST_ONLY），
    不会生成订单；Kraken 则是先建单再撤（status=canceled），走 status 那条路。
    2026-10-01 首次实盘加仓时踩到：被当成意外错误，整笔停下。
    """


@dataclass(frozen=True)
class OrderState:
    filled: float          # 累计成交（现货是币，永续是张）
    avg: float | None      # 累计成交均价
    fee: float             # 累计手续费（美元）
    open: bool             # 还在挂着 / 处理中
    status: str            # 交易所原始状态
    reason: str = ""


def round_down(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step


def round_up(x: float, step: float) -> float:
    return math.ceil(x / step - 1e-9) * step


def fmt(x: float, step: float) -> str:
    """按交易所精度输出字符串。step=0.01 → 2 位小数。"""
    d = max(0, -int(math.floor(math.log10(step) + 1e-9)))
    return f"{x:.{d}f}"


# ---------------------------------------------------------------- Coinbase
class _CoinbaseAuth:
    def __init__(self, key_name: str, key_secret: str):
        self.key_name, self.key_secret = key_name, key_secret

    def call(self, method: str, path: str, label: str, body: dict | None = None,
             params: dict | None = None) -> dict:
        tok = coinbase._token(self.key_name, self.key_secret, method, path)
        return request(method, f"https://{coinbase.HOST}{path}", label,
                       attempts=1 if method == "POST" else 3,
                       headers={"Authorization": f"Bearer {tok}",
                                "Content-Type": "application/json"},
                       data=json.dumps(body) if body is not None else None,
                       params=params)


ORDERS = "/api/v3/brokerage/orders"
CB_OPEN = ("OPEN", "PENDING", "QUEUED", "CANCEL_QUEUED", "EDIT_QUEUED")


class CoinbaseLeg:
    """Coinbase 现货（BTC-USD）或 CDE 永续（BIP-20DEC30-CDE）。两者走同一套下单接口；
    永续的数量单位是【张】。"""
    min_poll_s = 0.0          # Coinbase 私有接口每秒 30 次，2 秒一查绰绰有余
    min_reprice_s = 0.0

    def __init__(self, auth: _CoinbaseAuth, product_id: str, *, is_perp: bool):
        self.auth = auth
        self.product_id = product_id
        self.is_perp = is_perp
        self.name = "cde" if is_perp else "coinbase"
        p = request("GET", f"https://{coinbase.HOST}{coinbase.PRODUCTS}/{product_id}",
                    f"Coinbase 合约规格 {product_id}")
        # 永续的报价单位（1000PEPE → 1000）。对上层一律给「每枚」价格，见 coinbase.py 文件头
        self.unit = coinbase.price_unit(p) if is_perp else 1.0
        self.tick = float(p.get("price_increment") or p["quote_increment"]) / self.unit
        self.lot = float(p["base_increment"])
        self.min_qty = float(p.get("base_min_size") or self.lot)

    def book(self) -> Book:
        return coinbase.book(self.product_id, unit=self.unit)

    def _price(self, px: float, *, up: bool) -> str:
        """每枚价格 → 交易所原始单位、取整到最小价格单位的字符串。

        只按小数位数格式化是不够的：ZEC 永续最小单位 0.05，1359.27 会被拒
        （INVALID_PRICE_PRECISION）。up=True 向上取整。
        """
        step = self.tick * self.unit
        raw = px * self.unit
        return fmt(round_up(raw, step) if up else round_down(raw, step), step)

    def _submit(self, side: str, config: dict, label: str) -> str:
        body = {"client_order_id": str(uuid.uuid4()), "product_id": self.product_id,
                "side": side.upper(), "order_configuration": config}
        r = self.auth.call("POST", ORDERS, label, body)
        if not r.get("success"):
            err = r.get("error_response") or r
            if "POST_ONLY" in str(err.get("preview_failure_reason") or "") + str(err.get("error") or "") \
                    + str(err.get("new_order_failure_reason") or ""):
                raise PostOnlyRejected(f"{label}：会变成吃单，被拒")
            raise SourceError(f"{label} 被拒：{err.get('error') or ''} "
                              f"{err.get('message') or err.get('preview_failure_reason') or err}")
        return (r.get("success_response") or {}).get("order_id") or r["order_id"]

    def place_post_only(self, side: str, qty: float, px: float) -> str:
        return self._submit(side, {"limit_limit_gtc": {
            # 挂单价朝「不会吃单」的方向取整：买单向下、卖单向上
            "base_size": fmt(qty, self.lot), "limit_price": self._price(px, up=side == "sell"),
            "post_only": True}}, f"{self.name} 挂单 {side} {qty:g}@{px:g}")

    def status(self, oid: str) -> OrderState:
        o = self.auth.call("GET", f"{ORDERS}/historical/{oid}", f"{self.name} 订单 {oid}")["order"]
        filled = float(o.get("filled_size") or 0)
        avg = (float(o["average_filled_price"]) / self.unit
               if filled and o.get("average_filled_price") else None)
        return OrderState(filled, avg, float(o.get("total_fees") or 0),
                          o.get("status") in CB_OPEN, o.get("status", ""),
                          o.get("reject_reason") or o.get("cancel_message") or "")

    def cancel(self, oid: str) -> None:
        self.auth.call("POST", f"{ORDERS}/batch_cancel", f"{self.name} 撤单 {oid}",
                       {"order_ids": [oid]})

    def take(self, side: str, qty: float, limit_px: float | None) -> OrderState:
        """带限价的 IOC：最多吃到 limit_px，剩余撤销 —— 保护价交给撮合引擎执行，
        而不只是下单前看一眼簿子（看完到下单之间簿子可能已经变了）。
        2026-10-01 用 preview 确认 CDE 永续接受 sor_limit_ioc。

        limit_px=None → 真正的市价单（永续升级到最后一档时用）。不用一个很远的
        限价代替：偏离太大的限价可能超出交易所价格带被直接拒掉。
        """
        if limit_px is None:
            config = {"market_market_ioc": {"base_size": fmt(qty, self.lot)}}
        else:
            # 保护价朝「更容易成交」的方向取整：买单向上、卖单向下
            config = {"sor_limit_ioc": {"base_size": fmt(qty, self.lot),
                                        "limit_price": self._price(limit_px, up=side == "buy")}}
        oid = self._submit(side, config, f"{self.name} 吃单 {side} {qty:g}")
        return _wait_final(self, oid)

    def preview(self, side: str, qty: float) -> dict:
        body = {"product_id": self.product_id, "side": side.upper(),
                "order_configuration": {"market_market_ioc": {"base_size": fmt(qty, self.lot)}}}
        r = self.auth.call("POST", f"{ORDERS}/preview", f"{self.name} 预检", body)
        return {"ok": not r.get("errs"), "errors": r.get("errs") or [],
                "warnings": r.get("warning") or [],
                "fee": r.get("commission_total"), "avg_px": r.get("best_bid") if side == "sell"
                else r.get("best_ask"), "raw": r}


def coinbase_available(auth: _CoinbaseAuth) -> dict[str, float]:
    """币种 -> 可用余额（不含挂单冻结）。"""
    out: dict[str, float] = {}
    cursor = None
    for _ in range(20):
        params = {"limit": 250, **({"cursor": cursor} if cursor else {})}
        r = auth.call("GET", "/api/v3/brokerage/accounts", "Coinbase 账户", params=params)
        for a in r.get("accounts") or []:
            v = float((a.get("available_balance") or {}).get("value") or 0)
            if v:
                out[a["currency"]] = out.get(a["currency"], 0) + v
        if not r.get("has_next"):
            break
        cursor = r.get("cursor")
    return out


# ---------------------------------------------------------------- Kraken
class _KrakenAuth:
    def __init__(self, key: str, secret: str):
        self.key, self.secret = key, secret
        self._last_nonce = 0

    def call(self, path: str, data: dict, label: str) -> dict:
        """Kraken 私有接口按「计数器」限频（基础档上限 15、每 3 秒回落 1）。
        被限频的请求【不会执行】—— 包括下单 —— 所以等一下重签名重试是安全的。
        每次重试都用新 nonce：同一个 nonce 只能用一次。
        """
        for i in range(4):
            # nonce 必须严格递增；同一毫秒内连发两单会撞，所以手动保证 +1
            nonce = max(int(time.time() * 1000), self._last_nonce + 1)
            self._last_nonce = nonce
            d = {"nonce": nonce, **data}
            headers = {"API-Key": self.key, "API-Sign": kraken._sign(path, d, self.secret)}
            body = request("POST", kraken.API + path, label, attempts=1, data=d, headers=headers)
            errs = " ".join(body.get("error") or [])
            if "Rate limit" in errs or "Too many requests" in errs:
                if i < 3:
                    time.sleep(3 * (i + 1))
                    continue
            return kraken._result(body, label)
        return kraken._result(body, label)


KR_OPEN = ("pending", "open")


class KrakenLeg:
    # 节奏下限（Ladder 取它与配置的较大值）：
    # - 查单：QueryOrders 每次 +1 计数，基础档每 3 秒回落 1 —— 2 秒一查会越积越多直到被拒
    # - 撤单重挂：Kraken 对「挂上不久就撤」加罚分，5 秒内撤 +8、15 秒内 +5、45 秒外 +2
    #   （交易计数器基础档上限 60）。4 秒就撤、追几次价就会被暂停下单
    min_poll_s = 3.0
    min_reprice_s = 15.0

    def __init__(self, auth: _KrakenAuth, pair: str):
        self.auth = auth
        self.pair = pair
        self.name = "kraken"
        res = kraken._result(request("GET", f"{kraken.API}/0/public/AssetPairs", "Kraken 交易对",
                                     params={"pair": pair}), "Kraken 交易对")
        v = next(iter(res.values()))
        self.tick = float(v.get("tick_size") or 10 ** -int(v["pair_decimals"]))
        self.lot = 10 ** -int(v["lot_decimals"])
        self.min_qty = float(v["ordermin"])

    def book(self) -> Book:
        return kraken.depth(self.pair)

    def _add(self, data: dict, label: str) -> str:
        r = self.auth.call("/0/private/AddOrder", {"pair": self.pair, **data}, label)
        return r["txid"][0]

    def place_post_only(self, side: str, qty: float, px: float) -> str:
        px = round_up(px, self.tick) if side == "sell" else round_down(px, self.tick)
        try:
            return self._add({"type": side, "ordertype": "limit", "volume": fmt(qty, self.lot),
                              "price": fmt(px, self.tick), "oflags": "post"},
                             f"kraken 挂单 {side} {qty:g}@{px:g}")
        except SourceError as e:
            # Kraken 多数情况是先建单再撤（status=canceled，reason 含 Post only），
            # 走 status 那条路；万一在下单时直接报错，也按「被拒重挂」处理
            if "post" in str(e).lower() and "only" in str(e).lower():
                raise PostOnlyRejected(str(e)) from e
            raise

    def status(self, oid: str) -> OrderState:
        o = self.auth.call("/0/private/QueryOrders", {"txid": oid}, f"kraken 订单 {oid}")[oid]
        filled = float(o.get("vol_exec") or 0)
        cost = float(o.get("cost") or 0)
        return OrderState(filled, cost / filled if filled else None, float(o.get("fee") or 0),
                          o.get("status") in KR_OPEN, o.get("status", ""), o.get("reason") or "")

    def cancel(self, oid: str) -> None:
        try:
            self.auth.call("/0/private/CancelOrder", {"txid": oid}, f"kraken 撤单 {oid}")
        except SourceError as e:
            # 已经成交完/已撤的单再撤会报 Unknown order —— 终态由 status 判断，这里不算错
            if "Unknown order" not in str(e):
                raise

    def take(self, side: str, qty: float, limit_px: float | None) -> OrderState:
        if limit_px is None:
            data = {"type": side, "ordertype": "market", "volume": fmt(qty, self.lot)}
        else:
            px = round_up(limit_px, self.tick) if side == "buy" else round_down(limit_px, self.tick)
            data = {"type": side, "ordertype": "limit", "volume": fmt(qty, self.lot),
                    "price": fmt(px, self.tick), "timeinforce": "IOC"}
        oid = self._add(data, f"kraken 吃单 {side} {qty:g}")
        return _wait_final(self, oid)

    def preview(self, side: str, qty: float) -> dict:
        try:
            r = self.auth.call("/0/private/AddOrder",
                               {"pair": self.pair, "type": side, "ordertype": "market",
                                "volume": fmt(qty, self.lot), "validate": "true"},
                               "kraken 预检")
            return {"ok": True, "errors": [], "warnings": [], "fee": None,
                    "raw": r.get("descr")}
        except SourceError as e:
            return {"ok": False, "errors": [str(e)], "warnings": [], "fee": None, "raw": None}


def kraken_available(auth: _KrakenAuth) -> dict[str, float]:
    """可交易余额。质押中的（.B/.F…）不能直接卖，所以不算。"""
    res = auth.call("/0/private/Balance", {}, "Kraken 余额")
    out: dict[str, float] = {}
    for code, amt in res.items():
        base, suffix = kraken.normalize_asset(code)
        if not suffix:
            out[base] = out.get(base, 0) + float(amt)
    return out


def _wait_final(leg, oid: str, timeout_s: float = 20) -> OrderState:
    """IOC 单很快就到终态；轮询到不再 open 为止。超时说明交易所卡住了，抛出让上层停手。"""
    t0 = time.monotonic()
    while True:
        st = leg.status(oid)
        if not st.open:
            return st
        if time.monotonic() - t0 > timeout_s:
            raise SourceError(f"{leg.name} 订单 {oid} {timeout_s:g} 秒还没到终态（{st.status}）")
        time.sleep(0.5)
