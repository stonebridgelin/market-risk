"""v2.0 执行层（登记第一节第 6、7 小节；产品规格第八节第 1、2 条例子一、例子二）：只用构造数据。

例子里的“第 n 日”直接作为执行日序号使用；净值是手工给出的数，不来自任何价格。
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest
from wavewarn_v20_helpers import POLICY, POSITIONS, TOLERANCE, evidence, numbered, random_closes, run_model, signal

from market_risk.wavewarn_v20 import execution, nav
from market_risk.wavewarn_v20.channels import ChannelState
from market_risk.wavewarn_v20.convergence import Candidate
from market_risk.wavewarn_v20.execution import (
    ActualExecution,
    ExecutionError,
    PlannedTarget,
    PolicyState,
    Position,
    SignalRecord,
    SimulatedExecution,
    TargetSource,
    policy_next,
    policy_start,
    position_of,
    signal_records,
    signal_targets,
    switches,
)
from market_risk.wavewarn_v20.state_machine import Risk, SystemState, run_system

ACTIVE = ChannelState.ACTIVE
NORMAL, LEVEL1, LEVEL2 = Risk.NORMAL, Risk.LEVEL1, Risk.LEVEL2
PACKAGE = Path(execution.__file__).parent


def exposure(target: PlannedTarget) -> float:
    return target.core + POSITIONS.leverage * target.leverage


def step(state: PolicyState, number: int, risk: Risk, wealth: float | None, valid: bool = True
         ) -> tuple[PolicyState, PlannedTarget]:
    """第 number 日收盘后：给出当日信号与当日净值，得到第 number + 1 日的计划目标。"""
    return policy_next(state, number, numbered(number + 1), signal(number, risk, valid), wealth, POSITIONS, POLICY)


def walk(state: PolicyState, start: int, days: list[tuple[Risk, float | None, bool]]
         ) -> tuple[PolicyState, dict[int, PlannedTarget]]:
    """从第 start 日起逐日推进；返回各执行日（start + 1 起）的计划目标。"""
    targets: dict[int, PlannedTarget] = {}
    for offset, (risk, wealth, valid) in enumerate(days):
        state, targets[start + offset + 1] = step(state, start + offset, risk, wealth, valid)
    return state, targets


HOLDING = PolicyState(in_round=True, benchmark=1.0, exit_index=None, cap=False)


# ---------------------------------------------------------------------------
# 仓位映射与信号模拟（第一节第 6 小节第 5、6 步；规格第八节第 1 条例子一）
# ---------------------------------------------------------------------------


def test_position_map_exposures() -> None:
    expected = {Position.NORMAL: (0.6, 0.4, 1.4), Position.LEVEL1: (0.6, 0.0, 0.6),
                Position.LEVEL2: (0.3, 0.0, 0.3), Position.CASH: (0.0, 0.0, 0.0)}
    for position, (core, leverage, total) in expected.items():
        weights = POSITIONS.weights(position)
        assert (weights.core, weights.leverage) == (core, leverage)
        assert POSITIONS.exposure(position) == pytest.approx(total)
    assert [position_of(risk) for risk in Risk] == [Position.NORMAL, Position.LEVEL1, Position.LEVEL2]
    # 重入上限：核心与杠杆分别与一级仓位取小。
    assert [POSITIONS.capped(position) for position in Position] == [
        Position.LEVEL1, Position.LEVEL1, Position.LEVEL2, Position.CASH]


def example_one_signals(day_5_level_1: bool) -> list[SignalRecord]:
    """例子一的六个信号日（第 0 至 5 日），由状态机算出：h = 1，逐级恢复只发生在 S 里。"""
    proofs = [evidence(day=numbered(0)),                                              # 第 0 日：正常
              evidence(p_spx=ACTIVE, day=numbered(1)),                                # 第 1 日：一级证据
              evidence(p_spx=ACTIVE, pr_spx=ACTIVE, day=numbered(2)),                 # 第 2 日：二级证据
              evidence(p_spx=ACTIVE, pr_spx=ACTIVE, day=numbered(3)),                 # 第 3 日：二级证据
              evidence(day=numbered(4)),                                              # 第 4 日：证据解除
              evidence(p_spx=ACTIVE, day=numbered(5)) if day_5_level_1 else evidence(day=numbered(5))]
    system = run_system(SystemState(NORMAL, 0, 0), proofs, 5, 1)
    return list(signal_records(system, proofs))


def test_product_example_one_day_by_day() -> None:
    signals = example_one_signals(False)
    # 规格表里第 4 日“信号为正常”指证据解除；v2.0 的风险状态 S 在第 4 日为一级、第 5 日为正常（每日最多降一级）。
    assert [item.risk for item in signals] == [NORMAL, LEVEL1, LEVEL2, LEVEL2, LEVEL1, NORMAL]
    targets = signal_targets([numbered(number) for number in range(1, 7)], signals, POSITIONS)
    assert [item.day for item in targets] == [numbered(number) for number in range(1, 7)]
    assert [item.position for item in targets] == [Position.NORMAL, Position.LEVEL1, Position.LEVEL2,
                                                   Position.LEVEL2, Position.LEVEL1, Position.NORMAL]
    assert [exposure(item) for item in targets] == pytest.approx([1.4, 0.6, 0.3, 0.3, 0.6, 1.4])
    assert all(item.source is TargetSource.SYSTEM and not item.cap_active for item in targets)


def test_product_example_one_variant_level_1_on_day_5_keeps_exposure() -> None:
    """若第 5 日的信号为一级，第 6 日收盘执行的目标为一级，暴露保持 0.6。"""
    targets = signal_targets([numbered(number) for number in range(1, 7)], example_one_signals(True), POSITIONS)
    assert targets[5].position is Position.LEVEL1 and exposure(targets[5]) == pytest.approx(0.6)


def test_execution_layer_adds_no_second_stage_recovery() -> None:
    """只有一层阶段恢复：计划目标恒等于映射(S_d)，从二级恢复的延迟只来自 S。"""
    # 从二级恢复：S 为 二级、一级、正常 → 目标依次 0.3、0.6、1.4，执行层没有再加一天。
    days = [numbered(number) for number in range(1, 4)]
    targets = signal_targets(days, [signal(0, LEVEL2), signal(1, LEVEL1), signal(2, NORMAL)], POSITIONS)
    assert [exposure(item) for item in targets] == pytest.approx([0.3, 0.6, 1.4])
    # S 从二级直接给出正常时（执行层不该出现的输入），映射仍是直接映射，不自行插入一级。
    direct = signal_targets(days[:2], [signal(0, LEVEL2), signal(1, NORMAL)], POSITIONS)
    assert [item.position for item in direct] == [Position.LEVEL2, Position.NORMAL]
    # 随机构造路径：逐日相等。
    model = run_model(random_closes(61, 700), random_closes(62, 700), Candidate(5, Decimal("0.02"), 3))
    records = signal_records(model.system, model.evidence)
    execution_days = [item.day for item in records[1:]]
    planned = signal_targets(execution_days, records[:-1], POSITIONS)
    assert [item.position for item in planned] == [position_of(item.risk) for item in records[:-1]]
    assert {item.position for item in planned} == {Position.NORMAL, Position.LEVEL1, Position.LEVEL2}


def test_signal_targets_require_day_by_day_alignment() -> None:
    with pytest.raises(ExecutionError, match="对齐"):
        signal_targets([numbered(1), numbered(2)], [signal(0, NORMAL), signal(5, NORMAL)], POSITIONS)
    with pytest.raises(ExecutionError):
        signal_targets([numbered(1)], [signal(1, NORMAL)], POSITIONS)        # 首个信号日必须早于首个执行日


# ---------------------------------------------------------------------------
# 计划目标与执行隔离（第一节第 6 小节“三者分开记录”）
# ---------------------------------------------------------------------------


def test_three_record_types_do_not_substitute_for_each_other() -> None:
    kinds = (SignalRecord, PlannedTarget, SimulatedExecution, ActualExecution)
    for first in kinds:
        for second in kinds:
            assert first is second or not issubclass(first, second)
    fields = {kind: {field.name for field in dataclasses.fields(kind)} for kind in kinds}
    assert "wealth" not in fields[PlannedTarget] and "wealth" in fields[SimulatedExecution]
    assert "risk" in fields[SignalRecord] and "risk" not in fields[PlannedTarget]
    assert "source" in fields[PlannedTarget] and "source" not in fields[SimulatedExecution]
    assert {"executed", "recorded_at"} <= fields[ActualExecution]


def test_net_value_is_driven_by_planned_targets_only() -> None:
    target = PlannedTarget(numbered(1), Position.NORMAL, 0.6, 0.4, TargetSource.SYSTEM, False)
    done = SimulatedExecution(numbered(1), Position.NORMAL, 0.6, 0.4, 1.0)
    assert nav.simulate_targets([target], [], POSITIONS, TOLERANCE).executions == (done,)
    with pytest.raises(nav.NavError, match="PlannedTarget"):
        nav.simulate_targets([done], [], POSITIONS, TOLERANCE)                    # type: ignore[list-item]
    with pytest.raises(nav.NavError, match="PlannedTarget"):
        nav.simulate_targets([signal(1, NORMAL)], [], POSITIONS, TOLERANCE)       # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 止损、冷却与重入（规格第八节第 2 条例子二；登记第一节第 6 小节）
# ---------------------------------------------------------------------------


def test_window_start_begins_round_one_with_benchmark_one() -> None:
    state, target = policy_start(numbered(1), signal(0, LEVEL2), POSITIONS)
    # j₀ 的目标由 S_{j₀−1} 决定；若为二级，组合以 0.3 倍开始；没有未执行的止损、不在冷却期、U = 无。
    assert (target.position, exposure(target), target.source) == (Position.LEVEL2, pytest.approx(0.3),
                                                                  TargetSource.SYSTEM)
    assert state == PolicyState(in_round=True, benchmark=None, exit_index=None, cap=False)
    after, _ = step(state, 1, NORMAL, 1.0)                 # j₀ 收盘净值记为 1：第一轮基准 = 1
    assert after.benchmark == 1.0
    stopped, target = step(after, 2, NORMAL, 0.96)         # 相对基准 1 恰好 96%
    assert target.position is Position.CASH and not stopped.in_round


def test_product_example_two_day_by_day() -> None:
    """第 20 日止损确认、第 21 日离场、第 22—31 日冷却、第 32 日重入、第 33 日恢复杠杆。"""
    state, target_20 = step(HOLDING, 19, NORMAL, 1.0)
    assert target_20.position is Position.NORMAL                        # 第 20 日仍持仓
    # 第 20 日：收盘净值为基准的 95.8%，止损确认；当日系统信号为正常，不能取消。
    state, target_21 = step(state, 20, NORMAL, 0.958)
    assert (target_21.day, target_21.position, target_21.source) == (numbered(21), Position.CASH,
                                                                     TargetSource.STOP_CASH)
    assert state == PolicyState(in_round=False, benchmark=None, exit_index=21, cap=False)       # s = 21
    # 第 21 至 30 日：信号一直正常、输入完整，也保持现金（冷却期 22—31，最早第 31 日收盘检查）。
    state, cash = walk(state, 21, [(NORMAL, None, True)] * 10)
    assert sorted(cash) == list(range(22, 32))
    assert all(item.position is Position.CASH and item.source is TargetSource.OUT_CASH for item in cash.values())
    # 第 31 日：信号为正常、输入完整 → 第 32 日重入第一阶段（核心 0.6），新一轮开始。
    state, target_32 = step(state, 31, NORMAL, None)
    assert (target_32.day, target_32.position, target_32.source, target_32.cap_active) == (
        numbered(32), Position.LEVEL1, TargetSource.REENTRY_CAP, True)
    assert (target_32.core, target_32.leverage) == (0.6, 0.0)
    assert state == PolicyState(in_round=True, benchmark=None, exit_index=None, cap=True)
    # 第 32 日：基准重置为该日收盘净值；信号仍为正常 → 第 33 日恢复杠杆，暴露 1.4。
    state, target_33 = step(state, 32, NORMAL, 0.93)
    assert state.benchmark == 0.93 and not state.cap
    assert (target_33.position, exposure(target_33), target_33.source) == (Position.NORMAL, pytest.approx(1.4),
                                                                           TargetSource.SYSTEM)


def test_product_example_two_variant_waits_for_first_normal_and_complete_signal_day() -> None:
    out = PolicyState(in_round=False, benchmark=None, exit_index=21, cap=False)
    # 第 31 日信号为一级：继续现金；第 32 日正常但输入不完整：继续现金；第 33 日二级；
    # 第 34 日正常且完整 → 第 35 日重入。
    state, targets = walk(out, 31, [(LEVEL1, None, True), (NORMAL, None, False), (LEVEL2, None, True),
                                    (NORMAL, None, True)])
    assert [targets[number].position for number in (32, 33, 34)] == [Position.CASH] * 3
    assert targets[35].position is Position.LEVEL1 and targets[35].source is TargetSource.REENTRY_CAP
    assert state.in_round and state.cap


def test_cooldown_lasts_ten_trading_days() -> None:
    out = PolicyState(in_round=False, benchmark=None, exit_index=21, cap=False)
    _, early = step(out, 30, NORMAL, None)                  # s + 9：仍在冷却期内，不检查
    assert early.position is Position.CASH and early.source is TargetSource.OUT_CASH
    _, due = step(out, 31, NORMAL, None)                    # s + 10：最早的检查日
    assert due.position is Position.LEVEL1


@pytest.mark.parametrize(("wealth", "stopped"), [(0.96, True), (0.9599999999, True), (0.9600000001, False),
                                                 (1.0, False)])
def test_stop_is_confirmed_at_exactly_96_percent_of_benchmark(wealth: float, stopped: bool) -> None:
    """止损等号：W 恰好等于 0.96 × 基准时确认。"""
    state, target = step(HOLDING, 5, NORMAL, wealth)
    assert (target.position is Position.CASH) is stopped
    assert state.in_round is not stopped


def test_benchmark_is_the_running_maximum_of_the_round_including_today() -> None:
    state, _ = step(HOLDING, 5, NORMAL, 1.10)               # 当日净值创本轮新高：基准升到 1.10
    assert state.benchmark == 1.10
    state, target = step(state, 6, NORMAL, 1.07)            # 未到 1.10 × 0.96
    assert state.benchmark == 1.10 and target.position is Position.NORMAL
    line = 0.96 * 1.10
    _, target = step(state, 7, NORMAL, line)                # 恰好等于止损线
    assert target.position is Position.CASH and target.source is TargetSource.STOP_CASH


@pytest.mark.parametrize("risk", list(Risk))
def test_no_system_signal_can_cancel_a_confirmed_stop(risk: Risk) -> None:
    state, target = step(HOLDING, 5, risk, 0.95)
    assert target.position is Position.CASH and target.source is TargetSource.STOP_CASH
    assert state.exit_index == 6


def test_stop_is_judged_on_the_first_day_the_net_value_is_available() -> None:
    """判断所需的价格缺失（净值不可得）时不判断，留到净值可得的第一个交易日；缺失不取消止损。"""
    state, targets = walk(HOLDING, 5, [(NORMAL, None, True), (NORMAL, None, True), (NORMAL, 0.95, True)])
    assert [targets[number].position for number in (6, 7)] == [Position.NORMAL, Position.NORMAL]
    assert targets[8].position is Position.CASH and state.exit_index == 8
    # 已确认的止损：执行日当天净值不可得，也照常离场，之后保持现金。
    state, target = step(HOLDING, 5, NORMAL, 0.95)
    state, later = walk(state, 6, [(NORMAL, None, True)] * 3)
    assert target.position is Position.CASH
    assert all(item.position is Position.CASH for item in later.values()) and not state.in_round


def test_new_round_resets_the_benchmark_to_the_reentry_day_net_value() -> None:
    reentered = PolicyState(in_round=True, benchmark=None, exit_index=None, cap=True)
    state, _ = step(reentered, 32, LEVEL1, 0.90)             # 重入日收盘净值 0.90：新一轮基准
    assert state.benchmark == 0.90
    state, target = step(state, 33, LEVEL1, 0.88)            # 相对旧基准 1.0 早已低于 96%，但旧基准已失效
    assert target.position is Position.LEVEL1 and state.in_round
    _, target = step(state, 34, LEVEL1, 0.86)                # 新基准的止损线为 0.90 × 0.96 = 0.864
    assert target.position is Position.CASH


# ---------------------------------------------------------------------------
# 重入上限 U（登记第一节第 6 小节例子甲、乙、丙）
# ---------------------------------------------------------------------------

OUT = PolicyState(in_round=False, benchmark=None, exit_index=21, cap=False)       # s = 21，冷却期 22—31


def test_registered_reentry_example_a() -> None:
    state, targets = walk(OUT, 31, [(NORMAL, None, True), (NORMAL, 0.93, True)])
    assert exposure(targets[32]) == pytest.approx(0.6) and targets[32].cap_active     # 31：设 U = 一级 → 32：0.6
    assert exposure(targets[33]) == pytest.approx(1.4) and not targets[33].cap_active  # 32：U → 无 → 33：1.4
    assert not state.cap


def test_registered_reentry_example_b_gap_on_day_32_blocks_release_until_day_232() -> None:
    """乙：第 32 日缺价未补齐。S 保持正常，但五个通道在第 232 日之前不能全部有效，U 不能解除。"""
    days = [(NORMAL, None, True)]                               # 31：正常，全部有效 → 设 U = 一级
    days += [(NORMAL, None, False)] * 200                       # 32 至 231：S 为正常，五个通道未全部有效
    days += [(NORMAL, None, True)]                              # 232：最早可能全部有效的信号日
    state, targets = walk(OUT, 31, days)
    assert sorted(targets) == list(range(32, 234))
    assert all(exposure(targets[number]) == pytest.approx(0.6) for number in range(32, 233))
    assert all(targets[number].cap_active and targets[number].source is TargetSource.REENTRY_CAP
               for number in range(32, 233))
    assert exposure(targets[233]) == pytest.approx(1.4) and not targets[233].cap_active   # 最早第 233 日为 1.4
    assert not state.cap
    # 第 232 日条件不满足则更晚：U 不随时间解除。
    state, later = walk(OUT, 31, [*days[:-1], (NORMAL, None, False), (LEVEL1, None, True), (NORMAL, None, True)])
    assert [exposure(later[number]) for number in (233, 234)] == pytest.approx([0.6, 0.6])
    assert exposure(later[235]) == pytest.approx(1.4)


def test_registered_reentry_example_c_upgrade_after_reentry() -> None:
    state, targets = walk(OUT, 31, [(NORMAL, None, True), (LEVEL2, 0.93, True), (LEVEL1, 0.93, True),
                                    (NORMAL, 0.93, True)])
    assert [exposure(targets[number]) for number in (32, 33, 34, 35)] == pytest.approx([0.6, 0.3, 0.6, 1.4])
    assert [targets[number].cap_active for number in (32, 33, 34, 35)] == [True, True, True, False]
    # 上限没有压低目标时，来源是系统目标：第 33、34 日的 0.3、0.6 就是映射(S)。
    assert [targets[number].source for number in (33, 34)] == [TargetSource.SYSTEM, TargetSource.SYSTEM]
    assert not state.cap


def test_cap_is_released_only_by_normal_state_with_all_channels_valid() -> None:
    capped = PolicyState(in_round=True, benchmark=1.0, exit_index=None, cap=True)
    for risk, valid, released in ((NORMAL, True, True), (NORMAL, False, False), (LEVEL1, True, False),
                                  (LEVEL2, True, False), (LEVEL1, False, False)):
        state, _ = step(capped, 40, risk, 1.0, valid)
        assert state.cap is not released
    # 解除之后不会恢复：之后再出现一级、二级或无效输入，U 仍为“无”。
    free = PolicyState(in_round=True, benchmark=1.0, exit_index=None, cap=False)
    state, targets = walk(free, 40, [(LEVEL2, 1.0, True), (LEVEL1, 1.0, False), (NORMAL, 1.0, False)])
    assert not state.cap and exposure(targets[43]) == pytest.approx(1.4)


def test_new_stop_voids_the_cap_and_next_reentry_sets_it_again() -> None:
    capped = PolicyState(in_round=True, benchmark=1.0, exit_index=None, cap=True)
    state, target = step(capped, 40, NORMAL, 0.95, valid=False)          # U 尚未解除时再次止损
    assert target.position is Position.CASH and target.source is TargetSource.STOP_CASH
    assert state == PolicyState(in_round=False, benchmark=None, exit_index=41, cap=False)   # U 作废
    state, targets = walk(state, 41, [(NORMAL, None, True)] * 11)        # 41 至 51：s + 10 = 51 检查
    assert all(targets[number].position is Position.CASH for number in range(42, 52))
    assert targets[52].position is Position.LEVEL1 and state.cap          # 下一次重入重新设 U = 一级


def test_policy_simulation_end_to_end_on_constructed_prices() -> None:
    """价格 → 净值 → 止损 → 冷却 → 重入 → 恢复杠杆，逐日联动。两资产同价，日期轴为第 1 至 16 日。

    第 1 日 100，第 2 日 97（满仓 1.4 倍：净值 0.958 ≤ 0.96，止损确认），第 3 日离场（s = 3），
    第 4 至 13 日冷却；第 13 日（s + 10）信号正常且完整 → 第 14 日重入 0.6；第 14 日信号正常 → 第 15 日 1.4。
    """
    prices = ["100", "97", "90", "91", "92", "93", "94", "95", "96", "97", "98", "99", "100", "100", "101", "102"]
    days = [numbered(number) for number in range(1, 17)]
    closes = {"SPX": [Decimal(value) for value in prices], "QQQ": [Decimal(value) for value in prices]}
    signals = [signal(number, NORMAL) for number in range(0, 16)]
    result = nav.simulate_policy(days, signals, nav.basket_returns(days, closes), POSITIONS, POLICY, TOLERANCE)
    exposures = [exposure(item) for item in result.targets]
    assert exposures == pytest.approx([1.4, 1.4, *[0.0] * 11, 0.6, 1.4, 1.4])
    wealth = result.nav.wealth
    assert wealth[1] == pytest.approx(1 - 1.4 * 0.03)                              # 0.958
    assert wealth[2] == pytest.approx(0.958 * (1 - 1.4 * 7 / 97))                  # 确认到执行之间的跌幅仍由组合承担
    assert all(value == wealth[2] for value in wealth[2:14])                       # 离场期间净值不变（现金收益为 0）
    assert wealth[14] == pytest.approx(wealth[2] * (1 + 0.6 * 0.01))               # 第 14 至 15 日：0.6 倍
    assert wealth[15] == pytest.approx(wealth[14] * (1 + 1.4 * 1 / 101))           # 第 15 至 16 日：1.4 倍
    assert [item.source for item in result.targets[2:14]] == [TargetSource.STOP_CASH, *[TargetSource.OUT_CASH] * 10,
                                                              TargetSource.REENTRY_CAP]
    assert [(item.day, item.position) for item in result.nav.executions] == [
        (item.day, item.position) for item in result.targets]


# ---------------------------------------------------------------------------
# 切换计数（登记第一节第 7 小节）
# ---------------------------------------------------------------------------


def planned(positions: list[Position]) -> list[PlannedTarget]:
    return [PlannedTarget(numbered(number), position, POSITIONS.weights(position).core,
                          POSITIONS.weights(position).leverage, TargetSource.SYSTEM, False)
            for number, position in enumerate(positions, start=1)]


def test_switch_count_and_rebalancing_size() -> None:
    targets = planned([Position.LEVEL2, Position.LEVEL2, Position.LEVEL1, Position.NORMAL, Position.NORMAL,
                       Position.LEVEL2, Position.LEVEL1, Position.LEVEL1])
    found = switches(targets, POSITIONS)
    # j₀ 以二级仓位初始建仓不计；之后：二级→一级、一级→正常、正常→二级（直接跨级计一次）、二级→一级。
    assert len(found) == 4
    assert [(item.day, item.before, item.after) for item in found] == [
        (numbered(3), Position.LEVEL2, Position.LEVEL1), (numbered(4), Position.LEVEL1, Position.NORMAL),
        (numbered(6), Position.NORMAL, Position.LEVEL2), (numbered(7), Position.LEVEL2, Position.LEVEL1)]
    assert [item.stages for item in found] == [1, 1, 2, 1]                    # 直接跨级计两个阶段
    assert [item.exposure_change for item in found] == pytest.approx([0.3, 0.8, -1.1, 0.3])


def test_initial_position_is_not_a_switch_and_unchanged_targets_count_nothing() -> None:
    for position in Position:
        assert switches(planned([position]), POSITIONS) == ()
        assert switches(planned([position] * 5), POSITIONS) == ()


def test_switches_involving_cash_have_no_stage_count() -> None:
    found = switches(planned([Position.NORMAL, Position.CASH, Position.CASH, Position.LEVEL1, Position.NORMAL]),
                     POSITIONS)
    assert [(item.before, item.after, item.stages) for item in found] == [
        (Position.NORMAL, Position.CASH, None), (Position.CASH, Position.LEVEL1, None),
        (Position.LEVEL1, Position.NORMAL, 1)]
    assert [item.exposure_change for item in found] == pytest.approx([-1.4, 0.6, 0.8])
