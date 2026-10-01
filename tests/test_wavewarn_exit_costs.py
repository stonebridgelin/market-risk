"""退出代价的执行日、事件边界与四类分母均由构造数据手工核对。"""

import datetime as dt
from decimal import Decimal

from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.exit_costs import (
    common_exit_start,
    event_denominators,
    event_ledger_inclusion,
    exit_cost_for_event,
    independent_green_execution_count,
    rebound_values,
)
from market_risk.wavewarn.labels_zz import ZZEvent


def _case(actual: str = "GGGRRGGGGG", *, censored: bool = False) -> tuple:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(10))
    closes = tuple(Decimal(value) for value in (105, 104, 103, 100, 95, 90, 80, 84, 88, 92))
    # 执行灯色的第 j 日由第 j−1 日信号决定；第0日用初始灯色。
    codes = {"G": "绿", "R": "红", "Y": "黄"}
    signals = tuple(codes[actual[min(index + 1, 9)]] for index in range(10))
    executions = execute_asset(days, signals, closes, Decimal("0.5"), initial_executed=codes[actual[0]])
    event = ZZEvent("SPX", days[3], days[4], days[6], None if censored else days[8],
                    Decimal(100), Decimal(80), censored)
    return days, closes, executions, event


def test_exit_tau_uses_t0_plus_63_and_all_27_p1_convergences() -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(100))
    # t0 在序号1，t0+63 是64；第27套直到序号70才收敛，所以统一 τ_E=70。
    assert common_exit_start(days, days[1], [days[50]] * 26 + [days[70]]) == days[70]


def test_event_ledger_requires_full_twenty_days_after_tau() -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(90))
    before = ZZEvent("SPX", days[69], days[70], days[71], days[72],
                     Decimal(100), Decimal(90), False)
    at = ZZEvent("SPX", days[70], days[71], days[72], days[73],
                 Decimal(100), Decimal(90), False)
    # τ_E=第50日；P=69 的 P−20=49 早于 τ；P=70 的 P−20=50 恰在 τ，纳入。
    assert event_ledger_inclusion(before, days, days[0], days[50]) == "状态窗口不足：P−20 早于 τ_E"
    assert event_ledger_inclusion(at, days, days[0], days[50]) == "纳入"


def test_exit_inclusion_uses_tau_and_four_disjoint_denominators() -> None:
    days, closes, executions, event = _case()
    old = ZZEvent("SPX", days[0], days[1], days[2], days[3], closes[0], closes[2], False)
    censored = ZZEvent("SPX", days[1], days[2], days[4], None, closes[1], closes[4], True)
    rows = [exit_cost_for_event(days, executions, closes, item, None, days[1], days[3])
            for item in (old, event, censored)]
    # P=0 早于 t0=1 单列；P=τ_E=3 纳入；右截尾优先独立计数；寻峰尾段另加1。
    assert [row.inclusion for row in rows] == ["状态窗口不足：跨 t0", "纳入", "右截尾"]
    assert event_denominators((old, event, censored), rows, tail_pending=1) == {
        "纳入": 1, "状态窗口不足": 1, "右截尾": 1, "尾段未定": 1,
    }


def test_exit_inclusion_pre_t0_cross_t0_between_t0_tau_and_exact_tau() -> None:
    days, closes, executions, event = _case()
    cases = (
        ZZEvent("SPX", days[0], days[0], days[1], days[2], closes[0], closes[1], False),
        ZZEvent("SPX", days[1], days[2], days[4], days[5], closes[1], closes[4], False),
        ZZEvent("SPX", days[2], days[3], days[5], days[6], closes[2], closes[5], False),
        event,
    )
    results = [exit_cost_for_event(days, executions, closes, item, None, days[2], days[3]).inclusion
               for item in cases]
    # t0=第2日，τ=第3日：P=0且Tr=1在t0前；P=1跨t0；P=2处于t0至τ；P=3恰等于τ。
    assert results == ["状态窗口不足：t0 前", "状态窗口不足：跨 t0",
                       "状态窗口不足：t0 至 τ_E", "纳入"]


def test_exit_halfway_multiple_green_uses_deepest_execution_decline() -> None:
    days, closes, executions, event = _case("GRRGRGRRRG")
    row = exit_cost_for_event(days, executions, closes, event, None, days[0], days[3])
    # 第3日100元转绿、第5日90元再转绿，终低80；跌幅分别为20%及1−80/90=11.111…%。
    # 最深事件值为20%；第4日和第6日曾重新非绿，低点执行灯色非绿。
    assert row.half_way_green_count == 2
    assert row.deepest_decline == Decimal("0.2")
    assert row.re_alerted_before_trough is True
    # 第5日90元转绿，Tr−1=第5日执行绿，最后下跌区间仍满暴露：类别①b。
    assert row.rebound_class == "①低点前已绿"
    assert row.green_at_trough_subclass == "①b 低点前转绿"
    assert row.rebound_recovery is None
    assert row.green_executions[0].signal_close == Decimal(103)
    assert row.green_executions[0].execution_close == Decimal(100)
    assert row.green_executions[0].signal_decline_to_trough == 1 - Decimal(80) / Decimal(103)


def test_exit_green_at_trough_and_continuous_green_subclasses() -> None:
    days, closes, executions, event = _case("GGGGGGGGGG")
    row = exit_cost_for_event(days, executions, closes, event, None, days[0], days[3])
    # P−1 到 Tr 全绿，①a；没有事件内非绿→绿，跌幅不能填0。
    assert (row.rebound_class, row.green_at_trough_subclass) == ("①低点前已绿", "①a 高点前已绿并持续")
    assert row.half_way_green_count == 0 and row.deepest_decline is None and row.rebound_recovery is None
    days, closes, executions, event = _case("GGGGGGRRRR")
    row = exit_cost_for_event(days, executions, closes, event, None, days[0], days[3])
    # Tr=第6日虽执行红，Tr−1=第5日仍执行绿，故最后下跌区间暴露为绿，仍属①a。
    assert executions[6].executed == "红" and executions[5].executed == "绿"
    assert row.green_at_trough_subclass == "①a 高点前已绿并持续"
    days, closes, executions, event = _case("GGGRRGGGGG")
    row = exit_cost_for_event(days, executions, closes, event, None, days[0], days[3])
    # P−1 绿，第4日红、第5日(Tr−1)转绿，故①b且有一次半山腰切换。
    assert executions[4].executed == "红" and executions[5].executed == "绿"
    assert row.green_at_trough_subclass == "①b 低点前转绿"
    assert row.half_way_green_count == 1


def test_exit_rebound_classes_next_t0_boundary_and_same_day_trough() -> None:
    days, closes, executions, event = _case("GGGRRRGGGG")
    row = exit_cost_for_event(days, executions, closes, event, days[8], days[0], days[3])
    # Tr−1=第5日执行红；Tr=第6日才转绿，属于②，R=(80−80)/(100−80)=0。
    assert executions[5].executed == "红" and executions[6].executed == "绿"
    assert row.rebound_class == "②低点或之后转绿"
    assert row.rebound_recovery == Decimal(0)
    assert row.half_way_green_count == 0 and row.green_executions[0].timing == "低点当日"
    days, closes, executions, event = _case("GGGRRRRGGG")
    row = exit_cost_for_event(days, executions, closes, event, days[8], days[0], days[3])
    # 第7日84元转绿，较低点80元收复4/20=20%；早于下一事件 T0=第8日。
    assert row.rebound_class == "②低点或之后转绿"
    assert row.rebound_recovery == Decimal("0.2")
    assert row.raw_rebound_percent == Decimal(5)
    assert row.green_executions[0].rebound_from_trough_percent == Decimal(5)
    row = exit_cost_for_event(days, executions, closes, event, days[7], days[0], days[3])
    # g 与下一事件 T0 同为第7日，严格不早于，故类别③，不把新事件的解除算给本事件。
    assert row.rebound_class == "③下一事件前未转绿" and row.rebound_recovery is None


def test_exit_rebound_main_includes_g_at_trough_and_contrast_excludes_it() -> None:
    days, closes, executions, event = _case("GGGRRRGGGG")
    at_trough = exit_cost_for_event(days, executions, closes, event, None, days[0], days[3])
    days, closes, executions, event = _case("GGGRRRRGGG")
    after_trough = exit_cost_for_event(days, executions, closes, event, None, days[0], days[3])
    # g=Tr 的 R=0；g=Tr+1、84元的 R=(84−80)/(100−80)=0.2。
    # 主口径二者均纳入，线性中位数=(0+0.2)/2=0.1；对照只剩0.2。
    assert rebound_values((at_trough, after_trough)) == (Decimal(0), Decimal("0.2"))
    assert rebound_values((at_trough, after_trough), exclude_trough_day=True) == (Decimal("0.2"),)


def test_exit_early_green_then_relit_non_green_and_later_green_is_category_two() -> None:
    days, closes, executions, event = _case("GRRGRRRGGG")
    row = exit_cost_for_event(days, executions, closes, event, None, days[0], days[3])
    # 第3日100元转绿，第4日再转红；Tr=第6日80元时非绿；第7日84元再次转绿。
    # 因低点执行非绿，属于②，R=(84−80)/(100−80)=20%；同时半山腰转绿一次。
    assert row.half_way_green_count == 1
    assert row.re_alerted_before_trough is True
    assert row.rebound_class == "②低点或之后转绿"
    assert row.rebound_recovery == Decimal("0.2")


def test_exit_green_scope_stops_before_next_event_t0_even_if_censored() -> None:
    days, closes, executions, event = _case("GGGRRRRGGG")
    next_event = ZZEvent("SPX", days[7], days[7], days[9], None,
                         closes[7], closes[9], True)
    row = exit_cost_for_event(days, executions, closes, event, next_event.t0_date, days[0], days[3])
    # 第7日是下一事件 T0，严格不早于；旧事件只记晚到日期，不列逐次转绿，也不计 R。
    assert row.rebound_class == "③下一事件前未转绿"
    assert row.first_green_from_trough == days[7]
    assert row.green_executions == ()
    # 下一个事件虽右截尾，其 T0=第7日仍已由历史价格确定，须作为上界。
    assert next_event.right_censored is True


def test_exit_green_overlap_has_two_roles_but_one_independent_execution() -> None:
    days, initial, _, first = _case("GGGRRRRGGG")
    closes = tuple(Decimal(110) if index == 7 else Decimal(105) if index == 8 else
                   Decimal(90) if index == 9 else value for index, value in enumerate(initial))
    actual = "GGGRRRRGGG"
    signals = tuple({"G": "绿", "R": "红"}[actual[min(index + 1, 9)]] for index in range(10))
    executions = execute_asset(days, signals, closes, Decimal("0.5"))
    second = ZZEvent("SPX", days[7], days[8], days[9], None,
                     Decimal(110), Decimal(90), True)
    old = exit_cost_for_event(days, executions, closes, first, days[8], days[0], days[3])
    new = exit_cost_for_event(days, executions, closes, second, None, days[0], days[3])
    # 第7日转绿：旧事件低点后窗口与新事件[P′,Tr′)各一行，独立执行只发生一次。
    # 新事件右截尾的纯函数不计入已确认分母，因此这里另构造已确认版本检验角色。
    assert old.green_executions[0].role == "低点后窗口"
    assert new.green_executions == ()
    confirmed = ZZEvent("SPX", days[7], days[8], days[9], days[9],
                        Decimal(110), Decimal(90), False)
    new = exit_cost_for_event(days, executions, closes, confirmed, None, days[0], days[3])
    assert new.green_executions[0].role == "本事件[P,Tr)内"
    assert independent_green_execution_count((old, new)) == 1


def test_exit_last_event_can_record_window_final_execution_but_not_later_signal() -> None:
    days, closes, executions, event = _case("GGGRRRRRRG")
    row = exit_cost_for_event(days, executions, closes, event, None, days[0], days[3])
    # 最后一个窗口日第9日执行转绿，属于类别②，g=9，R=(92−80)/(100−80)=60%。
    assert row.first_green_from_trough == days[9]
    assert row.green_executions[-1].execution_date == days[9]
    assert row.rebound_recovery == Decimal("0.6")
    # 第9日收盘才出现的信号在第10日执行，超出长度为10的评价窗，函数没有第10日可记。
