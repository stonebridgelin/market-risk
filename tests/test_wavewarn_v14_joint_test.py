"""主检验正式口径（联合重抽样）与敏感性的测试：只用手算例与随机构造的数值，不读取任何行情数据。

对应《v1.4 检验口径与诊断补充登记》A.3—A.8。这里的日期只是行标签，数值都是构造的，不计算任何灯色或损失。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import random
import subprocess
import sys
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from market_risk.wavewarn.config_v14 import load_validation_config, parse_validation_config
from market_risk.wavewarn.joint_test import (
    JointDay,
    fixed_differences,
    joint_bootstrap,
    joint_draws,
    joint_matrix,
    one_sided_result,
    point_estimate,
    resampled_draw,
    resampled_sums,
)
from market_risk.wavewarn.labels_zz import MergedZZEvent, ZZEvent
from market_risk.wavewarn.main_test import (
    COMPONENTS,
    SWITCH_COMPONENT,
    DailyDifference,
    event_range,
    leave_one_events,
    run_main_test,
    without_switch,
)
from market_risk.wavewarn.paired_test import paired_stationary_test, stationary_bootstrap_indices

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_validation_config(ROOT / "config/wavewarn_v14_validation.yaml")
D = Decimal
START = dt.date(2015, 1, 1)


def _day(index: int, values: tuple[str, ...]) -> JointDay:
    return JointDay(START + dt.timedelta(days=index), *(D(value) for value in values))


def _random_days(count: int, seed: int) -> tuple[JointDay, ...]:
    """随机构造：日损失三位小数，暴露取 0、0.5、1。"""
    rng = random.Random(seed)
    number = lambda: f"{rng.uniform(-1, 3):.3f}"  # noqa: E731
    return tuple(_day(index, (number(), number(), rng.choice(("0", "0.5", "1")), rng.choice(("0", "1")),
                              number(), number())) for index in range(count))


HAND = (_day(0, ("1", "2", "1", "1", "4", "0")), _day(1, ("2", "1", "0.5", "1", "2", "2")),
        _day(2, ("0", "1", "0", "0", "0", "2")), _day(3, ("3", "1", "0.5", "0", "2", "0")))


def test_hand_example_of_point_estimate_and_one_resample() -> None:
    """手算例（四个区间）。

    原样本：L_M = 1+2+0+3 = 6，L_B = 2+1+1+1 = 5；ē_M = (1+0.5+0+0.5)/4 = 0.5，ē_B = (1+1+0+0)/4 = 0.5；
    ΣG = 4+2+0+2 = 8，ΣR = 0+2+2+0 = 4，A = 4。
    T_M = 6 − (0.5×8 + 0.5×4) = 0；T_B = 5 − 6 = −1；θ̂ = 0 − (−1) = 1。
    一次重抽样取索引 (0,0,1,1)：L*_M = 1+1+2+2 = 6，L*_B = 2+2+1+1 = 6；ē*_M = (1+1+0.5+0.5)/4 = 0.75，
    ē*_B = (1+1+1+1)/4 = 1；ΣG* = 4+4+2+2 = 12，ΣR* = 0+0+2+2 = 4，A* = 8。
    重算：T*_M = 6 − (0.75×12 + 0.25×4) = −4；T*_B = 6 − (1×12 + 0×4) = −6；θ*重算 = 2。
    固定：T*_M = 6 − (0.5×12 + 0.5×4) = −2；T*_B = 6 − 8 = −2；θ*固定 = 0。
    单模型恒等式：M：−2 − (−4) = 2 = Δē*_M·A* = 0.25×8；B：−2 − (−6) = 4 = 0.5×8。
    两模型之差：0 − 2 = −2 = (0.25 − 0.5)×8。
    """
    point = point_estimate(HAND)
    assert (point.mean_model, point.mean_baseline, point.spread) == (D("0.5"), D("0.5"), D(4))
    assert (point.model_score, point.baseline_score, point.theta) == (D(0), D(-1), D(1))
    differences = fixed_differences(HAND, point)
    assert sum(differences) == point.theta
    matrix = joint_matrix(HAND, differences)
    star, fixed_total = resampled_sums(matrix, (0, 0, 1, 1))
    assert (star.model_loss, star.baseline_loss, star.green_loss, star.red_loss) == (D(6), D(6), D(12), D(4))
    assert (star.model_exposure / 4, star.baseline_exposure / 4) == (D("0.75"), D(1))
    draw = resampled_draw(matrix, (0, 0, 1, 1), point)
    assert (draw.formal, draw.conditional, draw.gap) == (D(2), D(0), D(-2))
    assert fixed_total == draw.conditional and draw.conditional - draw.formal == draw.gap


def _fraction(value: Decimal) -> Fraction:
    return Fraction(str(value))


def test_identity_holds_exactly_on_random_data() -> None:
    """随机数据：用分数做精确算术，逐次重抽样核对单模型与两模型的恒等式，并与程序的 Decimal 结果比较。"""
    days = _random_days(37, 20261001)
    point, fixed_total, draws = joint_draws(days, 5, 25, 7)
    mean_m = sum(_fraction(row.model_exposure) for row in days) / 37
    mean_b = sum(_fraction(row.baseline_exposure) for row in days) / 37
    close = lambda value, exact: abs(_fraction(value) - exact) < Fraction(1, 10**20)  # noqa: E731
    assert close(point.mean_model, mean_m) and close(point.mean_baseline, mean_b)
    assert close(fixed_total, _fraction(point.theta)) and len(draws) == 25
    for draw, indices in zip(draws, stationary_bootstrap_indices(37, 5, 25, 7), strict=True):
        rows = [days[index] for index in indices]
        total = lambda name: sum(_fraction(getattr(row, name)) for row in rows)  # noqa: E731, B023
        green, red = total("green_loss"), total("red_loss")
        spread = green - red
        star_m, star_b = total("model_exposure") / 37, total("baseline_exposure") / 37
        score = lambda loss, mean: loss - (mean * green + (1 - mean) * red)  # noqa: E731, B023
        loss_m, loss_b = total("model_loss"), total("baseline_loss")
        # 单模型：T*固定 − T*重算 = Δē*·A*（精确成立）。
        assert score(loss_m, mean_m) - score(loss_m, star_m) == (star_m - mean_m) * spread
        assert score(loss_b, mean_b) - score(loss_b, star_b) == (star_b - mean_b) * spread
        # 两模型之差：θ*固定 − θ*重算 = (Δē*_M − Δē*_B)·A*；程序的三个量与精确值一致。
        formal = score(loss_m, star_m) - score(loss_b, star_b)
        fixed = score(loss_m, mean_m) - score(loss_b, mean_b)
        gap = ((star_m - mean_m) - (star_b - mean_b)) * spread
        assert fixed - formal == gap
        assert close(draw.formal, formal) and close(draw.conditional, fixed) and close(draw.gap, gap)


def test_every_column_is_resampled_with_the_same_indices() -> None:
    """第 c 列第 i 天取 (c+1)×10^i：一次重抽样后每列之和的十进制各位就是各天被抽中的次数（至多 6 次，不进位）。"""
    days = tuple(JointDay(START + dt.timedelta(days=index), *(D(column + 1) * D(10) ** index for column in range(6)))
                 for index in range(6))
    differences = tuple(D(7) * D(10) ** index for index in range(6))
    matrix = joint_matrix(days, differences)
    for indices in stationary_bootstrap_indices(6, 3, 20, 20260929):
        star, fixed_total = resampled_sums(matrix, indices)
        expected = sum(count * 10 ** index for index, count in Counter(indices).items())
        columns = (star.model_loss, star.baseline_loss, star.model_exposure, star.baseline_exposure,
                   star.green_loss, star.red_loss, fixed_total)
        # 七列（两模型的日损失、两模型的执行暴露、始终绿、始终红、d_j）都对应同一组索引。
        assert [value / (column + 1) for column, value in enumerate(columns)] == [expected] * 7
        assert star.count == 6


def test_one_sided_p_value_and_percentile_interval_by_hand() -> None:
    """θ̂ = −1，θ* = (−3, −2.5, −2, −1, 0)：θ* − θ̂ ≤ θ̂ 即 θ* ≤ −2，共 3 个，p = (1+3)/(5+1) = 2/3。

    百分位区间（线性插值）：2.5% 位置 0.1 → −3 + 0.1×0.5 = −2.95；97.5% 位置 3.9 → −1 + 0.9×1 = −0.1。
    """
    result = one_sided_result(D(-1), [D(-3), D("-2.5"), D(-2), D(-1), D(0)], 4, 20, 99)
    assert result.p_value == D(4) / D(6) and result.per_interval == D("-0.25")
    assert (float(result.ci_low), float(result.ci_high)) == pytest.approx((-2.95, -0.1))
    assert (result.resamples, result.sample_count, result.block_length, result.seed) == (5, 4, 20, 99)


def test_conditional_control_reproduces_the_registered_fixed_method() -> None:
    """条件性对照就是原登记的方法：对 d_j 做平稳自助法，同一种子下逐项相同。"""
    days = _random_days(60, 11)
    result = joint_bootstrap(days, 20, 200, 20260929)
    old = paired_stationary_test(fixed_differences(days, result.point), 20, 200, 20260929)
    assert result.conditional == old
    # 正式口径与条件性对照共用点估计（只差 Decimal 末位舍入），p 与区间来自各自的 θ*。
    assert abs(result.formal.total_difference - old.total_difference) < D("1e-20")
    assert result.gap.max_identity_residual < D("1e-20") and len(result.gap.quantiles) == 7


def _merged(peak: int, trough: int) -> MergedZZEvent:
    first, last = START + dt.timedelta(days=peak), START + dt.timedelta(days=trough)
    return MergedZZEvent(first, first, last, (ZZEvent("SPX", first, first, last, None, D(100), D(90), False),))


def test_leave_one_event_deletes_clipped_range_and_recomputes() -> None:
    """60 个区间（行号 0—59），前后各留 3 个交易日（padding=3）。

    事件 A：P=10、Tr=14 → 删除行号 7—17，共 11 个，剩 49 个。
    事件 B：P=16、Tr=20 → 删除行号 13—23，共 11 个，剩 49 个；与 A 重叠的 13—17 在两次中都删除。
    事件 C：P=1、Tr=2 → [−2, 5] 裁剪为 0—5，共 6 个，剩 54 个。
    事件 D：P=57、Tr=59 → [54, 62] 裁剪为 54—59，共 6 个，剩 54 个。
    事件 E：P、Tr 都在窗口之前（行号 −9、−5）→ 与窗口不相交，不列出。
    """
    days = _random_days(60, 5)
    point = point_estimate(days)
    totals = fixed_differences(days, point)
    events = (_merged(-9, -5), _merged(1, 2), _merged(10, 14), _merged(16, 20), _merged(57, 59))
    items = leave_one_events(days, totals, events, 3, point.theta)
    assert [(item.removed, item.remaining) for item in items] == [(6, 54), (11, 49), (11, 49), (6, 54)]
    dates = [row.date for row in days]
    ranges = [event_range(dates, event.peak_date, event.trough_date, 3) for event in events[1:]]
    assert ranges == [(0, 6), (7, 18), (13, 24), (54, 60)]
    for item, (start, stop) in zip(items, ranges, strict=True):
        kept = [*days[:start], *days[stop:]]
        # 删除后重算 ē 与基准：等于在剩余区间上重新计算的点估计，而不是从原 Σd 中减去一段。
        assert item.recomputed == point_estimate(kept).theta
        # 归因：原固定基准下把同一范围置零 = 原统计量减去该范围内的 d_j。
        assert abs(item.zeroed_total - (sum(totals) - sum(totals[start:stop]))) < D("1e-20")
        assert item.recomputed_sign_changed == ((item.recomputed > 0) != (point.theta > 0))
    # 每次只剔除一个事件：A、B 各自的结果都含对方未重叠的部分，二者不同。
    assert items[1].recomputed != items[2].recomputed


def _differences(days: tuple[JointDay, ...], zeroed: frozenset[int]) -> tuple[DailyDifference, ...]:
    totals = fixed_differences(days, point_estimate(days))
    return tuple(DailyDifference(row.date, total, {name: total / 2 if name in ("危险项", SWITCH_COMPONENT) else D(0)
                                                   for name, _ in COMPONENTS}, index in zeroed)
                 for index, (row, total) in enumerate(zip(days, totals, strict=True)))


def test_calendar_split_recomputes_each_half_and_zero_dates_are_deleted_and_recomputed() -> None:
    """14 个区间，日期为 2019-12-23 起的连续日历日（只是标签）；以 2020-01-01 分界：前半 9 个、后半 5 个。"""
    days = tuple(dataclasses.replace(row, date=dt.date(2019, 12, 23) + dt.timedelta(days=index))
                 for index, row in enumerate(_random_days(14, 3)))
    params = dataclasses.replace(CONFIG.model.base.paired_parameters(), resamples=30)
    assert params.half_split == dt.date(2020, 1, 1)
    differences = _differences(days, frozenset({4}))
    result = run_main_test(days, differences, params.half_split, (), params)
    first, second = days[:9], days[9:]
    assert first[-1].date == dt.date(2019, 12, 31) and second[0].date == dt.date(2020, 1, 1)
    assert result.first_half is not None and result.second_half is not None
    # 两半各自重算 ē 与基准：点估计等于各自子样本上的重新计算，区间数为 9 与 5。
    assert (result.first_half.point, result.second_half.point) == (point_estimate(first), point_estimate(second))
    assert (result.first_half.formal.sample_count, result.second_half.formal.sample_count) == (9, 5)
    # 不要求相加等于全期：两半各用自己的 ē，与“在全期 d_j 上取子集”不是同一个量。
    subset = sum(row.total for row in differences[:9])
    assert result.first_half.point.theta != subset
    assert result.first_half.point.theta + result.second_half.point.theta != result.main.point.theta
    # 三种区块共用同一个点估计；种子与区块长度取登记值。
    assert result.main.point == result.short_block.point == result.long_block.point
    assert [(item.formal.block_length, item.formal.seed) for item in (result.main, result.short_block,
                                                                      result.long_block)] == [
        (20, 20260929), (10, 20260910), (40, 20260940)]
    # 置零日期 1 个，占 1/14 > 1%：删除该日期并重算 ē 与基准（13 个区间）。
    kept = [row for index, row in enumerate(days) if index != 4]
    assert (result.zero_dates, result.zero_share) == (1, D(1) / D(14))
    assert result.without_zero is not None and result.without_zero.point == point_estimate(kept)
    # 没有置零日期时不做这项敏感性。
    assert run_main_test(days, _differences(days, frozenset()), params.half_split, (), params).without_zero is None
    # 分项分解之和等于 Σd。
    assert abs(sum(result.components.values()) - sum(row.total for row in differences)) < D("1e-20")


def test_price_only_differences_drop_the_switch_component() -> None:
    days = _random_days(5, 9)
    differences = _differences(days, frozenset())
    price = without_switch(differences)
    for before, after in zip(differences, price, strict=True):
        assert after.total == before.total - before.components[SWITCH_COMPONENT]
        assert after.components[SWITCH_COMPONENT] == 0 and sum(after.components.values()) == after.total


def test_split_dates_are_pinned_to_the_registration() -> None:
    assert CONFIG.rehearsal_split == dt.date(2013, 7, 1)
    assert CONFIG.model.base.paired_parameters().half_split == dt.date(2020, 1, 1)
    raw = yaml.safe_load((ROOT / "config/wavewarn_v14_validation.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="分界日"):
        parse_validation_config({**raw, "rehearsal": {"half_split": "2013-06-28"}}, CONFIG.model)


def test_main_test_modules_do_not_import_io_modules() -> None:
    """依赖方向：主检验、评价流程与报告生成是纯计算，不得导入读写模块、services 或 CLI（含传递依赖）。"""
    code = ("import importlib, json, sys\n"
            "for name in ('joint_test', 'main_test', 'validation_flow', 'validation_report'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.evaluation_v14_run',"
            " 'market_risk.wavewarn.validation_output', 'market_risk.wavewarn.validation_run',"
            " 'market_risk.wavewarn.lock_guard', 'market_risk.wavewarn.data_v14', 'market_risk.services',"
            " 'market_risk.cli') or m.startswith('market_risk.scoring')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
