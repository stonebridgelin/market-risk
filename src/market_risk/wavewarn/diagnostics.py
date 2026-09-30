"""开发期 P0 与 P1-E1/E2/E3 实现诊断；不读取研究标签或计算主损失。"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from market_risk.wavewarn.channels import (
    ChannelDay,
    ChannelPredicate,
    breadth_predicates,
    price_predicates,
    update_channel,
    volatility_predicates,
)
from market_risk.wavewarn.config import CandidateSets, ChannelSelection, FixedParameters
from market_risk.wavewarn.convergence import system_convergence
from market_risk.wavewarn.features import AssetFeatures, rolling_high, vix_term_ratio
from market_risk.wavewarn.inputs import DevelopmentInputs
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


def _ready_inputs(days: Sequence[dt.date], spx: Sequence[AssetFeatures], qqq: Sequence[AssetFeatures],
                  scenario: Scenario, fixed: FixedParameters | None = None) -> tuple[ReadyInputs, ...]:
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
    ready = _ready_inputs(days, spx, qqq, scenario, fixed)
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


def write_development_diagnostics(inputs: DevelopmentInputs, spx: Sequence[AssetFeatures],
                                  qqq: Sequence[AssetFeatures], destination: Path,
                                  fixed: FixedParameters,
                                  candidates: CandidateSets) -> tuple[dt.date, int]:
    """遍历全部预登记候选，不排名；P1 三个 E 版本分别输出。"""
    t0 = first_complete_day(inputs.days, spx, qqq, inputs.series["VIX"], inputs.series["VIX3M"], fixed)
    k_values = candidates.k
    theta_values = candidates.theta_p
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("date", "scenario", "k", "theta_p", "light", "data_status", "active_channels", "reason"))
        for k in k_values:
            for theta in theta_values:
                for scenario in ("P0", "P1-E1", "P1-E2", "P1-E3"):
                    for row in diagnostic_sequence(inputs.days, spx, qqq, t0, scenario, theta, k, fixed):
                        writer.writerow((row.date.isoformat(), row.scenario, row.k, str(row.theta_p),
                                         row.light, row.data_status, ";".join(row.active_channels), row.reason))
                        count += 1
    return t0, count


def write_convergence_diagnostics(inputs: DevelopmentInputs, spx: Sequence[AssetFeatures],
                                  qqq: Sequence[AssetFeatures], destination: Path,
                                  fixed: FixedParameters,
                                  candidates: CandidateSets) -> tuple[dt.date, int]:
    """分别列出 P0 与 P1 三种 E 护栏的全部候选收敛日；不确定统一 τ。"""
    t0 = first_complete_day(inputs.days, spx, qqq, inputs.series["VIX"], inputs.series["VIX3M"], fixed)
    k_values = candidates.k
    theta_values = candidates.theta_p
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("scenario", "k", "theta_p", "t0", "channel_convergence_dates",
                         "system_start", "system_convergence_date"))
        for k in k_values:
            for theta in theta_values:
                channels: dict[str, tuple[Literal["黄", "红"], tuple[ChannelPredicate, ...]]] = {}
                for symbol, items in (("SPX", spx), ("QQQ", qqq)):
                    channels[f"P_{symbol}"] = ("黄", price_predicates(items, theta, k))
                    channels[f"PR_{symbol}"] = ("红", price_predicates(items, theta, k, red=True))
                for scenario in ("P0", "P1-E1", "P1-E2", "P1-E3"):
                    version: Literal["P0", "E1", "E2", "E3"] = "P0" if scenario == "P0" else scenario[-2:]
                    ready = _ready_inputs(inputs.days, spx, qqq, scenario, fixed)
                    result = system_convergence(inputs.days, channels, ready, t0, k, version, fixed)
                    dates = ";".join(f"{name}:{date.isoformat() if date else '未收敛'}"
                                     for name, date in sorted(result.channel_dates.items()))
                    writer.writerow((scenario, k, str(theta), t0.isoformat(), dates,
                                     result.system_start.isoformat() if result.system_start else "",
                                     result.system_date.isoformat() if result.system_date else ""))
                    count += 1
    return t0, count


def _channel_snapshot(days: Sequence[dt.date], predicates: Sequence[ChannelPredicate],
                      t0: dt.date) -> tuple[ChannelDay, ...]:
    """初始已武装快照；次日开始处理输入。"""
    start = days.index(t0)
    rows = [ChannelDay(t0, "armed", True, "t0 初始快照")]
    state = "armed"
    for index in range(start + 1, len(days)):
        update = update_channel(days[index], state, predicates[index])
        rows.append(update)
        state = update.status
    return tuple(rows)


def write_independent_channel_diagnostics(inputs: DevelopmentInputs,
                                          spx_q10: Sequence[AssetFeatures], qqq_q10: Sequence[AssetFeatures],
                                          spx_q20: Sequence[AssetFeatures], qqq_q20: Sequence[AssetFeatures],
                                          destination: Path, fixed: FixedParameters,
                                          candidates: CandidateSets) -> tuple[dt.date, int]:
    """输出全部独立通道状态；N 四侧开关未定，不生成 N 系统灯色。"""
    t0 = first_complete_day(inputs.days, spx_q10, qqq_q10,
                            inputs.series["VIX"], inputs.series["VIX3M"], fixed)
    k_values = candidates.k
    theta_values = candidates.theta_p
    q_values = candidates.q
    ratios = tuple(vix_term_ratio(inputs.series["VIX"].get(day), inputs.series["VIX3M"].get(day))
                   for day in inputs.days)
    scenarios: list[tuple[str, str, str, str, tuple[ChannelPredicate, ...]]] = []
    for k in k_values:
        for theta in theta_values:
            for symbol, features in (("SPX", spx_q10), ("QQQ", qqq_q10)):
                scenarios.append((f"P_{symbol}", str(k), str(theta), "", price_predicates(features, theta, k)))
                scenarios.append((f"PR_{symbol}", str(k), str(theta), "",
                                  price_predicates(features, theta, k, red=True)))
    for q, assets in ((q_values[0], (("SPX", spx_q10), ("QQQ", qqq_q10))),
                      (q_values[1], (("SPX", spx_q20), ("QQQ", qqq_q20)))):
        for symbol, features in assets:
            scenarios.append((f"B_{symbol}", "", "", str(q), breadth_predicates(features, "B", fixed)))
    for symbol, features in (("SPX", spx_q10), ("QQQ", qqq_q10)):
        scenarios.append((f"DV_{symbol}", "", "", "", breadth_predicates(features, "DV", fixed)))
        scenarios.append((f"BW_{symbol}", "", "", "", breadth_predicates(features, "BW", fixed)))
    scenarios.append(("V", "", "", "", volatility_predicates(ratios, fixed)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("date", "channel", "k", "theta_p", "q", "status", "valid", "reason"))
        for name, k, theta, q, predicates in scenarios:
            for row in _channel_snapshot(inputs.days, predicates, t0):
                writer.writerow((row.date.isoformat(), name, k, theta, q, row.status,
                                 "是" if row.valid else "否", row.reason))
                count += 1
    return t0, count


def n_diagnostic_sequence(days: Sequence[dt.date], spx: Sequence[AssetFeatures],
                          qqq: Sequence[AssetFeatures], ratios: Sequence[Decimal | None],
                          t0: dt.date, theta_p: Decimal, k: int,
                          selection: ChannelSelection,
                          e_version: Literal["E1", "E2", "E3"],
                          fixed: FixedParameters | None = None) -> tuple[DiagnosticRow, ...]:
    """仅在四侧 B/DV 开关显式为布尔值后，组合 N 的完整通道与灯色。"""
    if not (len(days) == len(spx) == len(qqq) == len(ratios)):
        raise ValueError("N 诊断输入时间轴不一致")
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
    start = days.index(t0)
    states = {name: "armed" for name in predicates}
    memory = SystemMemory()
    scenario: Scenario = f"N-{e_version}"  # type: ignore[assignment]
    ready = _ready_inputs(days, spx, qqq, scenario, fixed)
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


def write_development_missing_audit(state_path: Path, channel_path: Path,
                                    destination: Path) -> int:
    """从开发期逐日明细汇总沿用/无效数量，不涉及标签或损失。"""
    counts: Counter[tuple[str, ...]] = Counter()
    with state_path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if row["data_status"] == "沿用":
                counts[("系统", row["scenario"], row["k"], row["theta_p"], "", "沿用")] += 1
    with channel_path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if row["valid"] == "否":
                counts[("通道", row["channel"], row["k"], row["theta_p"], row["q"], row["reason"])] += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("kind", "name", "k", "theta_p", "q", "reason", "days"))
        writer.writerows((*key, count) for key, count in sorted(counts.items()))
    return sum(counts.values())


def write_input_coverage(inputs: DevelopmentInputs, destination: Path) -> None:
    """分开列出序列未开始与开始后的交易日缺口。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("series", "first_date", "last_date", "observations", "not_started_days",
                         "gap_days", "off_calendar_rows"))
        for name, values in sorted(inputs.series.items()):
            if not values:
                writer.writerow((name, "", "", 0, len(inputs.days), 0, 0))
                continue
            first, last = min(values), max(values)
            not_started = sum(day < first for day in inputs.days)
            gaps = sum(first <= day <= last and day not in values for day in inputs.days)
            off_calendar = len(set(values) - set(inputs.days))
            writer.writerow((name, first.isoformat(), last.isoformat(), len(values), not_started,
                             gaps, off_calendar))
