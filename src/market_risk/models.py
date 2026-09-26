"""核心数据模型（SPEC 第4节）。

约定：
- 所有百分比统一用"百分数"表示（4.11 表示 4.11%）；计算 bp 时乘以 100。
- 数据类优先 frozen=True；序列字段使用 tuple，保证不可变。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class DateReferences:
    """一个基准日的全部日期参照（SPEC 5.2 / SOP 第6节）。"""

    base_date: dt.date
    t_minus_5: dt.date
    t_minus_20: dt.date
    window_start: dt.date                      # T−19
    window_end: dt.date                        # T
    window_days: tuple[dt.date, ...]           # 20日窗口内的全部股票交易日
    oas_o1: dt.date                            # 基准日之前最近一个债市营业日
    oas_o6_v3r1: dt.date                       # O1 之前第5个债市营业日
    oas_o1_v2m: dt.date | None                 # 基准日之前最新的有数值观测；无列表时为 None
    oas_o6_v2m: dt.date | None                 # 从 v2-M 的 O1 往前数第5个有数值观测
    oas_o1_to_o6_sequence: tuple[dt.date, ...]  # 从 O1 往前的债市营业日：O1, O1−1, …, O6
    o1_lag_stock_days: int                     # O1 之后（不含）至基准日（含）的股票交易日个数
    three_segment_query_start: dt.date         # T−45
    outcome_window_start: dt.date              # 基准日之后第1个股票交易日
    outcome_window_end: dt.date                # 基准日之后第20个股票交易日
    stock_holidays_in_window: tuple[dt.date, ...]
    bond_holidays_in_window: tuple[dt.date, ...]
    is_early_close: bool
    is_last_trading_day_of_week: bool
