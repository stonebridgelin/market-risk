"""P0、P1、N 的逐日状态序列与 Ready 输入（纯计算）；不读写文件、不读取研究标签。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from market_risk.wavewarn.channels import (
    ChannelDay,
    ChannelPredicate,
    breadth_predicates,
    price_predicates,
    update_channel,
    volatility_predicates,
)
from market_risk.wavewarn.config import ChannelSelection, FixedParameters
from market_risk.wavewarn.features import AssetFeatures, rolling_high, vix_term_ratio
from market_risk.wavewarn.state_machine import ReadyInputs, SystemMemory, step_system

Scenario = Literal["P0", "P1-E1", "P1-E2", "P1-E3", "N-E1", "N-E2", "N-E3"]


@dataclass(frozen=True)
class DiagnosticRow:
    date: dt.date
    scenario: Scenario
    k: int
    theta_p: Decimal
    light: str
    data_status: str
    active_channels: tuple[str, ...]
    reason: str


def first_complete_day(days: Sequence[dt.date], spx: Sequence[AssetFeatures],
                       qqq: Sequence[AssetFeatures], vix: Mapping[dt.date, Decimal],
                       vix3m: Mapping[dt.date, Decimal],
                       fixed: FixedParameters | None = None) -> dt.date:
    """N 的价格、广度分位数、期限结构全部具备的首个交易日。"""
    required = ("close", "high63", "drawdown63", "breadth", "delta_w20", "delta_w10",
                "quantile_reference", "median60", "ma50", "ma200", "close_t5", "close_t10")
    volatility_days = fixed.volatility_exit_long if fixed else 10
    repair_days = fixed.breadth_repair if fixed else 3
    for index, (day, a, b) in enumerate(zip(days, spx, qqq, strict=True)):
        if (all(getattr(item, name) is not None for item in (a, b) for name in required)
                and index >= volatility_days - 1
                and all(vix_term_ratio(vix.get(prior), vix3m.get(prior)) is not None
                        for prior in days[index - volatility_days + 1:index + 1])
                and all(all(getattr(item, name) is not None for name in ("breadth", "median60", "close", "close_t5"))
                        for past in range(index - repair_days + 1, index + 1)
                        for item in (spx[past], qqq[past]))):
            return day
    raise ValueError("开发期内 N 的输入未同时完整")


def _repair_streak(features: Sequence[AssetFeatures], index: int,
                   repair_days: int = 3) -> bool | None:
    if index < repair_days - 1:
        return False
    window = features[index - repair_days + 1:index + 1]
    if any(item.breadth is None or item.median60 is None for item in window):
        return None
    return all(item.breadth >= item.median60 for item in window)


def ready_inputs(days: Sequence[dt.date], spx: Sequence[AssetFeatures], qqq: Sequence[AssetFeatures],
                 scenario: Scenario, fixed: FixedParameters | None = None) -> tuple[ReadyInputs, ...]:
    """逐日降级所需输入；任一所需输入缺失时 valid 为假。"""
    spx_closes = tuple(item.close for item in spx)
    result = []
    for index, (a, b) in enumerate(zip(spx, qqq, strict=True)):
        high252 = rolling_high(spx_closes, index, fixed.e3_high_window if fixed else 252)
        e3_guard = None if a.close is None else (a.close > a.ma200 if a.ma200 is not None else None)
        if a.close is not None and high252 is not None:
            near_high = a.close / high252 > 1 - (fixed.e3_near_high if fixed else Decimal("0.10"))
            e3_guard = near_high if e3_guard is None else e3_guard or near_high
        repaired_spx = _repair_streak(spx, index, fixed.breadth_repair if fixed else 3)
        repaired_qqq = _repair_streak(qqq, index, fixed.breadth_repair if fixed else 3)
        above_spx = a.close > a.ma50 if a.close is not None and a.ma50 is not None else None
        above_qqq = b.close > b.ma50 if b.close is not None and b.ma50 is not None else None
        valid = (a.close is not None and b.close is not None and
                 (scenario == "P0" or (repaired_spx is not None and repaired_qqq is not None)) and
                 (scenario.endswith("E1") or (above_spx is not None and above_qqq is not None)) and
                 (not scenario.endswith("E3") or e3_guard is not None))
        result.append(ReadyInputs(a.q, b.q, repaired_spx, repaired_qqq,
                                  above_spx, above_qqq, e3_guard, valid))
    return tuple(result)


def diagnostic_sequence(days: Sequence[dt.date], spx: Sequence[AssetFeatures], qqq: Sequence[AssetFeatures],
                        t0: dt.date, scenario: Scenario, theta_p: Decimal, k: int,
                        fixed: FixedParameters | None = None) -> tuple[DiagnosticRow, ...]:
    """t0 快照为已武装/绿；次日起更新，输出逐日触发原因。"""
    if len(days) != len(spx) or len(days) != len(qqq):
        raise ValueError("特征与交易日数量不一致")
    start = days.index(t0)
    assets = {"SPX": spx, "QQQ": qqq}
    predicates: dict[str, tuple[ChannelPredicate, ...]] = {}
    for symbol, items in assets.items():
        predicates[f"P_{symbol}"] = price_predicates(items, theta_p, k)
        predicates[f"PR_{symbol}"] = price_predicates(items, theta_p, k, red=True)
    ready = ready_inputs(days, spx, qqq, scenario, fixed)
    states = {name: "armed" for name in predicates}
    memory = SystemMemory()
    result = [DiagnosticRow(t0, scenario, k, theta_p, "绿", "完整", (), "t0 初始快照")]
    version: Literal["P0", "E1", "E2", "E3"] = "P0" if scenario == "P0" else scenario[-2:]
    for index in range(start + 1, len(days)):
        channel_rows: dict[str, tuple[Literal["黄", "红"], ChannelDay]] = {}
        for name, series in predicates.items():
            update = update_channel(days[index], states[name], series[index])
            states[name] = update.status
            channel_rows[name] = ("红" if name.startswith("PR_") else "黄", update)
        system = step_system(days[index], memory, channel_rows, ready[index], k, version,
                             fixed.quiet_red_days if fixed else 3,
                             fixed.quiet_all_days if fixed else 5)
        memory = system.memory
        active = tuple(sorted(name for name, (_, channel) in channel_rows.items() if channel.status == "active"))
        result.append(DiagnosticRow(days[index], scenario, k, theta_p, memory.light,
                                    system.data_status, active, system.reason))
    return tuple(result)


def n_channel_inputs(spx: Sequence[AssetFeatures], qqq: Sequence[AssetFeatures],
                     ratios: Sequence[Decimal | None], theta_p: Decimal, k: int,
                     selection: ChannelSelection,
                     fixed: FixedParameters | None = None
                     ) -> dict[str, tuple[Literal["黄", "红"], tuple[ChannelPredicate, ...]]]:
    """N 状态与收敛检查共用同一套通道谓词。"""
    if not (len(spx) == len(qqq) == len(ratios)):
        raise ValueError("N 通道输入时间轴不一致")
    if any(type(getattr(selection, name)) is not bool for name in ("b_spx", "dv_spx", "b_qqq", "dv_qqq")):
        raise ValueError("N 的 B、DV 四侧开关须显式填写")
    predicates: dict[str, tuple[Literal["黄", "红"], tuple[ChannelPredicate, ...]]] = {}
    for symbol, features in (("SPX", spx), ("QQQ", qqq)):
        predicates[f"P_{symbol}"] = ("黄", price_predicates(features, theta_p, k))
        predicates[f"PR_{symbol}"] = ("红", price_predicates(features, theta_p, k, red=True))
        predicates[f"BW_{symbol}"] = ("红", breadth_predicates(features, "BW", fixed))
        if getattr(selection, f"b_{symbol.lower()}"):
            predicates[f"B_{symbol}"] = ("黄", breadth_predicates(features, "B", fixed))
        if getattr(selection, f"dv_{symbol.lower()}"):
            predicates[f"DV_{symbol}"] = ("黄", breadth_predicates(features, "DV", fixed))
    predicates["V"] = ("红", volatility_predicates(ratios, fixed))
    return predicates


def n_diagnostic_sequence(days: Sequence[dt.date], spx: Sequence[AssetFeatures],
                          qqq: Sequence[AssetFeatures], ratios: Sequence[Decimal | None],
                          t0: dt.date, theta_p: Decimal, k: int,
                          selection: ChannelSelection,
                          e_version: Literal["E1", "E2", "E3"],
                          fixed: FixedParameters | None = None) -> tuple[DiagnosticRow, ...]:
    """仅在四侧 B/DV 开关显式为布尔值后，组合 N 的完整通道与灯色。"""
    if not (len(days) == len(spx) == len(qqq) == len(ratios)):
        raise ValueError("N 诊断输入时间轴不一致")
    predicates = n_channel_inputs(spx, qqq, ratios, theta_p, k, selection, fixed)
    start = days.index(t0)
    states = {name: "armed" for name in predicates}
    memory = SystemMemory()
    scenario: Scenario = f"N-{e_version}"  # type: ignore[assignment]
    ready = ready_inputs(days, spx, qqq, scenario, fixed)
    result = [DiagnosticRow(t0, scenario, k, theta_p, "绿", "完整", (), "t0 初始快照")]
    for index in range(start + 1, len(days)):
        channel_rows: dict[str, tuple[Literal["黄", "红"], ChannelDay]] = {}
        for name, (level, series) in predicates.items():
            update = update_channel(days[index], states[name], series[index])
            states[name] = update.status
            channel_rows[name] = (level, update)
        system = step_system(days[index], memory, channel_rows, ready[index], k, e_version,
                             fixed.quiet_red_days if fixed else 3,
                             fixed.quiet_all_days if fixed else 5)
        memory = system.memory
        active = tuple(sorted(name for name, (_, channel) in channel_rows.items() if channel.status == "active"))
        result.append(DiagnosticRow(days[index], scenario, k, theta_p, memory.light,
                                    system.data_status, active, system.reason))
    return tuple(result)
