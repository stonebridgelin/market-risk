"""v2.0 净值（登记第六节第 2 小节、第八节第 4 小节；规格第五节“杠杆部分的模拟”）：只用构造数据。"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from wavewarn_v20_helpers import POSITIONS, TOLERANCE, numbered, random_closes, signal

from market_risk.wavewarn_v20.execution import PlannedTarget, Position, TargetSource, signal_targets
from market_risk.wavewarn_v20.nav import (
    NavError,
    NavUnavailable,
    basket_returns,
    hold_targets,
    portfolio_return,
    simulate_hold,
    simulate_targets,
)
from market_risk.wavewarn_v20.state_machine import Risk


def days(count: int) -> list:
    return [numbered(number) for number in range(1, count + 1)]


def closes(spx: list[str | None], qqq: list[str | None]) -> dict[str, list[Decimal | None]]:
    return {"SPX": [None if value is None else Decimal(value) for value in spx],
            "QQQ": [None if value is None else Decimal(value) for value in qqq]}


def target(number: int, core: float, leverage: float, position: Position = Position.NORMAL) -> PlannedTarget:
    return PlannedTarget(numbered(number), position, core, leverage, TargetSource.SYSTEM, False)


def test_daily_two_times_example_100_110_100() -> None:
    """登记第八节第 4 小节：指数 100 → 110 → 100，两日净收益为 0；

    每日 2 倍的杠杆部分为 1.2 × (1 − 0.181818) − 1 = −1.818%。
    """
    path = ["100", "110", "100"]
    basket = basket_returns(days(3), closes(path, path))
    assert basket == pytest.approx([0.10, -10 / 110])
    leveraged_only = [target(1, 0.0, 1.0), target(2, 0.0, 1.0), target(3, 0.0, 1.0)]     # 只看杠杆部分
    result = simulate_targets(leveraged_only, basket, POSITIONS, TOLERANCE)
    assert result.returns == pytest.approx([0.20, -20 / 110])
    assert result.wealth[-1] - 1 == pytest.approx(-0.0181818181818, abs=1e-12)
    assert result.wealth[-1] == pytest.approx(1.2 * (1 - 2 / 11))
    # 对照：“2 倍 × 跨期收益”会得到 0，与逐日模拟不同；不得用跨期近似。
    assert 2 * (100 / 100 - 1) == 0 != result.wealth[-1] - 1
    core_only = simulate_targets([target(1, 1.0, 0.0), target(2, 1.0, 0.0), target(3, 1.0, 0.0)], basket, POSITIONS,
                                 TOLERANCE)
    assert core_only.wealth[-1] == pytest.approx(1.0)                                     # 1 倍部分两日净收益为 0


def test_basket_is_the_equal_weight_average_of_the_two_assets() -> None:
    basket = basket_returns(days(3), closes(["100", "110", "99"], ["50", "45", "49.5"]))
    assert basket == pytest.approx([0.5 * (0.10 - 0.10), 0.5 * (-0.10 + 0.10)])           # 一涨一跌相抵
    basket = basket_returns(days(2), closes(["100", "104"], ["50", "51"]))
    assert basket == pytest.approx([0.5 * (0.04 + 0.02)])


def test_both_assets_carry_the_same_exposure() -> None:
    """R_j = 核心权重 × U_j + 杠杆权重 × λ × U_j：两资产暴露相同，只通过 U_j 进入。"""
    for core, leverage, total in ((0.6, 0.4, 1.4), (0.6, 0.0, 0.6), (0.3, 0.0, 0.3), (0.0, 0.0, 0.0)):
        assert portfolio_return(core, leverage, 2.0, 0.01) == pytest.approx(total * 0.01)
        assert portfolio_return(core, leverage, 2.0, -0.02) == pytest.approx(total * -0.02)
    # SPX +2%、QQQ −1%：U = 0.5%，正常仓位的组合收益为 1.4 × 0.5%。
    basket = basket_returns(days(2), closes(["100", "102"], ["100", "99"]))
    result = simulate_targets([target(1, 0.6, 0.4), target(2, 0.6, 0.4)], basket, POSITIONS, TOLERANCE)
    assert result.returns == pytest.approx([1.4 * 0.005])


def test_part_with_zero_target_is_not_rebalanced_back() -> None:
    """目标为 0 的部分不会被再平衡补回：一级期间杠杆部分恒为 0，大涨也不参与。"""
    path = ["100", "105", "110.25", "115.7625"]                                # 每日 +5%
    basket = basket_returns(days(4), closes(path, path))
    level1 = [target(number, 0.6, 0.0, Position.LEVEL1) for number in range(1, 5)]
    result = simulate_targets(level1, basket, POSITIONS, TOLERANCE)
    assert result.returns == pytest.approx([0.6 * 0.05] * 3)
    assert all(item.leverage == 0.0 for item in result.executions)
    cash = simulate_targets([target(number, 0.0, 0.0, Position.CASH) for number in range(1, 5)], basket, POSITIONS,
                            TOLERANCE)
    assert cash.returns == (0.0, 0.0, 0.0) and cash.wealth == (1.0, 1.0, 1.0, 1.0)   # 现金收益为 0


def test_leveraged_part_factor_must_be_positive() -> None:
    assert portfolio_return(0.6, 0.4, 2.0, -0.49) == pytest.approx(1.4 * -0.49)
    with pytest.raises(NavError, match="收益因子"):
        portfolio_return(0.6, 0.4, 2.0, -0.5)               # 1 + λU = 0：不为正
    with pytest.raises(NavError, match="收益因子"):
        portfolio_return(0.6, 0.4, 2.0, -0.6)
    # 杠杆部分暴露为 0 时不作此检查（没有杠杆子组合）。
    assert portfolio_return(0.6, 0.0, 2.0, -0.6) == pytest.approx(-0.36)
    path = ["100", "45"]                                    # 单日 −55%
    basket = basket_returns(days(2), closes(path, path))
    with pytest.raises(NavError, match="收益因子"):
        simulate_targets([target(1, 0.6, 0.4), target(2, 0.6, 0.4)], basket, POSITIONS, TOLERANCE)
    with pytest.raises(NavError):
        portfolio_return(0.6, 0.4, 2.0, math.nan)


def test_missing_price_makes_net_value_unavailable_without_any_approximation() -> None:
    with pytest.raises(NavUnavailable) as caught:
        basket_returns(days(5), closes(["100", "101", None, "103", "104"], ["50", None, None, "53", "54"]))
    assert caught.value.missing == (("QQQ", numbered(2)), ("QQQ", numbered(3)), ("SPX", numbered(3)))
    assert "无法计算" in str(caught.value) and str(numbered(3)) in str(caught.value)
    # 哪怕缺的是首日或末日，也不以相邻价格代替。
    for gap in (0, 4):
        values: list[str | None] = ["100", "101", "102", "103", "104"]
        values[gap] = None
        with pytest.raises(NavUnavailable):
            basket_returns(days(5), closes(values, ["50", "51", "52", "53", "54"]))


def test_net_value_starts_at_one_and_reconciles_with_log_returns() -> None:
    length = 600
    axis = days(length)
    basket = basket_returns(axis, {"SPX": random_closes(71, length), "QQQ": random_closes(72, length)})
    risks = [(Risk.NORMAL, Risk.LEVEL1, Risk.LEVEL2)[(number // 7) % 3] for number in range(length)]
    targets = signal_targets(axis, [signal(number, risk) for number, risk in enumerate(risks)], POSITIONS)
    result = simulate_targets(targets, basket, POSITIONS, TOLERANCE)
    assert result.wealth[0] == 1.0 and len(result.wealth) == length and len(result.returns) == length - 1
    # 独立重算：逐日连乘与对数收益之和。
    product = 1.0
    for change in result.returns:
        product *= 1 + change
    assert result.wealth[-1] == pytest.approx(product, rel=1e-12)
    assert abs(sum(math.log(1 + change) for change in result.returns) - math.log(result.wealth[-1])) <= 1e-10
    assert abs(result.log_sum - result.log_wealth) <= 1e-10
    # 每个区间的权重是区间起点那一日收盘执行的目标。
    for index, change in enumerate(result.returns[:50]):
        total = targets[index].core + 2.0 * targets[index].leverage
        assert change == pytest.approx(total * basket[index])


def test_reconciliation_failure_raises() -> None:
    path = ["100", "101", "102"]
    basket = basket_returns(days(3), closes(path, path))
    targets = [target(number, 0.6, 0.4) for number in range(1, 4)]
    with pytest.raises(NavError, match="对账不符"):
        simulate_targets(targets, basket, POSITIONS, -1.0)             # 容差为负：任何差值都超出
    with pytest.raises(NavError):
        simulate_targets(targets, basket[:1], POSITIONS, TOLERANCE)    # 目标与区间个数不匹配


def test_hold_is_always_at_the_normal_position() -> None:
    """同一组合一直持有（R1 的比较基准）：每个执行日都是正常仓位，组合收益为 1.4 × U_j。"""
    length = 300
    axis = days(length)
    basket = basket_returns(axis, {"SPX": random_closes(81, length), "QQQ": random_closes(82, length)})
    targets = hold_targets(axis, POSITIONS)
    assert all((item.position, item.core, item.leverage) == (Position.NORMAL, 0.6, 0.4) for item in targets)
    result = simulate_hold(axis, basket, POSITIONS, TOLERANCE)
    assert result.wealth[0] == 1.0
    assert result.returns == pytest.approx([1.4 * value for value in basket])
    assert [item.day for item in result.executions] == axis
