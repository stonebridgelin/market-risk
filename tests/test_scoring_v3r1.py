"""规则B（v3-R1）评分测试：与 v2-M 不同的四处修改（SOP 7.4）、边界、待补（SPEC 9节阶段3验收）。"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import load_sample_raw
from scoring_helpers import DEFAULT_ETFS, completed_three_segment, etf, make, no_three_segment
from test_scoring_v2m import SAMPLE_EXPECTED, sample_snapshot

from market_risk.data.snapshot import build_snapshot
from market_risk.models import OasVintageValue
from market_risk.scoring import v2m, v3r1

D = dt.date


# ---------------------------------------------------------------------------
# 一、价格（修改1：MA50 持续性过滤；修改2：三环节不计分）
# ---------------------------------------------------------------------------


def test_price_2a_requires_ma5_below_ma50():
    both = {**DEFAULT_ETFS, "SPY": etf("SPY", 97, 97.5, 99, 98, 90),
            "QQQ": etf("QQQ", 97, 97.99, 99, 98, 90)}
    p = v3r1.score_price(make(etfs=both))
    assert p.score == 2 and p.triggered_conditions == ("2分(a)",)
    # 收盘价低于 MA50，但 MA5 不低于 MA50：v3-R1 不给2分，v2-M 给2分
    only_close = {**DEFAULT_ETFS, "SPY": etf("SPY", 97, 99, 99, 98, 90),
                  "QQQ": etf("QQQ", 97, 98.00, 99, 98, 90)}   # QQQ 的 MA5 恰好等于 MA50
    assert v3r1.score_price(make(etfs=only_close)).score == 1
    assert v2m.score_price(make(etfs=only_close), True).score == 2


def test_price_2b_spy_below_ma200():
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 100, 100, 99, 98, 100.01)}
    assert v3r1.score_price(make(etfs=etfs)).triggered_conditions == ("2分(b)",)


def test_price_three_segment_not_scored():
    ts = no_three_segment()
    ts = {True: (completed_three_segment("SPY"), *ts[True][1:]), False: ts[False]}
    assert v3r1.score_price(make(three_segment=ts)).score == 0
    assert v2m.score_price(make(three_segment=ts), True).score == 2


def test_price_0_and_1():
    assert v3r1.score_price(make()).score == 0
    two_below20 = {**DEFAULT_ETFS, "SPY": etf("SPY", 98.5, 100, 99, 98, 90),
                   "QQQ": etf("QQQ", 98.5, 100, 99, 98, 90)}
    assert v3r1.score_price(make(etfs=two_below20)).score == 1
    one_below50 = {**DEFAULT_ETFS, "SPY": etf("SPY", 97, 97, 99, 98, 90)}
    assert v3r1.score_price(make(etfs=one_below50)).score == 1
    # 收盘价恰好等于 MA50：不算低于
    eq = {**DEFAULT_ETFS, "SPY": etf("SPY", 98, 97, 97, 98, 90)}
    assert v3r1.score_price(make(etfs=eq)).score == 0


# ---------------------------------------------------------------------------
# 二、广度（修改3：第二个2分条件改为 F<40 且较5日前下降）
# ---------------------------------------------------------------------------


def test_breadth_2a_and_2b():
    assert v3r1.score_breadth(make(f=39, w=45, f5=41, w5=46)).triggered_conditions == (
        "2分(a)", "2分(b)"
    )
    b = v3r1.score_breadth(make(f=39.99, w=60, f5=40.00, w5=50))
    assert b.score == 2 and b.triggered_conditions == ("2分(b)",)


def test_breadth_2b_boundaries():
    """F 恰好等于 F5：不满足 F<F5；F 恰好为40：不满足 F<40。"""
    assert v3r1.score_breadth(make(f=39.00, w=60, f5=39.00, w5=50)).score == 1
    assert v3r1.score_breadth(make(f=40.00, w=60, f5=45.00, w5=50)).score == 1


def test_breadth_no_near_high_alarm():
    """v2-M 的"指数高位、广度背离"条件在 v3-R1 中不存在。"""
    etfs = {**DEFAULT_ETFS, "SPY": etf("SPY", 99.0, 100, 97, 97, 90)}
    snap = make(etfs=etfs, spy_window_max=100.0, f=38.0, w=45.0, f5=37.0, w5=44.0)
    assert v2m.score_breadth(snap).score == 2
    assert v3r1.score_breadth(snap).score == 1


def test_breadth_l_50_and_pending():
    assert v3r1.score_breadth(make(f=50.00, w=55)).score == 0
    d = v3r1.score_breadth(make(f=38.0, w=45.0))
    assert d.score is None and d.possible_scores == (1, 2)
    assert v3r1.score_breadth(make(f=45.0, w=42.0)).score == 1


# ---------------------------------------------------------------------------
# 三、VIX；四、利率：与规则A相同
# ---------------------------------------------------------------------------


def test_vix_and_rates_same_as_v2m():
    assert v3r1.score_vix is v2m.score_vix
    assert v3r1.score_rates is v2m.score_rates


# ---------------------------------------------------------------------------
# 五、信用（修改4：滞后改为待补；按债市营业日计数）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("o1", "o6", "expected"),
    [(2.76, 2.71, 0), (2.77, 2.71, 1), (2.91, 2.71, 2), (2.90, 2.71, 1),
     (4.00, 4.10, 2), (3.99, 4.10, 0)],
)
def test_credit_boundaries(o1, o6, expected):
    assert v3r1.score_credit(make(o1=o1, o6=o6)).score == expected


def test_credit_o1_missing_is_data_lag():
    d = v3r1.score_credit(make(o1=None, o6=2.80))
    assert d.score is None and d.pending_reason == "待补：数据滞后"


def test_credit_o6_missing():
    assert v3r1.score_credit(make(o1=4.00, o6=None)).score == 2   # 此时无需 O6
    d = v3r1.score_credit(make(o1=3.00, o6=None))
    assert d.score is None and d.possible_scores == (0, 1, 2)


def test_credit_not_capped_by_lag():
    """v3-R1 没有滞后封顶：2025-10-14 真实数据信用为2分（v2-M 封顶为1分）。"""
    raw = dataclasses.replace(load_sample_raw("2025-10-31"), base_date=D(2025, 10, 14),
                              oas_vintage=None)
    snap = build_snapshot(raw)
    assert v3r1.score_credit(snap).score == 2
    assert v2m.score_credit(snap).score == 1


def test_revision_flag_v3r1():
    vintage = (
        OasVintageValue("v3-R1 O1", D(2025, 11, 26), 3.00, 3.00),
        OasVintageValue("v3-R1 O6", D(2025, 11, 19), 2.95, 3.17),   # 旧版本 ΔOAS=+5 → 0分
    )
    r = v3r1.score(make(o1=3.00, o6=3.17, vintage=vintage))
    assert r.review_flags == ()   # 0分 → 0分，不需判断
    vintage = (
        OasVintageValue("v3-R1 O1", D(2025, 11, 26), 3.20, 3.00),   # 旧版本 ΔOAS=+3？
        OasVintageValue("v3-R1 O6", D(2025, 11, 19), 3.00, 3.17),   # 3.20−3.00=+20 → 2分
    )
    r = v3r1.score(make(o1=3.00, o6=3.17, vintage=vintage))
    assert r.credit.score == 0 and len(r.review_flags) == 1


# ---------------------------------------------------------------------------
# 样本与 2025-09-02
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sample", list(SAMPLE_EXPECTED))
def test_samples(sample):
    r = v3r1.score(sample_snapshot(sample))
    assert tuple(d.score for d in r.dimensions) == SAMPLE_EXPECTED[sample]
    assert r.stage == ("中期确认信号" if sample == "2025-10-31" else "早期信号")
    assert dict(r.clear_deterioration)["大盘明确恶化"] == "否"


def test_2025_09_02_credit():
    raw = dataclasses.replace(load_sample_raw("2025-09-26"), base_date=D(2025, 9, 2),
                              oas_vintage=None)
    d = v3r1.score_credit(build_snapshot(raw))
    assert d.score == 0 and "ΔOAS=100×(O1−O6)=-6bp" in d.calculation


# ---------------------------------------------------------------------------
# OAS 长历史中缺失的两个股市交易日（2013-08-30、2015-01-16，长周末前债市提前收盘）
# ---------------------------------------------------------------------------


def _gap_snapshot(base, treasury_holidays, oas_overrides):
    from conftest import synthetic_raw

    raw = synthetic_raw(base, treasury_overrides=dict.fromkeys(treasury_holidays),
                        oas_overrides=oas_overrides)
    return build_snapshot(raw)


def test_oas_gap_2013_08_30_as_o1():
    """基准日 2013-09-03：v3-R1 的 O1 为 2013-08-30（债市营业日），OAS 缺失 → 待补：数据滞后。"""
    snap = _gap_snapshot(D(2013, 9, 3), [D(2013, 9, 2)], {D(2013, 8, 30): None})
    assert snap.refs.oas_o1 == D(2013, 8, 30)
    d = v3r1.score_credit(snap)
    assert d.score is None and d.pending_reason == "待补：数据滞后"
    # v2-M：O1 取最新有数值观测 08-29，滞后 2 个股票交易日 → 最高1分
    assert snap.refs.oas_o1_v2m == D(2013, 8, 29) and snap.refs.o1_v2m_lag_stock_days == 2
    assert max(v2m.score_credit(snap).possible_scores) <= 1


def test_oas_gap_2015_01_16_as_o6():
    """基准日 2015-01-27：v3-R1 的 O6 为 2015-01-16，OAS 缺失。
    O1<4.00% 时记待补；O1≥4.00% 时按 SOP 7.3"此时无需 O6"给2分（2015-01-26 实际 O1=5.26%）。"""
    low = _gap_snapshot(D(2015, 1, 27), [D(2015, 1, 19)], {D(2015, 1, 16): None, D(2015, 1, 26): 3.50})
    assert (low.refs.oas_o1, low.refs.oas_o6_v3r1) == (D(2015, 1, 26), D(2015, 1, 16))
    d = v3r1.score_credit(low)
    assert d.score is None and d.possible_scores == (0, 1, 2)
    high = _gap_snapshot(D(2015, 1, 27), [D(2015, 1, 19)], {D(2015, 1, 16): None, D(2015, 1, 26): 5.26})
    assert v3r1.score_credit(high).score == 2
    # v2-M 只数有数值的观测：O6 顺延到 01-15，信用可确定
    assert low.refs.oas_o6_v2m == D(2015, 1, 15) and v2m.score_credit(low).score is not None
