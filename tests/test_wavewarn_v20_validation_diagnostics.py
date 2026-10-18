"""阶段五必报诊断 D-1—D-3（validation_diagnostics）的构造测试（V2 指令第五节：T-D1、T-D2、T-D3、T-D3core、T-D-INV）。
纯算法测试。

构造方式：日期轴为连续的日历日 d0, d1, …（只作行号标签）；窗口信号日 [f, E] = [d1, d39]，提示状态覆盖 d0—d39；
事件按项目 r2 模块逐个判定（judge_event / r2_result），提示段取 r2.prompt_segments。
期望值一律按标准第五节的定义从构造的状态与事件手工推导，推导写在各测试的注释中。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from market_risk.wavewarn_v20.labels_r2 import R2Event
from market_risk.wavewarn_v20.r2 import EventClass, Prompt, R2Rule, Window, prompt_segments, r2_result
from market_risk.wavewarn_v20.state_machine import Risk
from market_risk.wavewarn_v20.validation_diagnostics import (
    NO_SEGMENT,
    NOT_APPLICABLE,
    NOT_EXIST,
    NOT_JUDGED,
    DiagnosticsError,
    DiagnosticWindow,
    abnormal_occupancy,
    diagnostics,
    long_hang,
    new_prompt_share,
    warning_lines,
)

RULE = R2Rule(3, 5, 20)                       # 登记：3/5（60%），观察期 20
START = dt.date(2010, 1, 1)
LENGTH = 40


def d(index: int) -> dt.date:
    return START + dt.timedelta(days=index)


DAYS = tuple(d(index) for index in range(LENGTH))
FIRST, LAST = d(1), d(LENGTH - 1)
DWINDOW = DiagnosticWindow(FIRST, LAST, DAYS[1:])

# 提示状态（YES 为提示）：d0—d5 提示（d0 在窗口前一日，所以 d1 起的段为“窗口前已启动”）；d6—d9 非提示；
# d10—d18 提示；d19—d22 非提示；d23—d26 提示；d27—d35 非提示；d36—d39 提示（窗口末未结束）。
PROMPTING = {*range(0, 6), *range(10, 19), *range(23, 27), *range(36, 40)}


def status(prompting: set[int] = PROMPTING) -> dict[dt.date, Prompt]:
    return {d(index): Prompt.YES if index in prompting else Prompt.NO for index in range(LENGTH)}


def window(prompting: set[int] = PROMPTING) -> Window:
    return Window(DAYS, FIRST, LAST, status(prompting))


def event(asset: str, peak: int, t3: int, trough: int) -> R2Event:
    """构造事件：T5 取 T3 次日（D-3 不用 T5）；结束日取低点次日。"""
    return R2Event(asset, d(peak), Decimal("100"), d(t3), d(t3 + 1), d(trough), Decimal("90"), d(trough + 1), False)


# SPX 七个事件（手工分类，依 r2 第 131 行起的判定顺序）：
# eA P=d2,T3=d4,Tr=d12：[P,T3)=d2,d3 全为提示 → 持续覆盖达标；
# eB P=d11,T3=d14,Tr=d16：d11—d13 全提示 → 持续覆盖达标；
# eC P=d12,T3=d15,Tr=d20：d12—d14 全提示 → 持续覆盖达标；
# eD P=d7,T3=d9,Tr=d24：d7、d8 非提示，无“非提示 → 提示”转换；[T3,Tr]=d9—d24 内有提示 → 迟到；
# eE P=d21,T3=d24,Tr=d25：d21、d22 非提示、d23 提示 → d23 为新提示 → 新提示达标；
# eF P=d0 ≤ f=d1 → 左截断（不进分母，不计 e）；
# eG P=d28,T3=d30,Tr=d33：d28、d29 非提示，d30—d33 无提示 → 漏报。
SPX_EVENTS = (event("SPX", 2, 4, 12), event("SPX", 11, 14, 16), event("SPX", 12, 15, 20), event("SPX", 7, 9, 24),
              event("SPX", 21, 24, 25), event("SPX", 0, 2, 3), event("SPX", 28, 30, 33))
# QQQ 五个事件：q1 P=d2,T3=d4,Tr=d5 持续覆盖；q2 P=d11,T3=d13,Tr=d14 持续覆盖；q3 P=d15,T3=d17,Tr=d18 持续覆盖；
# q4 P=d21,T3=d24,Tr=d25 新提示达标；q5 P=d28,T3=d30,Tr=d33 漏报。
QQQ_EVENTS = (event("QQQ", 2, 4, 5), event("QQQ", 11, 13, 14), event("QQQ", 15, 17, 18), event("QQQ", 21, 24, 25),
              event("QQQ", 28, 30, 33))


def results() -> dict:
    return {"SPX": r2_result(SPX_EVENTS, window(), RULE), "QQQ": r2_result(QQQ_EVENTS, window(), RULE)}


def segments() -> dict:
    found = prompt_segments(window())
    return {"SPX": found, "QQQ": found}


def test_constructed_classification_is_as_derived_by_hand() -> None:
    """前提核对：项目 r2 模块对构造事件的分类与手工推导相同（不是诊断的期望值，而是诊断输入的核对）。"""
    spx = results()["SPX"]
    assert [item.category for item in spx.judgements] == [
        EventClass.COVERED, EventClass.COVERED, EventClass.COVERED, EventClass.LATE, EventClass.NEW,
        EventClass.LEFT_TRUNCATED, EventClass.MISSED]
    assert (spx.denominator, spx.achieved, spx.new_only, spx.meets) == (6, 4, 1, True)   # 5×4=20 ≥ 3×6=18
    qqq = results()["QQQ"]
    assert (qqq.denominator, qqq.achieved, qqq.new_only, qqq.meets) == (5, 4, 1, True)   # 5×4=20 ≥ 3×5=15
    found = prompt_segments(window())
    assert [(item.start, item.end, item.pre_window) for item in found] == [
        (d(1), d(5), True), (d(10), d(18), False), (d(23), d(26), False), (d(36), d(39), False)]


# ---------------------------------------------------------------------------
# T-D3core：多个段、多个事件，逐段逐资产手算 c、e、L
# ---------------------------------------------------------------------------


def test_d3core_spx_by_hand() -> None:
    """SPX（手算）：
    s1 [d1,d5]：L = 5；c = 1（eA 的 P=d2 在段内）；e = 1（eA 的 [d2,d12] 与段相交；eF 左截断不计）。
    s2 [d10,d18]：L = 9；c = 2（eB、eC）；e = 4（eA [d2,d12]、eB、eC、eD [d7,d24] 都与段相交）。
    s3 [d23,d26]：L = 4；c = 0；e = 2（eD [d7,d24]、eE [d21,d25]）。
    s4 [d36,d39]：L = 4；c = 0；e = 0；窗口末未结束。
    Σc = 3 = 持续覆盖达标数；e ≥ 2 的段 2 个（s2、s3）；c 最大值 2，只有 s2；最长段 s2（L = 9）。
    剔除单段（R2 满足）：5 × (4 − c) < 3 × 6 = 18？s1：15 < 18 触发；s2：10 < 18 触发；s3、s4：20 不触发。
    eA 的 [P, Tr] 跨 s1、s2 两段，e 两段各计 1；新提示达标事件 eE 不计入任何段的 c。"""
    hang = long_hang("SPX", results()["SPX"], segments()["SPX"], DWINDOW, RULE)
    rows = [(row["start"], row["end"], row["L"], row["c"], row["e"], row["flags"]) for row in hang["segments"]]
    assert rows == [(d(1), d(5), 5, 1, 1, ["窗口前已启动"]), (d(10), d(18), 9, 2, 4, []), (d(23), d(26), 4, 0, 2, []),
                    (d(36), d(39), 4, 0, 0, ["窗口末未结束"])]
    assert hang["e_at_least_2"] == 2 and hang["c_max"] == 2
    assert [(item["start"], item["c"]) for item in hang["c_max_segments"]] == [(d(10), 2)]
    assert hang["longest_L"] == 9 and [item["start"] for item in hang["longest"]] == [d(10)]
    check = hang["removal_check"]
    assert check["result"] == "是" and [(item["start"], item["c"]) for item in check["triggered"]] == [
        (d(1), 1), (d(10), 2)]


def test_d3core_qqq_exactly_sixty_percent_does_not_trigger() -> None:
    """QQQ（手算）：分母 5，达标 4。s1 c = 1：5 × (4 − 1) = 15 < 15 不成立 → 恰为 60%，不触发；
    s2 c = 2（q2、q3）：5 × 2 = 10 < 15 → 触发。e：s1 = 1（q1），s2 = 2（q2、q3），s3 = 1（q4 [d21,d25]），s4 = 0。"""
    hang = long_hang("QQQ", results()["QQQ"], segments()["QQQ"], DWINDOW, RULE)
    assert [(row["c"], row["e"]) for row in hang["segments"]] == [(1, 1), (2, 2), (0, 1), (0, 0)]
    assert hang["e_at_least_2"] == 1
    assert [(item["start"], item["c"]) for item in hang["removal_check"]["triggered"]] == [(d(10), 2)]


def test_d3_warning_text_is_the_standard_wording_one_line_per_segment() -> None:
    """标准第五节第 3 小节原文；两段触发逐条列出（SPX：s1 的 L = 5、c = 1；s2 的 L = 9、c = 2）。"""
    hang = long_hang("SPX", results()["SPX"], segments()["SPX"], DWINDOW, RULE)
    assert warning_lines(hang) == [
        "**警示：SPX 的 R2 资格依赖单一提示段（2010-01-02—2010-01-06，5 个信号日）对 1 个事件的持续覆盖；"
        "剔除该段的持续覆盖后，R2 比例低于 60%。**",
        "**警示：SPX 的 R2 资格依赖单一提示段（2010-01-11—2010-01-19，9 个信号日）对 2 个事件的持续覆盖；"
        "剔除该段的持续覆盖后，R2 比例低于 60%。**"]


def test_d3core_sum_of_c_is_asserted() -> None:
    """持续覆盖事件按 P 唯一归段，Σ_s c = 持续覆盖达标数；少给一个含持续覆盖事件的段，断言失败。"""
    partial = segments()["SPX"][1:]                          # 去掉含 eA 的 s1：Σc = 2 ≠ 3
    with pytest.raises(DiagnosticsError, match="归段之和"):
        long_hang("SPX", results()["SPX"], partial, DWINDOW, RULE)


def test_d3core_no_warning_when_r2_not_met_even_if_integer_condition_holds() -> None:
    """R2 未满足时不判断“剔除单段”、不报警示，其余 D-3 内容照报。构造：只保留 eA（持续覆盖）与 eD（迟到）、eG（漏报）、
    eH（漏报，P=d29,T3=d31,Tr=d34）：分母 4，达标 1，5 × 1 = 5 < 12 → R2 未满足；整数条件 5 × (1 − 1) < 12 本身成立。"""
    events = (SPX_EVENTS[0], SPX_EVENTS[3], SPX_EVENTS[6], event("SPX", 29, 31, 34))
    result = r2_result(events, window(), RULE)
    assert (result.denominator, result.achieved, result.meets) == (4, 1, False)
    hang = long_hang("SPX", result, segments()["SPX"], DWINDOW, RULE)
    assert hang["removal_check"] == {"r2_meets": False, "result": NOT_JUDGED, "triggered": []}
    assert hang["c_max"] == 1 and hang["segments_count"] == 4 and warning_lines(hang) == []


# ---------------------------------------------------------------------------
# T-D3：边界与出口
# ---------------------------------------------------------------------------


def test_d3_no_prompt_segment() -> None:
    """无提示段：段数与 e ≥ 2 的段数为 0；c 最大值 0；最长段与达到最大值的段“不存在”；判断“否（无提示段）”。
    构造：全部非提示；事件 eG（漏报）一个，分母 1。"""
    quiet = window(set())
    result = r2_result((SPX_EVENTS[6],), quiet, RULE)
    hang = long_hang("SPX", result, prompt_segments(quiet), DWINDOW, RULE)
    assert (hang["segments_count"], hang["e_at_least_2"], hang["c_max"]) == (0, 0, 0)
    assert hang["c_max_segments"] == NOT_EXIST and hang["longest"] == NOT_EXIST and hang["longest_L"] is None
    assert hang["removal_check"]["result"] == NO_SEGMENT


def test_d3_segments_but_all_c_zero_and_ties() -> None:
    """有段但全部 c = 0：最大值 0，“达到最大值的段”写“不适用”。最长段并列时全部列出（按起日升序）：
    构造提示 d3—d6 与 d20—d23 两段，L 均为 4；事件只有 eG（漏报，P=d28），不落在任何段内。"""
    prompting = {*range(3, 7), *range(20, 24)}
    custom = window(prompting)
    result = r2_result((SPX_EVENTS[6],), custom, RULE)
    hang = long_hang("SPX", result, prompt_segments(custom), DWINDOW, RULE)
    assert hang["c_max"] == 0 and hang["c_max_segments"] == NOT_APPLICABLE
    assert hang["longest_L"] == 4 and [item["start"] for item in hang["longest"]] == [d(3), d(20)]


def test_d3_tied_c_maximum_listed_in_order() -> None:
    """c 最大值并列：两段各含 1 个持续覆盖事件（P=d2 在 s1、P=d11 在 s2），均列出，按起日升序。"""
    events = (event("SPX", 2, 4, 5), event("SPX", 11, 13, 14))
    result = r2_result(events, window(), RULE)
    hang = long_hang("SPX", result, segments()["SPX"], DWINDOW, RULE)
    assert hang["c_max"] == 1 and [item["start"] for item in hang["c_max_segments"]] == [d(1), d(10)]


def test_d3_segment_outside_window_is_rejected() -> None:
    from market_risk.wavewarn_v20.r2 import Segment

    with pytest.raises(DiagnosticsError, match="不在诊断窗口"):
        long_hang("SPX", results()["SPX"], (Segment(d(0), d(5), False),), DWINDOW, RULE)


# ---------------------------------------------------------------------------
# T-D1：新提示占比
# ---------------------------------------------------------------------------


def test_d1_exactly_sixty_percent_and_below() -> None:
    """D-1 整数判断 5 × 新提示达标数 ≥ 3 × 分母：
    - 构造 A（共用的 SPX 例）：新提示 1，分母 6：5 < 18 → 低于 60%；R2 满足 → 写事实陈述。
    - 构造 B：新提示 3、分母 5：15 ≥ 15 → 恰为 60%，判为“≥ 60%”，不写事实陈述。"""
    spx = new_prompt_share("SPX", results()["SPX"], RULE)
    assert (spx["new_achieved"], spx["covered_achieved"], spx["r2_achieved"], spx["denominator"]) == (1, 3, 4, 6)
    assert spx["new_share_at_least_60"] is False
    assert spx["statement"] == "SPX 的 R2 达标部分依赖持续覆盖；只计新提示时未达到 60%。"
    # 构造 B：提示状态 d10 起每 6 日一个非提示 → 提示 的转换；三个事件各在转换前一日为高点。
    prompting = {13, 14, 15, 19, 20, 21, 25, 26, 27}
    custom = window(prompting)
    events = (event("QQQ", 12, 14, 15), event("QQQ", 18, 20, 21), event("QQQ", 24, 26, 27),
              event("QQQ", 30, 32, 33), event("QQQ", 31, 33, 34))
    result = r2_result(events, custom, RULE)
    # 手工分类：前三个事件 [P,T3) 首日非提示、次日提示 → 新提示达标；后两个 [P,Tr] 内无提示 → 漏报。
    assert [item.category for item in result.judgements] == [EventClass.NEW] * 3 + [EventClass.MISSED] * 2
    share = new_prompt_share("QQQ", result, RULE)
    assert (share["new_achieved"], share["denominator"], share["new_share_at_least_60"]) == (3, 5, True)
    assert share["statement"] is None


def test_d1_no_statement_when_r2_not_met() -> None:
    """R2 未满足而新提示占比低于 60%：不写事实陈述（陈述只在 R2 满足时写）。"""
    events = (SPX_EVENTS[0], SPX_EVENTS[3], SPX_EVENTS[6], event("SPX", 29, 31, 34))
    share = new_prompt_share("SPX", r2_result(events, window(), RULE), RULE)
    assert share["new_share_at_least_60"] is False and share["statement"] is None


# ---------------------------------------------------------------------------
# T-D2：非正常状态总占用率
# ---------------------------------------------------------------------------


def test_d2_undetermined_days_count_in_denominator_only() -> None:
    """窗口 39 个信号日：一级 4 日、二级 2 日、无法确定 3 日、其余正常 30 日（窗口外 d0 为二级，不计入）。
    总占用率 = (4 + 2) ÷ 39；无法确定的 3 日计入分母、不计入分子。"""
    risks: dict[int, Risk | None] = {index: Risk.NORMAL for index in range(LENGTH)}
    risks.update({0: Risk.LEVEL2, 5: Risk.LEVEL1, 6: Risk.LEVEL1, 7: Risk.LEVEL1, 8: Risk.LEVEL1, 20: Risk.LEVEL2,
                  21: Risk.LEVEL2, 30: None, 31: None, 32: None})
    result = abnormal_occupancy([(d(index), value) for index, value in risks.items()], DWINDOW)
    assert (result["window_days"], result["level1_days"], result["level2_days"], result["undetermined_days"],
            result["normal_days"], result["abnormal_days"]) == (39, 4, 2, 3, 30, 6)
    assert result["abnormal_ratio"] == 6 / 39 and result["level1_ratio"] == 4 / 39 and result["level2_ratio"] == 2 / 39


def test_d2_missing_window_day_is_rejected() -> None:
    with pytest.raises(DiagnosticsError, match="缺少窗口内的信号日"):
        abnormal_occupancy([(d(index), Risk.NORMAL) for index in range(LENGTH - 1)], DWINDOW)


# ---------------------------------------------------------------------------
# 汇总与 T-D-INV
# ---------------------------------------------------------------------------


def test_report_lists_statements_and_warnings_per_asset() -> None:
    """两资产分列；事实陈述两条（SPX、QQQ 都是 R2 满足而新提示占比低于 60%）；警示逐资产逐段：
    SPX 两条（s1、s2），QQQ 一条（s2）。诊断不接收类别，不会因整体类别（如 D）而省略警示（T-D-INV 的一半；
    另一半“计算无效时不计算”由入口测试检查）。"""
    states = [(day, Risk.NORMAL) for day in DAYS]
    report = diagnostics(results(), segments(), states, DWINDOW, RULE)
    assert [item["asset"] for item in report["d1_new_prompt_share"]] == ["SPX", "QQQ"]
    assert report["statements"] == ["SPX 的 R2 达标部分依赖持续覆盖；只计新提示时未达到 60%。",
                                    "QQQ 的 R2 达标部分依赖持续覆盖；只计新提示时未达到 60%。"]
    assert len(report["warnings"]) == 3 and report["warnings"][2].startswith("**警示：QQQ ")
    assert report["window"] == {"first": FIRST, "last": LAST, "signal_days": 39}


def test_uncomputable_r2_is_not_diagnosed() -> None:
    """R2 无法计算（非左截断事件为 0）时不计算诊断。"""
    result = r2_result((SPX_EVENTS[5],), window(), RULE)
    assert result.computable is False
    with pytest.raises(DiagnosticsError, match="无法计算"):
        new_prompt_share("SPX", result, RULE)
