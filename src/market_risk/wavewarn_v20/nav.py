"""净值（登记第六节第 2 小节、第八节第 4 小节；产品规格第五节“杠杆部分的模拟”）。纯计算，float64。

- 区间 j 为第 j 日收盘到第 j+1 日收盘，权重为第 j 日收盘执行的计划目标。
- U_j = ½(R_SPX,j + R_QQQ,j)；组合收益 R_j = 核心权重 × U_j + 杠杆权重 × λ × U_j；现金收益为 0，不计费用。
- 杠杆权重为正时检查 1 + λU_j > 0，不满足即报错。
- 所需的任一价格缺失即“无法计算”，报告缺失日期，不做任何跨期近似。
- 对账：|Σ ln(1 + R_j) − ln W_末| 不得超过给定的容差。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn_v20.execution import (
    PlannedTarget,
    PolicyParameters,
    Position,
    PositionMap,
    SignalRecord,
    SimulatedExecution,
    TargetSource,
    policy_next,
    policy_start,
)


class NavError(ValueError):
    """净值计算失败：杠杆子组合收益因子不为正、出现非有限值、对账不符或输入不合法。"""


class NavUnavailable(NavError):
    """无法计算：净值所需的价格缺失。missing 列出全部缺失的（资产，日期）。"""

    def __init__(self, missing: Sequence[tuple[str, dt.date]]) -> None:
        self.missing = tuple(missing)
        listed = "、".join(f"{asset} {day}" for asset, day in self.missing)
        super().__init__(f"无法计算：净值所需的价格缺失（{listed}）")


@dataclass(frozen=True)
class NavResult:
    """逐日的模拟执行结果（含净值，首日为 1）、逐区间组合收益与对账的两个数。"""

    executions: tuple[SimulatedExecution, ...]
    returns: tuple[float, ...]        # 第 i 个为 executions[i] 到 executions[i + 1] 的组合收益
    log_sum: float                    # Σ ln(1 + R_j)
    log_wealth: float                 # ln W_末

    @property
    def wealth(self) -> tuple[float, ...]:
        return tuple(item.wealth for item in self.executions)


@dataclass(frozen=True)
class PolicyResult:
    """执行政策模拟：逐日的计划目标与模拟执行结果分开保存。"""

    targets: tuple[PlannedTarget, ...]
    nav: NavResult


def basket_returns(days: Sequence[dt.date], closes: dict[str, Sequence[Decimal | None]]) -> tuple[float, ...]:
    """两资产各半的指数日收益 U_j，共 len(days) − 1 个；任一所需价格缺失即抛出“无法计算”。"""
    if len(closes) != 2 or any(len(values) != len(days) for values in closes.values()):
        raise NavError("需要恰好两个资产、且与日期轴等长的收盘价序列")
    missing = [(asset, day) for asset, values in closes.items()
               for day, value in zip(days, values, strict=True) if value is None]
    if missing:
        raise NavUnavailable(sorted(missing, key=lambda item: (item[1], item[0])))
    result: list[float] = []
    for index in range(len(days) - 1):
        changes = [float(values[index + 1]) / float(values[index]) - 1.0     # type: ignore[arg-type]
                   for values in closes.values()]
        result.append(0.5 * (changes[0] + changes[1]))
    return tuple(result)


def portfolio_return(core: float, leverage_weight: float, leverage: float, basket: float) -> float:
    """R_j = 核心权重 × U_j + 杠杆权重 × λ × U_j。杠杆权重为正时先检查 1 + λU_j > 0。"""
    if not math.isfinite(basket):
        raise NavError("指数日收益不是有限值")
    if leverage_weight > 0 and not 1.0 + leverage * basket > 0:
        raise NavError(f"杠杆子组合的每日收益因子不为正：1 + λU = {1.0 + leverage * basket}")
    return core * basket + leverage_weight * leverage * basket


def _reconciled(returns: Sequence[float], wealth: float, tolerance: float) -> tuple[float, float]:
    if not math.isfinite(wealth) or wealth <= 0 or any(not math.isfinite(value) or value <= -1 for value in returns):
        raise NavError("净值或组合收益出现非有限值或不为正")
    log_sum, log_wealth = math.fsum(math.log1p(value) for value in returns), math.log(wealth)
    if abs(log_sum - log_wealth) > tolerance:
        raise NavError(f"对账不符：Σ ln(1 + R) = {log_sum}，ln W_末 = {log_wealth}")
    return log_sum, log_wealth


def _checked_targets(targets: Sequence[PlannedTarget], basket: Sequence[float]) -> None:
    if not targets or len(basket) != len(targets) - 1:
        raise NavError("计划目标的个数必须比区间收益多一个")
    if any(not isinstance(item, PlannedTarget) for item in targets):
        raise NavError("净值只能由计划目标（PlannedTarget）驱动")


def simulate_targets(targets: Sequence[PlannedTarget], basket: Sequence[float], positions: PositionMap,
                     tolerance: float) -> NavResult:
    """按给定的逐日计划目标执行：首个执行日收盘净值记为 1。信号模拟与一直持有都用它。"""
    _checked_targets(targets, basket)
    wealth = 1.0
    executions = [SimulatedExecution(targets[0].day, targets[0].position, targets[0].core, targets[0].leverage, wealth)]
    returns: list[float] = []
    for target, following, value in zip(targets, targets[1:], basket, strict=False):
        change = portfolio_return(target.core, target.leverage, positions.leverage, value)
        wealth *= 1.0 + change
        returns.append(change)
        executions.append(SimulatedExecution(following.day, following.position, following.core, following.leverage,
                                             wealth))
    return NavResult(tuple(executions), tuple(returns), *_reconciled(returns, wealth, tolerance))


def hold_targets(days: Sequence[dt.date], positions: PositionMap) -> tuple[PlannedTarget, ...]:
    """同一组合一直持有：每个执行日的目标恒为正常仓位（R1 的比较基准）。"""
    weights = positions.weights(Position.NORMAL)
    return tuple(PlannedTarget(day, Position.NORMAL, weights.core, weights.leverage, TargetSource.SYSTEM, False)
                 for day in days)


def simulate_hold(days: Sequence[dt.date], basket: Sequence[float], positions: PositionMap, tolerance: float
                  ) -> NavResult:
    return simulate_targets(hold_targets(days, positions), basket, positions, tolerance)


def simulate_policy(days: Sequence[dt.date], signals: Sequence[SignalRecord], basket: Sequence[float],
                    positions: PositionMap, parameters: PolicyParameters, tolerance: float) -> PolicyResult:
    """执行政策模拟：计划目标与净值逐日联动（止损看当日收盘净值，决定下一执行日的目标）。

    signals[0] 为 days[0] 的前一个信号日的记录，signals[i + 1] 为 days[i] 的记录；len(signals) == len(days)。
    """
    if not days or len(signals) != len(days) or len(basket) != len(days) - 1:
        raise NavError("执行日、信号记录与区间收益的个数不匹配")
    if any(signals[index + 1].day != days[index] for index in range(len(days) - 1)):
        raise NavError("信号记录与执行日没有逐日对齐")
    state, target = policy_start(days[0], signals[0], positions)
    wealth = 1.0
    targets = [target]
    executions = [SimulatedExecution(target.day, target.position, target.core, target.leverage, wealth)]
    returns: list[float] = []
    for index in range(len(days) - 1):
        state, following = policy_next(state, index, days[index + 1], signals[index + 1], wealth, positions,
                                       parameters)
        change = portfolio_return(target.core, target.leverage, positions.leverage, basket[index])
        wealth *= 1.0 + change
        returns.append(change)
        target = following
        targets.append(target)
        executions.append(SimulatedExecution(target.day, target.position, target.core, target.leverage, wealth))
    nav = NavResult(tuple(executions), tuple(returns), *_reconciled(returns, wealth, tolerance))
    return PolicyResult(tuple(targets), nav)
