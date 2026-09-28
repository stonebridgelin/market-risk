"""回测配置（config/backtest.yaml）与数据区间划分。"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.config import PROJECT_ROOT, ConfigError, _read_yaml

DEFAULT_PATH = PROJECT_ROOT / "config" / "backtest.yaml"
DEVELOPMENT, VALIDATION, HOLDOUT, BEFORE_START = "开发期", "验证期", "保留期", "起点之前"


@dataclass(frozen=True)
class Grades:
    small: Decimal
    correction: Decimal
    bear: Decimal

    def grade(self, depth: Decimal) -> str:
        """depth 为跌幅（正数，如 0.123）。左闭右开：[small,correction) 小回调、[correction,bear) 修正、≥bear 熊市。"""
        if depth >= self.bear:
            return "熊市"
        if depth >= self.correction:
            return "修正"
        if depth >= self.small:
            return "小回调"
        return "未达分级"


@dataclass(frozen=True)
class SourceDependentPrice:
    symbol: str
    date: dt.date
    alternative: Decimal
    note: str


@dataclass(frozen=True)
class BacktestConfig:
    config_version: int
    start: dt.date
    development: tuple[dt.date, dt.date]
    validation: tuple[dt.date, dt.date]
    holdout_start: dt.date
    levels: Mapping[str, tuple[Decimal, ...]]
    grades: Mapping[str, Grades]
    window_sessions: int
    single_value_phase_end: Mapping[str, dt.date]
    source_dependent: tuple[SourceDependentPrice, ...]
    sha256: str

    def period_of(self, day: dt.date) -> str:
        if day < self.start:
            return BEFORE_START
        if day <= self.development[1]:
            return DEVELOPMENT
        if day <= self.validation[1]:
            return VALIDATION
        return HOLDOUT

    def period_end(self, period: str) -> dt.date | None:
        return {DEVELOPMENT: self.development[1], VALIDATION: self.validation[1]}.get(period)

    def in_holdout(self, day: dt.date) -> bool:
        return day >= self.holdout_start


def _date(v: object) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def load_backtest_config(path: Path = DEFAULT_PATH) -> BacktestConfig:
    raw = _read_yaml(path)
    try:
        p = raw["periods"]
        cfg = BacktestConfig(
            config_version=int(raw["config_version"]),
            start=_date(raw["start"]),
            development=(_date(p["development"]["start"]), _date(p["development"]["end"])),
            validation=(_date(p["validation"]["start"]), _date(p["validation"]["end"])),
            holdout_start=_date(p["holdout"]["start"]),
            levels={k: tuple(Decimal(str(x)) for x in v) for k, v in raw["zigzag_levels"].items()},
            grades={k: Grades(Decimal(str(v["small"])), Decimal(str(v["correction"])), Decimal(str(v["bear"])))
                    for k, v in raw["grades"].items()},
            window_sessions=int(raw["episode_window_sessions"]),
            single_value_phase_end={k: _date(v) for k, v in raw["single_value_phase_end"].items()},
            source_dependent=tuple(SourceDependentPrice(str(x["symbol"]), _date(x["date"]),
                                                        Decimal(str(x["alternative"])), str(x.get("note", "")))
                                   for x in raw.get("source_dependent_prices") or ()),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"回测配置错误：{exc}") from exc
    if not (cfg.start == cfg.development[0] < cfg.development[1] < cfg.validation[0] <= cfg.validation[1]
            < cfg.holdout_start):
        raise ConfigError("回测区间次序错误")
    for sym, levels in cfg.levels.items():
        if not levels or any(not (0 < x < 1) for x in levels) or sym not in cfg.grades:
            raise ConfigError(f"{sym} 的回调层级或分级配置错误")
    return cfg
