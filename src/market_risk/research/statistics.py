"""小样本描述统计：AUC、簇重抽样、并列校正秩检验。"""

from __future__ import annotations

import math
import random
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Sequence

SEED = 20260928
RESAMPLES = 2000


def auc(sample: Sequence[float], control: Sequence[float]) -> float | None:
    """P(样本较大) + 0.5 P(并列)；分母为所有有效观测配对。"""
    if not sample or not control:
        return None
    ordered = sorted(control)
    wins = sum(bisect_left(ordered, value) + (bisect_right(ordered, value) - bisect_left(ordered, value)) / 2
               for value in sample)
    return wins / (len(sample) * len(control))


def percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def bootstrap_auc(sample: Sequence[float], control: Sequence[float], seed: int = SEED,
                  repeats: int = RESAMPLES) -> tuple[float | None, float | None]:
    """两组分别以危险时段／高位簇为单位有放回抽样。"""
    if not sample or not control:
        return None, None
    rng = random.Random(seed)
    values = []
    for _ in range(repeats):
        a = [sample[rng.randrange(len(sample))] for _ in sample]
        b = [control[rng.randrange(len(control))] for _ in control]
        result = auc(a, b)
        assert result is not None
        values.append(result)
    return percentile(values, 0.025), percentile(values, 0.975)


def mann_whitney_p(sample: Sequence[float], control: Sequence[float]) -> float | None:
    """双侧正态近似，含并列校正与0.5连续性校正。"""
    n1, n2 = len(sample), len(control)
    if not n1 or not n2:
        return None
    combined = list(sample) + list(control)
    total = n1 + n2
    ties = Counter(combined)
    tie_term = sum(count ** 3 - count for count in ties.values())
    variance = n1 * n2 / 12 * ((total + 1) - tie_term / (total * (total - 1)))
    if variance == 0:
        return 1.0
    proportion = auc(sample, control)
    assert proportion is not None
    u = proportion * n1 * n2
    centered = max(0.0, abs(u - n1 * n2 / 2) - 0.5)
    z = centered / math.sqrt(variance)
    return math.erfc(z / math.sqrt(2))


def bh_adjust(p_values: Sequence[float | None]) -> list[float | None]:
    """仅有效 p 值参与 Benjamini-Hochberg；输出维持原顺序。"""
    valid = sorted((p, i) for i, p in enumerate(p_values) if p is not None)
    m = len(valid)
    result: list[float | None] = [None] * len(p_values)
    previous = 1.0
    for rank in range(m, 0, -1):
        p, i = valid[rank - 1]
        previous = min(previous, p * m / rank)
        result[i] = previous
    return result


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total == 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return center - margin, center + margin


def direction(value: float | None) -> str:
    if value is None:
        return "缺失"
    return "高于50%" if value > 0.5 else "低于50%" if value < 0.5 else "等于50%"
