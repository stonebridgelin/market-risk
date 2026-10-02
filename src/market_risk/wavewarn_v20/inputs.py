"""输入派生量与完整性（登记第八节第 2 小节输入表；实施口径补充第 2、8 条）。纯计算，不读写文件。

- 回看窗口按交易日轴上的行号计算；日期轴之前的日期按“价格不存在”处理。
- 门槛比较一律用乘法形式，不做除法：D ≥ θ 写作 C ≤ (1 − θ)·H；C < MA 写作 n·C < n 日收盘价之和。
- 窗口长度等参数由 InputWindows 传入，函数里不写默认值。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from market_risk.wavewarn_v20.snapshot import Snapshot


class InputError(ValueError):
    """输入不合法，或出现登记没有覆盖的输入状态。"""


class NewLow(Enum):
    """20 日新低的三值口径。未知不是“实际创新低”，报告里不得混称。"""

    YES = "1"
    NO = "0"
    UNKNOWN = "未知"


@dataclass(frozen=True)
class InputWindows:
    """回看窗口的长度（登记：63、19、15、200）。"""

    high: int            # 最高收盘价的窗口，含当日
    low_prior: int       # 新低判定所看的此前交易日数，不含当日
    low_minimum: int     # 判 NL = 0 时，此前窗口里至少要存在的价格个数
    average: int         # 均线窗口，含当日

    def __post_init__(self) -> None:
        if min(self.high, self.low_prior, self.low_minimum, self.average) <= 0:
            raise InputError("窗口长度必须为正")
        if self.low_minimum > self.low_prior:
            raise InputError("新低判定的最少个数不得超过窗口长度")


@dataclass(frozen=True)
class AssetDay:
    """单资产单日的派生量。"""

    day: dt.date
    close: Decimal | None      # C；None 即当日缺价（此时 D 缺失）
    high: Decimal | None       # H（完整时）或 Ĥ（窗口内存在价格的最大值）；窗口内没有价格时为 None
    high_complete: bool        # 窗口内的收盘价全部存在
    new_low: NewLow
    q: int                     # 距最近一次 NL = 1 或 NL 未知的交易日数；当日 NL 为 1 或未知时为 0

    @property
    def new_low_unknown(self) -> bool:
        """“NL 未知”标记：Q 因此置 0，但不是实际创新低。"""
        return self.new_low is NewLow.UNKNOWN


@dataclass(frozen=True)
class TrendDay:
    """SPX 单日的均线输入。"""

    day: dt.date
    close: Decimal | None
    total: Decimal | None      # 均线窗口内收盘价之和；不完整时不计算（None）
    complete: bool             # 均线窗口内的收盘价全部存在


def window_high(closes: Sequence[Decimal | None], index: int, length: int) -> tuple[Decimal | None, bool]:
    """（H 或 Ĥ，是否完整）。完整要求最近 length 个交易日（含当日）的收盘价全部存在。"""
    window = closes[max(0, index - length + 1):index + 1]
    present = [value for value in window if value is not None]
    return (max(present) if present else None), (len(window) == length and len(present) == length)


def new_low(closes: Sequence[Decimal | None], index: int, prior_days: int, minimum: int) -> NewLow:
    """NL 三值（实施口径补充第 8 条）。"""
    today = closes[index]
    if today is None:
        return NewLow.UNKNOWN
    prior = closes[max(0, index - prior_days):index]
    present = [value for value in prior if value is not None]
    if len(prior) == prior_days and len(present) == prior_days and today < min(present):
        return NewLow.YES
    if len(present) >= minimum and today >= min(present):
        return NewLow.NO
    return NewLow.UNKNOWN


def window_total(closes: Sequence[Decimal | None], index: int, length: int) -> tuple[Decimal | None, bool]:
    """（均线窗口内收盘价之和，是否完整）；不完整时不计算。"""
    window = closes[max(0, index - length + 1):index + 1]
    if len(window) != length or any(value is None for value in window):
        return None, False
    return sum((value for value in window if value is not None), Decimal(0)), True


def asset_days(days: Sequence[dt.date], closes: Sequence[Decimal | None], windows: InputWindows
               ) -> tuple[AssetDay, ...]:
    """单资产的逐日派生量，按全部可得历史计算（不从 t0 开始计数）。"""
    if len(days) != len(closes):
        raise InputError("日期轴与收盘价序列长度不一致")
    result: list[AssetDay] = []
    q = 0
    for index, day in enumerate(days):
        high, complete = window_high(closes, index, windows.high)
        low = new_low(closes, index, windows.low_prior, windows.low_minimum)
        q = q + 1 if low is NewLow.NO else 0
        result.append(AssetDay(day, closes[index], high, complete, low, q))
    return tuple(result)


def trend_days(days: Sequence[dt.date], closes: Sequence[Decimal | None], windows: InputWindows
               ) -> tuple[TrendDay, ...]:
    """SPX 的逐日均线输入。"""
    if len(days) != len(closes):
        raise InputError("日期轴与收盘价序列长度不一致")
    result: list[TrendDay] = []
    for index, day in enumerate(days):
        total, complete = window_total(closes, index, windows.average)
        result.append(TrendDay(day, closes[index], total, complete))
    return tuple(result)


def snapshot_asset_days(snapshot: Snapshot, asset: str, windows: InputWindows) -> tuple[AssetDay, ...]:
    return asset_days(snapshot.days, snapshot.series(asset), windows)


def snapshot_trend_days(snapshot: Snapshot, asset: str, windows: InputWindows) -> tuple[TrendDay, ...]:
    return trend_days(snapshot.days, snapshot.series(asset), windows)


def drawdown_reaches(close: Decimal, high: Decimal, threshold: Decimal) -> bool:
    """D ≥ θ 的乘法形式：C ≤ (1 − θ)·H。用 Ĥ 时即 D̂ ≥ θ（Ĥ ≤ 真实 H，所以真实 D 也 ≥ θ）。"""
    return close <= (1 - threshold) * high


def below_average(close: Decimal, total: Decimal, length: int) -> bool:
    """C < MA 的乘法形式：length·C < length 日收盘价之和。"""
    return length * close < total
