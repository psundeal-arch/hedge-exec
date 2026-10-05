"""执行质量：到达价滑点，按标的存、看滚动值。

执行质量的退化是渐进、不可见的 —— 单笔报告里的「比开始时中价贵 x bp」看一次
就忘了，攒成序列才看得出来。每个 exec 任务结束时把下面这些数写进 runs.summary，
这里负责算和汇总。

口径（全部是【成本】bp：正 = 比基准差，负 = 比基准好）：

    arrival_bp    现货均价 vs 开始时现货中价。买：贵了为正；卖：便宜了为正
    fee_bp        两腿手续费 / 现货成交额
    basis_bp      对冲任务：成交基差 vs 开始时基差。两腿同时做，价格涨跌抵消，
                  真正的执行成本是基差 —— 开仓（卖永续买现货）基差变小为正，
                  平仓（买永续卖现货）基差变大为正
    cost_bp       合计：对冲 = basis_bp + fee_bp；纯现货 = arrival_bp + fee_bp

纯现货任务的 arrival_bp 里有一大块是窗口内的价格漂移（方向随机），单笔看不出
执行好坏，要看多笔的均值；对冲任务的 arrival_bp 只作参考，看 basis_bp。
"""
from __future__ import annotations

import datetime as dt
import json

EPS = 1e-9
ROLL_N = 5


def measure(job, contract_size: float = 1.0) -> dict:
    """一个结束了的任务的执行质量。没成交返回 {}。"""
    s = job.state
    if s.spot_qty <= EPS:
        return {}
    sign = 1 if job.buy else -1
    avg = s.spot_cost / s.spot_qty
    out = {"side": "buy" if job.buy else "sell", "notional": s.spot_cost,
           "window_min": round((job.end - job.start) / 60, 1),
           "fill_ratio": s.done / job.total if job.total else None}
    out["fee_bp"] = (s.spot_fees + s.perp_fees) / s.spot_cost * 1e4 if s.spot_cost else None
    if s.arrival:
        out["arrival_bp"] = (avg / s.arrival - 1) * 1e4 * sign
    if job.hedge and s.done:
        pavg = s.perp_cost / (s.done * contract_size)
        out["exec_basis_bp"] = (pavg / avg - 1) * 1e4
        if s.arrival_basis_bp is not None:
            # 开仓：卖永续、买现货，基差越大越好 → 比开始时小了是成本
            out["basis_bp"] = (s.arrival_basis_bp - out["exec_basis_bp"]) * (1 if job.kind == "hedge_open" else -1)
    core = out.get("basis_bp") if job.hedge else out.get("arrival_bp")
    if core is not None and out["fee_bp"] is not None:
        out["cost_bp"] = core + out["fee_bp"]
    return out


def history(db, *, symbol: str | None = None, mode: str = "live", days: float = 90) -> list[dict]:
    """exec 任务的执行质量，时间升序。老任务（summary 里没有这些字段）按均价和到达价补算 arrival_bp。"""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    sql = ("select id, ended, symbol, action, status, mode, summary from runs "
           "where action like 'exec-%' and summary is not null and ended >= ?")
    args: list = [since]
    if symbol:
        sql += " and symbol = ?"
        args.append(symbol.upper())
    if mode != "all":
        sql += " and mode = ?"
        args.append(mode)
    out = []
    for rid, ended, sym, action, status, m, summary in db.execute(sql + " order by ended", args):
        q = json.loads(summary or "{}")
        if not q.get("spot_qty"):
            continue
        kind = action.removeprefix("exec-")
        if "arrival_bp" not in q and q.get("arrival") and q.get("spot_avg"):
            sign = 1 if kind in ("hedge_open", "spot_buy") else -1
            q["arrival_bp"] = (q["spot_avg"] / q["arrival"] - 1) * 1e4 * sign
        q.update(run_id=rid, ended=ended, symbol=sym, kind=kind, status=status, mode=m)
        out.append(q)
    return out


def wavg(rows: list[dict], key: str) -> float | None:
    """按成交额加权的均值。小单的噪音不该和大单同权。"""
    pts = [(r[key], r.get("notional") or 0) for r in rows if r.get(key) is not None]
    w = sum(n for _, n in pts)
    return sum(v * n for v, n in pts) / w if w > 0 else None


def key_of(r: dict) -> str:
    """看哪个数：对冲看合计成本（基差口径），纯现货老数据没有 cost_bp 就看 arrival_bp。"""
    return "cost_bp" if r.get("cost_bp") is not None else "arrival_bp"


def rolling_line(rows: list[dict], n: int = ROLL_N) -> str:
    """'ZEC 实盘 最近 5 笔 +3.1bp，全部 12 笔 +1.4bp（变差 1.7bp）' —— 附在结束通知里。"""
    if not rows:
        return ""
    k = key_of(rows[-1])
    same = [r for r in rows if r.get(k) is not None]
    if not same:
        return ""
    recent, allv = wavg(same[-n:], k), wavg(same, k)
    label = "合计成本" if k == "cost_bp" else "到达价滑点"
    line = f"{label}：最近 {min(n, len(same))} 笔 {recent:+.1f}bp"
    if len(same) > n:
        d = recent - allv
        line += f"，全部 {len(same)} 笔 {allv:+.1f}bp（{'变差' if d > 0 else '变好'} {abs(d):.1f}bp）"
    return line
