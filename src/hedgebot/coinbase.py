"""Coinbase CDE（美国场内 nano 永续）：公开行情 + 只读持仓。不下单。

字段口径沿用 perp-track 实测（2026-09-29）：
- 资金费率在 future_product_details 的【顶层】funding_rate；同结构里的
  perpetual_details.funding_rate 是 INTX 用的，对 CDE 是空串。
- 合约乘数各不相同（BTC 0.01、ETH 0.1、LTC 5），取不到就报错，【不要】默认 1。
- 订单簿的数量单位是【张】。
"""
from __future__ import annotations

import base64
import secrets
import time

from .book import Book
from .http import SourceError, request

HOST = "api.coinbase.com"
PRODUCTS = "/api/v3/brokerage/market/products"
PRODUCT_BOOK = "/api/v3/brokerage/market/product_book"
POSITIONS = "/api/v3/brokerage/cfm/positions"


def _f(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return float(v)


def cde_perps() -> dict[str, dict]:
    """标的 -> 该标的的 CDE 永续。一个公开请求拿全部。

    CDE 的「永续」在接口里 contract_expiry_type 是 EXPIRING（到期日设在
    2030 年），和月度交割合约的区别是有 funding_interval。
    """
    body = request("GET", f"https://{HOST}{PRODUCTS}", "Coinbase 合约列表",
                   params={"product_type": "FUTURE", "limit": 250})
    out: dict[str, dict] = {}
    for p in body.get("products") or []:
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
        out[root] = {
            "product_id": p["product_id"],
            "contract_size": cs,
            "funding_rate": _f(d.get("funding_rate")),
            "interval_s": interval,
            "index_price": _f(d.get("index_price")),
            "open_interest": oi,
            "session_open": bool((p.get("fcm_trading_session_details") or {})
                                 .get("is_session_open")),
            "trading_disabled": bool(p.get("trading_disabled")),
            "margin_short_overnight": _f((d.get("overnight_margin_rate") or {})
                                         .get("short_margin_rate")),
            "margin_short_intraday": _f((d.get("intraday_margin_rate") or {})
                                        .get("short_margin_rate")),
        }
    if not out:
        raise SourceError("合约列表里没有 CDE 永续（接口结构变了？）")
    return out


def book(product_id: str, limit: int = 100) -> Book:
    body = request("GET", f"https://{HOST}{PRODUCT_BOOK}", f"Coinbase 订单簿 {product_id}",
                   params={"product_id": product_id, "limit": limit})
    pb = body.get("pricebook") or {}
    return Book(bids=tuple((float(x["price"]), float(x["size"])) for x in pb.get("bids") or []),
                asks=tuple((float(x["price"]), float(x["size"])) for x in pb.get("asks") or []))


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


def positions(key_name: str, key_secret: str) -> list[dict]:
    """[{product, side, contracts}]。key 只需 View 权限。"""
    tok = _token(key_name, key_secret, "GET", POSITIONS)
    body = request("GET", f"https://{HOST}{POSITIONS}", "Coinbase 期货持仓",
                   headers={"Authorization": f"Bearer {tok}"})
    return [{"product": r.get("product_id"), "side": r.get("side"),
             "contracts": _f(r.get("number_of_contracts")) or 0.0}
            for r in body.get("positions") or []]
