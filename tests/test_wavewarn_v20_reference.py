"""v2.0 主参照“200 日均线二级版”（登记第六节第 1 小节）及其收敛（第四节第 2 小节）：只用构造数据。"""

from __future__ import annotations

import inspect

import pytest
from wavewarn_v20_helpers import POSITIONS, WINDOWS, numbered, signal, trend

from market_risk.wavewarn_v20.convergence import (
    REGISTERED_REFERENCE,
    ConvergenceError,
    common_start_index,
    reference_convergence,
    reference_initial_states,
)
from market_risk.wavewarn_v20.execution import Position, signal_targets
from market_risk.wavewarn_v20.reference import next_reference, reference_level, run_reference
from market_risk.wavewarn_v20.state_machine import Risk, StateError

NORMAL, LEVEL1, LEVEL2 = Risk.NORMAL, Risk.LEVEL1, Risk.LEVEL2
BELOW, ABOVE, EQUAL, INCOMPLETE = trend("98", "99"), trend("100", "99"), trend("99", "99"), trend("98", None)


@pytest.mark.parametrize(("previous", "level", "expected"), [
    (NORMAL, None, NORMAL), (LEVEL1, None, LEVEL1), (LEVEL2, None, LEVEL2),     # 第 1 行：均线不完整，保持
    (NORMAL, 2, LEVEL2), (LEVEL1, 2, LEVEL2), (LEVEL2, 2, LEVEL2),              # 第 2 行：L_参照 = 2
    (LEVEL2, 0, LEVEL1),                                                         # 第 3 行
    (LEVEL1, 0, NORMAL),                                                         # 第 4 行
    (NORMAL, 0, NORMAL),                                                         # 第 5 行
])
def test_reference_state_table(previous: Risk, level: int | None, expected: Risk) -> None:
    assert next_reference(previous, level) is expected


def test_reference_level() -> None:
    assert reference_level(BELOW, WINDOWS.average) == 2
    assert reference_level(ABOVE, WINDOWS.average) == 0
    assert reference_level(EQUAL, WINDOWS.average) == 0             # 不低于均线
    assert reference_level(INCOMPLETE, WINDOWS.average) is None     # 均线不完整：无定义
    assert reference_level(trend(None, None), WINDOWS.average) is None
    with pytest.raises(StateError):
        next_reference(NORMAL, 1)


def test_reference_keeps_state_while_average_is_incomplete() -> None:
    for initial in Risk:
        path = run_reference(initial, [INCOMPLETE] * 5, WINDOWS.average)
        assert [item.risk for item in path] == [initial] * 5       # 不升级也不降级
        assert all(item.level is None and not item.complete for item in path)
    # 二级途中遇到不完整：停在原处，恢复完整后继续逐级恢复。
    path = run_reference(NORMAL, [BELOW, INCOMPLETE, INCOMPLETE, ABOVE, INCOMPLETE, ABOVE], WINDOWS.average)
    assert [item.risk for item in path] == [LEVEL2, LEVEL2, LEVEL2, LEVEL1, LEVEL1, NORMAL]


def test_reference_recovers_core_then_leverage_over_two_execution_days() -> None:
    """收复均线后第一个执行日恢复核心、第二个执行日恢复杠杆；执行层直接映射。"""
    inputs = [trend(close, average, numbered(number)) for number, (close, average) in enumerate(
        [("100", "99"), ("98", "99"), ("98", "99"), ("100", "99"), ("100", "99"), ("100", "99")])]
    path = run_reference(NORMAL, inputs, WINDOWS.average)
    assert [item.risk for item in path] == [NORMAL, LEVEL2, LEVEL2, LEVEL1, NORMAL, NORMAL]
    signals = [signal(number, item.risk) for number, item in enumerate(path)]
    targets = signal_targets([numbered(number) for number in range(1, 7)], signals, POSITIONS)
    assert [item.position for item in targets] == [Position.NORMAL, Position.LEVEL2, Position.LEVEL2,
                                                   Position.LEVEL1, Position.NORMAL, Position.NORMAL]
    exposures = [POSITIONS.exposure(item.position) for item in targets]
    assert exposures == pytest.approx([1.4, 0.3, 0.3, 0.6, 1.4, 1.4])
    assert (targets[3].core, targets[3].leverage) == (0.6, 0.0)      # 第一个执行日：核心恢复，杠杆为 0
    assert (targets[4].core, targets[4].leverage) == (0.6, 0.4)      # 第二个执行日：杠杆恢复


def test_reference_does_not_use_h() -> None:
    for function in (next_reference, run_reference, reference_level, reference_convergence):
        assert "h" not in inspect.signature(function).parameters


def test_reference_convergence_enumerates_three_initial_states() -> None:
    assert set(reference_initial_states()) == set(Risk) and len(reference_initial_states()) == 3
    assert REGISTERED_REFERENCE is NORMAL
    # 均线不完整时三种初始值各自保持；第一个低于均线的完整日，三者同为二级。
    assert reference_convergence([INCOMPLETE, INCOMPLETE, BELOW, ABOVE], WINDOWS.average) == 2
    # 一直不低于均线：从二级出发要两个完整日才回到正常（位置 2、3），此时三者相同。
    assert reference_convergence([INCOMPLETE, ABOVE, ABOVE, ABOVE], WINDOWS.average) == 2
    assert reference_convergence([ABOVE, INCOMPLETE, ABOVE], WINDOWS.average) == 2
    with pytest.raises(ConvergenceError, match="主参照"):
        reference_convergence([INCOMPLETE] * 20, WINDOWS.average)
    with pytest.raises(ConvergenceError, match="主参照"):
        reference_convergence([ABOVE], WINDOWS.average)


def test_reference_convergence_day_takes_part_in_the_common_start() -> None:
    # κ_全 取全部候选与主参照的最大值：主参照最晚时，j₀ 由它决定。
    candidates, reference = [120, 150], 190
    assert common_start_index(100, [*candidates, reference], 63, 500) == 191
    assert common_start_index(100, candidates, 63, 500) == 163
