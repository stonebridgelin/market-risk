"""失败路径分析（T2）的测试：全部用构造数据，期望值人工推算并写在各案例的说明里。"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from market_risk.wavewarn.config_failure_path import load_failure_path_config, parse_failure_path_config
from market_risk.wavewarn.failure_path import (
    DRAGGED,
    IMPROVED,
    NEUTRAL,
    START_BEFORE,
    START_RED,
    START_YELLOW,
    START_YELLOW_RED,
    PathError,
    PathSettings,
    Span,
    executed_lights,
    exposures_of,
    full_returns,
    interval_returns,
    log_gaps,
    non_green_spans,
    outcome_of,
    prior_start,
    rebuilt_wealth,
    reconcile,
    segment_rows,
    start_type_of,
    window_path,
)
from market_risk.wavewarn.failure_path_analysis import ObjectInput, WindowInput, analyse_object
from market_risk.wavewarn.failure_path_context import (
    AFTER_T0,
    AFTER_TROUGH,
    AFTER_TROUGH_OPEN,
    BEAR,
    BEFORE_T0,
    CHANNEL_CLASSES,
    DOWN_YEAR,
    FLAT_YEAR,
    HOLD,
    MULTIPLE,
    OUTSIDE,
    OUTSIDE_TAIL,
    PRE_PEAK,
    PULLBACK,
    STRESS,
    TREND,
    UP_YEAR,
    YEAR_UNAVAILABLE,
    EnvironmentSettings,
    MergedEvent,
    annual_returns,
    channel_class,
    channel_classes,
    class_totals,
    environment_of,
    segment_position,
)
from market_risk.wavewarn.failure_path_report import segment_table, summary_table

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = PathSettings(0.0001, 5, 1e-10)
LEVELS = {"绿": 1.0, "黄": 0.5, "红": 0.0}
WEIGHTS = {"SPX": 0.5, "QQQ": 0.5}
DAYS = tuple(dt.date(2021, 3, 1) + dt.timedelta(days=index) for index in range(40))   # 构造的日期轴，逐日相连
ENVIRONMENT = EnvironmentSettings(((dt.date(2000, 3, 24), dt.date(2002, 10, 9)),), 0.10, -0.10)


def _path(lights: str, full: tuple[float, ...], prior: str = ""):
    count = len(lights)
    days = DAYS[10:10 + count + 1]
    return window_path("对象", days, DAYS[9:9 + count], tuple(lights), LEVELS, full,
                       DAYS[10 - len(prior):10], tuple(prior))


def test_log_gap_and_wealth_ratio_by_hand() -> None:
    """SPX 收盘 100、110、99，QQQ 收盘 50、50、60。

    R_SPX = +10%、−10%；R_QQQ = 0、+20%；满仓 R = ½(0.10 + 0) = 0.05，½(−0.10 + 0.20) = 0.05。
    执行灯色 黄、红 → e = 0.5、0：策略 R = 0.025、0。
    d = ln(1.025) − ln(1.05)，ln(1) − ln(1.05)；Σd = ln(1.025) − 2·ln(1.05) = ln(1.025 ÷ 1.1025)。
    净值：满仓 1 → 1.05 → 1.1025；策略 1 → 1.025 → 1.025。Σd = 0.0246926 − 2 × 0.0487902 = −0.0728877。
    """
    closes = {"SPX": (100.0, 110.0, 99.0), "QQQ": (50.0, 50.0, 60.0)}
    returns = {symbol: interval_returns(values) for symbol, values in closes.items()}
    assert returns["SPX"] == pytest.approx((0.10, -0.10)) and returns["QQQ"] == pytest.approx((0.0, 0.20))
    full = full_returns(returns, WEIGHTS)
    assert full == pytest.approx((0.05, 0.05))
    exposures = exposures_of(("黄", "红"), LEVELS)
    gaps = log_gaps(exposures, full)
    assert gaps == pytest.approx((math.log(1.025) - math.log(1.05), -math.log(1.05)))
    strategy, hold = rebuilt_wealth(closes, WEIGHTS, exposures), rebuilt_wealth(closes, WEIGHTS, (1.0, 1.0))
    assert strategy == pytest.approx((1.0, 1.025, 1.025)) and hold == pytest.approx((1.0, 1.05, 1.1025))
    assert sum(gaps) == pytest.approx(math.log(1.025 / 1.1025), abs=1e-15)
    assert sum(gaps) == pytest.approx(-0.0728877, abs=1e-7)


def test_exposure_uses_the_previous_day_signal_not_the_same_day() -> None:
    """自 t0 起的信号：绿、红、绿、绿；窗口从 t0 后一日开始（offset = 1），共 3 个区间。

    区间 0 在第 1 日收盘执行第 0 日的信号（绿），区间 1 执行第 1 日的信号（红），区间 2 执行第 2 日的信号（绿）：
    执行灯色 绿、红、绿。若错把当日信号用进来，会得到 红、绿、绿。
    满仓 R = +1%、−4%、+2%：正确做法只在区间 1 空仓，Σd = −ln(0.96) > 0；错误做法在区间 0 空仓，Σd = −ln(1.01) < 0。
    """
    signals = ("绿", "红", "绿", "绿")
    lights = executed_lights(signals, 1, 3)
    assert lights == ("绿", "红", "绿") and lights != signals[1:]
    full = (0.01, -0.04, 0.02)
    assert sum(log_gaps(exposures_of(lights, LEVELS), full)) == pytest.approx(-math.log(0.96))
    assert sum(log_gaps(exposures_of(signals[1:], LEVELS), full)) == pytest.approx(-math.log(1.01))
    # 窗口从 t0 当日开始时没有前一日信号，沿用初始的绿灯。
    assert executed_lights(signals, 0, 3) == ("绿", "绿", "红")
    with pytest.raises(PathError, match="超出"):
        executed_lights(signals, 2, 3)


def test_stops_when_one_plus_return_is_not_positive() -> None:
    """1 + R ≤ 0 立即停止，不跳过、不截断：满仓 R = −1（1 + R = 0）与 R = −1.5 都报错；收盘价不为正也报错。"""
    with pytest.raises(PathError, match="不大于 0"):
        log_gaps((1.0, 0.0), (0.01, -1.0))
    with pytest.raises(PathError, match="不大于 0"):
        log_gaps((0.5,), (-1.5,))
    assert log_gaps((0.0,), (-0.999,)) == pytest.approx((-math.log(0.001),))
    with pytest.raises(PathError, match="不为正"):
        interval_returns((100.0, 0.0, 50.0))
    with pytest.raises(PathError, match="未知的灯色"):
        exposures_of(("绿", "蓝"), LEVELS)


def test_segment_boundaries_left_truncated_and_unclosed() -> None:
    """执行灯色 黄、红、绿、绿、黄、黄（6 个区间）：

    段 1 = [0, 2)：窗口第一个区间即非绿 → 左截断；v = 2（第一个恢复绿灯的区间），段长 2。
    段 2 = [4, 6)：到窗口末仍非绿 → 未闭合；终点取最后一个计入区间的后一交易日（第 7 个日期），段长 2。
    满仓 R 每个区间 +1%：段 1 的 D = [ln(1.005) − ln(1.01)] + [0 − ln(1.01)]；段 2 的 D = 2·[ln(1.005) − ln(1.01)]。
    """
    assert non_green_spans("黄红绿绿黄黄") == (Span(0, 2, True, False), Span(4, 6, False, True))
    assert non_green_spans("绿绿绿") == () and non_green_spans("红") == (Span(0, 1, True, True),)
    path = _path("黄红绿绿黄黄", (0.01,) * 6, prior="绿黄")
    first, second = segment_rows(path, SETTINGS)
    step = math.log(1.005) - math.log(1.01)
    assert (first.start_date, first.end_date, first.length) == (DAYS[10], DAYS[12], 2)
    assert first.gap == pytest.approx(step - math.log(1.01))
    assert first.gap_simple == pytest.approx(math.expm1(first.gap))
    assert (second.start_date, second.end_date, second.length) == (DAYS[14], DAYS[16], 2)
    assert second.span.unclosed and second.end_signal_date is None and second.gap == pytest.approx(2 * step)
    # 信号日另列：段 1 的启动信号日是窗口第一天的前一交易日；恢复绿灯的信号日是区间 2 起点日的前一日。
    assert (first.start_signal_date, first.end_signal_date, second.start_signal_date) == (DAYS[9], DAYS[11], DAYS[13])
    # 段内平均暴露、满仓对数收益、黄红往返。
    assert first.mean_exposure == pytest.approx(0.25) and first.full_log_return == pytest.approx(2 * math.log(1.01))
    assert (first.yellow_to_red, first.red_to_yellow, first.round_trips) == (1, 0, 0)
    assert first.short and second.short


def test_segment_drawdowns_include_both_end_points_and_round_trips() -> None:
    """执行灯色 绿、黄、红、黄、红、黄、黄、绿（一个 6 区间的段），段内满仓 R = −10%、+5%、−20%、+1%、+10%、−2%。

    满仓净值 1 → 0.9 → 0.945 → 0.756 → 0.76356 → 0.839916 → 0.82311768：最大回撤 = 1 − 0.756 = 0.244（含起点净值）。
    策略 R = −5%、0、−10%、0、+5%、−1%：净值 1 → 0.95 → 0.95 → 0.855 → 0.855 → 0.89775 → 0.8887725，最大回撤 0.145。
    黄→红 2 次（区间 0→1、2→3），红→黄 2 次（1→2、3→4），往返 = min(2, 2) = 2。段长 6 > 5，不是短段。
    """
    path = _path("绿黄红黄红黄黄绿", (0.0, -0.10, 0.05, -0.20, 0.01, 0.10, -0.02, 0.0))
    (row,) = segment_rows(path, SETTINGS)
    assert row.full_drawdown == pytest.approx(0.244) and row.strategy_drawdown == pytest.approx(0.145)
    assert (row.yellow_to_red, row.red_to_yellow, row.round_trips, row.short, row.length) == (2, 2, 2, False, 6)
    assert row.start_type == START_YELLOW_RED and row.prior is None


def test_start_types_and_actual_start_before_the_window() -> None:
    """(a) 黄黄绿：绿→黄启动、段内没有红；(b) 黄红黄绿：出现过红；(c) 红黄绿：绿→红直接启动；(d) 左截断。

    (d) 的段另查窗口前的执行灯色（自 t0 起连续）：绿、绿、红、黄 → 该段实际在窗口前第 2 个区间启动，转换为“绿→红”，
    而不是按窗口第一天的颜色（黄）判成绿→黄。没有窗口前的记录时留空。
    窗口前最后一个执行灯色是绿时，该段其实启动于窗口第一个区间，规格没有写明归类：报错停下。
    """
    assert start_type_of(Span(1, 3, False, False), "绿黄黄绿") == START_YELLOW
    assert start_type_of(Span(1, 4, False, False), "绿黄红黄绿") == START_YELLOW_RED
    assert start_type_of(Span(1, 3, False, False), "绿红黄绿") == START_RED
    assert start_type_of(Span(0, 2, True, False), "黄黄绿") == START_BEFORE
    (row,) = segment_rows(_path("黄黄绿", (0.01, 0.01, 0.01), prior="绿绿红黄"), SETTINGS)
    assert row.start_type == START_BEFORE and row.prior is not None
    assert (row.prior.date, row.prior.transition) == (DAYS[8], "绿→红")
    (blank,) = segment_rows(_path("黄黄绿", (0.01, 0.01, 0.01)), SETTINGS)
    assert blank.start_type == START_BEFORE and blank.prior is None
    assert prior_start(DAYS[:2], ("红", "黄")) is None                 # 记录里找不到此前的绿灯：没有合法记录
    with pytest.raises(PathError, match="规格未写明"):
        segment_rows(_path("黄黄绿", (0.01, 0.01, 0.01), prior="绿绿"), SETTINGS)
    # 不是左截断的段不查窗口前的记录。
    (inside,) = segment_rows(_path("绿黄绿", (0.01, 0.01, 0.01), prior="绿红"), SETTINGS)
    assert inside.start_type == START_YELLOW and inside.prior is None


def test_neutral_band_changes_the_class_but_never_the_raw_value() -> None:
    """中性带 0.0001：D = 0.0001（恰在带上）与 −0.0001 归“接近零”；0.00010001 归改善；−0.00010001 归拖累。

    构造三段：e = 0.5、满仓 R 分别为 −0.00018、+0.00018、−0.05 → D ≈ +0.00009、−0.00009、+0.0253。
    前两段落在中性带内，但汇总的 ΣD 仍是原始值之和（≈ 0 而不是被改成 0 之后的 0，逐位等于原值相加）。
    """
    assert outcome_of(0.0001, 0.0001) == NEUTRAL and outcome_of(-0.0001, 0.0001) == NEUTRAL
    assert outcome_of(0.00010001, 0.0001) == IMPROVED and outcome_of(-0.00010001, 0.0001) == DRAGGED
    path = _path("绿黄绿黄绿黄绿", (0.0, -0.00018, 0.0, 0.00018, 0.0, -0.05, 0.0))
    rows = segment_rows(path, SETTINGS)
    assert [row.outcome for row in rows] == [NEUTRAL, NEUTRAL, IMPROVED]
    assert rows[0].gap == pytest.approx(9.0007e-05, rel=1e-3) and rows[0].gap != 0.0
    assert rows[1].gap == pytest.approx(-8.9993e-05, rel=1e-3) and rows[1].gap != 0.0
    analysis = analyse_object(ObjectInput("对象", (*"绿黄绿黄绿黄绿", "绿", "绿"), None),
                              _window(path.days, path.full), SETTINGS, ENVIRONMENT)
    table = {(row[1], row[2]): row for row in summary_table([analysis])}
    # 两段原始值之和约 +2.43e-08：不是 0（没有被改成零），也没有丢掉中性带内的值。
    assert table["按结果", NEUTRAL][3] == 2
    assert float(table["按结果", NEUTRAL][5]) == pytest.approx(rows[0].gap + rows[1].gap, abs=1e-15)
    assert float(table["按结果", NEUTRAL][5]) == pytest.approx(2.43e-08, rel=1e-2)
    # 段账本里也是原始值。
    assert [float(row[9]) for row in segment_table([analysis])] == pytest.approx([row.gap for row in rows], abs=1e-15)


def _window(days: tuple[dt.date, ...], full: tuple[float, ...], events: tuple[MergedEvent, ...] = (),
            cases: tuple[tuple[dt.date, dt.date], ...] = ()) -> WindowInput:
    """由给定的满仓收益反推两资产相同的收盘价（两资产各半、收益相同，满仓收益即单资产收益）。"""
    closes = [100.0]
    for value in full:
        closes.append(closes[-1] * (1 + value))
    axis = (DAYS[DAYS.index(days[0]) - 1], *days)
    return WindowInput("构造", axis, 1, {"SPX": tuple(closes), "QQQ": tuple(closes)}, WEIGHTS, LEVELS, events, DAYS,
                       {"SPX": frozenset(), "QQQ": frozenset()}, {2021: 0.05}, 20, cases)


def test_three_reconciliations_hold_and_any_failure_stops() -> None:
    """信号（自 t0）绿、黄、红、绿、绿、黄、黄、黄，窗口从 t0 后一日起 6 个区间 → 执行 绿、黄、红、绿、绿、黄。

    信号日激活的通道：第 1 日 P_SPX（回调层），第 2 日 P_SPX 与 MR_SPX（多层），第 5 日没有通道（系统保持）。
    (1) 两段的 D 之和 = Σd；(2) 三类（回调、多层、系统保持）的 Σd 之和 = 段之和；(3) Σd = ln(W_策略 ÷ W_满仓)。
    """
    full = (0.01, -0.02, -0.03, 0.02, 0.01, -0.01)
    days = DAYS[10:17]
    signals = ("绿", "黄", "红", "绿", "绿", "黄", "黄", "黄")
    active = ((), ("P_SPX",), ("MR_SPX", "P_SPX"), (), (), (), ("P_QQQ",), ("P_QQQ",))
    window = _window(days, full)
    result = analyse_object(ObjectInput("对象", signals, active), window, SETTINGS, ENVIRONMENT)
    assert result.path.lights == ("绿", "黄", "红", "绿", "绿", "黄")
    # 区间 5 用第 5 日（信号日）的通道：没有；第 6 日才激活的 P_QQQ 不得用进来。
    assert result.classes == (None, PULLBACK, MULTIPLE, None, None, HOLD)
    first, second, third = result.reconciliation.differences()
    assert max(abs(first), abs(second), abs(third)) <= 1e-12
    expected = (math.log(1 - 0.01) - math.log(1 - 0.02)) + (0 - math.log(1 - 0.03)) + (
        math.log(1 - 0.005) - math.log(1 - 0.01))
    assert result.reconciliation.total_gap == pytest.approx(expected, abs=1e-15)
    assert sum(row.days for row in result.totals) == 3 and {row.name for row in result.totals} == set(CHANNEL_CLASSES)
    # 任何一项不满足即停下：给错的期末净值（第 3 项）、漏掉一类（第 2 项）、漏掉一段（第 1 项）。
    path, segments = result.path, result.segments
    good = rebuilt_wealth(window.closes, WEIGHTS, path.exposures)
    hold = rebuilt_wealth(window.closes, WEIGHTS, (1.0,) * 6)
    gaps = [row.gap for row in result.totals]
    reconcile(path, segments, gaps, good, hold, SETTINGS)
    with pytest.raises(PathError, match="第 3 项"):
        reconcile(path, segments, gaps, (*good[:-1], good[-1] * 1.001), hold, SETTINGS)
    with pytest.raises(PathError, match="第 2 项"):
        reconcile(path, segments, gaps[1:], good, hold, SETTINGS)
    with pytest.raises(PathError, match="第 1、2 项"):
        reconcile(path, segments[:1], gaps, good, hold, SETTINGS)


def test_channel_classes_are_exclusive_and_exhaustive() -> None:
    """八个通道的全部 256 种组合，每一种恰好归入五类之一：

    没有通道 → 系统保持；只含 P、PR（任一资产）→ 只有回调层；只含 MR → 只有趋势层；
    只含 BW、V → 只有压力层；跨两层及以上 → 多层同时。同层通道对两个资产取并集。
    """
    names = ("P_SPX", "P_QQQ", "PR_SPX", "PR_QQQ", "MR_SPX", "BW_SPX", "BW_QQQ", "V")
    counts = dict.fromkeys(CHANNEL_CLASSES, 0)
    for size in range(len(names) + 1):
        for subset in itertools.combinations(names, size):
            counts[channel_class(subset)] += 1
    # 回调层 4 个通道的非空子集 15 个；趋势层 1 个；压力层 3 个通道的非空子集 7 个；其余 256 − 1 − 15 − 1 − 7 = 232。
    assert counts == {HOLD: 1, PULLBACK: 15, TREND: 1, STRESS: 7, MULTIPLE: 232}
    assert channel_class(("P_SPX", "PR_QQQ")) == PULLBACK and channel_class(("BW_QQQ", "V")) == STRESS
    assert channel_class(("MR_SPX", "V")) == MULTIPLE
    with pytest.raises(PathError, match="未登记"):
        channel_class(("DV_SPX",))
    # 非绿区间恰好一类、绿灯区间不归类；违反即报错。
    classes = channel_classes(("绿", "黄", "红"), ((), (), ("MR_SPX",)))
    assert classes == (None, HOLD, TREND)
    totals = {row.name: (row.days, row.gap) for row in class_totals(classes, (0.0, -0.1, 0.2), ("绿", "黄", "红"))}
    assert totals[HOLD] == (1, -0.1) and totals[TREND] == (1, 0.2) and totals[MULTIPLE] == (0, 0.0)
    with pytest.raises(PathError, match="未归类"):
        class_totals((None, None, TREND), (0.0, -0.1, 0.2), ("绿", "黄", "红"))


def _event(number: int, peak: int, t0: int, trough: int, end: int | None) -> MergedEvent:
    return MergedEvent(number, DAYS[peak], DAYS[t0], DAYS[trough], None if end is None else DAYS[end], end is None, ())


NO_TAIL = {"SPX": frozenset(), "QQQ": frozenset()}


def test_event_position_half_open_intervals_and_end_day() -> None:
    """事件 A：P = 第 22 日，T0 = 第 25 日，Tr = 第 28 日，End = 第 31 日；高点前 20 日 = [第 2 日, 第 22 日)。

    s = 第 22 日 → 确认前（P 当日属于确认前）；第 24 日 → 确认前；第 25 日 → 确认后（T0 当日）；第 27 日 → 确认后；
    第 28 日 → 低点后（Tr 当日）；第 30 日 → 低点后；第 31 日（End 当日）不属于该事件 → 事件外；
    第 2 日 → 高点前 20 日；第 21 日 → 高点前 20 日；第 1 日 → 事件外。
    """
    events = (_event(1, 22, 25, 28, 31),)
    expected = {22: BEFORE_T0, 24: BEFORE_T0, 25: AFTER_T0, 27: AFTER_T0, 28: AFTER_TROUGH, 30: AFTER_TROUGH,
                31: OUTSIDE, 2: PRE_PEAK, 21: PRE_PEAK, 1: OUTSIDE, 35: OUTSIDE}
    for index, category in expected.items():
        position = segment_position(DAYS[index], events, DAYS, NO_TAIL, 20, DAYS[39])
        assert position.category == category, index
        assert position.event == (None if category == OUTSIDE else 1)
        assert position.matches == (() if category == OUTSIDE else (1,))


def test_event_position_priority_with_several_matches() -> None:
    """事件 A：P 5、T0 7、Tr 9、End 16；事件 B：P 12、T0 13、Tr 14、End 18；事件 C：P 30、T0 31、Tr 32、End 33；
    事件 D：P 34。

    s = 第 12 日：A 的低点后 [9, 16) 与 B 的确认前 [12, 13) 同时成立 → 取高点最晚且不晚于 s 的 B → 确认前；
    同时在 C 的高点前 20 日内（[10, 30)）→ 编号保留 A、B、C。
    s = 第 15 日：A 的低点后、B 的低点后 [14, 18) → 取 B。同时处在 C、D 的高点前 20 日内（[10, 30)、[14, 34)）
    → 编号全部保留。
    s = 第 10 日：A 的低点后，同时在 B、C 的高点前 20 日内 → 正在发生的事件优先 → A 的低点后；编号保留 A、B、C。
    s = 第 20 日：没有正在发生的事件；在 C（[10, 30)）与 D（[14, 34)）的高点前 20 日内 → 取高点最早的 C。
    """
    events = (_event(1, 5, 7, 9, 16), _event(2, 12, 13, 14, 18), _event(3, 30, 31, 32, 33), _event(4, 34, 35, 36, 37))
    position = segment_position(DAYS[12], events, DAYS, NO_TAIL, 20, DAYS[39])
    assert (position.category, position.event, position.matches) == (BEFORE_T0, 2, (1, 2, 3))
    position = segment_position(DAYS[15], events, DAYS, NO_TAIL, 20, DAYS[39])
    assert (position.category, position.event, position.matches) == (AFTER_TROUGH, 2, (1, 2, 3, 4))
    position = segment_position(DAYS[10], events, DAYS, NO_TAIL, 20, DAYS[39])
    assert (position.category, position.event, position.matches) == (AFTER_TROUGH, 1, (1, 2, 3))
    position = segment_position(DAYS[20], events, DAYS, NO_TAIL, 20, DAYS[39])
    assert (position.category, position.event, position.matches) == (PRE_PEAK, 3, (3, 4))


def test_event_position_right_censored_and_tail_unknown() -> None:
    """右截尾事件：P 20、T0 22、暂定低点 25、没有结束日；窗口末为第 30 日。

    s = 第 21 日 → 确认前；第 23 日 → 确认后；第 25、29 日 → 低点后（事件未结束）（[暂定低点, 窗口末)）。
    尾段未定：两个资产都把第 33—36 日标为“尾段（寻峰）”，且不属于任何事件 → 事件外（尾段未定）。
    只有一个资产标了的日子，规格没有写明合并口径：报错停下。尾段未定的日子若落在事件里，仍按事件归类。
    """
    events = (_event(1, 20, 22, 25, None),)
    expected = {21: BEFORE_T0, 23: AFTER_T0, 25: AFTER_TROUGH_OPEN, 29: AFTER_TROUGH_OPEN}
    for index, category in expected.items():
        assert segment_position(DAYS[index], events, DAYS, NO_TAIL, 20, DAYS[30]).category == category
    tail = {"SPX": frozenset(DAYS[33:37]), "QQQ": frozenset(DAYS[33:37])}
    position = segment_position(DAYS[34], (_event(1, 5, 6, 7, 8),), DAYS, tail, 20, DAYS[39])
    assert (position.category, position.event, position.matches) == (OUTSIDE_TAIL, None, ())
    assert segment_position(DAYS[32], (_event(1, 5, 6, 7, 8),), DAYS, tail, 20, DAYS[39]).category == OUTSIDE
    with pytest.raises(PathError, match="规格未写明"):
        segment_position(DAYS[34], (), DAYS, {"SPX": frozenset(DAYS[33:37]), "QQQ": frozenset()}, 20, DAYS[39])
    inside = segment_position(DAYS[34], (_event(1, 33, 35, 36, 38),), DAYS,
                              {"SPX": frozenset(DAYS[33:37]), "QQQ": frozenset()}, 20, DAYS[39])
    assert inside.category == BEFORE_T0


def test_bear_market_is_half_open_and_has_priority() -> None:
    """熊市 2000-03-24 至 2002-10-09：区间起点日 d 满足 P ≤ d < Tr。

    2000-03-23 不是熊市（按 2000 年的年度收益，这里给 −10.1% → 下跌年）；2000-03-24（P 当日）是熊市；
    2002-10-08 是熊市；2002-10-09（Tr 当日）不是熊市（2002 年 −23% → 下跌年）。熊市优先于年度标签。
    """
    returns = {2000: -0.101, 2001: -0.13, 2002: -0.23, 2003: 0.26}
    assert environment_of(dt.date(2000, 3, 23), returns, ENVIRONMENT) == DOWN_YEAR
    assert environment_of(dt.date(2000, 3, 24), returns, ENVIRONMENT) == BEAR
    assert environment_of(dt.date(2002, 10, 8), returns, ENVIRONMENT) == BEAR
    assert environment_of(dt.date(2002, 10, 9), returns, ENVIRONMENT) == DOWN_YEAR
    assert environment_of(dt.date(2003, 6, 2), returns, ENVIRONMENT) == UP_YEAR


def test_year_labels_thresholds_and_unavailable_years() -> None:
    """年度收益 = 当年最后一个收盘价 ÷ 上一年最后一个收盘价 − 1；≥ +10% 上涨年，≤ −10% 下跌年，其余平淡年
    （门槛含等号）。

    收盘价：2007 年末 100，2008 年末 90（−10% → 下跌年），2009 年的最后一个交易日 2009-12-31 晚于截止日 2009-09-30 →
    完整年度分类不可得，即使截止日之前有 2009 年的价格（105）也不用部分年度收益代替。
    截止日恰为当年最后一个交易日（2016-12-30）时可以分类。2007 年没有上一年的年末价 → 不可得。
    """
    ends = {2007: dt.date(2007, 12, 31), 2008: dt.date(2008, 12, 31), 2009: dt.date(2009, 12, 31)}
    closes = {ends[2007]: 100.0, ends[2008]: 90.0, dt.date(2009, 9, 30): 105.0}
    returns = annual_returns(closes, ends, dt.date(2009, 9, 30))
    assert returns[2007] is None and returns[2008] == pytest.approx(-0.10) and returns[2009] is None
    assert environment_of(dt.date(2009, 6, 1), returns, EnvironmentSettings((), 0.10, -0.10)) == YEAR_UNAVAILABLE
    late = {2015: dt.date(2015, 12, 31), 2016: dt.date(2016, 12, 30)}
    values = annual_returns({late[2015]: 200.0, late[2016]: 220.0}, late, dt.date(2016, 12, 30))
    assert values[2016] == pytest.approx(0.10)
    plain = EnvironmentSettings((), 0.10, -0.10)
    assert environment_of(dt.date(2016, 5, 2), {2016: 0.10}, plain) == UP_YEAR            # 恰为 +10%
    assert environment_of(dt.date(2016, 5, 2), {2016: -0.10}, plain) == DOWN_YEAR         # 恰为 −10%
    assert environment_of(dt.date(2016, 5, 2), {2016: 0.0999}, plain) == FLAT_YEAR
    assert environment_of(dt.date(2016, 5, 2), {2016: -0.0999}, plain) == FLAT_YEAR
    assert environment_of(dt.date(2016, 5, 2), {}, plain) == YEAR_UNAVAILABLE


def test_case_period_and_environment_summary_on_constructed_window() -> None:
    """窗口 6 个区间，执行 绿、黄、红、绿、绿、黄；案例段取起点日在 [第 11 日, 第 13 日) 的区间（区间 1、2）。

    案例段：2 个区间，都非绿；Σd = [ln(0.99) − ln(0.98)] + [0 − ln(0.97)]；策略对数收益 ln(0.99)；
    满仓对数收益 ln(0.98) + ln(0.97)；平均暴露 0.25；与之相交的非绿段是第 1 段。
    """
    full = (0.01, -0.02, -0.03, 0.02, 0.01, -0.01)
    window = _window(DAYS[10:17], full, cases=((DAYS[11], DAYS[13]),))
    result = analyse_object(ObjectInput("对象", ("绿", "黄", "红", "绿", "绿", "黄", "黄", "黄"), None), window,
                            SETTINGS, ENVIRONMENT)
    (case,) = result.cases
    assert (case.intervals, case.non_green, case.segments) == (2, 2, (1,))
    assert case.gap == pytest.approx(math.log(0.99) - math.log(0.98) - math.log(0.97))
    assert case.strategy_log_return == pytest.approx(math.log(0.99))
    assert case.full_log_return == pytest.approx(math.log(0.98) + math.log(0.97)) and case.mean_exposure == 0.25
    assert result.classes is None and result.totals is None                   # 对照没有通道：不适用
    assert set(result.environments) == {FLAT_YEAR}                            # 构造窗口的 2021 年给的是 +5%
    with pytest.raises(ValueError, match="不在评价窗口内"):
        analyse_object(ObjectInput("对象", ("绿",) * 8, None),
                       _window(DAYS[10:17], full, cases=((DAYS[30], DAYS[32]),)), SETTINGS, ENVIRONMENT)


def test_config_must_equal_the_approved_specification() -> None:
    config = load_failure_path_config(ROOT / "config/wavewarn_v14_failure_path.yaml")
    assert config.settings == SETTINGS and config.pre_peak_days == 20
    with (ROOT / "config/wavewarn_v14_failure_path.yaml").open(encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    for key, value in (("neutral_band", "0.001"), ("short_segment", 10), ("pre_peak_days", 10)):
        with pytest.raises(ValueError, match="规格不一致"):
            parse_failure_path_config({**raw, key: value})
    with pytest.raises(ValueError, match="熊市"):
        parse_failure_path_config({**raw, "environment": {**raw["environment"], "up_year": "0.2"}})
    with pytest.raises(ValueError, match="案例段"):
        parse_failure_path_config({**raw, "development_case_periods": []})


def test_failure_path_algorithm_modules_do_not_import_io_modules() -> None:
    """依赖方向：失败路径分析的纯计算模块不得导入读写模块、services 或 CLI（含传递依赖）。"""
    code = ("import importlib, json, sys\n"
            "for name in ('failure_path', 'failure_path_context', 'failure_path_analysis', 'failure_path_report'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.failure_path_run',"
            " 'market_risk.wavewarn.config_failure_path', 'market_risk.wavewarn.extended_nav_run',"
            " 'market_risk.wavewarn.diagnostics_round2_run', 'market_risk.wavewarn.lock_guard',"
            " 'market_risk.services', 'market_risk.cli') or m.startswith('market_risk.scoring')"
            " or m.startswith('market_risk.storage')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
