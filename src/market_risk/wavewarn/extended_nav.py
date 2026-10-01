"""补充历史的真实净值（纯计算，描述性，不参与任何判定）。

窗口与 v1.4 补充历史相同（t0′、τ′、j₀′ 的规则不变，窗口末日 2009-09-30）。只需要 SPX 与 QQQ 的收盘价：
各对象的执行灯色直接由状态序列按“次日收盘执行”得到，不读取任何标签，也不计算损失。
净值口径与第二轮诊断相同（nav.py）：每日收盘按总暴露 e、两资产按权重再平衡，现金收益为 0，不含分红与费用。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.buffered_ma import BUFFERED_MA200, buffered_ma_states
from market_risk.wavewarn.config_v14 import Round2Config, ValidationConfig
from market_risk.wavewarn.diagnostics_round2 import (
    MA200,
    PORTFOLIO,
    NavRow,
    executed_lights,
    exposure_levels,
    nav_rows,
    previous_light,
)
from market_risk.wavewarn.evaluation import SYMBOLS, CandidateStates, PreparedEvaluation
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.nav import Drawdown, max_drawdown, running_drawdowns, simple_returns, wealth_path
from market_risk.wavewarn.switch_diagnostics import (
    Switch,
    exposure_change,
    first_day_switches,
    reversal_count,
    window_switches,
)
from market_risk.wavewarn.timing import ma200_states

SELECTED_PRICE = "v1.4 纯价格版（选定设定）"
HOLD = "一直持有"
SCOPES = (PORTFOLIO, *SYMBOLS)


@dataclass(frozen=True)
class SignalObject:
    """一个由信号驱动的对象在窗口内的执行灯色、切换与平均执行暴露。"""

    name: str
    lights: tuple[str, ...]            # N+1 个
    exposures: tuple[Decimal, ...]     # N 个区间的执行暴露
    switches: tuple[Switch, ...]       # 含 j₀ 当日相对前一日的切换（与主损失的计费口径一致）
    mean_exposure: Decimal

    @property
    def switch_at_start(self) -> bool:
        """j₀ 当日恰有一次切换：旧口径（不计 j₀ 当日）下的切换次数比现在少这一次。"""
        return bool(first_day_switches(self.switches))


def signal_object(name: str, prepared: PreparedEvaluation, states: CandidateStates,
                  levels: Mapping[str, Decimal]) -> SignalObject:
    lights = executed_lights(prepared, states)
    days = prepared.inputs.days[prepared.inputs.days.index(prepared.first_loss_day):]
    exposures = tuple(levels[light] for light in lights[:-1])
    switches = window_switches(days, lights, levels, previous_light(prepared, states))
    return SignalObject(name, lights, exposures, switches, sum(exposures, Decimal(0)) / len(exposures))


@dataclass(frozen=True)
class PeriodRow:
    """一个对象、一个口径在某一期间（区间起点在 [start, end) 内）的收益与最大回撤。"""

    name: str
    scope: str
    start: dt.date
    end: dt.date
    intervals: int
    period_return: Decimal
    drawdown: Drawdown                 # 净值在期间起点重新归一化后的区间内最大回撤（恢复也只在期间内找）
    full_curve_depth: Decimal          # 全程净值曲线在该期间内相对此前最高点的最深回撤


@dataclass(frozen=True)
class ExtendedNavResult:
    prepared: PreparedEvaluation
    days: tuple[dt.date, ...]
    objects: tuple[SignalObject, ...]          # 选定设定、200 日均线、带缓冲带的 200 日均线
    grid: tuple[SignalObject, ...]             # 纯价格版九组（登记顺序）
    nav: tuple[NavRow, ...]                    # 全部对象（含一直持有与两条同平均暴露基准）× 三个口径
    grid_nav: tuple[NavRow, ...]               # 九组 × 三个口径
    periods: tuple[PeriodRow, ...]             # 两次熊市


def grid_name(states: CandidateStates) -> str:
    return f"v1.4 纯价格版 K={states.candidate.k} θ_P={states.candidate.theta_p}"


def period_rows(days: Sequence[dt.date], rows: Sequence[NavRow],
                periods: Sequence[tuple[dt.date, dt.date]]) -> tuple[PeriodRow, ...]:
    """期间取区间起点在 [start, end) 内的区间（最后一个区间止于 end 当日收盘），净值在期间起点重新记为 1。"""
    result = []
    for start, end in periods:
        indices = [index for index, day in enumerate(days[:-1]) if start <= day < end]
        if not indices or indices != list(range(indices[0], indices[-1] + 1)):
            raise ValueError("熊市期间不在评价窗口内")
        span = days[indices[0]:indices[-1] + 2]
        for row in rows:
            wealth = wealth_path(row.returns[indices[0]:indices[-1] + 1])
            running = running_drawdowns(wealth_path(row.returns))
            result.append(PeriodRow(row.name, row.scope, start, end, len(indices), wealth[-1] - 1,
                                    max_drawdown(span, wealth), max(running[indices[0]:indices[-1] + 2])))
    return tuple(result)


def object_nav(item: SignalObject, days: Sequence[dt.date], returns: Mapping[str, Sequence[Decimal]],
               weights: Mapping[str, Decimal], config: Round2Config) -> tuple[NavRow, ...]:
    return nav_rows(item.name, days, item.exposures, returns, weights, len(item.switches),
                    exposure_change(item.switches), config)


def reversal_share(switches: Sequence[Switch], window: int) -> Decimal | None:
    """随后 window 个交易日内出现反方向切换的切换占比；没有切换时为空。"""
    return Decimal(reversal_count(switches, window)) / len(switches) if switches else None


def extended_nav(prepared: PreparedEvaluation, validation: ValidationConfig,
                 config: Round2Config) -> ExtendedNavResult:
    """prepared 为纯价格版九组在补充历史窗口上的状态（输入已截至窗口末日）。"""
    model = validation.model
    if prepared.inputs.days[-1] != model.history_end:
        raise ValueError("补充历史净值只能使用截至窗口末日的输入")
    settings = configured_loss_settings(prepared.config)
    levels = exposure_levels(settings.parameters.eta)
    days = prepared.inputs.days[prepared.inputs.days.index(prepared.first_loss_day):]
    returns = {symbol: simple_returns(days, prepared.inputs.series[symbol]) for symbol in SYMBOLS}
    grid = tuple(signal_object(grid_name(states), prepared, states, levels) for states in prepared.states)
    chosen = [index for index, states in enumerate(prepared.states)
              if (states.candidate.k, states.candidate.theta_p) == (validation.locked_k, validation.locked_theta)]
    if len(chosen) != 1:
        raise ValueError("纯价格版九组中找不到唯一的选定设定")
    selected = signal_object(SELECTED_PRICE, prepared, prepared.states[chosen[0]], levels)
    average = signal_object(MA200, prepared, ma200_states(prepared, model.mr_window), levels)
    buffered = signal_object(BUFFERED_MA200, prepared,
                             buffered_ma_states(prepared, model.mr_window, config.buffer_band), levels)
    objects = (selected, average, buffered)
    nav = [row for item in objects for row in object_nav(item, days, returns, settings.weights, config)]
    constants = ((HOLD, Decimal(1)), *((f"恒定暴露 ē（取{item.name}的 ē）", item.mean_exposure)
                                       for item in (selected, average)))
    count = len(days) - 1
    for name, level in constants:
        nav.extend(nav_rows(name, days, [level] * count, returns, settings.weights, 0, Decimal(0), config))
    grid_nav = tuple(row for item in grid for row in object_nav(item, days, returns, settings.weights, config))
    return ExtendedNavResult(prepared, days, objects, grid, tuple(nav), grid_nav,
                             period_rows(days, nav, model.bear_markets))
