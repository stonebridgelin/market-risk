"""v1.4 唯一主检验（纯计算）：选定的 v1.4 设定对 200 日均线参照行的安全代理择时得分检验及全部敏感性。

统计口径按《v1.4 检验口径与诊断补充登记》A 部分（负责人 2026-10-01 确认）：
- 正式口径为联合重抽样（joint_test）：每次重抽样重算两模型各自的 ē 与同暴露基准；
- 原登记的固定 ē 方法在同一组索引上并列算出，只作条件性对照；
- 前后两半以固定日历日分界，各自重算 ē 与基准、各自完整重抽样；
- 逐事件剔除：删除 [P−20, Tr+20] 内的日期并重算（点估计），原“置零”保留为描述性归因；
- 置零日期超过 1% 时，删除这些日期并重算。
本模块不关心窗口是验证期还是开发期演练；验证期只在锁定后由正式命令运行一次。
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.config import PairedParameters
from market_risk.wavewarn.evaluation import SYMBOLS, CandidateEvaluation
from market_risk.wavewarn.execution import exposure
from market_risk.wavewarn.joint_test import JointDay, JointResult, joint_bootstrap, point_estimate
from market_risk.wavewarn.labels_zz import MergedZZEvent
from market_risk.wavewarn.paired_test import zero_around_event
from market_risk.wavewarn.timing import caution_gap, timing_differences

COMPONENTS = (("危险项", "danger_loss"), ("回撤项", "drawdown_loss"), ("机会项", "opportunity_loss"),
              ("切换项", "switch_cost"), ("漏报罚项", "full_exposure_cost"))
SWITCH_COMPONENT = "切换项"


@dataclass(frozen=True)
class DailyDifference:
    date: dt.date                          # 区间起点
    total: Decimal                         # d_j（ē 固定为评价窗口的值）
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


def without_switch(differences: Sequence[DailyDifference]) -> tuple[DailyDifference, ...]:
    """T价格 的日度差：从 d_j 中去掉切换项（只供开发期的解释性演练）。"""
    return tuple(DailyDifference(row.date, row.total - row.components[SWITCH_COMPONENT],
                                 {name: Decimal(0) if name == SWITCH_COMPONENT else value
                                  for name, value in row.components.items()}, row.zeroed)
                 for row in differences)


def _losses(evaluated: CandidateEvaluation, include_switch: bool) -> tuple[Decimal, ...]:
    return tuple(row.total if include_switch else row.total - row.switch_cost
                 for row in evaluated.daily_losses[:-1])


def joint_days(model: CandidateEvaluation, baseline: CandidateEvaluation, green: CandidateEvaluation,
               red: CandidateEvaluation, eta: Decimal, include_switch: bool) -> tuple[JointDay, ...]:
    """联合重抽样的逐日输入；include_switch 为否时两模型的日损失都不含切换罚分（T价格）。"""
    columns = [_losses(item, include_switch) for item in (model, baseline, green, red)]
    lights = [item.system_executed[:-1] for item in (model, baseline)]
    if len({len(column) for column in (*columns, *lights)}) != 1:
        raise ValueError("两模型与恒定参照的计入区间不一致")
    return tuple(JointDay(model.days[index], columns[0][index], columns[1][index],
                          exposure(lights[0][index], eta), exposure(lights[1][index], eta),  # type: ignore[arg-type]
                          columns[2][index], columns[3][index])
                 for index in range(len(columns[0])))


@dataclass(frozen=True)
class LeaveOneEvent:
    peak_date: dt.date
    trough_date: dt.date
    source: str
    removed: int                      # 删除的区间数（[P−20, Tr+20] 裁剪到窗口）
    remaining: int                    # 剩余区间数
    recomputed: Decimal | None        # 删除后重算 ē 与基准的点估计；没有剩余区间时为空
    recomputed_sign_changed: bool     # 相对于原点估计是否变号（含变为 0）
    zeroed_total: Decimal             # 归因：原固定基准下把同一范围的 d_j 置零后的 Σd_j
    zeroed_sign_changed: bool


@dataclass(frozen=True)
class MainTestResult:
    main: JointResult
    short_block: JointResult
    long_block: JointResult
    split: dt.date
    first_half: JointResult | None
    second_half: JointResult | None
    leave_one: tuple[LeaveOneEvent, ...]
    zero_dates: int
    zero_share: Decimal
    without_zero: JointResult | None
    components: Mapping[str, Decimal]
    mean_model: Decimal
    mean_baseline: Decimal
    caution_gap: Decimal


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


def event_range(days: Sequence[dt.date], peak: dt.date, trough: dt.date, padding: int) -> tuple[int, int]:
    """区间起点在 [P−padding, Tr+padding] 交易日内的行号范围 [start, stop)，裁剪到窗口。"""
    return max(0, bisect_left(days, peak) - padding), min(len(days), bisect_right(days, trough) + padding)


def leave_one_event(days: Sequence[JointDay], totals: Sequence[Decimal], event: MergedZZEvent, padding: int,
                    statistic: Decimal) -> LeaveOneEvent:
    """一个事件：删除并重算的点估计与剩余区间数；另给原固定基准下置零的归因。"""
    dates = [row.date for row in days]
    start, stop = event_range(dates, event.peak_date, event.trough_date, padding)
    kept = [*days[:start], *days[stop:]]
    recomputed = point_estimate(kept).theta if kept else None
    zeroed = sum(zero_around_event(dates, totals, event.peak_date, event.trough_date, padding), Decimal(0))
    return LeaveOneEvent(event.peak_date, event.trough_date, event.source, stop - start, len(kept), recomputed,
                         recomputed is not None and _sign(recomputed) != _sign(statistic), zeroed,
                         _sign(zeroed) != _sign(statistic))


def leave_one_events(days: Sequence[JointDay], totals: Sequence[Decimal], merged: Sequence[MergedZZEvent],
                     padding: int, statistic: Decimal) -> tuple[LeaveOneEvent, ...]:
    """与评价窗口相交的每个合并事件各剔除一次；重叠日期在各自那一次中都删除。"""
    first, last = days[0].date, days[-1].date
    return tuple(leave_one_event(days, totals, event, padding, statistic) for event in merged
                 if event.trough_date >= first and event.peak_date <= last)


def _test(days: Sequence[JointDay], block: int, params: PairedParameters, seed: int) -> JointResult | None:
    return joint_bootstrap(days, block, params.resamples, seed) if days else None


def run_main_test(days: Sequence[JointDay], differences: Sequence[DailyDifference], split: dt.date,
                  merged: Sequence[MergedZZEvent], params: PairedParameters) -> MainTestResult:
    """主设定（区块 20）、区块 10 与 40、前后两半、逐事件剔除、分项分解、置零日期超过 1% 时的删除并重算。"""
    if [row.date for row in days] != [row.date for row in differences]:
        raise ValueError("联合重抽样输入与日度差序列未对齐")
    main = joint_bootstrap(days, params.mean_block_length, params.resamples, params.seed_main)
    short = joint_bootstrap(days, params.short_block_length, params.resamples, params.seed_block_10)
    long = joint_bootstrap(days, params.long_block_length, params.resamples, params.seed_block_40)
    zero_dates = sum(row.zeroed for row in differences)
    retained = [day for day, row in zip(days, differences, strict=True) if not row.zeroed]
    # 删除置零日期会使区块跨越原本不相邻的交易日，只在置零日期超过 1% 时作为敏感性报告。
    without_zero = (_test(retained, params.mean_block_length, params, params.seed_main)
                    if zero_dates * 100 > len(differences) else None)
    components = {name: sum((row.components[name] for row in differences), Decimal(0)) for name, _ in COMPONENTS}
    point = main.point
    return MainTestResult(
        main, short, long, split,
        _test([row for row in days if row.date < split], params.mean_block_length, params, params.seed_main),
        _test([row for row in days if row.date >= split], params.mean_block_length, params, params.seed_main),
        leave_one_events(days, [row.total for row in differences], merged, params.event_padding, point.theta),
        zero_dates, Decimal(zero_dates) / len(differences), without_zero, components, point.mean_model,
        point.mean_baseline, caution_gap(point.mean_model, point.mean_baseline, point.spread, Decimal(0)))


SUPPORTED = "同等平均暴露时，v1.4 在200日均线之外带来了额外的择时价值"
NOT_SUPPORTED = "未证明优于200日均线"


def conclusion(result: MainTestResult, threshold: Decimal) -> str:
    """措辞规则：正式口径主设定 p 小于门槛才可写前一句，否则写后一句。"""
    return SUPPORTED if result.main.formal.p_value < threshold else NOT_SUPPORTED


def fragile_notes(result: MainTestResult, threshold: Decimal) -> tuple[str, ...]:
    """稳健性标注（按正式口径）：不改变判定，但必须全部报告。"""
    notes = []
    supported = result.main.formal.p_value < threshold
    if any((item.formal.p_value < threshold) != supported for item in (result.short_block, result.long_block)):
        notes.append("对依赖结构不稳健（区块 10、40 的结论与主设定不一致）")
    if any(item.recomputed_sign_changed for item in result.leave_one):
        notes.append("结论依赖单一事件（删除某一事件并重算后点估计变号）")
    if any(item.zeroed_sign_changed for item in result.leave_one):
        notes.append("归因：原固定基准下把某一事件的贡献置零后统计量变号")
    return tuple(notes)
