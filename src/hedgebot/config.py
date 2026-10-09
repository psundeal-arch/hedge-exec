"""config.toml（阈值、标的）+ .env（凭据）。凭据一律不进 config.toml。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Fees:
    kraken_taker: float
    kraken_maker: float
    cde_taker: float
    cde_maker: float
    cde_per_contract: float = 0.0     # 美元/张，按张收的固定费
    coinbase_taker: float = 0.0030
    coinbase_maker: float = 0.0015


@dataclass(frozen=True)
class Live:
    max_notional_usd: float = 25000
    max_wait_s: float = 600
    reprice_min_s: float = 4
    poll_s: float = 2
    clip_usd: float = 2000
    hedge_escalation_bps: tuple[float, ...] = (30, 100, 300)
    max_fixed_fee_bp: float = 5.0     # CDE 每张固定费折成 bp/边 的上限，超过不开仓（平仓不拦）
    exit_usd: float = 25000           # 容量按多大的出场算（scan 默认用这个）
    max_exit_impact_bp: float = 30.0  # 出场时 CDE 吃单冲击上限（VWAP vs 中价）
    max_adv_share: float = 0.01       # 出场额占 24h 成交额的上限（参与率）


@dataclass(frozen=True)
class Config:
    symbols: dict[str, str]           # 标的 -> Kraken 交易对
    fees: Fees
    max_slippage_bps: float
    hedge_tolerance: float
    default_usd: float
    db_path: Path
    hold_only: frozenset[str] = frozenset()   # 有意只持现货、不对冲的标的
    grid_only: frozenset[str] = frozenset()   # CDE 永续由 perp-grid 实盘网格独立交易的标的：不参与对齐、不报警
    min_alert_usd: float = 200.0
    blind_after: int = 3
    resend_hours: tuple[float, ...] = (1, 3, 6, 12, 24)
    venues: tuple[str, ...] = ("kraken", "robinhood", "coinbase")   # 本机应该读得到的场所
    unstake_targets: dict = field(default_factory=dict)   # RH 解押提醒：币 -> 可卖数量达到多少就通知
    live: Live = Live()


def load_config(path: Path | None = None) -> Config:
    path = path or PROJECT_ROOT / "config.toml"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    fees = Fees(**{k: float(v) for k, v in raw["fees"].items()})
    for k, v in vars(fees).items():
        if k == "cde_per_contract":
            if not 0 <= v < 5:
                raise ValueError(f"fees.{k} = {v}：是每张多少美元")
            continue
        # 写成百分数（0.4）而不是小数（0.004）是最容易犯的错，差 100 倍
        if not 0 <= v < 0.02:
            raise ValueError(f"fees.{k} = {v}：应写成小数，0.004 = 0.40%")
    risk = raw["risk"]
    watch = raw.get("watch", {})
    return Config(
        symbols={k.upper(): v for k, v in raw["symbols"].items()},
        fees=fees,
        max_slippage_bps=float(risk["max_slippage_bps"]),
        hedge_tolerance=float(risk["hedge_tolerance"]),
        default_usd=float(risk["default_usd"]),
        db_path=PROJECT_ROOT / "data" / "paper.db",
        hold_only=frozenset(s.upper() for s in risk.get("hold_only", [])),
        grid_only=frozenset(s.upper() for s in risk.get("grid_only", [])),
        min_alert_usd=float(watch.get("min_alert_usd", 200)),
        blind_after=int(watch.get("blind_after", 3)),
        resend_hours=tuple(float(x) for x in watch.get("resend_hours", (1, 3, 6, 12, 24))),
        venues=tuple(str(v).lower() for v in watch.get("venues", ("kraken", "robinhood", "coinbase"))),
        unstake_targets={str(k): float(v) for k, v in watch.get("unstake_targets", {}).items()},
        live=Live(**{k: tuple(float(x) for x in v) if isinstance(v, list) else float(v)
                     for k, v in raw.get("live", {}).items()}),
    )


VAULT_DIR = Path(os.environ.get("HEDGE_VAULT_DIR") or Path.home() / "vault")
# 只读 + 通知。显式白名单，不是 glob —— glob 会把将来放进 vault 的交易密钥也吃进来。
# 交易 key 从不经这里：本机 hedge live / rh-order 读 .env.trade，服务器 exec 读 HEDGE_EXEC_KEYS
VAULT_FILES = ("coinbase-view.env", "kraken-view.env", "robinhood-view.env", "notify.env", "misc.env")


def load_env(path: Path | None = None) -> None:
    """极简 .env：KEY=VALUE，不覆盖已有环境变量。

    不给 path 时：先项目 .env，再 ~/vault 里白名单的几个文件补缺口（2026-10-06 凭据集中到
    服务器 ~/vault，与 perptrack.config.load_env 同一口径）。.env 在就是它说了算，
    删掉之后 vault 接管；本机没有 vault，照旧只用 .env。"""
    if path is not None:
        return _inject(path)
    _inject(PROJECT_ROOT / ".env")
    for name in VAULT_FILES:
        _inject(VAULT_DIR / name)


def _inject(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
