"""批量体检：哪些标的适合做「Coinbase 现货 + CDE 空单」。

static_scan：一次看完价差、深度、每张金额、资金费均值、保证金率、一轮成本、回本天数。
paper_sweep：逐个用模拟盘真实跑一遍开 + 平（同一套 Ladder 引擎），量执行质量。
    必须串行 —— 并行跑几十个标的会撞 Coinbase 公开接口的限频。

资金费均值来自 perp-track 的数据库（每小时采样）。CDE 没有公开的费率历史接口，
单点读数又受 1e-6 量化噪声影响（低费率标的相对误差 25~30%），所以不拿单点下结论。
"""
from __future__ import annotations

import datetime as dt
import math
import sqlite3
import time
from pathlib import Path

from . import coinbase
from .config import Config
from .http import SourceError


def funding_avgs(db_path: Path | None) -> dict[str, dict[str, float]]:
    """product -> {'24h': 年化%, '7d': 年化%, 'n24': 样本数}。只算开市的样本。"""
    if not db_path or not Path(db_path).exists():
        return {}
    now = dt.datetime.now(dt.timezone.utc)
    rows = sqlite3.connect(db_path).execute(
        "select product, ts, apr from funding_rates where session_open=1").fetchall()
    acc: dict[str, dict[str, list]] = {}
    for prod, ts, apr in rows:
        age = (now - dt.datetime.fromisoformat(ts)).total_seconds() / 3600
        a = acc.setdefault(prod, {"24h": [], "7d": []})
        if age <= 24:
            a["24h"].append(apr)
        if age <= 24 * 7:
            a["7d"].append(apr)
    return {p: {"24h": sum(v["24h"]) / len(v["24h"]) if v["24h"] else None,
                "7d": sum(v["7d"]) / len(v["7d"]) if v["7d"] else None,
                "n24": len(v["24h"])} for p, v in acc.items()}


def walk_bp(levels, usd: float, mid: float, cs: float = 1.0) -> float | None:
    """吃掉 usd 美元要付多少：成交均价 vs 中价，bp（正 = 吃亏）。簿子不够吃完返回 None。

    levels 是同一侧从优到劣的 (价格, 数量)，数量 × cs = 币数（CDE 的数量是张）。"""
    got = cost = 0.0
    for px, sz in levels:
        take = min(sz * cs * px, usd - cost)
        got += take / px
        cost += take
        if cost >= usd - 1e-6:
            return abs(cost / got / mid - 1) * 1e4
    return None


def static_scan(cfg: Config, symbols: list[str], funding_db: Path | None,
                exit_usd: float | None = None) -> list[dict]:
    """exit_usd：容量按多大的【出场】算（默认 config [live] exit_usd）。

    进场时「30bp 内深度」只回答能不能进；真正的约束是出场：急着平 $25k 时
    自己要在 CDE 卖盘上吃掉多少（买回空单 → 吃 asks），以及这笔占两边 24h 成交额多少。
    现货腿平仓是挂单卖，不吃簿子，但完不成时要吃 bids，一并算给参考。"""
    from .live import fixed_fee_bp
    exit_usd = exit_usd or cfg.live.exit_usd
    perps = coinbase.cde_perps()
    avgs = funding_avgs(funding_db)
    f = cfg.fees
    out = []
    for s in symbols:
        p = perps.get(s)
        row: dict = {"symbol": s, "flags": []}
        out.append(row)
        if not p:
            row["flags"].append("CDE 无永续")
            continue
        try:
            sb = coinbase.book(f"{s}-USD", limit=250)
            pb = coinbase.book(p["product_id"], limit=250, unit=p["unit"])
            spot_vol = coinbase.spot_volume_usd(f"{s}-USD")
        except SourceError as e:
            row["flags"].append(f"取数失败 {e}")
            continue
        time.sleep(0.15)                              # 公开接口 10 次/秒，留余量
        if not (sb.mid and pb.mid):
            row["flags"].append("订单簿空")
            continue
        cs = p["contract_size"]
        per_contract = cs * pb.mid
        clip_n = max(1, math.floor(cfg.live.clip_usd / per_contract))
        notional = clip_n * per_contract
        a = avgs.get(p["product_id"], {})
        apr = a.get("24h")
        apr = apr if apr is not None else (p["funding_rate"] or 0) * 8760 * 100
        # 一轮（开 + 平）成本，按一块的规模：现货两次挂单 + CDE 两次吃单（含每张固定费）
        # + CDE 吃单越过半个价差两次。现货是挂单，不付价差
        perp_half_spread = (pb.best_ask - pb.best_bid) / 2 / pb.mid
        cost_pct = (2 * f.coinbase_maker + 2 * f.cde_taker
                    + 2 * f.cde_per_contract / per_contract + 2 * perp_half_spread) * 100
        margin = p["margin_short_overnight"] or 0
        row.update({
            "product": p["product_id"], "unit": p["unit"], "per_contract": per_contract,
            "clip_n": clip_n,
            "spot_spread_bp": (sb.best_ask - sb.best_bid) / sb.mid * 1e4,
            "perp_spread_bp": (pb.best_ask - pb.best_bid) / pb.mid * 1e4,
            "perp_depth_usd": sum(px * sz * cs for px, sz in pb.bids
                                  if px >= pb.best_bid * (1 - 0.003)),
            "spot_depth_usd": sum(px * sz for px, sz in sb.asks
                                  if px <= sb.best_ask * (1 + 0.003)),
            "basis_bp": (pb.mid / sb.mid - 1) * 1e4,
            "apr_now": (p["funding_rate"] or 0) * 8760 * 100,
            "apr_24h": a.get("24h"), "apr_7d": a.get("7d"), "n24": a.get("n24", 0),
            "margin": margin,
            "oi_usd": (p["open_interest"] or 0) * per_contract,
            "cost_pct": cost_pct,
            "breakeven_d": cost_pct / (apr / 365) if apr > 0 else None,
            "capital_apy": apr / (1 + margin),
            "fixed_fee_bp": fixed_fee_bp(cfg, per_contract),
            "exit_usd": exit_usd,
            "exit_impact_bp": walk_bp(pb.asks, exit_usd, pb.mid, cs),       # 买回空单
            "spot_exit_impact_bp": walk_bp(sb.bids, exit_usd, sb.mid),      # 现货挂单卖不完时
            "perp_adv_share": exit_usd / p["volume_24h_usd"] if p.get("volume_24h_usd") else None,
            "spot_adv_share": exit_usd / spot_vol if spot_vol else None,
        })
        lv = cfg.live
        if row["fixed_fee_bp"] > lv.max_fixed_fee_bp:
            row["flags"].append(f"固定费 {row['fixed_fee_bp']:.1f}bp/边 超上限，不开")
        k = f"${exit_usd / 1000:g}K"
        if row["exit_impact_bp"] is None:
            row["flags"].append(f"CDE 卖盘吃不完 {k} 出场")
        elif row["exit_impact_bp"] > lv.max_exit_impact_bp:
            row["flags"].append(f"{k} 出场冲击 {row['exit_impact_bp']:.0f}bp")
        for leg, key in (("CDE", "perp_adv_share"), ("现货", "spot_adv_share")):
            if row[key] is None:
                row["flags"].append(f"{leg} 无 24h 成交额")
            elif row[key] > lv.max_adv_share:
                row["flags"].append(f"{k} 占{leg}日成交 {row[key]:.1%}")
        if abs(row["basis_bp"]) > 100:
            row["flags"].append("基差 >1%（单位不对？）")
        if row["oi_usd"] < 1e6:
            row["flags"].append("OI<$1M")
        if row["perp_depth_usd"] < notional:
            row["flags"].append("CDE 30bp 内深度不够一块")
        if row["spot_spread_bp"] > 20:
            row["flags"].append("现货价差宽")
        if apr <= 0:
            row["flags"].append("资金费≤0")
        if per_contract > cfg.live.clip_usd:
            row["flags"].append("一张就超过一块")
        if not p["session_open"]:
            row["flags"].append("闭市")
    return out


def paper_sweep(cfg: Config, symbols: list[str], *, max_wait: float, say=print) -> list[dict]:
    """逐个标的：模拟开一块 → 立刻模拟平掉。量的是执行质量，不是收益。"""
    from .ladder import Ladder, Params
    from .live import build

    out = []
    lv = cfg.live
    for s in symbols:
        row = {"symbol": s}
        out.append(row)
        try:
            p, spot, perp, *_ = build(cfg, s, "coinbase", need_trade=False, paper=True)
            cs = p["contract_size"]
            n = max(1, math.floor(lv.clip_usd / (cs * perp.book().mid)))
            row["contracts"] = n
            for action in ("open", "close"):
                t0 = time.monotonic()
                ld = Ladder(spot, perp, action=action, contracts=n, contract_size=cs,
                            params=Params(poll_s=lv.poll_s, reprice_min_s=lv.reprice_min_s,
                                          max_wait_s=max_wait, clip_usd=lv.clip_usd,
                                          hedge_escalation_bps=lv.hedge_escalation_bps),
                            say=lambda m: None)
                r = ld.run()
                perp_avg = r.perp_cost / (r.contracts * cs) if r.contracts else None
                row[action] = {
                    "status": r.status, "secs": time.monotonic() - t0,
                    "orders": r.spot_orders,
                    "basis_bp": (perp_avg / r.spot_avg - 1) * 1e4 if perp_avg and r.spot_avg else None,
                    "fees": r.spot_fees + r.perp_fees,
                    "notional": r.spot_qty * (r.spot_avg or 0),
                    "max_unhedged_usd": r.max_unhedged * (r.spot_avg or 0),
                    "taker_finish": any("超时补齐" in m for m in r.log),
                }
                if r.status not in ("done",):
                    break
            o, c = row.get("open"), row.get("close")
            if o and c and o["basis_bp"] is not None and c["basis_bp"] is not None:
                # 开仓卖永续买现货、平仓反过来：基差收窄是赚，扩大是亏
                row["basis_pnl_bp"] = o["basis_bp"] - c["basis_bp"]
                row["cost_bp"] = (o["fees"] + c["fees"]) / o["notional"] * 1e4 - row["basis_pnl_bp"]
            say(f"  {s}: 开 {o['status'] if o else '—'} / 平 {c['status'] if c else '—'}")
        except Exception as e:      # noqa: BLE001 —— 一个标的出错不影响其它
            row["error"] = f"{type(e).__name__}: {e}"
            say(f"  {s}: ✗ {row['error']}")
    return out


def cde_funding_table(db_path: Path | None) -> tuple[list[dict], dict]:
    """CDE 全部永续的资金费：当前、24h、7d 均值 + OI、保证金率。只含美国能交易的市场。

    返回 (行, 元信息)。元信息里有样本的时间跨度，供输出提示「7d 其实只有几天」。
    """
    perps = coinbase.cde_perps()
    avgs = funding_avgs(db_path)
    span = None
    if db_path and Path(db_path).exists():
        lo, hi = sqlite3.connect(db_path).execute(
            "select min(ts), max(ts) from funding_rates").fetchone()
        if lo and hi:
            span = (dt.datetime.fromisoformat(lo), dt.datetime.fromisoformat(hi))
    rows = []
    for root, p in perps.items():
        a = avgs.get(p["product_id"], {})
        cur = (p["funding_rate"] or 0) * 8760 * 100 if p["funding_rate"] is not None else None
        per_contract = p["contract_size"] * (p["index_price"] or 0)
        rows.append({"symbol": root, "product": p["product_id"], "apr_now": cur,
                     "apr_24h": a.get("24h"), "apr_7d": a.get("7d"), "n24": a.get("n24", 0),
                     "oi_usd": (p["open_interest"] or 0) * per_contract,
                     "per_contract": per_contract,
                     "margin_short": p["margin_short_overnight"],
                     "session_open": p["session_open"]})
    return rows, {"span": span}
