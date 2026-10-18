"""阶段五验证期登记值（registered_v20_validation）与入口组合步骤（validation_run 的日期断言、重叠比对）的构造测试
（V2 指令第五节：T-WIN、T-DATE、T-OVL、T-D15、T-NC1、T-NC2）。纯算法测试。

真实配置的日期断言只用 market_risk.calendar 的 NYSE 交易日历（不读行情）。
构造轴：stock_trading_days(2002-01-02, 2005-12-30)，共 1008 个交易日；价格路径 random_closes 种子 SPX 2、QQQ 3；
冻结候选 K = 3、θ_P = 0.025、h = 3。期望值来自锁定补充记录（一）第二节、登记条文与 NYSE 日历的手工推导。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from wavewarn_v20_helpers import random_closes

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn_v20 import registered_v20
from market_risk.wavewarn_v20 import registered_v20_validation as registered
from market_risk.wavewarn_v20.convergence import Candidate, candidate_convergence, first_valid_index
from market_risk.wavewarn_v20.development_output import serialize_daily
from market_risk.wavewarn_v20.inputs import snapshot_asset_days, snapshot_trend_days
from market_risk.wavewarn_v20.r2 import EventClass, Window, judge_event
from market_risk.wavewarn_v20.reference import run_reference
from market_risk.wavewarn_v20.research_run import (
    REFERENCE,
    Continuity,
    Exit,
    Purpose,
    ResearchRunError,
    WindowResult,
    WindowSpec,
    candidate_key,
    confirmatory_input,
    run_window,
)
from market_risk.wavewarn_v20.snapshot import Snapshot, make_snapshot
from market_risk.wavewarn_v20.state_machine import evidence_series, run_channels, run_system
from market_risk.wavewarn_v20.validation_output import parse_signals
from market_risk.wavewarn_v20.validation_run import (
    compare_published,
    date_assertions,
    internal_consistency,
    recompute_signals,
)

FROZEN = registered.FROZEN_CANDIDATE
ACQUIRED = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
AXIS = tuple(stock_trading_days(dt.date(2002, 1, 2), dt.date(2005, 12, 30)))
CLOSES = {"SPX": random_closes(2, len(AXIS)), "QQQ": random_closes(3, len(AXIS))}
PARAMETERS = registered.run_parameters()
SMALL = dataclasses.replace(registered.VALIDATION_CONFIRMATORY, resamples=20, split=dt.date(2005, 1, 1))


def snapshot(axis: tuple[dt.date, ...] = AXIS, closes: dict | None = None) -> Snapshot:
    values = closes or CLOSES
    prices = {asset: {day: value for day, value in zip(axis, items, strict=False) if value is not None}
              for asset, items in values.items()}
    return make_snapshot(axis, prices, axis[-1], ACQUIRED, {"SPX": "0" * 64, "QQQ": "0" * 64})


def spec(axis: tuple[dt.date, ...], first: dt.date | None, purpose: Purpose = Purpose.FROZEN) -> WindowSpec:
    return WindowSpec(purpose, first, axis[-1], {"SPX": axis[0], "QQQ": axis[0]}, registered.INITIAL,
                      Continuity.COMPLETE_TRADING_AXIS)


def frozen(first: dt.date, axis: tuple[dt.date, ...] = AXIS, closes: dict | None = None) -> WindowResult:
    return run_window(snapshot(axis, closes), spec(axis, first), PARAMETERS, (FROZEN,))


# 构造评价期：j₀ = 2003-12-31（周三）。NYSE：2003-12-29 周一、12-30 周二、12-31 周三；2004-01-01 周四为元旦休市；
# 2004-01-02 周五开市。所以 f = 2003-12-30、R2 状态起日 = 2003-12-29、
# 首个计入区间 2003-12-31 → 2004-01-02（跨年休市日）。
# E = 2005-12-30（周五），前一交易日 2005-12-29（周四；2005-12-26 周一为圣诞补休），
# 最后计入区间 2005-12-29 → 2005-12-30。
PERIOD = registered.PeriodSpec("validation", "验证期", dt.date(2005, 12, 30), dt.date(2003, 12, 31),
                               dt.date(2003, 12, 30), dt.date(2003, 12, 29),
                               (dt.date(2003, 12, 31), dt.date(2004, 1, 2)),
                               (dt.date(2005, 12, 29), dt.date(2005, 12, 30)), SMALL, "evaluation_validation")
# 构造的保留期式边界：j₀ = 2004-12-31（周五）；2005-01-01 为周六（NYSE 不在 12-31 补休），下一交易日 2005-01-03 周一。
# f = 2004-12-30（周四），状态起日 2004-12-29（周三）。
HOLDOUT_PERIOD = dataclasses.replace(PERIOD, mode="holdout", period="保留期", first_return_day=dt.date(2004, 12, 31),
                                     first_signal_day=dt.date(2004, 12, 30), status_first_day=dt.date(2004, 12, 29),
                                     first_interval=(dt.date(2004, 12, 31), dt.date(2005, 1, 3)),
                                     formal_name="evaluation_holdout")


@pytest.fixture(scope="module")
def result() -> WindowResult:
    found = frozen(PERIOD.first_return_day)
    assert found.stop is None
    return found


# ---------------------------------------------------------------------------
# 登记值（F1）
# ---------------------------------------------------------------------------


def test_frozen_candidate_and_shared_values() -> None:
    """冻结候选恰为登记 27 组中的 (3, 0.025, 3)；共用登记值从 registered_v20 导入（同一对象，不复制数值）。"""
    assert FROZEN == Candidate(3, Decimal("0.025"), 3)
    assert [item for item in registered_v20.CANDIDATES if item == FROZEN] == [FROZEN]
    for name in ("HISTORIES", "INITIAL", "WINDOWS", "POSITIONS", "POLICY", "OFFSET", "TOLERANCE", "R2_RULE",
                 "R2_THRESHOLDS", "CONTINUITY", "run_parameters"):
        assert getattr(registered, name) is getattr(registered_v20, name), name


def test_registered_dates_follow_the_lock_supplement() -> None:
    """锁定补充记录（一）第二节逐项。"""
    assert registered.PURPOSE is Purpose.FROZEN
    validation, holdout = registered.VALIDATION, registered.HOLDOUT
    assert (validation.cutoff, validation.first_return_day, validation.first_signal_day,
            validation.status_first_day) == (dt.date(2022, 12, 30), dt.date(2016, 12, 30), dt.date(2016, 12, 29),
                                              dt.date(2016, 12, 28))
    assert validation.first_interval == (dt.date(2016, 12, 30), dt.date(2017, 1, 3))
    assert validation.last_interval == (dt.date(2022, 12, 29), dt.date(2022, 12, 30))
    assert (holdout.cutoff, holdout.first_return_day, holdout.first_signal_day, holdout.status_first_day) == (
        dt.date(2026, 9, 25), dt.date(2022, 12, 30), dt.date(2022, 12, 29), dt.date(2022, 12, 28))
    assert holdout.first_interval == (dt.date(2022, 12, 30), dt.date(2023, 1, 3))
    assert holdout.last_interval == (dt.date(2026, 9, 24), dt.date(2026, 9, 25))
    assert (validation.formal_name, holdout.formal_name) == ("evaluation_validation", "evaluation_holdout")
    assert registered.PERIODS == {"validation": validation, "holdout": holdout}
    window = registered.window_spec(validation)
    assert (window.purpose, window.first_return_day, window.last_day) == (Purpose.FROZEN, dt.date(2016, 12, 30),
                                                                           dt.date(2022, 12, 30))
    assert dict(window.histories) == dict(registered_v20.HISTORIES) and window.initial is registered_v20.INITIAL


def test_confirmatory_parameters() -> None:
    """V1 第四节与标准第二节第 2 小节“甲选项的具体规定”第 4、5 条。"""
    for parameters, seeds, split in (
            (registered.VALIDATION_CONFIRMATORY, (20261020, 20261010, 20261040, 20261060, 202610120),
             dt.date(2020, 1, 1)),
            (registered.HOLDOUT_CONFIRMATORY, (20261120, 20261110, 20261140, 20261160, 202611120),
             dt.date(2025, 1, 1))):
        assert (parameters.main.block, parameters.main.seed) == (20, seeds[0])
        assert [(item.block, item.seed) for item in parameters.sensitivities] == list(
            zip((10, 40, 60, 120), seeds[1:], strict=True))
        assert (parameters.resamples, parameters.alpha, parameters.warning_p, parameters.annual_days,
                parameters.minimum_growth, parameters.tolerance, parameters.padding, parameters.split) == (
            10_000, 0.05, 0.10, 252, 0.01, 1e-10, 20, split)


def test_basis_hashes_and_baseline_constants() -> None:
    assert registered.LOCK_SUPPLEMENT_SHA256 == "39de913cc7b765e3c0c1158b5c9a8630c7dfaa7ae9c9b45c5bee903440facf84"
    assert registered.STANDARD_R3_SHA256 == "652a5c5e17230f9dad28956d86646f8f67f728fefc7ff6555ea73fa0f240511e"
    assert registered.V1_DESIGN_SHA256 == "9b1a054276c7389b79b1da0d2d9f1973c08924ac99484ad3fb0e4de49fb8ae32"
    assert registered.M3_MANIFEST_SHA256 == "a8453dd505280af75e0bf108baf2b4bac431886e5deeb4964fbeb19a9b543183"
    assert registered.M3_SIGNALS_SHA256 == "1af662faba75587f8e947cbb97ed19e23b8f7d55ca95cdb06e794f672185c768"
    assert registered.M3_SIGNALS_BYTES == 375434
    assert registered.SIGNALS_HEADER == ("date", "object", "risk", "level", "c1", "c2", "all_valid")
    described = registered.describe(registered.VALIDATION)
    assert described["basis_sha256"]["lock_supplement_1"] == registered.LOCK_SUPPLEMENT_SHA256
    assert described["first_return_day"] == "2016-12-30" and described["frozen_candidate"] == {
        "K": 3, "theta_P": "0.025", "h": 3}


# ---------------------------------------------------------------------------
# T-DATE：真实配置的纯日期断言（只用交易日历）与构造交易日轴上的逐项断言
# ---------------------------------------------------------------------------


def neighbours(day: dt.date) -> tuple[dt.date, dt.date, dt.date]:
    """（前两个交易日，前一个交易日，后一个交易日）。"""
    days = stock_trading_days(day - dt.timedelta(days=15), day + dt.timedelta(days=15))
    index = days.index(day)
    return days[index - 2], days[index - 1], days[index + 1]


def test_real_dates_on_the_nyse_calendar() -> None:
    """验证期：2016-12-30 周五开市；前一交易日 2016-12-29、再前 2016-12-28；后一交易日 2017-01-03（2017-01-02 周一为
    元旦补休）。2022-12-30 周五开市，前一交易日 2022-12-29。
    保留期：2022-12-30 后一交易日 2023-01-03（2023-01-02 周一补休）；2026-09-25 周五，前一交易日 2026-09-24。"""
    for period in (registered.VALIDATION, registered.HOLDOUT):
        before2, before, after = neighbours(period.first_return_day)
        assert (before2, before) == (period.status_first_day, period.first_signal_day)
        assert (period.first_return_day, after) == period.first_interval
        _, last_before, _ = neighbours(period.cutoff)
        assert (last_before, period.cutoff) == period.last_interval
        assert period.cutoff in stock_trading_days(period.cutoff, period.cutoff)
    assert dt.date(2017, 1, 2) not in stock_trading_days(dt.date(2016, 12, 30), dt.date(2017, 1, 3))
    assert dt.date(2023, 1, 2) not in stock_trading_days(dt.date(2022, 12, 30), dt.date(2023, 1, 3))


def test_dates_on_a_constructed_axis_with_a_new_year_holiday(result: WindowResult) -> None:
    """构造轴上组合层冻结结果的 j₀、f、R2 状态起日、首末计入区间、E 逐项与构造评价期相同（见 PERIOD 的推导）。"""
    document = date_assertions(PERIOD, snapshot(), result, FROZEN, PARAMETERS.offset)
    assert document["passed"] is True and all(row["passed"] for row in document["checks"]), document["checks"]
    assert [row["item"] for row in document["checks"]] == [
        "j₀（first_return_day）", "E（last_day）", "f（候选首条净值模拟信号）", "f（主参照首条净值模拟信号）",
        "R2 状态起日（候选完整记录首日）", "R2 状态起日（主参照完整记录首日）", "R2 提示状态首日", "首个计入收益区间",
        "最后计入收益区间"]
    assert document["n"] == len(AXIS) - 1 - AXIS.index(dt.date(2003, 12, 31))


def test_dates_on_a_constructed_holdout_boundary() -> None:
    found = frozen(HOLDOUT_PERIOD.first_return_day)
    assert found.stop is None
    assert date_assertions(HOLDOUT_PERIOD, snapshot(), found, FROZEN, PARAMETERS.offset)["passed"] is True


def test_dates_mismatch_is_reported_item_by_item(result: WindowResult) -> None:
    wrong = dataclasses.replace(PERIOD, first_interval=(dt.date(2003, 12, 31), dt.date(2004, 1, 1)))
    document = date_assertions(wrong, snapshot(), result, FROZEN, PARAMETERS.offset)
    assert document["passed"] is False
    assert [row["item"] for row in document["checks"] if not row["passed"]] == ["首个计入收益区间"]


def test_left_truncation_first_day_is_f(result: WindowResult) -> None:
    """峰日恰等于 f 的事件判为左截断，峰日为 f 次日的不判；置零所用首日也是 f（检验输入的 first_signal_day）。"""
    from market_risk.wavewarn_v20.labels_r2 import R2Event

    outcome = result.candidates[FROZEN]
    window = Window(AXIS, PERIOD.first_signal_day, AXIS[-1], outcome.status)
    f_index = AXIS.index(PERIOD.first_signal_day)
    for peak, left in ((AXIS[f_index], True), (AXIS[f_index + 1], False)):
        item = R2Event("SPX", peak, Decimal("100"), AXIS[f_index + 5], AXIS[f_index + 6], AXIS[f_index + 8],
                       Decimal("90"), None, True)
        assert (judge_event(item, window).category is EventClass.LEFT_TRUNCATED) is left
    assert confirmatory_input(result, FROZEN, "验证期").first_signal_day == PERIOD.first_signal_day


# ---------------------------------------------------------------------------
# T-WIN：窗口起点净值记为 1；状态不重置；j₀−1 日全部枚举运行完整状态相同
# ---------------------------------------------------------------------------


def test_window_start_and_continuous_state(result: WindowResult) -> None:
    outcome, reference, common = result.candidates[FROZEN], result.reference, result.common
    j0 = AXIS.index(PERIOD.first_return_day)
    for nav in (outcome.outcome.signal_nav, reference.outcome.signal_nav, common.hold):
        assert nav.executions[0].day == PERIOD.first_return_day and nav.executions[0].wealth == 1.0
    # 状态不重置：自 t0 起以登记初始快照连续运行的系统记录，在 axis[j₀−2 .. E] 上与组合层冻结结果逐日相同。
    current = snapshot()
    spx, qqq = snapshot_asset_days(current, "SPX", PARAMETERS.windows), snapshot_asset_days(current, "QQQ",
                                                                                              PARAMETERS.windows)
    trend = snapshot_trend_days(current, "SPX", PARAMETERS.windows)
    k = first_valid_index(spx, qqq, trend) + 1
    paths = run_channels(registered.INITIAL.channels, spx[k:], qqq[k:], trend[k:], 3, Decimal("0.025"), 200)
    system = run_system(registered.INITIAL.system, evidence_series(spx[k:], qqq[k:], trend[k:], paths), 3, 3)
    assert outcome.system == system[j0 - 2 - k:]
    assert reference.days == run_reference(registered.INITIAL.reference, trend[k:], 200)[j0 - 2 - k:]
    # j₀−1 日：各通道、系统与主参照的全部枚举运行（全部初始状态）完整状态相同（诊断开，组合层设计第十四节）。
    diagnostics = result.diagnostics
    convergence = diagnostics.convergence[FROZEN]
    for enumerated in (*convergence.channels.values(), convergence.system, diagnostics.reference):
        local = j0 - 1 - enumerated.local_origin
        assert len({run.states[local] for run in enumerated.runs}) == 1, enumerated.object


# ---------------------------------------------------------------------------
# T-D15、T-NC1、T-NC2：固定起点的预热与收敛前提（组合层出口），与入口断言的一致
# ---------------------------------------------------------------------------


def indices(closes: dict) -> tuple[int, int]:
    """（t0，κ_全）：以 convergence 的公共函数独立计算（不经组合层）。"""
    current = snapshot(AXIS, closes)
    spx, qqq = snapshot_asset_days(current, "SPX", PARAMETERS.windows), snapshot_asset_days(current, "QQQ",
                                                                                              PARAMETERS.windows)
    trend = snapshot_trend_days(current, "SPX", PARAMETERS.windows)
    t0 = first_valid_index(spx, qqq, trend)
    from market_risk.wavewarn_v20.convergence import reference_convergence

    kappa = max(candidate_convergence(spx, qqq, trend, t0, FROZEN, 200).system_index,
                t0 + 1 + reference_convergence(trend[t0 + 1:], 200))
    return t0, kappa


def test_d15_warmup_not_met_stops_the_composition() -> None:
    """κ + 1 ≤ j₀ < t0 + 63：组合层以“固定窗口不满足预热或收敛条件”停止（j₀_min = max(t0 + 63, κ + 1) = t0 + 63）。"""
    t0, kappa = indices(CLOSES)
    j0 = t0 + 62
    assert kappa + 1 <= j0 < t0 + 63
    stopped = frozen(AXIS[j0])
    assert stopped.stop is not None and stopped.stop.exit is Exit.FIXED_START_UNMET
    assert (stopped.stop.detail["j0_index"], stopped.stop.detail["j0_min_index"]) == (j0, t0 + 63)
    with pytest.raises(ResearchRunError):
        confirmatory_input(stopped, FROZEN, "验证期")


def test_d15_entry_assertion_agrees_with_the_stop_rule(result: WindowResult) -> None:
    """入口的 D15 断言只检查：组合层返回的窗口满足 j₀ ≥ t0 + 63 且 j₀ ≥ κ + 1；若窗口记录的 j₀ 低于 t0 + 63
    （人为改写窗口概要），断言即不通过。"""
    good = date_assertions(PERIOD, snapshot(), result, FROZEN, PARAMETERS.offset)["d15"]
    assert good["warmup_ok"] and good["convergence_ok"] and good["j0"] >= good["t0"] + 63
    window = dataclasses.replace(result.window, j0=result.window.t0 + 62)
    bad = date_assertions(PERIOD, snapshot(), dataclasses.replace(result, window=window), FROZEN, PARAMETERS.offset)
    assert bad["passed"] is False and bad["d15"]["warmup_ok"] is False


def test_real_configuration_d15_relation_on_the_calendar() -> None:
    """真实配置（只用交易日历与 V1 第三节第 5 条所列 A 值）：自 1990-01-02 起的 NYSE 轴上 2016-12-30 的下标为 6804，
    QQQ 首日 1999-03-10 的下标为 2321；t0 = 2321 + 62 = 2383（63 日最高价窗口自 QQQ 首日起首次完整）；
    6804 ≥ 2383 + 63，D15 前提“j₀ < t0 + 63”不成立。κ 由正式运行时组合层计算，入口断言 j₀ ≥ κ + 1。"""
    axis = stock_trading_days(dt.date(1990, 1, 2), dt.date(2016, 12, 30))
    assert axis.index(dt.date(2016, 12, 30)) == 6804 and axis.index(dt.date(1999, 3, 10)) == 2321
    assert 6804 >= 2321 + 62 + 63


def test_nc1_never_converges() -> None:
    """SPX 自 t0 次日起全部缺价：P_SPX 的全部初始状态运行截至 E 始终不相遇 → “始终不收敛”，停在收敛阶段；
    不进入确认性检验（检验输入拒绝已停止的窗口）。"""
    t0, _ = indices(CLOSES)
    closes = {"SPX": CLOSES["SPX"][:t0 + 1] + [None] * (len(AXIS) - t0 - 1), "QQQ": CLOSES["QQQ"]}
    stopped = run_window(snapshot(AXIS, closes), spec(AXIS, PERIOD.first_return_day), PARAMETERS, (FROZEN,))
    assert stopped.stop is not None and stopped.stop.exit is Exit.NOT_CONVERGED and stopped.stop.stage == "收敛"
    assert stopped.window is None and stopped.candidates == {}
    with pytest.raises(ResearchRunError):
        confirmatory_input(stopped, FROZEN, "验证期")


def test_nc2_converges_after_the_fixed_start() -> None:
    """SPX 在 t0 之后缺价 100 日后恢复：最终收敛，但 κ + 1 > j₀；取 j₀ = t0 + 63（预热满足）→
    “固定窗口不满足预热或收敛条件”。与 T-D15（预热不足）分开构造。"""
    t0, _ = indices(CLOSES)
    hole = {"SPX": CLOSES["SPX"][:t0 + 1] + [None] * 100 + CLOSES["SPX"][t0 + 101:], "QQQ": CLOSES["QQQ"]}
    _, kappa = indices(hole)
    j0 = t0 + 63
    assert kappa + 1 > j0
    stopped = run_window(snapshot(AXIS, hole), spec(AXIS, AXIS[j0]), PARAMETERS, (FROZEN,))
    assert stopped.stop is not None and stopped.stop.exit is Exit.FIXED_START_UNMET
    assert stopped.stop.detail["j0_index"] == j0 and stopped.stop.detail["j0_min_index"] == kappa + 1


# ---------------------------------------------------------------------------
# T-OVL：完整状态跨截止日逐日相同；已发布字段比对的字段、日期集合与不符停止
# ---------------------------------------------------------------------------

EARLY = AXIS.index(dt.date(2004, 12, 31))             # 较早截止日 2004-12-31 的下标


def states(current: Snapshot) -> tuple[tuple, tuple, tuple]:
    spx, qqq = snapshot_asset_days(current, "SPX", PARAMETERS.windows), snapshot_asset_days(current, "QQQ",
                                                                                              PARAMETERS.windows)
    trend = snapshot_trend_days(current, "SPX", PARAMETERS.windows)
    k = first_valid_index(spx, qqq, trend) + 1
    paths = run_channels(registered.INITIAL.channels, spx[k:], qqq[k:], trend[k:], 3, Decimal("0.025"), 200)
    evidence = evidence_series(spx[k:], qqq[k:], trend[k:], paths)
    system = run_system(registered.INITIAL.system, evidence, 3, 3)
    return evidence, system, run_reference(registered.INITIAL.reference, trend[k:], 200)


def test_complete_state_is_identical_across_two_cutoffs() -> None:
    """同一构造数据分别以较早（2004-12-31）与较晚（2005-12-30）截止日计算：重叠范围内五个通道（含两资产与均线输入）、
    系统 (S、c₁、c₂、L) 与主参照的逐日完整状态逐日相同。"""
    early_axis = AXIS[:EARLY + 1]
    early = states(snapshot(early_axis, {asset: values[:EARLY + 1] for asset, values in CLOSES.items()}))
    late = states(snapshot())
    for short, long in zip(early, late, strict=True):
        assert len(short) < len(long) and tuple(short) == tuple(long[:len(short)])


@pytest.fixture(scope="module")
def baseline() -> bytes:
    """较早截止日的冻结候选与主参照已发布字段文件：经开发期输出的 serialize_daily 写出（与 M3 基准同一格式）。"""
    early_axis = AXIS[:EARLY + 1]
    early = run_window(snapshot(early_axis, {asset: values[:EARLY + 1] for asset, values in CLOSES.items()}),
                       spec(early_axis, None, Purpose.CONSTRUCTED), PARAMETERS, (FROZEN,))
    assert early.stop is None
    return serialize_daily(early, None)["daily_signals.csv.gz"]


def test_published_fields_match_and_internal_consistency(baseline: bytes, result: WindowResult) -> None:
    rows = parse_signals(baseline)
    recomputed = recompute_signals(snapshot(), FROZEN, registered.INITIAL, PARAMETERS.windows)
    document = compare_published(rows, recomputed, AXIS, "构造基准")
    assert document["passed"] is True and document["outcome"] == "相同" and document["mismatches"] == []
    keys = (candidate_key(FROZEN), REFERENCE)
    assert set(document["keys"]) == set(keys)
    counts = {key: sum(1 for row in rows if row[1] == key) for key in keys}
    assert all(document["keys"][key]["rows"] == counts[key] > 0 for key in keys)
    assert document["compared_cells"] == 5 * sum(counts.values())
    assert internal_consistency(recomputed, result, FROZEN) == []


def mutate(rows: list[tuple[str, ...]], index: int, column: int, value: str) -> list[tuple[str, ...]]:
    changed = list(rows)
    row = list(changed[index])
    row[column] = value
    changed[index] = tuple(row)
    return changed


def test_published_fields_mismatch_and_date_set_checks(baseline: bytes, result: WindowResult) -> None:
    rows = parse_signals(baseline)
    recomputed = recompute_signals(snapshot(), FROZEN, registered.INITIAL, PARAMETERS.windows)
    key = candidate_key(FROZEN)
    position = next(index for index, row in enumerate(rows) if row[1] == key)
    flipped = "一级" if rows[position][2] == "正常" else "正常"
    changed = compare_published(mutate(rows, position, 2, flipped), recomputed, AXIS, "构造基准")
    assert changed["passed"] is False and changed["mismatches"][0]["column"] == "risk"
    assert compare_published([*rows, rows[position]], recomputed, AXIS, "x")["passed"] is False        # 日期重复
    gap = [row for index, row in enumerate(rows) if index != position + 2 or row[1] != key]
    assert compare_published(gap, recomputed, AXIS, "x")["passed"] is False                            # 不连续
    missing = [row for row in rows if row[1] != REFERENCE]
    assert any("没有对象键" in item for item in compare_published(missing, recomputed, AXIS, "x")["problems"])
    early = mutate(rows, position, 0, AXIS[5].isoformat())                                             # 重算未覆盖
    assert compare_published(early, recomputed, AXIS, "x")["passed"] is False
    tampered = {**recomputed, key: tuple(dataclasses.replace(item, c1=99) if index == 400 else item
                                         for index, item in enumerate(recomputed[key]))}
    assert internal_consistency(tampered, result, FROZEN) != []
