"""描述性对照“带缓冲带的 200 日均线”（纯计算）：补充登记 G。

t0 当日 SPX 收盘价不低于其 200 日均线为绿，否则为红；之后高于均线的 (1+带宽) 倍时转绿，低于 (1−带宽) 倍时转红，
区间内保持前状态。次日收盘执行、切换罚分照常（与 200 日均线参照行走同一条执行与损失路径）。
带宽为登记的固定参考值 1%，不扫描其他变体；不替代主检验。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal

from market_risk.wavewarn.evaluation import CandidateStates, PreparedEvaluation
from market_risk.wavewarn.features import moving_average
from market_risk.wavewarn.state_machine import Light
from market_risk.wavewarn.timing import CONSTANT_REFERENCES, signal_states

BUFFERED_MA200 = "带缓冲带的200日均线"


def buffered_ma_signals(days: Sequence[dt.date], closes: Mapping[dt.date, Decimal | None], start: dt.date,
                        window: int, band: Decimal) -> tuple[Light, ...]:
    """自 start 起逐日信号；缺收盘价或均线即报错，不填补。"""
    values = tuple(closes.get(day) for day in days)
    signals: list[Light] = []
    for index in range(days.index(start), len(days)):
        close, average = values[index], moving_average(values, index, window)
        if close is None or average is None:
            raise ValueError(f"带缓冲带的200日均线参照在 {days[index]} 缺少收盘价或均线")
        if not signals:
            signals.append("绿" if close >= average else "红")
        elif close > average * (1 + band):
            signals.append("绿")
        elif close < average * (1 - band):
            signals.append("红")
        else:
            signals.append(signals[-1])
    return tuple(signals)


def buffered_ma_states(prepared: PreparedEvaluation, window: int, band: Decimal) -> CandidateStates:
    """把信号包装成与模型相同的状态序列（自 t0 起）：SPX 信号，两资产共用。"""
    signals = buffered_ma_signals(prepared.inputs.days, prepared.inputs.series["SPX"], prepared.t0, window, band)
    return signal_states(prepared, BUFFERED_MA200, signals, len(CONSTANT_REFERENCES) + 1)
