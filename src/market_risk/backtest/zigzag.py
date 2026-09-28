"""多层级回调识别（ZigZag）。事后标签：用基准日之后的数据确定，评分代码不得读取。

每个层级独立运行（2026-09-27 确认）：
- 以序列第一天为候选高点；只有严格创新高才更新高点（并列取最早的一天）；
- 收盘价相对候选高点跌幅 ≥ 门槛（含等号）→ 高点确认，开始追踪低点；只有严格创新低才更新低点（并列取最早）；
- 收盘价相对低点反弹 ≥ 门槛（含等号）→ 低点确认，记一段回调，并以当日为新的候选高点；
- 序列末尾处于下跌追踪中：记为"未确认"，低点取目前最低收盘价。
价格为 Decimal；跌幅 = 低点/高点 − 1。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

CONFIRMED, UNCONFIRMED = "已确认", "未确认"


@dataclass(frozen=True)
class Swing:
    """一段回调（未应用区间与保留期屏蔽）。"""

    level: Decimal
    high_date: dt.date
    high_close: Decimal
    low_date: dt.date
    low_close: Decimal
    confirm_date: dt.date | None       # 反弹达到门槛的日期；未确认为 None
    trading_days: int                  # 高点到低点的交易日差
    recovery_date: dt.date | None      # 低点之后首个收盘价 ≥ 高点收盘价的日期；未收复为 None

    @property
    def drawdown(self) -> Decimal:
        """跌幅（负数）。"""
        return self.low_close / self.high_close - 1

    @property
    def status(self) -> str:
        return CONFIRMED if self.confirm_date is not None else UNCONFIRMED


def zigzag(closes: Sequence[tuple[dt.date, Decimal]], level: Decimal) -> list[Swing]:
    """closes 按日期升序（交易日序列）。返回该层级的全部回调。"""
    if not closes:
        return []
    index = {d: i for i, (d, _) in enumerate(closes)}
    down_limit, up_limit = 1 - level, 1 + level
    swings: list[tuple[int, int, int | None]] = []     # (高点位置, 低点位置, 确认位置)
    hi = 0
    lo: int | None = None                              # 非 None 表示正在追踪低点
    for i in range(1, len(closes)):
        c = closes[i][1]
        if lo is None:
            if c > closes[hi][1]:
                hi = i
            elif c <= closes[hi][1] * down_limit:
                lo = i
        else:
            if c < closes[lo][1]:
                lo = i
            elif c >= closes[lo][1] * up_limit:
                swings.append((hi, lo, i))
                hi, lo = i, None
    if lo is not None:
        swings.append((hi, lo, None))

    out = []
    for h, low, conf in swings:
        high_close = closes[h][1]
        recovery = next((closes[j][0] for j in range(low + 1, len(closes)) if closes[j][1] >= high_close), None)
        out.append(Swing(level, closes[h][0], high_close, closes[low][0], closes[low][1],
                         closes[conf][0] if conf is not None else None, index[closes[low][0]] - h, recovery))
    return out
