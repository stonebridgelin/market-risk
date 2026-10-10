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


def test_label_errors_are_classified_into_three_reasons() -> None:
    """补修 Q5：六种情形各自归入正确的类别（按子类与 reason 属性判别，不解析报错文字）。"""
    from market_risk.wavewarn_v20 import labels_r2

    data = rows(["100", "99", "98"])
    cases = [
        (lambda: events(["100", "99", "98"], cutoff=2), labels_r2.LabelInputError),                  # 截止日之后的行
        (lambda: r2_events("SPX", [data[1], data[0]], numbered(3), THRESHOLDS), labels_r2.LabelInputError),  # 乱序
        (lambda: events(["100", None, "94"]), labels_r2.MissingPriceError),                         # 收盘价为空
        (lambda: r2_events("SPX", [(numbered(1), Decimal("-1"))], numbered(3), THRESHOLDS),
         labels_r2.LabelInputError),                                                                  # 价格非正
        (lambda: R2Thresholds(Decimal("0.95"), Decimal("1.05"), Decimal("NaN")), labels_r2.LabelInputError),  # 门槛
        (lambda: r2_events("SPX", rows(["100", "94"]), numbered(2),
                           R2Thresholds(Decimal("0.95"), Decimal("1.05"), Decimal("0.90"))),
         labels_r2.UnexpectedLabelError),                                                             # T3 晚于 T5
    ]
    expected_reasons = {labels_r2.LabelInputError: "输入校验失败", labels_r2.MissingPriceError: "缺少必需价格",
                        labels_r2.UnexpectedLabelError: "未预期异常"}
    for action, kind in cases:
        with pytest.raises(LabelError) as caught:
            action()
        assert type(caught.value) is kind and caught.value.reason == expected_reasons[kind]


# ---------------------------------------------------------------------------
# 修正批 K5（登记补充第 3 条；复查附件 G3 第三节 T1）：确认前的反弹不视为事件结束，也不重置寻峰
# ---------------------------------------------------------------------------


def test_rebound_before_confirmation_does_not_restart_the_peak_search() -> None:
    """收盘价 [100, 95.10, 99.90, 94.90]，截止日为第 4 日（登记补充第 3.3 条）。

    手推：H 自第 1 日为 100；第 2 日 95.10 > 0.95 × 100 = 95.00，未确认，但 95.10 ≤ 0.97 × 100 = 97.00；
    第 3 日 99.90 < 100，不更新 H（只在出现不低于 H 的收盘价时更新），反弹不结束寻峰；
    第 4 日 94.90 ≤ 95.00，确认：P = 第 1 日；T3 = P 之后第一个 ≤ 97.00 的日子 = 第 2 日；T5 = 第 4 日；
    确认后寻底的初始低点为 [P, T5] 内最低收盘价 = 第 4 日 94.90（第 3.2 条），截止日即第 4 日，Tr = 第 4 日、未结束。
    """
    (event,) = events(["100", "95.10", "99.90", "94.90"], cutoff=4)
    assert (event.peak, event.peak_close) == (numbered(1), Decimal("100"))
    assert (event.t3, event.t5) == (numbered(2), numbered(4))
    assert (event.trough, event.trough_close) == (numbered(4), Decimal("94.90"))
    assert (event.end, event.unfinished) == (None, True)


def test_rebound_to_a_new_high_before_confirmation_moves_the_peak() -> None:
    """确认前反弹并创新高：[100, 96.00, 101.00, 95.90]。

    手推：第 2 日 96.00 > 95.00，未确认；第 3 日 101.00 ≥ H = 100，H 更新为 101.00（P 移到第 3 日）；
    第 4 日 95.90 ≤ 0.95 × 101.00 = 95.95，确认：P = 第 3 日；T3 = 第 3 日之后第一个 ≤ 0.97 × 101.00 = 97.97 的
    日子 = 第 4 日；T5 = 第 4 日；Tr = 第 4 日 95.90、未结束。第 2 日的 96.00 不是 T3（它在新 P 之前）。
    """
    (event,) = events(["100", "96.00", "101.00", "95.90"], cutoff=4)
    assert (event.peak, event.peak_close) == (numbered(3), Decimal("101.00"))
    assert (event.t3, event.t5) == (numbered(4), numbered(4))
    assert (event.trough, event.trough_close, event.end, event.unfinished) == (
        numbered(4), Decimal("95.90"), None, True)
