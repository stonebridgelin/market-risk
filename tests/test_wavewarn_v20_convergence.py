"""v2.0 预热、收敛与共同起点（登记第四节）：只用构造数据。主参照的收敛在后续批次加入。"""

from __future__ import annotations

from decimal import Decimal
from itertools import product

import pytest
from wavewarn_v20_helpers import WINDOWS, asset, axis, evidence, random_closes, run_model, trend

from market_risk.wavewarn_v20.channels import (
    PULLBACK_DOMAIN,
    TREND_DOMAIN,
    ChannelState,
    run_pullback,
    run_trend,
)
from market_risk.wavewarn_v20.convergence import (
    REGISTERED_CHANNELS,
    REGISTERED_SYSTEM,
    Candidate,
    ConvergenceError,
    candidate_convergence,
    channel_initial_states,
    common_start_index,
    first_common_index,
    first_valid_index,
    pullback_convergence,
    system_convergence,
    system_initial_states,
    trend_convergence,
)
from market_risk.wavewarn_v20.inputs import NewLow, asset_days, trend_days
from market_risk.wavewarn_v20.state_machine import (
    ChannelInitial,
    Risk,
    SystemState,
    evidence_series,
    run_channels,
    run_system,
)

ACTIVE, ARMED, UNARMED = ChannelState.ACTIVE, ChannelState.ARMED, ChannelState.UNARMED
K = 5
THETA = Decimal("0.02")


# ---------------------------------------------------------------------------
# 枚举集合与状态域相等
# ---------------------------------------------------------------------------


def test_channel_enumeration_equals_the_state_domain() -> None:
    pullback, trending = channel_initial_states(PULLBACK_DOMAIN), channel_initial_states(TREND_DOMAIN)
    assert set(pullback) == PULLBACK_DOMAIN and len(pullback) == 3          # P、PR 各三种
    assert set(trending) == TREND_DOMAIN and len(trending) == 2             # MR 两种
    assert UNARMED not in trending


@pytest.mark.parametrize(("h", "count"), [(1, 12), (3, 48), (5, 108)])
def test_system_enumeration_has_3_times_h_plus_1_squared_states(h: int, count: int) -> None:
    states = system_initial_states(h)
    assert len(states) == len(set(states)) == count == 3 * (h + 1) ** 2
    assert set(states) == {SystemState(risk, c1, c2) for risk, c1, c2 in product(Risk, range(h + 1), range(h + 1))}


def test_registered_initial_snapshot() -> None:
    assert REGISTERED_CHANNELS == ChannelInitial(ARMED, ARMED, ARMED, ARMED, ARMED)
    assert REGISTERED_SYSTEM == SystemState(Risk.NORMAL, 0, 0)


# ---------------------------------------------------------------------------
# 完整状态逐项相同才算收敛
# ---------------------------------------------------------------------------


def test_same_risk_with_different_counters_is_not_convergence() -> None:
    same_risk_only = [[SystemState(Risk.NORMAL, 1, 1), SystemState(Risk.NORMAL, 2, 2), SystemState(Risk.NORMAL, 3, 3)],
                      [SystemState(Risk.NORMAL, 3, 3), SystemState(Risk.NORMAL, 3, 3), SystemState(Risk.NORMAL, 3, 3)]]
    assert first_common_index(same_risk_only) == 2          # 前两日风险状态相同但计数不同，不算收敛
    assert first_common_index([[SystemState(Risk.NORMAL, 0, 1)], [SystemState(Risk.NORMAL, 1, 0)]]) is None


def test_system_convergence_waits_for_counters_on_a_quiet_path() -> None:
    """全部日子 L = 0、A₁ 与 A₂ 为真，h = 3。

    手算：从（二级，c₂ = 0）出发最慢——c₂ 在第 1、2、3 日为 1、2、3，第 3 日降到一级；c₁ 此时已为 3，第 4 日回到正常。
    从正常出发的各组在第 1 日风险状态就相同，但计数到第 3 日才全部达到 3。所以系统收敛在第 4 日（位置 3）。
    """
    quiet = [evidence() for _ in range(8)]
    assert system_convergence(quiet, K, 3) == 3
    slow = run_system(SystemState(Risk.LEVEL2, 0, 0), quiet, K, 3)
    fast = run_system(SystemState(Risk.NORMAL, 3, 3), quiet, K, 3)
    low = run_system(SystemState(Risk.NORMAL, 0, 0), quiet, K, 3)
    assert [item.risk for item in slow[:4]] == [Risk.LEVEL2, Risk.LEVEL2, Risk.LEVEL1, Risk.NORMAL]
    assert [item.risk for item in low[:3]] == [item.risk for item in fast[:3]] == [Risk.NORMAL] * 3
    assert [item.state for item in low[:2]] != [item.state for item in fast[:2]]     # 风险状态相同、计数不同
    # h = 1 时同一条路径：二级第 1 日降到一级，第 2 日回到正常。
    assert system_convergence(quiet, K, 1) == 1


def test_identical_complete_state_stays_identical_forever() -> None:
    """永久同步：两次运行在某日完整状态逐项相同，此后每日都相同。"""
    model = run_model(random_closes(41, 800), random_closes(42, 800), Candidate(K, THETA, 3))
    after = slice(model.t0 + 1, None)
    for initial in product(channel_initial_states(PULLBACK_DOMAIN), repeat=2):
        other = run_channels(ChannelInitial(initial[0], initial[1], initial[1], initial[0], ACTIVE),
                             model.spx[after], model.qqq[after], model.ma[after], K, THETA, WINDOWS.average)
        proofs = evidence_series(model.spx[after], model.qqq[after], model.ma[after], other)
        system = run_system(SystemState(Risk.LEVEL2, 3, 0), proofs, K, 3)

        def complete(position: int, paths=other, states=system):
            return (paths.p_spx[position].state, paths.p_qqq[position].state, paths.pr_spx[position].state,
                    paths.pr_qqq[position].state, paths.mr[position].state, states[position].state)

        def registered(position: int):
            return (model.paths.p_spx[position].state, model.paths.p_qqq[position].state,
                    model.paths.pr_spx[position].state, model.paths.pr_qqq[position].state,
                    model.paths.mr[position].state, model.system[position].state)

        same = [complete(position) == registered(position) for position in range(len(system))]
        assert True in same
        first = same.index(True)
        assert all(same[first:])


# ---------------------------------------------------------------------------
# 通道收敛、t0 与不收敛
# ---------------------------------------------------------------------------


def test_t0_is_the_first_day_all_five_channels_are_valid() -> None:
    length = 320
    days = axis(length)
    spx: list[Decimal | None] = [Decimal("100")] * length
    qqq: list[Decimal | None] = [None] * 230 + [Decimal("50")] * (length - 230)      # QQQ 较晚才有价格
    spx_days, qqq_days = asset_days(days, spx, WINDOWS), asset_days(days, qqq, WINDOWS)
    # SPX 的均线自行号 199 完整；QQQ 的第 63 个收盘价在行号 230 + 62 = 292：t0 受 QQQ 的 63 个收盘价限制。
    assert first_valid_index(spx_days, qqq_days, trend_days(days, spx, WINDOWS)) == 292
    with pytest.raises(ConvergenceError, match="t0"):
        first_valid_index(spx_days[:250], qqq_days[:250], trend_days(days, spx, WINDOWS)[:250])


def test_trend_channel_converges_on_the_first_complete_day() -> None:
    """MR 从两种初始状态出发，在第一个输入完整的信号日结束时必然相同。"""
    inputs = [trend("100", None), trend("100", None), trend("100", "101"), trend("100", "99")]
    assert trend_convergence(inputs, WINDOWS.average) == 2
    assert [item.state for item in run_trend(ACTIVE, inputs, WINDOWS.average)][:2] == [ACTIVE, ACTIVE]
    assert [item.state for item in run_trend(ARMED, inputs, WINDOWS.average)][:2] == [ARMED, ARMED]


def test_pullback_channel_convergence_by_hand() -> None:
    """θ = 2%，K = 5。三种初始状态：激活、已武装未激活、未武装未激活。

    第 1 日 D = 1%、NL = 0、Q = 3：激活保持（Q < K）；已武装保持；未武装因 D < θ 武装。
    第 2 日 D = 3%、NL = 1：激活保持；其余两种都进入。三次运行自第 2 日（位置 1）起相同。
    """
    inputs = [asset("99", "100", q=3), asset("97", "100", nl=NewLow.YES, q=0)]
    runs = [[item.state for item in run_pullback(initial, inputs, K, THETA)] for initial in (ACTIVE, ARMED, UNARMED)]
    assert runs == [[ACTIVE, ACTIVE], [ARMED, ACTIVE], [ARMED, ACTIVE]]
    assert pullback_convergence(inputs, K, THETA, "P_SPX") == 1


def test_channel_that_never_converges_raises() -> None:
    # 全程缺价：激活的通道无法退出，未武装的只会武装而不能进入，三种初始状态永远不相同。
    missing = [asset(None, None, complete=False, nl=NewLow.UNKNOWN, q=0) for _ in range(30)]
    with pytest.raises(ConvergenceError, match="P_QQQ"):
        pullback_convergence(missing, K, THETA, "P_QQQ")
    with pytest.raises(ConvergenceError, match="MR"):
        trend_convergence([trend("100", None)] * 30, WINDOWS.average)
    with pytest.raises(ConvergenceError):
        pullback_convergence([], K, THETA, "P_SPX")


def test_system_that_never_converges_raises() -> None:
    # 全程 L = 0 且均线不完整：A₁ 恒为假，一级永远回不到正常，而正常保持正常。
    stuck = [evidence(ma=trend("100", None)) for _ in range(40)]
    with pytest.raises(ConvergenceError, match="系统"):
        system_convergence(stuck, K, 3)


# ---------------------------------------------------------------------------
# 共同起点 j₀ = max(t0 + 63, κ_全 + 1)
# ---------------------------------------------------------------------------


def test_common_start_formula() -> None:
    assert common_start_index(100, [120, 140], 63, 500) == 163          # t0 + 63 较晚
    assert common_start_index(100, [120, 180, 170], 63, 500) == 181      # κ_全 + 1 较晚
    assert common_start_index(100, [162], 63, 500) == 163                # 两者相等
    with pytest.raises(ConvergenceError):
        common_start_index(100, [], 63, 500)
    with pytest.raises(ConvergenceError, match="超出"):
        common_start_index(100, [499], 63, 500)


def test_all_enumerated_runs_agree_on_the_day_before_the_common_start() -> None:
    """验收测试：j₀ − 1 日，全部枚举运行（各通道的每种初始状态、系统层的每种初始值）的完整状态相同。"""
    length = 1000
    spx_closes, qqq_closes = random_closes(51, length), random_closes(52, length)
    candidates = [Candidate(k, theta, h) for k, theta, h in product((3, 10), (Decimal("0.015"), Decimal("0.025")),
                                                                    (1, 3, 5))]
    models = {candidate: run_model(spx_closes, qqq_closes, candidate) for candidate in candidates}
    first = models[candidates[0]]
    results = {candidate: candidate_convergence(first.spx, first.qqq, first.ma, first.t0, candidate, WINDOWS.average)
               for candidate in candidates}
    start = common_start_index(first.t0, [result.system_index for result in results.values()], 63, length)
    assert start == max(first.t0 + 63, max(result.system_index for result in results.values()) + 1)

    after = slice(first.t0 + 1, None)
    for candidate, result in results.items():
        model = models[candidate]
        assert result.kappa_channel == max(result.channel_indices.values()) < result.system_index < start
        target = model.position(start - 1)
        expected_channels = tuple(path[target].state for path in (
            model.paths.p_spx, model.paths.p_qqq, model.paths.pr_spx, model.paths.pr_qqq, model.paths.mr))
        # 通道层：五个通道的初始状态取遍各自的状态域。
        for p_spx, pr_qqq, mr in product(PULLBACK_DOMAIN, PULLBACK_DOMAIN, TREND_DOMAIN):
            paths = run_channels(ChannelInitial(p_spx, pr_qqq, p_spx, pr_qqq, mr), model.spx[after], model.qqq[after],
                                 model.ma[after], candidate.k, candidate.theta, WINDOWS.average)
            assert tuple(path[target].state for path in (
                paths.p_spx, paths.p_qqq, paths.pr_spx, paths.pr_qqq, paths.mr)) == expected_channels
        # 系统层：在 κ_通道 当日放入全部初始值，j₀ − 1 日的 (S, c₁, c₂) 与登记初始快照的运行相同。
        tail = model.evidence[model.position(result.kappa_channel) + 1:]
        offset = target - model.position(result.kappa_channel) - 1
        for initial in system_initial_states(candidate.h):
            assert run_system(initial, tail, candidate.k, candidate.h)[offset].state == model.system[target].state
