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


# ---------------------------------------------------------------------------
# 研究模拟层（补充裁决 Q3；第九节分层裁决）：simulate_policy_with_gaps
# 比较口径（第九节第 4 条）：净值、收益及浮点权重用 math.isclose(rel_tol=0.0, abs_tol=1e-12)；
# 日期、状态、来源、上限标记、记录数量、停止日与原因精确比较。
# ---------------------------------------------------------------------------

STOP_PATH = ["100", "97", "90", "91", "92", "93", "94", "95", "96", "97", "98", "99", "100", "100", "101", "102"]


def close(first: float, second: float) -> bool:
    return math.isclose(first, second, rel_tol=0.0, abs_tol=1e-12)


def same_targets(first, second) -> bool:
    return len(first) == len(second) and all(
        (a.day, a.position, a.source, a.cap_active) == (b.day, b.position, b.source, b.cap_active)
        and close(a.core, b.core) and close(a.leverage, b.leverage) for a, b in zip(first, second, strict=True))


def same_executions(first, second) -> bool:
    return len(first) == len(second) and all(
        (a.day, a.position) == (b.day, b.position) and close(a.core, b.core) and close(a.leverage, b.leverage)
        and close(a.wealth, b.wealth) for a, b in zip(first, second, strict=True))


def policy_with(prices: list[str | None]):
    """两资产同价、日期轴为第 1 至 16 日、信号一直正常且完整（同执行层的端到端例子：第 2 日收盘止损确认，
    第 3 日离场，第 4 至 13 日冷却，第 13 日收盘重入、第 14 日执行，第 15 日恢复杠杆）。"""
    from wavewarn_v20_helpers import POLICY

    from market_risk.wavewarn_v20 import nav

    axis = days(16)
    signals = [signal(number, Risk.NORMAL) for number in range(0, 16)]
    return nav.simulate_policy_with_gaps(axis, signals, closes(prices, prices), POSITIONS, POLICY, TOLERANCE)


def gapped(*missing_days: int) -> list[str | None]:
    """STOP_PATH 中第 missing_days 日（从 1 起）缺价，其余照常。"""
    return [None if number in missing_days else value for number, value in enumerate(STOP_PATH, start=1)]


def test_research_1_gap_while_holding_keeps_day_m_target_and_stops_from_day_m_plus_1() -> None:
    """研究层 1：持仓时缺少净值。第 15 日（m）缺价、此时处于本轮之内：
    第 15 日的目标（由第 14 日已确定的信息产生）保留；第 16 日起“无法确定”；
    第 14→15 日的区间收益与第 15 日净值无法计算（执行与净值只到第 14 日）。"""
    from market_risk.wavewarn_v20 import nav

    complete = policy_with(STOP_PATH)
    partial = policy_with(gapped(15))
    assert type(partial) is nav.PartialPolicyResult and not isinstance(partial, nav.PolicyResult)
    assert same_targets(partial.targets, complete.targets[:15])                     # 第 1 至 15 日的目标
    assert partial.undetermined == nav.Undetermined(numbered(16), nav.UNDETERMINED_REASON)
    assert same_executions(partial.executions, complete.nav.executions[:14])        # 第 1 至 14 日
    assert len(partial.returns) == 13
    assert all(close(a, b) for a, b in zip(partial.returns, complete.nav.returns[:13], strict=True))
    assert partial.missing == (("QQQ", numbered(15)), ("SPX", numbered(15)))
    # 持仓初期缺价（第 2 日）：第 2 日目标保留，第 3 日起无法确定；只剩第 1 日的执行，没有任何区间收益。
    early = policy_with(gapped(2))
    assert same_targets(early.targets, complete.targets[:2]) and early.undetermined.day == numbered(3)
    assert len(early.executions) == 1 and early.returns == ()


def test_research_2_stop_confirmed_before_the_gap_is_kept() -> None:
    """研究层 2：缺价之前已确认的止损（第 2 日收盘确认，第 3 日执行）保留，此后的现金目标保留。第 4 日缺价。"""
    complete = policy_with(STOP_PATH)
    partial = policy_with(gapped(4))
    assert partial.targets[2].day == numbered(3) and partial.targets[2].source is TargetSource.STOP_CASH
    assert [item.source for item in partial.targets[3:13]] == [TargetSource.OUT_CASH] * 10     # 第 4 至 13 日
    assert same_targets(partial.targets, complete.targets[:14])                                 # 至重入日第 14 日
    assert same_executions(partial.executions, complete.nav.executions[:3])                     # 第 1 至 3 日
    assert partial.undetermined is not None and partial.undetermined.day == numbered(15)


def test_research_3_gap_during_cooldown_keeps_cooldown_and_cash_targets() -> None:
    """研究层 3：现金冷却期间（第 6 日）缺价，冷却与现金目标只依赖信号，照常保留。"""
    complete = policy_with(STOP_PATH)
    partial = policy_with(gapped(6))
    assert [item.source for item in partial.targets[2:13]] == [TargetSource.STOP_CASH, *[TargetSource.OUT_CASH] * 10]
    assert same_targets(partial.targets, complete.targets[:14])
    assert same_executions(partial.executions, complete.nav.executions[:5])                     # 第 1 至 5 日


def test_research_4_reentry_kept_and_undetermined_when_the_benchmark_reset_lacks_net_value() -> None:
    """研究层 4：重入目标（第 13 日收盘由信号确定、第 14 日执行）保留；第 14 日缺价，重入后需以当日净值重置基准，
    净值不可得，从下一执行日第 15 日起“无法确定”。"""
    from market_risk.wavewarn_v20 import nav

    complete = policy_with(STOP_PATH)
    partial = policy_with(gapped(14))
    assert partial.targets[13].day == numbered(14) and partial.targets[13].source is TargetSource.REENTRY_CAP
    assert partial.targets[13].cap_active
    assert same_targets(partial.targets, complete.targets[:14])
    assert partial.undetermined == nav.Undetermined(numbered(15), nav.UNDETERMINED_REASON)
    assert same_executions(partial.executions, complete.nav.executions[:13])                    # 第 1 至 13 日


def test_research_5_research_path_does_not_resume_after_prices_come_back() -> None:
    """研究层 5：第 4 日缺价、第 5 日起价格重新齐全：净值仍不可得（执行只到第 3 日），重入之后仍“无法确定”；
    不补算缺口，也不重置止损基准继续推演。"""
    partial = policy_with(gapped(4))
    assert max(item.day for item in partial.executions) == numbered(3)
    assert len(partial.returns) == len(partial.executions) - 1 == 2
    assert partial.undetermined is not None and partial.undetermined.day == numbered(15)
    assert all(item.day <= numbered(14) for item in partial.targets)
    holding_gap = policy_with(gapped(15))                                            # 第 16 日价格恢复
    assert max(item.day for item in holding_gap.executions) == numbered(14)
    first_day_gap = policy_with(gapped(1))                                           # 首日缺价：没有任何执行记录
    assert first_day_gap.executions == () and first_day_gap.returns == ()
    assert first_day_gap.undetermined is not None and first_day_gap.undetermined.day == numbered(2)


def test_research_6_complete_prices_give_exactly_the_same_result_as_simulate_policy() -> None:
    """研究层 6：价格完整时，simulate_policy_with_gaps 与 simulate_policy 的结果完全相同（逐字段相等，不用容差）。"""
    from wavewarn_v20_helpers import POLICY

    from market_risk.wavewarn_v20 import nav

    for prices, length in ((STOP_PATH, 16), (None, 300)):
        axis = days(length)
        if prices is None:
            data = {"SPX": random_closes(91, length), "QQQ": random_closes(92, length)}
            risks = [(Risk.NORMAL, Risk.LEVEL1, Risk.LEVEL2)[(number // 9) % 3] for number in range(length)]
            signals = [signal(number, risk) for number, risk in enumerate(risks)]
        else:
            data = closes(prices, prices)
            signals = [signal(number, Risk.NORMAL) for number in range(0, length)]
        with_gaps = nav.simulate_policy_with_gaps(axis, signals, data, POSITIONS, POLICY, TOLERANCE)
        direct = nav.simulate_policy(axis, signals, basket_returns(axis, data), POSITIONS, POLICY, TOLERANCE)
        assert type(with_gaps) is nav.PolicyResult and with_gaps == direct


def test_basket_prefix_stops_before_the_first_missing_day() -> None:
    from market_risk.wavewarn_v20.nav import basket_prefix

    prefix = basket_prefix(days(5), closes(["100", "101", "102", None, "104"], ["50", "51", "52", "53", "54"]))
    assert prefix.first_missing == 3 and len(prefix.values) == 2                    # 第 1→2、2→3 日两个区间
    assert prefix.missing == (("SPX", numbered(4)),)
    assert basket_prefix(days(3), closes(["1", "2", "3"], ["1", "2", "3"])).first_missing is None
