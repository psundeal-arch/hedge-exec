"""hedge 命令行。

  hedge market [SYM ...] [--usd N]     两腿行情、基差、资金费、一轮成本与回本天数
  hedge account                        实盘只读：现货（Kraken + Robinhood）vs CDE 空单是否对齐
  hedge live open|close SYM --spot coinbase|kraken|robinhood --perp cde [--usd N | --contracts N] [--paper | --go]
  hedge live close SYM --perp-only --perp cde --contracts N [--go]   只平空单、不动现货
                                       开平仓：现货挂单 + 空单按成交跟进。默认只预检，
                                       --paper 模拟执行，--go 真实下单
  hedge funding [--sign neg|pos|all] [--min-oi N] [--top N]   CDE 资金费排行（美国可交易）
  hedge scan [SYM ...] [--perp-track] [--paper]   批量体检；--paper 逐个模拟实跑开+平
  hedge watch [--dry-run]              巡检：对齐报警 + 模拟盘记资金费（timer 每 15 分钟跑）
  hedge import-cb-key ~/Downloads/xxx.json   导入 Coinbase 交易 key 到 .env.trade
  hedge rh-order buy|sell SYM --qty N [--limit P] [--go]   Robinhood 现货下单（默认只预检）
  hedge rh-keygen                      生成 Robinhood API 密钥对：私钥写进 .env，打印公钥
  hedge paper open SYM [--usd N | --contracts N]
  hedge paper close SYM [--pct P]
  hedge paper tick                     记资金费（每小时跑一次）
  hedge paper status                   模拟仓位与盈亏
  hedge paper history                  成交与已平仓记录
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from . import coinbase
from .account import classify, hedge_table
from .config import load_config, load_env
from .executor import ExecError, LegError
from .market import contracts_for_usd, estimate, fetch_quotes
from .paper import Paper


def _pct(v, digits=2):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _usd(v, digits=2):
    if v is None:
        return "—"
    return f"-${-v:,.{digits}f}" if v < 0 else f"${v:,.{digits}f}"


def _table(rows: list[list[str]], header: list[str]) -> None:
    w = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    print("  ".join(str(h).ljust(w[i]) for i, h in enumerate(header)))
    print("  ".join("-" * x for x in w))
    for r in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))


def _quote_one(cfg, sym: str):
    quotes, skipped = fetch_quotes(cfg, [sym.upper()])
    if not quotes:
        sys.exit("；".join(skipped) or f"{sym} 取不到行情")
    return quotes[0]


# ----------------------------------------------------------------- market
def cmd_market(cfg, args) -> None:
    syms = [s.upper() for s in args.symbols] or list(cfg.symbols)
    usd = args.usd or cfg.default_usd
    quotes, skipped = fetch_quotes(cfg, syms)
    rows = []
    for q in sorted(quotes, key=lambda q: -(q.funding_apr or -9)):
        n = contracts_for_usd(q, usd)
        e = estimate(q, n, cfg.fees)
        be = e.get("breakeven_days")
        rows.append([
            q.symbol, q.product_id, "开" if q.session_open else "闭",
            f"{q.spot.mid:,.6g}" if q.spot.mid else "—",
            f"{q.perp.mid:,.6g}" if q.perp.mid else "—",
            _pct(q.basis, 3), _pct(q.funding_apr), _pct(q.margin_short, 1),
            f"{n}张", e.get("error") or _pct(e.get("roundtrip_pct")),
            _usd(e.get("funding_per_day")),
            "—" if be is None else f"{be:.1f}天",
        ])
    print(f"单腿规模约 {_usd(usd, 0)}，成本 = 开+平两次、两腿全吃单（手续费 + 吃簿子的滑点）\n")
    _table(rows, ["标的", "CDE合约", "时段", "Kraken中价", "CDE中价", "基差",
                  "资金费年化", "空单隔夜保证金", "张数", "一轮成本", "资金费/天", "回本"])
    print("\n资金费年化 = 当前单期费率 × 每年期数。单点读数受 1e-6 量化影响，"
          "低费率标的看 perp-track 的 24h 均值。")
    for s in skipped:
        print(f"  跳过 {s}")


# ----------------------------------------------------------------- account
def cmd_account(cfg, args) -> None:
    perps = coinbase.cde_perps()
    rows, notes, errors = hedge_table(perps)
    out = [[r["asset"], f"{r['spot']:g}",
            ", ".join(f"{k} {v:g}" for k, v in sorted(r["spot_parts"].items())) or "—",
            f"{r['perp']:g}", f"{r['delta']:+g}", _usd(r["delta_usd"]),
            _pct(r["mismatch"], 1), r["flag"], r["hint"]]
           for r in classify(rows, cfg)]
    if out:
        _table(out, ["标的", "现货合计", "构成", "CDE(负=空)", "净delta",
                     "净delta美元", "偏差", "状态", "整张调整"])
    else:
        print("没有任何持仓（或凭据都没配）")
    for e in errors:
        print(f"  ✗ {e}")
    if errors:
        print("  ↑ 有来源没读到，上表的对齐结果不可信")
    for n in notes:
        print(f"  · {n}")


# ----------------------------------------------------------------- scan
# perp-track 是另一个仓（2026-10-11 拆开）：scan --perp-track 读它的 perp-track.toml，
# scan / funding 读它的 perp.db。本机位置用 PERP_TRACK_HOME 指（默认 ~/git/hedge-bot ——
# 拆仓时 perp-track 留在了原目录）；服务器上是 ~/perp-track
PERP_TRACK = Path(os.environ.get("PERP_TRACK_HOME") or Path.home() / "git" / "hedge-bot")


def cmd_scan(cfg, args) -> None:
    import csv
    import datetime as dt
    import json
    import tomllib
    from .scan import paper_sweep, static_scan
    syms = [s.upper() for s in args.symbols]
    if args.perp_track:
        with open(PERP_TRACK / "perp-track.toml", "rb") as f:
            syms += tomllib.load(f)["perp"]["funding_underlyings"]
    if not syms:
        syms = sorted(coinbase.cde_perps())
    syms = list(dict.fromkeys(syms))
    db = Path(args.funding_db).expanduser() if args.funding_db else PERP_TRACK / "perp.db"
    rows = static_scan(cfg, syms, db, exit_usd=getattr(args, "exit_usd", None))
    ok = [r for r in rows if "product" in r]
    ok.sort(key=lambda r: -(r["capital_apy"] or -99))
    print(f"资金费均值来自 {db}（{'有' if any(r.get('n24') for r in ok) else '无'} 24h 样本）；"
          f"一块 = ≤${cfg.live.clip_usd:,.0f}，成本 = 现货挂单×2 + CDE 吃单×2 + 每张固定费 + CDE 半价差×2\n")
    _table([[r["symbol"], f"${r['per_contract']:,.0f}", r["clip_n"],
             f"{r['spot_spread_bp']:.1f}", f"{r['perp_spread_bp']:.1f}",
             f"${r['perp_depth_usd'] / 1000:,.0f}K",
             "不够" if r["exit_impact_bp"] is None else f"{r['exit_impact_bp']:.1f}",
             "—" if r.get("depth_share") is None else f"{r['depth_share']:.0%}",
             "—" if r["perp_adv_share"] is None else f"{r['perp_adv_share']:.2%}",
             "—" if r["spot_adv_share"] is None else f"{r['spot_adv_share']:.2%}",
             f"{r['fixed_fee_bp']:.1f}", f"{r['basis_bp']:+.1f}",
             f"{r['apr_now']:.1f}%", "—" if r["apr_24h"] is None else f"{r['apr_24h']:.1f}%",
             "—" if r["apr_7d"] is None else f"{r['apr_7d']:.1f}%",
             f"{r['margin'] * 100:.0f}%", f"{r['capital_apy']:.1f}%",
             f"{r['cost_pct']:.2f}%", "—" if r["breakeven_d"] is None else f"{r['breakeven_d']:.1f}",
             f"${r['oi_usd'] / 1e6:,.1f}M", "，".join(r["flags"])] for r in ok],
           ["标的", "每张", "块张数", "现货价差bp", "CDE价差bp", "CDE深度30bp",
            "出场冲击bp", "占簿深30bp", "CDE参与率", "现货参与率", "固定费bp/边", "基差bp",
            "费率当前", "24h均", "7d均", "空单保证金", "资本年化", "一轮成本", "回本天",
            "OI", "提示"])
    print("\n资本年化 = 24h 均费率 ÷ (1 + 空单隔夜保证金率)：现货全额 + 空单保证金都算占用。")
    ex = ok[0]["exit_usd"] if ok else cfg.live.exit_usd
    print(f"容量按【出场 ${ex:,.0f}】算（--exit-usd 改）：出场冲击 = 一口气买回这么多空单，CDE 卖盘成交均价比中价贵多少；"
          f"占簿深 = 出场额 ÷ CDE 卖盘 30bp 内深度（主门槛 {cfg.live.max_depth_share:.0%}）；"
          f"参与率 = 出场额 ÷ 24h 成交额（兜底 {cfg.live.max_adv_share:.0%}）。"
          f"固定费 = ${cfg.fees.cde_per_contract:g}/张 折成 bp/边，超过 {cfg.live.max_fixed_fee_bp:g}bp 拒绝开仓。")
    skipped: dict[str, list[str]] = {}
    for r in rows:
        if "product" not in r:
            skipped.setdefault("，".join(r["flags"]), []).append(r["symbol"])
    for why, ss in skipped.items():
        print(f"  跳过 {len(ss)} 个（{why}）：{' '.join(ss)}")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out = cfg.db_path.parent / f"scan-{stamp}.csv"
    keys = [k for k in ok[0] if k != "flags"] + ["flags"] if ok else []
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in ok:
            w.writerow({**r, "flags": "；".join(r["flags"])})
    print(f"\n已存 {out}")

    if not args.paper:
        return
    todo = [r["symbol"] for r in ok if not any(
        f in ("闭市", "基差 >1%（单位不对？）") for f in r["flags"])]
    print(f"\n模拟实跑 {len(todo)} 个（每个开一块再平掉，挂单最多等 {args.max_wait:g} 秒）：")
    res = paper_sweep(cfg, todo, max_wait=args.max_wait)
    rows2 = []
    for r in res:
        o, c = r.get("open") or {}, r.get("close") or {}
        rows2.append([r["symbol"], r.get("contracts", "—"),
                      f"{o.get('status', '—')}/{c.get('status', '—')}",
                      f"{o.get('secs', 0):.0f}+{c.get('secs', 0):.0f}s",
                      f"{o.get('orders', 0)}+{c.get('orders', 0)}",
                      "补" if o.get("taker_finish") or c.get("taker_finish") else "",
                      "—" if o.get("basis_bp") is None else f"{o['basis_bp']:+.1f}",
                      "—" if c.get("basis_bp") is None else f"{c['basis_bp']:+.1f}",
                      "—" if r.get("cost_bp") is None else f"{r['cost_bp']:.1f}",
                      f"${max(o.get('max_unhedged_usd', 0), c.get('max_unhedged_usd', 0)):,.0f}",
                      r.get("error", "")])
    print()
    _table(rows2, ["标的", "张", "开/平", "耗时", "挂单次数", "超时补齐", "开基差bp", "平基差bp",
                   "实际一轮成本bp", "最大裸敞口", "错误"])
    print("\n实际一轮成本 = (两轮手续费) ÷ 名义 − 基差收益（开仓基差 − 平仓基差）。"
          "模拟挂单成交偏乐观（不考虑同价排队）。")
    (cfg.db_path.parent / f"sweep-{stamp}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))


# ----------------------------------------------------------------- funding
def cmd_funding(cfg, args) -> None:
    """CDE（美国能交易的永续）资金费排行。默认只列均值为负的。

    均值来自 perp-track 每小时采样。默认先从服务器拉一份数据库副本（只读）——
    本机那份可能停了几个小时、合约也不全；拉不到就退回本机，并在输出里说明。
    Kraken 国际站的 PF_ 合约不列：platformsPermitted 里没有美国平台，美国账户交易不了。
    """
    import csv
    import datetime as dt
    import subprocess
    from .scan import cde_funding_table
    db = Path(args.funding_db).expanduser() if args.funding_db else None
    src = str(db) if db else ""
    if db is None and args.server:
        cache = cfg.db_path.parent / "perp-server.db"
        r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                            f"{args.server}:perp-track/perp.db", str(cache)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            db, src = cache, f"{args.server}（刚拉取）"
    if db is None:
        db, src = PERP_TRACK / "perp.db", "本机 perp.db（可能不是最新）"
    rows, meta = cde_funding_table(db)

    def key(r):          # 最长可用窗口的均值；都没有就用当前值
        return next((r[k] for k in ("apr_7d", "apr_24h", "apr_now") if r[k] is not None), 0)
    rows = [r for r in rows if r["oi_usd"] >= args.min_oi]
    if args.sign == "neg":
        rows = [r for r in rows if key(r) < 0]
    elif args.sign == "pos":
        rows = [r for r in rows if key(r) > 0]
    rows.sort(key=key, reverse=args.sign == "pos")
    if args.top:
        rows = rows[:args.top]

    def pct(v):
        return "—" if v is None else f"{v:+.1f}%"
    out = []
    for r in rows:
        avg = r["apr_7d"] if r["apr_7d"] is not None else r["apr_24h"]
        noise = (r["apr_now"] is not None and avg is not None and r["apr_now"] * avg < 0)
        out.append([r["symbol"], r["product"], pct(r["apr_now"]), pct(r["apr_24h"]),
                    pct(r["apr_7d"]), f"${r['per_contract']:,.0f}",
                    f"${r['oi_usd'] / 1e6:,.1f}M",
                    "—" if r["margin_short"] is None else f"{r['margin_short'] * 100:.0f}%",
                    ("当前与均值反号=单点噪音" if noise else "")
                    + ("" if r["session_open"] else " 闭市")])
    span = meta["span"]
    days = (span[1] - span[0]).total_seconds() / 86400 if span else 0
    age = (dt.datetime.now(dt.timezone.utc) - span[1]).total_seconds() / 3600 if span else None
    label = {"neg": "均值为负（多头收钱）", "pos": "均值为正（空头收钱）", "all": "全部"}[args.sign]
    print(f"CDE 永续资金费 · {label} · OI ≥ ${args.min_oi:,.0f} · {len(out)} 个")
    print(f"均值来源：{src}；样本 {days:.1f} 天，最新一条 "
          + (f"{age:.1f} 小时前" if age is not None else "—")
          + ("（「7d」实际不足 7 天）" if days < 7 else "") + "\n")
    if out:
        _table(out, ["标的", "合约", "当前", "24h均", "7d均", "每张", "OI", "空单隔夜保证金", "提示"])
    print("\n年化 = 每小时费率 × 8760。正 = 多付空收，负 = 空付多收。只列美国能交易的 CDE；"
          "Kraken 国际站 PF_ 合约美国账户不可交易，不列。")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    path = cfg.db_path.parent / f"funding-{stamp}.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["symbol"])
        w.writeheader()
        w.writerows(rows)
    print(f"已存 {path}")


# ----------------------------------------------------------------- watch
def cmd_watch(cfg, args) -> None:
    from .watch import run
    if args.test_notify:
        from .notify import send
        ok, fails = send("[hedge-bot] 测试通知", "通道连通性测试，可忽略。", 0)
        print("至少一个通道送达" if ok else "全部失败", *fails, sep="\n  ")
        return
    for line in run(cfg, dry_run=args.dry_run):
        print(line)
    # 模拟盘记资金费：独立 try —— 它坏了不能拖累对齐报警（报警已经跑完了）
    if not args.dry_run and Paper(cfg).positions():
        try:
            cmd_paper(cfg, argparse.Namespace(action="tick"))
        except Exception as e:      # noqa: BLE001
            print(f"模拟盘 tick 失败：{e}")


# ----------------------------------------------------------------- import-cb-key
def cmd_import_cb_key(cfg, args) -> None:
    """把 Coinbase 下载的 key JSON 写进 .env.trade（600）。私钥不经过屏幕和剪贴板。"""
    import base64
    import json
    import os
    from .config import PROJECT_ROOT
    raw = json.loads(open(os.path.expanduser(args.path)).read())
    name = raw.get("name") or raw.get("id")
    secret = raw.get("privateKey")
    if not (name and secret):
        sys.exit(f"JSON 里没有 name/id 与 privateKey（键：{', '.join(raw)}）")
    try:
        n = len(base64.b64decode(secret))
    except ValueError:
        n = -1
    if n != 64:
        sys.exit(f"privateKey 不是 Ed25519 格式（解码 {n} 字节，应为 64）—— 创建时要选 Ed25519")
    env = PROJECT_ROOT / ".env.trade"
    lines = [l for l in (env.read_text().splitlines() if env.exists() else [])
             if not l.startswith(("COINBASE_TRADE_KEY_NAME=", "COINBASE_TRADE_PRIVATE_KEY="))]
    lines += [f"COINBASE_TRADE_KEY_NAME={name}", f"COINBASE_TRADE_PRIVATE_KEY={secret}"]
    env.write_text("\n".join(lines) + "\n")
    os.chmod(env, 0o600)
    print(f"已写入 {env}（600）。key：{name[:24]}…")
    print(f"确认能用之后可以删掉下载的文件：rm {args.path}")


# ----------------------------------------------------------------- rh-keygen
def cmd_rh_keygen(cfg, args) -> None:
    """私钥直接写进文件（600），屏幕上只出现公钥 —— 私钥不经过终端回显和剪贴板。

    默认生成只读 key（写 .env，服务器巡检也用它）；--trade 生成下单 key（写 .env.trade，
    只留本机，deploy 不会带上服务器）。两把分开：服务器被攻破也下不了单。
    """
    import os
    from .config import PROJECT_ROOT
    from .robinhood import keygen
    env = PROJECT_ROOT / (".env.trade" if args.trade else ".env")
    kname, pname = (("RH_TRADE_API_KEY", "RH_TRADE_PRIVATE_KEY") if args.trade
                    else ("RH_API_KEY", "RH_PRIVATE_KEY"))
    text = env.read_text() if env.exists() else ""
    old = next((l for l in text.splitlines() if l.startswith(pname + "=")), "")
    if old.split("=", 1)[-1].strip() and not args.force:
        sys.exit(f"{env.name} 里已有 {pname}。要换新的加 --force（旧 key 在 Robinhood 上记得删掉）")
    priv, pub = keygen()
    lines = [l for l in text.splitlines() if not l.startswith(pname + "=")]
    if not any(l.startswith(kname + "=") for l in lines):
        lines.append(f"{kname}=")
    lines.append(f"{pname}={priv}")
    env.write_text("\n".join(lines) + "\n")
    os.chmod(env, 0o600)
    print(f"私钥已写入 {env.name}（600 权限）。把下面这个【公钥】贴到 Robinhood：\n")
    print(f"    {pub}\n")
    if args.trade:
        print("权限勾：Place crypto orders with fee tiers (v2)、Read crypto accounts、"
              "Read crypto holdings、Read crypto orders、Read crypto quotes、Read crypto products")
    print(f"Robinhood 生成 key 后，把它给你的 API key 填进 {env.name} 的 {kname}=")


# ----------------------------------------------------------------- rh-order
def cmd_rh_order(cfg, args) -> None:
    """Robinhood 现货下单。默认只预检；--go 真实下单（终端手输确认）。"""
    import datetime as dt
    import json
    import os
    import sqlite3
    from .config import PROJECT_ROOT
    from .http import SourceError
    from .rh_trade import RHTrader, fmt_step
    load_env(PROJECT_ROOT / ".env.trade")
    sym, side = args.symbol.upper(), args.side
    pair_sym = f"{sym}-USD"
    key, sec = os.environ.get("RH_TRADE_API_KEY"), os.environ.get("RH_TRADE_PRIVATE_KEY")
    notes, blockers = [], []
    if not (key and sec):
        if args.go:
            sys.exit("缺 RH_TRADE_API_KEY / RH_TRADE_PRIVATE_KEY —— 先 hedge rh-keygen --trade")
        key, sec = os.environ.get("RH_API_KEY"), os.environ.get("RH_PRIVATE_KEY")
        notes.append("没有 Robinhood 交易 key，用只读 key 预检（报价/规格可能因权限不足读不到）")
    if not (key and sec):
        sys.exit("连只读的 Robinhood key 都没有")
    t = RHTrader(key, sec)

    try:
        pr = t.pair(pair_sym)
        qty_str = fmt_step(args.qty, pr["step"])
        qty = float(qty_str)
        if qty <= 0 or qty > pr["max"]:
            blockers.append(f"数量 {qty_str} 超出 0–{pr['max']:g}")
    except SourceError as e:
        pr, qty_str, qty = None, f"{args.qty:g}", args.qty
        blockers.append(f"读不到 {pair_sym} 交易规格：{e}")
    try:
        q = t.quote(pair_sym, side, qty)
    except SourceError as e:
        q = None
        blockers.append(f"读不到报价：{e}")
    notional = qty * q["price"] if q else None
    if notional is not None and pr and notional < pr["min_usd"]:
        blockers.append(f"金额 ${notional:.2f} 低于最小下单金额 ${pr['min_usd']:g}")
    if notional and notional > cfg.live.max_notional_usd:
        blockers.append(f"金额 ${notional:,.0f} 超过上限 ${cfg.live.max_notional_usd:,.0f}")
    from .live import leftover_orders
    from .rh_trade import RobinhoodLeg
    try:
        blockers += leftover_orders(RobinhoodLeg(t, pair_sym), notes)
    except SourceError as e:
        notes.append(f"查不到 Robinhood 未完成订单：{e}")
    limit_str = None
    if args.limit is not None:
        limit_str = fmt_step(args.limit, pr["tick"]) if pr else f"{args.limit:g}"
    if side == "buy" and notional:
        try:
            bp = float(t.account().get("buying_power") or 0)
            fee = float(q.get("fee") or 0)
            if bp < (notional + fee) * 1.01:
                blockers.append(f"Robinhood 购买力 ${bp:,.2f} < 约需 ${notional * 1.01:,.2f}")
        except SourceError as e:
            notes.append(f"读不到购买力：{e}")

    # 下单前后的对冲状态：这一笔是补齐对冲，还是把它打歪
    try:
        rows, _, errs = hedge_table(coinbase.cde_perps())
        row = next((r for r in rows if r["asset"] == sym), None)
        if row and not errs:
            after = dict(row, spot=row["spot"] + (qty if side == "buy" else -qty))
            after["delta"] = after["spot"] + after["perp"]
            b, a = classify([row], cfg)[0], classify([after], cfg)[0]
            hedge_line = (f"  对冲：现货 {row['spot']:g} → {after['spot']:g}，CDE {row['perp']:g}，"
                          f"净 {b['delta']:+g} → {a['delta']:+g}（{a['flag']}）")
        else:
            hedge_line = "  对冲：读不全持仓，下单前后对冲状态没算"
    except SourceError:
        hedge_line = "  对冲：读不全持仓，下单前后对冲状态没算"

    print(f"Robinhood {'买入' if side == 'buy' else '卖出'} {qty_str} {sym}"
          f"（{'限价 ' + limit_str if limit_str else '市价'}）")
    if q:
        sp = f"，盘口价差 {q['spread'] * 100:.3f}%" if q["spread"] is not None else ""
        print(f"  预估成交价 {q['price']:g}（中价 {q['mid']:g}{sp}）≈ ${notional:,.2f}")
        if q.get("fee_ratio") is not None:
            ft = t.account().get("fee_tier_status") or {}
            print(f"  手续费 {float(q['fee_ratio']) * 100:.3f}% ≈ ${float(q['fee'] or 0):.2f}"
                  f"（30 天量 ${float(ft.get('thirty_day_volume') or 0):,.0f}；"
                  f"下一档 {float(ft.get('next_fee_tier_ratio') or 0) * 100:.3f}% 还差 "
                  f"${float(ft.get('next_fee_tier_threshold') or 0):,.0f}）")
    print(hedge_line)
    for n in notes:
        print(f"  · {n}")
    for b in blockers:
        print(f"  ✗ {b}")
    if blockers:
        sys.exit("\n有阻断项，不下单。")
    if not args.go:
        print("\n以上是预检，没有下单。确认无误后加 --go 真实执行。")
        return
    if not sys.stdin.isatty():
        sys.exit("真实下单必须在终端里手动确认")
    want = f"{side.upper()} {sym} {qty_str}"
    if input(f"\n真实下单。确认请输入「{want}」：").strip() != want:
        sys.exit("没确认，不下单")

    o = t.place(side, pair_sym, qty_str, limit_str)
    oid = o.get("id")
    print(f"已提交，订单 {oid}，状态 {o.get('state')}")
    o = t.wait(oid) if oid else o
    filled = float(o.get("filled_asset_quantity") or 0)
    avg = float(o.get("average_price") or 0)
    print(f"结果：{o.get('state')}，成交 {filled:g} {sym} @ {avg:g}"
          + (f" ≈ ${filled * avg:,.2f}" if filled else ""))
    if o.get("state") in ("open", "pending", "partially_filled"):
        print("  还没成交完（限价单会一直挂着）—— 在 Robinhood app 里看/撤")

    db = sqlite3.connect(cfg.db_path.parent / "live.db")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    db.execute("create table if not exists runs(id integer primary key, started text, ended text,"
               " symbol text, action text, spot_venue text, contracts integer, status text,"
               " summary text, mode text)")
    db.execute("create table if not exists fills(id integer primary key, run_id integer, ts text,"
               " venue text, side text, qty real, price real, fee real, order_id text, kind text)")
    rid = db.execute("insert into runs(started,ended,symbol,action,spot_venue,contracts,status,"
                     "summary,mode) values(?,?,?,?,?,?,?,?,?)",
                     (now, now, sym, f"rh-{side}", "robinhood", 0, o.get("state"),
                      json.dumps({"order_id": oid, "qty": qty_str, "limit": limit_str}), "live")).lastrowid
    if filled:
        db.execute("insert into fills(run_id,ts,venue,side,qty,price,fee,order_id,kind)"
                   " values(?,?,?,?,?,?,?,?,?)", (rid, now, "robinhood", side, filled, avg,
                                                  float(o.get("fee_charged") or 0), oid,
                                                  "limit" if limit_str else "market"))
    db.commit()
    print("核对两边：hedge account")


# ----------------------------------------------------------------- paper
def _print_fills(fills) -> None:
    for f in fills:
        print(f"  {f.venue:6} {f.side:4} {f.qty:g} @ {f.price:,.6g}  手续费 {_usd(f.fee)}")


def cmd_paper(cfg, args) -> None:
    pp = Paper(cfg)
    if args.action == "open":
        q = _quote_one(cfg, args.symbol)
        n = args.contracts or contracts_for_usd(q, args.usd or cfg.default_usd)
        try:
            fills = pp.open(q, n)
        except LegError as e:
            print(f"✗ {e}")
            _print_fills(e.fills)
            sys.exit(2)
        except ExecError as e:
            sys.exit(f"✗ 没下单：{e}")
        print(f"✓ 模拟开仓 {q.symbol}：卖 {n} 张 {q.product_id}，买 {fills[1].qty:g} 现货")
        _print_fills(fills)
        print(f"  开仓基差 {_pct(fills[0].price / fills[1].price - 1, 3)}")
    elif args.action == "close":
        q = _quote_one(cfg, args.symbol)
        try:
            fills = pp.close(q, args.pct)
        except LegError as e:
            print(f"✗ {e}")
            _print_fills(e.fills)
            sys.exit(2)
        except ExecError as e:
            sys.exit(f"✗ 没下单：{e}")
        print(f"✓ 模拟平仓 {q.symbol} {args.pct:g}%")
        _print_fills(fills)
        if not pp.position(q.symbol):
            c = pp.closed()[-1]
            print(f"  已全部平仓：价格 {_usd(c['realized'])}  手续费 -{_usd(c['fees'])}"
                  f"  资金费 {_usd(c['funding'])}  净 {_usd(c['net'])}")
    elif args.action == "tick":
        pos = pp.positions()
        if not pos:
            print("没有模拟仓位")
            return
        quotes, skipped = fetch_quotes(cfg, [p["symbol"] for p in pos])
        for q in quotes:
            amt = pp.accrue(q)
            print(f"{q.symbol}: 记入资金费 {_usd(amt, 4)}"
                  f"（费率 {q.funding_rate}，{'开市' if q.session_open else '闭市按0'}）")
        for s in skipped:
            print(f"  跳过 {s}")
    elif args.action == "status":
        pos = pp.positions()
        if not pos:
            print("没有模拟仓位")
        else:
            quotes, skipped = fetch_quotes(cfg, [p["symbol"] for p in pos])
            qmap = {q.symbol: q for q in quotes}
            rows = []
            for p in pos:
                q = qmap.get(p["symbol"])
                if not q:
                    rows.append([p["symbol"], "取不到行情"] + [""] * 9)
                    continue
                m = Paper.mark(p, q)
                rows.append([
                    p["symbol"], f"{p['spot_qty']:g}", f"{p['contracts']:g}张",
                    f"{m['delta_qty']:+g}", _usd(m["unreal_spot"] + m["unreal_perp"]),
                    _usd(p["realized_cum"]), f"-{_usd(p['fees_cum'])}",
                    _usd(p["funding_cum"], 4), _usd(m["net"]), _usd(m["capital"], 0),
                    _pct(m["apy"]) + f" ({m['days']:.1f}天)"])
            _table(rows, ["标的", "现货", "永续", "净delta", "浮盈", "已实现",
                          "手续费", "资金费", "净盈亏", "占用资本", "年化"])
            for s in skipped:
                print(f"  跳过 {s}")
            print("\n浮盈按立即平仓价估（现货看买一、空单看卖一），已含退出时的价差，未含退出手续费。")
    elif args.action == "history":
        for f in pp.fills(args.limit):
            print(f"{f['ts']}  {f['symbol']:5} {f['action']:15} {f['venue']:6} "
                  f"{f['side']:4} {f['qty']:g} @ {f['price']:,.6g}  费 {_usd(f['fee'])}")
        closed = pp.closed()
        if closed:
            print("\n已平仓：")
            _table([[c["symbol"], c["opened_at"][:16], c["closed_at"][:16], _usd(c["realized"]),
                     f"-{_usd(c['fees'])}", _usd(c["funding"], 4), _usd(c["net"])]
                    for c in closed],
                   ["标的", "开仓", "平仓", "价格盈亏", "手续费", "资金费", "净"])


def cmd_grid_only(cfg, args) -> None:
    """grid_only 的唯一主人是 hedge-bot 的 config.toml；perp-track 拆仓后经这个命令读它。"""
    import json
    names = sorted(cfg.grid_only)
    print(json.dumps(names) if args.json else " ".join(names))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="hedge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("market", help="两腿行情与成本")
    m.add_argument("symbols", nargs="*")
    m.add_argument("--usd", type=float)
    sub.add_parser("account", help="实盘只读对齐检查")
    lv = sub.add_parser("live", help="真实开/平仓（默认只预检）")
    lv.add_argument("action", choices=["open", "close"])
    lv.add_argument("symbol")
    # 不给默认值：Coinbase 便宜但买现货的现金会从 CDE 保证金里扣掉，Kraken 不扣 —— 每次自己选
    lv.add_argument("--spot", choices=["coinbase", "kraken", "robinhood"],
                    help="现货腿场所（--perp-only 时不用）")
    lv.add_argument("--perp-only", action="store_true",
                    help="只动合约腿、不碰现货：现货质押/锁定卖不了时减空单，或单独补空单")
    # 合约腿也必须写明。现在只有 CDE；以后接 Kraken Futures 就在这里加选项，
    # 免得命令行里看不出空单开在哪个场所
    lv.add_argument("--perp", choices=["cde"], required=True, help="合约（空单）腿所在场所")
    g = lv.add_mutually_exclusive_group(required=True)
    g.add_argument("--usd", type=float)
    g.add_argument("--contracts", type=int)
    lv.add_argument("--finish", choices=["complete", "trim"], default="complete",
                    help="挂单超时后：complete 吃单补齐（默认）/ trim 退掉凑不满一张的零头")
    lv.add_argument("--max-wait", type=float, help="挂单最多等多少秒（默认见 config）")
    lv.add_argument("--accept-fixed-fee", action="store_true",
                    help="每张固定费超过 [live] max_fixed_fee_bp 时，只对这一笔放行（开仓才会被它拦）")
    m = lv.add_mutually_exclusive_group()
    m.add_argument("--go", action="store_true", help="真实下单（会要求手输确认）")
    m.add_argument("--paper", action="store_true",
                   help="模拟执行：盯真实行情、同一套引擎，成交记入模拟盘")
    sc = sub.add_parser("scan", help="批量体检：哪些标的适合 Coinbase 现货 + CDE 空单")
    sc.add_argument("symbols", nargs="*")
    sc.add_argument("--perp-track", action="store_true", help="加上 perp-track 的观察名单")
    sc.add_argument("--funding-db", help="资金费历史库（默认仓库根目录的 perp.db）")
    sc.add_argument("--paper", action="store_true", help="逐个模拟实跑开+平")
    sc.add_argument("--max-wait", type=float, default=90)
    sc.add_argument("--exit-usd", type=float, help="容量按多大的出场算（默认 config [live] exit_usd）")
    go = sub.add_parser("grid-only", help="网格实盘管的合约名单（perp-track 经子进程读它）")
    go.add_argument("--json", action="store_true", help="输出 JSON 数组")
    fu = sub.add_parser("funding", help="CDE 资金费排行（默认只列均值为负的）")
    fu.add_argument("--sign", choices=["neg", "pos", "all"], default="neg")
    fu.add_argument("--min-oi", type=float, default=0, help="OI 下限（美元）")
    fu.add_argument("--top", type=int)
    fu.add_argument("--funding-db", help="资金费历史库；不给就先从服务器拉一份")
    fu.add_argument("--server", default="rhserver", help="perp-track 所在主机；空串 = 不拉")
    w = sub.add_parser("watch", help="巡检与报警")
    w.add_argument("--dry-run", action="store_true", help="只打印，不发通知、不改状态")
    w.add_argument("--test-notify", action="store_true", help="只发一条测试通知")
    ik = sub.add_parser("import-cb-key", help="导入 Coinbase 交易 key 的 JSON 到 .env.trade")
    ik.add_argument("path")
    k = sub.add_parser("rh-keygen", help="生成 Robinhood API 密钥对")
    k.add_argument("--force", action="store_true")
    k.add_argument("--trade", action="store_true", help="生成下单 key（写 .env.trade）")
    ro = sub.add_parser("rh-order", help="Robinhood 现货下单（默认只预检）")
    ro.add_argument("side", choices=["buy", "sell"])
    ro.add_argument("symbol")
    ro.add_argument("--qty", type=float, required=True, help="币的数量")
    ro.add_argument("--limit", type=float, help="限价；不给就是市价单")
    ro.add_argument("--go", action="store_true", help="真实下单（会要求手输确认）")
    from .exec_cli import add_parser as _exec_parser
    _exec_parser(sub)
    p = sub.add_parser("paper", help="模拟盘")
    ps = p.add_subparsers(dest="action", required=True)
    o = ps.add_parser("open")
    o.add_argument("symbol")
    g = o.add_mutually_exclusive_group()
    g.add_argument("--usd", type=float)
    g.add_argument("--contracts", type=int)
    c = ps.add_parser("close")
    c.add_argument("symbol")
    c.add_argument("--pct", type=float, default=100)
    ps.add_parser("tick")
    ps.add_parser("status")
    h = ps.add_parser("history")
    h.add_argument("--limit", type=int, default=30)
    args = ap.parse_args(argv)

    load_env()
    cfg = load_config()
    {"market": cmd_market, "account": cmd_account, "watch": cmd_watch, "scan": cmd_scan,
     "funding": cmd_funding,
     "grid-only": cmd_grid_only,
     "live": lambda c, a: __import__("hedgebot.live", fromlist=["execute"]).execute(c, a),
     "rh-keygen": cmd_rh_keygen, "rh-order": cmd_rh_order, "import-cb-key": cmd_import_cb_key,
     "paper": cmd_paper,
     "exec": lambda c, a: __import__("hedgebot.exec_cli", fromlist=["run"]).run(c, a)}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
