"""hedge live：开/平仓。默认只预检；--go 真实下单（终端手输确认）；
--paper 盯真实行情模拟下单，执行引擎与 --go 完全相同，成交记入模拟盘。

交易 key 放在 .env.trade（只留本机，deploy 不会带上服务器）：
    COINBASE_TRADE_KEY_NAME / COINBASE_TRADE_PRIVATE_KEY   现货 + CDE 下单
    KRAKEN_TRADE_KEY / KRAKEN_TRADE_SECRET                 现货在 Kraken 时才需要
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import coinbase
from .config import PROJECT_ROOT, Config, load_env
from .http import SourceError
from .ladder import Ladder, Params, Unreconciled, settle
from .rh_trade import RHTrader, RobinhoodLeg, rh_available
from .paper_legs import PaperPerpLeg, PaperSpotLeg, coinbase_trades, kraken_trades
from .trade_api import (OrderUnknown, CoinbaseLeg, KrakenLeg, _CoinbaseAuth, _KrakenAuth,
                        coinbase_available, kraken_available)

SCHEMA = """
create table if not exists runs(
    id integer primary key, started text, ended text, symbol text, action text,
    spot_venue text, contracts integer, status text, summary text);
create table if not exists fills(
    id integer primary key, run_id integer, ts text, venue text, side text,
    qty real, price real, fee real, order_id text, kind text);
"""


def _env(*names: str) -> tuple[str, ...] | None:
    vals = tuple(os.environ.get(n, "").strip() for n in names)
    return vals if all(vals) else None


def trade_auths(spot_venue: str, *, need_trade: bool):
    """返回 (coinbase_auth, kraken_auth, 说明)。预检时没有交易 key 就退回只读 key。"""
    load_env(PROJECT_ROOT / ".env.trade")
    notes = []
    cb = _env("COINBASE_TRADE_KEY_NAME", "COINBASE_TRADE_PRIVATE_KEY")
    if not cb:
        if need_trade:
            raise SystemExit("缺 COINBASE_TRADE_KEY_NAME / COINBASE_TRADE_PRIVATE_KEY（写进 .env.trade）")
        cb = _env("COINBASE_API_KEY_NAME", "COINBASE_API_PRIVATE_KEY")
        notes.append("没有 Coinbase 交易 key，用只读 key 预检（下单预检可能被拒）")
    kr = None
    if spot_venue == "kraken":
        kr = _env("KRAKEN_TRADE_KEY", "KRAKEN_TRADE_SECRET")
        if not kr:
            if need_trade:
                raise SystemExit("缺 KRAKEN_TRADE_KEY / KRAKEN_TRADE_SECRET（写进 .env.trade）")
            kr = _env("KRAKEN_API_KEY", "KRAKEN_API_SECRET")
            notes.append("没有 Kraken 交易 key，用只读 key 预检（validate 会被拒）")
    if not cb:
        raise SystemExit("连只读的 Coinbase key 都没有")
    return (_CoinbaseAuth(*cb), _KrakenAuth(*kr) if kr else None, notes)


def build(cfg: Config, symbol: str, spot_venue: str, *, need_trade: bool, paper: bool = False):
    perps = coinbase.cde_perps()
    p = perps.get(symbol)
    if not p:
        raise SystemExit(f"CDE 没有 {symbol} 的永续")
    if paper:
        # 模拟不碰任何账户：腿的构造只读公开的合约规格，不需要 key
        cb_auth = kr_auth = None
        notes = ["模拟盘：盯真实行情、不下真实订单，成交记入模拟盘（hedge paper status）"]
    else:
        cb_auth, kr_auth, notes = trade_auths(spot_venue, need_trade=need_trade)
    perp = CoinbaseLeg(cb_auth, p["product_id"], is_perp=True)
    if spot_venue == "coinbase":
        spot = CoinbaseLeg(cb_auth, f"{symbol}-USD", is_perp=False)
        avail = lambda: coinbase_available(cb_auth)            # noqa: E731
        maker = cfg.fees.coinbase_maker
    elif spot_venue == "robinhood":
        # Robinhood 的盘口接口也要签名，所以模拟盘同样需要 key（只读即可）
        load_env(PROJECT_ROOT / ".env.trade")
        rk = _env("RH_TRADE_API_KEY", "RH_TRADE_PRIVATE_KEY")
        if not rk:
            if need_trade:
                raise SystemExit("缺 RH_TRADE_API_KEY / RH_TRADE_PRIVATE_KEY —— 先 hedge rh-keygen --trade")
            rk = _env("RH_API_KEY", "RH_PRIVATE_KEY")
            notes.append("没有 Robinhood 交易 key，用只读 key（报价可能读不到）")
        if not rk:
            raise SystemExit("没有 Robinhood key")
        rht = RHTrader(*rk)
        spot = RobinhoodLeg(rht, f"{symbol}-USD")
        avail = lambda: rh_available(rht)                      # noqa: E731
        # v2 一律按吃单收；费率按账户当前档位（30 天量决定），读不到就按 0.15%
        try:
            maker = float((rht.account().get("fee_tier_status") or {}).get("fee_ratio") or 0.0015)
        except Exception:                                          # noqa: BLE001
            maker = 0.0015
    else:
        pair = cfg.symbols.get(symbol)
        if not pair:
            raise SystemExit(f"config.toml [symbols] 里没有 {symbol} 的 Kraken 交易对")
        spot = KrakenLeg(kr_auth, pair)
        avail = lambda: kraken_available(kr_auth)              # noqa: E731
        maker = cfg.fees.kraken_maker
    if paper:
        f = cfg.fees
        taker = {"coinbase": f.coinbase_taker, "kraken": f.kraken_taker}.get(spot_venue, maker)
        # 先把代码绑定下来 —— 下一行 spot 就被换成模拟腿了，闭包里不能再引用它
        if spot_venue == "robinhood":
            trades = lambda: []                                # noqa: E731 —— 只吃单，用不到成交流
        else:
            ident = spot.product_id if spot_venue == "coinbase" else spot.pair
            trades = ((lambda: coinbase_trades(ident)) if spot_venue == "coinbase"
                      else (lambda: kraken_trades(ident)))
        spot = PaperSpotLeg(spot, maker_fee=maker, taker_fee=taker, trades_fn=trades)
        perp = PaperPerpLeg(perp, contract_size=p["contract_size"], taker_fee=f.cde_taker,
                            per_contract=f.cde_per_contract)
        avail = None
    return p, spot, perp, cb_auth, avail, maker, notes


def session_risk(perp: dict, now: dt.datetime, window_s: float) -> str | None:
    """CDE 在 window_s 秒内会闭市或进维护窗口 → 返回原因。

    一轮挂单最长要 max_wait_s；跑到一半闭市，空单就跟不上了，现货成了裸腿。
    close_time 是交易日的切换点（每天美东 17:00：夏令时 21:00 UTC、冬令时 22:00 UTC），
    不一定真的停盘 —— 时间取自接口，不受夏令时影响；
    这里按会停处理，宁可多等一刻钟。
    """
    def parse(t):
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None

    close = parse(perp.get("close_time"))
    if close and 0 <= (close - now).total_seconds() < window_s:
        return f"距 CDE 交易日切换（{close:%H:%M} UTC）只剩 {(close - now).total_seconds() / 60:.0f} 分钟"
    m = perp.get("maintenance") or {}
    start, end = parse(m.get("start_time")), parse(m.get("end_time"))
    if start and end and (start - now).total_seconds() < window_s and now < end:
        return f"CDE 维护窗口 {start:%m-%d %H:%M}–{end:%H:%M} UTC 临近或进行中"
    return None


def leftover_orders(spot, warns: list) -> list[str]:
    """Robinhood 上这个交易对还挂着没到终态的单 → 拦下（H7）。

    RH 没有 IOC，「带保护价的吃单」是下 GTC 限价 → 等 → 撤；进程死在中间就留下一张
    没人管的活单，它之后成交不会被记账。新一轮开始前先把它找出来，让人处理掉。"""
    if not hasattr(spot, "open_orders"):
        return []
    try:
        left = spot.open_orders()
    except Exception as e:                                       # noqa: BLE001
        warns.append(f"查不到 Robinhood 未完成订单（{type(e).__name__}）—— 去 App 看一眼有没有挂着的单")
        return []
    return [f"Robinhood {spot.symbol} 还有未完成的订单：{o.get('side')} "
            f"{o.get('limit_order_config', {}).get('asset_quantity') or o.get('market_order_config', {}).get('asset_quantity')}"
            f"（{o.get('state')}，{o.get('created_at', '')[:16]}，id {o.get('id')}）—— 先在 App 里撤掉或等它成交"
            for o in left]


def perp_track_bin() -> str:
    """perp-track 命令的位置。依次：PERP_TRACK_BIN 环境变量 > 与本 Python 同目录 >
    本机 perp-track 仓的 venv（PERP_TRACK_HOME，默认 ~/git/perp-track）> 服务器部署目录
    ~/perp-track 的 venv > PATH。

    拆仓后 hedge-bot 的 venv 里没有 perp-track；只靠 PATH 的话本机 hedge live 找不到它，
    闸门「出错不放行」—— 等于所有开仓都被拦（10-12 拆仓当天发现）。"""
    import shutil
    import sys
    if os.environ.get("PERP_TRACK_BIN"):
        return os.environ["PERP_TRACK_BIN"]
    home = Path(os.environ.get("PERP_TRACK_HOME") or Path.home() / "git" / "perp-track")
    for c in (Path(sys.executable).with_name("perp-track"),
              home / ".venv" / "bin" / "perp-track",
              Path.home() / "perp-track" / ".venv" / "bin" / "perp-track"):
        if c.exists():
            return str(c)
    return shutil.which("perp-track") or "perp-track"


def margin_gate(*, add_notional: float, margin_rate: float | None, spend_cash: float,
                note: str, register: bool, ttl_s: float = 1800.0,
                product_id: str = "", contracts: int = 0) -> tuple[str | None, str]:
    """问 perp-track 的保证金闸门：这一单下完，账户保证金率（App 口径）会在哪。

    和「CDE 购买力」检查是【叠加】不是替换：购买力答「这一单下得了吗」，闸门答
    「下完之后会不会越过闸门线」（线由 perp-track 的 [perp] margin_ratio_max 定，10-16 起 70%，不跟时段）—— 购买力可以很充裕而比值已越线（网格据此停开新格）。

    register=True 时放行会登记一条动仓意图，80% 以下的传呼在 TTL 内都被压住（10-16 起传呼必须同时满足水位与速率，速率正是下单造成的） —— 只在
    真要下单时传（--go / exec worker），只看不下单别登记，否则白白压掉速率判定。
    登记由 perp-track gate 转发到采集机的活库（本机跑时走 ssh）；转发失败照样放行，但 reason 以 ⚠
    开头，下面会把它印出来 —— 那一笔可能招来一次误传呼。

    给了 product_id + contracts 就让闸门用 Coinbase 自己 orders/preview 的 projected_margin_ratio
    （比手算折算准：11 张 HEP 手算 55.5%、CB 57.2%）；spend_cash 照传 —— preview 不知道要花现金买现货。

    返回 (阻断原因或 None, 一行说明)。【取数失败一律拦】（fail-closed）。

    走 `perp-track gate` 子进程，不 import perptrack（拆仓：能下单的代码不依赖只读监控的包）。
    契约：退出码 0 = 放行 / 1 = 拦，stdout 是 Gate 的 JSON；其它退出码、超时、找不到命令、
    JSON 解析不了 —— 都是【闸门出错】，同样不放行。两类别混：1 是正常的拦，别当故障报。
    register=False → --no-intent（真的不登记；原来 import 方式下只要带 source 就会登记）。"""
    import json as _json
    import subprocess
    from types import SimpleNamespace
    cmd = [perp_track_bin(), "gate", "--add-notional", f"{add_notional}", "--spend-cash", f"{spend_cash}",
           "--source", "hedge-bot", "--note", note, "--ttl", f"{ttl_s:g}"]
    if margin_rate is not None:                               # None 要省略，不能传字符串 "None"
        cmd += ["--margin-rate", f"{margin_rate}"]
    if product_id and contracts > 0:
        cmd += ["--product-id", product_id, "--base-size", str(int(contracts)), "--side", "SELL"]
    if not register:
        cmd.append("--no-intent")
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"保证金闸门出错，不放行：{type(e).__name__}: {e}", ""
    if cp.returncode not in (0, 1):
        tail = (cp.stderr or cp.stdout or "").strip().splitlines()[-1:] or [""]
        return f"保证金闸门出错（退出码 {cp.returncode}），不放行：{tail[0][:200]}", ""
    try:
        g = SimpleNamespace(**_json.loads(cp.stdout))
        g.ok = bool(g.ok)
    except (ValueError, TypeError) as e:
        return f"保证金闸门出错（输出不是 JSON），不放行：{e}", ""
    if g.ok != (cp.returncode == 0):                          # 两个信号对不上就当出错，不猜
        return f"保证金闸门出错（退出码 {cp.returncode} 与 ok={g.ok} 不一致），不放行", ""
    info = (f"保证金率（App 口径）{g.ratio_now:.1%} → 成交后 {g.ratio_after:.1%}，线 {g.limit:.0%}"
            if g.ratio_now is not None and g.ratio_after is not None else "")
    if g.ok and (g.reason or "").startswith("⚠"):
        # 放行但动仓登记没写到采集机（ssh 转发失败）：单子照放，可能会有一次误传呼 —— 要让人看见
        info = (info + "；" if info else "") + g.reason.split("\n")[0]
    return (None if g.ok else g.reason), info


def fixed_fee_bp(cfg: Config, per_contract_usd: float) -> float:
    """CDE 每张固定费折成 bp/边。按张收、和名义无关，所以合约越小越贵。"""
    return cfg.fees.cde_per_contract / per_contract_usd * 1e4 if per_contract_usd > 0 else float("inf")


def fixed_fee_blocker(cfg: Config, per_contract_usd: float) -> str | None:
    """固定费是最小可行规模的硬约束：超上限就不开仓（只用在开仓，平仓永远放行）。"""
    bp, lim = fixed_fee_bp(cfg, per_contract_usd), cfg.live.max_fixed_fee_bp
    if bp > lim:
        return (f"一张 ${per_contract_usd:,.0f}，CDE 固定费 ${cfg.fees.cde_per_contract:g}/张 = {bp:.1f}bp/边"
                f"（往返 {2 * bp:.1f}bp），超过上限 {lim:g}bp —— 合约太小，固定费吃掉太多"
                f"（config.toml [live] max_fixed_fee_bp）")
    return None


def _fixed_fee(args, why: str, blockers: list, warns: list) -> None:
    """固定费超上限：默认拦；--accept-fixed-fee 只对这一笔放行，照样把账印出来（不改配置，不会忘了改回去）。"""
    if getattr(args, "accept_fixed_fee", False):
        warns.append(f"【已用 --accept-fixed-fee 放行】{why}")
    else:
        blockers.append(why + "；确认要做：这一笔加 --accept-fixed-fee")


def preflight(cfg: Config, args, *, need_trade: bool) -> dict:
    """所有能在下单前查的都查一遍。返回计划；有致命问题时 plan['blockers'] 非空。"""
    sym = args.symbol.upper()
    paper = getattr(args, "paper", False)
    p, spot, perp, cb_auth, avail, maker, notes = build(cfg, sym, args.spot,
                                                        need_trade=need_trade, paper=paper)
    cs = p["contract_size"]
    pb, sb = perp.book(), spot.book()
    if not (pb.mid and sb.mid):
        raise SystemExit("订单簿是空的（闭市？）")
    contracts = args.contracts or math.floor(args.usd / (cs * pb.mid))
    qty = contracts * cs
    notional = qty * sb.mid
    blockers, warns = [], list(notes)

    if contracts < 1:
        blockers.append("不足一张合约")
    # 缓冲 = 本轮最长挂单时间 + 5 分钟收尾
    window = (args.max_wait or cfg.live.max_wait_s) + 300
    why = session_risk(p, dt.datetime.now(dt.timezone.utc), window)
    if why:
        # 开仓直接拦；平仓只提醒 —— 出事要跑的时候不能被这条挡住
        (blockers if args.action == "open" else warns).append(
            f"{why}，一轮最长要 {window / 60:.0f} 分钟" + ("" if args.action == "open"
                                                        else "，平仓照做但可能跟不上"))
    if args.action == "open" and (p["funding_rate"] or 0) <= 0:
        warns.append(f"当前资金费 {p['funding_rate']}（≤0）：现在开空单是付钱不是收钱")
    if not p["session_open"] or p["trading_disabled"]:
        blockers.append("CDE 现在闭市或停牌")
    if notional > cfg.live.max_notional_usd:
        blockers.append(f"名义 ${notional:,.0f} 超过上限 ${cfg.live.max_notional_usd:,.0f}"
                        f"（config.toml [live] max_notional_usd）")
    if qty < spot.min_qty:
        blockers.append(f"现货 {qty:g} 低于最小下单量 {spot.min_qty:g}")
    if args.action == "open" and (why := fixed_fee_blocker(cfg, cs * pb.mid)):
        _fixed_fee(args, why, blockers, warns)
    blockers += leftover_orders(spot, warns)

    previews = {}
    bal = None
    # 模拟盘仓位只对 hedge live --paper 有意义；hedge exec --dry 不读写 paper.db（H1），
    # 原来演练平仓永远被「模拟盘里只有 0 张」拦下
    if paper and args.action == "close" and getattr(args, "paper_position_check", True):
        from .paper import Paper
        pos = Paper(cfg).position(sym)
        held = -pos["contracts"] if pos else 0
        if held < contracts or not pos or pos["spot_qty"] < qty - 1e-9:
            blockers.append(f"模拟盘里 {sym} 只有 {held:g} 张空单 / "
                            f"{pos['spot_qty'] if pos else 0:g} 现货，不够平 {contracts} 张")
    if not paper:
        try:
            bal = avail()
        except SourceError as e:
            bal = None
            blockers.append(f"读不到 {args.spot} 余额：{e}")
        if bal is not None:
            if args.action == "open":
                need = notional * (1 + maker) * 1.01
                usd, usdc = bal.get("USD", 0), bal.get("USDC", 0)
                if args.spot == "coinbase" and usd < need <= usdc:
                    # SYM-USD 只能用 USD 付，不会自动动用 USDC（2026-10-01 加仓第 3 块
                    # 因此 INSUFFICIENT_FUND）。SYM-USDC 是同一个订单簿的别名，改走它用 USDC 付
                    spot = CoinbaseLeg(cb_auth, f"{sym}-USDC", is_perp=False)
                    warns.append(f"USD 可用 ${usd:,.2f} 不够，改用 USDC 支付（{sym}-USDC 与 "
                                 f"{sym}-USD 是同一个订单簿）")
                elif args.spot in ("kraken", "robinhood") and usd < need:
                    blockers.append(f"{args.spot} 可用美元 ${usd:,.2f} < 需要 ${need:,.2f}")
                elif usd < need:
                    blockers.append(f"coinbase 可用 USD ${usd:,.2f}、USDC ${usdc:,.2f}，"
                                    f"单一币种都不够 ${need:,.2f} —— 先在 Coinbase 把一部分换成另一种（1:1）")
            else:
                have = bal.get(sym, 0)
                if have < qty:
                    blockers.append(f"{args.spot} 可卖 {sym} {have:g} < 要卖 {qty:g}"
                                    f"（质押中的币不能直接卖：先在 {args.spot} 解押，等解绑期结束；"
                                    f"期间空单别动，保持对冲）")

        # CDE 保证金。只有现金（USD/USDC）计入，【加密现货不算】—— 无论币在 Coinbase
        # 还是 Kraken。所以在 Coinbase 用 USD 买现货，花掉的那笔钱会直接从 CDE 的
        # 购买力里扣掉；空单再要一份保证金。按隔夜（更高的那档）保证金率算。
        if args.action == "open":
            try:
                bs = cb_auth.call("GET", "/api/v3/brokerage/cfm/balance_summary",
                                  "CDE 保证金")["balance_summary"]
                bp = float((bs.get("futures_buying_power") or {}).get("value") or 0)
                rate = p["margin_short_overnight"] or 0
                spend = notional if args.spot == "coinbase" else 0.0
                need = spend + notional * rate
                warns.append(f"CDE 购买力 ${bp:,.0f}，本笔占用约 ${need:,.0f}"
                             + (f"（含 Coinbase 买现货花掉的 ${spend:,.0f} 现金）" if spend else ""))
                if bp < need:
                    blockers.append(
                        f"CDE 购买力 ${bp:,.0f} < 需要 ${need:,.0f}"
                        + (f"（Coinbase 买现货花掉 ${spend:,.0f} 现金" if spend else "（")
                        + f"{' + ' if spend else ''}空单隔夜保证金 {rate*100:.1f}% × ${notional:,.0f}）")
                elif bp - need < notional * 0.3:
                    warns.append(f"开仓后 CDE 购买力只剩约 ${bp - need:,.0f}，币价上涨 30% 左右就可能"
                                 f"追加保证金 —— 看 perp-track 的强平距离")
            except SourceError as e:
                blockers.append(f"读不到 CDE 保证金：{e}")
            # 叠加 perp-track 的比值闸门（App 口径）：购买力够不等于离传呼线还远
            why, info = margin_gate(add_notional=notional, margin_rate=p["margin_short_overnight"],
                                    spend_cash=notional if args.spot == "coinbase" else 0.0,
                                    note=f"hedge live open {sym} {contracts} 张", register=need_trade,
                                    product_id=p["product_id"], contracts=contracts)
            if info:
                warns.append(info)
            if why:
                blockers.append(why)

        if args.action == "close":
            try:
                held = sum(r["contracts"] for r in coinbase.positions(cb_auth.key_name, cb_auth.key_secret)
                           if r["product"] == p["product_id"] and r["side"] == "SHORT")
                if held < contracts:
                    blockers.append(f"CDE 只有 {held:g} 张空单，要平 {contracts}")
            except SourceError as e:
                blockers.append(f"读不到 CDE 持仓：{e}")

        for name, leg, side, n in (("CDE", perp, "sell" if args.action == "open" else "buy", contracts),
                                   ("现货", spot, "buy" if args.action == "open" else "sell", qty)):
            try:
                pv = leg.preview(side, n)
                previews[name] = pv
                if not pv["ok"]:
                    (blockers if need_trade else warns).append(f"{name} 预检未通过：{pv['errors']}")
            except SourceError as e:
                (blockers if need_trade else warns).append(f"{name} 预检请求失败：{e}")

    f = cfg.fees
    spot_fee = notional * maker
    perp_fee = notional * f.cde_taker + contracts * f.cde_per_contract
    return {"symbol": sym, "product": p["product_id"], "cs": cs, "contracts": contracts,
            "qty": qty, "notional": notional, "spot_mid": sb.mid, "perp_mid": pb.mid,
            "basis": pb.mid / sb.mid - 1, "funding_rate": p["funding_rate"],
            "spot_fee": spot_fee, "perp_fee": perp_fee, "balances": bal,
            "previews": previews, "blockers": blockers, "warnings": warns,
            "legs": (spot, perp)}


def print_plan(plan: dict, args) -> None:
    act = "开仓：买现货 + 卖空" if args.action == "open" else "平仓：卖现货 + 买平空单"
    print(f"{act}  {plan['symbol']}  现货在 {args.spot}，空单在 {args.perp}")
    print(f"  {plan['contracts']} 张 {plan['product']}（每张 {plan['cs']:g}）= {plan['qty']:g} {plan['symbol']}"
          f" ≈ ${plan['notional']:,.0f}")
    print(f"  现货中价 {plan['spot_mid']:g}，CDE 中价 {plan['perp_mid']:g}，基差 {plan['basis']*100:+.3f}%")
    if plan["funding_rate"] is not None:
        print(f"  资金费 {plan['funding_rate']:g}/小时（年化 {plan['funding_rate']*8760*100:.2f}%）")
    if args.spot == "robinhood":
        print(f"  预计手续费：现货吃单（Robinhood 费率档）${plan['spot_fee']:.2f}"
              f" + CDE 吃单 ${plan['perp_fee']:.2f}")
    else:
        print(f"  预计手续费：现货挂单 ${plan['spot_fee']:.2f} + CDE 吃单 ${plan['perp_fee']:.2f}"
              f"（超时改吃单会更贵）")
    for name, pv in plan["previews"].items():
        tag = "✓" if pv["ok"] else "✗"
        print(f"  {name} 交易所预检 {tag}  {pv['errors'] or ''} {pv['warnings'] or ''}")
    for w in plan["warnings"]:
        print(f"  · {w}")
    for b in plan["blockers"]:
        print(f"  ✗ {b}")


def perp_only(cfg: Config, args) -> None:
    """只动 CDE 合约腿，不碰现货。用于：现货在质押/锁定卖不了时减空单（多出的现货
    变成有意持有），或者单独补空单。默认只预检；--go 才下单（终端手输确认）。

    成交方式与 Ladder 的空单跟进一致：吃单，限价逐档放宽 30→100→300bp→市价。
    """
    from .account import classify, hedge_table
    from .ladder import MARKET_BPS
    sym = args.symbol.upper()
    n = args.contracts
    if not n:
        sys.exit("--perp-only 要用 --contracts 指定张数")
    perps = coinbase.cde_perps()
    p = perps.get(sym)
    if not p:
        sys.exit(f"CDE 没有 {sym} 的永续")
    cb_auth, _, notes = trade_auths("coinbase", need_trade=args.go)
    perp = CoinbaseLeg(cb_auth, p["product_id"], is_perp=True)
    side = "buy" if args.action == "close" else "sell"
    cs = p["contract_size"]
    book = perp.book()
    ref = book.best_ask if side == "buy" else book.best_bid
    blockers, warns = [], list(notes)
    if not p["session_open"] or p["trading_disabled"]:
        blockers.append("CDE 现在闭市或停牌")
    if ref is None:
        blockers.append("CDE 订单簿是空的")
    notional = n * cs * (ref or 0)
    fee = notional * cfg.fees.cde_taker + n * cfg.fees.cde_per_contract
    if notional > cfg.live.max_notional_usd:
        blockers.append(f"名义 ${notional:,.0f} 超过上限 ${cfg.live.max_notional_usd:,.0f}")
    if args.action == "open" and ref and (why := fixed_fee_blocker(cfg, cs * ref)):
        _fixed_fee(args, why, blockers, warns)
    if args.action == "open":
        # 裸空的保证金占用是纯增量（没有现货抵消）—— 原来这条路径一点保证金检查都没有
        why, info = margin_gate(add_notional=notional, margin_rate=p["margin_short_overnight"],
                                spend_cash=0.0, note=f"hedge live --perp-only open {sym} {n} 张",
                                register=bool(args.go), product_id=p["product_id"], contracts=n)
        if info:
            warns.append(info)
        if why:
            blockers.append(why)
    if args.action == "close":
        try:
            held = sum(r["contracts"] for r in coinbase.positions(cb_auth.key_name, cb_auth.key_secret)
                       if r["product"] == p["product_id"] and r["side"] == "SHORT")
            if held < n:
                blockers.append(f"CDE 只有 {held:g} 张空单，要平 {n}")
        except SourceError as e:
            blockers.append(f"读不到 CDE 持仓：{e}")
    try:
        pv = perp.preview(side, n)
        if not pv["ok"]:
            (blockers if args.go else warns).append(f"CDE 预检未通过：{pv['errors']}")
    except SourceError as e:
        (blockers if args.go else warns).append(f"CDE 预检请求失败：{e}")
    # 平/开之后的对冲状态：现货不动，只变空单
    hedge_line = "  对冲：读不全持仓，没算"
    try:
        rows, _, errs = hedge_table(perps)
        row = next((r for r in rows if r["asset"] == sym), None)
        if row and not errs:
            d = n * cs * (1 if args.action == "close" else -1)
            after = dict(row, perp=row["perp"] + d, delta=row["delta"] + d)
            a = classify([after], cfg)[0]
            hedge_line = (f"  对冲：现货 {row['spot']:g} 不动，CDE {row['perp']:g} → {after['perp']:g}，"
                          f"净 {row['delta']:+g} → {after['delta']:+g}"
                          f"（≈ ${after['delta'] * (ref or 0):+,.0f}，{a['flag']}）")
            if a["problem"]:
                blockers.append(f"做完后 {sym} 会变成「{a['flag']}」并触发报警 —— "
                                f"多出的现货若是有意持有，先把 {sym} 加进 config.toml 的 hold_only")
    except SourceError:
        pass
    act = "买平空单" if side == "buy" else "卖出开空"
    print(f"只动合约腿：CDE {act} {n} 张 {p['product_id']}（每张 {cs:g} {sym}）≈ ${notional:,.0f}")
    print(f"  对手价 {ref:g}，预计手续费 ${fee:.2f}（吃单 + 每张固定费）")
    print(hedge_line)
    for w in warns:
        print(f"  · {w}")
    for b in blockers:
        print(f"  ✗ {b}")
    if blockers:
        sys.exit("\n有阻断项，不下单。")
    if not args.go:
        print("\n以上是预检，没有下单。确认无误后加 --go 真实执行。")
        return
    if not sys.stdin.isatty():
        sys.exit("真实下单必须在终端里手动确认")
    want = f"{sym} {n} 合约"
    if input(f"\n真实下单（只动合约）。确认请输入「{want}」：").strip() != want:
        sys.exit("没确认，不下单")

    db = sqlite3.connect(cfg.db_path.parent / "live.db")
    db.executescript(SCHEMA)
    for col in ("mode", "perp_venue"):
        try:
            db.execute(f"alter table runs add column {col} text")
        except sqlite3.OperationalError:
            pass
    now = lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    run_id = db.execute("insert into runs(started,symbol,action,spot_venue,contracts,status,mode,"
                        "perp_venue) values(?,?,?,?,?,?,?,?)",
                        (now(), sym, f"perp-{args.action}", "none", n, "running", "live",
                         "cde")).lastrowid
    db.commit()
    done, cost, fees = 0, 0.0, 0.0
    for bps in list(cfg.live.hedge_escalation_bps) + [MARKET_BPS]:
        want_n = n - done
        if want_n <= 0:
            break
        b = perp.book()
        r0 = b.best_ask if side == "buy" else b.best_bid
        if r0 is None and bps < MARKET_BPS:
            # M5：簿子这一侧空了（闭市/薄盘），原来 None * … 直接 TypeError，已成交的部分没记账。
            # 限价档没有参考价就跳过，交给下一档；最后一档是市价，不需要参考价
            print(f"  CDE {'卖' if side == 'buy' else '买'}盘是空的，跳过 {bps:g}bp 档")
            continue
        limit = None if bps >= MARKET_BPS else (r0 * (1 + bps / 1e4) if side == "buy"
                                                else r0 * (1 - bps / 1e4))
        try:
            st = perp.take(side, want_n, limit)
        except OrderUnknown as u:
            # 结果不明：对账，不重试（审查 C1）。对不上就停手，报出订单号
            try:
                st, _ = settle(perp, u, sleep=time.sleep, say=print)
            except Unreconciled as e:
                db.execute("update runs set ended=?, status=?, summary=? where id=?",
                           (now(), "unknown", json.dumps({"contracts": done, "error": str(e)}), run_id))
                db.commit()
                sys.exit(f"\n✗ 订单状态对不上账，已停手（不再下单）：{e}\n"
                         f"  先 hedge account 和 Coinbase 页面核对，已确认成交 {done}/{n} 张")
        got = int(round(st.filled))
        if got:
            done += got
            cost += got * cs * (st.avg or 0)
            fees += st.fee
            db.execute("insert into fills(run_id,ts,venue,side,qty,price,fee,order_id,kind)"
                       " values(?,?,?,?,?,?,?,?,?)",
                       (run_id, now(), "cde", side, got, st.avg, st.fee, None, "taker"))
            db.commit()
            print(f"  CDE {act} {got} 张 @ {st.avg:g}（累计 {done}/{n}"
                  f"{'，' + format(bps, 'g') + 'bp 档' if bps != cfg.live.hedge_escalation_bps[0] else ''}）")
    status = "done" if done == n else "partial"
    db.execute("update runs set ended=?, status=?, summary=? where id=?",
               (now(), status, json.dumps({"contracts": done, "avg": cost / (done * cs) if done else None,
                                           "fees": fees}), run_id))
    db.commit()
    print(f"\n结果：{status}，{done}/{n} 张"
          + (f" @ {cost / (done * cs):g}，手续费 ${fees:.2f}" if done else ""))
    print("核对两边：hedge account")


def execute(cfg: Config, args) -> None:
    if getattr(args, "perp_only", False):
        return perp_only(cfg, args)
    if not args.spot:
        sys.exit("要指定 --spot（或者只动合约腿用 --perp-only）")
    paper = args.paper
    plan = preflight(cfg, args, need_trade=args.go)
    print_plan(plan, args)
    if plan["blockers"]:
        sys.exit("\n有阻断项，不下单。")
    if paper:
        return _run(cfg, args, plan, mode="paper")
    if not args.go:
        print("\n以上是预检，没有下单。确认无误后加 --go 真实执行，或加 --paper 模拟执行。")
        return
    if not sys.stdin.isatty():
        sys.exit("真实下单必须在终端里手动确认，不接受管道/脚本输入")
    want = f"{plan['symbol']} {plan['contracts']}"
    if input(f"\n真实下单。确认请输入「{want}」：").strip() != want:
        sys.exit("没确认，不下单")
    _run(cfg, args, plan, mode="live")


def _run(cfg: Config, args, plan: dict, *, mode: str) -> None:
    db = sqlite3.connect(cfg.db_path.parent / "live.db")
    db.executescript(SCHEMA)
    try:
        db.execute("alter table runs add column mode text")
    except sqlite3.OperationalError:
        pass                                        # 已经有这一列
    try:
        db.execute("alter table runs add column perp_venue text")
    except sqlite3.OperationalError:
        pass
    now = lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    run_id = db.execute("insert into runs(started,symbol,action,spot_venue,contracts,status,mode,"
                        "perp_venue) values(?,?,?,?,?,?,?,?)",
                        (now(), plan["symbol"], args.action, args.spot, plan["contracts"],
                         "running", mode, args.perp)).lastrowid
    db.commit()

    # 模拟成交同时记进模拟盘，这样 paper status 能看到仓位、watch 会按整点记资金费
    pp = q = None
    if mode == "paper":
        from .executor import Fill
        from .market import fetch_quotes
        from .paper import Paper
        pp = Paper(cfg)
        quotes, skipped = fetch_quotes(cfg, [plan["symbol"]])
        if quotes:
            q = quotes[0]
            pp.accrue(q)                            # 先结清旧仓位的资金费，再改仓位
        else:
            print(f"  · 模拟盘记账跳过（{'；'.join(skipped)}），成交只记 live.db")

    def on_fill(**f):
        # 每笔成交立刻落盘 —— 进程中途死掉也能知道到底成交了什么
        db.execute("insert into fills(run_id,ts,venue,side,qty,price,fee,order_id,kind)"
                   " values(?,?,?,?,?,?,?,?,?)", (run_id, now(), f["venue"], f["side"], f["qty"],
                                                  f["price"], f["fee"], f["order_id"], f["kind"]))
        db.commit()
        if pp and q:
            pp._record(q, f"live-{args.action}", [Fill(f["venue"], f["side"], f["qty"], f["price"],
                                                       f["fee"], now())],
                       dt.datetime.now(dt.timezone.utc).replace(microsecond=0))

    spot, perp = plan["legs"]
    lv = cfg.live
    ladder = Ladder(spot, perp, action=args.action, contracts=plan["contracts"],
                    contract_size=plan["cs"], on_fill=on_fill,
                    params=Params(poll_s=lv.poll_s, reprice_min_s=lv.reprice_min_s,
                                  max_wait_s=args.max_wait or lv.max_wait_s,
                                  finish=args.finish, clip_usd=lv.clip_usd,
                                  hedge_escalation_bps=lv.hedge_escalation_bps))
    print()
    r = ladder.run()
    resid = r.residual(plan["cs"])
    summary = {"max_unhedged": r.max_unhedged, "error": r.error, "spot_qty": r.spot_qty, "spot_avg": r.spot_avg, "spot_fees": r.spot_fees,
               "contracts": r.contracts, "perp_avg": r.perp_cost / (r.contracts * plan["cs"])
               if r.contracts else None, "perp_fees": r.perp_fees, "residual": resid,
               "spot_orders": r.spot_orders}
    db.execute("update runs set ended=?, status=?, summary=? where id=?",
               (now(), r.status, json.dumps(summary), run_id))
    db.commit()

    print(f"\n结果：{r.status}{'（模拟）' if mode == 'paper' else ''}")
    print(f"  现货 {r.spot_qty:g} @ {r.spot_avg or 0:g}，手续费 ${r.spot_fees:.2f}，"
          f"{'下单' if args.spot == 'robinhood' else '挂单'} {r.spot_orders} 次")
    if r.contracts:
        print(f"  CDE {r.contracts} 张 @ {summary['perp_avg']:g}，手续费 ${r.perp_fees:.2f}")
        print(f"  成交基差 {(summary['perp_avg'] / r.spot_avg - 1) * 100:+.3f}%" if r.spot_avg else "")
    if abs(resid) > spot.lot:
        print(f"  ⚠ 未对冲零头 {resid:+g} {plan['symbol']}（≈ ${abs(resid) * plan['spot_mid']:,.0f}）")
    print(f"  运行中最大未对冲 {r.max_unhedged:g} {plan['symbol']}"
          f"（≈ ${r.max_unhedged * plan['spot_mid']:,.0f}）")
    if r.status == "unwound":
        print("  空单跟不上，已把没对冲的现货退掉（回到零敞口），手续费和价差是这次的成本")
    if r.status == "unknown":
        print(f"  ✗ 有一笔单子对不上账，已停手（没有继续对冲或回滚）：{r.error}\n"
              f"  先 hedge account 和交易所页面核对这笔订单，再决定补单还是平掉")
    if r.status in ("aborted", "error"):
        print(f"  ✗ {r.error or '空单没跟上且现货回滚失败'} —— 先 hedge account 看两边，"
              f"再手动补 CDE 或卖掉现货")
    print("  看模拟仓位：hedge paper status" if mode == "paper" else "  核对两边：hedge account")
