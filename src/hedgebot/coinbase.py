"""Coinbase CDE（美国场内 nano 永续）：公开行情 + 只读持仓。不下单。

字段口径沿用 perp-track 实测（2026-09-29）：
- 资金费率在 future_product_details 的【顶层】funding_rate；同结构里的
  perpetual_details.funding_rate 是 INTX 用的，对 CDE 是空串。
- 合约乘数各不相同（BTC 0.01、ETH 0.1、LTC 5），取不到就报错，【不要】默认 1。
- 订单簿的数量单位是【张】。
- 【报价单位】PEPE、SHIB 这类合约叫「1000PEPE PERP」：价格是每 1000 枚，合约乘数
  也以 1000 枚为单位（1 张 PEPE = 100000 × 1000 = 1 亿枚 ≈ $439）。不换算的话，
  对冲现货只会买到千分之一。所以这里在入口统一换算成【每枚币】：价格 ÷ unit，
  contract_size = 每张合约对应多少枚币。往上的代码只见到「每枚」的口径。
"""
from __future__ import annotations

import base64
import re
import secrets
import time

from .book import Book
from .http import SourceError, request

HOST = "api.coinbase.com"
PRODUCTS = "/api/v3/brokerage/market/products"
PRODUCT_BOOK = "/api/v3/brokerage/market/product_book"
POSITIONS = "/api/v3/brokerage/cfm/positions"
ACCOUNTS = "/api/v3/brokerage/accounts"


def _f(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return float(v)


def price_unit(product: dict) -> float:
    """合约名开头的数字就是报价单位：「1000PEPE PERP」→ 1000，「BTC PERP」→ 1。"""
    d = product.get("future_product_details") or {}
    for name in (d.get("contract_display_name"), product.get("display_name")):
        m = re.match(r"^\s*(\d+)\s*[A-Za-z]", name or "")
        if m:
            return float(m.group(1))
    return 1.0


def _all_futures(page: int = 100) -> list[dict]:
    """全部期货产品，按 offset 翻页（M6）。

    接口单页最多给 100 个（limit=250 也只回 100），2026-10-08 全场正好 100 个 ——
    再上一个新合约，单次请求就会把某个永续挤出去，所有命令报「CDE 没有 X 的永续」。
    不信 pagination.has_next（实测恒为 False），一直翻到空页为止。"""
    out, seen, off = [], set(), 0
    while off < 5000:
        body = request("GET", f"https://{HOST}{PRODUCTS}", "Coinbase 合约列表",
                       params={"product_type": "FUTURE", "limit": page, "offset": off})
        ps = [p for p in body.get("products") or [] if p.get("product_id") not in seen]
        if not ps:
            break
        out += ps
        seen |= {p.get("product_id") for p in ps}
        off += len(ps)
    return out


def cde_perps() -> dict[str, dict]:
    """标的 -> 该标的的 CDE 永续。一个公开请求拿全部。

    CDE 的「永续」在接口里 contract_expiry_type 是 EXPIRING（到期日设在
    2030 年），和月度交割合约的区别是有 funding_interval。
    """
    out: dict[str, dict] = {}
    for p in _all_futures():
        d = p.get("future_product_details") or {}
        if d.get("venue") != "cde" or not d.get("funding_interval"):
            continue
        root = (d.get("contract_root_unit") or "").upper()
        cs = _f(d.get("contract_size"))
        if not root or not cs:
            continue
        oi = _f(d.get("open_interest")) or 0.0
        # 同一标的万一有两个永续，取未平仓量大的那个
        if root in out and out[root]["open_interest"] >= oi:
            continue
        interval = float(str(d["funding_interval"]).rstrip("s"))
        unit = price_unit(p)
        ix = _f(d.get("index_price"))
        out[root] = {
            "product_id": p["product_id"],
            "unit": unit,                     # 报价单位：每个价格对应多少枚币
            "contract_size": cs * unit,       # 每张合约对应多少【枚】币
            "contract_size_raw": cs,
            "funding_rate": _f(d.get("funding_rate")),
            "interval_s": interval,
            "index_price": ix / unit if ix is not None else None,   # 每枚
            "open_interest": oi,
            "session_open": bool((p.get("fcm_trading_session_details") or {})
                                 .get("is_session_open")),
            "close_time": (p.get("fcm_trading_session_details") or {}).get("close_time"),
            "maintenance": (p.get("fcm_trading_session_details") or {}).get("maintenance"),
            "trading_disabled": bool(p.get("trading_disabled")),
            "volume_24h_usd": _f(p.get("approximate_quote_24h_volume")),
            "margin_short_overnight": _f((d.get("overnight_margin_rate") or {})
                                         .get("short_margin_rate")),
            "margin_short_intraday": _f((d.get("intraday_margin_rate") or {})
                                        .get("short_margin_rate")),
        }
    if not out:
        raise SourceError("合约列表里没有 CDE 永续（接口结构变了？）")
    return out


def spot_volume_usd(product_id: str) -> float | None:
    """现货 24h 成交额（美元）。算参与率用。"""
    body = request("GET", f"https://{HOST}{PRODUCTS}/{product_id}", f"Coinbase {product_id}")
    return _f(body.get("approximate_quote_24h_volume"))


def book(product_id: str, limit: int = 100, unit: float = 1.0) -> Book:
    """unit ≠ 1 时价格换算成每枚（见文件头）。数量单位不变（永续仍是张）。"""
    body = request("GET", f"https://{HOST}{PRODUCT_BOOK}", f"Coinbase 订单簿 {product_id}",
                   params={"product_id": product_id, "limit": limit})
    pb = body.get("pricebook") or {}
    return Book(bids=tuple((float(x["price"]) / unit, float(x["size"])) for x in pb.get("bids") or []),
                asks=tuple((float(x["price"]) / unit, float(x["size"])) for x in pb.get("asks") or []))


def _token(name: str, secret: str, method: str, path: str) -> str:
    """CDP 的 Ed25519 key 签 JWT。uri 必须与实际请求的方法+主机+路径一致。"""
    import jwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = base64.b64decode(secret)
    # 64 字节 = 32 种子 + 32 公钥。长度不对时会签出形式合法的 JWT、回 401，
    # 看起来像权限不够，实际是密钥贴漏了 —— 所以签名前就卡住。
    if len(raw) != 64:
        raise SourceError(f"COINBASE_API_PRIVATE_KEY 解码后 {len(raw)} 字节，应为 64")
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    now = int(time.time())
    return jwt.encode({"sub": name, "iss": "cdp", "nbf": now, "exp": now + 120,
                       "uri": f"{method} {HOST}{path}"},
                      key, algorithm="EdDSA",
                      headers={"kid": name, "nonce": secrets.token_hex(16)})


def spot_balances(key_name: str, key_secret: str) -> dict[str, float]:
    """现货账户里每个币的【总量】（可用 + 挂单冻结）。View 权限即可。

    冻结的也算 —— 对冲看的是价格风险，挂着卖单的币涨跌照样算你的。
    """
    out: dict[str, float] = {}
    cursor = None
    for _ in range(20):
        params = {"limit": 250, **({"cursor": cursor} if cursor else {})}
        tok = _token(key_name, key_secret, "GET", ACCOUNTS)
        body = request("GET", f"https://{HOST}{ACCOUNTS}", "Coinbase 现货余额",
                       headers={"Authorization": f"Bearer {tok}"}, params=params)
        for a in body.get("accounts") or []:
            v = sum(float((a.get(k) or {}).get("value") or 0)
                    for k in ("available_balance", "hold"))
            if v:
                cur = (a.get("currency") or "").upper()
                out[cur] = out.get(cur, 0.0) + v
        if not body.get("has_next"):
            break
        cursor = body.get("cursor")
    return out


def positions(key_name: str, key_secret: str) -> list[dict]:
    """[{product, side, contracts}]。key 只需 View 权限。"""
    tok = _token(key_name, key_secret, "GET", POSITIONS)
    body = request("GET", f"https://{HOST}{POSITIONS}", "Coinbase 期货持仓",
                   headers={"Authorization": f"Bearer {tok}"})
    return [{"product": r.get("product_id"), "side": r.get("side"),
             "contracts": _f(r.get("number_of_contracts")) or 0.0}
            for r in body.get("positions") or []]
