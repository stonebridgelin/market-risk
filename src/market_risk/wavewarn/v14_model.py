"""v1.4 模型（纯计算）：趋势层 MR、通道组成、解除规则 F 下的状态序列与评价准备。

三个变体：
- V4：MR + 两资产 P、PR + 两资产 BW + V（登记的候选模型）；
- V4去MR：去掉 MR 的描述性对照；
- V4纯价格：MR + 两资产 P、PR（补充历史只能运行这一版，不依赖广度与期限结构）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.channels import ChannelDay, ChannelPredicate, update_channel
from market_risk.wavewarn.config import ChannelSelection, FixedParameters
from market_risk.wavewarn.config_v14 import V14Config
from market_risk.wavewarn.convergence import loss_start, system_convergence
from market_risk.wavewarn.evaluation import (
    Candidate,
    CandidateStates,
    PreparedEvaluation,
    candidate_states,
    first_loss_interval,
    grid_features,
    price_channels,
    validated_channel_selection,
)
from market_risk.wavewarn.feasibility import N_PRIME_MODEL
from market_risk.wavewarn.features import AssetFeatures, moving_average
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.state_machine import Level, ReadyInputs, SystemMemory, step_system
from market_risk.wavewarn.state_sequences import DiagnosticRow, first_complete_day, n_channel_inputs

MR_CHANNEL = "MR_SPX"
RELEASE_RULE = "F"
FULL, NO_TREND, PRICE_ONLY = "V4", "V4去MR", "V4纯价格"
DISPLAY = {FULL: "v1.4", NO_TREND: "v1.4 去掉 MR", PRICE_ONLY: "v1.4 纯价格版", N_PRIME_MODEL: "N′"}
Channels = Mapping[str, tuple[Level, Sequence[ChannelPredicate]]]


def trend_predicates(closes: Sequence[Decimal | None], window: int) -> tuple[ChannelPredicate, ...]:
    """MR：当日收盘价低于其 window 日均线即进入，不低于即退出；无滞后区，退出后立即重新武装。

    因此输入有效时，当日 MR 激活 ⇔ 当日收盘价低于均线，与 200 日均线参照行的信号相同。
    收盘价或均线缺失时三个谓词都为未知，通道当日无效、状态沿用。
    """
    result = []
    for index, close in enumerate(closes):
        average = moving_average(closes, index, window)
        if close is None or average is None:
            result.append(ChannelPredicate(None, None, None, True, "收盘价或均线缺失"))
            continue
        below = close < average
        result.append(ChannelPredicate(below, not below, True, True, f"收盘 {close}；{window}日均线 {average}"))
    return tuple(result)


def v14_channels(model: str, spx: Sequence[AssetFeatures], qqq: Sequence[AssetFeatures],
                 ratios: Sequence[Decimal | None], candidate: Candidate, fixed: FixedParameters,
                 mr_window: int) -> dict[str, tuple[Level, Sequence[ChannelPredicate]]]:
    """按变体组合通道；B、DV 两侧都不含。"""
    if model == PRICE_ONLY:
        channels = dict(price_channels(spx, qqq, candidate.theta_p, candidate.k))
    elif model in (FULL, NO_TREND):
        channels = dict(n_channel_inputs(spx, qqq, ratios, candidate.theta_p, candidate.k,
                                         ChannelSelection(False, False, False, False), fixed))
    else:
        raise ValueError(f"不是 v1.4 的变体：{model}")
    if model != NO_TREND:
        channels[MR_CHANNEL] = ("红", trend_predicates(tuple(item.close for item in spx), mr_window))
    return channels  # type: ignore[return-value]


def release_f_inputs(spx: Sequence[AssetFeatures], qqq: Sequence[AssetFeatures]) -> tuple[ReadyInputs, ...]:
    """解除规则 F 只需要两指数的 Q；两指数当日都有收盘价才算输入有效。"""
    return tuple(ReadyInputs(a.q, b.q, None, None, None, None, None, a.close is not None and b.close is not None)
                 for a, b in zip(spx, qqq, strict=True))


def release_f_sequence(days: Sequence[dt.date], channels: Channels, ready: Sequence[ReadyInputs], t0: dt.date,
                       candidate: Candidate, fixed: FixedParameters) -> tuple[DiagnosticRow, ...]:
    """t0 为初始快照（绿、通道已武装）；次日起逐日更新通道并按解除规则 F 推进灯色。"""
    states = {name: "armed" for name in channels}
    memory = SystemMemory("绿", 0, 0, 0)
    rows = [DiagnosticRow(t0, candidate.model, candidate.k, candidate.theta_p, "绿",  # type: ignore[arg-type]
                          "完整", (), "t0 初始快照")]
    for index in range(days.index(t0) + 1, len(days)):
        today: dict[str, tuple[Level, ChannelDay]] = {}
        for name, (level, series) in channels.items():
            update = update_channel(days[index], states[name], series[index])  # type: ignore[arg-type]
            states[name] = update.status
            today[name] = (level, update)
        system = step_system(days[index], memory, today, ready[index], candidate.k, RELEASE_RULE,
                             fixed.quiet_red_days, fixed.quiet_all_days)
        memory = system.memory
        active = tuple(sorted(name for name, (_, state) in today.items() if state.status == "active"))
        rows.append(DiagnosticRow(days[index], candidate.model, candidate.k,  # type: ignore[arg-type]
                                  candidate.theta_p, memory.light, system.data_status, active, system.reason))
    return tuple(rows)


@dataclass(frozen=True)
class V14Features:
    """v1.4 各变体共用的输入：两资产特征与逐日 VIX÷VIX3M（纯价格版不使用后者）。"""

    spx: tuple[AssetFeatures, ...]
    qqq: tuple[AssetFeatures, ...]
    ratios: tuple[Decimal | None, ...]


def v14_states(candidate: Candidate, days: Sequence[dt.date], features: V14Features, t0: dt.date,
               fixed: FixedParameters, mr_window: int) -> CandidateStates:
    """一个 v1.4 设定的状态序列与系统收敛日。"""
    channels = v14_channels(candidate.model, features.spx, features.qqq, features.ratios, candidate, fixed,
                            mr_window)
    ready = release_f_inputs(features.spx, features.qqq)
    converged = system_convergence(days, channels, ready, t0, candidate.k, RELEASE_RULE, fixed)
    if converged.system_date is None:
        raise ValueError(f"v1.4 设定未收敛：{candidate.model} K={candidate.k} θ={candidate.theta_p}")
    return CandidateStates(candidate, converged.system_date,
                           release_f_sequence(days, channels, ready, t0, candidate, fixed))


def v14_grid(config: V14Config, models: Sequence[str], first_order: int) -> tuple[Candidate, ...]:
    """每个变体 9 组，登记顺序先 K 后 θ_P。"""
    result: list[Candidate] = []
    for model in models:
        for k in config.k:
            for theta in config.theta_p:
                result.append(Candidate(model, k, theta, None, first_order + len(result),  # type: ignore[arg-type]
                                        RELEASE_RULE))
    return tuple(result)


def median_controls(config: V14Config, first_order: int) -> tuple[Candidate, ...]:
    """描述性对照的中位设定：P1·E2、N′·X2、N·E2。"""
    middle = config.median
    return (Candidate("P1", middle.k, middle.theta_p, None, first_order, "E2"),
            Candidate(N_PRIME_MODEL, middle.k, middle.theta_p, None, first_order + 1, "X2"),  # type: ignore[arg-type]
            Candidate("N", middle.k, middle.theta_p, middle.q, first_order + 2, "E2"))


def prepare_v14(config: V14Config, inputs: DevelopmentInputs) -> PreparedEvaluation:
    """开发期：v1.4 九组、去掉 MR 九组与三个中位对照共 21 组，共用 t0、τ 与 j₀。"""
    base = config.base
    selection = validated_channel_selection(base)
    fixed = base.fixed_parameters()
    by_q, ratios = grid_features(base, inputs)
    spx, qqq = by_q[base.candidate_sets().q[0]]
    t0 = first_complete_day(inputs.days, spx, qqq, inputs.series["VIX"], inputs.series["VIX3M"], fixed)
    features = V14Features(tuple(spx), tuple(qqq), ratios)
    grid = v14_grid(config, (FULL, NO_TREND), 0)
    states = [v14_states(candidate, inputs.days, features, t0, fixed, config.mr_window) for candidate in grid]
    states.extend(candidate_states(candidate, base, inputs, by_q, ratios, t0, selection)
                  for candidate in median_controls(config, len(grid)))
    dates = [item.convergence_date for item in states]
    tau = loss_start(inputs.days, t0, dates, fixed)
    return PreparedEvaluation(base, inputs, t0, tau, first_loss_interval(inputs.days, tau, dates), tuple(states))
