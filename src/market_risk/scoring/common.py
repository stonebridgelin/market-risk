"""评分的共同机制（SOP 7.1 / SPEC 5.3）：精度、待补枚举、总分与阶段、证据链。

本模块不含任何门槛数值；各版本的判定逻辑写在 v2m.py、v3r1.py。
不读取结果标签（docs/STORAGE.md 第5节）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from market_risk.models import DimensionScore, OasVintageValue, ScoreResult

CENT = Decimal("0.01")
ALL_SCORES = (0, 1, 2)

Values = Mapping[str, Decimal]
CandidateFn = Callable[[Values], Iterable[Decimal]]


# ---------------------------------------------------------------------------
# 精度（SPEC 5.3）：原始数据按公布精度（两位小数）读取；派生值精确计算、不取整，
# 只在展示时保留两位。
# ---------------------------------------------------------------------------


def d2(x: float | Decimal | None) -> Decimal | None:
    """原始数据（价格、VIX、OAS、收益率、广度）按公布的两位小数读取为 Decimal。"""
    if x is None:
        return None
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def exact(x: float | Decimal | None) -> Decimal | None:
    """派生值（如均线）转为 Decimal，不取整。

    float 的最短十进制表示可还原两位小数价格的有限位平均值（如 670.4418）。
    """
    if x is None:
        return None
    return x if isinstance(x, Decimal) else Decimal(repr(float(x)))


def bp(a: Decimal, b: Decimal) -> Decimal:
    """100×(a − b)，单位 bp，精确计算不取整。"""
    return (a - b) * 100


def pct_change(v: Decimal, v0: Decimal) -> Decimal:
    """g = v ÷ v0 − 1，以百分数表示，精确计算不取整（SPEC 5.3）。"""
    return (v / v0 - 1) * 100


def fixed2(x: Decimal | int | float | None) -> str:
    """展示用：固定两位小数（价格、百分数），四舍五入。"""
    if x is None:
        return "缺失"
    d = x if isinstance(x, Decimal) else Decimal(repr(x))
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def show(x: Decimal | int | float | None) -> str:
    """展示用：整数原样显示，其余保留两位小数。"""
    if x is None:
        return "缺失"
    d = x if isinstance(x, Decimal) else Decimal(repr(x))
    if d == d.to_integral_value():
        return str(int(d))
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# 单个维度的判定结果与待补枚举
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """在全部字段已知时，某维度的判定结果。"""

    score: int
    triggered: tuple[str, ...]
    calculation: str


def grid(*points: Decimal | None, lo: Decimal, hi: Decimal) -> list[Decimal]:
    """缺失字段的候选值：按临界点取值（SPEC 5.3），不按固定步长取样。

    - 每个临界点（门槛值，或由其他已知字段推出的门槛）：点本身及两侧相邻的两位小数值；
      临界点不是两位小数时，取其两侧最近的两位小数值及再外侧的相邻值；
    - 取值范围两端 lo、hi。
    """
    result = {lo, hi}
    for p in points:
        if p is None:
            continue
        floor = p.quantize(CENT, rounding=ROUND_FLOOR)
        ceil = p.quantize(CENT, rounding=ROUND_CEILING)
        for v in (floor - CENT, floor, ceil, ceil + CENT):
            if lo <= v <= hi:
                result.add(v)
    return sorted(result)


def evaluate_dimension(
    name: str,
    values: Mapping[str, Decimal | None],
    rule: Callable[[Values], Outcome],
    candidates: Mapping[str, CandidateFn],
    labels: Mapping[str, str],
) -> DimensionScore:
    """判定一个维度；有缺失字段时按 SPEC 5.3 枚举其可能取值。

    - 全部已知：直接判定。
    - 有缺失：按 candidates 的顺序为缺失字段逐个生成候选值（候选可依赖已赋值字段），
      对全部组合判定。结果唯一则给确定分数并说明理由；否则记待补并给出可能取值。
    """
    missing = [k for k, v in values.items() if v is None]
    if not missing:
        known = {k: v for k, v in values.items() if v is not None}
        out = rule(known)
        return DimensionScore(name, out.score, (out.score,), out.triggered, out.calculation)

    missing_text = "、".join(labels.get(k, k) for k in missing)
    known_text = "；".join(
        f"{labels.get(k, k)}={v}" for k, v in values.items() if v is not None
    ) or "无"
    if any(k not in candidates for k in missing):
        return DimensionScore(
            name, None, ALL_SCORES, (), f"已知：{known_text}；缺失：{missing_text}。",
            f"待补：缺少 {missing_text}",
        )

    order = [k for k in candidates if k in missing]
    evaluations: list[Outcome] = []

    def _enumerate(assigned: dict[str, Decimal], rest: list[str]) -> None:
        if not rest:
            evaluations.append(rule(assigned))
            return
        key, tail = rest[0], rest[1:]
        for c in candidates[key](assigned):
            _enumerate({**assigned, key: c}, tail)

    base = {k: v for k, v in values.items() if v is not None}
    _enumerate(dict(base), order)
    scores = tuple(sorted({o.score for o in evaluations}))
    if len(scores) == 1:
        # 所有情形下都成立的触发条件
        common_trigger = tuple(
            t for t in evaluations[0].triggered if all(t in o.triggered for o in evaluations)
        )
        return DimensionScore(
            name,
            scores[0],
            scores,
            common_trigger,
            f"已知：{known_text}；缺失：{missing_text}。"
            f"枚举缺失字段满足/不满足各门槛（含等号）的全部情形，结果均为 {scores[0]} 分，"
            "缺失字段不改变结果，因此给确定分数。",
        )
    return DimensionScore(
        name,
        None,
        scores,
        (),
        f"已知：{known_text}；缺失：{missing_text}。"
        f"枚举缺失字段的可能取值，分数可能为 {'/'.join(map(str, scores))} 分。",
        f"待补：缺少 {missing_text}",
    )


# ---------------------------------------------------------------------------
# 总分、阶段、证据链
# ---------------------------------------------------------------------------


def stage_of(total: int) -> str:
    """0–2 早期信号；3–5 中期确认信号；6–10 高风险（SOP 7.1）。"""
    if total <= 2:
        return "早期信号"
    if total <= 5:
        return "中期确认信号"
    return "高风险"


def is_two(d: DimensionScore) -> bool | None:
    """该维度是否为2分：是 True、否 False、无法核验 None。"""
    if d.score is not None:
        return d.score == 2
    return None if 2 in d.possible_scores else False


def _yes_no(v: bool | None) -> str:
    return "无法核验" if v is None else ("是" if v else "否")


def _and(*xs: bool | None) -> bool | None:
    if any(x is False for x in xs):
        return False
    return None if any(x is None for x in xs) else True


def _or(*xs: bool | None) -> bool | None:
    if any(x is True for x in xs):
        return True
    return None if any(x is None for x in xs) else False


def assemble(
    version: str,
    price: DimensionScore,
    breadth: DimensionScore,
    vix: DimensionScore,
    rates: DimensionScore,
    credit: DimensionScore,
    notes: Iterable[str] = (),
    review_flags: Iterable[str] = (),
) -> ScoreResult:
    """汇总总分（或范围）、阶段与"大盘明确恶化"证据链（SOP 7.1）。"""
    dims = (price, breadth, vix, rates, credit)
    lo = sum(min(d.possible_scores) for d in dims)
    hi = sum(max(d.possible_scores) for d in dims)
    total = lo if lo == hi else None
    stage = stage_of(lo) if stage_of(lo) == stage_of(hi) else None

    p2, b2, c2, v2 = is_two(price), is_two(breadth), is_two(credit), is_two(vix)
    credit_or_vix = _or(c2, v2)
    chain = (
        ("价格=2", _yes_no(p2)),
        ("广度=2", _yes_no(b2)),
        ("信用=2", _yes_no(c2)),
        ("VIX=2", _yes_no(v2)),
        ("信用=2 或 VIX=2", _yes_no(credit_or_vix)),
        ("大盘明确恶化", _yes_no(_and(p2, b2, credit_or_vix))),
    )
    return ScoreResult(
        version=version,
        price=price,
        breadth=breadth,
        vix=vix,
        rates=rates,
        credit=credit,
        total=total,
        total_range=(lo, hi),
        stage=stage,
        clear_deterioration=chain,
        notes=tuple(notes),
        review_flags=tuple(review_flags),
    )


def vintage_or_current(v: OasVintageValue) -> float | None:
    """历史修订重算用的数值：基准日版本有数值就用它，否则沿用当前版本（不视为修订）。"""
    return v.vintage_value if v.comparable else v.current_value


def same_result(a: DimensionScore, b: DimensionScore) -> bool:
    """两个判定是否相同（分数与可能取值都相同）。"""
    return (a.score, a.possible_scores) == (b.score, b.possible_scores)
