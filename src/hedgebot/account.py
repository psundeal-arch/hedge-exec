"""实盘只读：现货（Kraken + Robinhood + Coinbase）vs CDE 空单，按标的对齐。"""
from __future__ import annotations

import os

from . import coinbase, kraken, robinhood
from .config import Config
from .http import SourceError

STABLES = ("USD", "USDC", "USDT", "USDG", "EUR")


def hedge_table(perps: dict[str, dict]) -> tuple[list[dict], list[str], list[str]]:
    """返回 (逐标的对齐行, 提示, 错误)。

    错误 = 某个来源没读到（取数失败或缺凭据）。【有错误时对齐结果不可信】：
    比如 Robinhood 没读到，BTC 现货就成了 0，看起来像裸空 —— 调用方
    （尤其 watch）必须先看 errors，非空就别拿 rows 下结论。

    spot 的构成按「场所」分：Kraken、Kraken.B（锁定质押）、Kraken.F、Robinhood…
    Kraken 里质押中的币也计入现货多头 —— 价格风险一样，
    只是解押要等，平仓时得按 README 的顺序来。
    """
    notes: list[str] = []
    errors: list[str] = []
    spot: dict[str, dict[str, float]] = {}
    shorts: dict[str, float] = {}

    kk, ks = os.environ.get("KRAKEN_API_KEY"), os.environ.get("KRAKEN_API_SECRET")
    if kk and ks:
        try:
            for asset, parts in kraken.balances(kk, ks).items():
                for suffix, amt in parts.items():
                    label = f"Kraken.{suffix}" if suffix else "Kraken"
                    spot.setdefault(asset, {})[label] = amt
        except SourceError as e:
            errors.append(f"Kraken 余额取数失败：{e}")
    else:
        errors.append("没有 KRAKEN_API_KEY / KRAKEN_API_SECRET（只需 Query Funds 权限）")

    rk, rs = os.environ.get("RH_API_KEY"), os.environ.get("RH_PRIVATE_KEY")
    if rk and rs:
        try:
            for asset, amt in robinhood.holdings(rk, rs).items():
                spot.setdefault(asset, {})["Robinhood"] = amt
        except SourceError as e:
            errors.append(f"Robinhood 持仓取数失败：{e}")
    else:
        errors.append("没有 RH_API_KEY / RH_PRIVATE_KEY（hedge rh-keygen 生成）")

    cn, cs = os.environ.get("COINBASE_API_KEY_NAME"), os.environ.get("COINBASE_API_PRIVATE_KEY")
    if cn and cs:
        # Coinbase 现货：hedge live --spot coinbase 买的币在这里
        try:
            for asset, amt in coinbase.spot_balances(cn, cs).items():
                # 小于 1e-6 的是历史尘埃（实测 ETH 5e-9），不让它出现在表里
                if amt > 1e-6:
                    spot.setdefault(asset, {})["Coinbase"] = amt
        except SourceError as e:
            errors.append(f"Coinbase 现货余额取数失败：{e}")
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
            errors.append(f"Coinbase 持仓取数失败：{e}")
    else:
        errors.append("没有 COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY（只需 View 权限）")

    rows = []
    for asset in sorted(set(spot) | set(shorts)):
        if asset in STABLES:
            continue
        parts = spot.get(asset, {})
        long_qty = sum(parts.values())
        perp_qty = shorts.get(asset, 0.0)       # 空单为负
        rows.append({"asset": asset, "spot": long_qty, "spot_parts": parts,
                     "perp": perp_qty, "delta": long_qty + perp_qty,
                     "price": perps.get(asset, {}).get("index_price"),
                     "contract_size": perps.get(asset, {}).get("contract_size")})
    return rows, notes, errors


def adjust_hint(delta: float, cs: float | None) -> str:
    """把净 delta 调到最接近 0 需要再空/平几张。只能整张，所以会剩零头。"""
    if not cs:
        return "CDE 无此永续"
    n = round(delta / cs)
    if n == 0:
        return ""
    rest = delta - n * cs
    return f"{'再空' if n > 0 else '平'} {abs(n)} 张 → 余 {rest:+.6g}"


def classify(rows: list[dict], cfg: Config) -> list[dict]:
    """给每行加 flag / problem / mismatch / delta_usd / hint。account 与 watch 共用。

    problem = 标了 ⚠ 且净敞口不小于 min_alert_usd —— 只差几美元的零头不值得传呼。
    """
    out = []
    for r in rows:
        px = r["price"]
        mismatch = abs(r["delta"]) / r["spot"] if r["spot"] > 0 else None
        off = mismatch is None or mismatch > cfg.hedge_tolerance
        if r["asset"] in cfg.grid_only:
            # perp-grid 实盘网格在 CDE 上独立做这个标的：永续仓位是网格的，不和现货对齐
            out.append({**r, "flag": "网格实盘（不对齐）", "problem": False, "mismatch": None,
                        "delta_usd": None, "hint": ""})
            continue
        held = r["asset"] in cfg.hold_only
        if held and r["perp"] == 0:
            flag = "有意持有"
        elif held and not (r["delta"] < 0 and off):
            # 名单内且净多：多出的现货是有意的
            flag = "✓ 余量有意持有"
        elif held:
            flag = "⚠ 空单多于现货"
        elif r["perp"] == 0:
            # 只有现货、没开空单，又不在名单里：列出来但不标红
            flag = "仅现货"
        elif off:
            flag = "⚠ 没对齐"
        else:
            flag = "✓"
        hint = (adjust_hint(r["delta"], r["contract_size"])
                if not held or flag.startswith("⚠") else "")
        delta_usd = r["delta"] * px if px else None
        problem = flag.startswith("⚠") and (
            delta_usd is None or abs(delta_usd) >= cfg.min_alert_usd)
        out.append({**r, "flag": flag, "problem": problem, "mismatch": mismatch,
                    "delta_usd": delta_usd, "hint": hint})
    return out
