"""小周期前瞻研究：构造数值均由注释中的手算过程确定。"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk import calendar as market_calendar
from market_risk.research import io as research_io
from market_risk.research.analysis import _distribution, _wilson_rows
from market_risk.research.features import FeatureEngine, high_position_days
from market_risk.research.groups import Candidate, classify_candidates, observations
from market_risk.research.io import ResearchInputs, _rows, load_development_feature_inputs, load_inputs
from market_risk.research.pullback import DangerPeriod, Episode, build_danger_periods, lead_lag, rule_performance
from market_risk.research.statistics import auc, bh_adjust, bootstrap_auc, mann_whitney_p, wilson
from market_risk.storage.paths import StoragePaths


def _days() -> tuple[dt.date, ...]:
    return tuple(dt.date(2008, 8, day) for day in (11, 12, 13, 14, 15, 18, 19, 20, 21))


def _prices(days: tuple[dt.date, ...]) -> dict[str, dict[dt.date, Decimal]]:
    return {"SPX": {day: Decimal("100") for day in days},
            "QQQ": {day: Decimal("200") for day in days}}


def test_overlap_transitive_merge_single_and_tied_endpoint() -> None:
    days = _days()
    episodes = (Episode("SPX", days[0], days[2], Decimal("100")),
                Episode("QQQ", days[2], days[4], Decimal("200")),
                Episode("SPX", days[4], days[5], Decimal("100")),
                Episode("QQQ", days[7], days[8], Decimal("200")))
    periods = build_danger_periods(episodes, _prices(days), days)
    # 前三段依次在 08-13、08-15 闭区间重叠，合并 08-11 至 08-18；末段独立。
    assert [(p.start, p.end, p.source) for p in periods] == [
        (days[0], days[5], "双指数"), (days[7], days[8], "QQQ")]
    assert periods[0].sessions == 6
    tied = build_danger_periods((Episode("SPX", days[0], days[2], Decimal("100")),
                                 Episode("QQQ", days[0], days[2], Decimal("200"))), _prices(days), days)
    assert tied[0].start_symbol == tied[0].end_symbol == "SPX+QQQ"
    assert lead_lag(tied[0], _prices(days), days)["early_bottom_rebound_pct"] is None


def test_multi_episode_uses_first_top_and_lowest_close_not_last_low() -> None:
    days = _days()
    prices = _prices(days)
    prices["SPX"][days[1]] = Decimal("95")
    prices["SPX"][days[2]] = Decimal("80")
    prices["SPX"][days[5]] = Decimal("90")
    episodes = (Episode("SPX", days[0], days[2], Decimal("100")),
                Episode("QQQ", days[1], days[4], Decimal("200")),
                Episode("SPX", days[3], days[5], Decimal("100")))
    period = build_danger_periods(episodes, prices, days)[0]
    assert period.spx_top == days[0]
    assert period.spx_bottom == days[2]  # 80 小于最后一段低点日的 90。
    scores = {(day, "v2-M"): {"total_min": "3" if day == days[1] else "0", "total_max": "3"}
              for day in days}
    result = rule_performance(period, "v2-M", scores, prices, days)
    # 首次预警日进度 = (100−95)/(100−80) = 1/4。
    assert result["spx_first_warning_progress"] == Decimal("0.25")


def test_equal_low_uses_earliest_trading_day() -> None:
    days = _days()[:6]
    prices = _prices(days)
    prices["SPX"][days[2]] = Decimal("80")
    prices["SPX"][days[4]] = Decimal("80")
    period = build_danger_periods((Episode("SPX", days[0], days[5], Decimal("100")),), prices, days)[0]
    assert period.spx_bottom == days[2]


def test_warning_coverage_unknown_and_early_downgrade() -> None:
    days = _days()[:6]
    prices = _prices(days)
    prices["SPX"].update({days[2]: Decimal("90"), days[3]: Decimal("80"),
                          days[4]: Decimal("95"), days[5]: Decimal("70")})
    period = build_danger_periods((Episode("SPX", days[1], days[5], Decimal("100")),), prices, days)[0]
    bounds = [(0, 0), (1, 2), (3, 3), (2, 4), (1, 2), (3, 3)]
    scores = {(day, "v2-M"): {"total_min": str(lo), "total_max": str(hi)}
              for day, (lo, hi) in zip(days, bounds, strict=True)}
    result = rule_performance(period, "v2-M", scores, prices, days)
    # 08-13 首次 ≥3；时段 08-12..18 共5日，两日预警，一日范围跨3；08-15 首次明确降级。
    assert result["first_warning"] == days[2]
    assert result["warning_offset"] == 1
    assert result["coverage_days"] == 2
    assert result["unknown_days"] == 1
    assert result["coverage"] == Decimal("0.4")
    assert result["first_downgrade"] == days[4]
    assert result["early_downgrade"] is True
    # 从降级日95到以后最低70：70/95−1；至终点也为70。
    assert result["spx_downgrade_to_min_pct"] == (Decimal("70") / Decimal("95") - 1) * 100


def test_initial_warning_window_uses_only_available_scores() -> None:
    days = _days()[:3]
    prices = _prices(days)
    period = build_danger_periods((Episode("SPX", days[0], days[2], Decimal("100")),), prices, days)[0]
    scores = {(day, "v2-M"): {"total_min": "3", "total_max": "3"} for day in days}
    result = rule_performance(period, "v2-M", scores, prices, days)
    assert result["warning_window_incomplete"] is True
    assert result["warning_offset"] == 0  # 第一个可得评分日就是起点，不伪造 -20。
    assert result["already_warning_at_window_start"] is False


def test_downgrade_before_danger_start_keeps_full_tail() -> None:
    days = _days()[:6]
    prices = _prices(days)
    prices["SPX"].update({days[1]: Decimal("95"), days[2]: Decimal("60"),
                          days[3]: Decimal("100"), days[4]: Decimal("80"), days[5]: Decimal("70")})
    period = build_danger_periods((Episode("SPX", days[3], days[5], Decimal("100")),), prices, days)[0]
    bounds = [(3, 3), (2, 2), (3, 3), (3, 3), (3, 3), (3, 3)]
    scores = {(day, "v2-M"): {"total_min": str(lo), "total_max": str(hi)}
              for day, (lo, hi) in zip(days, bounds, strict=True)}
    result = rule_performance(period, "v2-M", scores, prices, days)
    # 08-12 降级，08-13 的60虽早于危险起点，仍位于[降级日,终点]，不可漏掉。
    assert result["first_downgrade"] == days[1]
    assert result["spx_downgrade_to_min_pct"] == (Decimal("60") / Decimal("95") - 1) * 100


def test_danger_period_merge_sources_and_repeated_episodes_on_constructed_data() -> None:
    """危险时段的合并逻辑（构造数据）：按闭区间相交合并，来源按成员所属指数区分，同一指数可以有多段成员。

    九个交易日（行号 0—8）。五段回调：SPX [0,0]、QQQ [1,1]、SPX [3,4]、QQQ [4,6]、SPX [6,7]。
    SPX [0,0] 与 QQQ [1,1] 只是相邻、不相交 → 各自成段，来源分别为 SPX、QQQ。
    后三段在行号 4、6 处相接，传递合并为 [3,7]，来源“双指数”，其中 SPX 有两段成员。
    所以共 3 个危险时段：双指数 1、SPX 1、QQQ 1；含同一指数多段成员的只有 [3,7]。
    """
    days = _days()
    episodes = (Episode("SPX", days[0], days[0], Decimal("100")), Episode("QQQ", days[1], days[1], Decimal("200")),
                Episode("SPX", days[3], days[4], Decimal("100")), Episode("QQQ", days[4], days[6], Decimal("200")),
                Episode("SPX", days[6], days[7], Decimal("100")))
    periods = build_danger_periods(episodes, _prices(days), days)
    assert [(p.start, p.end, p.source) for p in periods] == [
        (days[0], days[0], "SPX"), (days[1], days[1], "QQQ"), (days[3], days[7], "双指数")]
    multi = [(p.start, p.end) for p in periods
             if sum(e.symbol == "SPX" for e in p.members) > 1 or sum(e.symbol == "QQQ" for e in p.members) > 1]
    assert multi == [(days[3], days[7])]
    assert [sum(p.source == name for p in periods) for name in ("双指数", "SPX", "QQQ")] == [1, 1, 1]


@pytest.mark.full_period
def test_formal_danger_period_counts() -> None:
    """需另行授权的全期回归（《暂停与纠错登记》补充裁决第 11 条）：保留，但默认不运行。

    它读取完整的正式回测（含 2017 年以后的数据），核对的是全期人工核对的危险时段件数；
    合并逻辑本身由上面的构造数据测试覆盖。未经负责人授权不得用 `-m full_period` 运行。
    """
    root = Path(__file__).resolve().parents[1]
    inputs = load_inputs(StoragePaths(root), "run_20260928T112013Z_2cc8969")
    periods = build_danger_periods(inputs.episodes, inputs.market, inputs.days)
    assert len(periods) == 57
    assert sum(p.source == "双指数" for p in periods) == 42
    assert sum(p.source == "SPX" for p in periods) == 11
    assert sum(p.source == "QQQ" for p in periods) == 4
    multi = [(p.start.isoformat(), p.end.isoformat()) for p in periods
             if sum(e.symbol == "SPX" for e in p.members) > 1
             or sum(e.symbol == "QQQ" for e in p.members) > 1]
    assert multi == [("2008-08-11", "2008-10-10"), ("2008-11-04", "2008-11-20"),
                     ("2020-02-19", "2020-03-12"), ("2022-08-15", "2022-10-14")]


def test_auc_bootstrap_mann_whitney_and_bh_by_hand() -> None:
    # 四个配对为 (胜、平、胜、胜)，AUC=(3+0.5)/4=0.875。
    assert auc([2, 3], [1, 2]) == 0.875
    # U=3.5；并列校正方差=1.5；连续性校正后 z=1/sqrt(1.5)。
    expected = math.erfc((1 / math.sqrt(1.5)) / math.sqrt(2))
    assert math.isclose(mann_whitney_p([2, 3], [1, 2]), expected)
    # 两个样本观测为1、3，对照簇恒为2；有放回抽样可得到 AUC 0、0.5、1。
    assert bootstrap_auc([1, 3], [2, 2], seed=20260928) == (0, 1)
    assert bh_adjust([0.01, 0.04, 0.03]) == [0.03, 0.04, 0.04]
    low, high = wilson(0, 1)
    assert low == 0
    assert 0 < high < 1


class _FakeEngine:
    def __init__(self, days: tuple[dt.date, ...], high: set[dt.date]) -> None:
        self.days = days
        self.high = high

    def window(self, day: dt.date, n: int) -> tuple[dt.date, ...]:
        return self.days[:n] if day in self.high else ()

    def value(self, name: str, day: dt.date) -> Decimal | None:
        return Decimal("100") if name == "SPX" else None

    def values(self, day: dt.date) -> dict[str, Decimal | None]:
        return {"manual_feature": Decimal(self.days.index(day))}


def _period(start: dt.date, end: dt.date) -> DangerPeriod:
    episode = Episode("SPX", start, end, Decimal("100"))
    return DangerPeriod(start, end, "SPX", "SPX", (episode,), "开发期", 1, start, None, end, None,
                        Decimal("-5"), None)


def test_candidate_categories_and_control_clusters(monkeypatch: pytest.MonkeyPatch) -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=i) for i in range(100))
    starts = (days[10], days[35])
    periods = (_period(starts[0], days[15]), _period(starts[1], days[40]))
    high = {days[i] for i in (5, 8, 10, 12, 20, 22, 29, 50, 53, 85)}
    engine = _FakeEngine(days, high)
    # 直接指定高位日，分类测试只检验与事后起点的相对交易日关系。
    monkeypatch.setattr("market_risk.research.groups.high_position_days", lambda *_: tuple(sorted(high)))
    grouped = classify_candidates(engine, periods, days, Decimal("0.99"))
    kinds = {item.date: item.category for item in grouped}
    assert kinds[days[5]] == "样本组"  # 5个交易日后开始。
    assert kinds[days[8]] == "样本组"
    assert kinds[days[10]] == "起点当天"
    assert kinds[days[12]] == "危险时段内且20日无新起点"
    assert kinds[days[20]] == "灰色区"  # 15日后开始新时段。
    assert kinds[days[29]] == "灰色区"  # 6日后开始。
    assert kinds[days[50]] == kinds[days[53]] == "对照组"
    assert kinds[days[85]] == "后续20日窗口不完整"
    obs = observations(engine, periods, grouped, days)
    controls = [item for item in obs if item.kind == "对照组"]
    assert any(item.dates == (days[50], days[53]) for item in controls)  # 序号差3，合为一簇。


def test_wilson_population_includes_candidate_inside_old_danger() -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=i) for i in range(20))
    periods = (_period(days[5], days[10]),)
    candidates = (Candidate(days[0], "样本组", (days[5],), False, True, False),
                  Candidate(days[8], "危险时段内且20日无新起点", (), True, True, False))
    scores = {(day, version): {"total_min": "3"} for day in (days[0], days[8])
              for version in ("v2-M", "v3-R1")}
    inputs = ResearchInputs("synthetic", scores, {}, (), (), (), 0, {}, {}, days)
    rows = _wilson_rows(candidates, inputs, periods, days)
    # 两个条件为是的候选日都进分母；只有 day0 的未来5日含危险起点，故为 1/2。
    assert [(row["condition_days"], row["future_start_days"], row["proportion"]) for row in rows] == [
        (2, 1, 0.5), (2, 1, 0.5)]


def test_inclusive_feature_windows_and_exact_high_position_boundary() -> None:
    day = dt.date(2008, 8, 11)
    sixty_days = tuple(market_calendar.stock_trading_days(dt.date(2008, 5, 1), day))[-60:]
    prices = {d: Decimal("100") for d in sixty_days}
    prices[day] = Decimal("99")
    breadth = {d: Decimal("50") for d in sixty_days}
    breadth[sixty_days[-2]] = Decimal("70")
    breadth[day] = Decimal("60")
    pcce = {d: Decimal(i) for i, d in enumerate(sixty_days[-10:], start=1)}
    scores = {(day, version): {name: "0" for name in
                                 ("total_min", "total_max", "price", "breadth", "vix", "rates", "credit")}
              for version in ("v2-M", "v3-R1")}
    inputs = ResearchInputs("synthetic", scores, {day: {}}, (), (), (), 0,
                            {"SPX": prices, "S5TW": breadth, "UST10Y": {}}, {"PCCE": pcce}, (day,))
    engine = FeatureEngine.create(inputs)
    assert high_position_days(engine, (day,), Decimal("0.99")) == (day,)  # 99 = 100 × 0.99。
    values = engine.values(day)
    assert values["S5TW_below_60d_high"] == Decimal("-10")  # 当日60 − 含当日窗口最高70。
    assert values["PCCE_ma10"] == Decimal("5.5")  # 当日及前9日的1..10，均值5.5。


def test_prestart_not_candidate_and_future_values_do_not_change_features(tmp_path: Path) -> None:
    day = dt.date(2008, 8, 11)
    future = dt.date(2008, 8, 12)
    market = {name: {day: Decimal("100"), future: Decimal("101")}
              for name in ("SPX", "SPY", "QQQ", "RSP", "HYG", "LQD", "UST10Y", "BAMLH0A0HYM2",
                           "VIXCLS", "VIX_CBOE", "S5FI", "S5TW")}
    tv = {name: {day: Decimal("10"), future: Decimal("11")}
          for name in ("ADD", "HIGN", "LOWN", "MMFI", "MMTW", "R2FI", "R2TW", "PCCE", "NDTW", "VIX3M")}
    metrics = {day: {"VIX": "20", "oas_o1_date_v3r1": "2008-08-08", "oas_o1_v3r1": "8"},
               future: {"VIX": "999"}}
    scores = {(day, version): {name: "0" for name in
                                 ("total_min", "total_max", "price", "breadth", "vix", "rates", "credit")}
              for version in ("v2-M", "v3-R1")}
    original = ResearchInputs("synthetic", scores, metrics, (), (), (), 0, market, tv, (day, future))
    base = FeatureEngine.create(original).values(day)
    altered_market = {name: dict(series) for name, series in market.items()}
    altered_tv = {name: dict(series) for name, series in tv.items()}
    for source in (altered_market, altered_tv):
        for series in source.values():
            series[future] = Decimal("999999")
    changed = FeatureEngine.create(replace(original, market=altered_market,
                                           tradingview=altered_tv)).values(day)
    assert base == changed
    # 即使此前有价格，调用者传入的样本日范围从2008-08-11开始。
    prior = day - dt.timedelta(days=1)
    assert all(d >= day for d in high_position_days(FeatureEngine.create(original), (day,), Decimal("0.99")))
    assert prior not in (day,)
    path = tmp_path / "only_until_validation.csv"
    path.write_text("date,value\n2022-12-30,1\n2023-01-03,999\n", encoding="utf-8")
    assert list(_rows(path)) == [{"date": "2022-12-30", "value": "1"}]


def test_future_extremes_leave_populated_formal_features_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只读开发期截止日（2016-12-30）以前的评分、指标与序列：读取函数在其后的第一行之前停止。
    # 研究模块的开发期截止常量是日历年末 2016-12-31（FRED 的 BAMLH0A0HYM2 有一条 2016-12-31 的观测），
    # 这里把截止日收紧到最后一个交易日 2016-12-30，使任何晚于它的行都不被读取。
    # 测试意图不变：在正式数据的 2015-10-30 上，把次一交易日的全部输入换成极端值，当日特征不变。
    monkeypatch.setattr(research_io, "DEVELOPMENT_END", dt.date(2016, 12, 30))
    inputs = load_development_feature_inputs(StoragePaths(Path(__file__).resolve().parents[1]),
                                             "run_20260928T112013Z_2cc8969")
    assert max(inputs.days) <= dt.date(2016, 12, 30)
    assert all(max(series, default=dt.date.min) <= dt.date(2016, 12, 30)
               for source in (inputs.market, inputs.tradingview) for series in source.values())
    day = dt.date(2015, 10, 30)
    future = dt.date(2015, 11, 2)
    original = FeatureEngine.create(inputs).values(day)
    assert sum(value is not None for value in original.values()) >= 40
    changed_market = {symbol: dict(series) for symbol, series in inputs.market.items()}
    changed_tv = {symbol: dict(series) for symbol, series in inputs.tradingview.items()}
    for source in (changed_market, changed_tv):
        for series in source.values():
            series[future] = Decimal("999999")
    changed_metrics = dict(inputs.metrics)
    changed_metrics[future] = {field: "999999" for field in inputs.metrics[day]}
    changed_scores = dict(inputs.scores)
    for version in ("v2-M", "v3-R1"):
        changed_scores[future, version] = {field: "999999" for field in inputs.scores[day, version]}
    changed = FeatureEngine.create(replace(inputs, market=changed_market, tradingview=changed_tv,
                                           metrics=changed_metrics, scores=changed_scores)).values(day)
    assert changed == original


def test_confirmed_drawdown_participants_and_tied_sources_by_hand() -> None:
    days = _days()[:4]
    prices = _prices(days)
    prices["SPX"][days[2]] = Decimal("80")
    prices["QQQ"][days[2]] = Decimal("100")
    only_spx = build_danger_periods((Episode("SPX", days[0], days[3], Decimal("100")),), prices, days)[0]
    # SPX 从首个高点100至区间最低80，跌幅为 -20%；QQQ 虽从200至100，却未参与该时段。
    assert only_spx.spx_drawdown_pct == Decimal("-20")
    assert only_spx.qqq_drawdown_pct is None
    tied = build_danger_periods((Episode("SPX", days[0], days[3], Decimal("100")),
                                 Episode("QQQ", days[0], days[3], Decimal("200"))), prices, days)[0]
    # 两个指数同日贡献高点与低点，端点来源均须保留两者。
    assert (tied.start_symbol, tied.end_symbol) == ("SPX+QQQ", "SPX+QQQ")


def test_confirmed_shared_candidate_partial_missing_and_zero_progress_denominator_by_hand(
        monkeypatch: pytest.MonkeyPatch) -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=i) for i in range(30))
    periods = (_period(days[3], days[3]), _period(days[5], days[5]))
    engine = _FakeEngine(days, {days[0], days[1]})
    monkeypatch.setattr("market_risk.research.groups.high_position_days", lambda *_: (days[0], days[1]))
    monkeypatch.setattr(engine, "values", lambda day: {
        "partial": Decimal("2") if day == days[0] else None, "empty": None})
    candidates = classify_candidates(engine, periods, days, Decimal("0.99"))
    observed = observations(engine, periods, candidates, days)
    # day0 距两段起点分别3、5日；day1 分别2、4日，两日均映射两段。
    assert [candidate.target_starts for candidate in candidates] == [(days[3], days[5])] * 2
    assert len(observed) == 2
    for item in observed:
        # 两日的 partial 为2与缺失，平均只用有效日，因此为2且有效日数1；empty全缺失。
        assert item.values == {"partial": Decimal("2"), "empty": None}
        assert item.effective_days == {"partial": 1, "empty": 0}
    prices = _prices(days)
    scores = {(day, "v2-M"): {"total_min": "3", "total_max": "3"} for day in days}
    performance = rule_performance(periods[0], "v2-M", scores, prices, days)
    # 高点=最低收盘价=100，进度分母为零，必须留空。
    assert performance["spx_first_warning_progress"] is None


def test_confirmed_auc_distribution_bootstrap_and_tie_corrected_u_by_hand() -> None:
    # 样本2、3与对照1、2配对：胜3次、并列1次，AUC=(3+0.5)/4=0.875。
    assert auc([2, 3], [1, 2]) == 0.875
    assert 2 * auc([2, 3], [1, 2]) - 1 == 0.75
    # 排序1、2、3、4后，线性插值 P25=1.75，中位=2.5，P75=3.25。
    assert _distribution([1, 2, 3, 4]) == "n=4；最小=1.0000；P25=1.7500；中位=2.5000；P75=3.2500；最大=4.0000"
    # 两个样本1、3对照恒2，有放回簇重抽样的分布可到0与1；2000次固定种子分位点均取端点。
    assert bootstrap_auc([1, 3], [2, 2], seed=20260928, repeats=2000) == (0, 1)
    # U=3.5、并列修正方差1.5、连续性校正后的 z=1/sqrt(1.5)，双侧p=erfc(z/sqrt(2))。
    expected = math.erfc((1 / math.sqrt(1.5)) / math.sqrt(2))
    assert math.isclose(mann_whitney_p([2, 3], [1, 2]), expected)


def test_confirmed_wilson_population_and_today_exclusion_by_hand() -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=i) for i in range(20))
    periods = (_period(days[5], days[10]), replace(_period(days[10], days[11]), period="验证期"))
    candidates = (Candidate(days[0], "样本组", (days[5],), False, True, True),
                  Candidate(days[5], "起点当天", (), True, True, True),
                  Candidate(days[8], "危险时段内且20日无新起点", (), True, True, False))
    scores = {(day, version): {"total_min": "3"} for day in (days[0], days[5], days[8])
              for version in ("v2-M", "v3-R1")}
    inputs = ResearchInputs("synthetic", scores, {}, (), (), (), 0, {}, {}, days)
    rows = _wilson_rows(candidates, inputs, periods, days)
    # 三个完整窗口且总分>=3的高位日都进分母；只有day0未来5日含开发期起点day5。
    # day5自身起点不算未来；day8未来虽含验证期起点day10，但开发期分子不计。
    assert [(row["condition_days"], row["future_start_days"], row["proportion"]) for row in rows] == [
        (3, 1, 1 / 3), (3, 1, 1 / 3)]


def test_confirmed_first_warning_window_incomplete_by_hand() -> None:
    days = _days()[:3]
    period = build_danger_periods((Episode("SPX", days[0], days[2], Decimal("100")),), _prices(days), days)[0]
    scores = {(day, "v2-M"): {"total_min": "3", "total_max": "3"} for day in days}
    row = rule_performance(period, "v2-M", scores, _prices(days), days)
    # 第一个评分日就是起点，实际偏移0；起点前20日评分不可得，不能写成-20。
    assert (row["warning_offset"], row["warning_window_incomplete"], row["already_warning_at_window_start"]) == (
        0, True, False)


def test_confirmed_feature_formulas_inclusive_windows_by_hand() -> None:
    day = dt.date(2008, 8, 11)
    window = tuple(market_calendar.stock_trading_days(dt.date(2008, 5, 1), day))[-60:]
    breadth = {d: Decimal("50") for d in window}
    breadth[window[-2]] = Decimal("70")
    breadth[day] = Decimal("60")
    pcce = {d: Decimal(i) for i, d in enumerate(window[-10:], start=1)}
    scores = {(day, version): {name: "0" for name in
                                 ("total_min", "total_max", "price", "breadth", "vix", "rates", "credit")}
              for version in ("v2-M", "v3-R1")}
    metrics = {day: {"SPY.ma5": "105", "SPY.ma20": "100", "QQQ.ma5": "95", "QQQ.ma20": "100"}}
    inputs = ResearchInputs("synthetic", scores, metrics, (), (), (), 0,
                            {"S5TW": breadth, "UST10Y": {}}, {"PCCE": pcce}, (day,))
    values = FeatureEngine.create(inputs).values(day)
    # 60日最高70，今日60，差距-10个百分点；105/100-1=+5%，95/100-1=-5%。
    # PCCE当日及前9日为1..10，平均5.5。
    assert values["S5TW_below_60d_high"] == Decimal("-10")
    assert values["SPY_ma5_vs_ma20_pct"] == Decimal("5")
    assert values["QQQ_ma5_vs_ma20_pct"] == Decimal("-5")
    assert values["PCCE_ma10"] == Decimal("5.5")


def test_confirmed_tied_bottom_opportunity_excluded_by_hand() -> None:
    days = _days()[:4]
    prices = _prices(days)
    prices["SPX"][days[2]] = Decimal("80")
    prices["QQQ"][days[2]] = Decimal("160")
    period = build_danger_periods((Episode("SPX", days[0], days[3], Decimal("100")),
                                   Episode("QQQ", days[1], days[3], Decimal("200"))), prices, days)[0]
    row = lead_lag(period, prices, days)
    # 两指数均在day2见最低收盘，无先见底者；反弹样本为空，分布n=0。
    assert row is not None
    assert row["bottom_first"] == "同日"
    assert row["early_bottom_rebound_pct"] is None
    assert _distribution([]) == "n=0"
