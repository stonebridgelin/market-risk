"""次日执行及价格缺口以手算路径核对。"""

import datetime as dt
from decimal import Decimal

from market_risk.wavewarn.execution import execute_asset, execution_intervals


def test_next_close_execution_and_t0_minus_one_signal_timing() -> None:
    days = [dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(4)]
    closes = [Decimal("100"), Decimal("99"), Decimal("98"), Decimal("97")]
    rows = execute_asset(days, ["绿", "红", "红", "黄"], closes, Decimal("0.5"))
    # 第1天收盘后红灯，第2天收盘才执行红；第1→2天区间仍按绿灯满暴露。
    assert [row.executed for row in rows] == ["绿", "绿", "红", "红"]
    assert [row.exposure for row in execution_intervals(days, closes, rows)] == [Decimal(1), Decimal(1), Decimal(0)]
    assert [row.switched for row in rows] == [False, False, True, False]


def test_missing_price_delays_execution_and_uses_latest_signal() -> None:
    days = [dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(5)]
    closes = [Decimal("100"), None, None, Decimal("95"), Decimal("96")]
    rows = execute_asset(days, ["绿", "红", "黄", "绿", "绿"], closes, Decimal("0.5"))
    # 第1、2天不能执行；第3天恢复时只执行第2天最新黄灯，不补执行第1天红灯。
    assert [row.executed for row in rows] == ["绿", "绿", "绿", "黄", "绿"]
    intervals = execution_intervals(days, closes, rows)
    assert [row.excluded for row in intervals] == [True, True, True, False]
    # 系统切换按目标信号计，即使缺价日也计数；资产实际执行另记。
    assert [row.switched for row in rows] == [False, False, True, True, True]
