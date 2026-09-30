"""通道与系统初始状态的逐日收敛检查。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from market_risk.wavewarn.channels import ChannelDay, ChannelPredicate, ChannelStatus, update_channel
from market_risk.wavewarn.config import FixedParameters
from market_risk.wavewarn.state_machine import Level, ReadyInputs, SystemMemory, step_system


@dataclass(frozen=True)
class ChannelConvergence:
    date: dt.date | None
    traces: Mapping[ChannelStatus, tuple[ChannelDay, ...]]


@dataclass(frozen=True)
class SystemConvergence:
    channel_dates: Mapping[str, dt.date | None]
    system_start: dt.date | None
    system_date: dt.date | None
    traces: Mapping[Literal["绿", "黄", "红"], tuple[str, ...]]


def channel_convergence(days: Sequence[dt.date], predicates: Sequence[ChannelPredicate],
                        t0: dt.date) -> ChannelConvergence:
    """t0 为初始快照；三种起始状态从下一交易日起接受相同输入。"""
    if len(days) != len(predicates):
        raise ValueError("日期与通道谓词数量不一致")
    if t0 not in days:
        raise ValueError("t0 不在交易日序列")
    start = days.index(t0)
    states: tuple[ChannelStatus, ...] = ("active", "armed", "unarmed")
    traces: dict[ChannelStatus, tuple[ChannelDay, ...]] = {}
    for initial in states:
        current = initial
        rows = [ChannelDay(t0, initial, True, "t0 初始快照")]
        for index in range(start + 1, len(days)):
            update = update_channel(days[index], current, predicates[index])
            rows.append(update)
            current = update.status
        traces[initial] = tuple(rows)
    for offset, date in enumerate(days[start:]):
        if len({traces[state][offset].status for state in states}) == 1:
            return ChannelConvergence(date, traces)
    return ChannelConvergence(None, traces)


def _quiet_count(start: int, names: Sequence[str],
                 channels: Mapping[str, tuple[Level, Sequence[ChannelDay]]]) -> int:
    count = 0
    for index in range(start, -1, -1):
        if not all(channels[name][1][index].valid and channels[name][1][index].status != "active"
                   for name in names):
            break
        count += 1
    return count


def system_convergence(days: Sequence[dt.date], channel_inputs: Mapping[str, tuple[Level, Sequence[ChannelPredicate]]],
                       ready_inputs: Sequence[ReadyInputs], t0: dt.date, k: int,
                       e_version: Literal["P0", "E1", "E2", "E3"],
                       fixed: FixedParameters | None = None) -> SystemConvergence:
    """全部通道收敛五个交易日后，以三种系统灯色快照检查收敛。"""
    if len(ready_inputs) != len(days):
        raise ValueError("Ready 输入与日期数量不一致")
    per_channel = {name: channel_convergence(days, predicates, t0)
                   for name, (_, predicates) in channel_inputs.items()}
    dates = {name: result.date for name, result in per_channel.items()}
    if not dates or any(date is None for date in dates.values()):
        return SystemConvergence(dates, None, None, {})
    latest = max(date for date in dates.values() if date is not None)
    start = days.index(latest) + (fixed.quiet_all_days if fixed else 5)
    if start >= len(days):
        return SystemConvergence(dates, None, None, {})
    channel_rows: dict[str, tuple[Level, Sequence[ChannelDay]]] = {}
    t0_index = days.index(t0)
    for name, (level, _) in channel_inputs.items():
        channel_rows[name] = (level, per_channel[name].traces["armed"])
    local_start = start - t0_index
    red_names = [name for name, (level, _) in channel_rows.items() if level == "红"]
    all_names = list(channel_rows)
    quiet_red = _quiet_count(local_start, red_names, channel_rows)
    quiet_all = _quiet_count(local_start, all_names, channel_rows)
    traces: dict[Literal["绿", "黄", "红"], tuple[str, ...]] = {}
    for initial in ("绿", "黄", "红"):
        memory = SystemMemory(initial, quiet_red, quiet_all)
        lights = [initial]
        for index in range(start + 1, len(days)):
            local = index - t0_index
            channels = {name: (level, rows[local]) for name, (level, rows) in channel_rows.items()}
            update = step_system(days[index], memory, channels, ready_inputs[index], k, e_version,
                                 fixed.quiet_red_days if fixed else 3,
                                 fixed.quiet_all_days if fixed else 5)
            memory = update.memory
            lights.append(memory.light)
        traces[initial] = tuple(lights)
    for offset, date in enumerate(days[start:]):
        if len({trace[offset] for trace in traces.values()}) == 1:
            return SystemConvergence(dates, days[start], date, traces)
    return SystemConvergence(dates, days[start], None, traces)


def loss_start(days: Sequence[dt.date], t0: dt.date,
               convergence_dates: Sequence[dt.date | None],
               fixed: FixedParameters | None = None) -> dt.date:
    """τ=max(t0+63, 所有候选模型的系统收敛日)；未收敛即拒绝。"""
    if not convergence_dates or any(date is None for date in convergence_dates):
        raise ValueError("有候选模型未收敛，不能确定损失起点")
    t0_index = days.index(t0)
    warmup = fixed.anchor if fixed else 63
    if t0_index + warmup >= len(days):
        raise ValueError("数据不足 t0 后 63 个交易日")
    return max(days[t0_index + warmup], *(date for date in convergence_dates if date is not None))
