"""阶段四登记参数（registered_v20）的纯算法测试（阶段四字段级设计稿第一节；M2 第一部分指令第二节第 1 小节第 4 条）。

期望值逐项按登记原文写在本测试内，并与 tests/wavewarn_v20_helpers.py 的独立期望值比较；不读任何文件。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

from wavewarn_v20_helpers import POLICY, POSITIONS, TOLERANCE, WINDOWS

from market_risk.wavewarn_v20 import registered_v20
from market_risk.wavewarn_v20.channels import ChannelState
from market_risk.wavewarn_v20.convergence import Candidate
from market_risk.wavewarn_v20.execution import PolicyParameters, PositionMap, Weights
from market_risk.wavewarn_v20.inputs import InputWindows
from market_risk.wavewarn_v20.labels_r2 import R2Thresholds
from market_risk.wavewarn_v20.r2 import R2Rule
from market_risk.wavewarn_v20.research_run import Continuity, Purpose, RunParameters, SegmentSpec, WindowSpec
from market_risk.wavewarn_v20.state_machine import Risk, SystemState

D = dt.date


def test_new_values_equal_the_registered_text_and_the_independent_helpers() -> None:
    """设计稿第一节表中“新写一次”的五项：登记原文的数值，并与 helpers 的独立期望值相等。"""
    assert registered_v20.WINDOWS == InputWindows(high=63, low_prior=19, low_minimum=15, average=200) == WINDOWS
    assert registered_v20.POSITIONS == PositionMap(Weights(0.6, 0.4), Weights(0.6, 0.0), Weights(0.3, 0.0), 2.0) \
        == POSITIONS
    assert registered_v20.POLICY == PolicyParameters(stop_ratio=0.96, cooldown=10) == POLICY
    assert registered_v20.R2_THRESHOLDS == R2Thresholds(Decimal("0.95"), Decimal("1.05"), Decimal("0.97"))
    assert registered_v20.R2_RULE == R2Rule(3, 5, 20)
    assert registered_v20.TOLERANCE == 1e-10 == TOLERANCE


def test_reused_constants_are_the_existing_objects() -> None:
    """已有常量导入复用（同一对象），不复制数值；值与登记原文相同。"""
    from market_risk.wavewarn_v20 import convergence, provenance_v20, research_run

    assert registered_v20.CANDIDATES is research_run.REGISTERED_CANDIDATES
    assert registered_v20.HISTORIES is provenance_v20.REGISTERED_FIRST_DAY
    assert registered_v20.INITIAL.channels is convergence.REGISTERED_CHANNELS
    assert registered_v20.INITIAL.system is convergence.REGISTERED_SYSTEM
    assert registered_v20.INITIAL.reference is convergence.REGISTERED_REFERENCE
    expected = tuple(Candidate(k, Decimal(theta), h) for k in (3, 5, 10) for theta in ("0.015", "0.02", "0.025")
                     for h in (1, 3, 5))
    assert registered_v20.CANDIDATES == expected and len(expected) == 27
    assert dict(registered_v20.HISTORIES) == {"SPX": D(1990, 1, 2), "QQQ": D(1999, 3, 10)}
    assert registered_v20.INITIAL.system == SystemState(Risk.NORMAL, 0, 0)
    assert registered_v20.INITIAL.reference is Risk.NORMAL
    assert {getattr(registered_v20.INITIAL.channels, field.name) for field in
            dataclasses.fields(registered_v20.INITIAL.channels)} == {ChannelState.ARMED}


def test_other_registered_values() -> None:
    assert (registered_v20.OFFSET, registered_v20.R1_RATIO, registered_v20.DIAGNOSTICS) == (63, 0.5, True)
    assert registered_v20.CUTOFF == D(2016, 12, 30)
    assert registered_v20.PURPOSE is Purpose.DEVELOPMENT
    assert registered_v20.CONTINUITY is Continuity.COMPLETE_TRADING_AXIS


def test_segments_and_bears_are_written_as_registered() -> None:
    """设计稿第一节第 2 小节：分段四项（边界 (start, end]）与两次熊市（P、Tr）。"""
    assert [(item.name, item.start, item.end) for item in registered_v20.SEGMENTS] == [
        ("1999—2009", D(1998, 12, 31), D(2009, 12, 31)), ("2010—2016", D(2009, 12, 31), D(2016, 12, 30)),
        ("熊市一", D(2000, 3, 24), D(2002, 10, 9)), ("熊市二", D(2007, 10, 9), D(2009, 3, 9))]
    assert registered_v20.BEARS == (("熊市一", D(2000, 3, 24), D(2002, 10, 9)),
                                    ("熊市二", D(2007, 10, 9), D(2009, 3, 9)))
    assert all(isinstance(item, SegmentSpec) and item.basis for item in registered_v20.SEGMENTS)
    assert (registered_v20.ENVIRONMENT_ASSET, registered_v20.ENVIRONMENT_UP, registered_v20.ENVIRONMENT_DOWN) == (
        "SPX", Decimal("0.10"), Decimal("-0.10"))


def test_assembled_parameters_and_window() -> None:
    parameters = registered_v20.run_parameters()
    assert parameters == RunParameters(WINDOWS, POSITIONS, POLICY, R2Thresholds(Decimal("0.95"), Decimal("1.05"),
                                       Decimal("0.97")), R2Rule(3, 5, 20), 63, 1e-10, 0.5, registered_v20.SEGMENTS,
                                       True)
    spec = registered_v20.window_spec()
    assert isinstance(spec, WindowSpec)
    assert (spec.purpose, spec.first_return_day, spec.last_day, dict(spec.histories), spec.continuity) == (
        Purpose.DEVELOPMENT, None, D(2016, 12, 30), {"SPX": D(1990, 1, 2), "QQQ": D(1999, 3, 10)},
        Continuity.COMPLETE_TRADING_AXIS)
    assert spec.initial == registered_v20.INITIAL


def test_no_override_parameters_are_offered() -> None:
    """不提供任何覆盖参数：组装函数没有参数；模块不读环境变量与命令行。"""
    import inspect

    for name in ("run_parameters", "window_spec", "describe"):
        assert list(inspect.signature(getattr(registered_v20, name)).parameters) == []
    source = inspect.getsource(registered_v20)
    assert "os.environ" not in source and "sys.argv" not in source and "getenv" not in source
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if line.split()[1].split(".")[0] in ("os", "sys", "argparse")]


def test_describe_lists_every_registered_value() -> None:
    described = registered_v20.describe()
    assert list(described) == ["candidates", "initial_states", "input_windows", "positions", "policy",
                               "r2_thresholds", "r2_rule", "offset", "tolerance", "r1_ratio", "diagnostics", "cutoff",
                               "purpose", "first_return_day", "continuity", "histories", "segments", "bears",
                               "environment"]
    assert len(described["candidates"]) == 27 and described["candidates"][0] == {"order": 0, "K": 3,
                                                                                 "theta_P": "0.015", "h": 1}
    assert described["positions"] == {"normal": [0.6, 0.4], "level1": [0.6, 0.0], "level2": [0.3, 0.0],
                                      "lambda": 2.0}
    assert described["histories"] == {"SPX": "1990-01-02", "QQQ": "1999-03-10"}
    assert described["cutoff"] == "2016-12-30" and described["first_return_day"] is None
