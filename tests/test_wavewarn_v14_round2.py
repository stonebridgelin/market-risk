"""v1.4 第二轮开发期诊断（补充登记 D）的测试：切换、转绿双层报告、完整净值。

期望值都是人工推算的，推算过程写在各测试的说明里。手算例只用构造的灯色、事件与价格；
读取真实数据的测试只读到 2016-12-30（开发期末）。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from market_risk.wavewarn.config_v14 import load_round2_config, load_validation_config, parse_round2_config
from market_risk.wavewarn.diagnostics_round2 import (
    CASH,
    FULLY_INVESTED,
    MA200,
    NO_MR,
    PORTFOLIO,
    SELECTED,
    Round2Result,
    round2_diagnostics,
)
from market_risk.wavewarn.diagnostics_round2_report import OPENING, csv_tables, report_lines
from market_risk.wavewarn.exit_costs import ExitCostEvent
from market_risk.wavewarn.extended_history import truncate_inputs
from market_risk.wavewarn.green_report import (
    CLASS_1,
    CLASS_2,
    CLASS_3,
    REASON_NEXT_EVENT,
    REASON_WINDOW_END,
    green_events,
    green_summary,
)
from market_risk.wavewarn.inputs import load_inputs_until
from market_risk.wavewarn.nav import (
    exposed_returns,
    max_drawdown,
    nav_metrics,
    portfolio_returns,
    running_drawdowns,
    simple_returns,
    wealth_path,
    worst_rolling,
)
from market_risk.wavewarn.switch_diagnostics import (
    TimingSplit,
    break_even_gamma,
    exposure_change,
    first_day_switches,
    holding_segments,
    holding_summary,
    reversal_count,
    switches_by_type,
    switches_by_year,
    window_switches,
)
from market_risk.wavewarn.v14_model import prepare_v14

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = load_validation_config(ROOT / "config/wavewarn_v14_validation.yaml")
CONFIG = load_round2_config(ROOT / "config/wavewarn_v14_diagnostics.yaml")
DEVELOPMENT_END = dt.date(2016, 12, 30)
D = Decimal
LEVELS = {"绿": D(1), "黄": D("0.5"), "红": D(0)}
# 十个交易日（九个区间）的执行灯色；日期只是标签，自 2015-12-27 起逐日。
LIGHTS = ("黄", "黄", "绿", "黄", "红", "红", "黄", "绿", "绿", "红")
DAYS = tuple(dt.date(2015, 12, 27) + dt.timedelta(days=index) for index in range(10))


def test_switches_inside_the_window_and_terminal_day() -> None:
    """窗口前一日的执行灯色也是黄（j₀ 当日没有切换）：

    行号 2 黄→绿（+0.5）、3 绿→黄（−0.5）、4 黄→红（−0.5）、6 红→黄（+0.5）、7 黄→绿（+0.5），共 5 次；
    行号 9 的绿→红在窗口末日，不计。Σ|Δe| = 0.5×5 = 2.5（若把末日的 1 计入，就会是 3.5）。
    执行日：12-29、12-30、12-31 属 2015 年（3 次），01-02、01-03 属 2016 年（2 次）。
    """
    switches = window_switches(DAYS, LIGHTS, LEVELS, "黄")
    assert [(item.index, item.before, item.after, item.change) for item in switches] == [
        (2, "黄", "绿", D("0.5")), (3, "绿", "黄", D("-0.5")), (4, "黄", "红", D("-0.5")),
        (6, "红", "黄", D("0.5")), (7, "黄", "绿", D("0.5"))]
    assert exposure_change(switches) == D("2.5")
    assert switches_by_year(switches) == {2015: 3, 2016: 2}
    assert switches_by_type(switches) == {("绿", "黄"): 1, ("黄", "绿"): 2, ("黄", "红"): 1, ("红", "黄"): 1,
                                          ("绿", "红"): 0, ("红", "绿"): 0}


def test_first_day_switch_is_counted_unless_there_was_no_prior_position() -> None:
    """《暂停与纠错登记》第 10 条：j₀ 当日执行灯色与前一日不同时计为一次切换，与主损失的计费口径一致。

    窗口前一日为绿、j₀ 当日为黄：多计一次 绿→黄（行号 0，−0.5），共 6 次，Σ|Δe| = 3.0；
    窗口前一日为红：多计一次 红→黄（+0.5）。前一日与 j₀ 同为黄：不多计。
    从无仓位开始（previous 为空）才是初始建仓，j₀ 当日不计。
    j₀ 当日的切换同样参加短期反转的判断：前一日为绿时，行号 0 的下降之后 5 日内（行号 2）有上升 → 反转数由 3 变 4。
    """
    counted = window_switches(DAYS, LIGHTS, LEVELS, "绿")
    first = counted[0]
    assert (first.index, first.before, first.after, first.change) == (0, "绿", "黄", D("-0.5"))
    assert len(counted) == 6 and exposure_change(counted) == D("3.0")
    assert [(item.index, item.change) for item in first_day_switches(counted)] == [(0, D("-0.5"))]
    assert window_switches(DAYS, LIGHTS, LEVELS, "红")[0].change == D("0.5")
    assert len(window_switches(DAYS, LIGHTS, LEVELS, "黄")) == 5
    assert len(window_switches(DAYS, LIGHTS, LEVELS, None)) == 5
    assert first_day_switches(window_switches(DAYS, LIGHTS, LEVELS, "黄")) == ()
    assert [reversal_count(counted, window) for window in (1, 2, 5)] == [1, 3, 4]
    assert switches_by_year(counted) == {2015: 4, 2016: 2}


def test_reversal_uses_exposure_direction_and_counts_each_switch_once_per_window() -> None:
    """方向：行号 2 上升、3 下降、4 下降、6 上升、7 上升。

    窗口 1：2 之后的 (2,3] 有下降 → 计；3 之后 (3,4] 只有同向的下降 → 不计；4、6、7 之后都没有反向 → 共 1。
    窗口 2：2 → 计；3 之后 (3,5] 只有 4（同向）→ 不计；4 之后 (4,6] 有 6（上升）→ 计；6、7 → 不计 → 共 2。
    窗口 5：2 之后 (2,7] 有 3、4 两次反向，只计一次；3 之后 (3,8] 有 6 → 计；4 之后 (4,9] 有 6 → 计；
            6、7 之后只有同向的上升（行号 9 的下降在窗口末日，不在切换之列）→ 不计 → 共 3（不是 4）。
    """
    switches = window_switches(DAYS, LIGHTS, LEVELS, "黄")
    assert [item.direction for item in switches] == ["暴露上升", "暴露下降", "暴露下降", "暴露上升", "暴露上升"]
    assert [reversal_count(switches, window) for window in (1, 2, 5)] == [1, 2, 3]
    # 黄→红 与 绿→红 同为暴露下降，不算互为反向；红→黄 之后的 黄→绿 也不算反向。
    same = window_switches(DAYS[:5], ("绿", "黄", "红", "黄", "黄"), LEVELS, "绿")
    assert [item.direction for item in same] == ["暴露下降", "暴露下降", "暴露上升"]
    assert reversal_count(same, 5) == 2      # 两次下降之后都有一次上升；上升之后没有下降


def test_holding_segments_lengths_and_truncation_flags() -> None:
    """区间 0—8 的执行灯色：黄黄｜绿｜黄｜红红｜黄｜绿绿 → 时长 2、1、1、2、1、2。

    首段：j₀ 当日没有切换（灯色是继承来的）→ 段的真实起点在窗口之前，标记截断；
    末段：窗口末日恰有一次切换（绿→红，不计费）→ 段在窗口末日结束，不算截断。
    黄：时长 (2,1,1) → 中位 1，P25 = 1，P75 = 1 + 0.5×(2−1) = 1.5，最大 2，其中 1 段被截断。
    绿：时长 (1,2) → 中位 1.5，P25 = 1.25，P75 = 1.75。
    """
    segments = holding_segments(DAYS, LIGHTS, False, True)
    assert [(item.light, item.length, item.truncated) for item in segments] == [
        ("黄", 2, True), ("绿", 1, False), ("黄", 1, False), ("红", 2, False), ("黄", 1, False), ("绿", 2, False)]
    assert (segments[0].start, segments[0].end, segments[-1].start, segments[-1].end) == (
        DAYS[0], DAYS[1], DAYS[7], DAYS[8])
    yellow, green = holding_summary(segments, "黄"), holding_summary(segments, "绿")
    assert (yellow.count, yellow.median, yellow.p25, yellow.p75, yellow.maximum, yellow.truncated) == (
        3, D(1), D(1), D("1.5"), 2, 1)
    assert (green.median, green.p25, green.p75, green.truncated) == (D("1.5"), D("1.25"), D("1.75"), 0)
    # 反过来：j₀ 当日恰有切换、窗口末日没有切换 → 首段不截断，末段截断。
    other = holding_segments(DAYS, LIGHTS, True, False)
    assert (other[0].truncated, other[-1].truncated) == (False, True)
    assert holding_summary(segments, "红").count == 1 and holding_summary((), "红").median is None


def test_break_even_gamma_is_the_flip_point() -> None:
    """T价格：模型 −0.23 − 0.97 = −1.2（194 次），均线 −0.41 − 0.22 = −0.63（44 次）。

    γ* = (−0.63 − (−1.2)) ÷ (194 − 44) = 0.57 ÷ 150 = 0.0038；
    验算：−1.2 + 0.0038×194 = −0.4628 = −0.63 + 0.0038×44。
    """
    model, baseline = TimingSplit(D("-0.23"), D("0.97"), 194), TimingSplit(D("-0.41"), D("0.22"), 44)
    assert (model.price_score, baseline.price_score) == (D("-1.20"), D("-0.63"))
    gamma = break_even_gamma(model, baseline)
    assert gamma == D("0.0038")
    assert model.price_score + gamma * 194 == baseline.price_score + gamma * 44 == D("-0.4628")
    assert break_even_gamma(model, TimingSplit(D(0), D(0), 194)) is None


AXIS = tuple(dt.date(2014, 1, 1) + dt.timedelta(days=index) for index in range(30))


def _cost(trough: int, rebound_class: str | None, first_green: int | None, inclusion: str = "纳入",
          early: int = 0, decline: str | None = None) -> ExitCostEvent:
    return ExitCostEvent("SPX", AXIS[max(trough - 3, 0)], AXIS[trough], inclusion, rebound_class,  # type: ignore[arg-type]
                         None, early, D(decline) if decline else None, None, None, None,
                         AXIS[first_green] if first_green is not None else None, ())


def test_green_report_reasons_windows_and_two_denominators() -> None:
    """日期轴 30 天（行号 0—29，窗口最后一日为行号 29），观察窗口 8 个交易日。

    E1 ②：Tr=5，转绿 9 → 延迟 4；5+8=13 ≤ 29 完整；8 日内转绿。
    E2 ②：Tr=10，转绿 20 → 延迟 10；完整；不在 8 日内。
    E3 ①：Tr=12，低点前已绿（此前转绿 2 次，其后最深跌幅 3%）；完整；不进入延迟分布，也不算 8 日内转绿。
    E4 ③：Tr=15，下一事件 T0 在行号 18；实际到行号 24 才转绿 → 原因 (a)，可观察的延迟 9。
    E5 ③：Tr=25，没有下一事件，到窗口末日仍未转绿 → 原因 (b)；25+8=33 > 29 观察窗口不完整；已过 29−25 = 4 日。
    E6 ②：Tr=22，转绿 26 → 延迟 4；22+8=30 > 29 观察窗口不完整，但已观察到 8 日内转绿。
    E7 右截尾：不纳入。
    纳入 6 件：①1、②3、③2；③(a) 1、(b) 1；完整窗口 4 件（E1—E4）。
    8 日内转绿：E1、E6 → 2/6（分母为全部纳入）；只看完整窗口的事件：E1 → 1/4。
    ②延迟 (4,10,4) 中位 4；保守口径 (4,4,10,∞,∞) 中位 10。
    """
    costs = (_cost(5, CLASS_2, 9), _cost(10, CLASS_2, 20), _cost(12, CLASS_1, None, early=2, decline="0.03"),
             _cost(15, CLASS_3, 24), _cost(25, CLASS_3, None), _cost(22, CLASS_2, 26),
             _cost(28, None, None, inclusion="右截尾"))
    following = (AXIS[9], AXIS[11], AXIS[14], AXIS[18], None, AXIS[27], None)
    events = green_events(AXIS, costs, following, 8)
    assert len(events) == 6
    assert [(item.delay, item.complete_window, item.within_window) for item in events] == [
        (4, True, True), (10, True, False), (None, True, False), (9, True, False), (None, False, False),
        (4, False, True)]
    assert [item.class_3_reason for item in events] == ["", "", "", REASON_NEXT_EVENT, REASON_WINDOW_END, ""]
    assert (events[3].first_green, events[4].first_green, events[4].unobserved_days) == (AXIS[24], None, 4)
    assert (events[2].first_green, events[2].half_way_green_count, events[2].deepest_decline) == (None, 2, D("0.03"))
    summary = green_summary("SPX", events)
    assert (summary.included, summary.class_1, summary.class_2, summary.class_3) == (6, 1, 3, 2)
    assert (summary.class_3_next_event, summary.class_3_window_end, summary.complete_window) == (1, 1, 4)
    assert (summary.within_window, summary.within_window_complete) == (2, 1)
    assert (summary.within_share_all, summary.within_share_complete) == (D(2) / D(6), D("0.25"))
    assert summary.share(summary.class_1) == summary.share(summary.class_3_next_event) == D(1) / D(6)
    assert summary.median_class_2 == D(4) and summary.median_conservative == D(10)
    # 类别③多于一半时保守口径中位为无穷（显示为超限），而有条件的中位数不变。
    mostly = green_summary("SPX", (events[0], events[3], events[4]))
    assert mostly.median_class_2 == D(4) and mostly.median_conservative.is_infinite()


def _closes(values: tuple[str, ...]) -> dict[dt.date, Decimal | None]:
    return {day: D(value) for day, value in zip(AXIS, values, strict=False)}


def test_single_asset_nav_by_hand_differs_from_exposure_times_log_return() -> None:
    """收盘价 100、110、99、99，暴露 e = (1, 0.5, 0)。

    简单收益：+10%、−10%、0。R = 1×0.10、0.5×(−0.10)、0×0 = 0.10、−0.05、0。
    W：1 → 1.1 → 1.1×0.95 = 1.045 → 1.045。
    对比：若用 e·对数收益再取指数，第二个区间是 exp(0.5×ln 0.9) − 1 = √0.9 − 1 ≈ −0.05132，不等于 −0.05。
    """
    days = AXIS[:4]
    returns = simple_returns(days, _closes(("100", "110", "99", "99")))
    assert returns == (D("0.1"), D("-0.1"), D(0))
    exposed = exposed_returns((D(1), D("0.5"), D(0)), returns)
    assert exposed == (D("0.1"), D("-0.05"), D(0))
    assert wealth_path(exposed) == (D(1), D("1.1"), D("1.045"), D("1.045"))
    log_version = (D("0.5") * D("0.9").ln()).exp() - 1
    assert abs(log_version - D("-0.0513167")) < D("1e-6") and exposed[1] != log_version
    # 缺价不填补。
    with pytest.raises(ValueError, match="不插值"):
        simple_returns(days, {**_closes(("100", "110", "99", "99")), days[2]: None})


def test_portfolio_nav_rebalances_daily_to_half_and_half() -> None:
    """A：100、110、99、99（+10%、−10%、0）；B：50、50、55、44（0、+10%、−20%）；e = (1, 0.5, 0.5)，各半。

    R_0 = 0.5×1×0.10 + 0.5×1×0 = 0.05；
    R_1 = 0.5×0.5×(−0.10) + 0.5×0.5×0.10 = 0；
    R_2 = 0.5×0.5×0 + 0.5×0.5×(−0.20) = −0.05。
    W：1 → 1.05 → 1.05 → 0.9975。
    """
    days = AXIS[:4]
    returns = {"SPX": simple_returns(days, _closes(("100", "110", "99", "99"))),
               "QQQ": simple_returns(days, _closes(("50", "50", "55", "44")))}
    half = {"SPX": D("0.5"), "QQQ": D("0.5")}
    combined = portfolio_returns((D(1), D("0.5"), D("0.5")), returns, half)
    assert combined == (D("0.05"), D(0), D("-0.05"))
    assert wealth_path(combined) == (D(1), D("1.05"), D("1.05"), D("0.9975"))
    # 现金（e=0）收益为 0。
    assert portfolio_returns((D(0),) * 3, returns, half) == (D(0),) * 3
    with pytest.raises(ValueError, match="权重"):
        portfolio_returns((D(1),) * 3, returns, {"SPX": D("0.5"), "QQQ": D("0.4")})


WEALTH = (D(1), D("1.2"), D("0.9"), D("1.08"), D("0.96"), D("1.3"))


def test_drawdown_duration_and_recovery_boundaries() -> None:
    """净值 1、1.2、0.9、1.08、0.96、1.3（行号 0—5）：最大回撤 25%，峰值行号 1、谷底行号 2。

    峰值到谷底 1 个交易日；谷底之后净值首次回到或超过峰值 1.2 是行号 5（1.3）→ 谷底到恢复 3 个交易日。
    只看前 5 天（行号 0—4）：窗口内没有恢复，恢复日与恢复天数为空，谷底到窗口末日已过 2 个交易日。
    恰好回到峰值也算恢复：1、1.2、0.9、1.2 → 恢复在行号 3，1 个交易日。
    谷底就是窗口最后一天：未恢复，已过 0 日。
    """
    days = AXIS[:6]
    full = max_drawdown(days, WEALTH)
    assert (full.depth, full.decline_days, full.recovery_date, full.recovery_days, full.days_after_trough) == (
        D("0.25"), 1, days[5], 3, 3)
    cut = max_drawdown(days[:5], WEALTH[:5])
    assert (cut.depth, cut.recovery_date, cut.recovery_days, cut.days_after_trough) == (D("0.25"), None, None, 2)
    exact = max_drawdown(days[:4], (D(1), D("1.2"), D("0.9"), D("1.2")))
    assert (exact.recovery_date, exact.recovery_days) == (days[3], 1)
    last = max_drawdown(days[:3], (D(1), D("1.2"), D("0.9")))
    assert (last.recovery_days, last.days_after_trough, last.decline_days) == (None, 0, 1)
    # 从未回撤：深度 0，下跌与恢复天数都是 0。
    flat = max_drawdown(days[:3], (D(1), D("1.1"), D("1.2")))
    assert (flat.depth, flat.decline_days, flat.recovery_days) == (D(0), 0, 0)
    assert running_drawdowns(WEALTH) == (D(0), D(0), D("0.25"), D("0.1"), D("0.2"), D(0))


def test_max_drawdown_and_rolling_return_boundaries() -> None:
    """净值 1、1.2、0.9、1.08、0.96、1.3（行号 0—5）。

    最大回撤：高点 1.2（行号 1）到 0.9（行号 2）= 1 − 0.9/1.2 = 0.25；其后 0.96 相对 1.2 只有 0.2。
    滚动 2 个区间：0.9/1 − 1 = −0.1（行号 0→2）、1.08/1.2 − 1 = −0.1（1→3）、0.96/0.9 − 1、1.3/1.08 − 1；
    最差 −0.1，并列时取最早的一段（行号 0 至 2）。
    滚动 5 个区间：只有一段 1.3/1 − 1 = 0.3（窗口恰等于区间数）；滚动 6 个区间：区间数不足，为空。
    """
    days = AXIS[:6]
    drawdown = max_drawdown(days, WEALTH)
    assert (drawdown.depth, drawdown.peak_date, drawdown.trough_date) == (D("0.25"), days[1], days[2])
    two = worst_rolling(days, WEALTH, 2)
    assert (two.value, two.start, two.end) == (D("-0.1"), days[0], days[2])
    five, six = worst_rolling(days, WEALTH, 5), worst_rolling(days, WEALTH, 6)
    assert (five.value, five.start, five.end) == (D("0.3"), days[0], days[5])
    assert (six.value, six.start, six.end) == (None, None, None)
    # 从未回撤：深度 0，起止日同为首日。同样深度出现两次时取最早的一次。
    rising = max_drawdown(days[:3], (D(1), D("1.1"), D("1.2")))
    assert (rising.depth, rising.peak_date, rising.trough_date) == (D(0), days[0], days[0])
    twice = max_drawdown(days[:4], (D(1), D("0.5"), D(1), D("0.5")))
    assert (twice.depth, twice.peak_date, twice.trough_date) == (D("0.5"), days[0], days[1])


def test_nav_metrics_by_hand() -> None:
    """R = (0.10, −0.05, 0)，年交易日数取 3（使年化指数为 1）。

    累计 = 1.1×0.95×1 − 1 = 0.045；年化 = 1.045^(3/3) − 1 = 0.045。
    均值 = 0.05/3；离差平方和 = (0.10−0.05/3)² + (−0.05−0.05/3)² + (0.05/3)² = 0.035/3；
    样本方差 = 0.035/3 ÷ 2 = 0.0175/3；年化波动率 = √(0.0175/3 × 3) = √0.0175 ≈ 0.1322876。
    最大回撤 = 1 − 1.045/1.1 = 0.05（行号 1 至 2）。
    """
    metrics = nav_metrics(AXIS[:4], (D("0.1"), D("-0.05"), D(0)), (2, 5), 3)
    assert (metrics.intervals, metrics.cumulative, metrics.annualized) == (3, D("0.045"), D("0.045"))
    assert abs(metrics.volatility - D("0.0175").sqrt()) < D("1e-20")
    assert (metrics.drawdown.depth, metrics.drawdown.peak_date, metrics.drawdown.trough_date) == (
        D("0.05"), AXIS[1], AXIS[2])
    assert metrics.rolling[0].value == D("1.045") / D("1.1") - 1 and metrics.rolling[1].value is None


def test_round2_config_is_pinned_to_registration() -> None:
    assert (CONFIG.reversal_windows, CONFIG.green_window, CONFIG.rolling_windows) == ((5, 10), 8, (20, 60, 120))
    raw = yaml.safe_load((ROOT / "config/wavewarn_v14_diagnostics.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="补充登记"):
        parse_round2_config({**raw, "green_window": 10})
    with pytest.raises(ValueError, match="补充登记"):
        parse_round2_config({**raw, "reversal_windows": [5, 20]})


# ---------------------------------------------------------------------------
# 真实数据：只读到 2016-12-30
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def development() -> Round2Result:
    inputs = load_inputs_until(ROOT, DEVELOPMENT_END, VALIDATION.vix3m_file)
    return round2_diagnostics(prepare_v14(VALIDATION.model, inputs), VALIDATION, CONFIG)


def test_window_ends_with_the_last_interval_inside_development(development: Round2Result) -> None:
    result = development
    # 最后一个区间为 2016-12-29 收盘至 2016-12-30 收盘：next_date 不晚于 2016-12-30，没有 2017 年的价格。
    assert (result.days[0], result.days[-2], result.days[-1]) == (
        dt.date(2009, 12, 31), dt.date(2016, 12, 29), DEVELOPMENT_END)
    assert result.prepared.inputs.days[-1] == DEVELOPMENT_END and len(result.days) - 1 == 1762
    assert all(max(series) <= DEVELOPMENT_END for series in result.prepared.inputs.series.values())
    assert all(row.metrics.intervals == 1762 and len(row.returns) == 1762 for row in result.nav)
    # 输入若不是恰好截至开发期末，诊断拒绝运行（这里把输入截短，仍只用开发期数据）。
    shorter = dataclasses.replace(result.prepared,
                                  inputs=truncate_inputs(result.prepared.inputs, dt.date(2016, 12, 29)))
    with pytest.raises(ValueError, match="截至开发期末"):
        round2_diagnostics(shorter, VALIDATION, CONFIG)


def test_switch_counts_match_registered_billing_and_initial_position_is_excluded(
        development: Round2Result) -> None:
    result = development
    assert [item.name for item in result.objects] == [SELECTED, NO_MR, MA200]
    for item in result.objects:
        # 与主损失计费的切换次数一致；j₀ 当日的初始暴露继承此前信号，不在切换之列。
        assert len(item.switches) == item.evaluated.billed_switches == item.split.switches
        assert item.split.switch_cost == Decimal("0.005") * len(item.switches)
        assert all(0 <= switch.index < len(result.days) - 1 for switch in item.switches)
        # 开发期窗口里三个对象在 j₀ 当日都没有切换：新旧两种口径的切换次数相同。
        assert first_day_switches(item.switches) == ()
        assert item.initial_light == item.evaluated.system_executed[0]
        assert sum(segment.length for segment in item.segments) == 1762
        assert item.segments[0].truncated and item.segments[0].start == result.days[0]
    selected, _, baseline = result.objects
    # 200 日均线不含 BW，切换次数不受纠错影响。选定设定含 BW：原实现下为 194 次（见 v1.4-asrun 的输出），
    # 补丁 f_BW 之后的数值以机械重跑入库的 objects.csv 为准，这里不再写死，只核对与计费口径一致（上面的循环）。
    assert len(baseline.switches) == 44 and len(selected.switches) > len(baseline.switches)
    # 200 日均线没有黄灯：只有绿↔红。
    assert {(item.before, item.after) for item in baseline.switches} == {("绿", "红"), ("红", "绿")}
    # T = T价格 + 切换项。
    assert selected.split.price_score + selected.split.switch_cost == selected.timing.score


def test_nav_reference_rows_follow_definitions(development: Round2Result) -> None:
    result = development
    rows = {(row.name, row.scope): row for row in result.nav}
    closes = result.prepared.inputs.series
    for symbol in ("SPX", "QQQ"):
        # 满仓单资产的累计收益就是窗口首末收盘价之比减一；现金恒为 0。
        expected = closes[symbol][result.days[-1]] / closes[symbol][result.days[0]] - 1
        assert abs(rows[FULLY_INVESTED, symbol].metrics.cumulative - expected) < Decimal("1e-18")
        assert rows[CASH, symbol].metrics.cumulative == 0
    assert rows[CASH, PORTFOLIO].metrics.volatility == 0 and rows[CASH, PORTFOLIO].metrics.drawdown.depth == 0
    # 双资产每日再平衡：当日收益是两个单资产当日收益的各半。
    for name in (SELECTED, MA200, FULLY_INVESTED):
        both = rows[name, PORTFOLIO].returns
        assert all(abs(both[index] - (rows[name, "SPX"].returns[index] + rows[name, "QQQ"].returns[index]) / 2)
                   < Decimal("1e-24") for index in range(0, 1762, 97))
    # 七个对象各三个口径；同平均暴露基准的切换次数与目标暴露变化量为 0。
    assert len(result.nav) == 21
    constant = [row for row in result.nav if row.name.startswith("恒定暴露")]
    assert len(constant) == 6 and all(row.switches == 0 and row.exposure_change == 0 for row in constant)


def test_report_opening_and_tables(development: Round2Result) -> None:
    lines = report_lines(development, CONFIG)
    assert lines[2] == OPENING
    assert OPENING == "本报告仅使用开发期数据；诊断不改变模型规则、参数、选定设定与γ。"
    text = "\n".join(lines)
    for heading in ("## 一、对象与初始暴露", "## 二、切换诊断", "## 三、转绿双层报告", "## 四、完整净值"):
        assert heading in text
    names = [name for name, _, _ in csv_tables(development, CONFIG)]
    assert names == ["objects.csv", "switch_by_year.csv", "switch_by_type.csv", "switch_reversals.csv",
                     "holding_segments.csv", "holding_summary.csv", "green_summary.csv", "green_events.csv",
                     "nav_metrics.csv"]
    assert all(len(row) == len(header) for _, header, rows in csv_tables(development, CONFIG) for row in rows)


def test_round2_algorithm_modules_do_not_import_io_modules() -> None:
    """依赖方向：诊断的纯计算模块不得导入读写模块、services 或 CLI（含传递依赖）。"""
    code = ("import importlib, json, sys\n"
            "for name in ('switch_diagnostics', 'green_report', 'nav', 'diagnostics_round2',"
            " 'diagnostics_round2_report'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.evaluation_v14_run',"
            " 'market_risk.wavewarn.validation_output', 'market_risk.wavewarn.validation_run',"
            " 'market_risk.wavewarn.diagnostics_round2_run', 'market_risk.wavewarn.lock_guard',"
            " 'market_risk.services', 'market_risk.cli') or m.startswith('market_risk.scoring')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
