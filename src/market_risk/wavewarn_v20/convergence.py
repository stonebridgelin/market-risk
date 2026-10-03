"""预热、收敛与共同起点（登记第四节）。纯计算。

- t0：五个通道的输入与回看期首次同时完整的交易日；当日只放初始状态、不更新。
- 通道收敛：每个通道从 t0 起，以其状态域中的每一种初始状态各运行一次，全部运行首次相同之日。
- 系统收敛：在 κ_通道 当日放入 (S, c₁, c₂) 的全部 3 × (h+1)² 种初始值，自下一日起更新，
  全部运行的完整状态首次逐项相同之日。判断用完整状态逐项相同，不用某一天风险状态相同。
- j₀ = max(t0 + 63, κ_全 + 1)。任一对象始终不收敛即报错。
主参照：S_参照 以三种初始值各运行一次，取首次相同之日；它参与 κ_全 的取最大值。
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from types import MappingProxyType

from market_risk.wavewarn_v20.channels import (
    PULLBACK_DOMAIN,
    TREND_DOMAIN,
    ChannelState,
    pullback_valid,
    run_pullback,
    run_trend,
    trend_valid,
)
from market_risk.wavewarn_v20.inputs import AssetDay, TrendDay
from market_risk.wavewarn_v20.reference import run_reference
from market_risk.wavewarn_v20.state_machine import (
    ChannelInitial,
    Evidence,
    Risk,
    SystemState,
    evidence_series,
    run_channels,
    run_system,
)

CHANNEL_NAMES = ("P_SPX", "P_QQQ", "PR_SPX", "PR_QQQ", "MR")
# 登记第四节第 1 小节：t0 是初始快照，所有通道“已武装、未激活”，S = 正常，c₁ = c₂ = 0。
REGISTERED_CHANNELS = ChannelInitial(*(ChannelState.ARMED,) * 5)
REGISTERED_SYSTEM = SystemState(Risk.NORMAL, 0, 0)
REGISTERED_REFERENCE = Risk.NORMAL        # 主参照在 t0 的初始值；收敛之后的路径与它无关


class ConvergenceError(ValueError):
    """预热、收敛与共同起点的异常基类。实际抛出的都是下面的子类之一，reason 为原因码，可在机器层面区分。"""

    reason = ""


class NoStartError(ConvergenceError):
    """序列内没有五个通道同时有效的交易日，无法确定 t0。"""

    reason = "无法确定 t0"


class NotConvergedError(ConvergenceError):
    """某个对象在给定的序列内始终不收敛。"""

    reason = "始终不收敛"


class StartInputError(ConvergenceError):
    """共同起点的输入不合法（如没有任何对象的系统收敛日）。"""

    reason = "输入校验失败"


# 评价窗口为空的细分（补充裁决 Q14）：两种情形主原因相同，细分字段区分。
EMPTY_AT_LAST_DAY = "j₀ 等于最后一个收盘日"
EMPTY_BEYOND_AXIS = "j₀ 超出日期轴"


class EmptyWindowError(ConvergenceError):
    """评价窗口为空：可计入收益区间数 n = 0（补充裁决 Q14）。与“始终不收敛”是不同的原因。

    detail 为细分：EMPTY_AT_LAST_DAY 或 EMPTY_BEYOND_AXIS。
    """

    reason = "评价窗口为空"

    def __init__(self, detail: str, start: int, length: int) -> None:
        self.detail = detail
        super().__init__(f"评价窗口为空（{detail}）：j₀ 行号 {start}，日期轴共 {length} 日，可计入收益区间数 n = 0")


@dataclass(frozen=True)
class Candidate:
    k: int
    theta: Decimal
    h: int


@dataclass(frozen=True)
class CandidateConvergence:
    """一组候选的收敛结果；下标都是交易日轴上的行号。"""

    candidate: Candidate
    channel_indices: Mapping[str, int]     # 各通道的收敛日
    kappa_channel: int                     # 五个通道中最晚的收敛日
    system_index: int                      # 系统收敛日


def first_valid_index(spx: Sequence[AssetDay], qqq: Sequence[AssetDay], trend: Sequence[TrendDay]) -> int:
    """t0：五个通道首次同时有效的交易日在日期轴上的行号。"""
    for index, (a, b, c) in enumerate(zip(spx, qqq, trend, strict=True)):
        if pullback_valid(a) and pullback_valid(b) and trend_valid(c):
            return index
    raise NoStartError("序列内没有五个通道同时有效的交易日，无法确定 t0")


def channel_initial_states(domain: frozenset[ChannelState]) -> tuple[ChannelState, ...]:
    """由状态域生成全部初始状态（不手写清单）。"""
    return tuple(sorted(domain, key=lambda state: state.value))


def system_initial_states(h: int) -> tuple[SystemState, ...]:
    """(S, c₁, c₂) 的全部 3 × (h+1)² 种组合。"""
    return tuple(SystemState(risk, c1, c2) for risk, c1, c2 in product(Risk, range(h + 1), range(h + 1)))


def first_common_index(runs: Sequence[Sequence[Hashable]]) -> int | None:
    """全部运行在同一位置上的完整状态首次逐项相同的位置；没有则为 None。"""
    for index, states in enumerate(zip(*runs, strict=True)):
        if len(set(states)) == 1:
            return index
    return None


def _required(index: int | None, name: str) -> int:
    if index is None:
        raise NotConvergedError(f"{name} 在给定的序列内始终不收敛")
    return index


def pullback_convergence(inputs: Sequence[AssetDay], k: int, threshold: Decimal, name: str) -> int:
    """P 或 PR 的收敛位置（相对于 inputs，即初始日之后的各日）。"""
    runs = [[item.state for item in run_pullback(initial, inputs, k, threshold)]
            for initial in channel_initial_states(PULLBACK_DOMAIN)]
    return _required(first_common_index(runs), name)


def trend_convergence(inputs: Sequence[TrendDay], average: int) -> int:
    """MR 的收敛位置（相对于 inputs）。"""
    runs = [[item.state for item in run_trend(initial, inputs, average)]
            for initial in channel_initial_states(TREND_DOMAIN)]
    return _required(first_common_index(runs), "MR")


def system_convergence(evidence: Sequence[Evidence], k: int, h: int) -> int:
    """系统的收敛位置（相对于 evidence，即 κ_通道 之后的各日）。"""
    runs = [[item.state for item in run_system(initial, evidence, k, h)] for initial in system_initial_states(h)]
    return _required(first_common_index(runs), f"系统（K = {k}，h = {h}）")


def reference_initial_states() -> tuple[Risk, ...]:
    """主参照的完整状态只有 S_参照，共三种初始值。"""
    return tuple(Risk)


def reference_convergence(inputs: Sequence[TrendDay], average: int) -> int:
    """主参照的收敛位置（相对于 inputs，即 t0 之后的各日）。"""
    runs = [[item.risk for item in run_reference(initial, inputs, average)] for initial in reference_initial_states()]
    return _required(first_common_index(runs), "主参照")


def candidate_convergence(spx: Sequence[AssetDay], qqq: Sequence[AssetDay], trend: Sequence[TrendDay],
                          t0_index: int, candidate: Candidate, average: int) -> CandidateConvergence:
    """一组候选的通道收敛日、κ_通道 与系统收敛日。三个输入序列覆盖整条日期轴。"""
    after = slice(t0_index + 1, None)
    first = t0_index + 1
    theta, k = candidate.theta, candidate.k
    channel_indices = {
        "P_SPX": first + pullback_convergence(spx[after], k, theta, "P_SPX"),
        "P_QQQ": first + pullback_convergence(qqq[after], k, theta, "P_QQQ"),
        "PR_SPX": first + pullback_convergence(spx[after], k, 2 * theta, "PR_SPX"),
        "PR_QQQ": first + pullback_convergence(qqq[after], k, 2 * theta, "PR_QQQ"),
        "MR": first + trend_convergence(trend[after], average),
    }
    kappa = max(channel_indices.values())
    # 自 κ_通道 起通道路径已与初始化无关，用登记的初始快照得到的路径即可。
    paths = run_channels(REGISTERED_CHANNELS, spx[after], qqq[after], trend[after], k, theta, average)
    evidence = evidence_series(spx[after], qqq[after], trend[after], paths)
    system_index = kappa + 1 + system_convergence(evidence[kappa - t0_index:], k, candidate.h)
    return CandidateConvergence(candidate, MappingProxyType(channel_indices), kappa, system_index)


def common_start_index(t0_index: int, system_indices: Sequence[int], offset: int, length: int) -> int:
    """j₀ = max(t0 + offset, κ_全 + 1)；offset 登记为 63。

    κ_全 为全部候选与主参照的系统收敛日的最大值：system_indices 须同时包含各候选与主参照的收敛日。

    变量含义：t0_index、system_indices 与返回的 j₀ 都是日期轴上收盘日的行号（从 0 起）；length 是日期轴的天数，
    日期轴的最后一日（行号 length − 1）即最后一个可用收盘日 E。收益区间为第 j 日收盘到第 j+1 日收盘，
    可计入的区间要求 j ≥ j₀ 且 j+1 ≤ E，所以 n = E − j₀ = length − 1 − j₀（j₀ ≤ E 时）。
    n = 0 时停止，原因为“评价窗口为空”（补充裁决 Q14）：j₀ = E 与 j₀ > E 两种情形用细分字段区分。
    """
    if not system_indices:
        raise StartInputError("没有任何对象的系统收敛日")
    start = max(t0_index + offset, max(system_indices) + 1)
    last = length - 1
    if start > last:
        raise EmptyWindowError(EMPTY_BEYOND_AXIS, start, length)
    if start == last:
        raise EmptyWindowError(EMPTY_AT_LAST_DAY, start, length)
    return start

