"""hedge exec 命令：提交母单（预检 + 终端手输确认）/ 状态 / 取消 / 报告 / 后台 worker。

交易 key：服务器上读 HEDGE_EXEC_KEYS 指向的文件（默认 ~/perp-grid-live/.env.trade —— 已绑定服务器 IP、
View + Trade、无 Transfer 的 Coinbase key）；本机没有这个文件时照旧读本机 .env.trade。
Robinhood 下单要一把绑定服务器 IP 的 RH key（第二阶段）。
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from . import exec_signal
from .config import Config, load_env
from .exec_sched import Job, JobParams, Scheduler, Store, parse_when

KINDS = {"open": "hedge_open", "close": "hedge_close", "buy": "spot_buy", "sell": "spot_sell"}


def load_exec_keys() -> None:
    p = Path(os.environ.get("HEDGE_EXEC_KEYS") or Path.home() / "perp-grid-live" / ".env.trade")
    if p.exists():
        load_env(p)


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path.parent / "live.db")


def _fmt_t(t: float) -> str:
    return time.strftime("%m-%d %H:%M UTC", time.gmtime(t))


def submit(cfg: Config, args) -> None:
    from .live import build, preflight, print_plan
    load_exec_keys()
    kind = KINDS[args.action]
    sym = args.symbol.upper()
    now = time.time()
    start = parse_when(args.start or "now", now)
    end = parse_when(args.by, now)
    if end - start < 600:
        sys.exit("时间窗太短：截止要比开始晚至少 10 分钟")
    dry = args.dry
    params = JobParams(limit=args.limit, basis_min_bp=args.basis_min, finish=args.finish,
                       dip_k=args.dip_k, chase_k=args.chase_k, min_slices=args.slices, clip_usd=cfg.live.clip_usd)
    blockers = []
    if kind.startswith("hedge"):
        if sym in cfg.grid_only:
            sys.exit(f"{sym} 在 grid_only（perp-grid 实盘网格在交易），不能用 hedge exec 做对冲")
        if not args.contracts:
            sys.exit("对冲任务要给张数：hedge exec open ZEC 2 ...")
        ns = SimpleNamespace(symbol=sym, spot=args.venue, perp="cde", contracts=args.contracts, usd=None,
                             action=args.action, max_wait=300, paper=dry, finish="complete")
        plan = preflight(cfg, ns, need_trade=False)
        print_plan(plan, ns)
        blockers = plan["blockers"]
        total, unit = float(args.contracts), "张"
        mid = plan["spot_mid"]
        notional = plan["notional"]
    else:
        _, spot, _, _, avail, _, notes = build(cfg, sym, args.venue, need_trade=False, paper=dry)
        mid = spot.book().mid
        if not mid:
            sys.exit("现货盘口是空的")
        qty = args.qty if args.qty else (args.usd / mid if args.usd else 0)
        total = float(qty)
        unit = sym
        notional = qty * mid
        if qty < spot.min_qty:
            blockers.append(f"数量 {qty:g} 低于最小下单量 {spot.min_qty:g}")
        if notional > cfg.live.max_notional_usd:
            blockers.append(f"名义 ${notional:,.0f} 超过上限 ${cfg.live.max_notional_usd:,.0f}")
        if not dry and avail:
            try:
                bal = avail()
                if kind == "spot_buy" and bal.get("USD", 0) < notional * 1.01:
                    blockers.append(f"{args.venue} 可用 USD ${bal.get('USD', 0):,.2f} < 约 ${notional * 1.01:,.2f}")
                if kind == "spot_sell" and bal.get(sym, 0) < qty:
                    blockers.append(f"{args.venue} 可卖 {sym} {bal.get(sym, 0):g} < {qty:g}")
            except Exception as e:                    # noqa: BLE001
                blockers.append(f"读不到 {args.venue} 余额：{e}")
        print(f"{'买入' if kind == 'spot_buy' else '卖出'} {sym} 现货 {qty:g}（≈ ${notional:,.0f}）在 {args.venue}，现价 {mid:g}")
        for n in notes:
            print(f"  · {n}")
    if params.limit is None and mid:                  # 默认价格保护：开始时价格 ±2%
        params.limit = float(f"{mid * (1.02 if kind in ('hedge_open', 'spot_buy') else 0.98):.6g}")
    slices = max(1, params.min_slices)
    print(f"\n执行窗口 {_fmt_t(start)} → {_fmt_t(end)}（{(end - start) / 3600:.1f} 小时），至少分 {slices} 份")
    print(f"  逢{'低' if KINDS[args.action] in ('hedge_open', 'spot_buy') else '高'}：偏离当日 VWAP ≥ {params.dip_k:g} 个 1h ATR 时超前；"
          f"反向偏离 ≥ {params.chase_k:g} 个 ATR 时不追（除非很急）")
    if params.limit:
        print(f"  价格保护：{'不高于' if kind in ('hedge_open', 'spot_buy') else '不低于'} {params.limit:g}，超出就暂停并通知")
    if kind.startswith("hedge"):
        b = "近 24 小时中位数" if params.basis_min_bp is None else f"{params.basis_min_bp:+g}bp"
        print(f"  基差：{'加对冲要 永续溢价 ≥ ' if kind == 'hedge_open' else '减对冲要 永续溢价 ≤ '}{b}（很急时放宽）")
    print(f"  截止时剩余：{'吃单补齐' if params.finish == 'complete' else '撤掉不做'}")
    for b in blockers:
        print(f"  ✗ {b}")
    if blockers:
        sys.exit("\n有阻断项，不提交。")
    st = _store(cfg)
    busy = [j for j in st.list(("queued", "running")) if j.symbol == sym]
    if busy:
        sys.exit(f"{sym} 已有未完成的任务 #{busy[0].id}，先等它做完或 hedge exec cancel {busy[0].id}")
    if not dry:
        if not sys.stdin.isatty():
            sys.exit("真实任务必须在终端里手动确认")
        want = f"EXEC {sym} {total:g}"
        if input(f"\n提交真实执行任务（后台 worker 会真实下单）。确认请输入「{want}」：").strip() != want:
            sys.exit("没确认，不提交")
    job = st.add(Job(kind=kind, symbol=sym, venue=args.venue, total=total, start=start, end=end,
                     params=params, mode="paper" if dry else "live"))
    print(f"\n已提交任务 #{job.id}（{'演练' if dry else '实盘'}）：{total:g} {unit}。"
          f"看进度：hedge exec status / hedge exec report {job.id}")


def status(cfg: Config, args) -> None:
    st = _store(cfg)
    jobs = st.list()
    if not jobs:
        print("还没有执行任务")
        return
    for j in jobs[-20:]:
        s = j.state
        avg = s.spot_cost / s.spot_qty if s.spot_qty > 1e-9 else None
        print(f"#{j.id:<3} {j.status:16} {j.mode:5} {j.kind:11} {j.symbol:5} {s.done:g}/{j.total:g}"
              f"  {_fmt_t(j.start)} → {_fmt_t(j.end)}"
              + (f"  均价 {avg:g}" if avg else "") + (f"  ⏸ {s.paused}" if s.paused else ""))


def report(cfg: Config, args) -> None:
    j = _store(cfg).get(args.id)
    if not j:
        sys.exit(f"没有任务 #{args.id}")
    status(cfg, SimpleNamespace())
    print(f"\n#{j.id} 日志（最近 40 行）：")
    for line in j.state.log[-40:]:
        print("  " + line)


def cancel(cfg: Config, args) -> None:
    st = _store(cfg)
    j = st.get(args.id)
    if not j:
        sys.exit(f"没有任务 #{args.id}")
    if j.status == "queued":
        j.status = "cancelled"
        st.save(j)
        print(f"#{j.id} 还没开始，已取消")
    elif j.status == "running":
        st.db.execute("update exec_jobs set status='cancel_requested' where id=?", (j.id,))
        st.db.commit()
        print(f"#{j.id} 正在执行：已请求取消，worker 下一轮撤单停止（已成交部分保留）")
    else:
        print(f"#{j.id} 已经是 {j.status}")


def worker(cfg: Config, args) -> None:
    """后台执行：一次一个任务；SIGTERM 时停在安全点（任务留在 running，重启后接着做）。"""
    from .live import build
    from .notify import send
    load_exec_keys()
    st = _store(cfg)
    stop = {"flag": False, "sched": None}
    fails: dict[int, int] = {}                        # 任务 id → 连续出错次数（网络抖动重试，3 次才放弃）

    def _term(*_):
        stop["flag"] = True
        if stop["sched"]:
            stop["sched"].stop = True
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    print("hedge exec worker 启动", flush=True)
    while not stop["flag"]:
        jobs = st.list(("running", "queued", "cancel_requested"))
        now = time.time()
        job = next((j for j in jobs if j.status in ("running", "cancel_requested")), None) \
            or next((j for j in jobs if j.status == "queued" and j.start <= now), None)
        if not job:
            time.sleep(15)
            continue
        if job.status == "cancel_requested":
            job.status = "running"                    # 交给排程器在第一轮处理取消
        try:
            p, spot, perp, *_ = build(cfg, job.symbol, job.venue, need_trade=job.mode == "live",
                                      paper=job.mode == "paper")
            perp_pid = p["product_id"] if job.hedge else None
            sched = Scheduler(job, spot, perp if job.hedge else None, contract_size=p["contract_size"],
                              ref_fn=lambda: exec_signal.reference(f"{job.symbol}-USD", perp_pid),
                              store=st, notify=lambda t, m, pr=0: send(t, m, pr),
                              say=lambda m: print(f"[#{job.id}] {m}", flush=True),
                              escalation_bps=cfg.live.hedge_escalation_bps)
            stop["sched"] = sched
            sched.run()
            fails.pop(job.id, None)                   # 「连续」出错：成功一次就清零（审查 H9）
        except Exception as e:                        # noqa: BLE001
            n = fails[job.id] = fails.get(job.id, 0) + 1
            job.state.log.append(f"worker 出错（第 {n} 次）：{type(e).__name__}: {e}")
            if n >= 3:
                job.status = "error"
                send(f"hedge exec #{job.id} worker 出错", f"{type(e).__name__}: {e}。已停止，先 hedge account 核对两腿", 1)
            elif job.status == "queued":
                job.status = "running"                # 下一轮按恢复流程接着做（撤旧子单、补对冲缺口）
            st.save(job)
            print(f"[#{job.id}] 出错（第 {n} 次）：{e}", flush=True)
            if n < 3:
                time.sleep(60)
        stop["sched"] = None
    print("hedge exec worker 退出", flush=True)


def add_parser(sub) -> None:
    ex = sub.add_parser("exec", help="日内执行算法：你定量，程序在时间窗内挑时机分批做完")
    es = ex.add_subparsers(dest="exec_cmd", required=True)
    for act in ("open", "close"):
        p = es.add_parser(act, help=f"对冲{'加' if act == 'open' else '减'}仓（现货 + CDE 空单），按张")
        p.add_argument("symbol")
        p.add_argument("contracts", type=int)
        p.add_argument("--venue", "--spot", dest="venue", choices=["coinbase", "robinhood"], required=True)
        _common(p)
        p.add_argument("--basis-min", type=float, help="基差门槛（bp）：加仓 ≥、减仓 ≤；默认近 24h 中位数")
        p.set_defaults(action=act)
    for act in ("buy", "sell"):
        p = es.add_parser(act, help=f"现货{'买入' if act == 'buy' else '卖出'}")
        p.add_argument("symbol")
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--usd", type=float)
        g.add_argument("--qty", type=float)
        p.add_argument("--venue", choices=["coinbase", "robinhood"], required=True)
        _common(p)
        p.set_defaults(action=act, basis_min=None, contracts=None)
    es.add_parser("status", help="任务列表")
    r = es.add_parser("report", help="任务日志")
    r.add_argument("id", type=int)
    c = es.add_parser("cancel", help="取消任务（已成交部分保留）")
    c.add_argument("id", type=int)
    es.add_parser("worker", help="后台执行（systemd 用）")


def _common(p) -> None:
    p.add_argument("--by", required=True, help="截止：4h / 90m / 21:00（UTC）/ ISO 时间")
    p.add_argument("--start", help="开始（默认现在）")
    p.add_argument("--limit", type=float, help="价格保护：买不高于 / 卖不低于")
    p.add_argument("--finish", choices=["complete", "cancel"], default="complete")
    p.add_argument("--dip-k", type=float, default=0.5)
    p.add_argument("--chase-k", type=float, default=0.5)
    p.add_argument("--slices", type=int, default=4)
    p.add_argument("--dry", action="store_true", help="演练：盯真实行情、模拟成交，不下单")


def run(cfg: Config, args) -> None:
    {"open": submit, "close": submit, "buy": submit, "sell": submit, "status": status,
     "report": report, "cancel": cancel, "worker": worker}[args.exec_cmd](cfg, args)
