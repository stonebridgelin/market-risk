"""v1.2.1 通道状态与同日优先级的构造测试。"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

from market_risk.wavewarn.channels import (
    ChannelPredicate,
    breadth_predicates,
    cmap_predicate,
    price_predicates,
    run_channel,
    update_channel,
    volatility_predicates,
)
from market_risk.wavewarn.features import AssetFeatures


def _feature(day: dt.date) -> AssetFeatures:
    return AssetFeatures(day, Decimal("95"), Decimal("100"), Decimal("0.05"), 1, 0,
                         Decimal("25"), Decimal("-20"), Decimal("-12"), Decimal("-10"),
                         Decimal("50"), Decimal("100"), Decimal("100"), Decimal("90"), Decimal("95"))


def test_price_new_low_rearms_p_and_pr_and_enters_red_same_day() -> None:
    day = dt.date(2010, 1, 4)
    feature = _feature(day)
    p = price_predicates((feature,), Decimal("0.02"), 5)[0]
    pr = price_predicates((feature,), Decimal("0.02"), 5, red=True)[0]
    # D=5% 同时大于黄门槛2%和红门槛4%；NL=1使两个未武装通道同日可重新进入。
    assert update_channel(day, "unarmed", p).status == "active"
    assert update_channel(day, "unarmed", pr).status == "active"


def test_price_unknown_new_low_rearms_without_missing_drawdown_entry() -> None:
    day = dt.date(2010, 1, 4)
    feature = replace(_feature(day), drawdown63=None, new_low20=None)
    predicate = price_predicates((feature,), Decimal("0.02"), 5)[0]
    result = update_channel(day, "unarmed", predicate)
    # NL未知可重新武装，但D缺失不能同日进入，通道对静默计数仍无效。
    assert (result.status, result.valid) == ("armed", False)


def test_exit_priority_prevents_same_day_reentry() -> None:
    day = dt.date(2010, 1, 4)
    predicate = ChannelPredicate(entry=True, exit=True, rearm=True, immediate_rearm=True)
    assert update_channel(day, "active", predicate).status == "unarmed"


def test_active_channel_uses_exit_inputs_even_if_entry_input_missing() -> None:
    day = dt.date(2010, 1, 4)
    # 负责人确认：只检查当前状态相关操作；激活态退出输入有效即可退出。
    result = update_channel(day, "active", ChannelPredicate(entry=None, exit=True, rearm=None))
    assert (result.status, result.valid) == ("unarmed", True)


def test_breadth_missing_required_input_is_invalid_even_when_other_clause_false() -> None:
    day = dt.date(2010, 1, 4)
    feature = replace(_feature(day), drawdown63=Decimal("0.005"), delta_w20=None)
    predicate = breadth_predicates((feature,), "B")[0]
    # D低于1%已可判不进入，但规格要求任一所需输入缺失则通道无效并沿用状态。
    assert predicate.entry is None
    assert update_channel(day, "armed", predicate).valid is False


def test_breadth_and_vix_exit_require_consecutive_valid_days() -> None:
    days = tuple(dt.date(2010, 1, 4) + dt.timedelta(days=i) for i in range(5))
    features = tuple(replace(_feature(day), breadth=Decimal("60")) for day in days[:3])
    rows = run_channel(days[:3], breadth_predicates(features, "B"), initial="active")
    # W=60连续3日高于中位50，前两日保持激活，第3日退出。
    assert [row.status for row in rows] == ["active", "active", "unarmed"]
    ratios = (Decimal("1.1"), Decimal("0.94"), None, Decimal("0.94"), Decimal("0.94"))
    vix = run_channel(days, volatility_predicates(ratios), initial="active")
    # 缺值打断<0.95的连续序列；最后只有两日有效，仍未满足3日退出。
    assert vix[-1].status == "active"
    assert vix[2].valid is False


def test_cmap_yellow_red_boundaries_and_unknown_deterioration() -> None:
    # CY 在分数下限3进入、上限2退出；CR 在下限6进入，但恶化无法核验时不能退出。
    yellow_entry = cmap_predicate("CY", 3, 5, "否")
    yellow_exit = cmap_predicate("CY", 0, 2, "否")
    red_entry = cmap_predicate("CR", 6, 8, "否")
    red_unknown = cmap_predicate("CR", 2, 5, "无法核验")
    assert (yellow_entry.entry, yellow_entry.exit) == (True, False)
    assert (yellow_exit.entry, yellow_exit.exit) == (False, True)
    assert (red_entry.entry, red_entry.exit) == (True, False)
    assert (red_unknown.entry, red_unknown.exit) == (False, None)
