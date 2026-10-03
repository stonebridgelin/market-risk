"""净值（登记第六节第 2 小节、第八节第 4 小节；产品规格第五节“杠杆部分的模拟”）。纯计算，float64。

- 区间 j 为第 j 日收盘到第 j+1 日收盘，权重为第 j 日收盘执行的计划目标。
- U_j = ½(R_SPX,j + R_QQQ,j)；组合收益 R_j = 核心权重 × U_j + 杠杆权重 × λ × U_j；现金收益为 0，不计费用。
- 杠杆权重为正时检查 1 + λU_j > 0，不满足即报错。
- 所需的任一价格缺失即“无法计算”，报告缺失日期，不做任何跨期近似。
- 执行政策模拟允许窗口内有缺价时（simulate_policy_with_gaps，补充裁决 Q3）：只保留第一个缺价日之前已确定的
  执行、止损与轮次记录，结果类型为 PartialPolicyResult，与完整结果 PolicyResult 区分；完整净值仍为“无法计算”。
  “无法确定”（Undetermined）只由研究模拟层判定；规则函数 execution.policy_next 保持登记行为（第九节分层裁决）。
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
    """执行政策模拟（窗口内价格完整）：逐日的计划目标与模拟执行结果分开保存。这是完整的评价结果。"""

    targets: tuple[PlannedTarget, ...]
    nav: NavResult


@dataclass(frozen=True)
class Undetermined:
    """研究模拟中，执行政策的路径从 day 起无法唯一确定（补充裁决 Q3，严格研究口径；第九节分层裁决）。

    持仓期间（本轮之内）当日净值不可得时，研究模拟无法判断止损是否触发；此后的目标、止损、冷却与重入都依赖
    这一判断，一律无法确定。它不是“止损未触发”。这一判定只属于研究模拟层，由 simulate_policy_with_gaps 在
    调用规则函数 policy_next 之前作出；规则函数本身保持登记行为（净值不可得时推迟到净值可得的第一个交易日判断）。
    """

    day: dt.date            # 第一个无法确定计划目标的执行日
    reason: str


UNDETERMINED_REASON = "持仓期间当日净值不可得，止损是否触发无法判断"


@dataclass(frozen=True)
class PartialPolicyResult:
    """执行政策模拟（窗口内有缺价，补充裁决 Q3）：**不是完整评价结果**，不得当作 PolicyResult 使用。

    - 窗口的完整净值为“无法计算”，missing 列出全部缺失的（资产，日期）；
    - executions 只保留第一个缺价日之前、净值已确定的各执行日（首日净值为 1），returns 为其间的组合收益；
    - targets 保留能够唯一确定的计划目标（缺价之前的，以及缺价之后离场期间只依赖信号的）；
    - undetermined 为此后路径无法唯一确定的起点（None 表示到窗口末日都能确定，例如一直处于离场期）；
      其后的目标、止损、冷却与重入都无法确定。价格恢复之后不补算缺口，也不重置止损基准继续计算。
    """

    targets: tuple[PlannedTarget, ...]
    executions: tuple[SimulatedExecution, ...]
    returns: tuple[float, ...]
    undetermined: Undetermined | None
    missing: tuple[tuple[str, dt.date], ...]


@dataclass(frozen=True)
class BasketPrefix:
    """第一个缺价日之前的指数日收益：values[j] 为第 j 日收盘到第 j+1 日收盘，只含两端价格都存在的区间。

    first_missing 为第一个有缺价的日期在日期轴上的行号（没有缺价时为 None）；missing 列出全部缺失的（资产，日期）。
    """

    values: tuple[float, ...]
    first_missing: int | None
    missing: tuple[tuple[str, dt.date], ...]


def basket_prefix(days: Sequence[dt.date], closes: dict[str, Sequence[Decimal | None]]) -> BasketPrefix:
    """两资产各半的指数日收益 U_j，只算到第一个缺价日之前；不跳过、不插补，缺价之后的区间一概不算。"""
    if len(closes) != 2 or any(len(values) != len(days) for values in closes.values()):
        raise NavError("需要恰好两个资产、且与日期轴等长的收盘价序列")
    missing = sorted(((asset, day) for asset, values in closes.items()
                      for day, value in zip(days, values, strict=True) if value is None),
                     key=lambda item: (item[1], item[0]))
    first_missing = next((index for index in range(len(days))
                          if any(values[index] is None for values in closes.values())), None)
    stop = len(days) - 1 if first_missing is None else max(first_missing - 1, 0)
    result: list[float] = []
    for index in range(stop):
        changes = [float(values[index + 1]) / float(values[index]) - 1.0     # type: ignore[arg-type]
                   for values in closes.values()]
        result.append(0.5 * (changes[0] + changes[1]))
    return BasketPrefix(tuple(result), first_missing, tuple(missing))


def basket_returns(days: Sequence[dt.date], closes: dict[str, Sequence[Decimal | None]]) -> tuple[float, ...]:
    """两资产各半的指数日收益 U_j，共 len(days) − 1 个；任一所需价格缺失即抛出“无法计算”。"""
    prefix = basket_prefix(days, closes)
    if prefix.missing:
        raise NavUnavailable(prefix.missing)
    return prefix.values


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


def simulate_policy_with_gaps(days: Sequence[dt.date], signals: Sequence[SignalRecord],
                              closes: dict[str, Sequence[Decimal | None]], positions: PositionMap,
                              parameters: PolicyParameters, tolerance: float) -> PolicyResult | PartialPolicyResult:
    """执行政策模拟，允许窗口内有缺价（补充裁决 Q3）。

    价格完整时直接调用 simulate_policy，返回 PolicyResult。有缺价时返回 PartialPolicyResult（第九节第 4 条的时间边界）：
    设第 m 日收盘价首次缺失（行号 m）——
    - 区间收益：第 m−1 日收盘到第 m 日收盘的区间起无法计算；逐日净值：第 m 日起无法计算，价格恢复也不再可计算；
    - 第 m 日的计划目标由第 m−1 日已确定的信息产生，保留；
    - 研究层在调用规则函数 policy_next 之前自行判定：本轮之内（持仓期间，含重入后需以当日净值重置基准）而当日净值
      不可得时，**不调用** policy_next，从下一执行日的目标起标为“无法确定”（Undetermined），停止推演；
    - 离场期间（止损已确认，冷却与重入资格只依赖信号）照常调用 policy_next，保留只依赖信号的目标与重入；
    - 不补算缺口，不自行重置止损基准后继续推演。
    """
    if not days or len(signals) != len(days):
        raise NavError("执行日与信号记录的个数不匹配")
    if any(signals[index + 1].day != days[index] for index in range(len(days) - 1)):
        raise NavError("信号记录与执行日没有逐日对齐")
    prefix = basket_prefix(days, closes)
    if prefix.first_missing is None:
        return simulate_policy(days, signals, prefix.values, positions, parameters, tolerance)
    known = prefix.first_missing                         # 第 0 至 known − 1 日的净值已确定
    state, target = policy_start(days[0], signals[0], positions)
    targets = [target]
    wealth: float | None = 1.0 if known > 0 else None
    executions = [SimulatedExecution(target.day, target.position, target.core, target.leverage, 1.0)] if known else []
    returns: list[float] = []
    undetermined: Undetermined | None = None
    for index in range(len(days) - 1):
        if state.in_round and wealth is None:
            # 本轮之内止损无法判断：不调用规则函数，从下一执行日的目标起“无法确定”。
            undetermined = Undetermined(days[index + 1], UNDETERMINED_REASON)
            break
        state, following = policy_next(state, index, days[index + 1], signals[index + 1], wealth, positions,
                                       parameters)
        if index + 1 < known:                           # 第 index 至 index + 1 日的区间在缺价之前
            change = portfolio_return(target.core, target.leverage, positions.leverage, prefix.values[index])
            returns.append(change)
            wealth = executions[-1].wealth * (1.0 + change)
            executions.append(SimulatedExecution(following.day, following.position, following.core,
                                                 following.leverage, wealth))
        else:
            wealth = None                               # 缺价之后净值不可得，不再恢复
        target = following
        targets.append(target)
    if returns:
        _reconciled(returns, executions[-1].wealth, tolerance)
    return PartialPolicyResult(tuple(targets), tuple(executions), tuple(returns), undetermined, prefix.missing)
