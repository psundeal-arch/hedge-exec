"""hedge exec 的日内参考数据：当日 VWAP、1 小时 ATR、永续 − 现货基差（全部公开接口，免鉴权）。

- 现货 5 分钟线：Coinbase Exchange `products/{SYM}-USD/candles`（Robinhood 标的也用它作参考）
- 永续 5 分钟线：Coinbase Advanced `market/products/{pid}/candles`
VWAP 从 UTC 0 点起算；0 点后不足 2 小时就用最近 24 小时，免得刚过 0 点只有几根线。
"""
from __future__ import annotations

import statistics as st
import time
from dataclasses import dataclass

from .http import request

SPOT_CANDLES = "https://api.exchange.coinbase.com/products/{pid}/candles"
PERP_CANDLES = "https://api.coinbase.com/api/v3/brokerage/market/products/{pid}/candles"


def spot_candles(product_id: str, start: int, end: int) -> list[tuple]:
    """[(ts, open, high, low, close, volume)]，5 分钟，按时间排序。单次最多 300 根，自动分页。"""
    out = {}
    s = start
    while s < end:
        e = min(end, s + 300 * 300)
        rows = request("GET", SPOT_CANDLES.format(pid=product_id), f"Coinbase K 线 {product_id}",
                       params={"granularity": 300, "start": s, "end": e})
        for r in rows:                      # [time, low, high, open, close, volume]
            out[int(r[0])] = (int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5]))
        s = e
    return [out[k] for k in sorted(out)]


def perp_closes(product_id: str, start: int, end: int) -> dict[int, float]:
    out = {}
    s = start
    while s < end:
        e = min(end, s + 300 * 300)
        body = request("GET", PERP_CANDLES.format(pid=product_id), f"CDE K 线 {product_id}",
                       params={"granularity": "FIVE_MINUTE", "start": s, "end": e})
        for r in body.get("candles") or []:
            out[int(r["start"])] = float(r["close"])
        s = e
    return out


def vwap(rows: list[tuple]) -> float | None:
    v = sum(r[5] for r in rows)
    if v <= 0:
        return None
    return sum((r[2] + r[3] + r[4]) / 3 * r[5] for r in rows) / v


def atr_pct(rows: list[tuple]) -> float | None:
    """1 小时振幅（高 − 低）/ 开 的均值，%（用 5 分钟线按整点聚合）。"""
    hours: dict[int, list[tuple]] = {}
    for r in rows:
        hours.setdefault(r[0] // 3600, []).append(r)
    rng = [(max(x[2] for x in h) - min(x[3] for x in h)) / h[0][1] * 100 for h in hours.values() if len(h) >= 6]
    return st.mean(rng) if rng else None


@dataclass
class Ref:
    ts: float
    vwap: float | None          # 当日成交量加权均价
    atr_pct: float | None       # 1 小时平均振幅 %
    basis_med_bp: float | None  # 近 24 小时 永续/现货 − 1 的中位数（基点）；纯现货任务为 None


def reference(spot_pid: str, perp_pid: str | None = None, now: float | None = None) -> Ref:
    now = now or time.time()
    end = int(now) // 300 * 300
    day0 = end // 86400 * 86400
    start = day0 if end - day0 >= 7200 else end - 86400
    rows24 = spot_candles(spot_pid, end - 86400, end)
    today = [r for r in rows24 if r[0] >= start]
    med = None
    if perp_pid:
        pc = perp_closes(perp_pid, end - 86400, end)
        bs = [(pc[r[0]] / r[4] - 1) * 1e4 for r in rows24 if r[0] in pc and r[4] > 0]
        med = st.median(bs) if bs else None
        if med is not None and abs(med) >= 1000:      # 单位不一致（千枚报价等），不可比
            med = None
    return Ref(ts=now, vwap=vwap(today), atr_pct=atr_pct(rows24), basis_med_bp=med)
