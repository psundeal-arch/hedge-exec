"""开仓硬约束与出场容量：纯计算，不联网。"""
import pytest

from hedgebot.config import load_config
from hedgebot.live import fixed_fee_blocker, fixed_fee_bp
from hedgebot.scan import walk_bp

CFG = load_config()


def test_fixed_fee_is_a_hard_limit_on_small_contracts():
    """AVAX 一张 $109：$0.12 固定费 = 11bp/边，往返 22bp —— 拒绝，而不是只在表里印个成本。"""
    assert fixed_fee_bp(CFG, 109) == pytest.approx(CFG.fees.cde_per_contract / 109 * 1e4)
    why = fixed_fee_blocker(CFG, 109)
    assert why and "bp/边" in why and "max_fixed_fee_bp" in why
    assert fixed_fee_blocker(CFG, 1320) is None                  # ZEC 0.9bp 放行
    assert fixed_fee_blocker(CFG, 0) is not None                 # 没价格不能当成免费


def test_walk_bp_measures_own_exit_not_entry_depth():
    """出场 $25k：第一档 100 张 @ 中价+5bp（$10,005），第二档 200 张 @ 中价+20bp → 吃到第二档。"""
    mid = 100.0
    asks = ((100.05, 100.0), (100.20, 200.0))                    # 数量是张，每张 1 枚
    bp = walk_bp(asks, 25_000, mid, cs=1.0)
    coins = 100 + (25_000 - 100 * 100.05) / 100.20               # 第一档吃光，余下在第二档
    assert bp == pytest.approx((25_000 / coins / mid - 1) * 1e4)
    assert 5 < bp < 20
    assert walk_bp(asks, 50_000, mid) is None, "簿子吃不完要说出来，不能当成没冲击"
    # 卖盘（bids）方向：成交价低于中价，冲击仍是正数
    assert walk_bp(((99.9, 1000.0),), 10_000, mid) == pytest.approx(10, rel=1e-6)


def test_load_env_reads_vault_whitelist_after_dotenv(tmp_path, monkeypatch):
    """凭据集中到 ~/vault：.env 优先，vault 只按白名单补缺 —— 交易密钥文件不读。"""
    import os
    from hedgebot import config
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "notify.env").write_text("PUSHOVER_TOKEN=from_vault\nX_ONLY_VAULT=v\n")
    (vault / "coinbase-trade-server.env").write_text("COINBASE_TRADE_KEY_NAME=must_not_load\n")
    dotenv = tmp_path / ".env"
    dotenv.write_text("PUSHOVER_TOKEN=from_dotenv\n")
    monkeypatch.setattr(config, "VAULT_DIR", vault)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for k in ("PUSHOVER_TOKEN", "X_ONLY_VAULT", "COINBASE_TRADE_KEY_NAME"):
        monkeypatch.delenv(k, raising=False)
    config.load_env()
    assert os.environ["PUSHOVER_TOKEN"] == "from_dotenv", ".env 在的时候它说了算"
    assert os.environ["X_ONLY_VAULT"] == "v", "vault 补缺口"
    assert "COINBASE_TRADE_KEY_NAME" not in os.environ, "交易密钥不在白名单里"


def test_all_futures_pages_until_empty(monkeypatch):
    """M6：接口单页最多 100 个，全场正好 100 个 —— 再多一个就会挤掉某个永续。翻页直到空页。"""
    from hedgebot import coinbase
    allp = [{"product_id": f"P{i}"} for i in range(230)]
    calls = []

    def fake(method, url, label, params=None, **k):
        calls.append(params["offset"])
        o, n = params["offset"], min(params["limit"], 100)
        return {"products": allp[o:o + n], "pagination": {"has_next": False}}  # has_next 不可信
    monkeypatch.setattr(coinbase, "request", fake)
    got = coinbase._all_futures(page=250)
    assert len(got) == 230 and calls == [0, 100, 200, 230]


def test_market_survives_one_bad_kraken_pair(monkeypatch):
    """M7：config 里一个写错的 Kraken 交易对，原来让整张 hedge market 表全灭。"""
    from hedgebot import coinbase, kraken, market
    from hedgebot.http import SourceError

    def pair_info(pairs):
        if "BADUSD" in pairs:
            raise SourceError("EQuery:Unknown asset pair")
        return {p: {} for p in pairs}
    monkeypatch.setattr(kraken, "pair_info", pair_info)
    monkeypatch.setattr(coinbase, "cde_perps", lambda: {})
    cfg = CFG.__class__(**{**CFG.__dict__, "symbols": {"ZEC": "ZECUSD", "BAD": "BADUSD"}})
    quotes, skipped = market.fetch_quotes(cfg, ["ZEC", "BAD"])
    assert any("BADUSD" in s for s in skipped), skipped
    assert any(s.startswith("ZEC") for s in skipped), "好的那个照常往下走（这里因为没有 CDE 永续被跳过）"


def test_accept_fixed_fee_is_a_one_shot_override(monkeypatch):
    """固定费超上限默认拦；--accept-fixed-fee 只对这一笔放行，账照样印出来。"""
    import types
    from hedgebot import live
    from hedgebot.book import Book

    class Leg:
        min_qty, lot, name = 0.01, 0.01, "kraken"

        def __init__(self, px):
            self.px = px

        def book(self):
            return Book(bids=((self.px, 1e6),), asks=((self.px * 1.0002, 1e6),))
    p = {"contract_size": 10.0, "product_id": "AVP-PERP", "funding_rate": 1e-5, "session_open": True,
         "trading_disabled": False, "unit": 1.0, "margin_short_overnight": 0.45}
    monkeypatch.setattr(live, "build", lambda *a, **k: (p, Leg(10.9), Leg(10.9), None, lambda: {}, 0.0025, []))
    monkeypatch.setattr(live, "session_risk", lambda *a: None)
    ns = types.SimpleNamespace(symbol="AVAX", spot="kraken", perp="cde", contracts=1, usd=None, action="open",
                               max_wait=300, paper=True, finish="complete")
    plan = live.preflight(CFG, ns, need_trade=False)
    assert any("bp/边" in b and "--accept-fixed-fee" in b for b in plan["blockers"]), plan["blockers"]
    ns.accept_fixed_fee = True
    plan = live.preflight(CFG, ns, need_trade=False)
    assert not any("bp/边" in b for b in plan["blockers"]), "放行后不再是阻断项"
    assert any("已用 --accept-fixed-fee 放行" in w for w in plan["warnings"]), "账要照样印出来"
