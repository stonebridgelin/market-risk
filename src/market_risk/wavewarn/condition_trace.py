"""逐日条件追踪（纯计算，描述性诊断用）：重放一个设定的通道与状态机，记下每个转绿条件当日是否成立。

不改变任何规则：灯色由 state_machine.step_system 给出，这里只把它用到的各个条件分别记录下来。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from market_risk.wavewarn.channels import ChannelDay, ChannelPredicate, update_channel
from market_risk.wavewarn.state_machine import (
    ExitVersion,
    Level,
    ReadyInputs,
    SystemMemory,
    is_price_channel,
    step_system,
)

Q_OK = "(1) 两指数 Q≥K"
PRICE_CLEAR = "(2) 价格通道有效且未激活"
NONPRICE_QUIET = "(3) 非价格通道连续5天有效且未激活"
ALL_QUIET = "全部通道连续5天有效且未激活（E2、P0）"
REPAIRED_SPX = "(4) S5TW 广度修复（不低于60日中位数连续3天）"
REPAIRED_QQQ = "(5) NDTW 广度修复（不低于60日中位数连续3天）"
ABOVE_MA50 = "(6) 两指数收盘高于 MA50"
NOT_RED = "(7) 系统已由红转黄"
RED_QUIET = "红通道连续3天有效且未激活"
CONDITIONS = (Q_OK, PRICE_CLEAR, NONPRICE_QUIET, ALL_QUIET, REPAIRED_SPX, REPAIRED_QQQ, ABOVE_MA50)


@dataclass(frozen=True)
class ConditionDay:
    """一个交易日收盘后的灯色、激活通道与各条件。"""

    date: dt.date
    light: str
    previous_light: str
    active: tuple[str, ...]
    ready: bool
    red_quiet: bool                          # 红转黄所需：红通道连续 3 天有效且未激活
    flags: Mapping[str, bool | None]         # 条件名 → 当日是否成立；None 表示该模型没有这一条件


def required_conditions(version: str, has_nonprice: bool) -> tuple[str, ...]:
    """各退出版本的黄转绿条件（与 state_machine.ready_condition 一致）。"""
    if version == "P0":
        return ALL_QUIET, Q_OK, ABOVE_MA50
    if version == "E2":
        return ALL_QUIET, Q_OK, REPAIRED_SPX, REPAIRED_QQQ, ABOVE_MA50
    relaxed = (Q_OK, PRICE_CLEAR, *((NONPRICE_QUIET,) if has_nonprice else ()), REPAIRED_SPX, REPAIRED_QQQ)
    if version == "X2":
        return relaxed
    if version == "X1":
        return *relaxed, ABOVE_MA50
    raise ValueError(version)


def _flags(memory: SystemMemory, rows: Mapping[str, tuple[Level, ChannelDay]], inputs: ReadyInputs, k: int,
           quiet_days: int) -> dict[str, bool | None]:
    """把 Ready 的各个组成部分分开记录。"""
    price = [state for name, (_, state) in rows.items() if is_price_channel(name)]
    has_nonprice = len(price) < len(rows)
    return {
        Q_OK: inputs.q_spx >= k and inputs.q_qqq >= k,
        PRICE_CLEAR: all(state.valid and state.status != "active" for state in price),
        NONPRICE_QUIET: memory.quiet_nonprice >= quiet_days if has_nonprice else None,
        ALL_QUIET: memory.quiet_all >= quiet_days,
        REPAIRED_SPX: inputs.breadth_repaired_spx is True,
        REPAIRED_QQQ: inputs.breadth_repaired_qqq is True,
        ABOVE_MA50: inputs.above_ma50_spx is True and inputs.above_ma50_qqq is True,
    }


def condition_trace(days: Sequence[dt.date],
                    channels: Mapping[str, tuple[Level, Sequence[ChannelPredicate]]],
                    ready_inputs: Sequence[ReadyInputs], t0: dt.date, k: int, version: ExitVersion,
                    red_quiet_days: int = 3, quiet_days: int = 5) -> tuple[ConditionDay, ...]:
    """自 t0 的次日起逐日重放；t0 为初始快照（绿、通道已武装），不产生追踪行。"""
    start = days.index(t0)
    states = {name: "armed" for name in channels}
    memory = SystemMemory()
    has_nonprice = any(not is_price_channel(name) for name in channels)
    required = required_conditions(version, has_nonprice)
    result: list[ConditionDay] = []
    for index in range(start + 1, len(days)):
        rows: dict[str, tuple[Level, ChannelDay]] = {}
        for name, (level, series) in channels.items():
            update = update_channel(days[index], states[name], series[index])  # type: ignore[arg-type]
            states[name] = update.status
            rows[name] = (level, update)
        previous = memory.light
        system = step_system(days[index], memory, rows, ready_inputs[index], k, version, red_quiet_days,
                             quiet_days)
        memory = system.memory
        flags = _flags(memory, rows, ready_inputs[index], k, quiet_days)
        if system.ready != all(flags[name] is True for name in required):
            raise ValueError(f"{days[index]} 条件分解与状态机的 Ready 不一致")
        active = tuple(sorted(name for name, (_, state) in rows.items() if state.status == "active"))
        result.append(ConditionDay(days[index], memory.light, previous, active, system.ready,
                                   memory.quiet_red >= red_quiet_days, flags))
    return tuple(result)
