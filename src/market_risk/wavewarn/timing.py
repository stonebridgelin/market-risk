"""v1.3 择时得分、平均执行暴露、非绿占比与参照行（纯计算）。

择时得分 T_M = L_M − [ē_M·L_G + (1−ē_M)·L_R]：把模型的主损失与“同等平均暴露的恒定持仓”相比。
每日价格损失对暴露线性，所以恒定暴露 e 的参照行主损失恰为 e·L_G + (1−e)·L_R，T 为 0。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    Candidate,
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    axis_loss,
    evaluate_candidate,
)
from market_risk.wavewarn.execution import ExecutionDay, exposure
from market_risk.wavewarn.features import moving_average
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.state_machine import Light
from market_risk.wavewarn.state_sequences import DiagnosticRow

CONSTANT_REFERENCES: tuple[tuple[str, Light], ...] = (("始终绿", "绿"), ("始终黄", "黄"), ("始终红", "红"))
MA200_REFERENCE = "200日均线"


@dataclass(frozen=True)
class TimingResult:
    intervals: int               # |J|：主损失计入的区间数，含被排除的价格区间
    mean_exposure: Decimal       # ē：J 上执行暴露的平均
    benchmark_loss: Decimal      # ē·L_G + (1−ē)·L_R
    score: Decimal               # T = L − 基准
    non_green_share: Decimal     # J 中执行灯色为黄或红的区间数 ÷ |J|


def mean_executed_exposure(evaluated: CandidateEvaluation, eta: Decimal) -> Decimal:
    """ē = (1/|J|) Σ e_j，e_j 由系统执行灯色 S_{j−1} 决定（绿 1、黄 η、红 0）。"""
    lights = evaluated.system_executed[:-1]
    if not lights:
        raise ValueError("没有计入区间，无法计算平均暴露")
    return sum((exposure(light, eta) for light in lights), Decimal(0)) / len(lights)  # type: ignore[arg-type]


def benchmark_loss(mean_exposure: Decimal, green_loss: Decimal, red_loss: Decimal) -> Decimal:
    """同等平均暴露的恒定持仓的主损失。"""
    return mean_exposure * green_loss + (1 - mean_exposure) * red_loss


def timing_result(evaluated: CandidateEvaluation, eta: Decimal, green_loss: Decimal,
                  red_loss: Decimal) -> TimingResult:
    """切换罚分与漏报罚项只在模型一侧：L_G、L_R 传入的是两条参照行的主损失。"""
    intervals = len(evaluated.days) - 1
    mean = mean_executed_exposure(evaluated, eta)
    benchmark = benchmark_loss(mean, green_loss, red_loss)
    return TimingResult(intervals, mean, benchmark, evaluated.total_loss - benchmark,
                        Decimal(evaluated.executed_non_green_days) / intervals)


def timing_daily_terms(model_daily: Sequence[Decimal], green_daily: Sequence[Decimal],
                       red_daily: Sequence[Decimal], mean_exposure: Decimal) -> tuple[Decimal, ...]:
    """逐日的 ℓ_{M,j} − ē_M·g_j − (1−ē_M)·r_j；全部相加即 T_M。ē 视为常数。"""
    if not (len(model_daily) == len(green_daily) == len(red_daily)):
        raise ValueError("逐日损失序列长度不一致")
    return tuple(loss - mean_exposure * green - (1 - mean_exposure) * red
                 for loss, green, red in zip(model_daily, green_daily, red_daily, strict=True))


def paired_timing_differences(n_daily: Sequence[Decimal], p1_daily: Sequence[Decimal],
                              green_daily: Sequence[Decimal], red_daily: Sequence[Decimal],
                              mean_n: Decimal, mean_p1: Decimal) -> tuple[Decimal, ...]:
    """唯一主检验的日度差 d_j = [ℓ_N − ē_N g − (1−ē_N) r] − [ℓ_P1 − ē_P1 g − (1−ē_P1) r]。

    本批只实现并用构造数据测试，没有在验证期运行。
    """
    n_terms = timing_daily_terms(n_daily, green_daily, red_daily, mean_n)
    p1_terms = timing_daily_terms(p1_daily, green_daily, red_daily, mean_p1)
    return tuple(first - second for first, second in zip(n_terms, p1_terms, strict=True))


def caution_gap(mean_n: Decimal, mean_p1: Decimal, green_loss: Decimal, red_loss: Decimal) -> Decimal:
    """“谨慎程度差” (ē_N − ē_P1)(L_G − L_R)，只作描述。"""
    return (mean_n - mean_p1) * (green_loss - red_loss)


def constant_reference(prepared: PreparedEvaluation, name: str, light: Light, order: int,
                       events: Mapping[str, Sequence[ZZEvent]], unknown: UnknownLabels) -> CandidateEvaluation:
    """恒定灯色的参照行：从 τ 起即为该暴露、零切换，按 j₀ 与同一损失函数计算。"""
    settings = configured_loss_settings(prepared.config)
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.first_loss_day):]
    level = exposure(light, settings.parameters.eta)
    executions = {symbol: tuple(ExecutionDay(day, light, light, level,
                                             prepared.inputs.series[symbol].get(day) is not None, False)
                                for day in axis)
                  for symbol in SYMBOLS}
    losses = axis_loss(prepared, executions, events, unknown, settings)
    candidate = Candidate(name, 0, Decimal(0), None, order)  # type: ignore[arg-type]
    count = len(axis)
    return CandidateEvaluation(candidate, axis, (light,) * count, ("完整",) * count, ((),) * count,
                               ("恒定参照",) * count, executions, losses.asset_losses, losses.daily_losses,
                               (light,) * count, losses.miss_penalty)


def ma200_signals(days: Sequence[dt.date], closes: Mapping[dt.date, Decimal | None], start: dt.date,
                  window: int) -> tuple[Light, ...]:
    """自 start 起逐日信号：收盘价不低于其 window 日均线为绿，否则为红；缺输入即报错，不填补。"""
    values = tuple(closes.get(day) for day in days)
    signals: list[Light] = []
    for index in range(days.index(start), len(days)):
        close, average = values[index], moving_average(values, index, window)
        if close is None or average is None:
            raise ValueError(f"200日均线参照在 {days[index]} 缺少收盘价或均线")
        signals.append("绿" if close >= average else "红")
    return tuple(signals)


def signal_reference(prepared: PreparedEvaluation, name: str, signals: Sequence[Light], order: int,
                     events: Mapping[str, Sequence[ZZEvent]], unknown: UnknownLabels) -> CandidateEvaluation:
    """由逐日信号驱动的参照行：自 t0 起次日收盘执行，切换罚分照常，同一损失函数与 j₀。"""
    return evaluate_candidate(prepared, signal_states(prepared, name, signals, order), events, unknown)


def signal_states(prepared: PreparedEvaluation, name: str, signals: Sequence[Light],
                  order: int) -> CandidateStates:
    """把逐日信号包装成与模型相同的状态序列（自 t0 起），供损失与退出代价共用同一条计算路径。"""
    state_days = prepared.inputs.days[prepared.inputs.days.index(prepared.t0):]
    if len(signals) != len(state_days):
        raise ValueError("参照信号须覆盖 t0 至窗口末日的全部交易日")
    candidate = Candidate(name, 0, Decimal(0), None, order)  # type: ignore[arg-type]
    rows = tuple(DiagnosticRow(day, "P0", 0, Decimal(0), light, "完整", (), "信号参照")
                 for day, light in zip(state_days, signals, strict=True))
    return CandidateStates(candidate, prepared.t0, rows)


def ma200_states(prepared: PreparedEvaluation, window: int) -> CandidateStates:
    """200日均线参照的状态序列：SPX 收盘价不低于其均线为绿，否则为红。"""
    signals = ma200_signals(prepared.inputs.days, prepared.inputs.series["SPX"], prepared.t0, window)
    return signal_states(prepared, MA200_REFERENCE, signals, len(CONSTANT_REFERENCES))


@dataclass(frozen=True)
class ReferenceRow:
    """一条参照行的评价结果与择时指标。"""

    evaluated: CandidateEvaluation
    timing: TimingResult

    @property
    def name(self) -> str:
        return self.evaluated.candidate.model


def reference_rows(prepared: PreparedEvaluation, events: Mapping[str, Sequence[ZZEvent]],
                   unknown: UnknownLabels, ma200_window: int) -> tuple[ReferenceRow, ...]:
    """四条参照行，顺序固定：始终绿、始终黄、始终红、200日均线（SPX 信号，两资产共用）。"""
    constants = tuple(constant_reference(prepared, name, light, index, events, unknown)
                      for index, (name, light) in enumerate(CONSTANT_REFERENCES))
    signals = ma200_signals(prepared.inputs.days, prepared.inputs.series["SPX"], prepared.t0, ma200_window)
    evaluations = (*constants,
                   signal_reference(prepared, MA200_REFERENCE, signals, len(constants), events, unknown))
    eta = configured_loss_settings(prepared.config).parameters.eta
    green, red = constants[0].total_loss, constants[2].total_loss
    return tuple(ReferenceRow(item, timing_result(item, eta, green, red)) for item in evaluations)
