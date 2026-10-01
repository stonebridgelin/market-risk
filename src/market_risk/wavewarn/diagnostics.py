"""开发期 P0 与 P1-E1/E2/E3 实现诊断；不读取研究标签或计算主损失。"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from collections.abc import Sequence
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
from market_risk.wavewarn.config import CandidateSets, FixedParameters
from market_risk.wavewarn.convergence import system_convergence
from market_risk.wavewarn.features import AssetFeatures, anchor_126_audit, vix_term_ratio
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.state_sequences import (
    diagnostic_sequence,
    first_complete_day,
    ready_inputs,
)


def write_anchor_126_audit(inputs: DevelopmentInputs, destination: Path,
                           fixed: FixedParameters, candidates: CandidateSets) -> int:
    """P 与 PR 分开列出126日锚点会触发、63日锚点不触发的开发期日期。"""
    count = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("symbol", "channel", "theta_p", "entry_threshold", "date",
                         "drawdown_63", "drawdown_126"))
        for symbol in ("SPX", "QQQ"):
            for theta in candidates.theta_p:
                for channel, threshold in (("P", theta), ("PR", 2 * theta)):
                    for day, short, long in anchor_126_audit(inputs.days, inputs.series[symbol],
                                                              threshold, fixed):
                        writer.writerow((symbol, channel, theta, threshold, day, short, long))
                        count += 1
    return count


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
                    ready = ready_inputs(inputs.days, spx, qqq, scenario, fixed)
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
