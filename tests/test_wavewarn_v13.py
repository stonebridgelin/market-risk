"""v1.3 修订的构造测试；期望值均由下列构造数据手算，推算过程写在各测试的注释里。

全部数据都是构造的，没有读取验证期或保留期数据，也没有运行验证期评价。
"""

from __future__ import annotations

import datetime as dt
import json
import random
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn.calibration import distribution, fixed_delay_reference
from market_risk.wavewarn.channels import ChannelDay, ChannelPredicate, update_channel
from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.config_v13 import FeasibilityLimits, load_v13_config, parse_v13_config
from market_risk.wavewarn.evaluation import (
    Candidate,
    CandidateStates,
    PreparedEvaluation,
    evaluate_candidate,
)
from market_risk.wavewarn.evaluation_v13 import v13_grid
from market_risk.wavewarn.extended_history import (
    crossing_events,
    p0_first_complete_day,
    truncate_inputs,
    window_events,
)
from market_risk.wavewarn.feasibility import (
    N_PRIME_MODEL,
    STOP_N_INFEASIBLE,
    STOP_NO_VERSION,
    AssetRebound,
    SelectionItem,
    feasibility,
    select,
)
from market_risk.wavewarn.features import asset_features
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.state_machine import ReadyInputs, SystemMemory, step_system
from market_risk.wavewarn.state_sequences import DiagnosticRow
from market_risk.wavewarn.timing import (
    caution_gap,
    ma200_signals,
    paired_timing_differences,
    reference_rows,
    timing_result,
)

ROOT = Path(__file__).resolve().parents[1]
HALF = Decimal("0.5")
NO_EVENTS: dict[str, tuple[ZZEvent, ...]] = {"SPX": (), "QQQ": ()}
LIMITS = FeasibilityLimits(Decimal("0.60"), Decimal("0.60"), Decimal("1.00"))
VERSIONS = ("E2", "X1", "X2")


def _near(actual: Decimal, expected: Decimal) -> bool:
    return abs(actual - expected) < Decimal("1e-20")


def _ln(numerator: str, denominator: str) -> Decimal:
    return (Decimal(numerator) / Decimal(denominator)).ln()


def _days(count: int) -> tuple[dt.date, ...]:
    return tuple(stock_trading_days(dt.date(2020, 1, 2), dt.date(2020, 6, 30)))[:count]


def _prepared(days: tuple[dt.date, ...], spx: tuple[str, ...], qqq: tuple[str, ...], t0_index: int,
              first_loss_index: int) -> PreparedEvaluation:
    series = {"SPX": dict(zip(days, map(Decimal, spx), strict=True)),
              "QQQ": dict(zip(days, map(Decimal, qqq), strict=True))}
    return PreparedEvaluation(load_wavewarn_config(ROOT / "config/wavewarn_v121.yaml"),
                              DevelopmentInputs(days, series), days[t0_index], days[t0_index],
                              days[first_loss_index], ())


def _states(days: tuple[dt.date, ...], signals: tuple[str, ...]) -> CandidateStates:
    candidate = Candidate("P1", 3, Decimal("0.015"), None, 0)
    return CandidateStates(candidate, days[0], tuple(
        DiagnosticRow(day, "P1-E2", 3, Decimal("0.015"), signal, "完整", (), "")
        for day, signal in zip(days, signals, strict=True)))


def _no_unknown(days: tuple[dt.date, ...]) -> UnknownLabels:
    return UnknownLabels(days[-1], {"SPX": frozenset(), "QQQ": frozenset()}, {"SPX": {}, "QQQ": {}})


# ---------------------------------------------------------------------------
# 状态机：X1、X2
# ---------------------------------------------------------------------------

def _run_single_price_channel(version: str, k: int, length: int) -> list[str]:
    """一个价格通道：第0日进入并创新低（Q=0），此后第 d 日 Q=d；通道在 Q≥K 时退出。返回逐日灯色。"""
    status, memory, lights = "armed", SystemMemory(), []
    for day in range(length):
        predicate = ChannelPredicate(day == 0, day >= k, True)
        update = update_channel(dt.date(2020, 1, 1) + dt.timedelta(days=day), status, predicate)  # type: ignore[arg-type]
        status = update.status
        inputs = ReadyInputs(day, day, True, True, True, True, True, True)
        system = step_system(update.date, memory, {"P_SPX": ("黄", update)}, inputs, k, version)  # type: ignore[arg-type]
        memory = system.memory
        lights.append(memory.light)
    return lights


def test_e2_waits_five_quiet_days_after_exit_while_x1_needs_only_k_days() -> None:
    k = 3
    # 第0日通道进入，系统转黄。第1、2日 Q=1、2<K，通道仍激活。第3日 Q=3=K，通道退出（当日有效且未激活）。
    # X1：第3日 Q≥K、价格通道当日未激活、没有非价格通道、广度修复与 MA50 成立 → 第3日（创新低后第 K 天）转绿。
    x1 = _run_single_price_channel("X1", k, 9)
    assert x1.index("绿", 1) == k
    # E2：还须“全部通道连续5天有效且未激活”。退出当日计为第1天（规格第三节反例同此计法），
    # 第3、4、5、6、7日共5天 → 第7日转绿，比 X1 晚 4 个交易日，即从创新低日算起第 K+4 天。
    e2 = _run_single_price_channel("E2", k, 9)
    assert e2.index("绿", 1) == k + 4
    assert e2[:k + 4] == ["黄"] * (k + 4) and x1[:k] == ["黄"] * k
    # X2 去掉 MA50 条件，这里 MA50 成立，结果与 X1 相同。
    assert _run_single_price_channel("X2", k, 9) == x1


def test_ready_versions_are_nested_on_random_sequences() -> None:
    rng = random.Random(20261001)
    names = (("P_SPX", "黄"), ("PR_SPX", "红"), ("P_QQQ", "黄"), ("PR_QQQ", "红"),
             ("BW_SPX", "红"), ("DV_QQQ", "黄"), ("V", "红"))
    counts = {version: 0 for version in VERSIONS}
    for _ in range(300):
        memories = {version: SystemMemory() for version in VERSIONS}
        for offset in range(60):
            day = dt.date(2020, 1, 1) + dt.timedelta(days=offset)
            channels = {name: (level, ChannelDay(day, rng.choices(("armed", "active", "unarmed"),
                                                                  (0.6, 0.03, 0.37))[0],
                                                 rng.random() > 0.02, ""))
                        for name, level in names}
            flags = [rng.choices((True, False, None), (0.8, 0.1, 0.1))[0] for _ in range(4)]
            inputs = ReadyInputs(rng.randint(0, 12), rng.randint(0, 12), flags[0], flags[1], flags[2],
                                 flags[3], True, rng.random() > 0.05)
            ready = {}
            for version in VERSIONS:
                system = step_system(day, memories[version], channels, inputs, 3, version)  # type: ignore[arg-type]
                memories[version] = system.memory
                ready[version] = system.ready
                counts[version] += system.ready
            # 同一天、同样的通道与输入：E2 成立必有 X1 成立，X1 成立必有 X2 成立。
            assert not ready["E2"] or ready["X1"]
            assert not ready["X1"] or ready["X2"]
    # 三个版本都确实出现过 Ready，且放宽后次数不减（嵌套不是空洞成立）。
    assert 0 < counts["E2"] <= counts["X1"] <= counts["X2"] and counts["E2"] < counts["X2"]


def test_x1_requires_price_channel_valid_today_and_nonprice_quiet_streak() -> None:
    day = dt.date(2020, 1, 2)
    inputs = ReadyInputs(5, 5, True, True, True, True, True, True)
    quiet = lambda name: ChannelDay(day, "armed", True, "")  # noqa: E731
    previous = SystemMemory("黄", 0, 0, 4)                    # 非价格通道此前已连续静默 4 天
    channels = {"P_SPX": ("黄", quiet("P")), "V": ("红", quiet("V"))}
    # 当日两通道都有效且未激活：非价格通道静默达到 5 天 → X1 成立；全部通道静默只有 1 天 → E2 不成立。
    assert step_system(day, previous, channels, inputs, 3, "X1").ready
    assert not step_system(day, previous, channels, inputs, 3, "E2").ready
    # 价格通道当日无效（输入缺失）：缺失不能证明风险解除 → X1 不成立。
    invalid = {**channels, "P_SPX": ("黄", ChannelDay(day, "armed", False, ""))}
    assert not step_system(day, previous, invalid, inputs, 3, "X1").ready
    # 非价格通道当日缺值：静默计数清零 → X1 不成立。
    missing = {**channels, "V": ("红", ChannelDay(day, "armed", False, ""))}
    assert not step_system(day, previous, missing, inputs, 3, "X1").ready
    # 收盘不高于 MA50：X1 不成立而 X2 成立。
    below = ReadyInputs(5, 5, True, True, False, True, True, True)
    assert not step_system(day, previous, channels, below, 3, "X1").ready
    assert step_system(day, previous, channels, below, 3, "X2").ready


def test_v13_grid_has_117_settings_in_registered_order() -> None:
    grid = v13_grid(load_wavewarn_config(ROOT / "config/wavewarn_v121.yaml"), VERSIONS)
    # P0 9 组；每个版本 P1 9、N 18、N′ 9 共 36 组，三个版本 108 组；合计 117。
    assert len(grid) == 117 and [row.order for row in grid] == list(range(117))
    assert [row.model for row in grid[:9]] == ["P0"] * 9
    for index, version in enumerate(VERSIONS):
        block = grid[9 + 36 * index:9 + 36 * (index + 1)]
        assert {row.exit_version for row in block} == {version}
        assert [sum(row.model == model for row in block) for model in ("P1", "N", N_PRIME_MODEL)] == [9, 18, 9]


# ---------------------------------------------------------------------------
# 择时得分
# ---------------------------------------------------------------------------

def test_timing_score_mean_exposure_and_linearity() -> None:
    days = _days(5)
    prices = ("100", "98", "99", "101", "102")
    prepared = _prepared(days, prices, prices, 0, 0)
    evaluated = evaluate_candidate(prepared, _states(days, ("绿", "黄", "黄", "绿", "绿")),
                                   NO_EVENTS, _no_unknown(days))
    # 计入区间 4 个，执行灯色 绿、绿、黄、黄 → 暴露 1、1、0.5、0.5，ē=0.75；非绿 2 个，占比 2/4=0.5。
    # 始终绿：无事件；H=100，98/100 恰为 0.98 不计回撤，其后未再低于 0.98×H → L_G=0。
    # 始终红：机会项 = Σr = ln(102/100)（两资产相同、权重各 0.5，加权后不变）→ L_R=ln(1.02)≈0.019803。
    green_loss, red_loss = Decimal(0), _ln("102", "100")
    result = timing_result(evaluated, HALF, green_loss, red_loss)
    assert (result.intervals, result.mean_exposure, result.non_green_share) == (4, Decimal("0.75"), HALF)
    assert result.non_green_share == Decimal(evaluated.executed_non_green_days) / result.intervals
    # 同暴露基准 = 0.75×0 + 0.25×ln(1.02)≈0.004951。
    # 模型 L = 0.5×ln(101/99)+0.5×ln(102/101)+γ(0.005)≈0.019927 → T≈0.014976。
    assert _near(result.benchmark_loss, Decimal("0.25") * red_loss)
    model_loss = HALF * _ln("101", "99") + HALF * _ln("102", "101") + Decimal("0.005")
    assert _near(result.score, model_loss - Decimal("0.25") * red_loss)
    # 线性：每个区间的价格损失 = e_j×(始终绿当日损失) + (1−e_j)×(始终红当日损失)。
    # 始终红逐日损失即 w 加权后的 r_j：ln(98/100)、ln(99/98)、ln(101/99)、ln(102/101)；始终绿逐日为 0。
    red_daily = (_ln("98", "100"), _ln("99", "98"), _ln("101", "99"), _ln("102", "101"))
    exposures = (Decimal(1), Decimal(1), HALF, HALF)
    for row, exposure, red in zip(evaluated.daily_losses, exposures, red_daily, strict=False):
        assert _near(row.danger_loss + row.drawdown_loss + row.opportunity_loss, (1 - exposure) * red)


def test_reference_rows_have_zero_timing_score_and_ma200_executes_next_day() -> None:
    days = _days(7)
    # SPX 收盘 100、102、104、100、98、103、105；QQQ 恒为 200。均线窗口取 3（构造用），t0=第3日，j₀=第4日。
    prepared = _prepared(days, ("100", "102", "104", "100", "98", "103", "105"), ("200",) * 7, 2, 3)
    signals = ma200_signals(days, prepared.inputs.series["SPX"], days[2], 3)
    # 三日均线：第3日 (100+102+104)/3=102，收盘 104≥102 → 绿；第4日均线 102，收盘 100 → 红；
    # 第5日均线 100.667，收盘 98 → 红；第6日均线 100.333，收盘 103 → 绿；第7日均线 102，收盘 105 → 绿。
    assert signals == ("绿", "红", "红", "绿", "绿")
    green, yellow, red, average = reference_rows(prepared, NO_EVENTS, _no_unknown(days), 3)
    # 参照行的 T 按定义为 0：始终绿 ē=1，始终红 ē=0，始终黄 ē=0.5 且 L_Y=0.5·L_G+0.5·L_R（线性）。
    assert green.timing.score == 0 and red.timing.score == 0 and _near(yellow.timing.score, Decimal(0))
    assert [row.timing.mean_exposure for row in (green, yellow, red)] == [1, HALF, 0]
    assert [row.timing.non_green_share for row in (green, yellow, red)] == [0, 1, 1]
    # 均线参照次日收盘执行：j₀ 起的执行灯色为前一日信号 → 绿、红、红、绿。
    assert average.evaluated.system_executed == ("绿", "红", "红", "绿")
    # 计入区间 3 个（第4→5、5→6、6→7日），暴露 1、0、0：ē=1/3，非绿占比 2/3。
    assert average.timing.mean_exposure == Decimal(1) / 3 and average.timing.non_green_share == Decimal(2) / 3
    # 第5日切换（绿→红）计费一次；第7日切换（红→绿）在窗口末日，不计费。
    assert average.evaluated.billed_switches == 1
    assert average.evaluated.daily_losses[-1].terminal_switch_unbilled
    # 主损失：第4→5日 e=1，100→98 恰为 2% 不计回撤 → 0；第5→6、6→7日 e=0，机会项 = 0.5×[ln(103/98)+ln(105/103)]
    # = 0.5×ln(105/98)≈0.034496；加切换 0.005。QQQ 不动，各项为 0。
    assert _near(average.evaluated.total_loss, HALF * _ln("103", "98") + HALF * _ln("105", "103")
                 + Decimal("0.005"))
    # 均线缺输入（窗口不足）时报错，不填补。
    with pytest.raises(ValueError, match="200日均线"):
        ma200_signals(days, prepared.inputs.series["SPX"], days[1], 3)


def test_paired_timing_differences_sum_to_score_difference() -> None:
    # 三个区间；始终绿逐日 g=(0.02, 0, 0.01)，始终红逐日 r=(0.01, 0.03, −0.01)。
    green = (Decimal("0.02"), Decimal("0"), Decimal("0.01"))
    red = (Decimal("0.01"), Decimal("0.03"), Decimal("-0.01"))
    n_daily = (Decimal("0.012"), Decimal("0.020"), Decimal("0.002"))       # ē_N=0.4
    p1_daily = (Decimal("0.018"), Decimal("0.010"), Decimal("0.008"))      # ē_P1=0.8
    d = paired_timing_differences(n_daily, p1_daily, green, red, Decimal("0.4"), Decimal("0.8"))
    # N 侧逐日：0.012−0.4×0.02−0.6×0.01=−0.002；0.020−0−0.6×0.03=0.002；0.002−0.4×0.01+0.6×0.01=0.004。
    # P1 侧逐日：0.018−0.8×0.02−0.2×0.01=0；0.010−0−0.2×0.03=0.004；0.008−0.8×0.01+0.2×0.01=0.002。
    assert d == (Decimal("-0.002"), Decimal("-0.002"), Decimal("0.002"))
    # Σd = T_N − T_P1：T_N=0.034−(0.4×0.03+0.6×0.03)=0.004；T_P1=0.036−(0.8×0.03+0.2×0.03)=0.006。
    assert sum(d, Decimal(0)) == Decimal("0.004") - Decimal("0.006")
    # 谨慎程度差 (ē_N−ē_P1)(L_G−L_R)=(0.4−0.8)×(0.03−0.03)=0。
    assert caution_gap(Decimal("0.4"), Decimal("0.8"), Decimal("0.03"), Decimal("0.03")) == 0
    with pytest.raises(ValueError, match="长度"):
        paired_timing_differences(n_daily[:2], p1_daily, green, red, Decimal("0.4"), Decimal("0.8"))


# ---------------------------------------------------------------------------
# 可行条件与选择程序
# ---------------------------------------------------------------------------

def _rebound(symbol: str, values: tuple[str, ...], class_3: int = 0) -> AssetRebound:
    finite = tuple(Decimal(value) for value in values)
    conservative = distribution((*finite, *(Decimal("Infinity"),) * class_3))
    return AssetRebound(symbol, len(finite) + class_3, 0, len(finite), class_3, distribution(finite),
                        conservative, distribution(()))


def test_feasibility_conditions_and_missing_class_two_sample() -> None:
    good = {"SPX": _rebound("SPX", ("0.2", "0.6", "1.0")), "QQQ": _rebound("QQQ", ("0.1", "0.3", "0.5"))}
    # SPX：中位数 0.6（恰等于上限，成立），P75 = 0.6+0.5×(1.0−0.6)=0.8≤1.00。非绿占比恰为 0.60 也成立。
    assert feasibility(Decimal("0.60"), good, LIMITS).feasible
    over = feasibility(Decimal("0.6001"), good, LIMITS)
    assert (over.feasible, over.non_green_ok, over.note) == (False, False, "非绿占比超过上限")
    # QQQ 类别②无样本：视为不可行并写明；条件值记为 None（报告中显示“无样本”）。
    empty = feasibility(Decimal("0.5"), {"SPX": good["SPX"], "QQQ": _rebound("QQQ", ())}, LIMITS)
    assert (empty.feasible, empty.median_ok["QQQ"], empty.p75_ok["QQQ"]) == (False, None, None)
    assert empty.note == "QQQ 类别②无样本"
    # 中位数超限：(0.2, 0.7, 0.9) 的中位数 0.7>0.60。
    high = feasibility(Decimal("0.5"), {"SPX": _rebound("SPX", ("0.2", "0.7", "0.9")), "QQQ": good["QQQ"]},
                       LIMITS)
    assert (high.feasible, high.median_ok["SPX"], high.p75_ok["SPX"]) == (False, False, True)
    # 保守口径不参与判定：主口径合格、另有 3 件类别③（保守 P75 为超限）仍可行。
    conservative = {"SPX": _rebound("SPX", ("0.2", "0.4"), class_3=3), "QQQ": good["QQQ"]}
    assert conservative["SPX"].conservative.p75 == Decimal("Infinity")
    assert feasibility(Decimal("0.5"), conservative, LIMITS).feasible


def _item(model: str, version: str, score: str, feasible: bool, order: int, non_green: int = 10,
          switches: int = 5) -> SelectionItem:
    return SelectionItem(f"{model}-{version}-{order}", model, version, Decimal(score), non_green, switches,
                         order, feasible)


def test_selection_program_all_branches() -> None:
    # 分支一：E2 下有可行 P1 → 选 E2，即使 X1 下的 P1 的 T 更小（退出版本不以 T 挑选）；N 可行，不启用 N′。
    items = [_item("P1", "E2", "0.30", True, 1), _item("P1", "E2", "0.10", False, 2),
             _item("P1", "E2", "0.20", True, 3), _item("P1", "X1", "-0.50", True, 4),
             _item("N", "E2", "0.05", True, 5), _item("N", "E2", "-0.10", False, 6),
             _item("N", "X1", "-0.90", True, 7), _item(N_PRIME_MODEL, "E2", "-0.80", True, 8)]
    trace = select(items, VERSIONS)
    # 可行的 P1 中 T 最小的是 order=3（0.20；order=2 的 0.10 不可行）；可行的 N 只有 order=5。
    assert (trace.exit_version, trace.p1.order, trace.n.order) == ("E2", 3, 5)  # type: ignore[union-attr]
    assert (trace.feasible_p1, trace.feasible_n, trace.n_prime_used, trace.stopped) == (
        (("E2", 2), ("X1", 1), ("X2", 0)), 1, False, "")
    assert trace.final_n is trace.n
    # 分支二：E2 下 P1 都不可行、X1 下有可行 P1 → 选 X1；N 只在 X1 下挑。
    items = [_item("P1", "E2", "0.10", False, 1), _item("P1", "X1", "0.40", True, 2),
             _item("P1", "X2", "0.01", True, 3), _item("N", "E2", "-1", True, 4),
             _item("N", "X1", "0.20", True, 5)]
    trace = select(items, VERSIONS)
    assert (trace.exit_version, trace.p1.order, trace.n.order) == ("X1", 2, 5)  # type: ignore[union-attr]
    # 分支三：三个版本的 P1 都不可行 → 停止“无可行退出版本”，不选任何设定。
    items = [_item("P1", version, "0.1", False, index) for index, version in enumerate(VERSIONS)]
    items.append(_item("N", "E2", "-1", True, 9))
    trace = select(items, VERSIONS)
    assert (trace.exit_version, trace.p1, trace.final_n, trace.stopped) == (None, None, None, STOP_NO_VERSION)
    # 分支四：选定版本下 N 不可行 → 启用 N′，在同一版本下选可行中 T 最小者。
    items = [_item("P1", "X2", "0.1", True, 1), _item("N", "X2", "-1", False, 2),
             _item("N", "E2", "-1", True, 3), _item(N_PRIME_MODEL, "X2", "0.3", True, 4),
             _item(N_PRIME_MODEL, "X2", "0.2", True, 5), _item(N_PRIME_MODEL, "E2", "-2", True, 6)]
    trace = select(items, VERSIONS)
    assert (trace.exit_version, trace.n, trace.feasible_n, trace.n_prime_used) == ("X2", None, 0, True)
    assert (trace.n_prime.order, trace.feasible_n_prime, trace.final_n.order, trace.stopped) == (5, 2, 5, "")  # type: ignore[union-attr]
    # 分支五：N′ 也不可行 → 停止“N 在登记约束下不可行”；P1 已选出，N 侧为空。
    items = [_item("P1", "E2", "0.1", True, 1), _item("N", "E2", "-1", False, 2),
             _item(N_PRIME_MODEL, "E2", "-1", False, 3)]
    trace = select(items, VERSIONS)
    assert (trace.p1.order, trace.final_n, trace.n_prime_used, trace.stopped) == (  # type: ignore[union-attr]
        1, None, True, STOP_N_INFEASIBLE)
    # 并列：T 相同依次比较执行非绿天数、计费切换次数、登记顺序。
    items = [_item("P1", "E2", "0.1", True, 1, non_green=10, switches=5),
             _item("P1", "E2", "0.1", True, 2, non_green=9, switches=9),
             _item("P1", "E2", "0.1", True, 3, non_green=9, switches=8),
             _item("P1", "E2", "0.1", True, 4, non_green=9, switches=8), _item("N", "E2", "0", True, 5)]
    assert select(items, VERSIONS).p1.order == 3  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 补充历史的窗口边界
# ---------------------------------------------------------------------------

def test_extended_history_window_boundaries() -> None:
    days = _days(30)
    start, end = days[5], days[20]

    def event(peak: int, trough: int, censored: bool = False) -> ZZEvent:
        return ZZEvent("SPX", days[peak], days[peak + 1], days[trough], None if censored else days[trough + 1],
                       Decimal(100), Decimal(90), censored)

    events = (event(2, 4), event(5, 8), event(10, 20), event(18, 22), event(24, 26, censored=True))
    included = window_events(events, start, end)
    # 第1件 P 早于窗口起点；第2件 P 恰为起点；第3件 Tr 恰为窗口末日；第4件 Tr 在窗口之后；第5件右截尾。
    # 纳入第2、3件；下一事件 T0 取自完整序列：第3件的下一 T0 是第4件的 T0（下标19），虽然第4件不纳入。
    assert [(item.peak_date, next_t0) for item, next_t0 in included] == [(days[5], days[11]), (days[10], days[19])]
    # 窗口只限定事件，不限定价格查询：第2件 Tr=下标8、m=3，执行日下标11 恰为下一事件 T0 → 先判类别③。
    closes = {day: Decimal(95) for day in days}
    assert fixed_delay_reference(days, closes, events[1], days[11], 3).classification == "③"
    # 另取一件 Tr 恰在窗口末日（下标20）、其后没有事件的情形：m=5 的执行日下标25 在窗口之后，
    # 仍读取该日价格计算 R=(95−90)/(100−90)=0.5，而不是“后续窗口不足”。
    last = fixed_delay_reference(days, closes, events[2], None, 5)
    assert (last.classification, last.execution_date, last.recovery) == ("②", days[25], HALF)
    # P0 稳健性：截断到窗口末日后，轴与各序列都不含其后的日期。
    inputs = truncate_inputs(DevelopmentInputs(days, {"SPX": dict(closes), "QQQ": dict(closes)}), end)
    assert inputs.days[-1] == end and max(inputs.series["QQQ"]) == end
    prepared = PreparedEvaluation(load_wavewarn_config(ROOT / "config/wavewarn_v121.yaml"), inputs, days[5],
                                  days[9], days[9], ())
    # τ′=下标9：第3件（P=下标10，Tr=20）在窗口内；第4件 P=下标18 在窗口内、Tr=下标22 在窗口之后 → 跨窗口末端。
    assert crossing_events(prepared, events) == (events[3],)
    with pytest.raises(ValueError, match="交易日"):
        truncate_inputs(DevelopmentInputs(days, {"SPX": {}, "QQQ": {}}), dt.date(2020, 1, 4))


def test_p0_first_complete_day_uses_p0_inputs_inside_window() -> None:
    days = tuple(stock_trading_days(dt.date(2020, 1, 2), dt.date(2020, 9, 30)))
    fixed = load_wavewarn_config(ROOT / "config/wavewarn_v121.yaml").fixed_parameters()
    flat = {day: Decimal(100) for day in days}
    late = {day: Decimal(50) for day in days[40:]}                       # 第二个资产自下标40才有价格
    spx = asset_features(days, flat, {}, Decimal("0.10"), fixed)
    qqq = asset_features(days, late, {}, Decimal("0.10"), fixed)
    # 63 日高点需要 63 个连续收盘价：第二个资产自下标40起，下标 40+62=102 首次具备；MA50 与 20 日新低窗口更早具备。
    assert p0_first_complete_day(days, spx, qqq, days[0]) == days[102]
    # 窗口起点晚于该日时，取窗口起点当天（两资产当天输入都已具备）。
    assert p0_first_complete_day(days, spx, qqq, days[120]) == days[120]


# ---------------------------------------------------------------------------
# 配置与依赖方向
# ---------------------------------------------------------------------------

def test_v13_config_matches_registration_and_rejects_changes() -> None:
    config = load_v13_config(ROOT / "config/wavewarn_v13.yaml")
    assert config.limits == LIMITS and config.exit_versions == VERSIONS and config.ma200_window == 200
    assert config.windows.fixed_delay_start == {"SPX": dt.date(1990, 1, 2), "QQQ": dt.date(1999, 3, 10)}
    assert (config.windows.fixed_delay_end, config.windows.p0_start, config.windows.p0_end) == (
        dt.date(2009, 9, 30), dt.date(1999, 3, 10), dt.date(2009, 9, 30))
    import yaml

    raw = yaml.safe_load((ROOT / "config/wavewarn_v13.yaml").read_text(encoding="utf-8"))
    # 警戒时间上限改成 0.70、退出版本改顺序，都必须被拒绝。
    with pytest.raises(ValueError, match="可行条件"):
        parse_v13_config({**raw, "feasibility": {**raw["feasibility"], "non_green_share_max": "0.70"}}, config.base)
    with pytest.raises(ValueError, match="退出版本"):
        parse_v13_config({**raw, "exit_versions": ["X1", "E2", "X2"]}, config.base)


def test_v13_algorithm_modules_do_not_import_io_modules() -> None:
    """依赖方向：v1.3 的纯计算模块不得导入读写模块、services 或 CLI（含传递依赖）。"""
    code = ("import importlib, json, sys\n"
            "for name in ('timing', 'feasibility', 'evaluation_v13', 'evaluation_v13_tables',"
            " 'evaluation_v13_report', 'extended_history', 'extended_history_report'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.evaluation_v13_run',"
            " 'market_risk.wavewarn.extended_history_run', 'market_risk.wavewarn.calibration_run',"
            " 'market_risk.wavewarn.diagnostics', 'market_risk.services', 'market_risk.cli',"
            " 'market_risk.research.io', 'market_risk.backtest.engine')"
            " or m.startswith('market_risk.scoring')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
