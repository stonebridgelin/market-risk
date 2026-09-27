"""规则A（v2-M）评分测试：每个维度的2/1/0分支、等号边界、待补枚举（SPEC 9节阶段3验收）。"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import load_sample_raw
from scoring_helpers import DEFAULT_ETFS, completed_three_segment, etf, make, no_three_segment

from market_risk.config import load_settings
from market_risk.data.snapshot import build_snapshot
from market_risk.models import BreadthReading, OasVintageValue
from market_risk.near_threshold import near_threshold_items
from market_risk.scoring import v2m
from market_risk.scoring.common import stage_of

D = dt.date


# ---------------------------------------------------------------------------
# 一、价格
# ---------------------------------------------------------------------------


def test_price_2a_two_below_ma50():
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 97, 100, 99, 98, 90),
            "QQQ": etf("QQQ", 97.99, 100, 99, 98, 90)}
    p = v2m.score_price(make(etfs=etfs), True)
    assert p.score == 2 and "2分(a)" in p.triggered_conditions


def test_price_2b_three_segment_completed():
    ts = no_three_segment()
    ts = {True: (completed_three_segment("RSP"), *ts[True][:2]), False: ts[False]}
    p = v2m.score_price(make(three_segment=ts), True)
    assert p.score == 2 and p.triggered_conditions == ("2分(b)",)
    assert "RSP" in p.calculation


def test_price_2c_spy_below_ma200():
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 100, 100, 99, 98, 100.01)}
    p = v2m.score_price(make(etfs=etfs), True)
    assert p.score == 2 and p.triggered_conditions == ("2分(c)",)


def test_price_0_and_boundary_close_equal_ma50():
    """收盘价恰好等于 MA50：不算"低于"。"""
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 98, 100, 97, 98, 90)}
    assert v2m.score_price(make(etfs=etfs), True).score == 0
    # 一只低于 MA20 仍可为0分
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 98.5, 100, 99, 98, 90)}
    assert v2m.score_price(make(etfs=etfs), True).score == 0


def test_price_1_branches():
    one_below50 = {**DEFAULT_ETFS, "SPY": etf("SPY", 97, 100, 99, 98, 90)}
    assert v2m.score_price(make(etfs=one_below50), True).score == 1
    two_below20 = {**DEFAULT_ETFS, "SPY": etf("SPY", 98.5, 100, 99, 98, 90),
                   "QQQ": etf("QQQ", 98.5, 100, 99, 98, 90)}
    assert v2m.score_price(make(etfs=two_below20), True).score == 1


def test_price_ma_is_exact_not_rounded():
    """SPEC 5.3：均线是派生值，不取整。MA50=98.004 时收盘价 98.00 低于 MA50。"""
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 98.0, 100, 97, 98.004, 90),
            "QQQ": etf("QQQ", 98.0, 100, 97, 98.004, 90)}
    p = v2m.score_price(make(etfs=etfs), True)
    assert p.score == 2 and p.triggered_conditions == ("2分(a)",)
    assert "MA50 98.00" in p.calculation  # 展示时保留两位
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 98.0, 100, 97, 98.0, 90),
            "QQQ": etf("QQQ", 98.0, 100, 97, 98.0, 90)}
    assert v2m.score_price(make(etfs=etfs), True).score == 0


def test_three_segment_scope_dependency_note():
    ts = {True: (completed_three_segment("SPY"),), False: no_three_segment()[False]}
    result = v2m.score(make(three_segment=ts))
    assert result.price.score == 2          # 计分以 SOP 7.2（含 T−20）为准
    assert any("三环节结果依赖口径" in n and "参考口径" in n for n in result.notes)
    assert v2m.score_price(make(three_segment=ts), False).score == 0   # 参考口径
    assert v2m.D1_INCLUDES_T_MINUS_20 is True


# ---------------------------------------------------------------------------
# 二、广度
# ---------------------------------------------------------------------------


def test_breadth_2a():
    b = v2m.score_breadth(make(f=39.0, w=45.0, f5=41.0, w5=46.0))
    assert b.score == 2 and "2分(a)" in b.triggered_conditions


def test_breadth_2b_spy_near_high_boundary():
    """F<40 且 SPY 收盘价 ≥ 20日最高×0.98；恰好等于时成立。"""
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 98.00, 100, 97, 97, 90)}
    b = v2m.score_breadth(make(etfs=etfs, spy_window_max=100.00, f=39.99, w=60.0))
    assert b.score == 2 and b.triggered_conditions == ("2分(b)",)
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 97.99, 100, 97, 97, 90)}
    b = v2m.score_breadth(
        make(etfs=etfs, spy_window_max=100.00, f=39.99, w=60.0, f5=30.0, w5=30.0)
    )
    assert b.score == 1


def test_breadth_l_exactly_50_is_0():
    assert v2m.score_breadth(make(f=50.00, w=70.0)).score == 0
    assert v2m.score_breadth(make(f=49.99, w=70.0)).score == 1


def test_breadth_f_exactly_40_not_below_40():
    """F 恰好为40：不满足 F<40；L=40 也不满足 L<40。"""
    b = v2m.score_breadth(make(f=40.00, w=45.0, f5=50.0, w5=50.0))
    assert b.score == 1


def test_breadth_f5_w5_not_needed_when_l_ge_50():
    """L≥50 时 F5/W5 缺失不影响结果：给确定0分并说明理由。"""
    b = v2m.score_breadth(make(f=58.44, w=76.73))
    assert b.score == 0 and b.pending_reason is None
    assert "缺失" in b.calculation and "不改变结果" in b.calculation


def test_breadth_f5_w5_pending_when_needed():
    """L<40、SPY 不接近高点：F5/W5 决定是否满足2分(a)，记待补 {1,2}。"""
    b = v2m.score_breadth(make(f=38.0, w=39.0))
    assert b.score is None and b.possible_scores == (1, 2)
    assert "5日前S5FI" in b.pending_reason


def test_breadth_2b_determinate_without_f5():
    """F<40 且 SPY 接近20日高点：不论 F5/W5 如何都是2分。"""
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 99.0, 100, 97, 97, 90)}
    b = v2m.score_breadth(make(etfs=etfs, spy_window_max=100.0, f=38.0, w=39.0))
    assert b.score == 2


def test_breadth_40_to_50_determinate_1():
    """L 在 40 至 50 之间、F≥40：F5/W5 缺失不影响，确定1分（SOP 4.2）。"""
    assert v2m.score_breadth(make(f=45.0, w=48.0)).score == 1


def test_enumeration_uses_known_fields():
    """SPEC 5.3：F 缺失、W 已知时，L=min(F,W) 不超过 W。W=45 时 L≥50 不可能，结果确定为1分。"""
    from decimal import Decimal

    from market_risk.scoring.common import Outcome, evaluate_dimension, grid

    def rule(v):
        low = min(v["F"], v["W"])
        return Outcome(0, (), "") if low >= Decimal("50") else Outcome(1, (), "")

    def cand(assigned):
        return grid(Decimal("40"), Decimal("50"), *assigned.values(),
                    lo=Decimal("0"), hi=Decimal("100"))

    d = evaluate_dimension("广度", {"F": None, "W": Decimal("45.00")}, rule, {"F": cand}, {})
    assert d.score == 1 and "不改变结果" in d.calculation
    d = evaluate_dimension("广度", {"F": None, "W": Decimal("55.00")}, rule, {"F": cand}, {})
    assert d.score is None and d.possible_scores == (0, 1)


def test_grid_uses_critical_points():
    from decimal import Decimal

    from market_risk.scoring.common import grid

    g = grid(Decimal("40"), Decimal("20.8") / Decimal("1.3"), lo=Decimal("0"), hi=Decimal("100"))
    # 40 本身与两侧相邻值；16.0 为 20.80/1.3 的精确值；范围两端
    assert {Decimal("39.99"), Decimal("40.00"), Decimal("40.01")} <= set(g)
    assert {Decimal("15.99"), Decimal("16.00"), Decimal("16.01")} <= set(g)
    assert {Decimal("0"), Decimal("100")} <= set(g)
    # 临界点不是两位小数：取两侧最近的两位小数值及再外侧的值
    g2 = grid(Decimal("26") / Decimal("1.3") + Decimal("0.001"), lo=Decimal("0"), hi=Decimal("50"))
    assert {Decimal("19.99"), Decimal("20.00"), Decimal("20.01"), Decimal("20.02")} <= set(g2)
    assert len(g2) == 6  # 4 个临界值 + 2 个端点，不按固定步长取样


def test_breadth_reading_missing_is_pending():
    b = v2m.score_breadth(make(f=None, w=None))
    assert b.score is None and b.possible_scores == (0, 1, 2)


# ---------------------------------------------------------------------------
# 三、VIX
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("v", "v5", "expected"),
    [
        (25.00, 30.0, 2),     # V 恰好 25
        (24.99, 30.0, 1),
        (26.00, 20.00, 2),    # V≥25
        (20.00, 15.38, 2),    # g=30.04%
        (20.80, 16.00, 2),    # g 恰好 30%
        (20.79, 16.00, 1),    # g=29.94%
        (19.99, 10.00, 1),    # V<20，g 大也不满足2分
        (17.99, 15.00, 0),    # V<18，g=19.93%
        (18.00, 17.00, 1),    # V 恰好18：不满足 V<18
        (12.00, 10.00, 1),    # g 恰好 20%：不满足 g<20%
        (11.99, 10.00, 0),
    ],
)
def test_vix_branches_and_boundaries(v, v5, expected):
    assert v2m.score_vix(make(v=v, v5=v5)).score == expected


def test_vix_g_is_exact_not_rounded():
    """SPEC 5.3：g 精确计算、不取整。V5=20.00 时 V=25.99（g=29.95%）不满足 g≥30%，V=26.00 满足。"""
    d = v2m.score_vix(make(v=25.99, v5=20.00))
    assert "2分：V≥20且g≥30%" not in d.triggered_conditions
    assert d.triggered_conditions == ("2分：V≥25",)
    d = v2m.score_vix(make(v=26.00, v5=20.00))
    assert "2分：V≥20且g≥30%" in d.triggered_conditions
    assert "g=V÷V5−1=30%" in d.calculation


def test_pct_change_is_not_rounded():
    """g 在 29.995% 与 30% 之间时不得视为满足 g≥30%（先取整会误判）。"""
    from decimal import Decimal

    from market_risk.scoring.common import pct_change

    g = pct_change(Decimal("200.00"), Decimal("153.85"))  # 29.9967…%
    assert Decimal("29.995") < g < Decimal("30")


def test_vix_v5_missing_enumeration():
    assert v2m.score_vix(make(v=25.5, v5=None)).score == 2       # V≥25 无需 V5
    d = v2m.score_vix(make(v=19.0, v5=None))
    assert d.score == 1 and "不改变结果" in d.calculation          # 18≤V<20 无需 V5
    d = v2m.score_vix(make(v=17.0, v5=None))
    assert d.score is None and d.possible_scores == (0, 1)
    d = v2m.score_vix(make(v=22.0, v5=None))
    assert d.score is None and d.possible_scores == (1, 2)
    d = v2m.score_vix(make(v=None, v5=None))
    assert d.score is None and d.possible_scores == (0, 1, 2)


def test_vix_v_missing_v5_known():
    d = v2m.score_vix(make(v=None, v5=15.0))
    assert d.score is None and d.possible_scores == (0, 1, 2)


# ---------------------------------------------------------------------------
# 四、利率
# ---------------------------------------------------------------------------


def test_rates_y_below_h_is_0():
    d = v2m.score_rates(make(y=4.02, h_other=4.17, y20=4.11))
    assert d.score == 0 and "y<H" in d.calculation


def test_rates_y_equals_h_boundaries():
    """y 恰好等于 H（与更早某天并列）；Δy 恰好 25 为2分，24 为1分。"""
    assert v2m.score_rates(make(y=4.30, h_other=4.30, y20=4.05)).score == 2
    assert v2m.score_rates(make(y=4.30, h_other=4.30, y20=4.06)).score == 1
    assert v2m.score_rates(make(y=4.31, h_other=4.30, y20=4.06)).score == 2


def test_rates_t20_missing():
    """SPEC 5.6 第5条：T−20 缺失时，y<H 直接0分；y=H 记待补 {1,2}。"""
    d = v2m.score_rates(make(y=4.02, h_other=4.17, y20=None))
    assert d.score == 0 and d.pending_reason is None
    d = v2m.score_rates(make(y=4.20, h_other=4.17, y20=None))
    assert d.score is None and d.possible_scores == (1, 2)


def test_rates_base_date_missing():
    """SPEC 5.6 第5条：基准日债市休市、y 缺失，记待补 {0,1,2}。"""
    d = v2m.score_rates(make(y=None, h_other=4.17, y20=4.11))
    assert d.score is None and d.possible_scores == (0, 1, 2)


# ---------------------------------------------------------------------------
# 五、信用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("o1", "o6", "expected"),
    [
        (2.76, 2.71, 0),   # ΔOAS 恰好 +5：0分（样本2）
        (2.77, 2.71, 1),   # +6
        (2.91, 2.71, 2),   # ΔOAS 恰好 +20：2分
        (2.90, 2.71, 1),   # +19
        (4.00, 4.10, 2),   # O1 恰好 4.00%：2分
        (3.99, 4.10, 0),
    ],
)
def test_credit_boundaries(o1, o6, expected):
    assert v2m.score_credit(make(o1=o1, o6=o6)).score == expected


def test_credit_lag_cap():
    """O1 滞后超过1个股票交易日，最高1分，并写明封顶原因。"""
    d = v2m.score_credit(make(o1=3.18, o6=2.80, lag=2))
    assert d.score == 1 and "滞后封顶1分" in d.triggered_conditions
    assert "滞后基准日 2 个股票交易日" in d.calculation
    d = v2m.score_credit(make(o1=2.80, o6=2.80, lag=2))
    assert d.score == 0 and "本次不影响结果" in d.calculation


def test_credit_o6_missing():
    d = v2m.score_credit(make(o1=4.05, o6=None))
    assert d.score == 2 and "不改变结果" in d.calculation   # O1≥4.00 无需 O6
    d = v2m.score_credit(make(o1=3.00, o6=None))
    assert d.score is None and d.possible_scores == (0, 1, 2)
    d = v2m.score_credit(make(o1=3.00, o6=None, lag=2))
    assert d.score is None and d.possible_scores == (0, 1)


def test_credit_2025_10_14_capped_real_data():
    """基准日 2025-10-14（真实数据）：v2-M 的 O1=10-10，滞后2个交易日，ΔOAS=+38bp 封顶为1分。"""
    raw = dataclasses.replace(load_sample_raw("2025-10-31"), base_date=D(2025, 10, 14),
                              oas_vintage=None)
    snap = build_snapshot(raw)
    d = v2m.score_credit(snap)
    assert (snap.refs.oas_o1_v2m, snap.refs.o1_v2m_lag_stock_days) == (D(2025, 10, 10), 2)
    assert "ΔOAS=100×(O1−O6)=38bp" in d.calculation
    assert d.score == 1 and "滞后封顶1分" in d.triggered_conditions


# ---------------------------------------------------------------------------
# 历史修订比对
# ---------------------------------------------------------------------------


def _vintage(o1_old, o6_old, o1=3.00, o6=3.17):
    return (
        OasVintageValue("v2-M O1", D(2025, 11, 26), o1_old, o1),
        OasVintageValue("v2-M O6", D(2025, 11, 19), o6_old, o6),
    )


def test_revision_that_changes_score_is_flagged():
    snap = make(o1=3.00, o6=3.17, vintage=_vintage(3.00, 2.70))  # 旧版本 ΔOAS=+30 → 2分
    result = v2m.score(snap)
    assert result.credit.score == 0  # 仍按当前版本计分
    assert len(result.review_flags) == 1
    assert "【需人工判断】" in result.review_flags[0] and "2 分" in result.review_flags[0]


def test_revision_without_score_change_not_flagged():
    snap = make(o1=3.00, o6=3.17, vintage=_vintage(3.01, 3.16))
    assert v2m.score(snap).review_flags == ()


def test_vintage_missing_point_is_not_a_revision():
    snap = make(o1=3.00, o6=3.17, vintage=_vintage(3.00, None))
    assert v2m.score(snap).review_flags == ()


# ---------------------------------------------------------------------------
# 总分、阶段、证据链
# ---------------------------------------------------------------------------


def test_stage_boundaries():
    assert [stage_of(t) for t in (0, 2, 3, 5, 6, 10)] == [
        "早期信号", "早期信号", "中期确认信号", "中期确认信号", "高风险", "高风险"
    ]


def test_clear_deterioration_yes():
    etfs = {s: etf(s, 90, 95, 99, 98, 80) for s in ("SPY", "QQQ", "RSP")}
    snap = make(etfs=etfs, f=30.0, w=35.0, f5=40.0, w5=45.0, v=26.0, v5=20.0)
    r = v2m.score(snap)
    assert (r.price.score, r.breadth.score, r.vix.score) == (2, 2, 2)
    assert dict(r.clear_deterioration)["大盘明确恶化"] == "是"
    assert r.total == 6 and r.stage == "高风险"


def test_clear_deterioration_unverifiable_with_pending():
    etfs = {s: etf(s, 90, 95, 99, 98, 80) for s in ("SPY", "QQQ", "RSP")}
    snap = make(etfs=etfs, f=30.0, w=35.0, f5=40.0, w5=45.0, v=16.0, v5=15.0,
                o1=3.00, o6=None)
    r = v2m.score(snap)
    chain = dict(r.clear_deterioration)
    assert chain["信用=2"] == "无法核验" and chain["VIX=2"] == "否"
    assert chain["大盘明确恶化"] == "无法核验"
    assert r.total is None and r.total_range == (4, 6) and r.stage is None
    assert r.pending == ("信用",)


def test_pending_range_within_one_stage_keeps_stage():
    r = v2m.score(make(y=4.20, h_other=4.17, y20=None))   # 利率 {1,2}
    assert r.total is None and r.total_range == (1, 2) and r.stage == "早期信号"


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------

SAMPLE_BREADTH = {
    "2025-08-29": [(D(2025, 8, 29), 63.22, 67.59)],
    "2025-09-26": [(D(2025, 9, 26), 54.67, 51.09)],
    "2025-10-31": [(D(2025, 10, 31), 40.15, 38.56), (D(2025, 10, 24), 52.88, 57.65)],
    "2025-11-28": [(D(2025, 11, 28), 58.44, 76.73)],
}
SAMPLE_EXPECTED = {
    "2025-08-29": (0, 0, 0, 0, 0),
    "2025-09-26": (0, 0, 0, 0, 0),
    "2025-10-31": (1, 2, 0, 0, 0),
    "2025-11-28": (0, 0, 0, 0, 0),
}


def sample_snapshot(sample: str):
    breadth = {d: BreadthReading(d, f, w) for d, f, w in SAMPLE_BREADTH[sample]}
    return build_snapshot(load_sample_raw(sample, breadth))


@pytest.mark.parametrize("sample", list(SAMPLE_EXPECTED))
def test_samples(sample):
    r = v2m.score(sample_snapshot(sample))
    assert tuple(d.score for d in r.dimensions) == SAMPLE_EXPECTED[sample]
    assert r.total == sum(SAMPLE_EXPECTED[sample])
    assert dict(r.clear_deterioration)["大盘明确恶化"] == "否"
    assert r.review_flags == ()


def test_2025_09_02_credit_and_near_threshold():
    """用户确认：两个版本信用均为0分；v2-M 的 ΔOAS=+4bp 贴近 +5bp 门槛。"""
    raw = dataclasses.replace(load_sample_raw("2025-09-26"), base_date=D(2025, 9, 2),
                              oas_vintage=None)
    snap = build_snapshot(raw)
    d = v2m.score_credit(snap)
    assert d.score == 0 and "ΔOAS=100×(O1−O6)=4bp" in d.calculation
    items = near_threshold_items(snap, load_settings().near_threshold)
    oas_items = [i for i in items if i.item.startswith("ΔOAS")]
    assert [(i.item, i.value, i.threshold, i.gap) for i in oas_items] == [
        ("ΔOAS（v2-M）", 4.0, 5.0, -1.0)
    ]
