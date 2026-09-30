"""v1.2.1 特征构造测试：每个数值均由注释中的公式手推。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.features import (
    anchor_126_audit,
    asset_features,
    breadth_median60,
    breadth_quantile_reference,
    new_low20,
    q_from_new_lows,
    vix_term_ratio,
)


def test_new_low_three_valued_missing_counterexample() -> None:
    prior = [Decimal("100")] * 19
    prior[5] = Decimal("98")
    missing = list(prior)
    missing[7] = None
    # 真实缺价可能为97；今日97.5虽低于有效最小98，却无法证明严格创新低。
    assert new_low20([*missing, Decimal("97.5")], 19) is None
    # 今日98.5高于已知最小98，即使缺1日也能确定未创新低。
    assert new_low20([*missing, Decimal("98.5")], 19) == 0
    # 前19日完整且最小98，今日97.5严格更低，才判1。
    assert new_low20([*prior, Decimal("97.5")], 19) == 1
    sparse = [Decimal("98")] * 14 + [None] * 5
    # 有效价不足15个时，即使今日98.5高于有效最小98，也优先判未知。
    assert new_low20([*sparse, Decimal("98.5")], 19) is None
    assert q_from_new_lows((0, 0, 1, 0, None, 0)) == (1, 2, 0, 1, 0, 1)


def test_breadth_quantile_median_and_vix_ratio_by_hand() -> None:
    values = [Decimal(i * i) for i in range(273)]
    # t=272：参考 t−252..t−1 = 20..271，共252个有效ΔW20，按下标单调递增。
    # q=0.10，第ceil(25.2)=26小，落在过去日45：45²−25²=1400。
    assert breadth_quantile_reference(values, 272, Decimal("0.10")) == Decimal("1400")
    sixty = [Decimal(i) for i in range(1, 61)]
    assert breadth_median60(sixty, 59) == Decimal("30.5")  # (30+31)/2。
    assert vix_term_ratio(Decimal("24"), Decimal("20")) == Decimal("1.2")


def test_future_extreme_values_do_not_change_asset_features() -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=i) for i in range(280))
    prices = {day: Decimal("100") for day in days}
    breadth = {day: Decimal(index) for index, day in enumerate(days)}
    earlier = asset_features(days, prices, breadth, Decimal("0.10"))[260]
    altered_prices = dict(prices)
    altered_breadth = dict(breadth)
    for day in days[261:]:
        altered_prices[day] = Decimal("999999")
        altered_breadth[day] = Decimal("-999999")
    changed = asset_features(days, altered_prices, altered_breadth, Decimal("0.10"))[260]
    # 只改第261日以后值，已算出的第260日锚点、均线、广度变化和分位数须逐项相同。
    assert changed == earlier


def test_anchor_126_audit_only_reports_old_peak_leaving_63_day_window() -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(126))
    prices = {day: Decimal(90) for day in days}
    prices[days[0]] = Decimal(100)
    audit = anchor_126_audit(days, prices, Decimal("0.05"))
    # 第125天的63日最高=90，D63=0；126日最高仍为首日100，D126=10%。
    assert audit[-1] == (days[125], Decimal(0), Decimal("0.1"))


def test_configured_fixed_windows_match_hand_computed_feature_values() -> None:
    fixed = load_wavewarn_config(Path(__file__).resolve().parents[1] / "config/wavewarn_v121.yaml").fixed_parameters()
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(270))
    prices = {day: Decimal(100) for day in days}
    breadth = {day: Decimal(60) for day in days}
    result = asset_features(days, prices, breadth, Decimal("0.10"), fixed)[-1]
    # 270日常数序列已超过63/200/252日回看：H63=100，D=0，MA50=MA200=100，中位数=60。
    assert (result.high63, result.drawdown63, result.ma50, result.ma200, result.median60) == (
        Decimal(100), Decimal(0), Decimal(100), Decimal(100), Decimal(60))
