"""v2.0 的 R2 回调事件（5%/5%）（产品规格第八节第 3 条、例子三）：只用构造数据。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from wavewarn_v20_helpers import numbered, random_closes

from market_risk.wavewarn_v20.labels_r2 import LabelError, R2Event, R2Thresholds, r2_events

THRESHOLDS = R2Thresholds(confirm=Decimal("0.95"), finish=Decimal("1.05"), early=Decimal("0.97"))


def rows(prices: list[str | None], first: int = 1) -> list:
    """第 first 日起逐日的（交易日，收盘价）。"""
    return [(numbered(first + offset), None if price is None else Decimal(price))
            for offset, price in enumerate(prices)]


def events(prices: list[str | None], first: int = 1, cutoff: int | None = None) -> tuple[R2Event, ...]:
    last = first + len(prices) - 1 if cutoff is None else cutoff
    return r2_events("SPX", rows(prices, first), numbered(last), THRESHOLDS)


def test_product_example_three() -> None:
    """规格例子三：P 为第 100 日（100.0）。

    第 103 日 96.8 第一次不高于 97.0（T3）；第 105 日 94.9 第一次不高于 95.0（T5）。
    """
    # 第 95 至 105 日
    prices = ["99.0", "99.2", "99.4", "99.6", "99.8", "100.0", "98.5", "97.6", "96.8", "96.0", "94.9"]
    (event,) = events(prices, first=95)
    assert (event.peak, event.peak_close) == (numbered(100), Decimal("100.0"))
    assert (event.t3, event.t5) == (numbered(103), numbered(105))
    assert (event.trough, event.trough_close) == (numbered(105), Decimal("94.9"))
    assert (event.end, event.unfinished) == (None, True)
    assert event.asset == "SPX"


def test_one_day_gap_through_three_percent_is_t3_that_day() -> None:
    """复核者的例子：第 101 日收盘 96.0（回撤 4%），一日越过 3%，该日即为 T3；之后反弹到 98.0，再跌到 95.0 确认。"""
    (event,) = events(["99.0", "100.0", "96.0", "98.0", "95.0"], first=99)
    assert (event.peak, event.t3, event.t5) == (numbered(100), numbered(101), numbered(103))


def test_threshold_equalities_count() -> None:
    # 97.0 恰好等于 0.97 × 100：算到达 T3；95.0 恰好等于 0.95 × 100：算确认；99.75 恰好等于 1.05 × 95：算结束。
    (event,) = events(["100", "97.0", "95.0", "99.74", "99.75"])
    assert (event.t3, event.t5, event.end) == (numbered(2), numbered(3), numbered(5))
    # 97.01 高于 97.0：还不是 T3；95.01 高于 95.0：还不是 T5；99.74 低于 99.75：没有结束。
    (event,) = events(["100", "97.01", "95.01", "95.0", "99.74"])
    assert (event.t3, event.t5, event.end, event.unfinished) == (numbered(3), numbered(4), None, True)


def test_equal_highs_take_the_last_date_as_peak() -> None:
    (event,) = events(["100", "98", "100", "99", "100", "97", "94"])
    assert event.peak == numbered(5)                      # 三次 100，取最后一次
    assert (event.t3, event.t5) == (numbered(6), numbered(7))


def test_equal_lows_take_the_last_date_as_trough() -> None:
    (event,) = events(["100", "94", "93", "95", "93", "96", "97.65"])     # 1.05 × 93 = 97.65
    assert (event.trough, event.trough_close) == (numbered(5), Decimal("93"))   # 两次 93，取最后一次
    assert (event.end, event.unfinished) == (numbered(7), False)


def test_next_round_high_includes_the_end_day() -> None:
    """下一轮从 Tr 的下一日开始寻峰，H 含 End 当日的收盘价。

    第一事件：P = 第 1 日（100），Tr = 第 3 日（90），End = 第 5 日（94.6 ≥ 1.05 × 90 = 94.5）。
    之后第 6 日 93、第 7 日 89.8。H 含 End（94.6）时，89.8 ≤ 0.95 × 94.6 = 89.87，第 7 日确认第二个事件，P = 第 5 日；
    若 H 不含 End、从第 6 日（93）起算，0.95 × 93 = 88.35，第 7 日不会确认。
    """
    first, second = events(["100", "95", "90", "92", "94.6", "93", "89.8"])
    assert (first.peak, first.trough, first.end) == (numbered(1), numbered(3), numbered(5))
    assert (second.peak, second.peak_close) == (numbered(5), Decimal("94.6"))
    assert (second.t3, second.t5, second.unfinished) == (numbered(7), numbered(7), True)


def test_unfinished_event_and_unconfirmed_tail() -> None:
    # 已确认、截止日时未结束：End 为空，Tr 取截至截止日的最低收盘价。
    (event,) = events(["100", "94", "92", "93", "91", "92"])
    assert (event.end, event.unfinished, event.trough, event.trough_close) == (None, True, numbered(5), Decimal("91"))
    # 截止日时尚未确认（只跌了 4%）：不构成事件。
    assert events(["100", "98", "96", "97"]) == ()
    # 事件结束之后的新一段只跌了 3%：只有前一个事件。
    assert len(events(["100", "94", "99", "101", "98"])) == 1


def test_rows_after_cutoff_and_missing_or_disordered_rows_are_rejected() -> None:
    with pytest.raises(LabelError, match="截止日"):
        events(["100", "99", "98"], cutoff=2)
    with pytest.raises(LabelError, match="缺收盘价"):
        events(["100", None, "94"])
    data = rows(["100", "99", "98"])
    with pytest.raises(LabelError, match="重复或乱序"):
        r2_events("SPX", [data[0], data[0], data[1]], numbered(3), THRESHOLDS)
    with pytest.raises(LabelError, match="重复或乱序"):
        r2_events("SPX", [data[1], data[0]], numbered(3), THRESHOLDS)
    with pytest.raises(LabelError, match="Decimal"):
        r2_events("SPX", [(numbered(1), 100.0)], numbered(3), THRESHOLDS)     # type: ignore[list-item]


def test_cutoff_invariance_on_random_paths() -> None:
    """截止日不变性：用不同截止日分别生成，较早截止日以前已经结束的事件完全相同。"""
    checked = 0
    for seed in (91, 92, 93):
        closes = random_closes(seed, 1500)
        data = [(numbered(index), close) for index, close in enumerate(closes)]
        full = r2_events("QQQ", data, numbered(1499), THRESHOLDS)
        assert len(full) >= 5
        for cut in (400, 777, 1100):
            early = r2_events("QQQ", data[:cut + 1], numbered(cut), THRESHOLDS)
            finished = [event for event in early if not event.unfinished]
            assert list(full[:len(finished)]) == finished
            checked += len(finished)
            for event in early[len(finished):]:        # 至多一个未结束事件：高点、T3、T5 与完整序列里的同一事件相同
                later = full[len(finished)]
                assert (event.peak, event.peak_close, event.t3, event.t5) == (later.peak, later.peak_close,
                                                                               later.t3, later.t5)
            assert sum(event.unfinished for event in early) <= 1
        for event in full:
            assert event.peak < event.t3 <= event.t5 <= event.trough        # 每个事件都有 T3，且不晚于 T5
            assert event.end is None or event.trough < event.end
    assert checked >= 10


def test_thresholds_must_be_positive_finite_decimals() -> None:
    """补修：事件门槛是参数类输入，非有限、非正或不是 Decimal 时抛异常。"""
    R2Thresholds(Decimal("0.95"), Decimal("1.05"), Decimal("0.97"))
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("0"), Decimal("-0.95"), 0.95):
        for position in range(3):
            values = [Decimal("0.95"), Decimal("1.05"), Decimal("0.97")]
            values[position] = bad
            with pytest.raises(LabelError, match="门槛"):
                R2Thresholds(*values)
