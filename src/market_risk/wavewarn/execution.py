"""次日收盘执行与跨缺价区间标记。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.state_machine import Light


@dataclass(frozen=True)
class ExecutionDay:
    date: dt.date
    signal: Light
    executed: Light
    exposure: Decimal
    price_available: bool
    switched: bool


@dataclass(frozen=True)
class ExecutionInterval:
    start: dt.date
    end: dt.date
    exposure: Decimal
    excluded: bool
    reason: str


def exposure(light: Light, eta: Decimal) -> Decimal:
    """绿=1，黄=η，红=0。"""
    if light == "绿":
        return Decimal(1)
    if light == "黄":
        return eta
    if light == "红":
        return Decimal(0)
    raise ValueError(light)


def execute_asset(days: Sequence[dt.date], signals: Sequence[Light],
                  closes: Sequence[Decimal | None], eta: Decimal,
                  initial_executed: Light = "绿") -> tuple[ExecutionDay, ...]:
    """缺价日保持实际暴露；恢复日只执行前一日最新信号。"""
    if len(days) != len(signals) or len(days) != len(closes):
        raise ValueError("日期、信号与收盘价数量不一致")
    if eta < 0 or eta > 1:
        raise ValueError("η 必须位于 0 至 1")
    actual = initial_executed
    system_previous = initial_executed
    rows = []
    for index, day in enumerate(days):
        target = signals[index - 1] if index else initial_executed
        switched = index > 0 and target != system_previous
        if closes[index] is not None:
            actual = target
        rows.append(ExecutionDay(day, signals[index], actual, exposure(actual, eta),
                                 closes[index] is not None, switched))
        system_previous = target
    return tuple(rows)


def execution_intervals(days: Sequence[dt.date], closes: Sequence[Decimal | None],
                        executions: Sequence[ExecutionDay]) -> tuple[ExecutionInterval, ...]:
    """任一端缺价即排除该资产区间；暴露取起点收盘后的实际执行值。"""
    if len(days) != len(closes) or len(days) != len(executions):
        raise ValueError("日期、价格与执行数量不一致")
    return tuple(ExecutionInterval(days[index], days[index + 1], executions[index].exposure,
                                   closes[index] is None or closes[index + 1] is None,
                                   "跨缺价区间" if closes[index] is None or closes[index + 1] is None else "")
                 for index in range(len(days) - 1))
