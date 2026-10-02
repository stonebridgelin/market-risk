"""v2.0 两级风险状态与恢复计数（登记第一节第 3 至 5 小节；第八节第 2 小节的 h = 3 构造边界）：只用构造数据。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from wavewarn_v20_helpers import asset, evidence, random_closes, run_model, trend

from market_risk.wavewarn_v20.channels import ChannelEvent, ChannelState, pullback_step
from market_risk.wavewarn_v20.convergence import Candidate
from market_risk.wavewarn_v20.inputs import NewLow
from market_risk.wavewarn_v20.state_machine import (
    Risk,
    StateError,
    SystemState,
    direct_level2_days,
    evidence_level,
    level1_release_checks,
    level2_release_checks,
    next_count,
    next_risk,
    system_step,
    transitions,
)

ACTIVE, ARMED, UNARMED = ChannelState.ACTIVE, ChannelState.ARMED, ChannelState.UNARMED
NORMAL, LEVEL1, LEVEL2 = Risk.NORMAL, Risk.LEVEL1, Risk.LEVEL2
K, H = 5, 3


# ---------------------------------------------------------------------------
# 证据级别与七行状态表（第一节第 3 小节）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("states", "expected"), [
    ({}, 0),
    ({"p_spx": ACTIVE}, 1), ({"p_qqq": ACTIVE}, 1), ({"mr": ACTIVE}, 1),
    ({"p_spx": ACTIVE, "p_qqq": ACTIVE, "mr": ACTIVE}, 1),              # MR、P 单独或同时激活只能使 L = 1
    ({"pr_spx": ACTIVE}, 2), ({"pr_qqq": ACTIVE}, 2),
    ({"pr_qqq": ACTIVE, "p_spx": ACTIVE, "mr": ACTIVE}, 2),
    ({"p_spx": UNARMED, "pr_qqq": UNARMED}, 0),
])
def test_evidence_level(states: dict[str, ChannelState], expected: int) -> None:
    assert evidence_level(evidence(**states)) == expected


@pytest.mark.parametrize(("previous", "level", "c1", "c2", "expected"), [
    (NORMAL, 2, 0, 0, LEVEL2), (LEVEL1, 2, 0, 0, LEVEL2), (LEVEL2, 2, 0, 0, LEVEL2),   # 第 1 行：任意，L = 2
    (NORMAL, 1, 0, 0, LEVEL1),                                                          # 第 2 行
    (NORMAL, 0, 0, 0, NORMAL), (NORMAL, 0, 3, 3, NORMAL),                               # 第 3 行
    (LEVEL1, 0, 3, 3, NORMAL), (LEVEL1, 0, 3, 0, NORMAL),                               # 第 4 行：c₁ ≥ h
    (LEVEL1, 0, 2, 3, LEVEL1), (LEVEL1, 1, 0, 3, LEVEL1),                               # 第 5 行
    (LEVEL2, 0, 0, 3, LEVEL1), (LEVEL2, 1, 0, 3, LEVEL1),                               # 第 6 行：c₂ ≥ h
    (LEVEL2, 0, 3, 2, LEVEL2), (LEVEL2, 1, 0, 0, LEVEL2),                               # 第 7 行
])
def test_seven_row_state_table(previous: Risk, level: int, c1: int, c2: int, expected: Risk) -> None:
    assert next_risk(previous, level, c1, c2, H) is expected


def test_recovery_is_at_most_one_level_per_signal_day() -> None:
    # 二级，且两个计数都已达到 h：当日只降到一级，次日才可能回到正常。
    assert next_risk(LEVEL2, 0, H, H, H) is LEVEL1
    quiet = evidence()
    first = system_step(SystemState(LEVEL2, H, H), quiet, K, H)
    second = system_step(first.state, quiet, K, H)
    assert (first.risk, second.risk) == (LEVEL1, NORMAL)


def test_state_and_parameters_outside_registered_range_are_rejected() -> None:
    with pytest.raises(StateError):
        system_step(SystemState(NORMAL, H + 1, 0), evidence(), K, H)
    with pytest.raises(StateError):
        system_step(SystemState(NORMAL, 0, 0), evidence(), K, 0)
    with pytest.raises(StateError, match="未武装未激活"):
        evidence(mr=UNARMED)


# ---------------------------------------------------------------------------
# 跨级条件：登记第一节第 4 小节例子 1 至 4（θ_P = 2%，2θ_P = 4%，K = 5）
# ---------------------------------------------------------------------------

THETA = Decimal("0.02")


def stepped(previous: ChannelState, today, deep: bool) -> ChannelState:
    return pullback_step(previous, today, K, 2 * THETA if deep else THETA).state


def test_registered_example_1_single_day_drop_goes_straight_to_level_2() -> None:
    before = asset("98.5", "100", q=7)                       # D_SPX = 1.5%：P、PR 都不进入
    assert stepped(ARMED, before, False) is ARMED and stepped(ARMED, before, True) is ARMED
    today = asset("95.7", "100", nl=NewLow.YES, q=0)         # D_SPX = 4.3%
    p, pr = stepped(ARMED, today, False), stepped(ARMED, today, True)
    assert (p, pr) == (ACTIVE, ACTIVE)                       # P_SPX、PR_SPX 同日进入
    day = system_step(SystemState(NORMAL, 0, 0), evidence(spx=today, p_spx=p, pr_spx=pr), K, H)
    assert day.level == 2 and day.risk is LEVEL2
    assert direct_level2_days(NORMAL, [day]) == (day.day,)


def test_registered_example_2_new_low_rearms_and_enters() -> None:
    before = asset("95.5", "100", q=6)                       # 此前因 Q ≥ K 退出而未武装，D_SPX = 4.5%，NL = 0
    assert stepped(UNARMED, before, False) is UNARMED and stepped(UNARMED, before, True) is UNARMED
    today = asset("95.5", "100", nl=NewLow.YES, q=0)         # NL_SPX,d = 1
    steps = [pullback_step(UNARMED, today, K, threshold) for threshold in (THETA, 2 * THETA)]
    assert [item.state for item in steps] == [ACTIVE, ACTIVE]
    assert all(item.events == (ChannelEvent.REARM, ChannelEvent.ENTER) for item in steps)
    day = system_step(SystemState(NORMAL, 0, 0), evidence(spx=today, p_spx=ACTIVE, pr_spx=ACTIVE), K, H)
    assert day.level == 2 and day.risk is LEVEL2 and direct_level2_days(NORMAL, [day]) == (day.day,)


def test_registered_example_3_entry_on_incomplete_high_window() -> None:
    today = asset("95.9", "100", complete=False, q=2)        # QQQ 的 63 日窗口内有一日缺价；D̂ = 4.1% ≥ 4%
    pr = pullback_step(ARMED, today, K, 2 * THETA)
    assert pr.state is ACTIVE and not pr.valid
    day = system_step(SystemState(NORMAL, 0, 0),
                      evidence(qqq=today, p_qqq=stepped(ARMED, today, False), pr_qqq=pr.state), K, H)
    assert day.level == 2 and day.risk is LEVEL2 and direct_level2_days(NORMAL, [day]) == (day.day,)


def test_registered_example_4_level_2_reached_through_level_1_is_not_direct() -> None:
    today = asset("95.8", "100", nl=NewLow.YES, q=0)
    assert stepped(ACTIVE, today, False) is ACTIVE and stepped(ARMED, today, True) is ACTIVE
    day = system_step(SystemState(LEVEL1, 0, 0), evidence(spx=today, p_spx=ACTIVE, pr_spx=ACTIVE), K, H)
    assert day.level == 2 and day.risk is LEVEL2
    assert direct_level2_days(LEVEL1, [day]) == ()                       # 属“经一级再入二级”
    assert transitions(LEVEL1, [day]) == ((day.day, LEVEL1, LEVEL2),)


def test_direct_entry_to_level_2_iff_previous_normal_and_level_2_on_random_paths() -> None:
    """性质：第 d 日由正常直接进入二级 ⇔ S_{d−1} = 正常 且 L_d = 2；此时必有某个 PR 在当日新进入。"""
    direct_total = through_level1_total = 0
    for seed, missing, candidate in ((1, 0.0, Candidate(5, Decimal("0.02"), 3)),
                                     (2, 0.0, Candidate(3, Decimal("0.015"), 1)),
                                     (3, 0.0, Candidate(10, Decimal("0.025"), 5)),
                                     (4, 0.01, Candidate(5, Decimal("0.015"), 5)),      # 含缺价的两条路径
                                     (5, 0.01, Candidate(3, Decimal("0.025"), 3))):
        model = run_model(random_closes(seed, 900, missing=missing, warmup=300),
                          random_closes(seed + 100, 900, missing=missing, warmup=300), candidate)
        previous_risk = NORMAL
        previous_deep = (ARMED, ARMED)
        for item, proof in zip(model.system, model.evidence, strict=True):
            deep = (proof.pr_spx, proof.pr_qqq)
            level_is_2 = any(channel.state is ACTIVE for channel in deep)     # 由通道状态独立重算 L_d = 2
            direct = previous_risk is NORMAL and item.risk is LEVEL2
            assert direct == (previous_risk is NORMAL and level_is_2)
            if direct:
                direct_total += 1
                assert any(channel.state is ACTIVE and before is not ACTIVE and ChannelEvent.ENTER in channel.events
                           for channel, before in zip(deep, previous_deep, strict=True))
            through_level1_total += previous_risk is LEVEL1 and item.risk is LEVEL2
            previous_risk, previous_deep = item.risk, (proof.pr_spx.state, proof.pr_qqq.state)
        assert direct_level2_days(NORMAL, model.system) == tuple(
            day for day, before, after in transitions(NORMAL, model.system) if (before, after) == (NORMAL, LEVEL2))
    assert direct_total >= 10 and through_level1_total >= 10               # 两种路径都确实出现过


# ---------------------------------------------------------------------------
# 恢复确认：A₂ 四项、A₁ 六项与截断计数（第一节第 5 小节）
# ---------------------------------------------------------------------------


def test_level_2_release_checks_exactly_four_items() -> None:
    assert level2_release_checks(evidence(), K) == (True, True, True, True)
    cases = {
        0: evidence(qqq=asset(None, "100", complete=False, nl=NewLow.UNKNOWN, q=0)),     # C 不全
        1: evidence(spx=asset(complete=False)),                                           # H 不完整
        2: evidence(pr_qqq=ACTIVE),                                                       # PR 激活
        3: evidence(spx=asset(q=K - 1)),                                                  # Q < K
    }
    for index, case in cases.items():
        checks = level2_release_checks(case, K)
        assert not checks[index]
        if index in (2, 3):                       # 这两项可以单独为假；C 缺失必然同时使 H 不完整、Q 为 0
            assert [position for position, value in enumerate(checks) if not value] == [index]
    # A₂ 不检查 MR、200 日均线与 P。
    unchecked = evidence(ma=trend("100", None), p_spx=ACTIVE, p_qqq=ACTIVE, mr=ACTIVE)
    assert all(level2_release_checks(unchecked, K))


def test_level_1_release_checks_six_items() -> None:
    assert level1_release_checks(evidence(), K) == (True,) * 6
    assert level1_release_checks(evidence(), K)[:4] == level2_release_checks(evidence(), K)
    only = {
        2: evidence(pr_spx=ACTIVE),
        3: evidence(qqq=asset(q=K - 1)),
        4: evidence(mr=ACTIVE, ma=trend("100", "101")),                   # MR 激活
        5: evidence(p_qqq=ACTIVE),                                        # P 激活
    }
    for index, case in only.items():
        checks = level1_release_checks(case, K)
        assert [position for position, value in enumerate(checks) if not value] == [index]
    incomplete_average = level1_release_checks(evidence(ma=trend("100", None)), K)
    assert [position for position, value in enumerate(incomplete_average) if not value] == [4]   # 均线不完整
    assert not level1_release_checks(evidence(spx=asset(complete=False)), K)[1]
    assert not level1_release_checks(evidence(spx=asset(None, "100", complete=False, nl=NewLow.UNKNOWN, q=0),
                                              ma=trend(None, None)), K)[0]


def test_truncated_counter() -> None:
    assert [next_count(previous, True, 3) for previous in (0, 1, 2, 3)] == [1, 2, 3, 3]     # 截断在 h
    assert [next_count(previous, False, 3) for previous in (0, 1, 2, 3)] == [0, 0, 0, 0]
    assert next_count(0, True, 1) == 1 and next_count(1, True, 1) == 1


def test_h_equal_to_1_looks_at_the_current_day_only() -> None:
    quiet = evidence()
    assert system_step(SystemState(LEVEL1, 0, 0), quiet, K, 1).risk is NORMAL
    assert system_step(SystemState(LEVEL2, 0, 0), quiet, K, 1).risk is LEVEL1
    assert system_step(SystemState(LEVEL1, 1, 1), evidence(p_spx=ACTIVE), K, 1).risk is LEVEL1


def test_counters_do_not_depend_on_system_state() -> None:
    quiet = evidence()
    for risk in Risk:
        day = system_step(SystemState(risk, 1, 2), quiet, K, H)
        assert (day.a1, day.a2, day.c1, day.c2) == (True, True, 2, 3)


def test_registered_example_a_gap_in_high_window_blocks_release() -> None:
    """例 A：二级；QQQ 第 d−30 日缺价；两资产 PR 未激活，Q = 8。A₂ 假（H_QQQ 不完整），c₂ = 0，停在二级。"""
    case = evidence(spx=asset(q=8), qqq=asset(complete=False, q=8))
    day = system_step(SystemState(LEVEL2, 0, 2), case, K, H)
    assert (day.a2, day.a1, day.c2, day.risk) == (False, False, 0, LEVEL2)
    assert not case.pr_qqq.valid


def test_registered_example_b_gap_in_average_window_allows_level_1_only() -> None:
    """例 B：二级；SPX 第 d−150 日缺价（只在 200 日窗口内）；已连续 3 日 A₂ 为真。降到一级；c₁ = 0。"""
    case = evidence(ma=trend("100", None))
    day = system_step(SystemState(LEVEL2, 0, 2), case, K, H)           # 前两日 A₂ 已为真，当日为第 3 日
    assert (day.a2, day.a1, day.c2, day.c1, day.risk) == (True, False, 3, 0, LEVEL1)
    following = system_step(day.state, case, K, H)
    assert following.risk is LEVEL1 and following.c1 == 0               # 不能回到正常


def test_registered_example_c_q_below_k_blocks_release() -> None:
    """例 C：二级；数据完整；PR_QQQ 未激活但 Q_QQQ = 4。A₂ 假（第 4 项），c₂ = 0。"""
    day = system_step(SystemState(LEVEL2, 0, 2), evidence(qqq=asset(q=4)), K, H)
    assert (day.a2, day.a1, day.c2, day.risk) == (False, False, 0, LEVEL2)
    assert level2_release_checks(evidence(qqq=asset(q=4)), K) == (True, True, True, False)


def test_h_3_boundary_after_day_32_gap_counts_1_2_3_on_days_232_233_234() -> None:
    """登记第八节第 2 小节：第 32 日缺价始终未补齐、h = 3、A₁ 自第 232 日起每天为真，

    则 c₁ 在第 232、233、234 日依次为 1、2、3，S 最早在第 234 日降为正常。
    构造：SPX 第 30、31 日跌到均线以下（MR 进入，系统升到一级；跌幅 0.5%，P、PR 不进入），
    第 32 日缺价，第 33 日起回到 101 并保持；QQQ 全程 100。
    """
    warmup, length = 260, 260 + 240
    row = lambda number: warmup + number - 1                                    # noqa: E731
    spx: list[Decimal | None] = [Decimal("100")] * length
    spx[row(30)] = spx[row(31)] = Decimal("99.50")
    spx[row(32)] = None
    for number in range(33, 241):
        spx[row(number)] = Decimal("101")
    model = run_model(spx, [Decimal("100")] * length, Candidate(K, Decimal("0.02"), 3))

    def system(number: int):
        return model.system[model.position(row(number))]

    assert system(29).risk is NORMAL and system(30).risk is LEVEL1             # MR 进入，正常 → 一级
    assert model.paths.mr[model.position(row(30))].events == (ChannelEvent.ENTER,)
    assert all(not model.ma[row(number)].complete for number in range(32, 232))
    assert all(system(number).c1 == 0 and system(number).risk is LEVEL1 for number in range(30, 232))
    assert all(system(number).a2 for number in range(95, 235))                 # H 自第 95 日完整，A₂ 可以成立
    assert not system(94).a2
    assert [system(number).c1 for number in (232, 233, 234)] == [1, 2, 3]
    assert [system(number).risk for number in (231, 232, 233, 234)] == [LEVEL1, LEVEL1, LEVEL1, NORMAL]
    assert model.paths.mr[model.position(row(232))].events == (ChannelEvent.EXIT,)   # 第 232 日 MR 才恢复有效并退出
    assert transitions(NORMAL, model.system) == ((model.days[row(30)], NORMAL, LEVEL1),
                                                 (model.days[row(234)], LEVEL1, NORMAL))


def test_system_transition_dates_are_reported_separately_from_channel_event_dates() -> None:
    """同一份通道证据在不同 h 下可能触发升级，也可能只是维持原状态；通道事件日期不随 h 改变。"""
    spx, qqq = random_closes(31, 700), random_closes(32, 700)
    models = [run_model(spx, qqq, Candidate(5, Decimal("0.02"), h)) for h in (1, 3, 5)]
    assert models[0].paths == models[1].paths == models[2].paths
    changes = [transitions(NORMAL, model.system) for model in models]
    assert changes[0] != changes[2]
