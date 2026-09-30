"""冻结模型配对检验的环形平稳自助抽样；仅供锁定后的验证使用。"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from market_risk.wavewarn.config import PairedParameters


@dataclass(frozen=True)
class PairedResult:
    total_difference: Decimal
    per_interval: Decimal
    p_value: Decimal
    ci_low: Decimal
    ci_high: Decimal
    sample_count: int
    resamples: int
    seed: int
    block_length: int


@dataclass(frozen=True)
class PairedRobustness:
    main: PairedResult
    block_10: PairedResult
    block_40: PairedResult
    first_half: PairedResult | None
    second_half: PairedResult | None
    leave_one_event_totals: tuple[Decimal, ...]
    zero_dates: int
    without_zero_dates: PairedResult | None


def stationary_bootstrap_indices(length: int, block_length: int, resamples: int,
                                 seed: int, generator: np.random.Generator | None = None
                                 ) -> Iterator[tuple[int, ...]]:
    """每次先抽首位置；其后先抽 u，再按 u<1/b 决定重启或环形续行。"""
    if length <= 0 or block_length <= 0 or resamples <= 0:
        raise ValueError("长度、区块均长与重抽次数必须为正")
    generator = generator if generator is not None else np.random.Generator(np.random.PCG64(seed))
    for _ in range(resamples):
        position = int(generator.integers(0, length))
        indices = [position]
        for _ in range(1, length):
            uniform = float(generator.random())
            position = (int(generator.integers(0, length)) if uniform < 1 / block_length
                        else (position + 1) % length)
            indices.append(position)
        yield tuple(indices)


def paired_stationary_test(differences: Sequence[Decimal], block_length: int = 20,
                           resamples: int = 10_000, seed: int = 20260929) -> PairedResult:
    """输入为已冻结模型的日度损失差；排除日以零保留在原交易日位置。"""
    if not differences:
        raise ValueError("配对检验需要非空日度损失差")
    total = sum(differences, Decimal(0))
    sums = [sum((differences[index] for index in indices), Decimal(0))
            for indices in stationary_bootstrap_indices(len(differences), block_length, resamples, seed)]
    exceed = sum(value - total <= total for value in sums)
    p_value = Decimal(1 + exceed) / Decimal(resamples + 1)
    # NumPy 默认百分位法（linear）；方法在负责人确认后锁定。
    lower, upper = np.quantile([float(value) for value in sums], [0.025, 0.975], method="linear")
    return PairedResult(total, total / len(differences), p_value, Decimal(str(lower)), Decimal(str(upper)),
                        len(differences), resamples, seed, block_length)


def zero_around_event(days: Sequence[dt.date], differences: Sequence[Decimal],
                      peak: dt.date, trough: dt.date, padding: int = 20) -> tuple[Decimal, ...]:
    """按交易日行号把 [P−20,Tr+20] 置零，不删除并拼接相邻日期。"""
    if len(days) != len(differences) or tuple(days) != tuple(sorted(set(days))):
        raise ValueError("事件置零需要唯一升序且对齐的日期")
    start = max(0, bisect_left(days, peak) - padding)
    stop = min(len(days), bisect_right(days, trough) + padding)
    return tuple(Decimal(0) if start <= index < stop else value
                 for index, value in enumerate(differences))


def paired_robustness(days: Sequence[dt.date], differences: Sequence[Decimal],
                      event_spans: Sequence[tuple[dt.date, dt.date]],
                      excluded_dates: Sequence[bool], resamples: int = 10_000,
                      fixed: PairedParameters | None = None) -> PairedRobustness:
    """固定三种区块、前后半段、逐事件置零与超1%排除日敏感性。"""
    if not (len(days) == len(differences) == len(excluded_dates)):
        raise ValueError("稳健性输入长度不一致")
    count = fixed.resamples if fixed else resamples
    block = fixed.mean_block_length if fixed else 20
    short_block = fixed.short_block_length if fixed else 10
    long_block = fixed.long_block_length if fixed else 40
    main_seed = fixed.seed_main if fixed else 20260929
    block_10_seed = fixed.seed_block_10 if fixed else 20260910
    block_40_seed = fixed.seed_block_40 if fixed else 20260940
    main = paired_stationary_test(differences, block, count, main_seed)
    block_10 = paired_stationary_test(differences, short_block, count, block_10_seed)
    block_40 = paired_stationary_test(differences, long_block, count, block_40_seed)
    split = fixed.half_split if fixed else dt.date(2020, 1, 1)
    first = [value for day, value in zip(days, differences, strict=True) if day < split]
    second = [value for day, value in zip(days, differences, strict=True) if day >= split]
    first_result = paired_stationary_test(first, block, count, main_seed) if first else None
    second_result = paired_stationary_test(second, block, count, main_seed) if second else None
    padding = fixed.event_padding if fixed else 20
    leaveout = tuple(sum(zero_around_event(days, differences, peak, trough, padding), Decimal(0))
                     for peak, trough in event_spans)
    zero_count = sum(excluded_dates)
    without_zero = None
    if zero_count * 100 > len(days):
        # 此敏感性显式删除日期，会使新区块跨越原本不相邻的交易日，报告须注明。
        retained = [value for value, excluded in zip(differences, excluded_dates, strict=True) if not excluded]
        if retained:
            without_zero = paired_stationary_test(retained, block, count, main_seed)
    return PairedRobustness(main, block_10, block_40, first_result, second_result,
                            leaveout, zero_count, without_zero)
