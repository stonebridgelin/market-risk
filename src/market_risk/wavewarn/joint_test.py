"""主检验的正式口径：两模型共用同一组交易日索引的联合重抽样（纯计算）。

补充登记 A.3—A.7：每次重抽样对日损失、执行暴露、始终绿与始终红的日损失用同一组索引抽取，
重算各自的 ē 与同暴露基准，θ* = T*_M − T*_B。原登记的固定 ē 方法在同一组索引上并列算出，只作条件性对照。
恒等式：θ*固定 − θ*重算 = (Δē*_M − Δē*_B)·A*，A* = ΣG* − ΣR*；逐次重抽样核对，不符即报错。
逐日一行已是两个资产按权重合并后的系统损失，所以抽取一个交易日即同时抽取其两个资产行。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from market_risk.wavewarn.paired_test import PairedResult, stationary_bootstrap_indices
from market_risk.wavewarn.timing import timing_differences

IDENTITY_TOLERANCE = Decimal("1e-18")


@dataclass(frozen=True)
class JointDay:
    """一个计入区间上两模型与两条恒定参照的日损失与执行暴露。"""

    date: dt.date                 # 区间起点
    model_loss: Decimal           # ℓ_{M,j}
    baseline_loss: Decimal        # ℓ_{B,j}
    model_exposure: Decimal       # e_{M,j}
    baseline_exposure: Decimal    # e_{B,j}
    green_loss: Decimal           # g_j
    red_loss: Decimal             # r_j


@dataclass(frozen=True)
class JointSums:
    """一个样本（原样本、子样本或一次重抽样）的各列之和。"""

    count: int
    model_loss: Decimal
    baseline_loss: Decimal
    model_exposure: Decimal
    baseline_exposure: Decimal
    green_loss: Decimal
    red_loss: Decimal


@dataclass(frozen=True)
class JointPoint:
    """按一个样本自身的 ē 与基准算出的点估计。"""

    intervals: int
    mean_model: Decimal           # ē_M
    mean_baseline: Decimal        # ē_B
    spread: Decimal               # A = ΣG − ΣR
    model_score: Decimal          # T_M
    baseline_score: Decimal       # T_B

    @property
    def theta(self) -> Decimal:
        return self.model_score - self.baseline_score


@dataclass(frozen=True)
class JointDraw:
    """一次重抽样在两种口径下的统计量及其差。"""

    formal: Decimal               # θ*重算
    conditional: Decimal          # θ*固定（Σd*_j）
    gap: Decimal                  # (Δē*_M − Δē*_B)·A*


@dataclass(frozen=True)
class GapSummary:
    """(Δē*_M − Δē*_B)·A* 在全部重抽样上的分布，以及两种口径下 θ* 的标准差。"""

    mean: Decimal
    std: Decimal
    quantiles: tuple[Decimal, ...]        # 与 GAP_QUANTILES 对应
    std_formal: Decimal
    std_conditional: Decimal
    max_identity_residual: Decimal        # max |θ*固定 − θ*重算 − gap|


GAP_QUANTILES = (0.0, 0.025, 0.25, 0.5, 0.75, 0.975, 1.0)


@dataclass(frozen=True)
class JointResult:
    """同一样本、同一组重抽样索引下的正式口径与条件性对照。"""

    point: JointPoint
    formal: PairedResult
    conditional: PairedResult
    gap: GapSummary


def sums(days: Sequence[JointDay]) -> JointSums:
    """按给定顺序把各列相加。"""
    zero = Decimal(0)
    return JointSums(len(days), sum((row.model_loss for row in days), zero),
                     sum((row.baseline_loss for row in days), zero),
                     sum((row.model_exposure for row in days), zero),
                     sum((row.baseline_exposure for row in days), zero),
                     sum((row.green_loss for row in days), zero), sum((row.red_loss for row in days), zero))


def timing_score(loss: Decimal, mean_exposure: Decimal, green: Decimal, red: Decimal) -> Decimal:
    """T = L − [ē·ΣG + (1−ē)·ΣR]。"""
    return loss - (mean_exposure * green + (1 - mean_exposure) * red)


def point_from_sums(total: JointSums) -> JointPoint:
    """ē 与基准都取这个样本自身的值。"""
    if total.count <= 0:
        raise ValueError("样本为空，无法计算择时得分")
    mean_model, mean_baseline = total.model_exposure / total.count, total.baseline_exposure / total.count
    return JointPoint(total.count, mean_model, mean_baseline, total.green_loss - total.red_loss,
                      timing_score(total.model_loss, mean_model, total.green_loss, total.red_loss),
                      timing_score(total.baseline_loss, mean_baseline, total.green_loss, total.red_loss))


def point_estimate(days: Sequence[JointDay]) -> JointPoint:
    return point_from_sums(sums(days))


def fixed_differences(days: Sequence[JointDay], point: JointPoint) -> tuple[Decimal, ...]:
    """原登记的日度差 d_j（ē 固定为原样本的值）；Σd_j = θ̂。"""
    return timing_differences([row.model_loss for row in days], [row.baseline_loss for row in days],
                              [row.green_loss for row in days], [row.red_loss for row in days],
                              point.mean_model, point.mean_baseline)


def joint_draw(resampled: JointSums, fixed_total: Decimal, point: JointPoint) -> JointDraw:
    """一次重抽样：重算 ē 与基准得到 θ*重算；fixed_total 为同一组索引上的 Σd*_j。"""
    star = point_from_sums(resampled)
    shift = (star.mean_model - point.mean_model) - (star.mean_baseline - point.mean_baseline)
    return JointDraw(star.theta, fixed_total, shift * star.spread)


def joint_matrix(days: Sequence[JointDay], differences: Sequence[Decimal]) -> np.ndarray:
    """逐日一行、七列（六列输入与 d_j）；用对象数组保持 Decimal 相加，不转成浮点。"""
    rows = [(row.model_loss, row.baseline_loss, row.model_exposure, row.baseline_exposure, row.green_loss,
             row.red_loss, difference) for row, difference in zip(days, differences, strict=True)]
    return np.array(rows, dtype=object).reshape(len(rows), 7)


def resampled_sums(matrix: np.ndarray, indices: Sequence[int]) -> tuple[JointSums, Decimal]:
    """全部七列用同一组索引抽取后相加：返回六列输入之和与 Σd*_j。"""
    totals = matrix[list(indices)].sum(axis=0)
    return JointSums(len(indices), totals[0], totals[1], totals[2], totals[3], totals[4], totals[5]), totals[6]


def resampled_draw(matrix: np.ndarray, indices: Sequence[int], point: JointPoint) -> JointDraw:
    resampled, fixed_total = resampled_sums(matrix, indices)
    return joint_draw(resampled, fixed_total, point)


def one_sided_result(statistic: Decimal, values: Sequence[Decimal], count: int, block_length: int, seed: int
                     ) -> PairedResult:
    """p = (1 + #{θ*_b − θ̂ ≤ θ̂}) ÷ (B+1)；区间为 θ* 的 2.5%、97.5% 分位数（线性插值）。"""
    exceed = sum(value - statistic <= statistic for value in values)
    lower, upper = np.quantile([float(value) for value in values], [0.025, 0.975], method="linear")
    return PairedResult(statistic, statistic / count, Decimal(1 + exceed) / Decimal(len(values) + 1),
                        Decimal(str(lower)), Decimal(str(upper)), count, len(values), seed, block_length)


def gap_summary(draws: Sequence[JointDraw]) -> GapSummary:
    """差的分布与两种口径的标准差（总体标准差）；恒等式残差取最大绝对值。"""
    gaps = np.array([float(draw.gap) for draw in draws])
    quantiles = np.quantile(gaps, GAP_QUANTILES, method="linear")
    residual = max(abs(draw.conditional - draw.formal - draw.gap) for draw in draws)
    return GapSummary(Decimal(str(gaps.mean())), Decimal(str(gaps.std())),
                      tuple(Decimal(str(value)) for value in quantiles),
                      Decimal(str(np.std([float(draw.formal) for draw in draws]))),
                      Decimal(str(np.std([float(draw.conditional) for draw in draws]))), residual)


def joint_draws(days: Sequence[JointDay], block_length: int, resamples: int, seed: int
                ) -> tuple[JointPoint, Decimal, tuple[JointDraw, ...]]:
    """点估计、Σd_j 与全部重抽样；抽样顺序与原登记的平稳自助法完全相同。"""
    point = point_estimate(days)
    differences = fixed_differences(days, point)
    matrix = joint_matrix(days, differences)
    draws = tuple(resampled_draw(matrix, indices, point)
                  for indices in stationary_bootstrap_indices(len(days), block_length, resamples, seed))
    return point, sum(differences, Decimal(0)), draws


def joint_bootstrap(days: Sequence[JointDay], block_length: int, resamples: int, seed: int) -> JointResult:
    """正式口径与条件性对照；恒等式逐次核对，超出容差即报错。"""
    point, fixed_total, draws = joint_draws(days, block_length, resamples, seed)
    summary = gap_summary(draws)
    if summary.max_identity_residual > IDENTITY_TOLERANCE:
        raise ValueError("固定法与重算法之差不满足恒等式 (Δē*_M − Δē*_B)·A*")
    formal = one_sided_result(point.theta, [draw.formal for draw in draws], len(days), block_length, seed)
    conditional = one_sided_result(fixed_total, [draw.conditional for draw in draws], len(days), block_length, seed)
    return JointResult(point, formal, conditional, summary)
