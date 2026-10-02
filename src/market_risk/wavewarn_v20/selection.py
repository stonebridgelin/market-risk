"""选择程序与预定出口（登记第五节）。纯计算。

出口优先级：任一对象（含主参照）计算失败 → “计算失败”；否则任一组的 R2、R1 或 ln W_末 无法计算 → “缺值无法评价”；
否则可行集为空 → “无合格候选”。可行 ⇔ R1 满足且 SPX、QQQ 的 R2 各自达标，两者不能互相补偿。
不论走哪个出口，都输出全部候选的记录。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from market_risk.wavewarn_v20.convergence import Candidate


class SelectionError(ValueError):
    """记录不合法（重复、顺序不符、缺字段）。"""


class Outcome(Enum):
    SELECTED = "选定"
    FAILED = "计算失败"
    UNEVALUABLE = "缺值无法评价"
    NO_FEASIBLE = "无合格候选"


@dataclass(frozen=True)
class CandidateRecord:
    candidate: Candidate
    order: int                              # 登记序号，从 0 起
    failed: bool                            # 计算失败（异常、不收敛、对账不符、杠杆因子不为正、非有限值）
    r1: bool | None                         # R1 是否满足；None 为无法计算
    r2: Mapping[str, bool | None]           # 各资产的 R2 是否达标；None 为无法计算
    log_wealth: float | None                # 信号模拟的 ln W_末；None 为无法计算
    switches: int | None                    # 目标状态切换次数

    @property
    def unevaluable(self) -> bool:
        return self.r1 is None or self.log_wealth is None or not self.r2 or any(
            value is None for value in self.r2.values())

    @property
    def feasible(self) -> bool:
        return self.r1 is True and bool(self.r2) and all(value is True for value in self.r2.values())


@dataclass(frozen=True)
class SelectionResult:
    outcome: Outcome
    selected: Candidate | None
    feasible: tuple[Candidate, ...]
    maximum: float | None                   # M：可行组中 ln W_末 的最大值
    tied: tuple[Candidate, ...]             # 并列组：ln W_末 ≥ M − 容差 的全部可行组
    records: tuple[CandidateRecord, ...]    # 全部候选的记录，按登记顺序


def registered_candidates(ks: Sequence[int], thetas: Sequence[Decimal], hs: Sequence[int]) -> tuple[Candidate, ...]:
    """登记顺序：先 K、再 θ_P、再 h。"""
    return tuple(Candidate(k, theta, h) for k in ks for theta in thetas for h in hs)


def select(records: Sequence[CandidateRecord], reference_failed: bool, tolerance: float) -> SelectionResult:
    """按登记的顺序判断出口并选定。records 须按登记序号排列且不重复。"""
    ordered = tuple(records)
    if [item.order for item in ordered] != list(range(len(ordered))) or len(
            {item.candidate for item in ordered}) != len(ordered) or not ordered:
        raise SelectionError("候选记录须非空、不重复，并按登记序号 0、1、2…… 排列")
    if reference_failed or any(item.failed for item in ordered):
        return SelectionResult(Outcome.FAILED, None, (), None, (), ordered)
    if any(item.unevaluable for item in ordered):
        return SelectionResult(Outcome.UNEVALUABLE, None, (), None, (), ordered)
    if any(item.switches is None or item.switches < 0 or not math.isfinite(item.log_wealth or 0.0)
           for item in ordered):
        raise SelectionError("可评价的记录须有切换次数与有限的 ln W_末")
    feasible = tuple(item for item in ordered if item.feasible)
    if not feasible:
        return SelectionResult(Outcome.NO_FEASIBLE, None, (), None, (), ordered)
    maximum = max(float(item.log_wealth) for item in feasible)       # type: ignore[arg-type]
    tied = tuple(item for item in feasible if float(item.log_wealth) >= maximum - tolerance)   # type: ignore[arg-type]
    chosen = min(tied, key=lambda item: (item.switches, item.order))
    return SelectionResult(Outcome.SELECTED, chosen.candidate, tuple(item.candidate for item in feasible), maximum,
                           tuple(item.candidate for item in tied), ordered)
