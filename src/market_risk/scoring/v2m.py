"""规则A：v2-M（SOP 7.2，规则版本冻结）。

逐条对应 SOP 7.2；SOP 未写清之处按 SPEC 5.6 口径。
修改规则必须新建版本文件，不得改动本文件的判定逻辑。
所有比较先把数据四舍五入到其自身精度（SPEC 5.3）；等号边界严格按原文。
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from market_risk.models import DimensionScore, MarketSnapshot, ScoreResult
from market_risk.scoring.common import (
    Outcome,
    Values,
    assemble,
    bp,
    d2,
    evaluate_dimension,
    exact,
    grid,
    pct_change,
    same_result,
    show,
    vintage_or_current,
)

VERSION = "v2-M"

# ---- 门槛（SOP 7.2 原文）----
B40, B50 = Decimal("40"), Decimal("50")
SPY_NEAR_HIGH = Decimal("0.98")
V18, V20, V25 = Decimal("18"), Decimal("20"), Decimal("25")
G20, G30 = Decimal("20"), Decimal("30")          # g，百分数
DY25 = 25                                        # Δy，bp
OAS_HIGH = Decimal("4.00")                       # O1 ≥ 4.00%
DOAS5, DOAS20 = 5, 20                            # ΔOAS，bp
LAG_CAP_DAYS = 1                                 # O1 滞后超过1个股票交易日，最高1分
D1_INCLUDES_T_MINUS_20 = True                    # 三环节 d1 候选为 T−20 至 T−2（SOP 7.2，2026-09-27 确定）

PCT_LO, PCT_HI = Decimal("0"), Decimal("100")
VIX_LO, VIX_HI = Decimal("0.01"), Decimal("200")
YIELD_LO, YIELD_HI = Decimal("-5"), Decimal("30")
OAS_LO, OAS_HI = Decimal("0"), Decimal("50")


# ---------------------------------------------------------------------------
# 一、价格
# ---------------------------------------------------------------------------


def score_price(snapshot: MarketSnapshot, d1_includes_t_minus_20: bool) -> DimensionScore:
    """2分：(a) 至少两只收盘价 < 各自MA50；(b) 任一只在基准日完成三环节；(c) SPY 收盘价 < MA200。
    0分：三只收盘价均 ≥ 各自MA50，且最多一只 < 各自MA20。1分：其余。
    """
    lines: list[str] = []
    below50: list[str] = []
    below20: list[str] = []
    for sym, e in snapshot.etfs.items():
        c, m20, m50 = d2(e.close), exact(e.ma20), exact(e.ma50)  # 均线为派生值，不取整
        assert c is not None and m20 is not None and m50 is not None
        if c < m50:
            below50.append(sym)
        if c < m20:
            below20.append(sym)
        lines.append(f"{sym} 收盘 {c}，MA20 {show(m20)}，MA50 {show(m50)}")
    spy = snapshot.etfs["SPY"]
    spy_c, spy_m200 = d2(spy.close), exact(spy.ma200)
    assert spy_c is not None and spy_m200 is not None
    results = snapshot.three_segment[d1_includes_t_minus_20]
    completed = [r.symbol for r in results if r.completed]
    scope = "T−20 至 T−2" if d1_includes_t_minus_20 else "T−19 至 T−2"

    a = len(below50) >= 2
    b = bool(completed)
    c_ = spy_c < spy_m200
    triggered = tuple(
        label for label, hit in (("2分(a)", a), ("2分(b)", b), ("2分(c)", c_)) if hit
    )
    detail = (
        "；".join(lines)
        + f"。收盘价 < MA50：{'、'.join(below50) or '无'}（{len(below50)}只）；"
        + f"收盘价 < MA20：{'、'.join(below20) or '无'}（{len(below20)}只）；"
        + f"三环节（d1 候选 {scope}）完成：{'、'.join(completed) or '无'}；"
        + f"SPY 收盘 {spy_c} {'<' if c_ else '≥'} MA200 {show(spy_m200)}。"
    )
    if triggered:
        calc = detail + f"满足{'、'.join(triggered)} → 2分"
        return DimensionScore("价格", 2, (2,), triggered, calc)
    if not below50 and len(below20) <= 1:
        calc = detail + "三只均 ≥ MA50 且最多一只 < MA20 → 0分"
        return DimensionScore("价格", 0, (0,), ("0分",), calc)
    return DimensionScore("价格", 1, (1,), ("1分",), detail + "其余有效组合 → 1分")


# ---------------------------------------------------------------------------
# 二、广度
# ---------------------------------------------------------------------------

BREADTH_LABELS = {"F": "S5FI", "W": "S5TW", "F5": "5日前S5FI", "W5": "5日前S5TW"}


def breadth_candidates() -> dict[str, Callable[[Values], list[Decimal]]]:
    def cand(assigned: Values) -> list[Decimal]:
        return grid(B40, B50, *assigned.values(), lo=PCT_LO, hi=PCT_HI)

    return {"F": cand, "W": cand, "F5": cand, "W5": cand}


def breadth_values(snapshot: MarketSnapshot) -> dict[str, Decimal | None]:
    b, b5 = snapshot.breadth, snapshot.breadth_t5
    return {
        "F": d2(b.s5fi) if b else None,
        "W": d2(b.s5tw) if b else None,
        "F5": d2(b5.s5fi) if b5 else None,
        "W5": d2(b5.s5tw) if b5 else None,
    }


def score_breadth(snapshot: MarketSnapshot) -> DimensionScore:
    """2分：(a) L<40%，且 F<F5、W<W5；(b) F<40%，且 SPY 收盘价 ≥ 过去20个交易日最高收盘价×0.98。
    0分：L≥50%。1分：其余。
    """
    spy_c = d2(snapshot.etfs["SPY"].close)
    hi20 = d2(snapshot.spy_window_max_close)
    assert spy_c is not None and hi20 is not None
    threshold = hi20 * SPY_NEAR_HIGH  # 精确乘积，不取整
    near_high = spy_c >= threshold

    def rule(v: Values) -> Outcome:
        f, w, f5, w5 = v["F"], v["W"], v["F5"], v["W5"]
        low = min(f, w)
        a = low < B40 and f < f5 and w < w5
        b = f < B40 and near_high
        calc = (
            f"F={f}，W={w}，L=min(F,W)={low}，F5={f5}，W5={w5}；"
            f"SPY 收盘 {spy_c} vs 20日最高 {hi20}×0.98={threshold}"
            f"（{'≥' if near_high else '<'}）。"
        )
        trig = tuple(x for x, hit in (("2分(a)", a), ("2分(b)", b)) if hit)
        if trig:
            return Outcome(2, trig, calc + f"满足{'、'.join(trig)} → 2分")
        if low >= B50:
            return Outcome(0, ("0分",), calc + "L≥50% → 0分")
        return Outcome(1, ("1分",), calc + "其余有效组合 → 1分")

    return evaluate_dimension(
        "广度", breadth_values(snapshot), rule, breadth_candidates(), BREADTH_LABELS
    )


# ---------------------------------------------------------------------------
# 三、VIX（v3-R1 与本规则相同）
# ---------------------------------------------------------------------------

VIX_LABELS = {"V": "VIX", "V5": "5日前VIX"}


def score_vix(snapshot: MarketSnapshot) -> DimensionScore:
    """g = V ÷ V5 − 1。2分：V≥25；或 V≥20 且 g≥30%。0分：V<18 且 g<20%。1分：其余。"""

    def rule(v: Values) -> Outcome:
        vv, v5 = v["V"], v["V5"]
        g = pct_change(vv, v5)  # 精确值，不取整
        calc = f"V={vv}，V5={v5}，g=V÷V5−1={show(g)}%。"
        trig = tuple(
            x
            for x, hit in (("2分：V≥25", vv >= V25), ("2分：V≥20且g≥30%", vv >= V20 and g >= G30))
            if hit
        )
        if trig:
            return Outcome(2, trig, calc + f"满足{'、'.join(trig)} → 2分")
        if vv < V18 and g < G20:
            return Outcome(0, ("0分",), calc + "V<18 且 g<20% → 0分")
        return Outcome(1, ("1分",), calc + "其余有效组合 → 1分")

    def cand_v(assigned: Values) -> list[Decimal]:
        v5 = assigned.get("V5")
        pts = [V18, V20, V25]
        if v5 is not None:
            pts += [v5 * Decimal("1.2"), v5 * Decimal("1.3")]
        return grid(*pts, lo=VIX_LO, hi=VIX_HI)

    def cand_v5(assigned: Values) -> list[Decimal]:
        vv = assigned["V"]
        return grid(vv / Decimal("1.2"), vv / Decimal("1.3"), lo=VIX_LO, hi=VIX_HI)

    values = {"V": d2(snapshot.vix), "V5": d2(snapshot.vix_t5)}
    return evaluate_dimension("VIX", values, rule, {"V": cand_v, "V5": cand_v5}, VIX_LABELS)


# ---------------------------------------------------------------------------
# 四、利率（v3-R1 与本规则相同）
# ---------------------------------------------------------------------------

RATE_LABELS = {"y": "基准日财政部10年期", "y20": "T−20 的财政部10年期"}


def score_rates(snapshot: MarketSnapshot) -> DimensionScore:
    """H 为基准日及之前19个交易日的最高值，Δy = 100×(y − 20个交易日前的y)。
    2分：y=H 且 Δy≥25。0分：y<H。1分：y=H 且 Δy<25。
    """
    base = snapshot.refs.base_date
    window = set(snapshot.refs.window_days)
    others = [d2(v) for d, v in snapshot.yields.items() if d in window and d != base]
    h_others = max((x for x in others if x is not None), default=None)

    def rule(v: Values) -> Outcome:
        y, y20 = v["y"], v["y20"]
        h = y if h_others is None else max(h_others, y)
        dy = bp(y, y20)
        calc = f"y={y}，H={h}，T−20 的 y={y20}，Δy=100×(y−T−20)={show(dy)}bp。"
        if y == h and dy >= DY25:
            return Outcome(2, ("2分",), calc + "y=H 且 Δy≥25 → 2分")
        if y < h:
            return Outcome(0, ("0分",), calc + "y<H → 0分")
        return Outcome(1, ("1分",), calc + "y=H 且 Δy<25 → 1分")

    def cand_y(assigned: Values) -> list[Decimal]:
        y20 = assigned.get("y20")
        return grid(h_others, None if y20 is None else y20 + Decimal("0.25"),
                    lo=YIELD_LO, hi=YIELD_HI)

    def cand_y20(assigned: Values) -> list[Decimal]:
        return grid(assigned["y"] - Decimal("0.25"), lo=YIELD_LO, hi=YIELD_HI)

    values = {"y": d2(snapshot.y), "y20": d2(snapshot.y_t20)}
    result = evaluate_dimension(
        "利率", values, rule, {"y": cand_y, "y20": cand_y20}, RATE_LABELS
    )
    if snapshot.h_dates and result.score is not None:
        dates = "、".join(str(d) for d in snapshot.h_dates)
        return DimensionScore(
            result.name, result.score, result.possible_scores, result.triggered_conditions,
            result.calculation + f"（H 所在日期：{dates}）", result.pending_reason,
        )
    return result


# ---------------------------------------------------------------------------
# 五、信用
# ---------------------------------------------------------------------------

CREDIT_LABELS = {"O1": "OAS O1", "O6": "OAS O6"}


def _credit_rule(lag: int | None, o1_date: object, o6_date: object) -> Callable[[Values], Outcome]:
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
            out = Outcome(2, trig, calc + f"满足{'、'.join(trig)} → 2分")
        elif doas <= DOAS5 and o1 < OAS_HIGH:
            out = Outcome(0, ("0分",), calc + "ΔOAS≤5 且 O1<4.00% → 0分")
        else:
            out = Outcome(1, ("1分",), calc + "其余 → 1分")
        if lag is not None and lag > LAG_CAP_DAYS and out.score > 1:
            return Outcome(
                1,
                (*out.triggered, "滞后封顶1分"),
                out.calculation
                + f"；但 O1 滞后基准日 {lag} 个股票交易日（>1），最高只计1分 → 最终1分",
            )
        if lag is not None and lag > LAG_CAP_DAYS:
            return Outcome(
                out.score,
                out.triggered,
                out.calculation + f"（O1 滞后 {lag} 个股票交易日，最高1分，本次不影响结果）",
            )
        return out

    return rule


def credit_from_values(
    o1: Decimal | None,
    o6: Decimal | None,
    lag: int | None,
    o1_date: object,
    o6_date: object,
) -> DimensionScore:
    """按给定 O1、O6 数值判定 v2-M 信用（供当前版本与历史版本比对共用）。"""

    def cand_o1(assigned: Values) -> list[Decimal]:
        o6v = assigned.get("O6")
        pts = [OAS_HIGH]
        if o6v is not None:
            pts += [o6v + Decimal("0.05"), o6v + Decimal("0.20")]
        return grid(*pts, lo=OAS_LO, hi=OAS_HI)

    def cand_o6(assigned: Values) -> list[Decimal]:
        o1v = assigned["O1"]
        return grid(o1v - Decimal("0.05"), o1v - Decimal("0.20"), lo=OAS_LO, hi=OAS_HI)

    # 滞后封顶写在判定规则内，待补枚举时同样生效
    return evaluate_dimension(
        "信用",
        {"O1": o1, "O6": o6},
        _credit_rule(lag, o1_date, o6_date),
        {"O1": cand_o1, "O6": cand_o6},
        CREDIT_LABELS,
    )


def score_credit(snapshot: MarketSnapshot) -> DimensionScore:
    """ΔOAS = 100×(O1 − O6)。O1 为分析时点已发布的最新观测（SPEC 5.6 第8、10条）；
    O6 按 FRED 所列顺序往前数第5个（月末周末观测计入）。
    2分：ΔOAS≥20，或 O1≥4.00%。0分：ΔOAS≤5，且 O1<4.00%。1分：其余。
    若 O1 滞后基准日超过1个股票交易日，最高只计1分。
    """
    r = snapshot.refs
    return credit_from_values(
        d2(snapshot.oas_o1_v2m), d2(snapshot.oas_o6_v2m),
        r.o1_v2m_lag_stock_days, r.oas_o1_v2m, r.oas_o6_v2m,
    )


def revision_flags(snapshot: MarketSnapshot, current: DimensionScore) -> list[str]:
    """历史修订比对（SPEC 5.6 第3条）：用基准日版本重算信用，分数不同则交给用户判断。"""
    points = {v.label: v for v in snapshot.oas_vintage}
    o1v, o6v = points.get(f"{VERSION} O1"), points.get(f"{VERSION} O6")
    if not (o1v and o6v) or not (o1v.revised or o6v.revised):
        return []
    r = snapshot.refs
    vintage = credit_from_values(
        d2(vintage_or_current(o1v)), d2(vintage_or_current(o6v)),
        r.o1_v2m_lag_stock_days, r.oas_o1_v2m, r.oas_o6_v2m,
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
    """按规则A（v2-M）计算五个维度、总分、阶段与证据链。

    三环节按 SOP 7.2 的 d1 范围（T−20 至 T−2）计分；另一口径（T−19 至 T−2）只作参考，结果不同时标注。
    """
    price = score_price(snapshot, D1_INCLUDES_T_MINUS_20)
    notes: list[str] = []
    reference = score_price(snapshot, not D1_INCLUDES_T_MINUS_20)
    if reference.score != price.score:
        notes.append(
            f"【三环节结果依赖口径】按 SOP 7.2（d1 为 T−20 至 T−2）价格 {price.score} 分；"
            f"参考口径（d1 为 T−19 至 T−2）为 {reference.score} 分。计分以 SOP 7.2 为准"
        )
    credit = score_credit(snapshot)
    return assemble(
        VERSION,
        price,
        score_breadth(snapshot),
        score_vix(snapshot),
        score_rates(snapshot),
        credit,
        notes=notes,
        review_flags=revision_flags(snapshot, credit),
    )
