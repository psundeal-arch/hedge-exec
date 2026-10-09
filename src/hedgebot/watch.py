"""定时巡检：两腿对齐报警 + 模拟盘记资金费。每 15 分钟由 systemd timer 跑一次。

报警的生命周期按「问题键」管理（misalign:BTC、blind）：
    新出现      → 立刻发
    持续存在    → 按 resend_hours 退避重发（1h/3h/6h/12h/24h…）
    消失        → 发一条恢复通知，删掉记录

【取数有任何一边失败时，不评估对齐】—— Robinhood 一抖，BTC 现货读成 0，
看起来就是裸空。这时已有的 misalign 记录原样保留（不当成恢复），
只累计失败轮数；连续 blind_after 轮才报「监控看不见」。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import coinbase, notify
from .account import classify, hedge_table
from .config import Config
from .http import SourceError

SCHEMA = """
create table if not exists alerts(
    key text primary key, first_seen text, last_sent text,
    sent_count integer, detail text);
create table if not exists kv(k text primary key, v text);
"""


@dataclass
class Alert:
    key: str
    kind: str          # new / resend / recover
    title: str
    message: str
    priority: int


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def alerts(self) -> dict[str, dict]:
        return {r["key"]: dict(r) for r in self.db.execute("select * from alerts")}

    def get(self, k: str, default: str = "") -> str:
        r = self.db.execute("select v from kv where k=?", (k,)).fetchone()
        return r["v"] if r else default

    def put(self, k: str, v) -> None:
        self.db.execute("insert or replace into kv(k,v) values(?,?)", (k, str(v)))
        self.db.commit()

    def mark_sent(self, key: str, detail: str, now: dt.datetime) -> None:
        r = self.db.execute("select * from alerts where key=?", (key,)).fetchone()
        if r:
            self.db.execute("update alerts set last_sent=?, sent_count=sent_count+1, detail=?"
                            " where key=?", (now.isoformat(), detail, key))
        else:
            self.db.execute("insert into alerts values(?,?,?,?,?)",
                            (key, now.isoformat(), now.isoformat(), 1, detail))
        self.db.commit()

    def drop(self, key: str) -> None:
        self.db.execute("delete from alerts where key=?", (key,))
        self.db.commit()


def _row_text(r: dict) -> str:
    parts = ", ".join(f"{k} {v:g}" for k, v in sorted(r["spot_parts"].items())) or "无"
    usd = f"≈ ${r['delta_usd']:+,.0f}" if r["delta_usd"] is not None else ""
    hint = f"；{r['hint']}" if r["hint"] else ""
    return (f"{r['asset']}：现货 {r['spot']:g}（{parts}）/ CDE {r['perp']:g}"
            f" → 净 {r['delta']:+g} {usd}{hint}")


def plan(current: dict[str, tuple[str, int]], scope: set[str] | None,
         existing: dict[str, dict], cfg: Config, now: dt.datetime) -> list[Alert]:
    """纯函数：根据当前问题与已有记录，决定发什么。

    current: 问题键 -> (详情, 优先级)
    scope:   这一轮【评估过】的键前缀；None = 全部。不在 scope 里的已有记录
             既不重发也不当恢复（比如取数失败时 misalign:* 就不在 scope 里）。
    """
    out = []
    for key, (detail, pri) in current.items():
        rec = existing.get(key)
        if rec is None:
            out.append(Alert(key, "new", "", detail, pri))
            continue
        n = rec["sent_count"]
        wait = cfg.resend_hours[min(n - 1, len(cfg.resend_hours) - 1)]
        if now - dt.datetime.fromisoformat(rec["last_sent"]) >= dt.timedelta(hours=wait):
            out.append(Alert(key, "resend", "", detail, pri))
    for key, rec in existing.items():
        in_scope = scope is None or any(key.startswith(p) for p in scope)
        if in_scope and key not in current:
            out.append(Alert(key, "recover", "", rec["detail"], -1))
    return out


def run(cfg: Config, *, dry_run: bool = False, now: dt.datetime | None = None) -> list[str]:
    """跑一轮，返回日志行。dry_run 只打印，不发、不改状态。"""
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0)
    st = State(cfg.db_path.parent / "watch.db")
    log: list[str] = []

    try:
        perps = coinbase.cde_perps()
        rows, notes, errors = hedge_table(perps, expected=cfg.venues)
    except SourceError as e:
        perps, rows, notes, errors = {}, [], [], [f"Coinbase 合约列表取数失败：{e}"]

    current: dict[str, tuple[str, int]] = {}
    from .account import MISSING
    missing = [e for e in errors if e.startswith(MISSING)]
    errors = [e for e in errors if not e.startswith(MISSING)]
    if missing:
        # 永久的配置问题：立刻报、走重发退避，不进连续失败计数（M4）。对齐照样评估不了
        current["config"] = ("这台机器读不到下面这些场所的持仓 —— 两腿对齐【看不见】，等多久都不会自己好：\n"
                             + "\n".join(m.removeprefix(MISSING) for m in missing)
                             + "\n\n修：把对应 key 放进 ~/vault，或在 config.toml [watch] venues 里去掉这个场所", 0)
    if missing and not errors:
        log.append("凭据不全，本轮不评估对齐：" + "；".join(missing))
        scope = {"config"}
    elif errors:
        streak = int(st.get("fail_streak", "0")) + 1
        if not dry_run:
            st.put("fail_streak", streak)
        log.append(f"取数失败（连续第 {streak} 轮），本轮不评估对齐：" + "；".join(errors))
        if streak >= cfg.blind_after:
            current["blind"] = (
                f"连续 {streak} 轮（约 {streak * 15} 分钟）没读全持仓，"
                f"两腿是否对齐现在【看不见】，不是【没问题】。\n" + "\n".join(errors), 0)
        scope = {"blind", "config"} if missing else {"blind"}
    else:
        if not dry_run:
            st.put("fail_streak", 0)
        bad = [r for r in classify(rows, cfg) if r["problem"]]
        for r in bad:
            current[f"misalign:{r['asset']}"] = (_row_text(r), 1)
        log.append(f"对齐检查：{len(rows)} 个标的，{len(bad)} 个有问题")
        scope = None

    # Robinhood 解押提醒：和对齐检查互不影响（各自取数、各自 scope）
    if cfg.unstake_targets:
        import os
        from . import robinhood
        rk, rs = os.environ.get("RH_API_KEY"), os.environ.get("RH_PRIVATE_KEY")
        try:
            if not (rk and rs):
                raise SourceError("没有 RH_API_KEY / RH_PRIVATE_KEY")
            av = robinhood.holdings(rk, rs, available=True)
            for sym, need in cfg.unstake_targets.items():
                have = av.get(sym, 0.0)
                log.append(f"RH {sym} 可卖 {have:g} / 等 {need:g}")
                if have >= need - 1e-9:
                    current[f"unstake:{sym}"] = (
                        f"Robinhood 上 {sym} 现在可卖 {have:g}（≥ {need:g}）—— 解押到账了。\n"
                        f"平对冲腿（先预检，再加 --go）：\n"
                        f"hedge live close {sym} --spot robinhood --perp cde --contracts N\n"
                        f"做完后把 config.toml [watch] unstake_targets 里的 {sym} 删掉，提醒就停", 1)
            scope = None if scope is None else scope | {"unstake"}
        except SourceError as e:
            log.append(f"RH 解押提醒取数失败：{e}")
            # 这轮没读到：已发过的提醒原样保留（不当成「条件消失」清掉，否则下轮又按新的重发）
            for k, rec in st.alerts().items():
                if k.startswith("unstake:"):
                    current[k] = (rec["detail"], 1)

    titles = {"new": "⚠ 两腿没对齐", "resend": "⚠ 仍未对齐", "recover": "✓ 已恢复"}
    for a in plan(current, scope, st.alerts(), cfg, now):
        if a.key.startswith("unstake:"):
            if a.kind == "recover":                   # 条件消失（已卖掉或配置删了）：静默清掉，不发「已恢复」
                if not dry_run:
                    st.drop(a.key)
                continue
            title = f"[hedge-bot] 🔔 RH 解押到账：{a.key.split(':', 1)[1]}"
            if dry_run:
                log.append(f"[dry-run] 会发 {title}\n{a.message}")
                continue
            ok, fails = notify.send(title, a.message, a.priority)
            if ok:
                st.mark_sent(a.key, a.message.split("\n")[0], now)
            log.append(("已发 " if ok else "✗ 没发出去 ") + title)
            continue
        what = {"blind": "监控看不见", "config": "凭据缺失"}.get(a.key) or a.key.split(":", 1)[1]
        head = titles[a.kind] if a.key.startswith("misalign:") else {"new": "⚠", "resend": "⚠ 仍然", "recover": "✓ 已恢复"}[a.kind]
        title = f"[hedge-bot] {head}：{what}" if a.key.startswith("misalign:") else f"[hedge-bot] {head} {what}"
        msg = a.message
        if a.kind != "recover" and a.key.startswith("misalign:"):
            msg += ("\n\n可能原因：现货被卖出/转走、CDE 空单被部分强平、手动调仓只做了一腿。"
                    "\n核对：hedge account")
        if dry_run:
            log.append(f"[dry-run] 会发 {title}\n{msg}")
            continue
        ok, fails = notify.send(title, msg, a.priority)
        if not ok:
            # 一个通道都没送达：不记状态，下一轮会再试
            log.append(f"✗ 没发出去 {title}：{'；'.join(fails)}")
            continue
        if a.kind == "recover":
            st.drop(a.key)
        else:
            st.mark_sent(a.key, msg.split("\n\n")[0], now)
        log.append(f"已发 {title}" + (f"（{'；'.join(fails)}）" if fails else ""))
    return log + [f"· {n}" for n in notes]
