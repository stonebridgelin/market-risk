"""R2 回调事件（5%/5%）（产品规格第八节第 3 条；登记第三节；实施口径补充第 2、17 条）。纯计算。

前提：输入已通过交易日完整性校验。本函数只检查传入各行的日期顺序、重复日期与空价格；
某个交易日整行缺失（根本不在序列里）在这里看不出来，由数据入口对照交易日轴检测，正式入口不得绕过那一步。

每个资产从其最早的收盘价开始逐日处理，窗口之前的部分只作预热；比较一律用 Decimal 的乘法形式。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


class LabelError(ValueError):
    """输入不合法，或出现规格没有覆盖的情形。"""


@dataclass(frozen=True)
class R2Thresholds:
    """事件门槛的乘数（规格：确认 0.95、结束 1.05、T3 为 0.97）。"""

    confirm: Decimal     # 确认：C ≤ confirm × H
    finish: Decimal      # 结束：C ≥ finish × L
    early: Decimal       # T3：C ≤ early × C_P


@dataclass(frozen=True)
class R2Event:
    asset: str
    peak: dt.date                  # P：事件高点
    peak_close: Decimal            # C_P
    t3: dt.date                    # P 之后第一个 C ≤ 0.97 × C_P 的交易日
    t5: dt.date                    # 确认日
    trough: dt.date                # Tr：低点；未结束事件为截至截止日的最低收盘价所在日
    trough_close: Decimal          # C_Tr
    end: dt.date | None            # End；未结束时为 None
    unfinished: bool


def _checked(rows: Sequence[tuple[dt.date, Decimal | None]], cutoff: dt.date) -> list[tuple[dt.date, Decimal]]:
    result: list[tuple[dt.date, Decimal]] = []
    for day, close in rows:
        if day > cutoff:
            raise LabelError(f"出现了截止日 {cutoff} 之后的行：{day}")
        if result and day <= result[-1][0]:
            raise LabelError(f"交易日重复或乱序：{result[-1][0]} 之后是 {day}")
        if close is None:
            raise LabelError(f"{day} 缺收盘价：不跳过，也不插补")
        if not isinstance(close, Decimal) or not close.is_finite() or close <= 0:
            raise LabelError(f"{day} 的收盘价必须是正的有限 Decimal")
        result.append((day, close))
    return result


def r2_events(asset: str, rows: Sequence[tuple[dt.date, Decimal | None]], cutoff: dt.date,
              thresholds: R2Thresholds) -> tuple[R2Event, ...]:
    """按截止日生成某资产的全部 R2 回调事件。rows 为按日升序的（交易日，收盘价）。

    寻峰：H 取最高收盘价，相同的最高价取最后一次出现的日期作为 P。
    确认：收盘价 C ≤ confirm × H 的那一日为 T5。
    寻底：自 P 起取最低收盘价 L，相同的最低价取最后一次出现的日期作为 Tr。
    结束：C ≥ finish × L 的那一日为 End。
    下一轮：从 Tr 的下一个交易日开始寻峰，H 取 Tr 之后（含 End）的最高收盘价，并继续更新。
    截止日时已确认但未结束的事件 End 为空、标“未结束”；截止日时尚未确认的期间不构成事件。
    """
    data = _checked(rows, cutoff)
    events: list[R2Event] = []
    start = 0
    while start < len(data):
        high: Decimal | None = None
        peak = confirmed = -1
        for index in range(start, len(data)):
            close = data[index][1]
            if high is None or close >= high:
                high, peak = close, index
            elif close <= thresholds.confirm * high:
                confirmed = index
                break
        if confirmed < 0 or high is None:
            break
        early = next((index for index in range(peak + 1, len(data)) if data[index][1] <= thresholds.early * high), -1)
        if early < 0 or early > confirmed:
            raise LabelError(f"{asset} 高点 {data[peak][0]} 的事件：T3 晚于 T5 或不存在")
        low, trough = high, peak
        for index in range(peak, confirmed + 1):
            if data[index][1] <= low:
                low, trough = data[index][1], index
        finished = -1
        for index in range(confirmed + 1, len(data)):
            close = data[index][1]
            if close <= low:
                low, trough = close, index
            elif close >= thresholds.finish * low:
                finished = index
                break
        events.append(R2Event(asset, data[peak][0], high, data[early][0], data[confirmed][0], data[trough][0], low,
                              data[finished][0] if finished >= 0 else None, finished < 0))
        if finished < 0:
            break
        start = trough + 1
    return tuple(events)
