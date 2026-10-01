"""Robinhood 加密货币：只读持仓（Crypto Trading API）。不下单。

鉴权（官方文档 + 社区实测）：
- 三个头：x-api-key / x-timestamp / x-signature
- 签名原文 = api_key + timestamp + path + method + body，无分隔符；
  path 含查询串和结尾的斜杠；GET 没有 body 就不拼
- timestamp 是 Unix【秒】，30 秒内有效 —— 本机时钟偏了会 401
- 私钥是 32 字节 Ed25519 种子（base64）。有的库导出 64 字节（种子+公钥），
  只取前 32
"""
from __future__ import annotations

import base64
import time
import urllib.parse

from .http import SourceError, request

BASE = "https://trading.robinhood.com"
HOLDINGS = "/api/v1/crypto/trading/holdings/"


def _key(secret_b64: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = base64.b64decode(secret_b64)
    if len(raw) not in (32, 64):
        raise SourceError(f"RH_PRIVATE_KEY 解码后 {len(raw)} 字节，应为 32（或 64 = 种子+公钥）")
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


def sign(api_key: str, secret_b64: str, ts: int, path: str, method: str,
         body: str = "") -> str:
    msg = f"{api_key}{ts}{path}{method}{body}"
    return base64.b64encode(_key(secret_b64).sign(msg.encode())).decode()


def keygen() -> tuple[str, str]:
    """生成一对新密钥，返回 (私钥 base64, 公钥 base64)。公钥贴到 Robinhood 网页上。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                           serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def holdings(api_key: str, secret_b64: str) -> dict[str, float]:
    """标的 -> 总数量。key 只需 Read crypto holdings 权限。

    total_quantity 含挂单冻结的部分 —— 对冲看的是价格风险，冻结的也算。
    """
    out: dict[str, float] = {}
    path = HOLDINGS
    for _ in range(50):                       # 分页上限，防止 next 死循环
        ts = int(time.time())
        headers = {"x-api-key": api_key, "x-timestamp": str(ts),
                   "x-signature": sign(api_key, secret_b64, ts, path, "GET")}
        # attempts=1：时间戳 30 秒过期，重试要重新签名，不如直接报错
        body = request("GET", BASE + path, "Robinhood 持仓", attempts=1, headers=headers)
        for r in body.get("results") or []:
            code = (r.get("asset_code") or "").upper()
            qty = float(r.get("total_quantity") or 0)
            if code and qty:
                out[code] = out.get(code, 0.0) + qty
        nxt = body.get("next")
        if not nxt:
            return out
        u = urllib.parse.urlsplit(nxt)
        path = u.path + (f"?{u.query}" if u.query else "")
    raise SourceError("Robinhood 持仓分页超过 50 页，next 可能在死循环")
