"""backtest zigzag-check：只输出由收盘价计算的价格波段（补充2，2026-09-27）。

不读取、不输出任何分数、预警状态或结果标签，不导入评分模块，不读取回测运行目录；
价格波段是公开的市场事实，不涉及系统表现，因此允许包含保留期的日期。
收盘价取数据集 data/market/（公布的两位小数，ROUND_HALF_UP）；ZigZag 在该标的全部历史上运行，
输出与 [from, to] 有交集的回调（高点不晚于 to，且低点不早于 from；未确认的回调按序列末尾计）。
负责人图表中的波段线使用日内最高价与最低价，这里使用收盘价，日期与点位可能略有差异。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from market_risk.backtest.settings import Grades
from market_risk.backtest.zigzag import zigzag
from market_risk.data.market import read_series_file
from market_risk.storage.paths import StoragePaths

PRICE_Q = Decimal("0.01")


@dataclass(frozen=True)
class SwingRow:
    high_date: dt.date
    high_close: Decimal
    low_date: dt.date
    low_close: Decimal
    drawdown_pct: Decimal
    grade: str
    status: str


def price_swings(paths: StoragePaths, symbol: str, level: Decimal, grades: Grades,
                 start: dt.date, end: dt.date) -> list[SwingRow]:
    rows = read_series_file(paths.market_daily_file(symbol))[1]
    closes = [(d, Decimal(repr(float(r["value"]))).quantize(PRICE_Q, ROUND_HALF_UP))
              for d, r in sorted(rows.items()) if r["value"] is not None]
    out = []
    for s in zigzag(closes, level):
        if s.high_date > end or s.low_date < start:
            continue
        out.append(SwingRow(s.high_date, s.high_close, s.low_date, s.low_close,
                            (s.drawdown * 100).quantize(Decimal("0.0001"), ROUND_HALF_UP),
                            grades.grade(-s.drawdown), s.status))
    return out
