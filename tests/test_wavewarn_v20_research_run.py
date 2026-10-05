"""研究组合层的构造验收（《研究组合层设计 修订三》第十五节；实现指令第五节）。纯算法测试。

层级：L1 组合层端到端（真实构造交易日期的价格路径，经 run_window）；L2 已有算法函数的局部构造；
L3 替身验证异常传播与出口。L2、L3 不宣称证明完整价格路径。
预期值写在测试内并注明设计条目号，来自登记条文与构造输入，不来自独立工具。
构造轴：stock_trading_days(2003-01-02, 2005-12-30)，共 756 个交易日；价格路径 random_closes 种子 SPX 2、QQQ 3，无缺价。
本文件导入 NYSE 日历库生成构造轴（market_risk.calendar 的导入会加载 pandas_market_calendars）。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from decimal import Decimal

import pytest
from wavewarn_v20_helpers import POLICY, POSITIONS, TOLERANCE, WINDOWS, random_closes

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn_v20 import research_run
from market_risk.wavewarn_v20.channels import PULLBACK_DOMAIN, TREND_DOMAIN
from market_risk.wavewarn_v20.convergence import (
    EMPTY_AT_LAST_DAY,
    EMPTY_BEYOND_AXIS,
    REGISTERED_CHANNELS,
    REGISTERED_REFERENCE,
    REGISTERED_SYSTEM,
    Candidate,
    EmptyWindowError,
    NoStartError,
    NotConvergedError,
    channel_initial_states,
    common_start_index,
    first_valid_index,
    pullback_convergence,
    reference_convergence,
    reference_initial_states,
    system_convergence,
    system_initial_states,
    trend_convergence,
)
from market_risk.wavewarn_v20.data_v20 import check_trading_axis
from market_risk.wavewarn_v20.inputs import TrendDay, snapshot_asset_days, snapshot_trend_days
from market_risk.wavewarn_v20.labels_r2 import REASON_MISSING_PRICE, R2Thresholds
from market_risk.wavewarn_v20.nav import NavError, NavResult, PartialPolicyResult, PolicyResult
from market_risk.wavewarn_v20.r2 import Prompt, R2Rule, R2Undeterminable, SegmentClass
from market_risk.wavewarn_v20.research_run import (
    REGISTERED_CANDIDATES,
    Continuity,
    Coverage,
    Exit,
    InitialStates,
    Purpose,
    ResearchRunError,
    RunParameters,
    SegmentSpec,
    Unavailable,
    WindowResult,
    WindowSpec,
    confirmatory_input,
    run_window,
    select_development,
)
from market_risk.wavewarn_v20.selection import Outcome, select
from market_risk.wavewarn_v20.snapshot import Snapshot, make_snapshot
from market_risk.wavewarn_v20.state_machine import Risk, SystemState, evidence_series, run_channels

FIRST = dt.date(2003, 1, 2)
END = dt.date(2005, 12, 30)
SEEDS = {"SPX": 2, "QQQ": 3}
ACQUIRED = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
SUBSET = (Candidate(3, Decimal("0.015"), 1), Candidate(10, Decimal("0.025"), 5))      # 构造子集（登记域内）
INITIAL = InitialStates(REGISTERED_CHANNELS, REGISTERED_SYSTEM, REGISTERED_REFERENCE)
THRESHOLDS = R2Thresholds(Decimal("0.95"), Decimal("1.05"), Decimal("0.97"))
RULE = R2Rule(3, 5, 20)


def parameters(segments: tuple[SegmentSpec, ...] = (), diagnostics: bool = False) -> RunParameters:
    """登记的运行参数（设计第四节）：63/19/15/200，仓位与止损，R2 门槛与规则，offset 63，容差 1e-10，R1 比例 0.5。"""
    return RunParameters(WINDOWS, POSITIONS, POLICY, THRESHOLDS, RULE, 63, TOLERANCE, 0.5, segments, diagnostics)


def build(axis: tuple[dt.date, ...], closes: dict[str, list[Decimal | None]],
          starts: dict[str, dt.date] | None = None) -> Snapshot:
    """构造入口：先对两资产调用 check_trading_axis（自登记起点至 E），再 make_snapshot。"""
    starts = starts or {"SPX": axis[0], "QQQ": axis[0]}
    for asset in ("SPX", "QQQ"):
        check_trading_axis(asset, starts[asset], axis[-1], [day for day in axis if day >= starts[asset]])
    prices = {asset: {day: value for day, value in zip(axis, values, strict=True) if value is not None}
              for asset, values in closes.items()}
    return make_snapshot(axis, prices, axis[-1], ACQUIRED, {"SPX": "0" * 64, "QQQ": "0" * 64})


def spec(axis: tuple[dt.date, ...], purpose: Purpose = Purpose.CONSTRUCTED, first: dt.date | None = None,
         starts: dict[str, dt.date] | None = None) -> WindowSpec:
    return WindowSpec(purpose, first, axis[-1], starts or {"SPX": axis[0], "QQQ": axis[0]}, INITIAL,
                      Continuity.COMPLETE_TRADING_AXIS)


@pytest.fixture(scope="module")
def axis() -> tuple[dt.date, ...]:
    days = tuple(stock_trading_days(FIRST, END))
    assert len(days) == 756 and days[0] == FIRST and days[-1] == END
    return days


@pytest.fixture(scope="module")
def closes(axis: tuple[dt.date, ...]) -> dict[str, list[Decimal | None]]:
    return {asset: random_closes(seed, len(axis)) for asset, seed in SEEDS.items()}


@pytest.fixture(scope="module")
def snapshot(axis: tuple[dt.date, ...], closes: dict[str, list[Decimal | None]]) -> Snapshot:
    return build(axis, closes)


@pytest.fixture(scope="module")
def base(axis: tuple[dt.date, ...], snapshot: Snapshot) -> WindowResult:
    """构造子集 2 组，构造验收用途，共同起点。"""
    return run_window(snapshot, spec(axis), parameters(), SUBSET)


@pytest.fixture(scope="module")
def development(axis: tuple[dt.date, ...], snapshot: Snapshot) -> WindowResult:
    """登记 27 组，开发期用途。"""
    return run_window(snapshot, spec(axis, Purpose.DEVELOPMENT), parameters(), REGISTERED_CANDIDATES)


# ---------------------------------------------------------------------------
# #1（L1）t0、全局与局部对齐、j₀、n、两种切片
# ---------------------------------------------------------------------------


def expected_system_index(snapshot: Snapshot, t0: int, candidate: Candidate) -> int:
    """设计第十五节 #1：system_index = 局部 run_system 枚举结果 + 偏移（κ_通道 + 1）。"""
    spx, qqq = (snapshot_asset_days(snapshot, asset, WINDOWS)[t0 + 1:] for asset in ("SPX", "QQQ"))
    trend = snapshot_trend_days(snapshot, "SPX", WINDOWS)[t0 + 1:]
    k, theta = candidate.k, candidate.theta
    kappa = t0 + 1 + max(pullback_convergence(spx, k, theta, "P_SPX"), pullback_convergence(qqq, k, theta, "P_QQQ"),
                         pullback_convergence(spx, k, 2 * theta, "PR_SPX"),
                         pullback_convergence(qqq, k, 2 * theta, "PR_QQQ"), trend_convergence(trend, 200))
    evidence = evidence_series(spx, qqq, trend, run_channels(REGISTERED_CHANNELS, spx, qqq, trend, k, theta, 200))
    return kappa + 1 + system_convergence(evidence[kappa - t0:], k, candidate.h)


def test_l1_window_alignment_and_two_slices(axis: tuple[dt.date, ...], snapshot: Snapshot,
                                             base: WindowResult) -> None:
    assert base.stop is None and base.window is not None and base.reference is not None
    window, e_index = base.window, len(axis) - 1
    # t0 = 行号 199（第 200 个交易日，200 日均线首次完整；63、19 日窗口更早完整，无缺价）。
    assert window.t0 == 199
    assert window.t0 == first_valid_index(snapshot_asset_days(snapshot, "SPX", WINDOWS),
                                          snapshot_asset_days(snapshot, "QQQ", WINDOWS),
                                          snapshot_trend_days(snapshot, "SPX", WINDOWS))
    for candidate in SUBSET:
        assert window.convergences[candidate].system_index == expected_system_index(snapshot, window.t0, candidate)
    reference_index = window.t0 + 1 + reference_convergence(snapshot_trend_days(snapshot, "SPX", WINDOWS)[200:], 200)
    assert window.reference_index == reference_index
    kappa_all = max([*(window.convergences[c].system_index for c in SUBSET), reference_index])
    assert window.kappa_all == kappa_all
    assert window.j0 == window.j0_min == max(window.t0 + 63, kappa_all + 1)       # j₀ = max(t0 + 63, κ_全 + 1)
    assert window.e_index == e_index and window.n == e_index - window.j0
    assert (window.first_day, window.last_day) == (axis[window.j0], END)
    assert (window.history_first, window.history_last) == (axis[0], END)
    j0, days = window.j0, axis[window.j0:]
    objects = [(research_run.candidate_key(c), base.candidates[c].outcome, base.candidates[c].system) for c in SUBSET]
    objects.append(("主参照", base.reference.outcome, base.reference.days))
    for name, outcome, full in objects:
        signals = outcome.signals
        # 净值模拟信号：axis[j0−1 .. E−1]，长度 = len(days)，逐日期对应。
        assert len(signals) == len(days), name
        assert signals[0].day == axis[j0 - 1], name
        assert signals[-1].day == axis[e_index - 1], name
        assert all(signals[i + 1].day == days[i] for i in range(len(days) - 1)), name
        # 完整逐日记录：axis[j0−2 .. E]，共 n + 3 条，末条为 E 日（不截掉 E）。
        assert full[0].day == axis[j0 - 2], name
        assert full[-1].day == END, name
        assert len(full) == window.n + 3, name
        assert [item.day for item in full] == list(axis[j0 - 2:]), name
    assert len(base.reference.all_valid) == window.n + 3
    assert [record.candidate for record in base.records] == list(SUBSET)


# ---------------------------------------------------------------------------
# #1b（L1）开发期完整 27 组组合调用
# ---------------------------------------------------------------------------


def test_l1_development_twenty_seven_candidates_and_selection(base: WindowResult,
                                                              development: WindowResult) -> None:
    assert development.stop is None and development.window is not None and base.window is not None
    assert [record.order for record in development.records] == list(range(27))
    assert [record.candidate for record in development.records] == list(REGISTERED_CANDIDATES)
    assert len(development.candidates) == 27
    chosen = select_development(development, TOLERANCE)
    result = chosen.selection
    assert chosen.window is development and chosen.tolerance == TOLERANCE
    assert isinstance(result.outcome, Outcome) and len(result.records) == 27
    candidates = {record.candidate for record in development.records}
    if result.outcome is Outcome.SELECTED:
        assert result.selected in result.feasible and set(result.feasible) <= candidates
        assert set(result.tied) <= set(result.feasible)
        logs = {record.candidate: record.log_wealth for record in development.records}
        assert result.maximum == max(logs[c] for c in result.feasible)
    else:
        assert result.outcome in (Outcome.UNEVALUABLE, Outcome.NO_FEASIBLE) and result.selected is None
    # 共用起点规则：27 组的 j₀ 按登记 27 组与主参照计算（不要求与 2 组子集相同）。
    assert development.window.start_basis.endswith("按登记 27 组与主参照计算")
    with pytest.raises(ResearchRunError):
        select_development(base, TOLERANCE)                                     # 非开发期用途


# ---------------------------------------------------------------------------
# #2（L2 / L3）评价窗口为空两种细分、未收敛、无 t0
# ---------------------------------------------------------------------------


def test_l2_start_index_errors() -> None:
    with pytest.raises(EmptyWindowError) as at_last:
        common_start_index(10, [20], 63, 74)                                    # j₀ = 73 = E
    assert at_last.value.detail == EMPTY_AT_LAST_DAY
    with pytest.raises(EmptyWindowError) as beyond:
        common_start_index(10, [20], 63, 73)                                    # j₀ = 73 > E = 72
    assert beyond.value.detail == EMPTY_BEYOND_AXIS
    day = dt.date(2003, 1, 2)
    incomplete = [TrendDay(day + dt.timedelta(days=i), None, None, False) for i in range(50)]
    with pytest.raises(NotConvergedError):
        reference_convergence(incomplete, 200)                                  # 均线始终不完整，三种初始值不相遇
    with pytest.raises(NoStartError):
        first_valid_index([], [], [])


def test_l3_stop_exits_for_start_errors(axis: tuple[dt.date, ...], closes: dict[str, list[Decimal | None]],
                                        base: WindowResult, monkeypatch: pytest.MonkeyPatch) -> None:
    assert base.window is not None
    j0 = base.window.j0

    def truncated(last: int) -> tuple[tuple[dt.date, ...], Snapshot]:
        days = axis[:last + 1]
        return days, build(days, {asset: values[:last + 1] for asset, values in closes.items()})

    days, short = truncated(150)                                                # 不足 200 日：无 t0
    stopped = run_window(short, spec(days), parameters(), SUBSET)
    assert stopped.stop is not None and stopped.stop.exit is Exit.NO_START and stopped.window is None
    for last, detail in ((j0, EMPTY_AT_LAST_DAY), (j0 - 1, EMPTY_BEYOND_AXIS)):
        days, short = truncated(last)
        stopped = run_window(short, spec(days), parameters(), SUBSET)
        assert stopped.stop is not None and stopped.stop.exit is Exit.EMPTY_WINDOW
        assert stopped.stop.detail["detail"] == detail and stopped.stop.exception_type == "EmptyWindowError"

    def never(*_: object) -> None:
        raise NotConvergedError("构造：始终不收敛")

    monkeypatch.setattr(research_run, "candidate_convergence", never)
    stopped = run_window(build(axis, closes), spec(axis), parameters(), SUBSET)   # 收敛函数替身
    assert stopped.stop is not None and stopped.stop.exit is Exit.NOT_CONVERGED
    assert stopped.stop.reason_code == "始终不收敛" and stopped.candidates == {}


# ---------------------------------------------------------------------------
# #3（L1）缺价分层
# ---------------------------------------------------------------------------


def test_l1_missing_price_layers(axis: tuple[dt.date, ...], closes: dict[str, list[Decimal | None]],
                                 base: WindowResult) -> None:
    assert base.window is not None
    j0 = base.window.j0
    gap = j0 + 100
    changed = {asset: list(values) for asset, values in closes.items()}
    changed["SPX"][gap] = None
    result = run_window(build(axis, changed), spec(axis), parameters(), SUBSET)
    assert result.stop is None and result.window is not None and result.window.j0 == j0
    assert result.common is not None and result.reference is not None
    missing = (("SPX", axis[gap]),)                                            # 构造的缺价（资产，日期）集合
    # 甲线补充裁决一第三条：预先分层的结构化原因。
    expected = Unavailable(missing, "缺少必需价格", None, "basket_prefix")
    assert result.common.hold == expected
    assert (result.common.hold.reason_code, result.common.hold.source) == ("缺少必需价格", "basket_prefix")
    assert result.common.hold.exception_type is None and result.common.hold.missing == missing
    assert result.common.prefix.first_missing == 100
    for candidate in SUBSET:
        outcome = result.candidates[candidate]
        assert len(outcome.outcome.targets) == len(axis) - j0                  # 信号目标照常生成
        signal_nav = outcome.outcome.signal_nav
        assert signal_nav == expected
        assert isinstance(signal_nav, Unavailable) and signal_nav.reason_code == "缺少必需价格"
        assert signal_nav.source == "basket_prefix" and signal_nav.exception_type is None
        assert set(signal_nav.missing) == set(missing)
        assert isinstance(outcome.outcome.policy, PartialPolicyResult)
        assert outcome.r1.computable is False and outcome.record.r1 is None and outcome.record.log_wealth is None
        signal = next(item for item in outcome.outcome.signals if item.day == axis[gap])
        assert signal.all_valid is False                                       # P_SPX、PR_SPX、MR 当日无效
    assert result.reference.all_valid[gap - (j0 - 2)] is False
    assert next(item for item in result.reference.outcome.signals if item.day == axis[gap]).all_valid is False
    assert isinstance(result.reference.outcome.policy, PartialPolicyResult)


# ---------------------------------------------------------------------------
# #4（L1）QQQ 起点当日缺价
# ---------------------------------------------------------------------------


def test_l1_qqq_missing_on_its_history_start(axis: tuple[dt.date, ...],
                                             closes: dict[str, list[Decimal | None]]) -> None:
    start = axis[20]
    changed = {"SPX": list(closes["SPX"]), "QQQ": [None] * 21 + list(closes["QQQ"][21:])}
    starts = {"SPX": axis[0], "QQQ": start}
    result = run_window(build(axis, changed, starts), spec(axis, starts=starts), parameters(), SUBSET)
    assert result.stop is None and result.common is not None
    events = result.common.events["QQQ"]
    assert isinstance(events, Unavailable) and events.reason_code == REASON_MISSING_PRICE == "缺少必需价格"
    # 甲线补充裁决一第三条：标签路径的结构化原因。
    assert events.exception_type == "MissingPriceError" and events.source == "labels_r2.r2_events"
    assert events.missing == (("QQQ", start),)                                 # 标签史首行即起点当日，价格为 None
    assert ("QQQ", start) in events.missing
    assert isinstance(result.common.events["SPX"], tuple)
    assert all(result.candidates[c].record.r2["QQQ"] is None for c in SUBSET)
    chosen = select(result.records, False, TOLERANCE)
    assert chosen.outcome is Outcome.UNEVALUABLE and len(chosen.records) == len(SUBSET)


# ---------------------------------------------------------------------------
# #5（L2）起点不在轴内、缺历史起点
# ---------------------------------------------------------------------------


def test_l2_history_start_checks(axis: tuple[dt.date, ...], snapshot: Snapshot) -> None:
    for starts in ({"SPX": axis[0]}, {"SPX": axis[0], "QQQ": axis[0] - dt.timedelta(days=1)},
                   {"SPX": axis[0], "QQQ": dt.date(2003, 1, 4)}):                # 缺 QQQ；早于轴首；星期六
        with pytest.raises(ResearchRunError):
            run_window(snapshot, spec(axis, starts=starts), parameters(), SUBSET)


# ---------------------------------------------------------------------------
# #6（L1）用途字段：开发期入口拒绝
# ---------------------------------------------------------------------------


def test_l1_development_entry_rejections(axis: tuple[dt.date, ...], snapshot: Snapshot, base: WindowResult) -> None:
    with pytest.raises(ResearchRunError, match="开发期"):
        select_development(base, TOLERANCE)
    out_of_domain = (Candidate(4, Decimal("0.015"), 1), *REGISTERED_CANDIDATES[1:])
    for candidates in (SUBSET, tuple(reversed(REGISTERED_CANDIDATES)), out_of_domain):
        with pytest.raises(ResearchRunError):
            run_window(snapshot, spec(axis, Purpose.DEVELOPMENT), parameters(), candidates)


# ---------------------------------------------------------------------------
# #7（L1）固定起点
# ---------------------------------------------------------------------------


def test_l1_fixed_start(axis: tuple[dt.date, ...], snapshot: Snapshot, base: WindowResult) -> None:
    assert base.window is not None
    j0_min = base.window.j0_min
    early = run_window(snapshot, spec(axis, Purpose.FROZEN, axis[j0_min - 1]), parameters(), SUBSET)
    assert early.stop is not None and early.stop.exit is Exit.FIXED_START_UNMET
    assert early.stop.detail["j0_min"] == axis[j0_min] and early.stop.detail["first_return_day"] == axis[j0_min - 1]
    assert early.candidates == {} and early.records == ()
    first_return_day = axis[j0_min + 10]
    late = run_window(snapshot, spec(axis, Purpose.FROZEN, first_return_day), parameters(), SUBSET)
    assert late.stop is None and late.window is not None
    assert late.window.j0 == axis.index(first_return_day)                      # 甲线补充裁决一第一条
    assert (late.window.j0, late.window.j0_min) == (j0_min + 10, j0_min)
    assert late.window.start_basis.startswith("固定起点") and late.window.n == len(axis) - 1 - (j0_min + 10)


# ---------------------------------------------------------------------------
# #9、#10（L3）计算失败与分类无法确定的出口
# ---------------------------------------------------------------------------


def test_l3_failure_exit_keeps_only_completed_candidates(axis: tuple[dt.date, ...], snapshot: Snapshot,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    original = research_run.simulate_targets

    def failing(*arguments: object, **keywords: object) -> NavResult:
        calls.append(1)
        if len(calls) == 3:                                                    # 主参照、第一个候选之后：第二个候选
            raise NavError("构造：第二个候选的信号模拟失败")
        return original(*arguments, **keywords)                                # type: ignore[arg-type]

    monkeypatch.setattr(research_run, "simulate_targets", failing)
    result = run_window(snapshot, spec(axis, Purpose.DEVELOPMENT), parameters(), REGISTERED_CANDIDATES)
    assert result.stop is not None and result.stop.exit is Exit.FAILED
    assert result.stop.exception_type == "NavError" and result.stop.traceback is None
    assert list(result.candidates) == [REGISTERED_CANDIDATES[0]] and len(result.records) == 1
    with pytest.raises(ResearchRunError, match="已停止"):
        select_development(result, TOLERANCE)


def test_l3_undeterminable_exit(axis: tuple[dt.date, ...], snapshot: Snapshot,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    def undeterminable(*_: object) -> None:
        raise R2Undeterminable("构造：无法确定的状态影响分类")

    monkeypatch.setattr(research_run, "r2_result", undeterminable)
    result = run_window(snapshot, spec(axis), parameters(), SUBSET)
    assert result.stop is not None and result.stop.exit is Exit.UNDETERMINABLE
    assert result.stop.exception_type == "R2Undeterminable" and result.candidates == {}


# ---------------------------------------------------------------------------
# #11（L1）分段：日期边界 (start, end]，按收益区间末日归属
# ---------------------------------------------------------------------------


def test_l1_segments(axis: tuple[dt.date, ...], snapshot: Snapshot, base: WindowResult) -> None:
    assert base.window is not None
    days = axis[base.window.j0:]
    saturday = days[10] + dt.timedelta(days=(5 - days[10].weekday()) % 7)      # days[10] 之后的第一个星期六
    new_year = dt.date(2005, 1, 1)                                             # 星期六，休市
    segments = (
        SegmentSpec("甲", days[2], days[5], "设计第八节第 3 条"),
        SegmentSpec("乙", days[5], days[8], "设计第八节第 3 条"),
        SegmentSpec("非交易日边界", saturday, new_year, "构造"),
        SegmentSpec("部分覆盖", days[0] - dt.timedelta(days=30), days[20], "构造"),
        SegmentSpec("无收益区间", days[0] - dt.timedelta(days=30), days[0], "构造"),
        SegmentSpec("边界日计入", days[30], days[40], "构造"),
    )
    result = run_window(snapshot, spec(axis), parameters(segments), SUBSET)
    assert result.stop is None and result.common is not None
    hold = result.common.hold
    assert isinstance(hold, NavResult)
    for candidate in SUBSET:
        reports = {report.spec.name: report for report in result.candidates[candidate].segments}
        nav = result.candidates[candidate].outcome.signal_nav
        assert isinstance(nav, NavResult)
        first, second = reports["甲"], reports["乙"]
        assert (first.first_nav_day, first.last_nav_day, first.returns_count) == (days[2], days[5], 3)
        assert (second.first_nav_day, second.last_nav_day, second.returns_count) == (days[5], days[8], 3)
        assert first.coverage is second.coverage is Coverage.FULL
        assert first.returns_count + second.returns_count == 8 - 2
        ends_first, ends_second = set(days[3:6]), set(days[6:9])                # 计入收益的区间末日
        assert not ends_first & ends_second and ends_first | ends_second == set(days[3:9])
        logs = math.fsum(math.log1p(value) for value in nav.returns[2:8])       # r[3..8]，r[i] = returns[i − 1]
        assert abs(logs - (math.log(nav.wealth[8]) - math.log(nav.wealth[2]))) <= 1e-12
        weekend = reports["非交易日边界"]
        after = next(index for index, day in enumerate(days) if day > saturday)
        last = max(index for index, day in enumerate(days) if day <= new_year)
        assert (weekend.first_nav_day, weekend.last_nav_day) == (days[after - 1], days[last])
        assert weekend.returns_count == last - (after - 1) and weekend.coverage is Coverage.FULL
        assert weekend.last_nav_day == dt.date(2004, 12, 31)
        partial = reports["部分覆盖"]
        assert (partial.coverage, partial.first_nav_day, partial.last_nav_day, partial.returns_count) == (
            Coverage.PARTIAL, days[0], days[20], 20)
        empty = reports["无收益区间"]
        assert (empty.coverage, empty.first_nav_day, empty.last_nav_day, empty.returns_count) == (
            Coverage.EMPTY, None, None, 0)
        boundary = reports["边界日计入"]
        assert (boundary.first_nav_day, boundary.last_nav_day, boundary.returns_count) == (days[30], days[40], 10)
        for report in (first, second, weekend, partial, boundary):
            assert (report.drawdown_ratio is None) == report.undefined
        assert result.candidates[candidate].segments[0].spec is segments[0]    # 申请边界原样保存


# ---------------------------------------------------------------------------
# #12（L1）检验输入
# ---------------------------------------------------------------------------


def test_l1_confirmatory_input(axis: tuple[dt.date, ...], snapshot: Snapshot, base: WindowResult) -> None:
    assert base.window is not None
    candidate = SUBSET[0]
    frozen = run_window(snapshot, spec(axis, Purpose.FROZEN, axis[base.window.j0]), parameters(), (candidate,))
    assert frozen.stop is None and frozen.window is not None and frozen.reference is not None
    data = confirmatory_input(frozen, candidate, "构造期")
    days = axis[frozen.window.j0:]
    outcome = frozen.candidates[candidate]
    assert data.period == "构造期"
    assert data.end_days == days[1:] and len(data.end_days) == frozen.window.n
    assert isinstance(outcome.outcome.signal_nav, NavResult)
    assert data.candidate_returns == outcome.outcome.signal_nav.returns
    assert data.candidate_log_wealth == outcome.outcome.signal_nav.log_wealth
    reference = frozen.reference.outcome.signal_nav
    assert isinstance(reference, NavResult) and data.reference_returns == reference.returns
    assert data.r1 == outcome.record.r1 and dict(data.r2) == dict(outcome.record.r2)
    assert data.first_signal_day == axis[frozen.window.j0 - 1]
    assert set(data.events) == {"SPX", "QQQ"}
    with pytest.raises(ResearchRunError):
        confirmatory_input(frozen, SUBSET[1], "构造期")                       # 候选不在结果中


# ---------------------------------------------------------------------------
# #13（L1）诊断开关
# ---------------------------------------------------------------------------


def business_fields_equal(first: WindowResult, second: WindowResult) -> None:
    """甲线补充裁决一第二条：只排除顶层 diagnostics 与 parameters.diagnostics，其余逐字段相等。"""
    for item in dataclasses.fields(WindowResult):
        if item.name in ("diagnostics", "parameters"):
            continue
        assert getattr(first, item.name) == getattr(second, item.name), item.name
    assert dataclasses.replace(first.parameters, diagnostics=True) == second.parameters


def test_l1_diagnostics_switch(axis: tuple[dt.date, ...], snapshot: Snapshot, base: WindowResult,
                               development: WindowResult) -> None:
    assert base.diagnostics is None and development.diagnostics is None
    switched = run_window(snapshot, spec(axis), parameters(diagnostics=True), SUBSET)
    business_fields_equal(base, switched)
    diagnostics, window = switched.diagnostics, switched.window
    assert diagnostics is not None and window is not None
    for candidate in SUBSET:
        convergence = window.convergences[candidate]
        found = diagnostics.convergence[candidate]
        for name, item in found.channels.items():
            assert item.first_common_global == convergence.channel_indices[name], name
            assert item.local_origin == window.t0 + 1
            domain = TREND_DOMAIN if name == "MR" else PULLBACK_DOMAIN
            assert tuple(run.initial for run in item.runs) == channel_initial_states(domain)
        assert found.system.first_common_global == convergence.system_index
        assert found.system.local_origin == convergence.kappa_channel + 1
        assert tuple(run.initial for run in found.system.runs) == system_initial_states(candidate.h)
        assert len(diagnostics.leverage[repr(candidate)]) == window.n
    assert diagnostics.reference.first_common_global == window.reference_index
    assert tuple(run.initial for run in diagnostics.reference.runs) == reference_initial_states()
    assert len(diagnostics.drawdowns["SPX"]) == len(axis) and "reference" in diagnostics.leverage
    # 27 组：开关前后记录与选择结果相同。
    switched_all = run_window(snapshot, spec(axis, Purpose.DEVELOPMENT), parameters(diagnostics=True),
                              REGISTERED_CANDIDATES)
    business_fields_equal(development, switched_all)
    assert select_development(switched_all, TOLERANCE).selection == select_development(development,
                                                                                       TOLERANCE).selection


# ---------------------------------------------------------------------------
# #14（L1）不可变
# ---------------------------------------------------------------------------


def test_l1_results_are_immutable(base: WindowResult) -> None:
    assert base.common is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        base.stop = None                                                        # type: ignore[misc]
    with pytest.raises(TypeError):
        base.candidates[SUBSET[0]] = base.candidates[SUBSET[1]]                 # type: ignore[index]
    with pytest.raises(TypeError):
        base.common.events["SPX"] = ()                                          # type: ignore[index]
    with pytest.raises(TypeError):
        base.spec.histories["SPX"] = END                                        # type: ignore[index]
    with pytest.raises(TypeError):
        base.candidates[SUBSET[0]].status[END] = None                           # type: ignore[index]


def test_l1_policy_result_types_without_missing_prices(base: WindowResult) -> None:
    """价格完整时执行政策为 PolicyResult，信号模拟与一直持有为 NavResult（设计第七节）。"""
    assert base.common is not None and base.reference is not None
    assert isinstance(base.common.hold, NavResult)
    assert isinstance(base.reference.outcome.policy, PolicyResult)
    for candidate in SUBSET:
        assert isinstance(base.candidates[candidate].outcome.policy, PolicyResult)
        assert isinstance(base.candidates[candidate].outcome.signal_nav, NavResult)


# ---------------------------------------------------------------------------
# 甲补修二（A2 补充一裁决 1）：“灯色相同、计数器不同”按定稿二第五节的“任意两次运行”口径（L2）
# ---------------------------------------------------------------------------


def test_l2_same_risk_different_counters_any_two_runs() -> None:
    """三条内存构造的运行：第 0 日两条 S 同为正常而计数不同、第三条为二级 → 记录（原“全部运行 S 相同”口径不记录）；
    第 1 日三条完整状态全同 → 不记录；收敛位置及之后的日不记录。"""
    days = (dt.date(2004, 1, 5), dt.date(2004, 1, 6), dt.date(2004, 1, 7))
    normal = SystemState(Risk.NORMAL, 1, 1)
    runs = (research_run.EnumeratedRun(SystemState(Risk.NORMAL, 0, 0),
                                       (SystemState(Risk.NORMAL, 0, 0), normal, SystemState(Risk.NORMAL, 0, 1))),
            research_run.EnumeratedRun(SystemState(Risk.NORMAL, 1, 1), (normal, normal, normal)),
            research_run.EnumeratedRun(SystemState(Risk.LEVEL2, 0, 0),
                                       (SystemState(Risk.LEVEL2, 0, 0), normal, normal)))
    first_day = [run.states[0] for run in runs]
    assert len({state.risk for state in first_day}) == 2      # 原口径（全部运行 S 相同）不会记录第 0 日
    assert research_run._same_risk_different_counters(days, runs, 3) == (days[0], days[2])   # 第 1 日全同，不记录
    assert research_run._same_risk_different_counters(days, runs, 1) == (days[0],)            # 收敛位置起不再记录


# ---------------------------------------------------------------------------
# 甲补修三（D14 裁决 B）：R2 窗口状态 axis[j₀−2 .. E] 每一天取登记初始快照下已计算的 S（L1）
# ---------------------------------------------------------------------------


def expected_prompt(risk: Risk) -> Prompt:
    """设计第十节（附录“D14 裁决 B”）：正常 → 非提示，一级、二级 → 提示。"""
    return Prompt.NO if risk is Risk.NORMAL else Prompt.YES


def test_l1_prompt_mapping_covers_f_minus_one(axis: tuple[dt.date, ...], base: WindowResult) -> None:
    """提示映射：axis[j₀−2]（f−1）及其后每一天的状态都等于该日系统记录的映射值，不出现“无法确定”。"""
    assert base.window is not None
    j0 = base.window.j0
    for candidate in SUBSET:
        outcome = base.candidates[candidate]
        assert list(outcome.status) == list(axis[j0 - 2:])
        assert outcome.status[axis[j0 - 2]] is expected_prompt(outcome.system[0].risk)
        assert all(outcome.status[item.day] is expected_prompt(item.risk) for item in outcome.system)
        assert Prompt.UNKNOWN not in outcome.status.values()


D14_SEEDS = {"SPX": 16, "QQQ": 17}
D14_GAP = range(200, 280)                    # QQQ 自 t0 + 1 起缺价 80 日，推迟收敛，使 j₀ = κ_全 + 1
D14_SUBSET = (Candidate(3, Decimal("0.025"), 1), Candidate(3, Decimal("0.025"), 3))


def test_l1_d14_segment_from_f_uses_actual_previous_state(axis: tuple[dt.date, ...]) -> None:
    """j₀ 由 κ_全 + 1 决定、最晚收敛组 S_f 为提示且段自 f 开始：f−1 = axis[j₀−2] 早于该组系统收敛日（旧映射记为
    无法确定并停止）；补修三后不再停止，SPX 首段按 S_{f−1} 的实际值分类。构造：种子 SPX 16、QQQ 17，QQQ 第 200—279
    行缺价（QQQ 事件因缺价不可得，SPX 段账照常计算）；构造参数由搜索确定后写定。"""
    closes = {asset: random_closes(seed, len(axis)) for asset, seed in D14_SEEDS.items()}
    for index in D14_GAP:
        closes["QQQ"][index] = None
    result = run_window(build(axis, closes), spec(axis), parameters(), D14_SUBSET)
    assert result.stop is None and result.window is not None
    window = result.window
    j0, f = window.j0, axis[window.j0 - 1]
    assert j0 == window.kappa_all + 1 > window.t0 + 63                          # j₀ 由 κ_全 + 1 决定
    assert isinstance(result.common.events["QQQ"], Unavailable)
    previous_states = {}
    for candidate in D14_SUBSET:
        outcome = result.candidates[candidate]
        assert window.convergences[candidate].system_index == window.kappa_all  # 最晚收敛组
        assert j0 - 2 < window.convergences[candidate].system_index             # f−1 早于收敛日
        assert outcome.system[1].day == f and outcome.system[1].risk is not Risk.NORMAL   # S_f 为提示
        assert outcome.status[axis[j0 - 2]] is expected_prompt(outcome.system[0].risk)
        segment, category = outcome.ledgers["SPX"].classes[0]
        assert segment.start == f                                               # 段自 f 开始
        previous = outcome.system[0].risk
        previous_states[candidate] = previous
        if previous is Risk.NORMAL:
            assert not segment.pre_window and category is not SegmentClass.PRE_WINDOW   # 自 f 开始
        else:
            assert segment.pre_window and category is SegmentClass.PRE_WINDOW           # 窗口前已启动
    # 两组分别覆盖 S_{f−1} 为非提示与提示两种情形。
    assert previous_states[D14_SUBSET[0]] is Risk.NORMAL and previous_states[D14_SUBSET[1]] is not Risk.NORMAL


# ---------------------------------------------------------------------------
# 阶段四 M2 第一部分：journal（阶段四字段级设计稿第四节第 2 小节；M2 指令第二节第 3 小节第 4 条）。只追加。
# ---------------------------------------------------------------------------

from market_risk.wavewarn_v20.labels_r2 import MissingPriceError  # noqa: E402
from market_risk.wavewarn_v20.r1 import R1Error  # noqa: E402


class Recorder:
    """列表记录器：只做追加。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, str]] = []

    def __call__(self, event: str, item: str, field: str, stage: str) -> None:
        self.events.append((event, item, field, stage))


# 设计稿第四节第 2 小节表中每一行（事件，对象，字段）；对象以“候选键”“诊断对象”表示一类。
JOURNAL_ROWS = (
    ("derived", "公共", "输入派生量"), ("derived", "公共", "t0"), ("derived", "公共", "R2 事件"),
    ("performance", "候选键", "通道与系统收敛日（含状态路径）"), ("performance", "主参照", "收敛（含状态路径）"),
    ("performance", "一直持有", "净值"),
    ("performance", "主参照", "逐日状态"), ("performance", "主参照", "信号"), ("performance", "主参照", "计划目标"),
    ("performance", "主参照", "净值"), ("performance", "主参照", "执行政策"),
    ("performance", "候选键", "系统逐日记录"), ("performance", "候选键", "信号与计划目标"),
    ("performance", "候选键", "信号模拟净值"), ("performance", "候选键", "执行政策"), ("performance", "候选键", "R2"),
    ("performance", "候选键", "段账"), ("performance", "候选键", "R1"), ("performance", "候选键", "分段"),
    ("performance", "候选键", "候选记录"),
    ("performance", "诊断对象", "回撤"), ("performance", "诊断对象", "杠杆因子"),
    ("performance", "诊断对象", "收敛枚举"),
)


def journal_row(event: str, item: str, field: str, stage: str) -> tuple[str, str, str]:
    """把一条记录归到表中的一行：候选键归“候选键”；诊断阶段的对象归“诊断对象”；“R2：SPX”等去掉资产后缀。"""
    if stage == "诊断":
        item = "诊断对象"
    elif item.startswith("K="):
        item = "候选键"
    return event, item, field.split("：")[0]


def test_m2_journal_none_and_recorder_give_equal_results(axis: tuple[dt.date, ...], snapshot: Snapshot) -> None:
    """journal=None 与传入列表记录器两次运行的 WindowResult 完全相等；记录覆盖设计稿表中每一行至少一次；
    表现信息事件都在 derived 事件之后，记录的阶段字段取当时的阶段。"""
    plain = run_window(snapshot, spec(axis), parameters(diagnostics=True), SUBSET)
    recorder = Recorder()
    recorded = run_window(snapshot, spec(axis), parameters(diagnostics=True), SUBSET, journal=recorder)
    assert recorded == plain and plain.stop is None and plain.diagnostics is not None
    rows = {journal_row(*event) for event in recorder.events}
    assert rows == set(JOURNAL_ROWS)
    assert {event for event, _, _, _ in recorder.events} == {"derived", "performance"}
    assert recorder.events[0] == ("derived", "公共", "输入派生量", "输入派生量")
    keys = {item for _, item, _, _ in recorder.events if item.startswith("K=")}
    assert keys == {research_run.candidate_key(candidate) for candidate in SUBSET}
    assert any(stage == "诊断" for *_, stage in recorder.events) and recorder.events[-1][3] == "诊断"


class NavErrorLookalike(Exception):
    """与 NavError 同名的测试类（不是 nav.NavError）。"""


NavErrorLookalike.__name__ = "NavError"


@pytest.mark.parametrize("factory", [
    lambda: RuntimeError("构造：回调失败"),
    lambda: NavError("构造：与组合层捕获的业务异常同类型"),
    lambda: NoStartError("构造：同类型"),
    lambda: R1Error("构造：同类型"),
    lambda: MissingPriceError("构造：同类型"),
    lambda: NavErrorLookalike("构造：同名的测试类"),
], ids=["RuntimeError", "NavError", "NoStartError", "R1Error", "MissingPriceError", "同名NavError"])
@pytest.mark.parametrize("when", ["首个记录", "表现信息之中", "诊断阶段"])
def test_m2_journal_exceptions_pass_through_unchanged(axis: tuple[dt.date, ...], snapshot: Snapshot,
                                                     factory, when: str) -> None:
    """回调抛出的普通异常（含与组合层业务捕获同一类型、以及同名的测试类）原样传出：run_window 抛出的正是该对象，
    不返回停止结果、不被分类为任何出口；诊断阶段（_diagnostics，位于业务 try 之外）同样原样传出。"""
    error = factory()
    calls: list[str] = []

    def failing(event: str, item: str, field: str, stage: str) -> None:
        calls.append(field)
        trigger = {"首个记录": len(calls) == 1, "表现信息之中": field == "信号模拟净值",
                   "诊断阶段": stage == "诊断"}[when]
        if trigger:
            raise error

    with pytest.raises(BaseException) as caught:
        run_window(snapshot, spec(axis), parameters(diagnostics=True), SUBSET, journal=failing)
    assert caught.value is error


def test_m2_journal_base_exception_is_not_caught(axis: tuple[dt.date, ...], snapshot: Snapshot) -> None:
    """回调抛 KeyboardInterrupt（BaseException，非 Exception）：不捕获，原样传出。"""
    interrupt = KeyboardInterrupt()

    def interrupting(*_: str) -> None:
        raise interrupt

    with pytest.raises(KeyboardInterrupt) as caught:
        run_window(snapshot, spec(axis), parameters(), SUBSET, journal=interrupting)
    assert caught.value is interrupt


def test_m2_business_stop_is_unchanged_with_and_without_journal(axis: tuple[dt.date, ...],
                                                                closes: dict[str, list[Decimal | None]],
                                                                base: WindowResult) -> None:
    """同一构造窗口由真实业务异常触发的停止结果（出口、阶段、异常类名、reason）在 journal=None 下与改动前相同
    （期望值按开工版本源码的停止路径写出），传入记录器时也相同；改动前后停止记录的其余字段同样相等。"""
    assert base.window is not None
    cases = ((150, (Exit.NO_START, "t0", "NoStartError", "无法确定 t0")),
             (base.window.j0, (Exit.EMPTY_WINDOW, "起点", "EmptyWindowError", "评价窗口为空")))
    for last, expected in cases:
        days = axis[:last + 1]
        short = build(days, {asset: values[:last + 1] for asset, values in closes.items()})
        plain = run_window(short, spec(days), parameters(), SUBSET)
        recorder = Recorder()
        recorded = run_window(short, spec(days), parameters(), SUBSET, journal=recorder)
        assert plain.stop is not None
        assert (plain.stop.exit, plain.stop.stage, plain.stop.exception_type, plain.stop.reason_code) == expected
        assert recorded == plain
        assert all(event == "derived" for event, *_ in recorder.events) or expected[0] is Exit.EMPTY_WINDOW


def test_m2_journal_none_does_not_call_the_private_caller(axis: tuple[dt.date, ...], snapshot: Snapshot,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """journal=None 时不调用私有调用函数（M2 指令第二节第 3 小节第 3 条）。"""
    def forbidden(*_: object) -> None:
        raise AssertionError("journal=None 时调用了私有调用函数")

    monkeypatch.setattr(research_run, "_record", forbidden)
    assert run_window(snapshot, spec(axis), parameters(diagnostics=True), SUBSET).stop is None
