"""Kraken 现货：公开行情 + 只读余额。不下单。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

from .book import Book
from .http import SourceError, request

API = "https://api.kraken.com"

# Kraken 的老资产代码带 X/Z 前缀，余额接口里还会挂 .B/.F/.S 等后缀
# （锁定质押 / 灵活质押 / 老式质押）。质押中的币也是现货多头，对冲时要算进去。
ASSET_ALIASES = {
    "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "ETH2": "ETH",
    "XXDG": "DOGE", "XDG": "DOGE", "XXRP": "XRP", "XLTC": "LTC",
    "XXLM": "XLM", "XETC": "ETC", "XZEC": "ZEC", "XXMR": "XMR", "ZUSD": "USD",
}


def _result(body: dict, label: str):
    if body.get("error"):
        raise SourceError(f"{label}: {body['error']}")
    return body["result"]


def depth(pair: str, count: int = 100) -> Book:
    res = _result(request("GET", f"{API}/0/public/Depth", f"Kraken 深度 {pair}",
                          params={"pair": pair, "count": count}), f"Kraken 深度 {pair}")
    # 返回的键是内部名（XBTUSD -> XXBTZUSD），只有一个，直接取
    v = next(iter(res.values()))
    return Book(bids=tuple((float(p), float(s)) for p, s, *_ in v["bids"]),
                asks=tuple((float(p), float(s)) for p, s, *_ in v["asks"]))


def pair_info(pairs: list[str]) -> dict[str, dict]:
    """altname -> 下单约束。一个请求查全部。"""
    res = _result(request("GET", f"{API}/0/public/AssetPairs", "Kraken 交易对",
                          params={"pair": ",".join(pairs)}), "Kraken 交易对")
    return {v["altname"]: {"ordermin": float(v["ordermin"]),
                           "lot_decimals": int(v["lot_decimals"]),
                           "base": v["base"]}
            for v in res.values()}


def normalize_asset(code: str) -> tuple[str, str]:
    """'XXBT' -> ('BTC', '')；'AVAX.B' -> ('AVAX', 'B')。"""
    base, _, suffix = code.partition(".")
    return ASSET_ALIASES.get(base, base), suffix


def _sign(path: str, data: dict, secret: str) -> str:
    post = urllib.parse.urlencode(data)
    msg = path.encode() + hashlib.sha256((str(data["nonce"]) + post).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), msg, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def balances(key: str, secret: str) -> dict[str, dict[str, float]]:
    """标的 -> {后缀: 数量}。后缀 '' 是可交易现货，'B' 锁定质押，'F' 灵活质押…

    key 只需 Query Funds 权限。
    """
    path = "/0/private/Balance"
    data = {"nonce": int(time.time() * 1000)}
    headers = {"API-Key": key, "API-Sign": _sign(path, data, secret)}
    res = _result(request("POST", API + path, "Kraken 余额", attempts=1,
                          data=data, headers=headers), "Kraken 余额")
    out: dict[str, dict[str, float]] = {}
    for code, amt in res.items():
        a = float(amt)
        if a == 0:
            continue
        base, suffix = normalize_asset(code)
        out.setdefault(base, {})
        out[base][suffix] = out[base].get(suffix, 0.0) + a
    return out
