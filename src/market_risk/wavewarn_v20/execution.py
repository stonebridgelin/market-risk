"""执行层（登记第一节第 6、7 小节；产品规格第八节第 1、2 条）。纯计算。

三种记录分开、互不替代：
- SignalRecord：信号计算结果；
- PlannedTarget：由信号日决定、在下一执行日收盘执行的计划目标仓位；
- SimulatedExecution：研究模拟中按计划目标执行的结果（由 nav 模块产生）。
研究计算不产生“实际执行”记录；ActualExecution 只是前瞻运行的接口类型，本包的研究函数都不返回它。

执行层只做直接映射，不叠加第二次阶段恢复：逐级恢复已由风险状态 S 实现。
止损、冷却、重入与重入上限 U 只属于执行政策模拟；净值由调用方逐日给入（nav 模块负责计算）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import pairwise

from market_risk.wavewarn_v20.reference import ReferenceDay
from market_risk.wavewarn_v20.state_machine import Evidence, Risk, SystemDay


class ExecutionError(ValueError):
    """执行层的输入不合法，或出现登记没有覆盖的情形。"""


class Position(Enum):
    NORMAL = "正常"
    LEVEL1 = "一级"
    LEVEL2 = "二级"
    CASH = "全部现金"


class TargetSource(Enum):
    SYSTEM = "系统目标"
    STOP_CASH = "止损现金"
    OUT_CASH = "离场期现金"
    REENTRY_CAP = "重入上限"


@dataclass(frozen=True)
class Weights:
    """组合两部分的权重：核心部分（1 倍）与杠杆部分（每日 λ 倍）。"""

    core: float
    leverage: float


@dataclass(frozen=True)
class PositionMap:
    """状态到仓位的映射与杠杆倍数 λ（登记：正常 0.6/0.4，一级 0.6/0，二级 0.3/0，λ = 2）。"""

    normal: Weights
    level1: Weights
    level2: Weights
    leverage: float

    def weights(self, position: Position) -> Weights:
        if position is Position.NORMAL:
            return self.normal
        if position is Position.LEVEL1:
            return self.level1
        if position is Position.LEVEL2:
            return self.level2
        if position is Position.CASH:
            return Weights(0.0, 0.0)
        raise ExecutionError(f"仓位不在定义之内：{position!r}")

    def exposure(self, position: Position) -> float:
        """总暴露 = 核心 + λ × 杠杆。"""
        weights = self.weights(position)
        return weights.core + self.leverage * weights.leverage

    def capped(self, position: Position) -> Position:
        """重入上限：核心与杠杆分别与一级仓位取小。"""
        weights, cap = self.weights(position), self.level1
        limited = Weights(min(weights.core, cap.core), min(weights.leverage, cap.leverage))
        for candidate in Position:
            if self.weights(candidate) == limited:
                return candidate
        raise ExecutionError(f"重入上限下的仓位 {limited!r} 不是已定义的仓位")


@dataclass(frozen=True)
class PolicyParameters:
    """执行政策模拟的参数（登记：止损线为基准的 96%，冷却 10 个交易日）。"""

    stop_ratio: float
    cooldown: int


@dataclass(frozen=True)
class SignalRecord:
    """信号计算结果。主参照没有 c₁、c₂（为 None），L 在均线不完整时无定义（None）。"""

    day: dt.date
    risk: Risk
    level: int | None
    c1: int | None
    c2: int | None
    all_valid: bool        # 重入与 U 的解除所要求的“当日输入完整”（候选：五个通道全部有效）


@dataclass(frozen=True)
class PlannedTarget:
    """计划目标仓位：在 day 收盘执行。它不是实际仓位。"""

    day: dt.date
    position: Position
    core: float
    leverage: float
    source: TargetSource
    cap_active: bool       # 该目标确定时 U = 一级


@dataclass(frozen=True)
class SimulatedExecution:
    """研究模拟中按计划目标执行的结果：day 收盘执行后的权重与当日收盘的时间加权净值。"""

    day: dt.date
    position: Position
    core: float
    leverage: float
    wealth: float


@dataclass(frozen=True)
class ActualExecution:
    """前瞻运行的实际执行记录（接口）：是否成交、成交价格与实际仓位以实际记录为准，不由计划目标推定。

    本包的研究函数都不构造、不返回这一类型。
    """

    day: dt.date
    executed: bool
    core: float | None
    leverage: float | None
    recorded_at: dt.datetime


@dataclass(frozen=True)
class Switch:
    """一次目标状态切换及其调仓幅度（只作描述）。"""

    day: dt.date
    before: Position
    after: Position
    exposure_change: float     # 目标总暴露的变化量（后 − 前）
    stages: int | None         # 正常、一级、二级之间的阶段变化数；涉及全部现金时无定义


def position_of(risk: Risk) -> Position:
    """系统目标的直接映射：T^系统_{d+1} = 映射(S_d)。"""
    if risk is Risk.NORMAL:
        return Position.NORMAL
    if risk is Risk.LEVEL1:
        return Position.LEVEL1
    if risk is Risk.LEVEL2:
        return Position.LEVEL2
    raise ExecutionError(f"风险状态不在状态域内：{risk!r}")


def signal_records(system: Sequence[SystemDay], evidence: Sequence[Evidence]) -> tuple[SignalRecord, ...]:
    """候选的信号记录：风险状态、L、计数与“五个通道全部有效”。"""
    result: list[SignalRecord] = []
    for item, proof in zip(system, evidence, strict=True):
        if item.day != proof.day:
            raise ExecutionError("系统记录与证据的日期不一致")
        result.append(SignalRecord(item.day, item.risk, item.level, item.c1, item.c2, proof.all_valid))
    return tuple(result)


def reference_signal_records(reference: Sequence[ReferenceDay], all_valid: Sequence[bool]) -> tuple[SignalRecord, ...]:
    """主参照的信号记录；“当日输入完整”由调用方逐日给出。"""
    return tuple(SignalRecord(item.day, item.risk, item.level, None, None, valid)
                 for item, valid in zip(reference, all_valid, strict=True))


def _target(day: dt.date, position: Position, positions: PositionMap, source: TargetSource,
            cap_active: bool) -> PlannedTarget:
    weights = positions.weights(position)
    return PlannedTarget(day, position, weights.core, weights.leverage, source, cap_active)


def _checked_axis(days: Sequence[dt.date], signals: Sequence[SignalRecord]) -> None:
    """signals[0] 是 days[0] 的前一个信号日；signals[i + 1] 的日期是 days[i]。"""
    if not days or len(signals) != len(days):
        raise ExecutionError("执行日与信号记录的个数必须相同且不为零")
    if signals[0].day >= days[0] or any(signals[index + 1].day != days[index] for index in range(len(days) - 1)):
        raise ExecutionError("信号记录与执行日没有逐日对齐")


def signal_targets(days: Sequence[dt.date], signals: Sequence[SignalRecord], positions: PositionMap
                   ) -> tuple[PlannedTarget, ...]:
    """信号模拟的计划目标：第 d+1 日收盘执行映射(S_d)，不叠加第二次阶段恢复。"""
    _checked_axis(days, signals)
    return tuple(_target(day, position_of(signal.risk), positions, TargetSource.SYSTEM, False)
                 for day, signal in zip(days, signals, strict=True))


@dataclass(frozen=True)
class PolicyState:
    """执行政策模拟在某执行日收盘（执行完当日目标之后）的状态。"""

    in_round: bool
    benchmark: float | None     # 本轮开始以来时间加权净值的最高值；本轮尚无可用净值时为 None
    exit_index: int | None      # 止损执行日 s 的序号；持仓期间为 None
    cap: bool                   # 重入上限 U 是否为“一级”


def policy_start(day: dt.date, signal: SignalRecord, positions: PositionMap) -> tuple[PolicyState, PlannedTarget]:
    """窗口起点：第一轮在 j₀ 开始，没有未执行的止损、不在冷却期、U = 无；j₀ 的目标由已收敛的 S_{j₀−1} 决定。"""
    if signal.day >= day:
        raise ExecutionError("窗口起点的信号日必须早于第一个执行日")
    return (PolicyState(True, None, None, False),
            _target(day, position_of(signal.risk), positions, TargetSource.SYSTEM, False))


def policy_next(state: PolicyState, index: int, next_day: dt.date, signal: SignalRecord, wealth: float | None,
                positions: PositionMap, parameters: PolicyParameters) -> tuple[PolicyState, PlannedTarget]:
    """由第 index 个执行日收盘后的状态、当日信号与当日净值，决定下一执行日的状态与计划目标。

    wealth 为 None 表示当日净值尚不可得：不作止损判断，留到净值可得的第一个交易日；缺失不取消已确认的止损。
    """
    if state.in_round:
        benchmark = state.benchmark
        if wealth is not None:
            benchmark = wealth if benchmark is None else max(benchmark, wealth)
        if wealth is not None and benchmark is not None and wealth <= parameters.stop_ratio * benchmark:
            # 止损确认：次日全部现金，任何系统信号都不能取消；U 作废。
            return (PolicyState(False, None, index + 1, False),
                    _target(next_day, Position.CASH, positions, TargetSource.STOP_CASH, False))
        cap = state.cap and not (signal.risk is Risk.NORMAL and signal.all_valid)
        mapped = position_of(signal.risk)
        position = positions.capped(mapped) if cap else mapped
        source = TargetSource.REENTRY_CAP if position is not mapped else TargetSource.SYSTEM
        return PolicyState(True, benchmark, None, cap), _target(next_day, position, positions, source, cap)
    if state.exit_index is None:
        raise ExecutionError("离场状态缺少止损执行日")
    if index >= state.exit_index + parameters.cooldown and signal.risk is Risk.NORMAL and signal.all_valid:
        # 重入：新一轮在下一执行日开始，基准在该日收盘重置；设 U = 一级。
        return (PolicyState(True, None, None, True),
                _target(next_day, positions.capped(Position.NORMAL), positions, TargetSource.REENTRY_CAP, True))
    return state, _target(next_day, Position.CASH, positions, TargetSource.OUT_CASH, False)


_STAGE = {Position.NORMAL: 0, Position.LEVEL1: 1, Position.LEVEL2: 2}


def switches(targets: Sequence[PlannedTarget], positions: PositionMap) -> tuple[Switch, ...]:
    """目标状态切换：某执行日的目标仓位与前一执行日不同计一次（直接跨级也只计一次）；首个执行日的初始建仓不计。"""
    result: list[Switch] = []
    for before, after in pairwise(targets):
        if after.position is before.position:
            continue
        stages = (abs(_STAGE[after.position] - _STAGE[before.position])
                  if before.position in _STAGE and after.position in _STAGE else None)
        result.append(Switch(after.day, before.position, after.position,
                             positions.exposure(after.position) - positions.exposure(before.position), stages))
    return tuple(result)
