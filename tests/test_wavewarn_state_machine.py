"""v1.2.1 灯色转换、静默窗口与数据状态的手推构造测试。"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from market_risk.wavewarn.channels import ChannelDay, ChannelPredicate, update_channel
from market_risk.wavewarn.state_machine import ReadyInputs, SystemMemory, ready_condition, step_system


def _inputs() -> ReadyInputs:
    return ReadyInputs(5, 5, True, True, True, True, True, True)


def _red_channel(day: dt.date, valid: bool = True, active: bool = False
                 ) -> dict[str, tuple[str, ChannelDay]]:
    return {"V": ("红", ChannelDay(day, "active" if active else "armed", valid, "构造输入"))}


def test_missing_day_resets_red_quiet_and_delays_downgrade_to_day_five() -> None:
    days = tuple(dt.date(2010, 1, 4) + dt.timedelta(days=i) for i in range(5))
    memory = SystemMemory("红")
    lights = []
    counts = []
    for index, day in enumerate(days):
        result = step_system(day, memory, _red_channel(day, valid=index != 1), _inputs(), 5, "E2")
        memory = result.memory
        lights.append(memory.light)
        counts.append(memory.quiet_red)
    # 第1日有效c=1，第2日VIX3M缺值c=0；第3/4/5日c=1/2/3，第5日才红降黄。
    assert counts == [1, 0, 1, 2, 3]
    assert lights == ["红", "红", "红", "红", "黄"]


def test_yellow_to_green_requires_five_valid_quiet_days_after_missing() -> None:
    days = tuple(dt.date(2010, 1, 4) + dt.timedelta(days=i) for i in range(6))
    memory = SystemMemory("黄")
    results = []
    for index, day in enumerate(days):
        result = step_system(day, memory, _red_channel(day, valid=index != 0), _inputs(), 5, "E1")
        memory = result.memory
        results.append(result)
    # 缺值日清零；其后连续5日均有效且未激活，第6日才黄转绿。
    assert [result.memory.quiet_all for result in results] == [0, 1, 2, 3, 4, 5]
    assert [result.memory.light for result in results] == ["黄"] * 5 + ["绿"]


def test_ready_e_versions_are_nested_and_missing_data_flag_is_independent() -> None:
    day = dt.date(2010, 1, 4)
    inputs = replace(_inputs(), above_ma50_qqq=False)
    assert ready_condition("E1", 5, 5, inputs) is True
    assert ready_condition("E2", 5, 5, inputs) is False
    assert ready_condition("E3", 5, 5, inputs) is False
    # 旧绿灯遇输入缺失仍维持绿，但并列的数据状态须为“沿用”。
    green = step_system(day, SystemMemory("绿"), _red_channel(day, valid=False),
                        replace(_inputs(), downgrade_inputs_valid=False), 5, "E1")
    assert (green.memory.light, green.data_status) == ("绿", "沿用")


def test_red_can_only_downgrade_one_level_per_day() -> None:
    day = dt.date(2010, 1, 4)
    result = step_system(day, SystemMemory("红", quiet_red=2, quiet_all=4),
                         _red_channel(day), _inputs(), 5, "E1")
    # quietRed从2到3、quietAll从4到5，即使Ready成立，红也只能转黄。
    assert result.ready is True
    assert result.memory.light == "黄"


def test_incremental_append_matches_full_replay_states_counts_and_reasons() -> None:
    days = tuple(dt.date(2010, 1, 4) + dt.timedelta(days=index) for index in range(6))
    predicates = (
        ChannelPredicate(True, False, False, evidence="V进入"),
        ChannelPredicate(None, True, None),
        ChannelPredicate(False, False, True),
        ChannelPredicate(False, False, False),
        ChannelPredicate(None, None, None),
        ChannelPredicate(False, False, False),
    )

    def replay(length: int) -> tuple[tuple[object, object], ...]:
        channel_status = "armed"
        memory = SystemMemory()
        rows = []
        for day, predicate in zip(days[:length], predicates[:length], strict=True):
            channel = update_channel(day, channel_status, predicate)
            system = step_system(day, memory, {"V": ("红", channel)}, _inputs(), 3, "E1")
            rows.append((channel, system))
            channel_status, memory = channel.status, system.memory
        return tuple(rows)

    full = replay(len(days))
    # 第0日进入红；第1日退出后静默1日，第3日累计到3才降黄；第4日缺值清零。
    assert [row[1].memory.light for row in full] == ["红", "红", "红", "黄", "黄", "黄"]
    assert [row[1].memory.quiet_red for row in full] == [0, 1, 2, 3, 0, 1]
    for length in range(1, len(days) + 1):
        # 每次仅追加一日重新运行，逐日通道、系统计数及中文原因与全量重放逐字相同。
        assert replay(length) == full[:length]
