"""实盘只读：Kraken 现货余额 vs CDE 空单，按标的对齐。"""
from __future__ import annotations

import os

from . import coinbase, kraken
from .http import SourceError


def hedge_table(perps: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """返回 (逐标的对齐行, 提示)。缺哪边凭据就跳过哪边，并在提示里说清楚。

    Kraken 里质押中的币（.B/.F/.S）也计入现货多头 —— 价格风险一样，
    只是解押要等，平仓时得按 README 的顺序来。
    """
    notes: list[str] = []
    spot: dict[str, dict[str, float]] = {}
    shorts: dict[str, float] = {}

    kk, ks = os.environ.get("KRAKEN_API_KEY"), os.environ.get("KRAKEN_API_SECRET")
    if kk and ks:
        try:
            spot = kraken.balances(kk, ks)
        except SourceError as e:
            notes.append(f"Kraken 余额取数失败：{e}")
    else:
        notes.append("没有 KRAKEN_API_KEY / KRAKEN_API_SECRET，跳过 Kraken（只需 Query Funds 权限）")

    cn, cs = os.environ.get("COINBASE_API_KEY_NAME"), os.environ.get("COINBASE_API_PRIVATE_KEY")
    if cn and cs:
        by_product = {p["product_id"]: (root, p["contract_size"]) for root, p in perps.items()}
        try:
            for r in coinbase.positions(cn, cs):
                root, size = by_product.get(r["product"], (None, None))
                if root is None:
                    notes.append(f"CDE 持仓 {r['product']} 不在永续列表里（交割合约？），未计入")
                    continue
                sign = -1 if r["side"] == "SHORT" else 1
                shorts[root] = shorts.get(root, 0.0) + sign * r["contracts"] * size
        except SourceError as e:
            notes.append(f"Coinbase 持仓取数失败：{e}")
    else:
        notes.append("没有 COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY，跳过 CDE（只需 View 权限）")

    rows = []
    for asset in sorted(set(spot) | set(shorts)):
        if asset in ("USD", "USDC", "USDT", "USDG", "EUR"):
            continue
        parts = spot.get(asset, {})
        long_qty = sum(parts.values())
        perp_qty = shorts.get(asset, 0.0)       # 空单为负
        rows.append({"asset": asset, "spot": long_qty, "spot_parts": parts,
                     "perp": perp_qty, "delta": long_qty + perp_qty,
                     "price": perps.get(asset, {}).get("index_price")})
    return rows, notes
