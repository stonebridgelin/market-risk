"""规则B：v3-R1（SOP 7.3，规则版本冻结）。

逐条对应 SOP 7.3；VIX、利率"与规则A相同"，直接复用 v2m 的实现。
修改规则必须新建版本文件，不得改动本文件的判定逻辑。
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from market_risk.models import DimensionScore, MarketSnapshot, ScoreResult
from market_risk.scoring import v2m
from market_risk.scoring.common import (
    Outcome,
    Values,
    assemble,
    bp,
    d2,
    evaluate_dimension,
    exact,
    grid,
    same_result,
    show,
    vintage_or_current,
)

VERSION = "v3-R1"

B40, B50 = v2m.B40, v2m.B50
OAS_HIGH = v2m.OAS_HIGH
DOAS5, DOAS20 = v2m.DOAS5, v2m.DOAS20


# ---------------------------------------------------------------------------
# 一、价格
# ---------------------------------------------------------------------------


def score_price(snapshot: MarketSnapshot) -> DimensionScore:
    """2分：(a) 至少两只同时满足"收盘价 < MA50"与"MA5 < MA50"；(b) SPY 收盘价 < MA200。
    0分：不满足2分，且三只收盘价均 ≥ 各自MA50，同时最多一只 < 各自MA20。
    1分：其余。三环节不参与计分。
    """
    lines: list[str] = []
    both: list[str] = []
    below50: list[str] = []
    below20: list[str] = []
    for sym, e in snapshot.etfs.items():
        c, m5, m20, m50 = d2(e.close), exact(e.ma5), exact(e.ma20), exact(e.ma50)
        assert c is not None and m5 is not None and m20 is not None and m50 is not None
        if c < m50:
            below50.append(sym)
            if m5 < m50:
                both.append(sym)
        if c < m20:
            below20.append(sym)
        lines.append(f"{sym} 收盘 {c}，MA5 {show(m5)}，MA20 {show(m20)}，MA50 {show(m50)}")
    spy = snapshot.etfs["SPY"]
    spy_c, spy_m200 = d2(spy.close), exact(spy.ma200)
    assert spy_c is not None and spy_m200 is not None
    a = len(both) >= 2
    b = spy_c < spy_m200
    triggered = tuple(x for x, hit in (("2分(a)", a), ("2分(b)", b)) if hit)
    detail = (
        "；".join(lines)
        + f"。同时满足收盘价<MA50 与 MA5<MA50：{'、'.join(both) or '无'}（{len(both)}只）；"
        + f"收盘价<MA50：{'、'.join(below50) or '无'}；收盘价<MA20：{'、'.join(below20) or '无'}"
        + f"（{len(below20)}只）；SPY 收盘 {spy_c} {'<' if b else '≥'} MA200 {show(spy_m200)}。"
    )
    if triggered:
        calc = detail + f"满足{'、'.join(triggered)} → 2分"
        return DimensionScore("价格", 2, (2,), triggered, calc)
    if not below50 and len(below20) <= 1:
        return DimensionScore(
            "价格", 0, (0,), ("0分",), detail + "不满足2分，三只均 ≥ MA50 且最多一只 < MA20 → 0分"
        )
    return DimensionScore("价格", 1, (1,), ("1分",), detail + "其余有效组合 → 1分")


# ---------------------------------------------------------------------------
# 二、广度
# ---------------------------------------------------------------------------


def score_breadth(snapshot: MarketSnapshot) -> DimensionScore:
    """2分：(a) L<40%，且 F<F5、W<W5；(b) F<40%，且 F<F5。0分：L≥50%。1分：其余。"""

    def rule(v: Values) -> Outcome:
        f, w, f5, w5 = v["F"], v["W"], v["F5"], v["W5"]
        low = min(f, w)
        a = low < B40 and f < f5 and w < w5
        b = f < B40 and f < f5
        calc = f"F={f}，W={w}，L=min(F,W)={low}，F5={f5}，W5={w5}。"
        trig = tuple(x for x, hit in (("2分(a)", a), ("2分(b)", b)) if hit)
        if trig:
            return Outcome(2, trig, calc + f"满足{'、'.join(trig)} → 2分")
        if low >= B50:
            return Outcome(0, ("0分",), calc + "L≥50% → 0分")
        return Outcome(1, ("1分",), calc + "其余有效组合 → 1分")

    return evaluate_dimension(
        "广度", v2m.breadth_values(snapshot), rule, v2m.breadth_candidates(), v2m.BREADTH_LABELS
    )


# ---------------------------------------------------------------------------
# 三、VIX；四、利率：与规则A相同
# ---------------------------------------------------------------------------

score_vix = v2m.score_vix
score_rates = v2m.score_rates


# ---------------------------------------------------------------------------
# 五、信用
# ---------------------------------------------------------------------------


def _credit_rule(o1_date: object, o6_date: object) -> Callable[[Values], Outcome]:
    def rule(v: Values) -> Outcome:
        o1, o6 = v["O1"], v["O6"]
        doas = bp(o1, o6)
        calc = f"O1（{o1_date}）={o1}%，O6（{o6_date}）={o6}%，ΔOAS=100×(O1−O6)={show(doas)}bp。"
        if doas >= DOAS20 or o1 >= OAS_HIGH:
            trig = tuple(
                x
                for x, hit in (
                    ("2分：ΔOAS≥20", doas >= DOAS20),
                    ("2分：O1≥4.00%", o1 >= OAS_HIGH),
                )
                if hit
            )
            return Outcome(2, trig, calc + f"满足{'、'.join(trig)} → 2分")
        if doas <= DOAS5 and o1 < OAS_HIGH:
            return Outcome(0, ("0分",), calc + "ΔOAS≤5 且 O1<4.00% → 0分")
        return Outcome(1, ("1分",), calc + "其余 → 1分")

    return rule


def credit_from_values(
    o1: Decimal | None, o6: Decimal | None, o1_date: object, o6_date: object
) -> DimensionScore:
    """O1 须为基准日之前最近一个债市营业日的观测，缺失记"待补：数据滞后"；
    O6 缺失时按共同规则枚举（O1≥4.00% 时无需 O6）。
    """
    if o1 is None:
        return DimensionScore(
            "信用", None, (0, 1, 2), (),
            f"O1（{o1_date}，基准日之前最近一个债市营业日）没有 OAS 数值。",
            "待补：数据滞后",
        )

    def cand_o6(assigned: Values) -> list[Decimal]:
        o1v = assigned["O1"]
        return grid(o1v - Decimal("0.05"), o1v - Decimal("0.20"), lo=v2m.OAS_LO, hi=v2m.OAS_HI)

    return evaluate_dimension(
        "信用",
        {"O1": o1, "O6": o6},
        _credit_rule(o1_date, o6_date),
        {"O6": cand_o6},
        v2m.CREDIT_LABELS,
    )


def score_credit(snapshot: MarketSnapshot) -> DimensionScore:
    """ΔOAS = 100×(O1 − O6)；O6 为 O1 之前第5个债市营业日（月末周末观测不计入）。
    2分：ΔOAS≥20，或 O1≥4.00%。0分：ΔOAS≤5，且 O1<4.00%。1分：其余。
    """
    r = snapshot.refs
    return credit_from_values(
        d2(snapshot.oas_o1), d2(snapshot.oas_o6_v3r1), r.oas_o1, r.oas_o6_v3r1
    )


def revision_flags(snapshot: MarketSnapshot, current: DimensionScore) -> list[str]:
    """历史修订比对（SPEC 5.6 第3条）：用基准日版本重算信用，分数不同则交给用户判断。"""
    points = {v.label: v for v in snapshot.oas_vintage}
    o1v, o6v = points.get(f"{VERSION} O1"), points.get(f"{VERSION} O6")
    if not (o1v and o6v) or not (o1v.revised or o6v.revised):
        return []
    r = snapshot.refs
    vintage = credit_from_values(
        d2(vintage_or_current(o1v)), d2(vintage_or_current(o6v)), r.oas_o1, r.oas_o6_v3r1
    )
    if same_result(vintage, current):
        return []
    cur = current.score if current.score is not None else current.possible_scores
    old = vintage.score if vintage.score is not None else vintage.possible_scores
    return [
        f"【需人工判断】{VERSION} 信用：按当前版本 OAS 为 {cur} 分，按基准日版本"
        f"（O1={vintage_or_current(o1v)}，O6={vintage_or_current(o6v)}）为 {old} 分；"
        "程序暂按当前版本计分"
    ]


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def score(snapshot: MarketSnapshot) -> ScoreResult:
    """按规则B（v3-R1）计算五个维度、总分、阶段与证据链。"""
    credit = score_credit(snapshot)
    return assemble(
        VERSION,
        score_price(snapshot),
        score_breadth(snapshot),
        score_vix(snapshot),
        score_rates(snapshot),
        credit,
        review_flags=revision_flags(snapshot, credit),
    )
