"""config.toml（阈值、标的）+ .env（凭据）。凭据一律不进 config.toml。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Fees:
    kraken_taker: float
    kraken_maker: float
    cde_taker: float
    cde_maker: float


@dataclass(frozen=True)
class Config:
    symbols: dict[str, str]           # 标的 -> Kraken 交易对
    fees: Fees
    max_slippage_bps: float
    hedge_tolerance: float
    default_usd: float
    db_path: Path


def load_config(path: Path | None = None) -> Config:
    path = path or PROJECT_ROOT / "config.toml"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    fees = Fees(**{k: float(v) for k, v in raw["fees"].items()})
    for k, v in vars(fees).items():
        # 写成百分数（0.4）而不是小数（0.004）是最容易犯的错，差 100 倍
        if not 0 <= v < 0.02:
            raise ValueError(f"fees.{k} = {v}：应写成小数，0.004 = 0.40%")
    risk = raw["risk"]
    return Config(
        symbols={k.upper(): v for k, v in raw["symbols"].items()},
        fees=fees,
        max_slippage_bps=float(risk["max_slippage_bps"]),
        hedge_tolerance=float(risk["hedge_tolerance"]),
        default_usd=float(risk["default_usd"]),
        db_path=PROJECT_ROOT / "data" / "paper.db",
    )


def load_env(path: Path | None = None) -> None:
    """极简 .env：KEY=VALUE，不覆盖已有环境变量。"""
    path = path or PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
