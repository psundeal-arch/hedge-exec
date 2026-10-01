"""不联网：用手搓的订单簿测成交、记账、回滚、资金费。"""
import datetime as dt
from dataclasses import replace

import pytest

from hedgebot.book import Book, walk
from hedgebot.config import Config, Fees
from hedgebot.executor import ExecError, LegError, PaperVenue, run_pair
from hedgebot.market import Quote, estimate
from hedgebot.paper import Paper, apply_fill

T0 = dt.datetime(2026, 10, 1, 10, 30, tzinfo=dt.timezone.utc)


def cfg(tmp_path, slip=50):
    return Config(symbols={"LTC": "LTCUSD"},
                  fees=Fees(kraken_taker=0.004, kraken_maker=0.0025,
                            cde_taker=0.0005, cde_maker=0.0002),
                  max_slippage_bps=slip, hedge_tolerance=0.02, default_usd=5000,
                  db_path=tmp_path / "p.db")


def quote(spot_bid=100.0, spot_ask=100.1, perp_bid=100.2, perp_ask=100.3,
          rate=0.00003, session=True):
    # LTC 风格：每张 5 个币
    return Quote(symbol="LTC", pair="LTCUSD", product_id="LCP-TEST", contract_size=5,
                 spot=Book(bids=((spot_bid, 1000),), asks=((spot_ask, 1000),)),
                 perp=Book(bids=((perp_bid, 100),), asks=((perp_ask, 100),)),
                 funding_rate=rate, interval_s=3600, index_price=100.0,
                 session_open=session, margin_short=0.5, open_interest=1e4,
                 ordermin=0.1, lot_decimals=8)


def test_walk_multi_level():
    avg, filled = walk([(10, 1), (11, 1)], 1.5)
    assert filled == 1.5 and avg == pytest.approx((10 + 5.5) / 1.5)
    assert walk([(10, 1)], 2)[1] == 1


def test_apply_fill_avg_and_realize():
    q, a, r = apply_fill(0, 0, -2, 100, 5)        # 开空 2 张
    q, a, r = apply_fill(q, a, -2, 110, 5)        # 加空
    assert (q, a, r) == (-4, 105, 0)
    q, a, r = apply_fill(q, a, 1, 95, 5)          # 买平 1 张，空单赚 (105-95)*5
    assert (q, a) == (-3, 105) and r == pytest.approx(50)


def test_open_close_roundtrip_accounting(tmp_path):
    pp = Paper(cfg(tmp_path))
    q = quote()
    perp_fill, spot_fill = pp.open(q, 2, now=T0)
    assert (perp_fill.side, spot_fill.side) == ("sell", "buy")
    pos = pp.position("LTC")
    assert pos["contracts"] == -2 and pos["spot_qty"] == 10
    assert Paper.mark(pos, q, now=T0)["delta_qty"] == 0

    pp.close(q, 100, now=T0)
    assert pp.position("LTC") is None
    c = pp.closed()[-1]
    # 10 个币：现货 100.1 买 100 卖 = -1；永续 100.2 卖 100.3 买 = -1
    assert c["realized"] == pytest.approx(-2.0)
    fees = 10 * 100.1 * 0.004 + 10 * 100 * 0.004 + 10 * 100.2 * 0.0005 + 10 * 100.3 * 0.0005
    assert c["fees"] == pytest.approx(fees)
    assert c["net"] == pytest.approx(-2.0 - fees)


def test_partial_close(tmp_path):
    pp = Paper(cfg(tmp_path))
    q = quote()
    pp.open(q, 4, now=T0)
    pp.close(q, 50, now=T0)
    pos = pp.position("LTC")
    assert pos["contracts"] == -2 and pos["spot_qty"] == 10


def test_funding_accrues_per_whole_hour(tmp_path):
    pp = Paper(cfg(tmp_path))
    q = quote(rate=0.0001)
    pp.open(q, 2, now=T0)                                  # 10:30 开，从 10:00 起算
    assert pp.accrue(q, now=T0 + dt.timedelta(minutes=20)) == 0      # 10:50 还没到整点
    amt = pp.accrue(q, now=T0 + dt.timedelta(hours=2))     # 12:30 → 11:00、12:00 两期
    assert amt == pytest.approx(0.0001 * 10 * 100 * 2)     # 空单收钱
    assert pp.accrue(q, now=T0 + dt.timedelta(hours=2, minutes=10)) == 0
    # 闭市时按 0 记，但时间照样往前走
    assert pp.accrue(replace(q, session_open=False), now=T0 + dt.timedelta(hours=3)) == 0
    assert pp.position("LTC")["funding_cum"] == pytest.approx(0.2)


def test_negative_funding_costs_short(tmp_path):
    pp = Paper(cfg(tmp_path))
    q = quote(rate=-0.0001)
    pp.open(q, 2, now=T0)
    assert pp.accrue(q, now=T0 + dt.timedelta(hours=1)) < 0


def test_closed_session_refuses(tmp_path):
    pp = Paper(cfg(tmp_path))
    with pytest.raises(ExecError, match="闭市"):
        pp.open(quote(session=False), 2, now=T0)
    assert pp.position("LTC") is None and pp.fills() == []


def test_slippage_guard_blocks_both_legs(tmp_path):
    pp = Paper(cfg(tmp_path, slip=1))
    q = quote(spot_bid=99, spot_ask=101)          # 现货价差 2%
    with pytest.raises(ExecError, match="滑点"):
        pp.open(q, 2, now=T0)
    assert pp.fills() == []                        # 预检失败，一腿都没下


def test_second_leg_failure_rolls_back_first():
    perp = PaperVenue("cde", Book(bids=((100, 10),), asks=((101, 10),)), 0.0005, 5)
    spot = PaperVenue("kraken", Book(bids=((100, 1),), asks=((100, 1),)), 0.004)
    with pytest.raises(LegError) as ei:
        run_pair(perp, "sell", 2, spot, "buy", 10)        # 现货深度只有 1
    e = ei.value
    assert not e.naked
    assert [(f.venue, f.side) for f in e.fills] == [("cde", "sell"), ("cde", "buy")]


def test_rollback_failure_flags_naked():
    perp = PaperVenue("cde", Book(bids=((100, 10),), asks=()), 0.0005, 5)   # 买不回来
    spot = PaperVenue("kraken", Book(bids=(), asks=()), 0.004)
    with pytest.raises(LegError) as ei:
        run_pair(perp, "sell", 2, spot, "buy", 10)
    assert ei.value.naked and len(ei.value.fills) == 1


def test_rollback_is_booked_as_loss(tmp_path):
    pp = Paper(cfg(tmp_path))
    q = quote()
    q = replace(q, spot=Book(bids=((100, 1),), asks=((100.1, 1),)))   # 现货深度不够
    # 预检会先拦住；绕过预检直接测记账
    pp.cfg = replace(pp.cfg, max_slippage_bps=1e9)
    import hedgebot.paper as paper_mod
    orig = paper_mod.check_slippage
    paper_mod.check_slippage = lambda *a, **k: 0.0
    try:
        with pytest.raises(LegError):
            pp.open(q, 2, now=T0)
    finally:
        paper_mod.check_slippage = orig
    assert pp.position("LTC") is None
    c = pp.closed()[-1]
    assert c["net"] < 0 and c["realized"] == pytest.approx(-(100.3 - 100.2) * 10)


def test_estimate_breakeven():
    e = estimate(quote(rate=0.0001), 2, Fees(0.004, 0.0025, 0.0005, 0.0002))
    assert e["funding_per_day"] == pytest.approx(0.0001 * 10 * 100 * 24)
    assert e["breakeven_days"] == pytest.approx(e["roundtrip"] / e["funding_per_day"])
    assert "error" in estimate(quote(), 0, Fees(0.004, 0.0025, 0.0005, 0.0002))
