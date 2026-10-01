"""v1.3 修订登记的新增配置；其余参数沿用它指向的 v1.2.1 配置（该配置及其校验不改）。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from market_risk.wavewarn.config import ChannelSelection, WavewarnConfig, load_wavewarn_config

EXIT_VERSIONS = ("E2", "X1", "X2")
DELAYS = (0, 1, 2, 3, 5, 8, 10, 15)


@dataclass(frozen=True)
class FeasibilityLimits:
    non_green_share_max: Decimal
    r_median_max: Decimal
    r_p75_max: Decimal


@dataclass(frozen=True)
class ExtendedHistoryWindows:
    fixed_delay_start: dict[str, dt.date]
    fixed_delay_end: dt.date
    p0_start: dt.date
    p0_end: dt.date


@dataclass(frozen=True)
class V13Config:
    base: WavewarnConfig
    limits: FeasibilityLimits
    exit_versions: tuple[str, ...]
    windows: ExtendedHistoryWindows
    ma200_window: int


def _date(value: Any) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def _limits(raw: dict[str, Any]) -> FeasibilityLimits:
    """可行条件的三个上限必须等于登记值。"""
    values = raw.get("feasibility", {})
    limits = FeasibilityLimits(*(Decimal(str(values.get(name)))
                                 for name in ("non_green_share_max", "r_median_max", "r_p75_max")))
    if limits != FeasibilityLimits(Decimal("0.60"), Decimal("0.60"), Decimal("1.00")):
        raise ValueError("可行条件上限与 v1.3 登记不一致")
    return limits


def _windows(raw: dict[str, Any], development_end: dt.date) -> ExtendedHistoryWindows:
    """补充历史期间必须等于登记值，且早于开发期评价使用的区间。"""
    history = raw.get("extended_history", {})
    fixed, p0 = history.get("fixed_delay", {}), history.get("p0_robustness", {})
    windows = ExtendedHistoryWindows(
        {"SPX": _date(fixed.get("spx_start")), "QQQ": _date(fixed.get("qqq_start"))},
        _date(fixed.get("end")), _date(p0.get("start")), _date(p0.get("end")))
    expected = ExtendedHistoryWindows({"SPX": dt.date(1990, 1, 2), "QQQ": dt.date(1999, 3, 10)},
                                      dt.date(2009, 9, 30), dt.date(1999, 3, 10), dt.date(2009, 9, 30))
    if windows != expected or tuple(fixed.get("delays", ())) != DELAYS or windows.p0_end > development_end:
        raise ValueError("补充历史期间或固定延迟与 v1.3 登记不一致")
    return windows


def parse_v13_config(raw: dict[str, Any], base: WavewarnConfig) -> V13Config:
    """逐项核对 v1.3 新增项；任何一项偏离登记即拒绝。"""
    if raw.get("version") != "v1.3":
        raise ValueError("wavewarn v1.3 配置版本错误")
    if tuple(raw.get("exit_versions", ())) != EXIT_VERSIONS:
        raise ValueError("退出版本顺序必须为 E2、X1、X2")
    prime = raw.get("n_prime_channels", {})
    if any(type(prime.get(name)) is not bool for name in ("b_spx", "dv_spx", "b_qqq", "dv_qqq")) or \
            ChannelSelection(prime["b_spx"], prime["dv_spx"], prime["b_qqq"], prime["dv_qqq"]) != \
            ChannelSelection(False, False, False, False):
        raise ValueError("N′ 须两侧去掉 B、DV")
    reference = raw.get("ma200_reference", {})
    if (reference.get("symbol"), reference.get("window"), reference.get("at_or_above"),
            reference.get("below")) != ("SPX", base.fixed_parameters().ma200, "绿", "红"):
        raise ValueError("200日均线参照与 v1.3 登记不一致")
    return V13Config(base, _limits(raw), EXIT_VERSIONS, _windows(raw, base.development_end()),
                     int(reference["window"]))


def load_v13_config(path: Path) -> V13Config:
    """读取 v1.3 配置与它指向的 v1.2.1 配置（同目录）。"""
    with path.open(encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    if not isinstance(raw, dict) or not isinstance(raw.get("base"), str):
        raise ValueError("wavewarn v1.3 配置格式错误或缺少 base")
    return parse_v13_config(raw, load_wavewarn_config(path.parent / raw["base"]))
