"""hedge 命令行。

  hedge market [SYM ...] [--usd N]     两腿行情、基差、资金费、一轮成本与回本天数
  hedge account                        实盘只读：现货（Kraken + Robinhood）vs CDE 空单是否对齐
  hedge rh-keygen                      生成 Robinhood API 密钥对：私钥写进 .env，打印公钥
  hedge paper open SYM [--usd N | --contracts N]
  hedge paper close SYM [--pct P]
  hedge paper tick                     记资金费（每小时跑一次）
  hedge paper status                   模拟仓位与盈亏
  hedge paper history                  成交与已平仓记录
"""
from __future__ import annotations

import argparse
import sys

from . import coinbase
from .account import hedge_table
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
    rows, notes = hedge_table(perps)
    out = []
    for r in rows:
        px = r["price"]
        mismatch = abs(r["delta"]) / r["spot"] if r["spot"] > 0 else None
        bad = (r["perp"] != 0 and (mismatch is None or mismatch > cfg.hedge_tolerance))
        parts = ", ".join(f"{k} {v:g}" for k, v in sorted(r["spot_parts"].items()))
        out.append([r["asset"], f"{r['spot']:g}", parts or "—", f"{r['perp']:g}",
                    f"{r['delta']:+g}", _usd(r["delta"] * px if px else None),
                    _pct(mismatch, 1), "⚠ 没对齐" if bad else ""])
    if out:
        _table(out, ["标的", "现货合计", "构成", "CDE(负=空)", "净delta",
                     "净delta美元", "偏差", ""])
    else:
        print("没有任何持仓（或两边凭据都没配）")
    for n in notes:
        print(f"  · {n}")


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
    {"market": cmd_market, "account": cmd_account, "rh-keygen": cmd_rh_keygen,
     "paper": cmd_paper}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
