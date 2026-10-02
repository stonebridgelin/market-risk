"""失败路径分析（T2）的配置；不含任何 v1.4 规则，不参与任何判定。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from market_risk.wavewarn.failure_path import PathSettings
from market_risk.wavewarn.failure_path_context import EnvironmentSettings

Period = tuple[dt.date, dt.date]


@dataclass(frozen=True)
class StoredFiles:
    """一致性检查所对照的已入库输出与事件标签文件（相对仓库根目录）。"""

    development_daily: str
    buffered_reference: str
    migration_nav: str
    events: str
    merged: str
    unknown: str


@dataclass(frozen=True)
class FailurePathConfig:
    settings: PathSettings
    environment: EnvironmentSettings
    pre_peak_days: int
    development_case_periods: tuple[Period, ...]
    output: str
    stored: StoredFiles


def _date(value: Any) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def _periods(raw: Any) -> tuple[Period, ...]:
    return tuple((_date(start), _date(end)) for start, end in raw or ())


def parse_failure_path_config(raw: dict[str, Any]) -> FailurePathConfig:
    """分类与验收参数必须等于负责人批准的规格（T2）写明的值。"""
    if raw.get("version") != "v1.4-failure-path":
        raise ValueError("失败路径分析配置版本错误")
    environment, stored = raw.get("environment", {}), raw.get("stored", {})
    config = FailurePathConfig(
        PathSettings(float(raw.get("neutral_band", 0)), int(raw.get("short_segment", 0)),
                     float(raw.get("reconcile_tolerance", 0))),
        EnvironmentSettings(_periods(environment.get("bear_markets")), float(environment.get("up_year", 0)),
                            float(environment.get("down_year", 0))),
        int(raw.get("pre_peak_days", 0)), _periods(raw.get("development_case_periods")), str(raw.get("output")),
        StoredFiles(*(str(stored.get(name)) for name in ("development_daily", "buffered_reference", "migration_nav",
                                                         "events", "merged", "unknown"))))
    if (config.settings, config.pre_peak_days) != (PathSettings(0.0001, 5, 1e-10), 20):
        raise ValueError("中性带、短段长度、对账容差或高点前交易日数与规格不一致")
    if config.environment != EnvironmentSettings(((dt.date(2000, 3, 24), dt.date(2002, 10, 9)),
                                                  (dt.date(2007, 10, 9), dt.date(2009, 3, 9))), 0.10, -0.10):
        raise ValueError("熊市区间或年度收益门槛与规格不一致")
    if config.development_case_periods != ((dt.date(2011, 4, 29), dt.date(2011, 12, 28)),
                                           (dt.date(2014, 11, 28), dt.date(2016, 6, 27))):
        raise ValueError("案例段与规格不一致")
    return config


def load_failure_path_config(path: Path) -> FailurePathConfig:
    with path.open(encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    if not isinstance(raw, dict):
        raise ValueError("失败路径分析配置格式错误")
    return parse_failure_path_config(raw)
