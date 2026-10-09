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


# --- M4 / M9：watch.run 的状态机 ---------------------------------------------------

def _run_env(tmp_path, monkeypatch, table):
    from hedgebot import watch
    sent = []
    monkeypatch.setattr(watch.coinbase, "cde_perps", lambda: {})
    monkeypatch.setattr(watch, "hedge_table", table)
    monkeypatch.setattr(watch.notify, "send", lambda t, m, p=0: (sent.append((t, p)), (True, []))[1])
    c = cfg(db_path=tmp_path / "paper.db", blind_after=3)
    return watch, c, sent


def test_missing_credentials_alert_immediately_not_after_streak(tmp_path, monkeypatch):
    """M4：缺凭据是配置问题，等多久都不会好 —— 第一轮就报「凭据缺失」，不进连续失败计数。"""
    from hedgebot.account import MISSING
    watch, c, sent = _run_env(tmp_path, monkeypatch,
                              lambda perps, expected=None: ([], [], [MISSING + "没有 RH_API_KEY"]))
    watch.run(c, now=NOW)
    assert any("凭据缺失" in t for t, _ in sent), sent
    assert watch.State(tmp_path / "watch.db").get("fail_streak", "0") == "0"
    assert not any("没对齐" in t for t, _ in sent), "配置错误不能用「两腿没对齐」的标题"


def test_transient_errors_alert_only_after_blind_after(tmp_path, monkeypatch):
    """临时取数失败照旧：连续 blind_after 轮才报「监控看不见」。"""
    watch, c, sent = _run_env(tmp_path, monkeypatch,
                              lambda perps, expected=None: ([], [], ["Kraken 余额取数失败：502"]))
    for i in range(2):
        watch.run(c, now=NOW + dt.timedelta(minutes=15 * i))
    assert not sent
    watch.run(c, now=NOW + dt.timedelta(minutes=30))
    assert any("监控看不见" in t for t, _ in sent), sent


def test_unexpected_venue_missing_is_only_a_note(monkeypatch):
    """名单外的场所缺凭据：只记提示，不算错误。"""
    from hedgebot import account
    for k in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "RH_API_KEY", "RH_PRIVATE_KEY",
              "COINBASE_API_KEY_NAME", "COINBASE_API_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    rows, notes, errors = account.hedge_table({}, expected=("coinbase",))
    assert [e for e in errors if "COINBASE" in e] and not [e for e in errors if "KRAKEN" in e or "RH_" in e]
    assert any("kraken" in n for n in notes) and any("robinhood" in n for n in notes)


# --- RH 解押提醒 ----------------------------------------------------------------------

def _unstake_env(tmp_path, monkeypatch, avail):
    from hedgebot import robinhood
    watch, c, sent = _run_env(tmp_path, monkeypatch, lambda perps, expected=None: ([], [], []))
    c = c.__class__(**{**c.__dict__, "unstake_targets": {"ETH": 4.5}})
    monkeypatch.setenv("RH_API_KEY", "k")
    monkeypatch.setenv("RH_PRIVATE_KEY", "s")
    calls = []

    def holdings(k, s, available=False):
        calls.append(available)
        v = avail()
        if isinstance(v, Exception):
            raise v
        return {"ETH": v}
    monkeypatch.setattr(robinhood, "holdings", holdings)
    return watch, c, sent, calls


def test_unstake_reminder_fires_when_sellable(tmp_path, monkeypatch):
    state = {"v": 0.0}
    watch, c, sent, calls = _unstake_env(tmp_path, monkeypatch, lambda: state["v"])
    watch.run(c, now=NOW)
    assert not sent and calls == [True], "查的必须是【可卖】数量，不是总量"
    state["v"] = 4.5
    watch.run(c, now=NOW + dt.timedelta(minutes=15))
    assert any("解押到账" in t and "ETH" in t and p == 1 for t, p in sent), sent
    n = len(sent)
    watch.run(c, now=NOW + dt.timedelta(minutes=30))
    assert len(sent) == n, "按退避重发，不是每轮都响"


def test_unstake_reminder_survives_fetch_failure(tmp_path, monkeypatch):
    from hedgebot.http import SourceError
    state = {"v": 4.5}
    watch, c, sent, calls = _unstake_env(tmp_path, monkeypatch, lambda: state["v"])
    watch.run(c, now=NOW)
    assert len(sent) == 1
    state["v"] = SourceError("RH 502")
    watch.run(c, now=NOW + dt.timedelta(minutes=15))
    state["v"] = 4.5
    watch.run(c, now=NOW + dt.timedelta(minutes=30))
    assert len(sent) == 1, "取数抖一下不该把已发的提醒清掉、再当新的重发"
    assert not any("已恢复" in t for t, _ in sent)
