"""阶段四描述性对照、对账与报告统计（development_compare）的纯算法测试（M2 第一部分指令第二节第 4 小节第 6 条）。

构造：stock_trading_days(2003-01-02, 2005-12-30) 共 756 个交易日；random_closes 种子 SPX 2、QQQ 3，无缺价；
登记 27 组、开发期用途（与 test_wavewarn_v20_research_run.py 同一构造）。期望值来自设计稿条文与构造输入。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from decimal import Decimal

import pytest
from wavewarn_v20_helpers import random_closes

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn_v20 import development_compare as compare
from market_risk.wavewarn_v20 import registered_v20
from market_risk.wavewarn_v20.execution import Position, SimulatedExecution, TargetSource
from market_risk.wavewarn_v20.inputs import TrendDay
from market_risk.wavewarn_v20.nav import BasketPrefix, NavError, NavResult, portfolio_return
from market_risk.wavewarn_v20.research_run import (
    Continuity,
    Purpose,
    WindowResult,
    WindowSpec,
    candidate_key,
    run_window,
    select_development,
)
from market_risk.wavewarn_v20.snapshot import Snapshot, make_snapshot
from market_risk.wavewarn_v20.state_machine import Risk

D = dt.date
ACQUIRED = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
YEAR_ENDS = {year: max(stock_trading_days(D(year, 1, 1), D(year, 12, 31))) for year in range(2002, 2007)}
TEST_BEAR = (("构造熊市", D(2004, 1, 2), D(2005, 6, 30)),)


@pytest.fixture(scope="module")
def axis() -> tuple[dt.date, ...]:
    return tuple(stock_trading_days(D(2003, 1, 2), D(2005, 12, 30)))


@pytest.fixture(scope="module")
def snapshot(axis: tuple[dt.date, ...]) -> Snapshot:
    closes = {asset: dict(zip(axis, random_closes(seed, len(axis)), strict=True))
              for asset, seed in (("SPX", 2), ("QQQ", 3))}
    return make_snapshot(axis, closes, axis[-1], ACQUIRED, {"SPX": "0" * 64, "QQQ": "0" * 64})


@pytest.fixture(scope="module")
def result(axis: tuple[dt.date, ...], snapshot: Snapshot) -> WindowResult:
    spec = WindowSpec(Purpose.DEVELOPMENT, None, axis[-1], {"SPX": axis[0], "QQQ": axis[0]}, registered_v20.INITIAL,
                      Continuity.COMPLETE_TRADING_AXIS)
    return run_window(snapshot, spec, registered_v20.run_parameters(), registered_v20.CANDIDATES)


@pytest.fixture(scope="module")
def selection(result: WindowResult):
    return select_development(result, registered_v20.TOLERANCE)


def trend(close: str | None, total: str | None) -> TrendDay:
    return TrendDay(D(2004, 1, 2), None if close is None else Decimal(close), None if total is None else Decimal(total),
                    total is not None)


# ---------------------------------------------------------------------------
# 两个均线对照的状态表逐格（设计稿第五节第 1、2 小节）
# ---------------------------------------------------------------------------


def test_one_level_evidence_and_state_table() -> None:
    # 均线完整：Σ = 200 × 100；C = 99.99 低于均线 → 1；C = 100 等于均线（不低于）→ 0；不完整 → 无定义。
    assert compare.one_level_evidence(trend("99.99", "20000"), 200) == 1
    assert compare.one_level_evidence(trend("100", "20000"), 200) == 0
    assert compare.one_level_evidence(trend("100", None), 200) is None
    assert compare.one_level_evidence(trend(None, None), 200) is None
    table = {(previous, level): compare.next_one_level(previous, level)
             for previous in (Risk.NORMAL, Risk.LEVEL1) for level in (None, 1, 0)}
    assert table == {(Risk.NORMAL, None): Risk.NORMAL, (Risk.NORMAL, 1): Risk.LEVEL1, (Risk.NORMAL, 0): Risk.NORMAL,
                     (Risk.LEVEL1, None): Risk.LEVEL1, (Risk.LEVEL1, 1): Risk.LEVEL1, (Risk.LEVEL1, 0): Risk.NORMAL}


def test_buffered_evidence_uses_exact_decimal_boundaries() -> None:
    """20000·C 与 99·Σ、101·Σ 的 Decimal 精确比较；边界恰等于 99%、101% 归带内。Σ = 20000（均线 100）。"""
    cases = {"98.99": compare.Band.BELOW, "99": compare.Band.INSIDE, "99.00000001": compare.Band.INSIDE,
             "100": compare.Band.INSIDE, "101": compare.Band.INSIDE, "101.00000001": compare.Band.ABOVE,
             "101.01": compare.Band.ABOVE}
    for close, expected in cases.items():
        assert compare.band_evidence(trend(close, "20000"), 200) is expected, close
    assert 20000 * Decimal("99") == 99 * Decimal("20000") and 20000 * Decimal("101") == 101 * Decimal("20000")
    assert compare.band_evidence(trend("100", None), 200) is None


def test_buffered_state_table_every_cell() -> None:
    expected = {
        (Risk.NORMAL, None): Risk.NORMAL, (Risk.LEVEL1, None): Risk.LEVEL1, (Risk.LEVEL2, None): Risk.LEVEL2,
        (Risk.NORMAL, compare.Band.INSIDE): Risk.NORMAL, (Risk.LEVEL1, compare.Band.INSIDE): Risk.LEVEL1,
        (Risk.LEVEL2, compare.Band.INSIDE): Risk.LEVEL2,
        (Risk.NORMAL, compare.Band.BELOW): Risk.LEVEL2, (Risk.LEVEL1, compare.Band.BELOW): Risk.LEVEL2,
        (Risk.LEVEL2, compare.Band.BELOW): Risk.LEVEL2,
        (Risk.LEVEL2, compare.Band.ABOVE): Risk.LEVEL1, (Risk.LEVEL1, compare.Band.ABOVE): Risk.NORMAL,
        (Risk.NORMAL, compare.Band.ABOVE): Risk.NORMAL,
    }
    assert {key: compare.next_buffered(*key) for key in expected} == expected


# ---------------------------------------------------------------------------
# 收敛与“不可评价”（第五节共同约定）
# ---------------------------------------------------------------------------


def test_average_comparisons_converge_and_are_evaluated(snapshot: Snapshot, result: WindowResult) -> None:
    window = result.window
    assert window is not None
    for buffered, domain in ((False, (Risk.NORMAL, Risk.LEVEL1)), (True, (Risk.NORMAL, Risk.LEVEL1, Risk.LEVEL2))):
        item = compare.average_comparison(snapshot, result, registered_v20.run_parameters(), buffered)
        assert item.domain == domain and item.evaluable and item.note is None
        assert item.convergence_index is not None and item.convergence_index <= window.j0 - 1
        assert item.days[0].day == snapshot.days[window.t0 + 1]                # 自 t0+1 起逐日更新
        assert [record.day for record in item.signals] == list(snapshot.days[window.j0 - 1:window.e_index])
        assert [target.day for target in item.targets] == list(snapshot.days[window.j0:])
        assert isinstance(item.signal_nav, NavResult) and item.policy is not None
        # 对照的收敛不参与 κ_全 与 j₀：组合层的窗口不变。
        assert result.window is window


def test_average_comparison_not_evaluable_when_converging_after_j0(snapshot: Snapshot, result: WindowResult) -> None:
    window = result.window
    assert window is not None
    early = dataclasses.replace(result, window=dataclasses.replace(window, j0=window.t0 + 1))
    item = compare.average_comparison(snapshot, early, registered_v20.run_parameters(), True)
    assert not item.evaluable and item.note == compare.NOT_EVALUABLE
    assert item.signal_nav is None and item.policy is None and item.targets == ()


# ---------------------------------------------------------------------------
# 恒定仓位（第五节第 3 小节）
# ---------------------------------------------------------------------------


def test_constant_exposure_averages_and_portfolio_return(result: WindowResult, selection) -> None:
    parameters = registered_v20.run_parameters()
    assert result.reference is not None and result.common is not None and result.window is not None
    targets = result.reference.outcome.targets
    n = result.window.n
    item = compare.constant_exposure("主参照", targets, result, parameters)
    core = math.fsum(target.core for target in targets[:n]) / n
    leverage = math.fsum(target.leverage for target in targets[:n]) / n
    assert (item.core, item.leverage, item.exposure) == (core, leverage, core + 2.0 * leverage)
    assert isinstance(item.nav, compare.PathNav)
    values = result.common.prefix.values
    assert item.nav.returns == tuple(portfolio_return(core, leverage, 2.0, value) for value in values)
    assert item.nav.wealth[0] == 1.0 and len(item.nav.wealth) == n + 1
    items = compare.constant_exposures(result, selection, parameters)
    assert items[0].object == "主参照"
    if selection.selection.selected is not None:
        assert items[1].object == candidate_key(selection.selection.selected) and items[1].computed
    else:
        assert items[1].note == compare.NO_SELECTION and not items[1].computed


def test_constant_exposure_failure_and_zero_leverage(result: WindowResult) -> None:
    """w̄_l = 0 时不检查 1 + λU；w̄_l > 0 且 1 + λU ≤ 0 时 portfolio_return 抛 NavError（计算失败）。"""
    parameters = registered_v20.run_parameters()
    assert result.common is not None and result.window is not None and result.reference is not None
    n = result.window.n
    crash = list(result.common.prefix.values)
    crash[5] = -0.6                                                            # 1 + 2 × (−0.6) < 0
    crashed = dataclasses.replace(result, common=dataclasses.replace(
        result.common, prefix=BasketPrefix(tuple(crash), None, ())))
    level1 = tuple(dataclasses.replace(target, position=Position.LEVEL1, core=0.6, leverage=0.0)
                   for target in result.reference.outcome.targets)
    zero = compare.constant_exposure("一级恒定", level1, crashed, parameters)
    assert zero.leverage == 0.0 and isinstance(zero.nav, compare.PathNav) and zero.nav.returns[5] == 0.6 * -0.6
    normal = tuple(dataclasses.replace(target, position=Position.NORMAL, core=0.6, leverage=0.4)
                   for target in result.reference.outcome.targets)
    with pytest.raises(NavError, match="不为正"):
        compare.constant_exposure("正常恒定", normal[:n + 1], crashed, parameters)
    missing = dataclasses.replace(result, common=dataclasses.replace(
        result.common, prefix=BasketPrefix(tuple(result.common.prefix.values[:3]), 4, (("SPX", D(2004, 6, 1)),))))
    item = compare.constant_exposure("主参照", result.reference.outcome.targets, missing, parameters)
    assert not item.computed and item.note == compare.NOT_COMPUTABLE


# ---------------------------------------------------------------------------
# 暴露替换（第五节第 5 小节）
# ---------------------------------------------------------------------------


def test_exposure_substitution_selection_correspondence_and_sums(snapshot: Snapshot, result: WindowResult) -> None:
    parameters = registered_v20.run_parameters()
    rebuilt = compare.rebuild_states(snapshot, result, parameters)
    items = {item.candidate: item for item in compare.substitutions(result, rebuilt, parameters, TEST_BEAR)}
    assert result.common is not None
    values = result.common.prefix.values
    total = 0
    for candidate, outcome in result.candidates.items():
        item = items[candidate_key(candidate)]
        evidence = {proof.day: proof for proof in rebuilt.evidence[(candidate.k, candidate.theta)]}
        days = [target.day for target in outcome.outcome.targets]
        expected = []
        for interval in range(1, len(days)):
            signal = outcome.outcome.signals[interval - 1]
            assert signal.day < days[interval - 1]                              # 信号日为区间起点的前一交易日
            proof = evidence[signal.day]
            if (signal.risk is Risk.LEVEL1 and proof.mr.active and not proof.pr_spx.active
                    and not proof.pr_qqq.active and D(2004, 1, 2) <= days[interval - 1] < D(2005, 6, 30)):
                expected.append(interval)
        assert item.intervals == tuple((days[i - 1], days[i]) for i in expected)
        assert item.registered_log == math.fsum(math.log1p(portfolio_return(0.6, 0.0, 2.0, values[i - 1]))
                                                for i in expected)
        assert item.substituted_log == math.fsum(math.log1p(portfolio_return(0.3, 0.0, 2.0, values[i - 1]))
                                                 for i in expected)
        assert item.difference == item.substituted_log - item.registered_log
        total += len(expected)
    assert total > 0                                                           # 构造中确有被筛出的区间


def test_state_rebuild_mismatch_is_a_calculation_failure(snapshot: Snapshot, result: WindowResult) -> None:
    candidate, outcome = next(iter(result.candidates.items()))
    changed = list(outcome.system)
    day = changed[3]
    changed[3] = dataclasses.replace(day, risk=Risk.LEVEL2 if day.risk is not Risk.LEVEL2 else Risk.NORMAL)
    broken = dataclasses.replace(result, candidates={**result.candidates,
                                                     candidate: dataclasses.replace(outcome, system=tuple(changed))})
    with pytest.raises(compare.ComparisonError, match="无法按同参同切片重建"):
        compare.rebuild_states(snapshot, broken, registered_v20.run_parameters())
    assert result.reference is not None
    reference_days = list(result.reference.days)
    reference_days[2] = dataclasses.replace(reference_days[2], risk=Risk.LEVEL2 if reference_days[2].risk is not
                                            Risk.LEVEL2 else Risk.NORMAL)
    broken = dataclasses.replace(result, reference=dataclasses.replace(result.reference, days=tuple(reference_days)))
    with pytest.raises(compare.ComparisonError, match="主参照"):
        compare.rebuild_states(snapshot, broken, registered_v20.run_parameters())


# ---------------------------------------------------------------------------
# 市场环境与边界日（设计稿第一节第 2 小节）
# ---------------------------------------------------------------------------


def test_bear_condition_boundaries() -> None:
    bears = registered_v20.BEARS
    assert compare.in_bear(D(2000, 3, 24), bears) and not compare.in_bear(D(2002, 10, 9), bears)   # P ≤ d < Tr
    assert compare.in_bear(D(2002, 10, 8), bears) and not compare.in_bear(D(2000, 3, 23), bears)
    assert compare.in_bear(D(2007, 10, 9), bears) and not compare.in_bear(D(2009, 3, 9), bears)


def test_year_return_boundaries_and_unavailable_years() -> None:
    days = (D(2002, 12, 31), D(2003, 12, 31), D(2004, 12, 31), D(2005, 12, 30))
    closes = {"SPX": {days[0]: Decimal("100"), days[1]: Decimal("110"), days[2]: Decimal("99")},
              "QQQ": {day: Decimal("1") for day in days}}
    small = make_snapshot(days, closes, days[-1], ACQUIRED, {"SPX": "0" * 64, "QQQ": "0" * 64})
    ends = {2002: days[0], 2003: days[1], 2004: days[2], 2005: days[3], 2006: D(2006, 12, 29)}
    assert compare.year_return(small, 2003, ends, "SPX") == Decimal("0.1")       # 恰 +10%：上涨年
    assert compare.year_return(small, 2004, ends, "SPX") == Decimal("-0.1")      # 恰 −10%：下跌年
    assert compare.year_return(small, 2005, ends, "SPX") is None                 # 2005 年末收盘价缺失
    assert compare.year_return(small, 2006, ends, "SPX") is None                 # 年末晚于截止日
    assert compare.year_return(small, 2002, ends, "SPX") is None                 # 前一年不在轴上


def test_environments_classify_interval_starts(snapshot: Snapshot, result: WindowResult) -> None:
    rows = compare.environments(snapshot, result, TEST_BEAR, YEAR_ENDS, Decimal("0.10"), Decimal("-0.10"), "SPX")
    window = result.window
    assert window is not None and len(rows) == window.n
    for row in rows:
        assert row.start == snapshot.days[window.j0 + row.interval - 1]         # 起点归属
        if D(2004, 1, 2) <= row.start < D(2005, 6, 30):
            assert row.category == compare.BEAR
        else:
            value = compare.year_return(snapshot, row.start.year, YEAR_ENDS, "SPX")
            assert row.year_return == value
            assert row.category == (compare.UNKNOWN_YEAR if value is None else compare.UP if value >= Decimal("0.10")
                                    else compare.DOWN if value <= Decimal("-0.10") else compare.FLAT)
    assert result.common is not None and result.reference is not None
    summary = compare.environment_sums(rows, {"主参照": result.reference.outcome.signal_nav, "缺": None})
    assert sum(entry["intervals"] for entry in summary.values()) == window.n
    nav = result.reference.outcome.signal_nav
    assert isinstance(nav, NavResult)
    assert math.isclose(math.fsum(entry["log_return_sums"]["主参照"] for entry in summary.values()),
                        math.fsum(math.log1p(value) for value in nav.returns), abs_tol=1e-12)
    assert all(entry["log_return_sums"]["缺"] is None for entry in summary.values())


# ---------------------------------------------------------------------------
# 对账（第六节）：恰为 1e-10 通过、略超 1e-10 失败
# ---------------------------------------------------------------------------


def test_single_path_tolerance_boundary() -> None:
    assert compare._single("构造", "信号模拟", (), 1e-10, 1e-10).passed                 # abs(0 − 1e-10) = 1e-10
    assert not compare._single("构造", "信号模拟", (), math.nextafter(1e-10, 1.0), 1e-10).passed


def nav(days: tuple[dt.date, ...], returns: tuple[float, ...], log_wealth: float) -> NavResult:
    executions = tuple(SimulatedExecution(day, Position.NORMAL, 0.6, 0.4, 1.0) for day in days)
    return NavResult(executions, returns, math.fsum(math.log1p(value) for value in returns), log_wealth)


def test_relative_tolerance_boundary_and_day_check() -> None:
    days = (D(2004, 1, 2), D(2004, 1, 5))
    reference = nav(days, (0.0,), 0.0)
    assert compare.relative_row("构造", nav(days, (0.0,), -1e-10), reference, 1e-10).passed
    assert not compare.relative_row("构造", nav(days, (0.0,), -math.nextafter(1e-10, 1.0)), reference, 1e-10).passed
    with pytest.raises(compare.ComparisonError, match="执行日序列不同"):
        compare.relative_row("构造", nav((D(2004, 1, 2), D(2004, 1, 6)), (0.0,), 0.0), reference, 1e-10)


def test_reconcile_paths_passes_and_fails(snapshot: Snapshot, result: WindowResult, selection) -> None:
    parameters = registered_v20.run_parameters()
    averages = tuple(compare.average_comparison(snapshot, result, parameters, flag) for flag in (False, True))
    constants = compare.constant_exposures(result, selection, parameters)
    rows = compare.reconcile_paths(result, averages, constants, 1e-10)
    assert all(row.passed for row in rows)
    kinds = {(row.kind, row.path) for row in rows}
    assert ("单一路径", "信号模拟") in kinds and ("单一路径", "执行政策研究模拟") in kinds
    assert ("单一路径", "一直持有") in kinds and ("单一路径", "恒定仓位") in kinds
    assert ("相对主参照", "信号模拟") in kinds
    candidate, outcome = next(iter(result.candidates.items()))
    signal_nav = outcome.outcome.signal_nav
    assert isinstance(signal_nav, NavResult)
    shifted = dataclasses.replace(signal_nav, log_wealth=signal_nav.log_wealth + 1e-9)
    broken = dataclasses.replace(result, candidates={**result.candidates, candidate: dataclasses.replace(
        outcome, outcome=dataclasses.replace(outcome.outcome, signal_nav=shifted))})
    with pytest.raises(compare.ComparisonError, match="对账不符"):
        compare.reconcile_paths(broken, averages, constants, 1e-10)


# ---------------------------------------------------------------------------
# 报告统计（第十一节；四项统计依补充二第二节）
# ---------------------------------------------------------------------------


def targets_for(risks: list[Risk]) -> list:
    from market_risk.wavewarn_v20.execution import PlannedTarget

    position = {Risk.NORMAL: Position.NORMAL, Risk.LEVEL1: Position.LEVEL1, Risk.LEVEL2: Position.LEVEL2}
    return [PlannedTarget(D(2004, 1, 1) + dt.timedelta(days=index), position[risk], 0.0, 0.0, TargetSource.SYSTEM,
                          False) for index, risk in enumerate(risks)]


def statistics_of(sequence: list[Risk]) -> compare.StateStatistics:
    """sequence 为 axis[j₀−2 .. E] 的状态；区间 i 的持有目标由信号日 axis[j₀−2+i] 的状态决定。"""
    days = [D(2004, 1, 1) + dt.timedelta(days=index) for index in range(len(sequence))]
    n = len(sequence) - 2
    return compare.state_statistics("构造", list(zip(days, sequence, strict=True)), targets_for(sequence[1:]), n)


def test_example_one_direct_entry_and_example_four_via_level_one() -> None:
    """登记第一节第 4 小节例 1（正常 → 二级：由正常直接进入二级）与例 4（一级 → 二级：经一级再入二级）。"""
    N, L1, L2 = Risk.NORMAL, Risk.LEVEL1, Risk.LEVEL2
    example_one = statistics_of([N, N, L2, L2, N, N])
    assert len(example_one.direct_level2_days) == 1 and example_one.via_level1_days == ()
    example_four = statistics_of([N, L1, L2, L2, L1, N])
    assert example_four.direct_level2_days == () and len(example_four.via_level1_days) == 1
    # 段数等于这类转换次数：两次经一级再入二级。
    twice = statistics_of([N, L1, L2, L1, L2, N, N])
    assert len(twice.via_level1_days) == 2 and len(twice.level2_runs) == 2


def test_window_starting_in_level_two_is_truncated_not_counted() -> None:
    """窗口首日已处于二级：前一日（axis[j₀−2]）也为二级，不计入直接进入或经一级；首段二级执行区间标“左截断”。"""
    L2, L1, N = Risk.LEVEL2, Risk.LEVEL1, Risk.NORMAL
    stats = statistics_of([L2, L2, L2, L1, N, N])
    assert stats.direct_level2_days == () and stats.via_level1_days == ()
    assert stats.level2_runs[0].left_truncated and stats.level2_runs[0].intervals == 2
    assert stats.level1_runs[0].intervals == 1 and not stats.level1_runs[0].left_truncated
    ending = statistics_of([N, N, N, L1, L1])
    assert ending.level1_runs[0].right_truncated


def test_policy_events_and_differences(result: WindowResult) -> None:
    for outcome in result.candidates.values():
        events = compare.policy_events(outcome.outcome.policy)
        kinds = [event.kind for event in events]
        assert kinds.count("止损执行") == sum(1 for target in outcome.outcome.policy.targets
                                              if target.source is TargetSource.STOP_CASH)
        difference = compare.policy_minus_signal(outcome.outcome.signal_nav, outcome.outcome.policy)
        assert difference["note"] == compare.POLICY_DIFFERENCE_NOTE
        policy, signal_nav = outcome.outcome.policy, outcome.outcome.signal_nav
        assert difference["difference"] == policy.nav.log_wealth - signal_nav.log_wealth
    assert compare.policy_events(None) == ()


def test_descriptive_collects_every_item(snapshot: Snapshot, result: WindowResult, selection) -> None:
    marks: list[str] = []
    found = compare.descriptive(snapshot, result, selection, registered_v20.run_parameters(), registered_v20.BEARS,
                                YEAR_ENDS, registered_v20.ENVIRONMENT_UP, registered_v20.ENVIRONMENT_DOWN, "SPX",
                                marks.append)
    assert marks == ["均线对照", "恒定仓位", "状态重建", "暴露替换", "市场环境", "报告统计"]
    assert [item.name for item in found.averages] == [compare.ONE_LEVEL, compare.BUFFERED]
    assert len(found.substitutions) == 27 and len(found.statistics) == 28
    assert set(found.policy_events) == set(found.policy_differences)
