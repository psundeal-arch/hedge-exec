import datetime as dt

from hedgebot.config import Config, Fees
from hedgebot.account import classify
from hedgebot.watch import plan

NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)


def cfg(**kw):
    base = dict(symbols={}, fees=Fees(0.004, 0.0025, 0.0005, 0.0002), max_slippage_bps=30,
                hedge_tolerance=0.02, default_usd=5000, db_path=None,
                hold_only=frozenset({"BTC", "PAXG"}), min_alert_usd=200)
    base.update(kw)
    return Config(**base)


def rec(hours_ago, n):
    t = (NOW - dt.timedelta(hours=hours_ago)).isoformat()
    return {"first_seen": t, "last_sent": t, "sent_count": n, "detail": "旧"}


def kinds(alerts):
    return sorted((a.key, a.kind) for a in alerts)


def test_new_resend_backoff_and_recover():
    c = cfg()
    cur = {"misalign:ETH": ("x", 1)}
    assert kinds(plan(cur, None, {}, c, NOW)) == [("misalign:ETH", "new")]
    # 发过 1 次，第一档 1 小时：半小时后不发，1 小时后发
    assert plan(cur, None, {"misalign:ETH": rec(0.5, 1)}, c, NOW) == []
    assert kinds(plan(cur, None, {"misalign:ETH": rec(1, 1)}, c, NOW)) == [("misalign:ETH", "resend")]
    # 发过 2 次，第二档 3 小时
    assert plan(cur, None, {"misalign:ETH": rec(2, 2)}, c, NOW) == []
    # 问题消失 → 恢复
    assert kinds(plan({}, None, {"misalign:ETH": rec(5, 2)}, c, NOW)) == [("misalign:ETH", "recover")]


def test_blind_round_keeps_misalign_records():
    """取数失败那轮只评估 blind；已有的 misalign 记录既不重发也不算恢复。"""
    c = cfg()
    existing = {"misalign:ETH": rec(30, 1)}
    assert plan({}, {"blind"}, existing, c, NOW) == []
    out = plan({"blind": ("看不见", 0)}, {"blind"}, existing, c, NOW)
    assert kinds(out) == [("blind", "new")]


def row(asset, spot, perp, px=100.0, cs=1.0):
    return {"asset": asset, "spot": spot, "spot_parts": {"X": spot}, "perp": perp,
            "delta": spot + perp, "price": px, "contract_size": cs}


def test_classify_problem_rules():
    c = cfg()
    by = {r["asset"]: r for r in classify([
        row("ETH", 10, -11, px=2700),       # 净空 1 个 ≈ $2700 → 问题
        row("LTC", 10, -9.9),               # 1% 以内 → ✓
        row("LINK", 10, -5, px=10),         # 没对齐但只差 $50 → 标 ⚠ 不报警
        row("BTC", 0.07, -0.06, px=84000),  # 名单内净多 → ✓
        row("PAXG", 20, 0),                 # 名单内纯现货
        row("SOL", 5, 0),                   # 不在名单的纯现货
    ], c)}
    assert by["ETH"]["problem"] and by["ETH"]["flag"] == "⚠ 没对齐"
    assert by["LTC"]["flag"] == "✓" and not by["LTC"]["problem"]
    assert by["LINK"]["flag"] == "⚠ 没对齐" and not by["LINK"]["problem"]
    assert by["BTC"]["flag"] == "✓ 余量有意持有" and by["BTC"]["hint"] == ""
    assert by["PAXG"]["flag"] == "有意持有"
    assert by["SOL"]["flag"] == "仅现货" and not by["SOL"]["problem"]


def test_hold_only_net_short_still_alerts():
    r = classify([row("BTC", 0.05, -0.06, px=84000)], cfg())[0]
    assert r["flag"] == "⚠ 空单多于现货" and r["problem"]


def test_cde_funding_table_uses_averages(monkeypatch, tmp_path):
    import sqlite3, datetime as dt
    import hedgebot.scan as sc
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    con.execute("create table funding_rates(ts text, product text, apr real, session_open real)")
    for h in range(30):
        con.execute("insert into funding_rates values(?,?,?,?)",
                    ((now - dt.timedelta(hours=h)).isoformat(), "NER-X", -12.0, 1))
    con.commit()
    monkeypatch.setattr(sc.coinbase, "cde_perps", lambda: {"NEAR": {
        "product_id": "NER-X", "funding_rate": 2e-5, "contract_size": 100, "index_price": 2.0,
        "open_interest": 1000, "margin_short_overnight": 0.5, "session_open": True}})
    rows, meta = sc.cde_funding_table(db)
    r = rows[0]
    assert r["apr_24h"] == -12.0 and r["apr_7d"] == -12.0
    assert r["apr_now"] > 0                       # 当前为正、均值为负 → 输出里会标成噪音
    assert r["oi_usd"] == 1000 * 100 * 2.0 and meta["span"] is not None


def test_grid_only_perp_is_not_aligned_or_alerted():
    # perp-grid 实盘网格独立做 SUI：只有永续、没有现货，也不报"没对齐"
    r = classify([row("SUI", 0.0, -2000.0, px=1.18)], cfg(grid_only=frozenset({"SUI"})))[0]
    assert r["flag"] == "网格实盘（不对齐）" and not r["problem"]
    r = classify([row("SUI", 0.0, -2000.0, px=1.18)], cfg())[0]
    assert r["problem"]
