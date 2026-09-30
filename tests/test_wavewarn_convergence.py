"""用逐日手算的三态路径核对收敛时点。"""

import datetime as dt

from market_risk.wavewarn.channels import ChannelPredicate
from market_risk.wavewarn.convergence import channel_convergence, loss_start, system_convergence
from market_risk.wavewarn.state_machine import ReadyInputs


def _days(count: int) -> list[dt.date]:
    return [dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(count)]


def _ready(count: int) -> list[ReadyInputs]:
    return [ReadyInputs(10, 10, True, True, True, True, True, True) for _ in range(count)]


def test_channel_convergence_t0_is_snapshot_and_next_day_updates() -> None:
    days = _days(12)
    # 第1天：激活→未武装，未武装→已武装，仍有两态；第2天：未武装→已武装，三态一致。
    predicates = [ChannelPredicate(False, True, True) for _ in days]
    result = channel_convergence(days, predicates, days[0])
    assert result.date == days[2]
    assert {trace[0].status for trace in result.traces.values()} == {"active", "armed", "unarmed"}


def test_system_convergence_starts_five_days_after_channels_and_steps_down_one_level() -> None:
    days = _days(16)
    predicates = [ChannelPredicate(False, True, True) for _ in days]
    result = system_convergence(days, {"B": ("黄", predicates)}, _ready(len(days)), days[0], 3, "E1")
    # 通道第2天收敛，第7天系统三态快照；第8天红→黄、黄→绿，第9天黄→绿。
    assert result.channel_dates == {"B": days[2]}
    assert result.system_start == days[7]
    assert result.system_date == days[9]
    assert result.traces["红"][:3] == ("红", "黄", "绿")


def test_p0_fixed_guard_does_not_require_breadth_repair() -> None:
    days = _days(16)
    predicates = [ChannelPredicate(False, True, True) for _ in days]
    inputs = [ReadyInputs(10, 10, False, False, True, True, True, True) for _ in days]
    p0 = system_convergence(days, {"P": ("黄", predicates)}, inputs, days[0], 3, "P0")
    p1 = system_convergence(days, {"P": ("黄", predicates)}, inputs, days[0], 3, "E2")
    # 两者通道同在第2天收敛；P0 的 MA50 护栏成立，P1 的广度修复护栏始终为假。
    assert p0.system_date == days[9]
    assert p1.system_date is None


def test_old_active_breadth_can_delay_loss_start_beyond_t0_plus_63() -> None:
    days = _days(75)
    predicates = [ChannelPredicate(False, False, True) for _ in days]
    predicates[65] = ChannelPredicate(False, True, True)
    result = channel_convergence(days, predicates, days[0])
    # 激活路径到第65天才退出；第66天它重武装，三条路径才同为已武装。
    assert result.date == days[66]
    assert loss_start(days, days[0], [days[71]]) == days[71]
