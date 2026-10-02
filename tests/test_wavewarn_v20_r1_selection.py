"""v2.0 的 R1 与回撤、选择程序与预定出口（登记第五节）：只用构造数据。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from market_risk.wavewarn_v20.convergence import Candidate
from market_risk.wavewarn_v20.r1 import R1Error, max_drawdown, r1_result, segment_drawdown_ratio
from market_risk.wavewarn_v20.selection import (
    CandidateRecord,
    Outcome,
    SelectionError,
    registered_candidates,
    select,
)

HALF = 0.5
TOLERANCE = 1e-10
KS, THETAS, HS = (3, 5, 10), (Decimal("0.015"), Decimal("0.02"), Decimal("0.025")), (1, 3, 5)


# ---------------------------------------------------------------------------
# R1 与回撤
# ---------------------------------------------------------------------------


def test_drawdown_includes_the_starting_point() -> None:
    # 净值从起点 1 直接下跌到 0.9，再回到 0.95：最大回撤是相对起点的 10%，起点本身算作高点。
    assert max_drawdown([1.0, 0.9, 0.95]) == pytest.approx(0.10)
    assert max_drawdown([1.0, 1.2, 0.9, 1.5, 1.35]) == pytest.approx(0.25)      # 1.2 → 0.9
    assert max_drawdown([1.0, 1.1, 1.2]) == 0.0
    assert max_drawdown([1.0]) == 0.0


def test_r1_is_satisfied_at_exactly_half() -> None:
    hold = [1.0, 0.5, 0.75]                      # 一直持有的最大回撤 50%
    exact = r1_result([1.0, 0.75, 0.9], hold, HALF)           # 信号模拟 25%：恰为一半
    assert (exact.computable, exact.signal_drawdown, exact.hold_drawdown, exact.satisfied) == (True, 0.25, 0.5, True)
    assert r1_result([1.0, 0.7499, 0.9], hold, HALF).satisfied is False        # 略超过一半
    assert r1_result([1.0, 0.7501, 0.9], hold, HALF).satisfied is True


def test_r1_cannot_be_computed_without_net_value() -> None:
    for signal, hold in ((None, [1.0, 0.9]), ([1.0, 0.9], None), (None, None)):
        result = r1_result(signal, hold, HALF)
        assert (result.computable, result.satisfied, result.signal_drawdown, result.hold_drawdown) == (
            False, None, None, None)
    with pytest.raises(R1Error, match="1 开头"):
        r1_result([0.9, 0.8], [1.0, 0.9], HALF)
    with pytest.raises(R1Error):
        r1_result([1.0, float("nan")], [1.0, 0.9], HALF)


def test_segment_drawdown_ratio_rebuilds_from_the_segment_start() -> None:
    signal = [1.0, 1.2, 1.08, 1.3, 1.17, 1.4]
    hold = [1.0, 1.5, 1.2, 1.6, 1.28, 1.7]
    # 段 [3, 5]：以段首（1.3、1.6）为起点；信号 1.3 → 1.17 为 10%，一直持有 1.6 → 1.28 为 20%，比值 0.5。
    assert segment_drawdown_ratio(signal, hold, 3, 5) == pytest.approx(0.5)
    # 段 [2, 3]：段内只涨不跌；一直持有的回撤为 0 → “无定义”。段首之前的高点不带入段内。
    assert segment_drawdown_ratio(signal, hold, 2, 3) is None
    assert segment_drawdown_ratio(signal, hold, 0, 5) == pytest.approx(0.10 / 0.20)
    with pytest.raises(R1Error):
        segment_drawdown_ratio(signal, hold, 4, 9)


# ---------------------------------------------------------------------------
# 选择程序
# ---------------------------------------------------------------------------

CANDIDATES = registered_candidates(KS, THETAS, HS)


def records(**overrides: dict) -> list[CandidateRecord]:
    """27 组全部可行的记录：ln W_末 = 0.5，切换 10 次；overrides 按登记序号改写个别字段。"""
    result = []
    for order, candidate in enumerate(CANDIDATES):
        fields = {"failed": False, "r1": True, "r2": {"SPX": True, "QQQ": True}, "log_wealth": 0.5, "switches": 10}
        fields.update(overrides.get(f"n{order}", {}))
        result.append(CandidateRecord(candidate, order, **fields))
    return result


def test_registered_order_is_generated_from_the_parameter_sets() -> None:
    assert len(CANDIDATES) == 27 == len(set(CANDIDATES))
    assert CANDIDATES[0] == Candidate(3, Decimal("0.015"), 1)
    assert CANDIDATES[1] == Candidate(3, Decimal("0.015"), 3)             # 最内层是 h
    assert CANDIDATES[3] == Candidate(3, Decimal("0.02"), 1)              # 其次是 θ_P
    assert CANDIDATES[9] == Candidate(5, Decimal("0.015"), 1)             # 最外层是 K
    assert CANDIDATES[26] == Candidate(10, Decimal("0.025"), 5)
    assert registered_candidates((7,), (Decimal("0.01"),), (2, 4)) == (Candidate(7, Decimal("0.01"), 2),
                                                                       Candidate(7, Decimal("0.01"), 4))


def test_tie_group_is_relative_to_the_maximum_not_pairwise() -> None:
    """非传递的并列：a、b、c 两两相差 0.6e-10，a 与 c 相差 1.2e-10。并列组只含与最大值相差不超过容差的 a、b。"""
    a, b, c = 1.0, 1.0 - 0.6e-10, 1.0 - 1.2e-10
    result = select(records(n4={"log_wealth": a, "switches": 9}, n8={"log_wealth": b, "switches": 7},
                            n12={"log_wealth": c, "switches": 1}), False, TOLERANCE)
    assert result.outcome is Outcome.SELECTED and result.maximum == a
    assert result.tied == (CANDIDATES[4], CANDIDATES[8])           # c 不在并列组里，尽管它与 b 只差 0.6e-10
    assert result.selected == CANDIDATES[8]                        # 并列组内切换次数少者优先；c 切换最少也不入选
    assert len(result.feasible) == 27 and len(result.records) == 27


def test_tie_break_by_switches_then_registered_order() -> None:
    by_switches = select(records(n20={"switches": 3}, n5={"switches": 4}), False, TOLERANCE)
    assert by_switches.selected == CANDIDATES[20] and len(by_switches.tied) == 27
    by_order = select(records(n20={"switches": 3}, n5={"switches": 3}), False, TOLERANCE)
    assert by_order.selected == CANDIDATES[5]                       # 切换次数相同：登记序号靠前者
    clear = select(records(n17={"log_wealth": 0.7, "switches": 99}), False, TOLERANCE)
    assert clear.selected == CANDIDATES[17] and clear.tied == (CANDIDATES[17],)      # 不并列时只看 ln W_末


def test_r1_and_r2_cannot_compensate_for_each_other() -> None:
    result = select(records(n2={"log_wealth": 9.0, "r1": False},                               # R1 不通过
                            n3={"log_wealth": 8.0, "r2": {"SPX": True, "QQQ": False}},         # 一个资产的 R2 不通过
                            n4={"log_wealth": 0.6}), False, TOLERANCE)
    assert result.selected == CANDIDATES[4]
    assert CANDIDATES[2] not in result.feasible and CANDIDATES[3] not in result.feasible
    assert len(result.feasible) == 25


def test_exit_no_feasible_candidate() -> None:
    nothing = {f"n{order}": {"r1": False} for order in range(27)}
    result = select(records(**nothing), False, TOLERANCE)
    assert (result.outcome, result.selected, result.feasible, result.tied) == (Outcome.NO_FEASIBLE, None, (), ())
    assert len(result.records) == 27                                # 仍输出全部 27 组的记录


def test_exit_unevaluable_when_any_value_cannot_be_computed() -> None:
    for broken in ({"r1": None}, {"log_wealth": None}, {"r2": {"SPX": True, "QQQ": None}}):
        result = select(records(n13=broken), False, TOLERANCE)
        assert (result.outcome, result.selected) == (Outcome.UNEVALUABLE, None)       # 不在其余组中选择
        assert len(result.records) == 27


def test_exit_failed_and_priority_of_exits() -> None:
    failed = select(records(n7={"failed": True}), False, TOLERANCE)
    assert (failed.outcome, failed.selected) == (Outcome.FAILED, None)
    reference = select(records(), True, TOLERANCE)                   # 主参照失败：整体停止
    assert (reference.outcome, reference.selected) == (Outcome.FAILED, None) and len(reference.records) == 27
    # 优先级：计算失败 > 缺值无法评价 > 无合格候选。
    everything = {f"n{order}": {"r1": False} for order in range(27)}
    everything["n1"] = {"r1": None}
    assert select(records(**everything), False, TOLERANCE).outcome is Outcome.UNEVALUABLE
    everything["n2"] = {"failed": True, "r1": None, "log_wealth": None, "switches": None}
    assert select(records(**everything), False, TOLERANCE).outcome is Outcome.FAILED
    assert select(records(**everything), True, TOLERANCE).outcome is Outcome.FAILED


def test_records_must_follow_the_registered_order() -> None:
    shuffled = records()
    shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
    with pytest.raises(SelectionError):
        select(shuffled, False, TOLERANCE)
    with pytest.raises(SelectionError):
        select([], False, TOLERANCE)
