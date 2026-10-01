"""hedge 命令行。

  hedge market [SYM ...] [--usd N]     两腿行情、基差、资金费、一轮成本与回本天数
  hedge account                        实盘只读：现货（Kraken + Robinhood）vs CDE 空单是否对齐
  hedge live open|close SYM --spot coinbase|kraken [--usd N | --contracts N] [--paper | --go]
                                       开平仓：现货挂单 + 空单按成交跟进。默认只预检，
                                       --paper 模拟执行，--go 真实下单
  hedge scan [SYM ...] [--perp-track] [--paper]   批量体检；--paper 逐个模拟实跑开+平
  hedge watch [--dry-run]              巡检：对齐报警 + 模拟盘记资金费（timer 每 15 分钟跑）
  hedge import-cb-key ~/Downloads/xxx.json   导入 Coinbase 交易 key 到 .env.trade
  hedge rh-keygen                      生成 Robinhood API 密钥对：私钥写进 .env，打印公钥
  hedge paper open SYM [--usd N | --contracts N]
  hedge paper close SYM [--pct P]
  hedge paper tick                     记资金费（每小时跑一次）
  hedge paper status                   模拟仓位与盈亏
  hedge paper history                  成交与已平仓记录
"""
from __future__ import annotations

import argparse
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
PERP_TRACK = Path.home() / "git" / "perp-track"


def cmd_scan(cfg, args) -> None:
    import csv
    import datetime as dt
    import json
    import tomllib
    from .scan import paper_sweep, static_scan
    syms = [s.upper() for s in args.symbols]
    if args.perp_track:
        with open(PERP_TRACK / "config.toml", "rb") as f:
            syms += tomllib.load(f)["perp"]["funding_underlyings"]
    if not syms:
        syms = sorted(coinbase.cde_perps())
    syms = list(dict.fromkeys(syms))
    db = Path(args.funding_db).expanduser() if args.funding_db else PERP_TRACK / "perp.db"
    rows = static_scan(cfg, syms, db)
    ok = [r for r in rows if "product" in r]
    ok.sort(key=lambda r: -(r["capital_apy"] or -99))
    print(f"资金费均值来自 {db}（{'有' if any(r.get('n24') for r in ok) else '无'} 24h 样本）；"
          f"一块 = ≤${cfg.live.clip_usd:,.0f}，成本 = 现货挂单×2 + CDE 吃单×2 + 每张固定费 + CDE 半价差×2\n")
    _table([[r["symbol"], f"${r['per_contract']:,.0f}", r["clip_n"],
             f"{r['spot_spread_bp']:.1f}", f"{r['perp_spread_bp']:.1f}",
             f"${r['perp_depth_usd'] / 1000:,.0f}K", f"{r['basis_bp']:+.1f}",
             f"{r['apr_now']:.1f}%", "—" if r["apr_24h"] is None else f"{r['apr_24h']:.1f}%",
             "—" if r["apr_7d"] is None else f"{r['apr_7d']:.1f}%",
             f"{r['margin'] * 100:.0f}%", f"{r['capital_apy']:.1f}%",
             f"{r['cost_pct']:.2f}%", "—" if r["breakeven_d"] is None else f"{r['breakeven_d']:.1f}",
             f"${r['oi_usd'] / 1e6:,.1f}M", "，".join(r["flags"])] for r in ok],
           ["标的", "每张", "块张数", "现货价差bp", "CDE价差bp", "CDE深度30bp", "基差bp",
            "费率当前", "24h均", "7d均", "空单保证金", "资本年化", "一轮成本", "回本天",
            "OI", "提示"])
    print("\n资本年化 = 24h 均费率 ÷ (1 + 空单隔夜保证金率)：现货全额 + 空单保证金都算占用。")
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
    """私钥直接写进 .env（600），屏幕上只出现公钥 —— 私钥不经过终端回显和剪贴板。"""
    import os
    from .config import PROJECT_ROOT
    from .robinhood import keygen
    env = PROJECT_ROOT / ".env"
    text = env.read_text() if env.exists() else ""
    if "RH_PRIVATE_KEY=" in text and not args.force:
        line = next(l for l in text.splitlines() if l.startswith("RH_PRIVATE_KEY="))
        if line.split("=", 1)[1].strip():
            sys.exit(".env 里已有 RH_PRIVATE_KEY。要换新的加 --force"
                     "（旧 key 在 Robinhood 上记得删掉）")
    priv, pub = keygen()
    lines = [l for l in text.splitlines() if not l.startswith("RH_PRIVATE_KEY=")]
    if not any(l.startswith("RH_API_KEY=") for l in lines):
        lines.append("RH_API_KEY=")
    lines.append(f"RH_PRIVATE_KEY={priv}")
    env.write_text("\n".join(lines) + "\n")
    os.chmod(env, 0o600)
    print("私钥已写入 .env（600 权限）。把下面这个【公钥】贴到 Robinhood：\n")
    print(f"    {pub}\n")
    print("Robinhood 生成 key 后，把它给你的 API key 填进 .env 的 RH_API_KEY=")


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
    lv.add_argument("--spot", choices=["coinbase", "kraken"], required=True)
    g = lv.add_mutually_exclusive_group(required=True)
    g.add_argument("--usd", type=float)
    g.add_argument("--contracts", type=int)
    lv.add_argument("--finish", choices=["complete", "trim"], default="complete",
                    help="挂单超时后：complete 吃单补齐（默认）/ trim 退掉凑不满一张的零头")
    lv.add_argument("--max-wait", type=float, help="挂单最多等多少秒（默认见 config）")
    m = lv.add_mutually_exclusive_group()
    m.add_argument("--go", action="store_true", help="真实下单（会要求手输确认）")
    m.add_argument("--paper", action="store_true",
                   help="模拟执行：盯真实行情、同一套引擎，成交记入模拟盘")
    sc = sub.add_parser("scan", help="批量体检：哪些标的适合 Coinbase 现货 + CDE 空单")
    sc.add_argument("symbols", nargs="*")
    sc.add_argument("--perp-track", action="store_true", help="加上 perp-track 的观察名单")
    sc.add_argument("--funding-db", help="资金费历史库（默认 ~/git/perp-track/perp.db）")
    sc.add_argument("--paper", action="store_true", help="逐个模拟实跑开+平")
    sc.add_argument("--max-wait", type=float, default=90)
    w = sub.add_parser("watch", help="巡检与报警")
    w.add_argument("--dry-run", action="store_true", help="只打印，不发通知、不改状态")
    w.add_argument("--test-notify", action="store_true", help="只发一条测试通知")
    ik = sub.add_parser("import-cb-key", help="导入 Coinbase 交易 key 的 JSON 到 .env.trade")
    ik.add_argument("path")
    k = sub.add_parser("rh-keygen", help="生成 Robinhood API 密钥对")
    k.add_argument("--force", action="store_true")
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
     "live": lambda c, a: __import__("hedgebot.live", fromlist=["execute"]).execute(c, a),
     "rh-keygen": cmd_rh_keygen, "import-cb-key": cmd_import_cb_key,
     "paper": cmd_paper}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
