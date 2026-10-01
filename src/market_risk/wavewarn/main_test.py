"""v1.4 唯一主检验（纯计算）：选定的 v1.4 设定对 200 日均线参照行的择时得分配对检验及全部敏感性。

日度差 d_j 只按整个评价窗口的 ē 计算一次；前后两半、逐事件剔除、删除置零日期都在同一条 d_j 序列上
取子集、置零或删除，不重算 ē（负责人 2026-10-01 确认）。
本模块不关心窗口是验证期还是开发期演练；验证期只在锁定后由正式命令运行一次。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.config import PairedParameters
from market_risk.wavewarn.evaluation import SYMBOLS, CandidateEvaluation
from market_risk.wavewarn.labels_zz import MergedZZEvent
from market_risk.wavewarn.paired_test import PairedResult, paired_stationary_test, zero_around_event
from market_risk.wavewarn.timing import caution_gap, timing_differences

COMPONENTS = (("危险项", "danger_loss"), ("回撤项", "drawdown_loss"), ("机会项", "opportunity_loss"),
              ("切换项", "switch_cost"), ("漏报罚项", "full_exposure_cost"))


@dataclass(frozen=True)
class DailyDifference:
    date: dt.date                          # 区间起点
    total: Decimal                         # d_j
    components: Mapping[str, Decimal]      # d_j 按损失分项的分解，各项之和等于 d_j
    zeroed: bool                           # 当日有资产区间被排除（价格类损失对各模型都记 0）


def _series(evaluated: CandidateEvaluation, field: str) -> tuple[Decimal, ...]:
    """计入区间（不含窗口末日那一行）的逐日损失或某一分项。"""
    return tuple(getattr(row, field) for row in evaluated.daily_losses[:-1])


def daily_differences(model: CandidateEvaluation, baseline: CandidateEvaluation, green: CandidateEvaluation,
                      red: CandidateEvaluation, mean_model: Decimal,
                      mean_baseline: Decimal) -> tuple[DailyDifference, ...]:
    """d_j = [ℓ_M − ē_M g − (1−ē_M) r] − [ℓ_B − ē_B g − (1−ē_B) r]，并按分项分解。"""
    def difference(field: str) -> tuple[Decimal, ...]:
        return timing_differences(_series(model, field), _series(baseline, field), _series(green, field),
                                  _series(red, field), mean_model, mean_baseline)

    totals = difference("total")
    parts = {name: difference(field) for name, field in COMPONENTS}
    zeroed = [any(bool(model.asset_losses[symbol][index].excluded_reason) for symbol in SYMBOLS)
              for index in range(len(totals))]
    return tuple(DailyDifference(model.days[index], totals[index],
                                 {name: values[index] for name, values in parts.items()}, zeroed[index])
                 for index in range(len(totals)))


@dataclass(frozen=True)
class LeaveOneEvent:
    peak_date: dt.date
    trough_date: dt.date
    source: str
    total: Decimal               # 把 [P−20, Tr+20] 内的 d_j 置零后的 Σd_j
    sign_changed: bool           # 相对于原统计量是否变号（含变为 0）


@dataclass(frozen=True)
class MainTestResult:
    main: PairedResult
    short_block: PairedResult
    long_block: PairedResult
    split: dt.date
    first_half: PairedResult | None
    second_half: PairedResult | None
    leave_one: tuple[LeaveOneEvent, ...]
    zero_dates: int
    zero_share: Decimal
    without_zero: PairedResult | None
    components: Mapping[str, Decimal]
    mean_model: Decimal
    mean_baseline: Decimal
    caution_gap: Decimal


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


def leave_one_events(days: Sequence[dt.date], totals: Sequence[Decimal], merged: Sequence[MergedZZEvent],
                     padding: int) -> tuple[LeaveOneEvent, ...]:
    """与评价窗口相交的每个合并事件：把 [P−20, Tr+20] 内的 d_j 置零后重算 Σd_j。"""
    statistic = sum(totals, Decimal(0))
    result = []
    for event in merged:
        if event.trough_date < days[0] or event.peak_date > days[-1]:
            continue
        total = sum(zero_around_event(days, totals, event.peak_date, event.trough_date, padding), Decimal(0))
        result.append(LeaveOneEvent(event.peak_date, event.trough_date, event.source, total,
                                    _sign(total) != _sign(statistic)))
    return tuple(result)


def _test(values: Sequence[Decimal], block: int, params: PairedParameters, seed: int) -> PairedResult | None:
    return paired_stationary_test(values, block, params.resamples, seed) if values else None


def run_main_test(differences: Sequence[DailyDifference], split: dt.date, merged: Sequence[MergedZZEvent],
                  params: PairedParameters, mean_model: Decimal, mean_baseline: Decimal, green_loss: Decimal,
                  red_loss: Decimal) -> MainTestResult:
    """主设定（区块 20）、区块 10 与 40、前后两半、逐事件剔除、分项分解、置零日期超过 1% 时的删除敏感性。"""
    days = [row.date for row in differences]
    totals = [row.total for row in differences]
    main = paired_stationary_test(totals, params.mean_block_length, params.resamples, params.seed_main)
    short = paired_stationary_test(totals, params.short_block_length, params.resamples, params.seed_block_10)
    long = paired_stationary_test(totals, params.long_block_length, params.resamples, params.seed_block_40)
    first = [row.total for row in differences if row.date < split]
    second = [row.total for row in differences if row.date >= split]
    zero_dates = sum(row.zeroed for row in differences)
    retained = [row.total for row in differences if not row.zeroed]
    # 删除置零日期会使区块跨越原本不相邻的交易日，只在置零日期超过 1% 时作为敏感性报告。
    without_zero = (_test(retained, params.mean_block_length, params, params.seed_main)
                    if zero_dates * 100 > len(differences) else None)
    components = {name: sum((row.components[name] for row in differences), Decimal(0)) for name, _ in COMPONENTS}
    return MainTestResult(
        main, short, long, split, _test(first, params.mean_block_length, params, params.seed_main),
        _test(second, params.mean_block_length, params, params.seed_main),
        leave_one_events(days, totals, merged, params.event_padding), zero_dates,
        Decimal(zero_dates) / len(differences), without_zero, components, mean_model, mean_baseline,
        caution_gap(mean_model, mean_baseline, green_loss, red_loss))


SUPPORTED = "同等平均暴露时，v1.4 在200日均线之外带来了额外的择时价值"
NOT_SUPPORTED = "未证明优于200日均线"


def conclusion(result: MainTestResult, threshold: Decimal) -> str:
    """措辞规则：主设定 p 小于门槛才可写前一句，否则写后一句。"""
    return SUPPORTED if result.main.p_value < threshold else NOT_SUPPORTED


def fragile_notes(result: MainTestResult, threshold: Decimal) -> tuple[str, ...]:
    """稳健性标注：不改变判定，但必须全部报告。"""
    notes = []
    supported = result.main.p_value < threshold
    if any((item.p_value < threshold) != supported for item in (result.short_block, result.long_block)):
        notes.append("对依赖结构不稳健（区块 10、40 的结论与主设定不一致）")
    if any(item.sign_changed for item in result.leave_one):
        notes.append("结论依赖单一事件（剔除某一事件后统计量变号）")
    return tuple(notes)
