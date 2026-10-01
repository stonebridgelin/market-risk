"""v1.4 第二轮开发期诊断的编排（纯计算，描述性，不参与任何判定）：补充登记 D。

对象：选定的 v1.4 设定、同参数的“v1.4 去掉 MR”、200 日均线；完整净值另加满仓、现金与两条同平均暴露基准。
评价窗口为 j₀ 至最后一个 next_date ≤ 开发期末的区间；输入必须恰好截至开发期末，不使用其后的任何价格。
不改变模型规则、参数、选定设定与 γ：各对象的状态序列与损失直接取自开发期评价的同一条计算路径。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.config_v14 import Round2Config, ValidationConfig
from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    development_labels,
    evaluate_candidate,
)
from market_risk.wavewarn.evaluation_tables import n_exit_costs
from market_risk.wavewarn.execution import exposure
from market_risk.wavewarn.green_report import GreenEvent, GreenSummary, green_events, green_summary
from market_risk.wavewarn.labels_zz import ZZEvent
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.nav import NavMetrics, exposed_returns, nav_metrics, portfolio_returns, simple_returns
from market_risk.wavewarn.switch_diagnostics import (
    Segment,
    Switch,
    TimingSplit,
    exposure_change,
    holding_segments,
    window_switches,
)
from market_risk.wavewarn.timing import TimingResult, ma200_states, reference_rows, timing_result
from market_risk.wavewarn.v14_model import FULL, NO_TREND

LIGHTS = ("绿", "黄", "红")
SELECTED, NO_MR, MA200 = "选定的 v1.4", "v1.4 去掉 MR（同参数）", "200日均线"
FULLY_INVESTED, CASH = "满仓", "现金"
PORTFOLIO = "双资产"


@dataclass(frozen=True)
class ObjectDiagnostics:
    """一个诊断对象在评价窗口内的切换与转绿诊断。"""

    name: str
    evaluated: CandidateEvaluation
    timing: TimingResult
    split: TimingSplit
    initial_light: str                      # j₀ 当日收盘执行的灯色（继承此前的信号）
    initial_exposure: Decimal
    switches: tuple[Switch, ...]
    segments: tuple[Segment, ...]
    green: Mapping[str, tuple[GreenEvent, ...]]
    green_summaries: Mapping[str, GreenSummary]


@dataclass(frozen=True)
class NavRow:
    """一个对象在一个口径（双资产、SPX 或 QQQ）下的净值指标。"""

    name: str
    scope: str
    returns: tuple[Decimal, ...]
    metrics: NavMetrics
    switches: int
    exposure_change: Decimal


@dataclass(frozen=True)
class Round2Result:
    prepared: PreparedEvaluation
    days: tuple[dt.date, ...]               # 评价窗口的全部交易日（N+1 个）
    objects: tuple[ObjectDiagnostics, ...]
    nav: tuple[NavRow, ...]


def setting_states(prepared: PreparedEvaluation, model: str, config: ValidationConfig) -> CandidateStates:
    """与锁定设定同 K、同 θ_P 的那一组；不做任何选择或比较。"""
    found = [states for states in prepared.states
             if states.candidate.model == model and states.candidate.k == config.locked_k
             and states.candidate.theta_p == config.locked_theta]
    if len(found) != 1:
        raise ValueError(f"找不到唯一的 {model} 设定")
    return found[0]


def exposure_levels(eta: Decimal) -> dict[str, Decimal]:
    return {light: exposure(light, eta) for light in LIGHTS}  # type: ignore[arg-type]


def executed_from_t0(prepared: PreparedEvaluation, states: CandidateStates) -> tuple[str, ...]:
    """自 t0 起逐日的系统执行灯色：第 j 天收盘执行前一日信号；t0 当日为初始的绿灯。"""
    days = prepared.inputs.days
    if tuple(row.date for row in states.rows) != days[days.index(prepared.t0):]:
        raise ValueError("状态序列与 t0 后交易日轴不一致")
    return ("绿", *(row.light for row in states.rows[:-1]))


def window_offset(prepared: PreparedEvaluation) -> int:
    """j₀ 在 t0 起的日期轴上的行号。"""
    days = prepared.inputs.days
    return days.index(prepared.first_loss_day) - days.index(prepared.t0)


def executed_lights(prepared: PreparedEvaluation, states: CandidateStates) -> tuple[str, ...]:
    """评价窗口（j₀ 至末日）逐日的系统执行灯色。

    与 evaluate_candidate 的 system_executed 是同一条规则（有测试核对），不需要标签与损失。
    """
    return executed_from_t0(prepared, states)[window_offset(prepared):]


def previous_light(prepared: PreparedEvaluation, states: CandidateStates) -> str | None:
    """j₀ 前一个交易日的系统执行灯色；状态机自 t0 起连续运行，j₀ 晚于 t0 时它总是存在。

    j₀ 就是 t0（此前没有执行状态）时为空：那才是从无仓位开始的初始建仓。
    """
    offset = window_offset(prepared)
    return executed_from_t0(prepared, states)[offset - 1] if offset > 0 else None


def object_green(prepared: PreparedEvaluation, states: CandidateStates,
                 events: Mapping[str, Sequence[ZZEvent]], window: int
                 ) -> tuple[dict[str, tuple[GreenEvent, ...]], dict[str, GreenSummary]]:
    """两资产分别的转绿双层报告；纳入门槛同开发期评价（高点 P ≥ τ 的已确认事件）。"""
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.t0):]
    rows: dict[str, tuple[GreenEvent, ...]] = {}
    for symbol in SYMBOLS:
        own = events[symbol]
        following = [own[index + 1].t0_date if index + 1 < len(own) else None for index in range(len(own))]
        rows[symbol] = green_events(axis, n_exit_costs(prepared, states, symbol, own, prepared.tau), following,
                                    window)
    return rows, {symbol: green_summary(symbol, rows[symbol]) for symbol in SYMBOLS}


def object_diagnostics(name: str, prepared: PreparedEvaluation, states: CandidateStates,
                       evaluated: CandidateEvaluation, timing: TimingResult,
                       events: Mapping[str, Sequence[ZZEvent]], config: Round2Config) -> ObjectDiagnostics:
    """切换按主损失的计费口径（j₀ 当日相对前一日的切换计入）；与计费的切换次数不符即报错。"""
    eta = configured_loss_settings(prepared.config).parameters.eta
    lights = evaluated.system_executed
    if lights != executed_lights(prepared, states):
        raise ValueError(f"{name} 的执行灯色与状态序列不一致")
    switches = window_switches(evaluated.days, lights, exposure_levels(eta), previous_light(prepared, states))
    marks = evaluated.executions["SPX"]
    if len(switches) != evaluated.billed_switches:
        raise ValueError(f"{name} 的窗口内切换次数与登记的计费切换次数不一致")
    cost = sum((row.switch_cost for row in evaluated.daily_losses), Decimal(0))
    green, summaries = object_green(prepared, states, events, config.green_window)
    return ObjectDiagnostics(name, evaluated, timing, TimingSplit(timing.score, cost, len(switches)), lights[0],
                             exposure(lights[0], eta),  # type: ignore[arg-type]
                             switches, holding_segments(evaluated.days, lights, marks[0].switched,
                                                        marks[-1].switched), green, summaries)


def nav_rows(name: str, days: Sequence[dt.date], exposures: Sequence[Decimal],
             returns: Mapping[str, Sequence[Decimal]], weights: Mapping[str, Decimal], switches: int,
             change: Decimal, config: Round2Config) -> tuple[NavRow, ...]:
    """双资产与两个单资产各一行；切换次数与目标暴露变化量是对象本身的，三行相同。"""
    series = {PORTFOLIO: portfolio_returns(exposures, returns, weights),
              **{symbol: exposed_returns(exposures, returns[symbol]) for symbol in SYMBOLS}}
    return tuple(NavRow(name, scope, values,
                        nav_metrics(days, values, config.rolling_windows, config.trading_days_per_year),
                        switches, change) for scope, values in series.items())


def all_nav_rows(prepared: PreparedEvaluation, days: Sequence[dt.date], objects: Sequence[ObjectDiagnostics],
                 config: Round2Config) -> tuple[NavRow, ...]:
    """三个诊断对象、满仓、现金，以及取 v1.4 与 200 日均线的 ē 的两条恒定暴露基准。"""
    settings = configured_loss_settings(prepared.config)
    returns = {symbol: simple_returns(days, prepared.inputs.series[symbol]) for symbol in SYMBOLS}
    levels = exposure_levels(settings.parameters.eta)
    count = len(days) - 1
    rows: list[NavRow] = []
    for item in objects:
        exposures = [levels[light] for light in item.evaluated.system_executed[:-1]]
        rows.extend(nav_rows(item.name, days, exposures, returns, settings.weights, len(item.switches),
                             exposure_change(item.switches), config))
    constants = [(FULLY_INVESTED, Decimal(1)), (CASH, Decimal(0)),
                 *((f"恒定暴露 ē（取{item.name}的 ē）", item.timing.mean_exposure)
                   for item in objects if item.name in (SELECTED, MA200))]
    for name, level in constants:
        rows.extend(nav_rows(name, days, [level] * count, returns, settings.weights, 0, Decimal(0), config))
    return tuple(rows)


def round2_diagnostics(prepared: PreparedEvaluation, validation: ValidationConfig,
                       config: Round2Config) -> Round2Result:
    """标签 → 参照行 → 三个对象的切换与转绿诊断 → 完整净值。输入须恰好截至开发期末。"""
    end = prepared.config.development_end()
    if prepared.inputs.days[-1] != end:
        raise ValueError("第二轮诊断只能使用截至开发期末的输入")
    events, unknown = development_labels(prepared)
    references = reference_rows(prepared, events, unknown, validation.model.mr_window)
    eta = configured_loss_settings(prepared.config).parameters.eta
    green, red, average = references[0].evaluated.total_loss, references[2].evaluated.total_loss, references[3]
    objects = []
    for name, model in ((SELECTED, FULL), (NO_MR, NO_TREND)):
        states = setting_states(prepared, model, validation)
        evaluated = evaluate_candidate(prepared, states, events, unknown)
        objects.append(object_diagnostics(name, prepared, states, evaluated,
                                          timing_result(evaluated, eta, green, red), events, config))
    objects.append(object_diagnostics(MA200, prepared, ma200_states(prepared, validation.model.mr_window),
                                      average.evaluated, average.timing, events, config))
    days = objects[0].evaluated.days
    if days[0] != prepared.first_loss_day or days[-1] != end or any(item.evaluated.days != days for item in objects):
        raise ValueError("诊断对象的评价窗口不一致，或最后一个区间越过开发期末")
    return Round2Result(prepared, days, tuple(objects), all_nav_rows(prepared, days, objects, config))
