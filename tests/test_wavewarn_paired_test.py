"""冻结模型配对检验只在构造损失差上运行。"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.paired_test import (
    paired_robustness,
    paired_stationary_test,
    stationary_bootstrap_indices,
    zero_around_event,
)


def test_stationary_bootstrap_one_date_has_deterministic_indices() -> None:
    # 唯一位置是0；不管 PCG64 的取值与是否重启，每条序列只能是 (0,)。
    assert list(stationary_bootstrap_indices(1, 20, 10, 20260929)) == [(0,)] * 10


def test_stationary_bootstrap_draw_order_with_constructed_random_stream() -> None:
    class FixedGenerator:
        def __init__(self) -> None:
            self.positions = iter((1, 3, 0, 2, 1))
            self.uniforms = iter((0.6, 0.4, 0.7, 0.2, 0.8, 0.1))

        def integers(self, _low: int, _high: int) -> int:
            return next(self.positions)

        def random(self) -> float:
            return next(self.uniforms)

    # b=2，u<0.5才重新抽位置：第1条 1→2→(重抽3)→0；第2条 0→(重抽2)→3→(重抽1)。
    rows = list(stationary_bootstrap_indices(4, 2, 2, 7, FixedGenerator()))  # type: ignore[arg-type]
    assert rows == [(1, 2, 3, 0), (0, 2, 3, 1)]


def test_negative_constant_difference_has_minimum_p_value_and_reproduces() -> None:
    # 三天各 -2，总和 -6；任意重抽样总和仍 -6，中心化后为0，0≤-6 永不成立。
    data = [Decimal(-2)] * 3
    first = paired_stationary_test(data, block_length=2, resamples=99, seed=20260929)
    second = paired_stationary_test(data, block_length=2, resamples=99, seed=20260929)
    assert first == second
    assert first.total_difference == -6
    assert first.per_interval == -2
    assert first.p_value == Decimal("0.01")
    assert first.ci_low == first.ci_high == -6


def test_zero_dates_stay_in_sequence() -> None:
    # 置零日期仍占交易日位置，样本长度为3；总和只来自 -1 与 +2，等于1。
    result = paired_stationary_test([Decimal(-1), Decimal(0), Decimal(2)], resamples=20, seed=7)
    assert result.sample_count == 3
    assert result.total_difference == 1
    assert result.per_interval == Decimal(1) / 3


def test_leave_one_event_zeroes_trading_day_padding_without_deleting_dates() -> None:
    days = [dt.date(2019, 1, 1) + dt.timedelta(days=index) for index in range(7)]
    values = [Decimal(index) for index in range(1, 8)]
    # P在第2行、Tr在第4行，padding=1，故第1至5行置零，仅保留第0、6行的1与7。
    assert zero_around_event(days, values, days[2], days[4], padding=1) == (
        Decimal(1), Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(7))


def test_robustness_constructed_halves_and_exclusion_threshold() -> None:
    days = [dt.date(2019, 12, 29), dt.date(2019, 12, 30),
            dt.date(2020, 1, 2), dt.date(2020, 1, 3)]
    values = [Decimal(-1), Decimal(0), Decimal(1), Decimal(2)]
    result = paired_robustness(days, values, [], [False, True, False, False], resamples=20)
    # 前半总和−1，后半总和3；四日主总和2；排除1/4>1%才有删日敏感性。
    assert result.main.total_difference == 2
    assert result.first_half is not None and result.first_half.total_difference == -1
    assert result.second_half is not None and result.second_half.total_difference == 3
    assert result.zero_dates == 1
    assert result.without_zero_dates is not None
    assert result.without_zero_dates.sample_count == 3


def test_paired_robustness_uses_strict_configured_blocks_and_seeds_on_synthetic_data() -> None:
    fixed = load_wavewarn_config(Path(__file__).resolve().parents[1] / "config/wavewarn_v121.yaml").paired_parameters()
    days = (dt.date(2019, 12, 31), dt.date(2020, 1, 2))
    # 两个构造日各为零，五种重抽样（主、短、长、前半、后半）总和都只能为零。
    result = paired_robustness(days, (Decimal(0), Decimal(0)), (), (False, False), fixed=fixed)
    assert (result.main.block_length, result.block_10.block_length, result.block_40.block_length) == (20, 10, 40)
    assert (result.main.seed, result.block_10.seed, result.block_40.seed) == (
        20260929, 20260910, 20260940)
    assert result.first_half is not None and result.first_half.total_difference == 0
    assert result.second_half is not None and result.second_half.total_difference == 0
