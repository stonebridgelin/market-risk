"""v2.0 输入派生量与完整性（登记第八节第 2 小节输入表；实施口径补充第 2、8 条）：只用构造数据。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from wavewarn_v20_helpers import WINDOWS, axis, dec, random_closes

from market_risk.wavewarn_v20.inputs import (
    InputError,
    InputWindows,
    NewLow,
    asset_days,
    below_average,
    drawdown_reaches,
    new_low,
    trend_days,
    window_high,
)

WARMUP = 260        # “第 n 日”对应行号 WARMUP + n − 1；此前的预热段价格齐全


def flat(length: int, missing: tuple[int, ...] = ()) -> list[Decimal | None]:
    return [None if index in missing else Decimal("100") for index in range(length)]


def row(day_number: int) -> int:
    return WARMUP + day_number - 1


def test_day_32_gap_leaves_high_window_on_day_95_and_average_window_on_day_232() -> None:
    """登记第一节第 6 小节例子乙：第 32 日缺价，H 自第 95 日、MA 自第 232 日恢复完整。"""
    length = WARMUP + 240
    closes = flat(length, (row(32),))
    days = axis(length)
    assets, averages = asset_days(days, closes, WINDOWS), trend_days(days, closes, WINDOWS)
    assert assets[row(31)].high_complete and averages[row(31)].complete
    assert not any(assets[row(n)].high_complete for n in range(32, 95))
    assert all(assets[row(n)].high_complete for n in range(95, 241))
    assert not any(averages[row(n)].complete for n in range(32, 232))
    assert all(averages[row(n)].complete for n in range(232, 241))
    assert averages[row(232)].total == 200 * Decimal("100") and averages[row(231)].total is None


def test_dates_before_the_axis_count_as_missing_prices() -> None:
    days, closes = axis(210), flat(210)
    assets, averages = asset_days(days, closes, WINDOWS), trend_days(days, closes, WINDOWS)
    # 第 63 个收盘价（行号 62）起 H 完整；第 200 个收盘价（行号 199）起均线完整。
    assert [item.high_complete for item in assets[60:64]] == [False, False, True, True]
    assert [item.complete for item in averages[197:201]] == [False, False, True, True]
    # 此前 19 日里不足 15 个价格时 NL 未知；满 15 个且今日不低于其最小值时为 0。
    assert [item.new_low for item in assets[13:17]] == [NewLow.UNKNOWN, NewLow.UNKNOWN, NewLow.NO, NewLow.NO]


def test_missing_close_today_means_no_drawdown_unknown_new_low_and_zero_q() -> None:
    length = 120
    closes = flat(length, (100,))
    assets = asset_days(axis(length), closes, WINDOWS)
    today = assets[100]
    assert today.close is None and not today.high_complete
    assert today.new_low is NewLow.UNKNOWN and today.q == 0
    assert today.high == Decimal("100")              # Ĥ 仍可由窗口内存在的价格给出，但当日 D 缺失
    # 缺价前 Q 逐日加 1；缺价当日清零一次；次日此前 19 日里有 18 个价格、今日不低于其最小值，NL = 0，Q 重新从 1 数起。
    assert assets[99].q == assets[98].q + 1 > 5
    assert [item.q for item in assets[100:104]] == [0, 1, 2, 3]


def test_unknown_new_low_is_flagged_and_never_reported_as_an_actual_low() -> None:
    assets = asset_days(axis(120), flat(120, (100,)), WINDOWS)
    assert assets[100].new_low_unknown and assets[100].new_low is not NewLow.YES
    assert not assets[101].new_low_unknown and assets[101].new_low is NewLow.NO


def test_gap_outside_low_window_but_inside_high_window() -> None:
    """登记例子 A、E 的输入：缺价日已离开 20 日窗口、仍在 63 日窗口内。"""
    length = 230
    assets = asset_days(axis(length), flat(length, (150,)), WINDOWS)
    for offset in (30, 40):
        today = assets[150 + offset]
        assert today.new_low is NewLow.NO and today.q >= 5 and not today.high_complete
    assert assets[150 + 63].high_complete


@pytest.mark.parametrize(("present", "today", "expected"), [
    (19, "99.99", NewLow.YES),        # 此前 19 日全部存在，今日严格低于其最小值
    (19, "100", NewLow.NO),           # 今日等于最小值：不是新低
    (19, "100.01", NewLow.NO),
    (18, "99.99", NewLow.UNKNOWN),    # 今日低于最小值，但此前 19 日不全：不能判 1，也不能判 0
    (15, "99.99", NewLow.UNKNOWN),
    (15, "100", NewLow.NO),           # 恰好 15 个，今日不低于其最小值
    (14, "100", NewLow.UNKNOWN),      # 恰好 14 个：个数不足，一律未知
    (14, "150", NewLow.UNKNOWN),
    (14, "99.99", NewLow.UNKNOWN),
])
def test_new_low_three_values(present: int, today: str, expected: NewLow) -> None:
    """实施口径补充第 8 条：只数此前 19 日里存在的价格。"""
    prior: list[Decimal | None] = [Decimal("100")] * present + [None] * (19 - present)
    closes = [*prior, dec(today)]
    assert new_low(closes, 19, WINDOWS.low_prior, WINDOWS.low_minimum) is expected


def test_new_low_is_unknown_when_today_is_missing_even_with_full_window() -> None:
    closes: list[Decimal | None] = [*[Decimal("100")] * 19, None]
    assert new_low(closes, 19, WINDOWS.low_prior, WINDOWS.low_minimum) is NewLow.UNKNOWN


def test_new_low_uses_the_minimum_of_existing_prior_prices_only() -> None:
    # 此前 19 日里存在 16 个，最小值 98；今日 98 不低于它 → 0；今日 97.99 → 未知（窗口不全，不能证明创新低）。
    prior: list[Decimal | None] = [Decimal("98"), None, None, None, *[Decimal("101")] * 15]
    assert new_low([*prior, Decimal("98")], 19, 19, 15) is NewLow.NO
    assert new_low([*prior, Decimal("97.99")], 19, 19, 15) is NewLow.UNKNOWN


def test_q_counts_trading_days_since_last_low_or_unknown() -> None:
    closes: list[Decimal | None] = [*[Decimal("100")] * 30, Decimal("99"), *[Decimal("100")] * 4, Decimal("98.5")]
    assets = asset_days(axis(len(closes)), closes, WINDOWS)
    assert [item.new_low for item in assets[30:36]] == [NewLow.YES, NewLow.NO, NewLow.NO, NewLow.NO, NewLow.NO,
                                                        NewLow.YES]
    assert [item.q for item in assets[30:36]] == [0, 1, 2, 3, 4, 0]


def test_partial_high_never_exceeds_true_high() -> None:
    """Ĥ ≤ H，所以 D̂ ≤ D：用不完整窗口判定“达到门槛”时，真实回撤一定也达到。"""
    length = 400
    full = random_closes(21, length)
    gapped = [None if index % 17 == 5 or index % 29 == 3 else value for index, value in enumerate(full)]
    partial_seen = 0
    for index in range(length):
        true_high, _ = window_high(full, index, WINDOWS.high)
        partial_high, complete = window_high(gapped, index, WINDOWS.high)
        close = gapped[index]
        if close is None or partial_high is None or true_high is None:
            continue
        partial_seen += not complete
        assert partial_high <= true_high
        assert 1 - close / partial_high <= 1 - close / true_high
        for threshold in (Decimal("0.015"), Decimal("0.02"), Decimal("0.025"), Decimal("0.05")):
            if drawdown_reaches(close, partial_high, threshold):
                assert drawdown_reaches(close, true_high, threshold)
    assert partial_seen > 100


def test_drawdown_threshold_is_compared_by_multiplication_with_equality_included() -> None:
    # θ = 2.5%，H = 100：C = 97.50 恰好等于 (1 − θ)·H，达到门槛；97.51 未达到。
    assert drawdown_reaches(Decimal("97.50"), Decimal("100"), Decimal("0.025"))
    assert not drawdown_reaches(Decimal("97.51"), Decimal("100"), Decimal("0.025"))
    # θ = 1.96%，H = 1500：(1 − θ)·H = 1470.60，乘法形式下边界是精确的。
    assert drawdown_reaches(Decimal("1470.60"), Decimal("1500.00"), Decimal("0.0196"))
    assert not drawdown_reaches(Decimal("1470.61"), Decimal("1500.00"), Decimal("0.0196"))


def test_average_is_compared_by_multiplication_and_equality_is_not_below() -> None:
    total = 200 * Decimal("100")
    assert below_average(Decimal("99.99"), total, 200)
    assert not below_average(Decimal("100"), total, 200)        # C 恰好等于 MA：不算低于
    assert not below_average(Decimal("100.01"), total, 200)
    # 均线不是有限小数时也精确：199 个 100 加一个 100.01，MA = 100.00005。
    uneven = 199 * Decimal("100") + Decimal("100.01")
    assert below_average(Decimal("100"), uneven, 200) and not below_average(Decimal("100.01"), uneven, 200)


def test_window_parameters_are_validated_and_required() -> None:
    with pytest.raises(InputError):
        InputWindows(high=63, low_prior=19, low_minimum=20, average=200)
    with pytest.raises(InputError):
        InputWindows(high=0, low_prior=19, low_minimum=15, average=200)
    with pytest.raises(TypeError):
        InputWindows()                                         # type: ignore[call-arg]
    with pytest.raises(InputError, match="长度不一致"):
        asset_days(axis(3), [Decimal("1")], WINDOWS)
