"""v2.0 的 R2 判定、比例与提示段账（产品规格第八节第 4、5 条；实施口径补充第 10、11、14、15 条）：只用构造数据。

构造方式：交易日为第 0、1、2…… 日；提示状态用一串字符给出，Y = 提示，N = 非提示，? = 无法确定；
字符串的第一个字符对应窗口第一个信号日 f 的前一日。
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from wavewarn_v20_helpers import numbered

from market_risk.wavewarn_v20.labels_r2 import R2Event
from market_risk.wavewarn_v20.r2 import (
    EventClass,
    Prompt,
    R2Error,
    R2Rule,
    SegmentClass,
    Window,
    judge_event,
    prompt_segments,
    r2_result,
    segment_ledger,
)

RULE = R2Rule(numerator=3, denominator=5, observation=20)
SYMBOLS = {"Y": Prompt.YES, "N": Prompt.NO, "?": Prompt.UNKNOWN}


def window(states: str, before_first: int = 0, extra_days: int = 0) -> Window:
    """states[0] 是第 before_first 日（即 f − 1）的状态；f = before_first + 1；E = 最后一个字符对应的日子。"""
    last = before_first + len(states) - 1
    days = tuple(numbered(number) for number in range(0, last + 1 + extra_days))
    status = {numbered(before_first + offset): SYMBOLS[symbol] for offset, symbol in enumerate(states)}
    return Window(days, numbered(before_first + 1), numbered(last), status)


def marked(length: int, prompts: dict[int, str], before_first: int = 0) -> Window:
    """全部为 N、只在指定日子改写的状态串；prompts 的键是日序号。"""
    symbols = ["N"] * length
    for number, symbol in prompts.items():
        symbols[number - before_first] = symbol
    return window("".join(symbols), before_first)


def event(peak: int, t3: int, t5: int, trough: int, end: int | None, asset: str = "SPX") -> R2Event:
    return R2Event(asset, numbered(peak), Decimal("100"), numbered(t3), numbered(t5), numbered(trough),
                   Decimal("94"), None if end is None else numbered(end), end is None)


EXAMPLE = event(100, 103, 105, 106, 110)        # 例子三：P = 100，T3 = 103，T5 = 105


def span(first: int, last: int, symbol: str = "Y") -> dict[int, str]:
    return dict.fromkeys(range(first, last + 1), symbol)


# ---------------------------------------------------------------------------
# 事件判定：例子三、五类、边界
# ---------------------------------------------------------------------------


def test_product_example_three_prompt_starting_on_day_102_is_a_timely_new_prompt() -> None:
    judged = judge_event(EXAMPLE, marked(131, span(102, 120)))
    assert judged.category is EventClass.NEW                                  # 102 < 103
    assert (judged.first_new_day, judged.executable_day, judged.executable_offset) == (numbered(102), numbered(103), 0)


def test_product_example_three_prompt_starting_on_day_103_is_late() -> None:
    assert judge_event(EXAMPLE, marked(131, span(103, 120))).category is EventClass.LATE   # T3 当日的提示不算提前


def test_product_example_three_prompt_held_from_day_97_to_102_is_covered() -> None:
    assert judge_event(EXAMPLE, marked(131, span(97, 102))).category is EventClass.COVERED


def test_reviewer_example_t3_on_day_101_prompt_from_day_102_is_late() -> None:
    """复核者的例子：第 101 日一日越过 3%，T3 = 101；在信号日 102 才开始提示，仍判为迟到。"""
    assert judge_event(event(100, 101, 105, 106, 110), marked(131, span(102, 120))).category is EventClass.LATE


@pytest.mark.parametrize(("prompts", "expected"), [
    (span(99, 102), EventClass.COVERED),                         # S_P 为提示，且 [P, T3) 每日都为提示
    ({101: "Y"}, EventClass.NEW),                                # [P, T3) 内 S_d 为提示、S_{d−1} 为非提示
    ({99: "Y", 100: "Y"}, EventClass.INTERRUPTED),               # S_P 为提示（延续自第 99 日），T3 之前解除，没有再开始
    ({99: "Y", 100: "Y", 102: "Y"}, EventClass.NEW),             # 解除后在 T3 之前又新开始
    ({104: "Y"}, EventClass.LATE),                               # [P, T3) 无提示，[T3, Tr] 内出现提示
    ({106: "Y"}, EventClass.LATE),                               # Tr 当日的提示也在 [T3, Tr] 内
    ({107: "Y"}, EventClass.MISSED),                             # 提示在 Tr 之后才出现
    ({}, EventClass.MISSED),                                     # [P, Tr] 内都没有提示
])
def test_five_classes(prompts: dict[int, str], expected: EventClass) -> None:
    assert judge_event(EXAMPLE, marked(131, prompts)).category is expected


def test_prompt_newly_started_on_the_peak_day_then_released_is_a_timely_new_prompt() -> None:
    """实施口径补充第 15 条：S_P 为提示、S_{P−1} 为非提示，之后在 T3 前解除且没有再开始 → 新提示达标。

    这表示曾及时产生新提示，不表示持续覆盖，也不表示已完成保护。
    """
    judged = judge_event(EXAMPLE, marked(131, {100: "Y"}))
    assert judged.category is EventClass.NEW and judged.category is not EventClass.COVERED
    assert judged.peak_new_uncertain is False              # S_{P−1} 为非提示：P 当日确实是新提示
    assert (judged.first_new_day, judged.executable_day, judged.executable_offset) == (numbered(100), numbered(101), -2)


def test_left_truncation_boundary() -> None:
    """P = f 时为左截断，P 为 f 的下一日时不是。f 为第 1 日。"""
    states = marked(60, span(0, 59))
    assert states.first == numbered(1)
    assert judge_event(event(1, 3, 5, 6, 9), states).category is EventClass.LEFT_TRUNCATED
    assert judge_event(event(0, 3, 5, 6, 9), states).category is EventClass.LEFT_TRUNCATED
    assert judge_event(event(2, 4, 5, 6, 9), states).category is EventClass.COVERED


def test_unknown_state_before_t3_is_insufficient_input() -> None:
    for day in (100, 101, 102):
        assert judge_event(EXAMPLE, marked(131, {day: "?", 99: "Y"})).category is EventClass.INSUFFICIENT
    # 第 103 日（T3 当日）不在 [P, T3) 内：不是输入不足。
    assert judge_event(EXAMPLE, marked(131, {103: "?", 101: "Y"})).category is EventClass.NEW


def test_unknown_state_that_affects_classification_raises() -> None:
    with pytest.raises(R2Error, match="迟到"):
        judge_event(EXAMPLE, marked(131, {104: "?"}))                    # [T3, Tr] 内无法确定，分不清迟到与漏报
    # 已有确定的提示：不受影响。
    assert judge_event(EXAMPLE, marked(131, {104: "?", 105: "Y"})).category is EventClass.LATE
    assert judge_event(EXAMPLE, marked(131, {104: "?", 101: "Y"})).category is EventClass.NEW    # 分类在 T3 之前已定
    with pytest.raises(R2Error, match="前一日"):
        judge_event(EXAMPLE, marked(131, {99: "?", 100: "Y"}))           # 需要判断新提示的前一日无法确定
    # 前一日无法确定，但之后在 T3 之前另有确定的新提示：分类不受影响。
    assert judge_event(EXAMPLE, marked(131, {99: "?", 100: "Y", 102: "Y"})).category is EventClass.NEW


def test_five_classes_are_exclusive_and_exhaustive_on_random_sequences() -> None:
    """性质：每个可评价事件恰好满足规格表中五类条件里的一类，且与判定结果一致。"""
    rng = random.Random(7)
    seen: set[EventClass] = set()
    for _ in range(400):
        symbols = "".join(rng.choice("YYNNN") if rng.random() < 0.5 else rng.choice("YN") for _ in range(60))
        states = window(symbols)
        peak = rng.randint(2, 40)
        t3 = peak + rng.randint(1, 4)
        t5 = t3 + rng.randint(0, 3)
        trough = t5 + rng.randint(0, 5)
        candidate = event(peak, t3, t5, trough, trough + 4)
        prompt = lambda number, states=states: states.status[numbered(number)] is Prompt.YES    # noqa: E731
        early = range(peak, t3)
        covered = prompt(peak) and all(prompt(day) for day in early)
        new = not covered and any(prompt(day) and not prompt(day - 1) for day in early)
        interrupted = prompt(peak) and not covered and not new
        late = not any(prompt(day) for day in early) and any(prompt(day) for day in range(t3, trough + 1))
        missed = not any(prompt(day) for day in range(peak, trough + 1))
        truths = {EventClass.COVERED: covered, EventClass.NEW: new, EventClass.INTERRUPTED: interrupted,
                  EventClass.LATE: late, EventClass.MISSED: missed}
        assert sum(truths.values()) == 1
        result = judge_event(candidate, states).category
        assert truths[result]
        seen.add(result)
    assert seen == set(truths)


# ---------------------------------------------------------------------------
# R2 比例与分母边界
# ---------------------------------------------------------------------------


def test_denominator_boundary_a_no_non_truncated_event() -> None:
    """（a）非左截断事件数为 0 → “R2 无法计算”。

    构造一：事件表为空。构造二：两个事件的 P 都 ≤ f（f 为第 10 日，P 为第 5、10 日）。
    推算：两种构造里计入分母的事件都是 0 个，所以不给比例、不判断是否 ≥ 60%；构造二左截断 2 件单列。
    """
    states = marked(60, {}, before_first=9)
    empty = r2_result([], states, RULE)
    assert (empty.computable, empty.meets, empty.excluding_insufficient, empty.denominator) == (False, None, None, 0)
    truncated = r2_result([event(5, 7, 8, 9, 12), event(10, 12, 13, 14, 18)], states, RULE)
    assert (truncated.computable, truncated.meets, truncated.excluding_insufficient) == (False, None, None)
    assert truncated.counts[EventClass.LEFT_TRUNCATED] == 2 and truncated.denominator == 0


def test_denominator_boundary_b_all_events_have_insufficient_input() -> None:
    """（b）存在非左截断事件，但全部为输入不足 → 计入分母、按不达标计。

    构造：f 为第 1 日；事件一 P = 10、T3 = 13，事件二 P = 30、T3 = 32；第 11 日、第 31 日的状态无法确定，
    [T3, Tr] 内与高点的前一日都没有无法确定。
    推算：左截断 0；两个事件的 [P, T3) 内各有一个无法确定 → 输入不足 2；分母 2，达标 0；
    R2 = 0/2，5 × 0 ≥ 3 × 2 为假 → 不满足；只计新提示 0/2；剔除输入不足后分母为 0 → “无定义”。
    """
    result = r2_result([event(10, 13, 14, 16, 20), event(30, 32, 33, 35, 39)], marked(60, {11: "?", 31: "?"}), RULE)
    assert result.computable is True
    assert (result.counts[EventClass.INSUFFICIENT], result.counts[EventClass.LEFT_TRUNCATED]) == (2, 0)
    assert (result.achieved, result.denominator, result.meets) == (0, 2, False)
    assert (result.new_only, result.excluding_insufficient) == (0, None)


def test_denominator_boundary_c_mixed() -> None:
    """（c）混合：左截断、输入不足、新提示达标各一个。

    构造：f 为第 10 日。事件一 P = 8（≤ f，左截断）；事件二 P = 20、T3 = 23，第 21 日无法确定（输入不足）；
    事件三 P = 40、T3 = 43，第 41 日为提示、第 40 日为非提示（新提示达标）。
    推算：左截断 1，单列、不入分母；分母 2；达标 1；R2 = 1/2，5 × 1 ≥ 3 × 2 为假 → 不满足；
    只计新提示 1/2；剔除输入不足后 1/1。
    """
    states = marked(60, {21: "?", 41: "Y"}, before_first=9)
    result = r2_result([event(8, 11, 12, 13, 16), event(20, 23, 24, 26, 30), event(40, 43, 44, 46, 50)], states, RULE)
    assert [item.category for item in result.judgements] == [EventClass.LEFT_TRUNCATED, EventClass.INSUFFICIENT,
                                                             EventClass.NEW]
    assert (result.computable, result.counts[EventClass.LEFT_TRUNCATED], result.denominator) == (True, 1, 2)
    assert (result.achieved, result.meets, result.new_only, result.excluding_insufficient) == (1, False, 1, (1, 1))


def many_events(count: int, covered: int, new: int = 0) -> tuple[list[R2Event], Window]:
    """count 个互不重叠的事件（每 10 日一个）：前 covered 个持续覆盖达标，接着 new 个新提示达标，其余漏报。"""
    prompts: dict[int, str] = {}
    events = []
    for number in range(count):
        peak = 10 * number + 15
        events.append(event(peak, peak + 2, peak + 3, peak + 4, peak + 6))
        if number < covered:
            prompts.update(span(peak - 1, peak + 1))
        elif number < covered + new:
            prompts[peak + 1] = "Y"
    return events, marked(10 * count + 30, prompts)


def test_sixty_percent_boundary_is_decided_with_integers() -> None:
    three_of_five = r2_result(*many_events(5, covered=3), RULE)
    assert (three_of_five.achieved, three_of_five.denominator, three_of_five.meets) == (3, 5, True)     # 3/5 达标
    fifty_nine = r2_result(*many_events(100, covered=59), RULE)
    assert (fifty_nine.achieved, fifty_nine.denominator, fifty_nine.meets) == (59, 100, False)          # 59/100 不达标
    sixty = r2_result(*many_events(100, covered=60), RULE)
    assert sixty.meets is True


def test_new_prompt_only_ratio_is_reported_separately() -> None:
    result = r2_result(*many_events(10, covered=4, new=3), RULE)
    assert (result.achieved, result.new_only, result.denominator) == (7, 3, 10)        # 达标 7/10，其中只计新提示 3/10
    assert result.counts[EventClass.COVERED] == 4 and result.counts[EventClass.MISSED] == 3
    assert result.meets is True and result.excluding_insufficient == (7, 10)


# ---------------------------------------------------------------------------
# 提示段账
# ---------------------------------------------------------------------------

FINISHED = event(20, 22, 24, 26, 30)             # P = 20，T5 = 24，Tr = 26，End = 30


def ledger_classes(prompts: dict[int, str], events: list[R2Event], length: int = 80) -> list[SegmentClass]:
    return [category for _, category in segment_ledger(events, marked(length, prompts), RULE).classes]


def test_segment_classes_one_each() -> None:
    assert ledger_classes(span(22, 23), [FINISHED]) == [SegmentClass.IN_EVENT]          # s ∈ [P, Tr)
    assert ledger_classes(span(26, 27), [FINISHED]) == [SegmentClass.AFTER_TROUGH]      # s ∈ [Tr, End)
    assert ledger_classes(span(10, 11), [FINISHED]) == [SegmentClass.EARLY]             # (s, s+20] 内到达 T5
    assert ledger_classes(span(40, 41), [FINISHED]) == [SegmentClass.FALSE_ALARM]       # 20 日内没有事件到达 T5
    assert ledger_classes(span(70, 71), [FINISHED]) == [SegmentClass.INCOMPLETE]        # s+20 = 90 晚于 E = 79
    # 边界：s = P 属事件内；s = Tr 属低点后；s = End 已不在事件之内。
    assert ledger_classes({20: "Y"}, [FINISHED]) == [SegmentClass.IN_EVENT]
    assert ledger_classes({26: "Y"}, [FINISHED]) == [SegmentClass.AFTER_TROUGH]
    assert ledger_classes({30: "Y"}, [FINISHED]) == [SegmentClass.FALSE_ALARM]
    # (s, s+20] 的两端：s = 4 时 s+20 = 24 恰为 T5，算提前；s = 3 时 T5 在 s+20 之后，不算。
    assert ledger_classes({4: "Y"}, [FINISHED]) == [SegmentClass.EARLY]
    assert ledger_classes({3: "Y"}, [FINISHED]) == [SegmentClass.FALSE_ALARM]


def test_after_trough_interval_of_unfinished_event_runs_to_window_end() -> None:
    unfinished = event(20, 22, 24, 26, None)
    assert ledger_classes(span(60, 61), [unfinished]) == [SegmentClass.AFTER_TROUGH]     # 未结束事件为 [Tr, E]
    assert ledger_classes(span(60, 61), [FINISHED]) == [SegmentClass.INCOMPLETE]


def test_segment_ledger_uses_left_truncated_events_too() -> None:
    # 事件的高点早于窗口第一个信号日（左截断），提示段落在它的 [P, Tr) 之内：仍归事件内提示。
    states = marked(60, span(24, 25), before_first=21)
    assert segment_ledger([FINISHED], states, RULE).counts[SegmentClass.IN_EVENT] == 1


def test_segment_containing_the_first_signal_day_three_cases() -> None:
    """实施口径补充第 11 条：f 为第 10 日，看 S_{f−1}（第 9 日）。"""
    # （a）S_{f−1} 为提示：单列“窗口前已启动”，不进入误报比例。
    started_before = segment_ledger([], marked(60, span(9, 12), before_first=9), RULE)
    assert [category for _, category in started_before.classes] == [SegmentClass.PRE_WINDOW]
    assert started_before.false_alarm_ratio is None
    # （b）S_{f−1} 为非提示：s = f，按正常规则归类——一例误报，一例事件内提示。
    in_window = marked(60, span(10, 12), before_first=9)
    assert [category for _, category in segment_ledger([], in_window, RULE).classes] == [SegmentClass.FALSE_ALARM]
    assert segment_ledger([], in_window, RULE).false_alarm_ratio == (1, 1)
    inside = segment_ledger([event(8, 9, 11, 14, 18)], in_window, RULE)
    assert [category for _, category in inside.classes] == [SegmentClass.IN_EVENT]
    assert prompt_segments(in_window)[0].start == numbered(10)
    # （c）S_{f−1} 无法确定：抛异常，不归类。
    with pytest.raises(R2Error, match="无法确定"):
        segment_ledger([], marked(60, {9: "?", 10: "Y", 11: "Y"}, before_first=9), RULE)


def test_unknown_state_right_before_a_segment_inside_the_window_raises() -> None:
    """实施口径补充第 14 条：提示段开头的前一状态无法确定、影响起始日判断时抛异常。"""
    with pytest.raises(R2Error, match="起始日无法判断"):
        prompt_segments(marked(60, {30: "?", 31: "Y", 32: "Y"}))
    # 无法确定的日子不紧挨着任何提示段的开头时不影响：段在它之前结束，下一段的前一日是确定的非提示。
    segments = prompt_segments(marked(60, {28: "Y", 29: "Y", 30: "?", 32: "Y"}))
    assert [(item.start, item.end) for item in segments] == [(numbered(28), numbered(29)), (numbered(32), numbered(32))]


def test_observation_boundary_s_plus_20_equal_to_window_end() -> None:
    # E 为第 59 日。s = 39：s+20 = 59 恰为 E → 误报；s = 40：s+20 = 60 晚一日 → 观察不完整。
    assert ledger_classes({39: "Y"}, [], length=60) == [SegmentClass.FALSE_ALARM]
    assert ledger_classes({40: "Y"}, [], length=60) == [SegmentClass.INCOMPLETE]


def test_same_segment_can_fall_into_different_classes_for_the_two_assets() -> None:
    states = marked(80, span(22, 23))
    spx = segment_ledger([FINISHED], states, RULE)
    qqq = segment_ledger([event(50, 52, 54, 56, 60, asset="QQQ")], states, RULE)
    assert [category for _, category in spx.classes] == [SegmentClass.IN_EVENT]
    assert [category for _, category in qqq.classes] == [SegmentClass.FALSE_ALARM]


def test_false_alarm_ratio() -> None:
    prompts = {**span(4, 5), **span(40, 41), **span(50, 51), **span(22, 22)}
    ledger = segment_ledger([FINISHED], marked(80, prompts), RULE)
    assert ledger.counts[SegmentClass.EARLY] == 1 and ledger.counts[SegmentClass.FALSE_ALARM] == 2
    assert ledger.counts[SegmentClass.IN_EVENT] == 1
    assert ledger.false_alarm_ratio == (2, 3)                       # 误报 ÷ (误报 + 提前提示)
    # 分母为 0：只有事件内提示与观察不完整 → “无定义”。
    undefined = segment_ledger([FINISHED], marked(80, {**span(22, 23), **span(70, 71)}), RULE)
    assert undefined.false_alarm_ratio is None
    assert segment_ledger([FINISHED], marked(80, {}), RULE).false_alarm_ratio is None


def test_window_requires_states_from_the_day_before_the_first_signal_day() -> None:
    days = tuple(numbered(number) for number in range(5))
    with pytest.raises(R2Error):
        Window(days, numbered(0), numbered(4), {day: Prompt.NO for day in days})        # f 之前没有交易日
    with pytest.raises(R2Error, match="恰好覆盖"):
        Window(days, numbered(2), numbered(4), {day: Prompt.NO for day in days[2:]})    # 缺 f − 1 的状态


# ---------------------------------------------------------------------------
# 实施口径补充第 20 条：P 前一日状态无法确定时的新提示
# ---------------------------------------------------------------------------


def test_rule_20_first_confirmable_new_prompt_day_when_the_day_before_peak_is_unknown() -> None:
    """S_P 为提示、S_{P−1} 无法确定，[P, T3) 内在第 P+2 日有一次确定的“非提示 → 提示”转换。

    P = 100，T3 = 103。第 99 日无法确定，第 100 日提示，第 101 日非提示，第 102 日提示。
    期望：新提示达标；日期记录为第 102 日（首次可确认的新提示日，不是真实的首次新提示日）；新字段为真；
    可执行日为第 103 日，相对 T3 的偏移为 0，都按第 102 日计算。
    """
    judged = judge_event(EXAMPLE, marked(131, {99: "?", 100: "Y", 102: "Y"}))
    assert judged.category is EventClass.NEW
    assert judged.first_new_day == numbered(102) and judged.peak_new_uncertain is True
    assert (judged.executable_day, judged.executable_offset) == (numbered(103), 0)


def test_rule_20_control_day_before_peak_is_not_prompting() -> None:
    """对照：同样的状态，只把第 99 日改为非提示。P 当日就是新提示：日期为 P，新字段为假。"""
    judged = judge_event(EXAMPLE, marked(131, {99: "N", 100: "Y", 102: "Y"}))
    assert judged.category is EventClass.NEW
    assert judged.first_new_day == numbered(100) and judged.peak_new_uncertain is False
    assert (judged.executable_day, judged.executable_offset) == (numbered(101), -2)


def test_new_field_is_false_in_every_other_case() -> None:
    cases = [marked(131, span(99, 102)),                       # 持续覆盖
             marked(131, {101: "Y"}),                           # 新提示（S_P 为非提示）
             marked(131, {99: "Y", 100: "Y"}),                  # 提示中断
             marked(131, {99: "Y", 100: "Y", 102: "Y"}),        # 中断后重新开始，S_{P−1} 为提示
             marked(131, {104: "Y"}),                           # 迟到
             marked(131, {}),                                   # 漏报
             marked(131, {101: "?"})]                           # 输入不足
    assert [judge_event(EXAMPLE, states).peak_new_uncertain for states in cases] == [False] * 7
    left = marked(60, span(0, 59))
    assert judge_event(event(1, 3, 5, 6, 9), left).peak_new_uncertain is False      # 左截断
