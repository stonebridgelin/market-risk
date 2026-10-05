"""阶段四开发期运行的描述性对照、对账与报告统计（阶段四字段级设计稿第一节第 2 小节、第五节、第六节、第十一节；
M2 第一部分指令第二节第 4 小节）。算法侧，纯计算：不读写文件，不取当前时间，不使用随机量。

- 三项描述性对照按设计稿第五节第 1—4 小节（J-4 按推荐裁决）：200 日均线一级版、带缓冲带的 200 日均线二级版
  （101%/99%）、同平均暴露的恒定仓位。不可评价与无法计算不停止；计算失败（未预期异常、非有限值、对账不符、
  杠杆子组合收益因子不为正）向上抛出，由入口整体停止。
- 固定区间暴露替换算术对照按设计稿第五节第 5 小节；通道状态按同参同切片重建，并与组合层系统记录逐日精确比较。
- 分段（收益末日归属）由组合层的 SegmentReport 给出；市场环境（收益起点归属）在这里按设计稿第一节第 2 小节计算。
- 对账按设计稿第六节：容差只用于两类对数净值对账，比较写成 abs(差) <= 容差；状态可重建精确相等。
  不调用 confirmatory 的任何入口。
- 报告统计按设计稿第十一节；四项状态统计的读法依《M2 第一部分修订二·补充二》第二节。
所有描述性结果不参与筛选、排序或确认性检验，不新增扫描参数。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from market_risk.wavewarn_v20.convergence import first_common_index
from market_risk.wavewarn_v20.execution import (
    PlannedTarget,
    Position,
    PositionMap,
    SignalRecord,
    Switch,
    TargetSource,
    reference_signal_records,
    signal_targets,
    switches,
)
from market_risk.wavewarn_v20.inputs import TrendDay, below_average, snapshot_asset_days, snapshot_trend_days
from market_risk.wavewarn_v20.nav import (
    NavResult,
    PartialPolicyResult,
    PolicyResult,
    portfolio_return,
    simulate_policy_with_gaps,
    simulate_targets,
)
from market_risk.wavewarn_v20.r1 import max_drawdown
from market_risk.wavewarn_v20.reference import ReferenceDay, run_reference
from market_risk.wavewarn_v20.research_run import (
    ASSETS,
    HOLD,
    MISSING_PRICE,
    REFERENCE,
    SOURCE_PREFIX,
    DevelopmentSelection,
    RunParameters,
    Unavailable,
    WindowResult,
    candidate_key,
)
from market_risk.wavewarn_v20.selection import Outcome
from market_risk.wavewarn_v20.snapshot import Snapshot
from market_risk.wavewarn_v20.state_machine import Evidence, Risk, SystemDay, evidence_series, run_channels, run_system

ONE_LEVEL = "200 日均线一级版"
BUFFERED = "带缓冲带的 200 日均线二级版（101%/99%）"
NOT_EVALUABLE = "不可评价：未在 j₀ − 1 前收敛"
NOT_COMPUTABLE = "无法计算"
NO_SELECTION = "无选定候选，未计算"
CONSTANT_NOTE = "ē 为窗口结束后才知道的平均值，是机会成本参照，不是可实时执行的策略"
SUBSTITUTION_NOTE = "只在相同区间逐日替换暴露，不重新运行状态机、止损或冷却；不称保护收益，不作因果解释；不参与选择"
POLICY_DIFFERENCE_NOTE = "差异包含整套止损、冷却、重入路径的差异，不归因为任何单项规则"
CALCULATION_FAILED = "计算失败"


class ComparisonError(ValueError):
    """描述性对照、状态重建或对账的计算失败：入口按“计算失败”整体停止（设计稿第五节第 4 小节、第六节）。"""

    reason = CALCULATION_FAILED


class Band(Enum):
    """带缓冲带二级版的证据（Decimal 精确比较；边界相等归带内）。"""

    BELOW = "低于下界"
    INSIDE = "带内"
    ABOVE = "高于上界"


# ---------------------------------------------------------------------------
# 通用：净值摘要
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathNav:
    """一条恒定权重路径的逐区间收益与净值（首日为 1）。"""

    days: tuple[dt.date, ...]
    returns: tuple[float, ...]
    wealth: tuple[float, ...]
    log_sum: float
    log_wealth: float


def _drawdown(wealth: Sequence[float]) -> float:
    return max_drawdown(wealth)


def nav_summary(nav: NavResult | PathNav | Unavailable | None) -> dict:
    """净值、ln W_末、最大回撤（只作描述）。"""
    if nav is None:
        return {"computable": False, "log_wealth": None, "max_drawdown": None, "final_wealth": None}
    if isinstance(nav, Unavailable):
        return {"computable": False, "log_wealth": None, "max_drawdown": None, "final_wealth": None,
                "missing": [[asset, day.isoformat()] for asset, day in nav.missing]}
    wealth = nav.wealth
    return {"computable": True, "log_wealth": nav.log_wealth, "max_drawdown": _drawdown(wealth),
            "final_wealth": wealth[-1]}


def switch_summary(items: Sequence[Switch]) -> dict:
    """切换次数与调仓幅度（绝对值为主字段，方向另列；补充裁决 Q6）。"""
    return {"count": len(items), "magnitude_total": math.fsum(item.exposure_magnitude for item in items),
            "change_total": math.fsum(item.exposure_change for item in items)}


# ---------------------------------------------------------------------------
# 第五节第 1、2 小节：两个均线对照
# ---------------------------------------------------------------------------


def one_level_evidence(today: TrendDay, length: int) -> int | None:
    """L¹：均线完整且收盘价低于均线（length·C < Σ，与主参照“低于”同一比较）为 1；完整且不低于为 0；不完整无定义。"""
    if not today.complete or today.close is None or today.total is None:
        return None
    return 1 if below_average(today.close, today.total, length) else 0


def next_one_level(previous: Risk, level: int | None) -> Risk:
    """一级版状态表：均线不完整保持；L¹ = 1 → 一级；L¹ = 0 → 正常（一步恢复）。"""
    if level is None:
        return previous
    return Risk.LEVEL1 if level == 1 else Risk.NORMAL


def band_evidence(today: TrendDay, length: int) -> Band | None:
    """缓冲带证据：100·length·C 与 99·Σ、101·Σ 的 Decimal 精确比较；length = 200 时即 20000·C；边界相等归带内。"""
    if not today.complete or today.close is None or today.total is None:
        return None
    scaled = 100 * length * today.close
    if scaled < 99 * today.total:
        return Band.BELOW
    if scaled > 101 * today.total:
        return Band.ABOVE
    return Band.INSIDE


def next_buffered(previous: Risk, band: Band | None) -> Risk:
    """缓冲带二级版状态表：不完整或带内保持；低于下界 → 二级；高于上界时二级 → 一级、一级 → 正常、正常 → 正常。"""
    if band is None or band is Band.INSIDE:
        return previous
    if band is Band.BELOW:
        return Risk.LEVEL2
    return Risk.LEVEL1 if previous is Risk.LEVEL2 else Risk.NORMAL


@dataclass(frozen=True)
class AverageDay:
    day: dt.date
    evidence: str | None              # 一级版为 "1"/"0"，缓冲带为 Band 的值；不完整为 None
    risk: Risk


@dataclass(frozen=True)
class AverageComparison:
    name: str
    domain: tuple[Risk, ...]
    convergence_index: int | None     # 全部初始状态运行首次相同的轴行号；不收敛为 None
    convergence_day: dt.date | None
    evaluable: bool
    note: str | None
    days: tuple[AverageDay, ...]      # 以“正常”初始化的主路径，axis[t0+1 .. E]
    signals: tuple[SignalRecord, ...]
    targets: tuple[PlannedTarget, ...]
    switches: tuple[Switch, ...]
    signal_nav: NavResult | Unavailable | None
    policy: PolicyResult | PartialPolicyResult | None


def _run_average(initial: Risk, trend: Sequence[TrendDay], length: int, buffered: bool) -> tuple[AverageDay, ...]:
    state = initial
    result: list[AverageDay] = []
    for today in trend:
        if buffered:
            band = band_evidence(today, length)
            state = next_buffered(state, band)
            evidence = None if band is None else band.value
        else:
            level = one_level_evidence(today, length)
            state = next_one_level(state, level)
            evidence = None if level is None else str(level)
        result.append(AverageDay(today.day, evidence, state))
    return tuple(result)


def _level(day: AverageDay, buffered: bool) -> int | None:
    """写入信号记录的 L（只作记录）：一级版 1/0；缓冲带低于下界 2、高于上界 0，带内与不完整为 None。"""
    if day.evidence is None:
        return None
    if not buffered:
        return int(day.evidence)
    return {Band.BELOW.value: 2, Band.ABOVE.value: 0}.get(day.evidence)


def average_comparison(snapshot: Snapshot, result: WindowResult, parameters: RunParameters,
                       buffered: bool) -> AverageComparison:
    """两个均线对照（设计稿第五节共同约定与第 1、2 小节）：与 27 组、主参照同一 j₀、E、days；对照的收敛不参与 κ_全。"""
    window, common = result.window, result.common
    if window is None or common is None:
        raise ComparisonError("对照只对完整的窗口结果计算")
    length = parameters.windows.average
    trend = snapshot_trend_days(snapshot, "SPX", parameters.windows)
    axis, t0, j0, e_index = snapshot.days, window.t0, window.j0, window.e_index
    after = trend[t0 + 1:]
    domain = (Risk.NORMAL, Risk.LEVEL1, Risk.LEVEL2) if buffered else (Risk.NORMAL, Risk.LEVEL1)
    runs = {initial: _run_average(initial, after, length, buffered) for initial in domain}
    local = first_common_index([[item.risk for item in run] for run in runs.values()])
    convergence = None if local is None else t0 + 1 + local
    main = runs[Risk.NORMAL]
    name = BUFFERED if buffered else ONE_LEVEL
    if convergence is None or convergence > j0 - 1:
        return AverageComparison(name, domain, convergence, None if convergence is None else axis[convergence], False,
                                 NOT_EVALUABLE, main, (), (), (), None, None)
    days = tuple(axis[j0:])
    valid = tuple(trend[index].close is not None and trend[index].complete for index in range(len(axis)))
    window_days = main[j0 - 1 - (t0 + 1):e_index - (t0 + 1)]
    records = reference_signal_records([ReferenceDay(item.day, _level(item, buffered), item.risk)
                                        for item in window_days], valid[j0 - 1:e_index])
    if len(records) != len(days) or records[0].day != axis[j0 - 1] or any(
            records[index + 1].day != days[index] for index in range(len(days) - 1)):
        raise ComparisonError(f"{name} 的信号记录与执行日没有逐日对齐")
    targets = signal_targets(days, records, parameters.positions)
    prefix = common.prefix
    if prefix.first_missing is None:
        signal_nav: NavResult | Unavailable = simulate_targets(targets, prefix.values, parameters.positions,
                                                               parameters.tolerance)
    else:
        signal_nav = Unavailable(prefix.missing, MISSING_PRICE, None, SOURCE_PREFIX)
    closes = {asset: snapshot.series(asset)[j0:] for asset in ASSETS}
    policy = simulate_policy_with_gaps(days, records, closes, parameters.positions, parameters.policy,
                                       parameters.tolerance)
    return AverageComparison(name, domain, convergence, axis[convergence], True, None, main, records, targets,
                             switches(targets, parameters.positions), signal_nav, policy)


# ---------------------------------------------------------------------------
# 第五节第 3 小节：同平均暴露的恒定仓位（S-15）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConstantExposure:
    object: str
    computed: bool
    note: str | None
    core: float | None                # w̄_c
    leverage: float | None            # w̄_l
    exposure: float | None            # ē = w̄_c + λ·w̄_l
    nav: PathNav | Unavailable | None


def _constant_path(days: Sequence[dt.date], core: float, leverage: float, positions: PositionMap,
                   values: Sequence[float], tolerance: float) -> PathNav:
    """R_i = portfolio_return(w̄_c, w̄_l, λ, U_i)；w̄_l > 0 时由 portfolio_return 检查 1 + λU_i > 0。净值 j₀ 收盘为 1。"""
    returns = tuple(portfolio_return(core, leverage, positions.leverage, value) for value in values)
    wealth = [1.0]
    for value in returns:
        wealth.append(wealth[-1] * (1.0 + value))
    if any(not math.isfinite(item) or item <= 0 for item in wealth):
        raise ComparisonError("恒定仓位的净值出现非有限值或不为正")
    log_sum, log_wealth = math.fsum(math.log1p(value) for value in returns), math.log(wealth[-1])
    if not abs(log_sum - log_wealth) <= tolerance:
        raise ComparisonError(f"恒定仓位对账不符：Σ ln(1 + R) = {log_sum}，ln W_末 = {log_wealth}")
    return PathNav(tuple(days), returns, tuple(wealth), log_sum, log_wealth)


def constant_exposure(name: str, targets: Sequence[PlannedTarget], result: WindowResult,
                      parameters: RunParameters) -> ConstantExposure:
    """平均权重：在 n 个收益区间上，区间 i 持有原对象信号模拟的 targets[i−1]；只算恒定路径，不施加止损、冷却与重入。"""
    window, common = result.window, result.common
    if window is None or common is None:
        raise ComparisonError("对照只对完整的窗口结果计算")
    n = window.n
    held = tuple(targets[:n])
    if len(held) != n or n <= 0:
        raise ComparisonError(f"{name} 的计划目标个数与收益区间数不符")
    core = math.fsum(item.core for item in held) / n
    leverage = math.fsum(item.leverage for item in held) / n
    exposure = core + parameters.positions.leverage * leverage
    prefix = common.prefix
    if prefix.first_missing is not None:
        return ConstantExposure(name, False, NOT_COMPUTABLE, core, leverage, exposure,
                                Unavailable(prefix.missing, MISSING_PRICE, None, SOURCE_PREFIX))
    days = tuple(item.day for item in targets)
    return ConstantExposure(name, True, CONSTANT_NOTE, core, leverage, exposure,
                            _constant_path(days, core, leverage, parameters.positions, prefix.values,
                                           parameters.tolerance))


def constant_exposures(result: WindowResult, selection: DevelopmentSelection,
                       parameters: RunParameters) -> tuple[ConstantExposure, ...]:
    """原对象：主参照（总是计算）与选定候选（只在出口为“选定”时计算；否则记“无选定候选，未计算”）。"""
    if result.reference is None:
        raise ComparisonError("主参照结果缺失")
    items = [constant_exposure(REFERENCE, result.reference.outcome.targets, result, parameters)]
    chosen = selection.selection.selected
    if selection.selection.outcome is Outcome.SELECTED and chosen is not None:
        items.append(constant_exposure(candidate_key(chosen), result.candidates[chosen].outcome.targets, result,
                                       parameters))
    else:
        items.append(ConstantExposure("选定候选", False, NO_SELECTION, None, None, None, None))
    return tuple(items)


# ---------------------------------------------------------------------------
# 状态重建（设计稿第六节第 3 行；第五节第 5 小节的通道状态来源）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RebuiltStates:
    """按同参同切片重建的逐日证据（自 t0+1 起）与各候选系统记录（axis[j₀−2 .. E]）。"""

    evidence: Mapping[tuple[int, Decimal], tuple[Evidence, ...]]
    systems: Mapping[object, tuple[SystemDay, ...]]
    reference: tuple[ReferenceDay, ...]
    first: int                                        # 证据第 0 项的轴行号（t0 + 1）


def rebuild_states(snapshot: Snapshot, result: WindowResult, parameters: RunParameters) -> RebuiltStates:
    """用 state_machine.run_channels / evidence_series / run_system 与 reference.run_reference 按同参同切片重建，
    与组合层逐日比较；任一日不相等即计算失败（精确相等，不用容差）。"""
    window, reference = result.window, result.reference
    if window is None or reference is None:
        raise ComparisonError("状态重建只对完整的窗口结果计算")
    windows, average = parameters.windows, parameters.windows.average
    spx = snapshot_asset_days(snapshot, "SPX", windows)
    qqq = snapshot_asset_days(snapshot, "QQQ", windows)
    trend = snapshot_trend_days(snapshot, "SPX", windows)
    first = window.t0 + 1
    j0, e_index = window.j0, window.e_index
    initial = result.spec.initial
    evidence: dict[tuple[int, Decimal], tuple[Evidence, ...]] = {}
    systems: dict[object, tuple[SystemDay, ...]] = {}
    for candidate, outcome in result.candidates.items():
        pair = (candidate.k, candidate.theta)
        if pair not in evidence:
            paths = run_channels(initial.channels, spx[first:], qqq[first:], trend[first:], candidate.k,
                                 candidate.theta, average)
            evidence[pair] = evidence_series(spx[first:], qqq[first:], trend[first:], paths)
        whole = run_system(initial.system, evidence[pair], candidate.k, candidate.h)
        rebuilt = whole[j0 - 2 - first:e_index - first + 1]
        if tuple(rebuilt) != tuple(outcome.system):
            raise ComparisonError(f"{candidate_key(candidate)} 的系统记录无法按同参同切片重建")
        systems[candidate] = tuple(rebuilt)
    rebuilt_reference = run_reference(initial.reference, trend[first:], average)[j0 - 2 - first:e_index - first + 1]
    if tuple(rebuilt_reference) != tuple(reference.days):
        raise ComparisonError("主参照的逐日状态无法按同参同切片重建")
    valid = tuple(trend[index].close is not None and trend[index].complete for index in range(j0 - 2, e_index + 1))
    if valid != tuple(reference.all_valid):
        raise ComparisonError("主参照的“当日输入完整”无法重建")
    return RebuiltStates(evidence, systems, tuple(rebuilt_reference), first)


# ---------------------------------------------------------------------------
# 第五节第 5 小节：固定区间暴露替换算术对照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Substitution:
    candidate: str
    computed: bool
    note: str
    intervals: tuple[tuple[dt.date, dt.date], ...]     # （区间起点执行日，区间终点执行日）
    registered_log: float | None                         # Σ ln(1 + R_i)，登记仓位（一级）
    substituted_log: float | None                        # Σ ln(1 + R′_i)，替换为二级
    difference: float | None                             # 替换 − 登记


def in_bear(day: dt.date, bears: Sequence[tuple[str, dt.date, dt.date]]) -> bool:
    """熊市条件 P ≤ d < Tr。"""
    return any(peak <= day < trough for _, peak, trough in bears)


def substitutions(result: WindowResult, rebuilt: RebuiltStates, parameters: RunParameters,
                  bears: Sequence[tuple[str, dt.date, dt.date]]) -> tuple[Substitution, ...]:
    """筛选：信号日 S = 一级；当日 MR 激活；PR_SPX、PR_QQQ 都不激活（通道取自重建证据，系统记录已逐日核对）；
    区间起点日满足熊市条件。U_i 取 BasketPrefix.values；R_i 用一级权重、R′_i 用二级权重。"""
    common = result.common
    if common is None or result.window is None:
        raise ComparisonError("对照只对完整的窗口结果计算")
    positions = parameters.positions
    level1, level2 = positions.weights(Position.LEVEL1), positions.weights(Position.LEVEL2)
    items = []
    for candidate, outcome in result.candidates.items():
        key = candidate_key(candidate)
        if isinstance(outcome.outcome.signal_nav, Unavailable):
            items.append(Substitution(key, False, NOT_COMPUTABLE, (), None, None, None))
            continue
        by_day = {item.day: item for item in rebuilt.evidence[(candidate.k, candidate.theta)]}
        days = tuple(target.day for target in outcome.outcome.targets)
        signals = outcome.outcome.signals
        chosen: list[int] = []
        for interval in range(1, len(days)):
            signal = signals[interval - 1]
            proof = by_day.get(signal.day)
            if proof is None:
                raise ComparisonError(f"{key} 的信号日 {signal.day} 没有重建证据")
            if (signal.risk is Risk.LEVEL1 and proof.mr.active and not proof.pr_spx.active
                    and not proof.pr_qqq.active and in_bear(days[interval - 1], bears)):
                chosen.append(interval)
        values = common.prefix.values
        registered = [portfolio_return(level1.core, level1.leverage, positions.leverage, values[index - 1])
                      for index in chosen]
        replaced = [portfolio_return(level2.core, level2.leverage, positions.leverage, values[index - 1])
                    for index in chosen]
        first, second = math.fsum(math.log1p(value) for value in registered), math.fsum(
            math.log1p(value) for value in replaced)
        if not (math.isfinite(first) and math.isfinite(second)):
            raise ComparisonError(f"{key} 的暴露替换出现非有限值")
        intervals = tuple((days[index - 1], days[index]) for index in chosen)
        items.append(Substitution(key, True, SUBSTITUTION_NOTE, intervals, first, second, second - first))
    return tuple(items)


# ---------------------------------------------------------------------------
# 设计稿第一节第 2 小节：市场环境（收益区间起点归属）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentDay:
    interval: int                      # 区间 i（1…n）
    start: dt.date                     # 区间起点 d
    category: str                      # 熊市、上涨年、下跌年、平淡年、完整年度分类不可得
    year_return: Decimal | None        # r_y（Decimal；熊市或不可得时为 None）


BEAR, UP, DOWN, FLAT, UNKNOWN_YEAR = "熊市", "上涨年", "下跌年", "平淡年", "完整年度分类不可得"
ENVIRONMENTS = (BEAR, UP, DOWN, FLAT, UNKNOWN_YEAR)


def year_return(snapshot: Snapshot, year: int, year_ends: Mapping[int, dt.date], asset: str) -> Decimal | None:
    """r_y =（y 年最后一个收盘价 ÷ y−1 年最后一个收盘价）− 1，收盘价取自快照（截止日以内）。
    y 年的最后一个交易日晚于截止日、所需交易日不在快照轴上或收盘价缺失时为 None（完整年度分类不可得）。
    year_ends 为各年 NYSE 最后一个交易日（由入口按交易日历给出）。"""
    if year not in year_ends or year - 1 not in year_ends:
        return None
    end, before = year_ends[year], year_ends[year - 1]
    on_axis = set(snapshot.days)
    if end > snapshot.day or end not in on_axis or before not in on_axis:
        return None
    current, previous = snapshot.closes[asset].get(end), snapshot.closes[asset].get(before)
    if current is None or previous is None:
        return None
    return current / previous - 1


def environments(snapshot: Snapshot, result: WindowResult, bears: Sequence[tuple[str, dt.date, dt.date]],
                 year_ends: Mapping[int, dt.date], up: Decimal, down: Decimal,
                 asset: str) -> tuple[EnvironmentDay, ...]:
    """熊市优先（P ≤ d < Tr）；其余按 d 所在日历年 y 的 r_y：r_y ≥ up 上涨年，r_y ≤ down 下跌年，其余平淡年。"""
    window = result.window
    if window is None:
        raise ComparisonError("市场环境只对完整的窗口结果计算")
    days = snapshot.days[window.j0:]
    rows = []
    cache: dict[int, Decimal | None] = {}
    for interval in range(1, len(days)):
        start = days[interval - 1]
        if in_bear(start, bears):
            rows.append(EnvironmentDay(interval, start, BEAR, None))
            continue
        if start.year not in cache:
            cache[start.year] = year_return(snapshot, start.year, year_ends, asset)
        value = cache[start.year]
        if value is None:
            category = UNKNOWN_YEAR
        elif value >= up:
            category = UP
        elif value <= down:
            category = DOWN
        else:
            category = FLAT
        rows.append(EnvironmentDay(interval, start, category, value))
    return tuple(rows)


def environment_sums(rows: Sequence[EnvironmentDay], objects: Mapping[str, NavResult | Unavailable | None]) -> dict:
    """各环境的区间数与各对象的对数收益之和（math.fsum(log1p(R_i))）；对象净值不可计算时为 None。"""
    summary: dict[str, dict] = {}
    for category in ENVIRONMENTS:
        chosen = [row.interval for row in rows if row.category == category]
        sums: dict[str, float | None] = {}
        for name, nav in objects.items():
            if not isinstance(nav, NavResult):
                sums[name] = None
            else:
                sums[name] = math.fsum(math.log1p(nav.returns[index - 1]) for index in chosen)
        summary[category] = {"intervals": len(chosen), "log_return_sums": sums}
    return summary


# ---------------------------------------------------------------------------
# 第六节：对账
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationRow:
    kind: str                          # 单一路径 / 相对主参照
    object: str
    path: str
    difference: float
    tolerance: float
    passed: bool


def _single(name: str, path: str, returns: Sequence[float], log_wealth: float, tolerance: float) -> ReconciliationRow:
    difference = abs(math.fsum(math.log1p(value) for value in returns) - log_wealth)
    return ReconciliationRow("单一路径", name, path, difference, tolerance, difference <= tolerance)


def relative_row(name: str, mine: NavResult, reference: NavResult, tolerance: float) -> ReconciliationRow:
    """相对主参照（信号模拟）：先逐日断言执行日序列相同，再
    abs(fsum(log1p(rc) − log1p(rr)) − (ln W_c − ln W_ref)) <= 容差。"""
    if tuple(item.day for item in mine.executions) != tuple(item.day for item in reference.executions):
        raise ComparisonError(f"{name} 与主参照的执行日序列不同")
    difference = abs(math.fsum(math.log1p(rc) - math.log1p(rr) for rc, rr in
                               zip(mine.returns, reference.returns, strict=True))
                     - (mine.log_wealth - reference.log_wealth))
    return ReconciliationRow("相对主参照", name, "信号模拟", difference, tolerance, difference <= tolerance)


def reconcile_paths(result: WindowResult, comparisons: Sequence[AverageComparison],
                    constants: Sequence[ConstantExposure], tolerance: float) -> tuple[ReconciliationRow, ...]:
    """单一路径：每条可计算的净值路径 abs(fsum(log1p(r)) − ln W_末) <= 容差。
    相对主参照：逐日断言执行日序列相同后，只对信号模拟、双方均可计算时
    abs(fsum(log1p(rc) − log1p(rr)) − (ln W_c − ln W_ref)) <= 容差。任一不符即计算失败。"""
    if result.reference is None or result.common is None:
        raise ComparisonError("对账只对完整的窗口结果计算")
    rows: list[ReconciliationRow] = []

    def object_paths(name: str, signal_nav: object, policy: object) -> None:
        if isinstance(signal_nav, NavResult):
            rows.append(_single(name, "信号模拟", signal_nav.returns, signal_nav.log_wealth, tolerance))
        if isinstance(policy, PolicyResult):
            rows.append(_single(name, "执行政策研究模拟", policy.nav.returns, policy.nav.log_wealth, tolerance))

    for candidate, outcome in result.candidates.items():
        object_paths(candidate_key(candidate), outcome.outcome.signal_nav, outcome.outcome.policy)
    object_paths(REFERENCE, result.reference.outcome.signal_nav, result.reference.outcome.policy)
    hold = result.common.hold
    if isinstance(hold, NavResult):
        rows.append(_single(HOLD, "一直持有", hold.returns, hold.log_wealth, tolerance))
    for comparison in comparisons:
        if comparison.evaluable:
            object_paths(comparison.name, comparison.signal_nav, comparison.policy)
    for constant in constants:
        if isinstance(constant.nav, PathNav):
            rows.append(_single(constant.object, "恒定仓位", constant.nav.returns, constant.nav.log_wealth, tolerance))
    reference = result.reference.outcome.signal_nav
    if isinstance(reference, NavResult):
        for candidate, outcome in result.candidates.items():
            mine = outcome.outcome.signal_nav
            if isinstance(mine, NavResult):
                rows.append(relative_row(candidate_key(candidate), mine, reference, tolerance))
    failed = [row for row in rows if not row.passed]
    if failed:
        raise ComparisonError("对账不符：" + "；".join(f"{row.object} {row.path} 差 {row.difference!r}"
                                                    for row in failed))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 第十一节：报告统计
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateRun:
    position: str
    start: dt.date                     # 首个执行日（区间起点）
    end: dt.date                       # 末个执行日对应区间的终点
    intervals: int
    left_truncated: bool
    right_truncated: bool


@dataclass(frozen=True)
class StateStatistics:
    """第一节第 4 小节的四项（依补充二第二节）：信号模拟的状态序列；转换按信号日 axis[j₀−1 .. E−1]，执行区间按 1…n。"""

    object: str
    direct_level2_days: tuple[dt.date, ...]           # S_{d−1} = 正常 且 S_d = 二级
    via_level1_days: tuple[dt.date, ...]              # S_{d−1} = 一级 且 S_d = 二级（每个开始一个二级段）
    level1_runs: tuple[StateRun, ...]
    level2_runs: tuple[StateRun, ...]
    start_note: str | None                            # 起点已处于该级（无前一日状态）时的说明


def state_statistics(name: str, risks: Sequence[tuple[dt.date, Risk]], targets: Sequence[PlannedTarget],
                     n: int) -> StateStatistics:
    """risks 为 axis[j₀−2 .. E] 的（日期，状态），首项即报告窗口首个信号日的前一日；
    targets[i−1] 为区间 i 的持有目标。"""
    if len(risks) < 2 or len(targets) < n:
        raise ComparisonError(f"{name} 的状态序列或计划目标不足以计算报告统计")
    signal_span = risks[1:len(risks) - 1]              # axis[j₀−1 .. E−1]
    direct, via = [], []
    previous = risks[0][1]
    for day, risk in signal_span:
        if previous is Risk.NORMAL and risk is Risk.LEVEL2:
            direct.append(day)
        elif previous is Risk.LEVEL1 and risk is Risk.LEVEL2:
            via.append(day)
        previous = risk

    def runs(position: Position) -> tuple[StateRun, ...]:
        result: list[StateRun] = []
        start: int | None = None
        for index in range(n + 1):
            holding = index < n and targets[index].position is position
            if holding and start is None:
                start = index
            elif not holding and start is not None:
                result.append(StateRun(position.value, targets[start].day, targets[index].day, index - start,
                                       start == 0, index == n))
                start = None
        return tuple(result)

    return StateStatistics(name, tuple(direct), tuple(via), runs(Position.LEVEL1), runs(Position.LEVEL2), None)


@dataclass(frozen=True)
class PolicyEvent:
    kind: str                          # 止损执行、离场期、重入、U 设置、U 解除、无法确定
    day: dt.date
    detail: str


def policy_events(policy: PolicyResult | PartialPolicyResult | None) -> tuple[PolicyEvent, ...]:
    """止损确认与执行日、冷却期（离场期）、重入日、U 设置与解除、无法确定路径的起点与原因（第一节第 6 小节）。"""
    if policy is None:
        return ()
    targets = policy.targets
    events: list[PolicyEvent] = []
    for index, target in enumerate(targets):
        before = targets[index - 1] if index > 0 else None
        if target.source is TargetSource.STOP_CASH:
            events.append(PolicyEvent("止损执行", target.day, f"止损确认日 {before.day if before else '—'}"))
        elif target.source is TargetSource.OUT_CASH and (before is None or before.source is not TargetSource.OUT_CASH):
            events.append(PolicyEvent("离场期", target.day, "离场期（冷却与等待重入）开始"))
        if before is not None and before.position is Position.CASH and target.position is not Position.CASH:
            events.append(PolicyEvent("重入", target.day, f"重入后目标 {target.position.value}"))
        if target.cap_active and (before is None or not before.cap_active):
            events.append(PolicyEvent("U 设置", target.day, "U = 一级"))
        if before is not None and before.cap_active and not target.cap_active:
            events.append(PolicyEvent("U 解除", target.day, "U = 无"))
    if isinstance(policy, PartialPolicyResult) and policy.undetermined is not None:
        events.append(PolicyEvent("无法确定", policy.undetermined.day, policy.undetermined.reason))
    return tuple(events)


def policy_minus_signal(signal_nav: object, policy: object) -> dict:
    """执行政策与信号模拟的 ln W 之差（只作描述，不作单项归因）。"""
    if isinstance(signal_nav, NavResult) and isinstance(policy, PolicyResult):
        return {"difference": policy.nav.log_wealth - signal_nav.log_wealth, "note": POLICY_DIFFERENCE_NOTE}
    return {"difference": None, "note": POLICY_DIFFERENCE_NOTE}


# ---------------------------------------------------------------------------
# 汇总入口（由 development_run 在第 9、10 步分别调用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Descriptive:
    """第 9 步：描述性对照（含重建的状态，供暴露替换与第 10 步对账使用）。"""

    averages: tuple[AverageComparison, ...]
    constants: tuple[ConstantExposure, ...]
    rebuilt: RebuiltStates
    substitutions: tuple[Substitution, ...]
    environments: tuple[EnvironmentDay, ...]
    environment_summary: dict
    statistics: tuple[StateStatistics, ...]
    policy_events: Mapping[str, tuple[PolicyEvent, ...]]
    policy_differences: Mapping[str, dict]


def descriptive(snapshot: Snapshot, result: WindowResult, selection: DevelopmentSelection, parameters: RunParameters,
                bears: Sequence[tuple[str, dt.date, dt.date]], year_ends: Mapping[int, dt.date], up: Decimal,
                down: Decimal, asset: str, note: Callable[[str], None] | None = None) -> Descriptive:
    """第 9 步全部描述性对照与报告统计；note 不为 None 时每项算出后记录一次（信息状态的阶段记录）。"""

    def mark(field: str) -> None:
        if note is not None:
            note(field)

    if result.window is None or result.common is None or result.reference is None:
        raise ComparisonError("对照只对完整的窗口结果计算")
    averages = (average_comparison(snapshot, result, parameters, False),
                average_comparison(snapshot, result, parameters, True))
    mark("均线对照")
    constants = constant_exposures(result, selection, parameters)
    mark("恒定仓位")
    rebuilt = rebuild_states(snapshot, result, parameters)
    mark("状态重建")
    replaced = substitutions(result, rebuilt, parameters, bears)
    mark("暴露替换")
    rows = environments(snapshot, result, bears, year_ends, up, down, asset)
    objects: dict[str, NavResult | Unavailable | None] = {
        candidate_key(candidate): outcome.outcome.signal_nav for candidate, outcome in result.candidates.items()}
    objects[REFERENCE] = result.reference.outcome.signal_nav
    objects[HOLD] = result.common.hold
    summary = environment_sums(rows, objects)
    mark("市场环境")
    n = result.window.n
    statistics = [state_statistics(candidate_key(candidate), [(item.day, item.risk) for item in outcome.system],
                                   outcome.outcome.targets, n) for candidate, outcome in result.candidates.items()]
    statistics.append(state_statistics(REFERENCE, [(item.day, item.risk) for item in result.reference.days],
                                       result.reference.outcome.targets, n))
    events = {candidate_key(candidate): policy_events(outcome.outcome.policy)
              for candidate, outcome in result.candidates.items()}
    events[REFERENCE] = policy_events(result.reference.outcome.policy)
    for comparison in averages:
        events[comparison.name] = policy_events(comparison.policy)
    differences = {candidate_key(candidate): policy_minus_signal(outcome.outcome.signal_nav, outcome.outcome.policy)
                   for candidate, outcome in result.candidates.items()}
    differences[REFERENCE] = policy_minus_signal(result.reference.outcome.signal_nav, result.reference.outcome.policy)
    for comparison in averages:
        differences[comparison.name] = policy_minus_signal(comparison.signal_nav, comparison.policy)
    mark("报告统计")
    return Descriptive(averages, constants, rebuilt, replaced, rows, summary, tuple(statistics), events, differences)
