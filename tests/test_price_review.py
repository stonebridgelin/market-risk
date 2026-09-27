"""固定残差法：构造数据与边界验证，不依赖行情或网络，不写真实目录。"""

import dataclasses
import datetime as dt
from decimal import Decimal as D

import pandas as pd
import pytest

from market_risk import calendar as cal
from market_risk import services
from market_risk.data.cache import DataFetchError
from market_risk.data.price_review import (
    ResidualStats,
    classify,
    load_review_config,
    residual_stats,
    review_case,
)
from market_risk.data.price_review_inputs import ReviewInputs, dividend_series_from_frame
from market_risk.storage.paths import StoragePaths

CFG = load_review_config()
DAY = dt.date(2020, 7, 15)


def stat(e0: str, e1: str) -> ResidualStats:
    return ResidualStats(D(e0), D(e1), D('0.0001'), 39, ())


@pytest.mark.parametrize(('z', 'other', 'expected'), [
    ('6', '0', ('无法判定', '不适用')),
    ('6.000001', '3', ('Yahoo 有误', '中')),
    ('10', '2', ('Yahoo 有误', '中')),
    ('10.000001', '2', ('Yahoo 有误', '高')),
    ('11', '2.000001', ('Yahoo 有误', '中')),
    ('11', '3.000001', ('无法判定', '不适用')),
])
def test_strict_and_inclusive_z_boundaries(z, other, expected):
    suspect = stat(str(D(z) / 10000), str(-D(z) / 10000))
    normal = stat(str(D(other) / 10000), str(D(other) / 10000))
    assert classify(normal, suspect, CFG)[:2] == expected


@pytest.mark.parametrize(('ratio', 'abnormal'), [('0.499999', False), ('0.5', True),
                                               ('1.5', True), ('1.500001', False)])
def test_reversal_boundaries(ratio, abnormal):
    assert stat('0.001', str(-D('0.001') * D(ratio))).abnormal(CFG) is abnormal


def test_both_abnormal_and_same_sign_cannot_decide():
    abnormal = stat('0.002', '-0.002')
    assert classify(abnormal, abnormal, CFG)[0] == '无法判定'
    assert classify(stat('0.002', '0.002'), stat('0', '0'), CFG)[0] == '无法判定'
    assert classify(abnormal, stat('0', '0'), CFG)[:2] == ('TradingView 有误', '高')


def flat_series():
    days = cal.stock_trading_days(cal.shift_trading_days(DAY, -21), cal.shift_trading_days(DAY, 20))
    return {day: D('100') for day in days}


def test_constructed_one_day_bad_close_recovers_next_session():
    index = flat_series()
    bad = {**index, DAY: D('99')}
    result = review_case('SPY', DAY, index, bad, index, index, frozenset(), CFG)
    assert (result.verdict, result.confidence) == ('Yahoo 有误', '高')
    assert result.yahoo.e0 == D('-0.01')
    assert result.yahoo.e1 == D(100) / D(99) - 1
    assert result.yahoo.sigma == D('0.0001')
    assert result.yahoo.sample_count == 39


def test_mad_center_dividend_exclusion_and_window():
    cfg = dataclasses.replace(CFG, window_sessions=2)
    days = cal.stock_trading_days(cal.shift_trading_days(DAY, -3), cal.shift_trading_days(DAY, 2))
    index = {d: D('100') for d in days}
    # 六个收盘价产生五个残差：0.001、0.003、争议日、次日、除息日。
    prices = {days[0]: D('100')}
    for day, change in zip(days[1:], [D('.001'), D('.003'), D('.1'), D('-.1'), D('-.02')], strict=True):
        prices[day] = prices[max(prices)] * (1 + change)
    stats = residual_stats(DAY, prices, index, frozenset({days[-1]}), cfg)
    assert stats.sample_count == 2
    assert stats.sigma == D('0.0014826')  # 中位数.002，两项绝对偏差均.001
    assert stats.excluded_dividends == (days[-1],)


@pytest.mark.parametrize('which', [0, 1])
@pytest.mark.parametrize(('diff', 'expected'), [('0.02', 'Yahoo 有误'), ('0.020001', '无法判定')])
def test_index_tolerance_veto_on_either_date(which, diff, expected):
    index = flat_series()
    t = [DAY, cal.shift_trading_days(DAY, 1)][which]
    other_index = {**index, t: index[t] + D(diff)}
    result = review_case('SPY', DAY, index, {**index, DAY: D(99)}, index, other_index, frozenset(), CFG)
    assert result.verdict == expected


def test_rsp_uses_spx_and_incomplete_window_does_not_shrink():
    index = flat_series()
    result = review_case('RSP', DAY, index, {**index, DAY: D(99)}, index, index, frozenset(), CFG)
    assert result.benchmark == 'SPX' and result.verdict == 'Yahoo 有误'
    del index[min(index)]
    result = review_case('RSP', DAY, index, index, index, index, frozenset(), CFG)
    assert result.verdict == '无法判定' and '缺失' in result.reason


def test_audit_ignores_data_after_its_explicit_window():
    series = flat_series()
    before = residual_stats(DAY, series, series, frozenset(), CFG)
    extra = {**series, cal.shift_trading_days(DAY, 21): D('99999999')}
    assert residual_stats(DAY, extra, series, frozenset(), CFG) == before


def test_dividends_require_explicit_complete_column():
    frame = pd.DataFrame({'Dividends': [0, 1.2]}, index=pd.to_datetime(['2020-01-02', '2020-01-03']))
    assert dividend_series_from_frame(frame) == {dt.date(2020, 1, 2): 0.0, dt.date(2020, 1, 3): 1.2}
    with pytest.raises(DataFetchError):
        dividend_series_from_frame(pd.DataFrame())


def test_service_renders_evidence_and_reuses_verified_third_party(tmp_path):
    cfg = dataclasses.replace(CFG, cases=(('SPY', DAY),))
    series = flat_series()
    inputs = ReviewInputs({'SPY': series, 'SPX': series}, {'SPY': {**series, DAY: D(99)}},
                         {'SPX': series}, {'SPY': frozenset()}, {}, ('测试输入',))
    from market_risk.config import load_settings, load_symbols

    def loader(symbol, start, end):
        info = next(s for s in load_symbols().values() if s.symbol == symbol)
        return {**info.known_values, DAY: 100.0, cal.shift_trading_days(DAY, 1): 100.0}

    ctx = services.Context(load_settings(), StoragePaths(tmp_path), '')
    report = services.review_price_disputes(ctx, config=cfg, inputs=inputs, third_party=(loader, '测试第三方'),
                                            run_impact=False)
    assert report.results[0].verdict == 'Yahoo 有误'
    assert 'e(d) bp' in report.text and '无法判定的原因（由计算结果生成）' in report.text
    assert '已批准的价格修正' in report.text
    assert '多数一致：Yahoo 有误，建议值 100.00' in report.text and '已用已知读数验证' in report.text
    assert '隐含价格（ETF 取 TradingView）：100.00' in report.text
    assert report.majority[('SPY', DAY)].suggested == D(100)
    assert '## 处理结论' in report.text and '满足建议修正条件' in report.text
    assert report.path.read_text(encoding='utf-8') == report.text


@pytest.mark.parametrize('offset', [0, 1])
def test_dispute_or_next_day_ex_dividend_cannot_decide(offset):
    """争议日或次日是除息日：分红会混入残差，判"无法判定"。"""
    index = flat_series()
    ex_div = cal.shift_trading_days(DAY, offset)
    result = review_case('SPY', DAY, index, {**index, DAY: D(99)}, index, index, frozenset({ex_div}), CFG)
    assert result.verdict == '无法判定' and '除息日' in result.reason and str(ex_div) in result.reason


def test_explain_is_generated_from_results():
    """说明文字由计算结果生成：反向回归但未过门槛、同号、两方都正常、相邻争议日。"""
    from market_risk.data.price_review import ReviewResult, explain

    def result(tv, yahoo):
        return ReviewResult('SPY', DAY, cal.shift_trading_days(DAY, 1), 'SPX', tv, yahoo, (D(0), D(0)),
                            '无法判定', '不适用', '')

    below = explain(result(stat('0', '0'), stat('0.00057', '-0.00057')), CFG)
    assert 'Yahoo 的 z(d)=5.70' in below and '次日反向回归' in below and '未超过异常门槛 6' in below
    same = explain(result(stat('0', '0'), stat('0.0004', '0.0004')), CFG)
    assert '两日残差同号' in same
    calm = explain(result(stat('0.0001', '0'), stat('0.0002', '0')), CFG, (cal.shift_trading_days(DAY, 1),))
    assert '残差法看不出哪一方异常' in calm and '同为争议日' in calm


def pair(a, b):
    return (D(a), D(b))


@pytest.mark.parametrize(('tv', 'yahoo', 'third', 'verdict', 'suggested'), [
    # 口径1：TradingView 与第三方两日都相差≤0.01，争议日 Yahoo 与两者都相差>0.02
    (pair('43.31', '42.45'), pair('43.37', '42.45'), pair('43.31', '42.45'), '多数一致：Yahoo 有误', D('43.31')),
    (pair('43.31', '42.45'), pair('43.37', '42.45'), pair('43.30', '42.46'), '多数一致：Yahoo 有误', None),
    (pair('43.31', '42.45'), pair('43.33', '42.45'), pair('43.31', '42.45'), '无法判定', None),      # 只差0.02
    # 口径2：第三方与 Yahoo 两日都相差≤0.01
    (pair('94.15', '94.13'), pair('93.11', '94.13'), pair('93.11', '94.13'), 'Yahoo 正确', None),
    # 口径3：其余组合（SPY 2015-08-21/24 的两种形态）
    (pair('197.63', '189.55'), pair('197.83', '189.50'), pair('197.63', '189.50'), '无法判定', None),
    (pair('189.55', '187.27'), pair('189.50', '187.27'), pair('189.50', '187.23'), '无法判定', None),
])
def test_majority_rule_three_clauses(tv, yahoo, third, verdict, suggested):
    from market_risk.data.price_review import majority_verdict

    m = majority_verdict(tv, yahoo, third, CFG)
    assert (m.verdict, m.suggested) == (verdict, suggested)


def test_majority_requires_all_values():
    from market_risk.data.price_review import majority_verdict

    assert '缺失' in majority_verdict((D(1), None), (D(1), D(1)), (D(1), D(1)), CFG).verdict


def test_implied_price_and_notes():
    from market_risk.data.price_review import implied_price

    prev, nxt = cal.shift_trading_days(DAY, -1), cal.shift_trading_days(DAY, 1)
    index = {prev: D('900'), DAY: D('940'), nxt: D('950')}
    etf = {prev: D('90'), DAY: D('93'), nxt: D('95.19')}
    ip = implied_price(DAY, etf, index, 'TradingView', frozenset({nxt}), frozenset({prev}))
    assert ip.ratio_prev == D('0.1') and ip.value == (D('0.1') + D('95.19') / D('950')) / 2 * D('940')
    assert any('除息日' in n for n in ip.notes) and any('争议日' in n for n in ip.notes)
    missing = implied_price(DAY, {DAY: D(1)}, index, 'Yahoo')
    assert missing.value is None and '无法推算' in missing.notes[-1]


def test_disposition_rules():
    from market_risk.data.price_review import MajorityResult, ReviewResult, disposition

    def result(verdict='无法判定', conf='不适用', yahoo=None):
        return ReviewResult('SPY', DAY, cal.shift_trading_days(DAY, 1), 'SPX', stat('0', '0'), yahoo,
                            (D(0), D(0)), verdict, conf, '')

    ok = MajorityResult('Tiingo', (), (), (), 'Yahoo 正确')
    assert disposition(result(), None, '无实质影响', '94.15', CFG).startswith('已修正为 94.15')
    assert disposition(result('Yahoo 有误', '中'), None, None, None, CFG).startswith('满足建议修正条件')
    assert disposition(result('TradingView 有误', '中'), None, None, None, CFG).startswith('保留 Yahoo 原值')
    shaped = disposition(result(yahoo=stat('0.00057', '-0.00057')), ok, '无实质影响', None, CFG)
    assert '多数一致规则判 Yahoo 正确' in shaped and '单日异常、次日反转形态（z(d)=5.70' in shaped
    assert disposition(result(), None, '无实质影响', None, CFG) == '保留 Yahoo 原值：无法判定，无实质影响'
