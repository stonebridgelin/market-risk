"""v2.0 通道（登记第一节第 1 小节；第八节第 2 小节更新表与例子 E、F、G、MR 例子）：只用构造数据。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from wavewarn_v20_helpers import DAY, WINDOWS, asset, axis, trend

from market_risk.wavewarn_v20.channels import (
    PULLBACK_DOMAIN,
    TREND_DOMAIN,
    ChannelError,
    ChannelEvent,
    ChannelState,
    event_dates,
    pullback_step,
    pullback_valid,
    run_pullback,
    run_trend,
    trend_step,
    trend_valid,
)
from market_risk.wavewarn_v20.inputs import NewLow, asset_days

ACTIVE, ARMED, UNARMED = ChannelState.ACTIVE, ChannelState.ARMED, ChannelState.UNARMED
K = 5
THETA = Decimal("0.025")


# ---------------------------------------------------------------------------
# MR 两状态（登记第一节第 1 小节“状态域”；第八节第 2 小节）
# ---------------------------------------------------------------------------


def test_trend_channel_domain_has_exactly_two_states() -> None:
    assert TREND_DOMAIN == {ACTIVE, ARMED}
    assert PULLBACK_DOMAIN == {ACTIVE, ARMED, UNARMED} == set(ChannelState)


@pytest.mark.parametrize("bad", [UNARMED, "激活", None, 1])
def test_trend_channel_rejects_any_third_state(bad: object) -> None:
    with pytest.raises(ChannelError, match="MR"):
        trend_step(bad, trend(), WINDOWS.average)            # type: ignore[arg-type]
    with pytest.raises(ChannelError, match="MR"):
        run_trend(bad, [trend()], WINDOWS.average)           # type: ignore[arg-type]


def test_pullback_channel_rejects_states_outside_its_domain() -> None:
    with pytest.raises(ChannelError):
        pullback_step("已武装未激活", asset(), K, THETA)     # type: ignore[arg-type]


def test_trend_channel_registered_example() -> None:
    """登记第八节第 2 小节 MR 构造例子：t0+1 均线不完整，t0+2 完整且 C ≥ MA，t0+3 完整且 C < MA。"""
    days = axis(3)
    inputs = [trend("100", None, days[0]), trend("100", "99", days[1]), trend("98", "99", days[2])]
    from_active = run_trend(ACTIVE, inputs, WINDOWS.average)
    from_armed = run_trend(ARMED, inputs, WINDOWS.average)
    assert [item.state for item in from_active] == [ACTIVE, ARMED, ACTIVE]
    assert [item.state for item in from_armed] == [ARMED, ARMED, ACTIVE]
    assert [item.valid for item in from_active] == [False, True, True]
    assert from_active[0].state != from_armed[0].state                  # t0+1：不相同
    assert [item.state for item in from_active[1:]] == [item.state for item in from_armed[1:]]   # t0+2 起相同
    assert from_active[1].events == (ChannelEvent.EXIT,) and from_armed[1].events == ()
    assert from_active[2].events == from_armed[2].events == (ChannelEvent.ENTER,)


@pytest.mark.parametrize("previous", [ACTIVE, ARMED])
@pytest.mark.parametrize(("close", "below"), [("98.99", True), ("99", False), ("99.01", False)])
def test_trend_channel_with_complete_inputs_is_active_iff_close_below_average(
        previous: ChannelState, close: str, below: bool) -> None:
    updated = trend_step(previous, trend(close, "99"), WINDOWS.average)
    assert updated.active is below
    assert len(updated.events) <= 1                                      # 同一日不会既退出又进入


def test_close_equal_to_average_does_not_activate_trend_channel() -> None:
    """门槛等号：C 恰好等于 MA 时 MR 不激活（进入条件是 C < MA，退出条件是 C ≥ MA）。"""
    assert trend_step(ARMED, trend("99", "99"), WINDOWS.average).state is ARMED
    exited = trend_step(ACTIVE, trend("99", "99"), WINDOWS.average)
    assert exited.state is ARMED and exited.events == (ChannelEvent.EXIT,)


@pytest.mark.parametrize("previous", [ACTIVE, ARMED])
@pytest.mark.parametrize("today", [trend("90", None), trend(None, None)])
def test_trend_channel_keeps_state_when_inputs_are_incomplete(previous: ChannelState, today) -> None:
    updated = trend_step(previous, today, WINDOWS.average)
    assert updated.state is previous and not updated.valid and updated.events == ()
    assert not trend_valid(today)


# ---------------------------------------------------------------------------
# P、PR 的更新表（退出 > 重新武装 > 进入）
# ---------------------------------------------------------------------------


def test_close_equal_to_threshold_enters() -> None:
    """门槛等号：C 恰好等于 (1 − θ)·H 时进入。θ = 2.5%，H = 100，(1 − θ)·H = 97.50。"""
    entered = pullback_step(ARMED, asset("97.50", "100", nl=NewLow.YES, q=0), K, THETA)
    assert entered.state is ACTIVE and entered.events == (ChannelEvent.ENTER,)
    assert pullback_step(ARMED, asset("97.51", "100"), K, THETA).state is ARMED


def test_exit_requires_valid_channel_and_q_at_least_k() -> None:
    assert pullback_step(ACTIVE, asset(q=5), K, THETA).state is UNARMED          # Q = K：退出
    assert pullback_step(ACTIVE, asset(q=4), K, THETA).state is ACTIVE           # Q < K：保持
    kept = pullback_step(ACTIVE, asset(complete=False, q=9), K, THETA)           # 无效：不退出
    assert kept.state is ACTIVE and not kept.valid and kept.events == ()
    missing = pullback_step(ACTIVE, asset(None, "100", complete=False, nl=NewLow.UNKNOWN, q=0), K, THETA)
    assert missing.state is ACTIVE and not missing.valid


def test_exit_ends_the_day_so_the_channel_cannot_reenter_on_the_same_day() -> None:
    # 回撤仍在门槛以上（97 ≤ 97.5），但当日先退出，转为未武装，当日结束。
    updated = pullback_step(ACTIVE, asset("97", "100", q=6), K, THETA)
    assert updated.state is UNARMED and updated.events == (ChannelEvent.EXIT,)


@pytest.mark.parametrize("nl", [NewLow.YES, NewLow.UNKNOWN])
def test_rearm_on_new_low_or_unknown_then_enter_on_the_same_day(nl: NewLow) -> None:
    updated = pullback_step(UNARMED, asset("97", "100", nl=nl, q=0), K, THETA)
    assert updated.state is ACTIVE and updated.events == (ChannelEvent.REARM, ChannelEvent.ENTER)
    quiet = pullback_step(UNARMED, asset("99", "100", nl=nl, q=0), K, THETA)
    assert quiet.state is ARMED and quiet.events == (ChannelEvent.REARM,)


def test_missing_close_rearms_but_cannot_enter() -> None:
    # 当日缺价：NL 未知 → 可重新武装（保守方向）；D 缺失 → 不得进入。
    today = asset(None, "100", complete=False, nl=NewLow.UNKNOWN, q=0)
    assert pullback_step(UNARMED, today, K, THETA).state is ARMED
    assert pullback_step(ARMED, today, K, THETA).state is ARMED


def test_rearm_when_entry_predicate_is_false_does_not_enter_that_day() -> None:
    updated = pullback_step(UNARMED, asset("99", "100", q=3), K, THETA)          # NL = 0，有效，D < θ
    assert updated.state is ARMED and updated.events == (ChannelEvent.REARM,)
    # NL = 0 且 D ≥ θ：进入谓词为真，不能武装，保持未武装。
    assert pullback_step(UNARMED, asset("97", "100", q=3), K, THETA).state is UNARMED


def test_registered_example_e_active_channel_does_not_exit_while_window_is_incomplete() -> None:
    """登记例子 E：P_SPX 前一日激活；SPX 第 d−40 日缺价；Q_SPX,d = 6。v2.0：通道无效，不退出、保持激活。"""
    length, d = 200, 190
    closes: list[Decimal | None] = [Decimal("100")] * length
    closes[d - 40] = None
    today = asset_days(axis(length), closes, WINDOWS)[d]
    # 缺价日已离开 20 日窗口，仍在 63 日窗口内。
    assert today.new_low is NewLow.NO and today.q >= 6 and not today.high_complete
    constructed = asset("100", "100", complete=False, q=6)
    for item in (today, constructed):
        updated = pullback_step(ACTIVE, item, K, THETA)
        assert updated.state is ACTIVE and not updated.valid and updated.events == ()
    # 旧实现口径下退出谓词 Q ≥ K 的输入齐全会退出；v2.0 的区别只在“通道当日无效”。
    assert pullback_step(ACTIVE, asset("100", "100", complete=True, q=6), K, THETA).state is UNARMED


def test_registered_example_f_incomplete_high_can_support_entry() -> None:
    """登记例子 F：同一缺价；P_SPX 已武装未激活；Ĥ = 100，C = 97.4。D̂ = 2.6% ≥ 2.5%，进入。"""
    today = asset("97.4", "100", complete=False, q=3)
    updated = pullback_step(ARMED, today, K, THETA)
    assert updated.state is ACTIVE and updated.events == (ChannelEvent.ENTER,) and not updated.valid
    assert not pullback_valid(today)


def test_registered_example_g_invalid_channel_cannot_rearm_on_small_drawdown() -> None:
    """登记例子 G：同一缺价；P_SPX 未武装；NL = 0；D̂ = 1.0%。通道无效，不能以“D < 门槛”武装。"""
    updated = pullback_step(UNARMED, asset("99", "100", complete=False, q=3), K, THETA)
    assert updated.state is UNARMED and updated.events == ()
    # 对照：同样的价格，窗口完整时可以武装。
    assert pullback_step(UNARMED, asset("99", "100", complete=True, q=3), K, THETA).state is ARMED


def test_deep_pullback_channel_uses_twice_the_threshold() -> None:
    today = asset("96", "100", nl=NewLow.YES, q=0)                    # D = 4%
    assert pullback_step(ARMED, today, K, THETA).state is ACTIVE       # P：4% ≥ 2.5%
    assert pullback_step(ARMED, today, K, 2 * THETA).state is ARMED    # PR：4% < 5%
    assert pullback_step(ARMED, asset("95", "100", nl=NewLow.YES, q=0), K, 2 * THETA).state is ACTIVE


@pytest.mark.parametrize("bad", [
    asset(None, "100", complete=True, nl=NewLow.UNKNOWN, q=0),      # 缺价却称 H 完整
    asset(None, "100", complete=False, nl=NewLow.NO, q=3),          # 缺价却给出 NL = 0
    asset("100", None, complete=False),                              # 有价却没有窗口最高价
    asset("100", "99"),                                              # 窗口最高价低于当日收盘价
    asset("100", "100", nl=NewLow.YES, q=2),                         # NL = 1 时 Q 必须为 0
    asset("100", "100", nl=NewLow.NO, q=0),                          # NL = 0 时 Q 至少为 1
])
def test_inputs_outside_the_registered_table_stop_with_an_error(bad) -> None:
    with pytest.raises(ChannelError):
        pullback_step(ARMED, bad, K, THETA)


def test_channel_events_depend_only_on_the_channel_inputs() -> None:
    days = axis(4)
    inputs = [asset("97", "100", nl=NewLow.YES, q=0, day=days[0]), asset("98", "100", q=1, day=days[1]),
              asset("99", "100", q=5, day=days[2]), asset("99.5", "100", q=6, day=days[3])]
    path = run_pullback(ARMED, inputs, K, THETA)
    assert [item.state for item in path] == [ACTIVE, ACTIVE, UNARMED, ARMED]
    assert event_dates(path) == ((days[0], ChannelEvent.ENTER), (days[2], ChannelEvent.EXIT),
                                 (days[3], ChannelEvent.REARM))
    assert path[0].day == days[0] != DAY
