"""旧补充历史的标签影响量化（纯计算）：同一条冻结路径上，两套标签下的损失与代理得分之差。

旧补充历史（v1.3 的 P0 稳健性、v1.4 的纯价格版）的损失计算用的是开发期标签（截止 2016-12-30）；
窗口末日为 2009-09-30，应使用按该截止日生成的标签。这里是局部纠错核算，不是重新选参：
信号、执行暴露、日期轴与损失函数都不变，只把逐资产区间的归类（危险、非危险、未定）换成截止日内的标签。
不读写文件，不导入读写模块。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.evaluation import CandidateEvaluation
from market_risk.wavewarn.loss import AssetLossDay
from market_risk.wavewarn.timing import TimingResult

DANGER, NORMAL = "危险", "非危险"
PRICE_COMPONENTS = ("danger_loss", "drawdown_loss", "opportunity_loss")
COMPONENT_NAMES = {"danger_loss": "危险项", "drawdown_loss": "回撤项", "opportunity_loss": "机会项",
                   "switch_cost": "切换项", "full_exposure_cost": "漏报罚项", "total_loss": "主损失 L"}
ALL_COMPONENTS = (*PRICE_COMPONENTS, "switch_cost", "full_exposure_cost")


class ImpactError(ValueError):
    """核算的前提不成立（暴露、日期轴或切换变了），或逐区间的局部调整之和与汇总的变化不一致。"""


def interval_class(row: AssetLossDay) -> str:
    """一个资产区间的归类：危险、非危险，或未定（写出原因：尾段（寻峰）、右截尾（寻底）、跨缺价区间）。"""
    if row.dangerous is None:
        return f"未定：{row.excluded_reason}"
    return DANGER if row.dangerous else NORMAL


@dataclass(frozen=True)
class LabelDifference:
    """两套标签下归类不同的一个资产区间。"""

    symbol: str
    start: dt.date
    end: dt.date
    old_class: str
    new_class: str


def label_differences(old: Mapping[str, Sequence[AssetLossDay]],
                      new: Mapping[str, Sequence[AssetLossDay]]) -> tuple[LabelDifference, ...]:
    """在整个窗口内逐日比对两套标签对每个资产区间的归类，列出全部不同的区间。"""
    if set(old) != set(new):
        raise ImpactError("两套标签下的资产不一致")
    result = []
    for symbol in sorted(old):
        if [row.start for row in old[symbol]] != [row.start for row in new[symbol]]:
            raise ImpactError(f"{symbol} 在两套标签下的日期轴不一致")
        for before, after in zip(old[symbol], new[symbol], strict=True):
            if interval_class(before) != interval_class(after):
                result.append(LabelDifference(symbol, before.start, before.end, interval_class(before),
                                              interval_class(after)))
    return tuple(sorted(result, key=lambda item: (item.start, item.symbol)))


@dataclass(frozen=True)
class Totals:
    """一个对象在一套标签下的主损失分项、主损失、平均暴露、同暴露基准与择时得分。"""

    parts: Mapping[str, Decimal]               # 五个分项
    total_loss: Decimal
    mean_exposure: Decimal
    benchmark_loss: Decimal
    score: Decimal                             # T
    billed_switches: int
    intervals: int


def totals_of(evaluated: CandidateEvaluation, timing: TimingResult) -> Totals:
    parts = {name: sum((getattr(row, name) for row in evaluated.daily_losses), Decimal(0)) for name in ALL_COMPONENTS}
    return Totals(parts, evaluated.total_loss, timing.mean_exposure, timing.benchmark_loss, timing.score,
                  evaluated.billed_switches, timing.intervals)


def local_adjustment(old: CandidateEvaluation, new: CandidateEvaluation, differences: Sequence[LabelDifference],
                     weights: Mapping[str, Decimal]) -> dict[str, dict[str, Decimal]]:
    """只看归类不同的那些资产区间：资产 → 分项 → 权重 ×（新损失 − 原损失）之和。"""
    result = {symbol: dict.fromkeys(PRICE_COMPONENTS, Decimal(0)) for symbol in weights}
    for symbol in weights:
        index = {row.start: number for number, row in enumerate(old.asset_losses[symbol])}
        for item in differences:
            if item.symbol != symbol:
                continue
            before, after = old.asset_losses[symbol][index[item.start]], new.asset_losses[symbol][index[item.start]]
            if before.exposure != after.exposure:
                raise ImpactError(f"{symbol} {item.start} 的执行暴露在两套标签下不同")
            for name in PRICE_COMPONENTS:
                result[symbol][name] += weights[symbol] * (getattr(after, name) - getattr(before, name))
    return result


def local_total(adjustment: Mapping[str, Mapping[str, Decimal]]) -> Decimal:
    return sum((value for parts in adjustment.values() for value in parts.values()), Decimal(0))


@dataclass(frozen=True)
class ObjectImpact:
    """一个对象（候选设定或参照行）在两套标签下的结果与局部调整。"""

    name: str
    role: str                                  # 候选设定 或 参照行
    old: Totals
    new: Totals
    local: Mapping[str, Mapping[str, Decimal]]         # 资产 → 分项 → 局部调整
    local_score: Decimal                               # 由局部调整按线性口径推出的 T 的变化

    def change(self, name: str) -> Decimal:
        if name == "total_loss":
            return self.new.total_loss - self.old.total_loss
        if name == "score":
            return self.new.score - self.old.score
        return self.new.parts[name] - self.old.parts[name]


def reconcile(item: ObjectImpact, tolerance: Decimal) -> tuple[Decimal, ...]:
    """核对：日期轴、暴露与切换不变；逐区间的局部调整之和等于汇总的变化（三个价格分项、主损失、T）。

    返回五个差（局部之和 − 汇总的变化）；任何一个的绝对值超过 tolerance 即报错停下。
    """
    old, new = item.old, item.new
    if (old.intervals, old.mean_exposure, old.billed_switches) != (new.intervals, new.mean_exposure,
                                                                   new.billed_switches):
        raise ImpactError(f"{item.name}：区间数、平均暴露或计费切换次数在两套标签下不同")
    if old.parts["switch_cost"] != new.parts["switch_cost"]:
        raise ImpactError(f"{item.name}：切换项在两套标签下不同")
    gaps = [sum((item.local[symbol][name] for symbol in item.local), Decimal(0)) - item.change(name)
            for name in PRICE_COMPONENTS]
    penalty = item.change("full_exposure_cost")
    gaps.append(local_total(item.local) + penalty - item.change("total_loss"))
    gaps.append(item.local_score - item.change("score"))
    if any(abs(value) > tolerance for value in gaps):
        raise ImpactError(f"{item.name}：逐区间的局部调整之和与汇总的变化不一致")
    return tuple(gaps)


def object_impact(name: str, role: str, old: tuple[CandidateEvaluation, TimingResult],
                  new: tuple[CandidateEvaluation, TimingResult], differences: Sequence[LabelDifference],
                  weights: Mapping[str, Decimal], green_local: Decimal, red_local: Decimal) -> ObjectImpact:
    """green_local、red_local 为“始终绿”“始终红”两条参照行主损失的局部调整之和。

    T = L − [ē·L_G + (1−ē)·L_R]，ē 不变，所以 ΔT = ΔL − ē·ΔL_G − (1−ē)·ΔL_R（线性口径）。
    """
    local = local_adjustment(old[0], new[0], differences, weights)
    before, after = totals_of(*old), totals_of(*new)
    penalty = after.parts["full_exposure_cost"] - before.parts["full_exposure_cost"]
    exposure = before.mean_exposure
    score = local_total(local) + penalty - exposure * green_local - (1 - exposure) * red_local
    return ObjectImpact(name, role, before, after, local, score)


def ranking(items: Sequence[ObjectImpact], field: str, new: bool) -> tuple[str, ...]:
    """按主损失或 T 从小到大排列的对象名（越小越好）；field 为 total_loss 或 score。"""
    def key(item: ObjectImpact) -> Decimal:
        totals = item.new if new else item.old
        return totals.total_loss if field == "total_loss" else totals.score
    return tuple(item.name for item in sorted(items, key=key))


@dataclass(frozen=True)
class Comparison:
    """一个候选设定相对一条参照行的差值（设定 − 参照），在两套标签下。"""

    setting: str
    reference: str
    field: str                                 # total_loss 或 score
    old: Decimal
    new: Decimal

    @property
    def direction_changed(self) -> bool:
        """比较方向（正、负、零）是否改变。"""
        return (self.old > 0) != (self.new > 0) or (self.old < 0) != (self.new < 0)


def comparisons(settings: Sequence[ObjectImpact], references: Sequence[ObjectImpact]) -> tuple[Comparison, ...]:
    result = []
    for item in settings:
        for reference in references:
            for field in ("total_loss", "score"):
                values = [(getattr(side(item), field) - getattr(side(reference), field))
                          for side in (lambda x: x.old, lambda x: x.new)]
                result.append(Comparison(item.name, reference.name, field, values[0], values[1]))
    return tuple(result)
