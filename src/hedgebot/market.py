"""把两腿行情拼成一个 Quote，并估算开平一轮的成本与回本时间。"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import coinbase, kraken
from .book import Book, walk
from .config import Config, Fees


@dataclass(frozen=True)
class Quote:
    symbol: str
    pair: str                 # Kraken 现货交易对
    product_id: str           # CDE 永续
    contract_size: float      # 每张多少币
    spot: Book
    perp: Book                # 数量单位：张
    funding_rate: float | None   # 每期（每小时）费率；正 = 多付空收
    interval_s: float
    index_price: float | None
    session_open: bool
    margin_short: float | None   # 隔夜空单保证金率
    open_interest: float
    ordermin: float           # Kraken 最小下单量（币）
    lot_decimals: int

    @property
    def basis(self) -> float | None:
        """永续相对现货的溢价。正 = 永续贵（开仓时卖得贵，有利）。"""
        if not (self.spot.mid and self.perp.mid):
            return None
        return self.perp.mid / self.spot.mid - 1

    @property
    def funding_apr(self) -> float | None:
        if self.funding_rate is None:
            return None
        return self.funding_rate * 365 * 86400 / self.interval_s


def fetch_quotes(cfg: Config, symbols: list[str]) -> tuple[list[Quote], list[str]]:
    """返回 (行情, 跳过的原因)。单个标的失败不影响其它。"""
    perps = coinbase.cde_perps()
    pairs = [cfg.symbols[s] for s in symbols if s in cfg.symbols]
    info = kraken.pair_info(pairs) if pairs else {}
    quotes, skipped = [], []
    for s in symbols:
        pair = cfg.symbols.get(s)
        if not pair:
            skipped.append(f"{s}: config.toml 的 [symbols] 里没有")
            continue
        p = perps.get(s)
        if not p:
            skipped.append(f"{s}: CDE 没有这个标的的永续")
            continue
        if pair not in info:
            skipped.append(f"{s}: Kraken 没有交易对 {pair}")
            continue
        try:
            quotes.append(Quote(
                symbol=s, pair=pair, product_id=p["product_id"],
                contract_size=p["contract_size"],
                spot=kraken.depth(pair), perp=coinbase.book(p["product_id"]),
                funding_rate=p["funding_rate"], interval_s=p["interval_s"],
                index_price=p["index_price"],
                session_open=p["session_open"] and not p["trading_disabled"],
                margin_short=p["margin_short_overnight"],
                open_interest=p["open_interest"],
                ordermin=info[pair]["ordermin"],
                lot_decimals=info[pair]["lot_decimals"]))
        except Exception as e:      # noqa: BLE001 —— 一个标的取数失败不拖垮整张表
            skipped.append(f"{s}: {e}")
    return quotes, skipped


def contracts_for_usd(q: Quote, usd: float) -> int:
    """单腿约 usd 美元对应几张。只能整张，所以向下取整。"""
    px = q.perp.mid or q.index_price
    if not px:
        return 0
    return math.floor(usd / (q.contract_size * px))


def spot_qty(q: Quote, contracts: int) -> float:
    """与 contracts 张等量的现货。按 Kraken 的数量精度取整。"""
    return round(contracts * q.contract_size, q.lot_decimals)


def estimate(q: Quote, contracts: int, fees: Fees) -> dict:
    """按【当前】订单簿估算开仓 + 平仓一整轮（全吃单）的成本。

    平仓也用现在的簿子估 —— 真实平仓时的簿子未知，这是个近似。
    滑点 = 成交均价与各自中间价的差；基差另算，不混进成本：
    开仓时永续比现货贵 x，平仓时若基差回到 0，这 x 是赚的。
    """
    qty = spot_qty(q, contracts)
    cs = q.contract_size
    out: dict = {"contracts": contracts, "qty": qty}
    if contracts < 1:
        out["error"] = "不足一张合约"
        return out
    if qty < q.ordermin:
        out["error"] = f"现货 {qty} 低于 Kraken 最小下单量 {q.ordermin}"
        return out

    legs = {
        "open_spot": (q.spot.asks, qty, q.spot.mid, 1),    # 买现货
        "open_perp": (q.perp.bids, contracts, q.perp.mid, cs),   # 卖永续
        "close_spot": (q.spot.bids, qty, q.spot.mid, 1),
        "close_perp": (q.perp.asks, contracts, q.perp.mid, cs),
    }
    slip, fee = 0.0, 0.0
    for name, (levels, n, mid, mult) in legs.items():
        avg, filled = walk(levels, n)
        if filled + 1e-9 < n or avg is None or mid is None:
            out["error"] = f"{name} 深度不够（簿子上只有 {filled:g}，要 {n:g}）"
            return out
        notional = avg * n * mult
        slip += abs(avg - mid) * n * mult
        if "spot" in name:
            fee += notional * fees.kraken_taker
        else:
            fee += notional * fees.cde_taker + n * fees.cde_per_contract
        out[f"{name}_px"] = avg
    notional = qty * q.spot.mid
    out["notional"] = notional
    out["fees"] = fee
    out["slippage"] = slip
    out["roundtrip"] = fee + slip
    out["roundtrip_pct"] = out["roundtrip"] / notional
    if q.funding_rate:
        per_hour = q.funding_rate * contracts * cs * (q.index_price or q.perp.mid) \
            * 3600 / q.interval_s
        out["funding_per_day"] = per_hour * 24
        # 只有费率为正（空头收钱）时回本时间才有意义
        out["breakeven_days"] = (out["roundtrip"] / (per_hour * 24)
                                 if per_hour > 0 else None)
    # 资本占用：现货全额 + 空单隔夜保证金
    if q.margin_short:
        out["capital"] = notional * (1 + q.margin_short)
    return out
