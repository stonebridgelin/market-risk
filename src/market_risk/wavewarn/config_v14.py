"""v1.4 修订登记的新增配置；其余参数沿用它指向的 v1.2.1 配置（该配置及其校验不改）。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from market_risk.wavewarn.config import WavewarnConfig, load_wavewarn_config

NON_GREEN = "non_green_share"
DELAY_SPX = "green_delay_spx"
DELAY_QQQ = "green_delay_qqq"


@dataclass(frozen=True)
class V14Limits:
    non_green_share_max: Decimal        # 亮灯上限
    green_delay_median_max: Decimal     # 转绿延迟中位数上限（交易日）


@dataclass(frozen=True)
class SelectionTier:
    conditions: tuple[str, ...]         # 进入这一级须满足的条件；空表示全部候选
    note: str                           # 落在这一级时的标注


@dataclass(frozen=True)
class MedianSetting:
    k: int
    theta_p: Decimal
    q: Decimal


@dataclass(frozen=True)
class V14Config:
    base: WavewarnConfig
    k: tuple[int, ...]
    theta_p: tuple[Decimal, ...]
    mr_window: int
    limits: V14Limits
    tiers: tuple[SelectionTier, ...]
    median: MedianSetting
    history_start: dt.date
    history_end: dt.date
    bear_markets: tuple[tuple[dt.date, dt.date], ...]


REGISTERED_TIERS = (SelectionTier((NON_GREEN, DELAY_SPX, DELAY_QQQ), ""),
                    SelectionTier((NON_GREEN,), "转绿要求未达标"),
                    SelectionTier((), "亮灯上限与转绿要求均未达标"))
REGISTERED_BEAR_MARKETS = ((dt.date(2000, 3, 24), dt.date(2002, 10, 9)),
                           (dt.date(2007, 10, 9), dt.date(2009, 3, 9)))


def _date(value: Any) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def _candidates(raw: dict[str, Any]) -> tuple[tuple[int, ...], tuple[Decimal, ...]]:
    values = raw.get("candidates", {})
    k = tuple(int(item) for item in values.get("k", ()))
    theta = tuple(Decimal(str(item)) for item in values.get("theta_p", ()))
    if k != (3, 5, 10) or theta != (Decimal("0.015"), Decimal("0.02"), Decimal("0.025")):
        raise ValueError("候选 K、θ_P 或登记顺序与 v1.4 登记不一致")
    return k, theta


def _limits(raw: dict[str, Any]) -> V14Limits:
    values = raw.get("feasibility", {})
    limits = V14Limits(Decimal(str(values.get("non_green_share_max"))),
                       Decimal(str(values.get("green_delay_median_max"))))
    if limits != V14Limits(Decimal("0.60"), Decimal(8)):
        raise ValueError("亮灯上限或转绿延迟上限与 v1.4 登记不一致")
    return limits


def _tiers(raw: dict[str, Any]) -> tuple[SelectionTier, ...]:
    tiers = tuple(SelectionTier(tuple(item.get("conditions", ())), str(item.get("note", "")))
                  for item in raw.get("selection_tiers", ()))
    if tiers != REGISTERED_TIERS:
        raise ValueError("终止规则三级与 v1.4 登记不一致")
    return tiers


def _history(raw: dict[str, Any]) -> tuple[dt.date, dt.date, tuple[tuple[dt.date, dt.date], ...]]:
    values = raw.get("extended_history", {})
    start, end = _date(values.get("start")), _date(values.get("end"))
    bears = tuple((_date(first), _date(last)) for first, last in values.get("bear_markets", ()))
    if (start, end) != (dt.date(1999, 3, 10), dt.date(2009, 9, 30)) or bears != REGISTERED_BEAR_MARKETS:
        raise ValueError("补充历史期间或熊市区间与 v1.4 登记不一致")
    return start, end, bears


def parse_v14_config(raw: dict[str, Any], base: WavewarnConfig) -> V14Config:
    """逐项核对 v1.4 新增项；任何一项偏离登记即拒绝。"""
    if raw.get("version") != "v1.4" or raw.get("release_rule") != "F":
        raise ValueError("wavewarn v1.4 配置版本或解除规则错误")
    trend = raw.get("mr", {})
    if (trend.get("symbol"), trend.get("window")) != ("SPX", base.fixed_parameters().ma200):
        raise ValueError("趋势层 MR 与 v1.4 登记不一致")
    middle = raw.get("reference_median", {})
    median = MedianSetting(int(middle.get("k", 0)), Decimal(str(middle.get("theta_p"))),
                           Decimal(str(middle.get("q"))))
    if median != MedianSetting(5, Decimal("0.02"), Decimal("0.10")):
        raise ValueError("描述性对照的中位设定与 v1.4 登记不一致")
    k, theta = _candidates(raw)
    start, end, bears = _history(raw)
    return V14Config(base, k, theta, int(trend["window"]), _limits(raw), _tiers(raw), median, start, end, bears)


def load_v14_config(path: Path) -> V14Config:
    """读取 v1.4 配置与它指向的 v1.2.1 配置（同目录）。"""
    with path.open(encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    if not isinstance(raw, dict) or not isinstance(raw.get("base"), str):
        raise ValueError("wavewarn v1.4 配置格式错误或缺少 base")
    return parse_v14_config(raw, load_wavewarn_config(path.parent / raw["base"]))


@dataclass(frozen=True)
class ValidationConfig:
    """验证期工程的配置；不含任何 v1.4 规则。"""

    model: V14Config
    locked_k: int
    locked_theta: Decimal
    first_interval: dt.date
    end: dt.date
    vix3m_file: str
    big_drop_threshold: Decimal
    significance: Decimal
    locked_files: dict[str, tuple[str, ...]]
    git_executable: str
    validation_output: str
    rehearsal_output: str


def parse_validation_config(raw: dict[str, Any], model: V14Config) -> ValidationConfig:
    """验证期起止日、锁定设定与大跌门槛必须等于登记与审核过的值。"""
    if raw.get("version") != "v1.4-validation":
        raise ValueError("wavewarn v1.4 验证期配置版本错误")
    setting, period, output = raw.get("locked_setting", {}), raw.get("validation", {}), raw.get("output", {})
    config = ValidationConfig(
        model, int(setting.get("k", 0)), Decimal(str(setting.get("theta_p"))), _date(period.get("first_interval")),
        _date(period.get("end")), str(period.get("vix3m_file")), Decimal(str(raw.get("big_drop_threshold"))),
        Decimal(str(raw.get("significance"))),
        {str(name): tuple(str(item) for item in files) for name, files in raw.get("locked_files", {}).items()},
        str(raw.get("git_executable", "")), str(output.get("validation")), str(output.get("rehearsal")))
    if (config.locked_k, config.locked_theta) not in [(k, theta) for k in model.k for theta in model.theta_p]:
        raise ValueError("锁定设定不在登记的候选网格内")
    if (config.first_interval, config.end, config.big_drop_threshold, config.significance) != (
            dt.date(2017, 1, 3), dt.date(2022, 12, 30), Decimal("0.15"), Decimal("0.05")):
        raise ValueError("验证期起止日、大跌门槛或显著性门槛与登记不一致")
    if not config.locked_files:
        raise ValueError("缺少须与锁定记录核对的文件清单")
    return config


def load_validation_config(path: Path) -> ValidationConfig:
    """读取验证期工程配置与它指向的 v1.4 配置（同目录）。"""
    with path.open(encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    if not isinstance(raw, dict) or not isinstance(raw.get("base"), str):
        raise ValueError("wavewarn v1.4 验证期配置格式错误或缺少 base")
    return parse_validation_config(raw, load_v14_config(path.parent / raw["base"]))
