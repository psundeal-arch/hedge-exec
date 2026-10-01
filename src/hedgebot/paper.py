"""模拟盘：持仓、成交、资金费都记在 SQLite（data/paper.db）。

记账按【每条腿各自】做均价法：开仓、平仓、回滚都只是往某条腿上加一笔成交。
这样回滚（卖了永续又买回来）和裸腿（回滚也失败）都不用特殊处理 ——
前者自然落成一笔已实现盈亏，后者会在 status 里表现为 delta ≠ 0。

数量带符号：现货多头为正，永续空单张数为负。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .config import Config
from .executor import ExecError, Fill, LegError, PaperVenue, check_slippage, run_pair
from .market import Quote, spot_qty

SCHEMA = """
create table if not exists positions(
    symbol text primary key,
    pair text, product_id text, contract_size real,
    spot_qty real default 0, spot_avg real default 0,
    contracts real default 0, perp_avg real default 0,
    realized_cum real default 0,     -- 价格上的已实现盈亏（不含费用、资金费）
    fees_cum real default 0,
    funding_cum real default 0,      -- 正 = 收到
    opened_at text, funding_from text);
create table if not exists fills(
    id integer primary key, ts text, symbol text, action text,
    venue text, side text, qty real, price real, fee real);
create table if not exists funding(
    id integer primary key, ts text, symbol text, hours integer,
    rate real, notional real, amount real, session_open integer);
create table if not exists closed(
    id integer primary key, symbol text, opened_at text, closed_at text,
    realized real, fees real, funding real, net real);
"""

EPS = 1e-9


def _utc(now: dt.datetime | None = None) -> dt.datetime:
    return (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0)


def _hour_floor(t: dt.datetime) -> dt.datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def apply_fill(qty: float, avg: float, delta: float, px: float,
               mult: float) -> tuple[float, float, float]:
    """均价法记一笔成交。返回 (新数量, 新均价, 已实现盈亏)。

    qty/delta 都带符号。同向加仓摊均价；反向减仓按均价结算盈亏，
    减过头（翻向）的部分以成交价为新均价。
    """
    if abs(qty) < EPS or (qty > 0) == (delta > 0):
        new = qty + delta
        return new, (qty * avg + delta * px) / new, 0.0
    closing = min(abs(delta), abs(qty))
    realized = closing * mult * (px - avg) * (1 if qty > 0 else -1)
    new = qty + delta
    if abs(new) < EPS:
        return 0.0, 0.0, realized
    if (new > 0) != (qty > 0):          # 翻向
        return new, px, realized
    return new, avg, realized


class Paper:
    def __init__(self, cfg: Config, db_path: Path | None = None):
        self.cfg = cfg
        path = db_path or cfg.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------- 查询
    def position(self, symbol: str) -> dict | None:
        r = self.db.execute("select * from positions where symbol=?", (symbol,)).fetchone()
        return dict(r) if r else None

    def positions(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("select * from positions order by symbol")]

    def closed(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("select * from closed order by id")]

    def fills(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "select * from fills order by id desc limit ?", (limit,))][::-1]

    # ------------------------------------------------------------- 记账
    def _record(self, q: Quote, action: str, fills: list[Fill],
                now: dt.datetime) -> None:
        pos = self.position(q.symbol) or {
            "symbol": q.symbol, "pair": q.pair, "product_id": q.product_id,
            "contract_size": q.contract_size, "spot_qty": 0.0, "spot_avg": 0.0,
            "contracts": 0.0, "perp_avg": 0.0, "realized_cum": 0.0,
            "fees_cum": 0.0, "funding_cum": 0.0,
            "opened_at": now.isoformat(), "funding_from": None}
        for f in fills:
            sign = 1 if f.side == "buy" else -1
            if f.venue != "cde":                 # 现货腿：kraken 或 coinbase
                pos["spot_qty"], pos["spot_avg"], r = apply_fill(
                    pos["spot_qty"], pos["spot_avg"], sign * f.qty, f.price, 1.0)
                pos["spot_qty"] = round(pos["spot_qty"], 10)    # 消掉浮点尘埃
            else:
                was_flat = abs(pos["contracts"]) < EPS
                pos["contracts"], pos["perp_avg"], r = apply_fill(
                    pos["contracts"], pos["perp_avg"], sign * f.qty, f.price,
                    q.contract_size)
                # 资金费从永续开出来的那个整点算起
                if was_flat and abs(pos["contracts"]) > EPS:
                    pos["funding_from"] = _hour_floor(now).isoformat()
            pos["realized_cum"] += r
            pos["fees_cum"] += f.fee
            self.db.execute(
                "insert into fills(ts,symbol,action,venue,side,qty,price,fee)"
                " values(?,?,?,?,?,?,?,?)",
                (f.ts, q.symbol, action, f.venue, f.side, f.qty, f.price, f.fee))

        if abs(pos["spot_qty"]) < EPS and abs(pos["contracts"]) < EPS:
            net = pos["realized_cum"] - pos["fees_cum"] + pos["funding_cum"]
            self.db.execute(
                "insert into closed(symbol,opened_at,closed_at,realized,fees,funding,net)"
                " values(?,?,?,?,?,?,?)",
                (q.symbol, pos["opened_at"], now.isoformat(), pos["realized_cum"],
                 pos["fees_cum"], pos["funding_cum"], net))
            self.db.execute("delete from positions where symbol=?", (q.symbol,))
        else:
            cols = list(pos)
            self.db.execute(
                f"insert or replace into positions({','.join(cols)})"
                f" values({','.join('?' * len(cols))})", [pos[c] for c in cols])
        self.db.commit()

    def accrue(self, q: Quote, now: dt.datetime | None = None) -> float:
        """把上次结算以来每个整点的资金费记上。返回本次记入的金额。

        近似：错过的几个小时都按【当前】费率算 —— 所以要每小时跑一次 tick，
        跑得越勤越准。闭市整点 CDE 不发布费率，此时按 0 记。
        """
        now = _utc(now)
        pos = self.position(q.symbol)
        if not pos or abs(pos["contracts"]) < EPS or not pos["funding_from"]:
            return 0.0
        start = dt.datetime.fromisoformat(pos["funding_from"])
        hours = int((now - start).total_seconds() // 3600)
        if hours < 1:
            return 0.0
        rate = q.funding_rate if (q.session_open and q.funding_rate is not None) else 0.0
        px = q.index_price or q.perp.mid
        notional = pos["contracts"] * q.contract_size * px       # 空单为负
        # 费率为正时多付空收：空单 notional 为负，所以前面加负号
        amount = -rate * notional * hours * 3600 / q.interval_s
        self.db.execute(
            "update positions set funding_cum=funding_cum+?, funding_from=? where symbol=?",
            (amount, (start + dt.timedelta(hours=hours)).isoformat(), q.symbol))
        self.db.execute(
            "insert into funding(ts,symbol,hours,rate,notional,amount,session_open)"
            " values(?,?,?,?,?,?,?)",
            (now.isoformat(), q.symbol, hours, rate, abs(notional), amount,
             int(q.session_open)))
        self.db.commit()
        return amount

    # ------------------------------------------------------------- 交易
    def _venues(self, q: Quote) -> tuple[PaperVenue, PaperVenue]:
        f = self.cfg.fees
        spot = PaperVenue("kraken", q.spot, f.kraken_taker)
        perp = PaperVenue("cde", q.perp, f.cde_taker, q.contract_size,
                          available=q.session_open, reason="CDE 闭市或停牌",
                          per_unit_fee=f.cde_per_contract)
        return spot, perp

    def _execute(self, q: Quote, action: str, perp_side: str, contracts: int,
                 spot_side: str, qty: float, now: dt.datetime) -> tuple[Fill, Fill]:
        if not q.session_open:
            raise ExecError("CDE 现在闭市或停牌，不下单")
        mx = self.cfg.max_slippage_bps
        check_slippage(q.perp, perp_side, contracts, mx, f"CDE {q.product_id}")
        check_slippage(q.spot, spot_side, qty, mx, f"Kraken {q.pair}")
        spot, perp = self._venues(q)
        try:
            fills = run_pair(perp, perp_side, contracts, spot, spot_side, qty)
        except LegError as e:
            self._record(q, f"{action}-rollback", e.fills, now)
            raise
        self._record(q, action, list(fills), now)
        return fills

    def open(self, q: Quote, contracts: int,
             now: dt.datetime | None = None) -> tuple[Fill, Fill]:
        """卖 contracts 张永续 + 买等量现货。已有仓位就加仓。"""
        now = _utc(now)
        if contracts < 1:
            raise ExecError("至少一张合约")
        qty = spot_qty(q, contracts)
        if qty < q.ordermin:
            raise ExecError(f"现货 {qty} 低于 Kraken 最小下单量 {q.ordermin}")
        self.accrue(q, now)          # 先把旧仓位的资金费结清，再改仓位大小
        return self._execute(q, "open", "sell", contracts, "buy", qty, now)

    def close(self, q: Quote, pct: float = 100,
              now: dt.datetime | None = None) -> tuple[Fill, Fill]:
        """买平 pct% 的空单 + 卖出等量现货。100 = 全平（两腿都清零）。"""
        now = _utc(now)
        pos = self.position(q.symbol)
        if not pos:
            raise ExecError(f"{q.symbol} 没有模拟仓位")
        if pos["contracts"] > -EPS or pos["spot_qty"] < EPS:
            raise ExecError(f"{q.symbol} 不是「现货多 + 永续空」的形态，"
                            f"请先看 status（可能是回滚失败留下的裸腿）")
        held = round(-pos["contracts"])
        if pct >= 100:
            contracts, qty = held, pos["spot_qty"]
        else:
            contracts = round(held * pct / 100)
            if contracts < 1:
                raise ExecError(f"{pct}% 不足一张（共 {held} 张）")
            qty = min(spot_qty(q, contracts), pos["spot_qty"])
        self.accrue(q, now)
        return self._execute(q, "close", "buy", contracts, "sell", qty, now)

    # ------------------------------------------------------------- 估值
    @staticmethod
    def mark(pos: dict, q: Quote, now: dt.datetime | None = None) -> dict:
        """按【能立即平掉】的价格估值：现货看买一侧，空单看卖一侧。"""
        cs = pos["contract_size"]
        spot_px = q.spot.best_bid if pos["spot_qty"] > 0 else q.spot.best_ask
        perp_px = q.perp.best_ask if pos["contracts"] < 0 else q.perp.best_bid
        spot_px = spot_px or q.spot.mid or 0.0
        perp_px = perp_px or q.perp.mid or q.index_price or 0.0
        unreal_spot = pos["spot_qty"] * (spot_px - pos["spot_avg"])
        unreal_perp = pos["contracts"] * cs * (perp_px - pos["perp_avg"])
        net = (pos["realized_cum"] + unreal_spot + unreal_perp
               - pos["fees_cum"] + pos["funding_cum"])
        capital = (abs(pos["spot_qty"]) * pos["spot_avg"]
                   + abs(pos["contracts"]) * cs * pos["perp_avg"] * (q.margin_short or 0))
        days = (_utc(now) - dt.datetime.fromisoformat(pos["opened_at"])).total_seconds() / 86400
        return {
            "delta_qty": pos["spot_qty"] + pos["contracts"] * cs,
            "delta_usd": (pos["spot_qty"] + pos["contracts"] * cs) * (q.spot.mid or 0),
            "unreal_spot": unreal_spot, "unreal_perp": unreal_perp,
            "net": net, "capital": capital, "days": days,
            "apy": (net / capital * 365 / days) if capital and days >= 1 / 24 else None,
        }
