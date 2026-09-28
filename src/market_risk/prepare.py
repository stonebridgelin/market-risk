"""评分准备层：单日评分与逐日回测共用（阶段6，2026-09-27 确认）。

正常情况直接调用冻结的 v2m.score、v3r1.score。
数据集已覆盖基准日、但某只评分 ETF 当日或回看窗口内缺数据时（snapshot.missing_etfs 非空）：
- 对缺失 ETF 的相关价格条件全枚举，各组调用冻结规则函数；结果唯一时给确定分数，否则待补；
- v2-M 广度的条件 (b) 需要 SPY 收盘价与20日最高收盘价：SPY 缺失时，分别假设条件 (b) 的价格部分"成立"与
  "不成立"，各调用一次冻结的 v2m.score_breadth，取两次结果的并集作为可能取值（两次相同则给确定分数）；
- QQQ、RSP 缺失不影响广度；v3-R1 广度不用价格；其余维度照常调用冻结的维度函数，再用 common.assemble 汇总。
本模块不写任何判定逻辑，只组织对冻结规则函数的调用。
"""

from __future__ import annotations

import dataclasses
import itertools
from decimal import Decimal

from market_risk import calendar as mcal
from market_risk.models import (
    DimensionScore,
    EtfSnapshot,
    MarketSnapshot,
    ScoreResult,
    ThreeSegmentResult,
    ThreeSegmentTrace,
)
from market_risk.scoring import v2m, v3r1
from market_risk.scoring.common import assemble

SPY = "SPY"


def missing_etf_message(snapshot: MarketSnapshot) -> str | None:
    """命令行醒目提示用。"""
    if not snapshot.missing_etfs:
        return None
    detail = "；".join(reason for _, reason in snapshot.missing_etfs)
    return f"【数据源可能有问题】{detail}。价格维度按全部可能条件枚举（数据集已覆盖基准日，请核查数据源）"


def _price_assumption(snapshot: MarketSnapshot, symbol: str, below20: bool, below50: bool,
                      below200: bool, ma5_below50: bool, segment: bool) -> tuple[EtfSnapshot, ThreeSegmentResult]:
    """用严格小于关系构造一个候选；价格分数仍只由冻结的规则函数计算。"""
    close = Decimal("100")
    ma50 = Decimal("101") if below50 else Decimal("99")
    etf = EtfSnapshot(symbol, close, ma50 - 1 if ma5_below50 else ma50 + 1, close,
                      Decimal("101") if below20 else Decimal("99"), close, ma50,
                      Decimal("101") if below200 else Decimal("99"), ())
    base = snapshot.refs.base_date
    d1 = mcal.shift_trading_days(base, -20)
    trace = ThreeSegmentTrace(symbol, d1, Decimal("110"), Decimal("120"),
                              mcal.shift_trading_days(base, -40), segment,
                              (mcal.shift_trading_days(base, -10),) if segment else (), segment, segment)
    return etf, ThreeSegmentResult(symbol, True, (trace,) if segment else ())


def _price_dimension(name: str, scores: set[int], triggers: set[str], missing: str) -> DimensionScore:
    possible = tuple(sorted(scores))
    detail = (f"{missing} 的收盘价缺失；枚举各自收盘价与 MA20/MA50/MA200、MA5 与 MA50、"
              f"三环节完成与否的全部组合，并逐一调用冻结规则，可能分数为 {possible}。")
    if len(possible) == 1:
        return DimensionScore(name, possible[0], possible, tuple(sorted(triggers)),
                              detail + "所有组合分数相同，给确定分数。")
    return DimensionScore(name, None, possible, (), detail, f"{missing} 收盘价缺失（数据源可能有问题）")


def enumerated_prices(snapshot: MarketSnapshot) -> tuple[DimensionScore, DimensionScore]:
    """缺失 ETF 的相关布尔条件全枚举；每组调用两个冻结版本的价格判定函数。"""
    missing = tuple(sym for sym, _ in snapshot.missing_etfs)
    spaces = [list(itertools.product((False, True), repeat=5 if sym == SPY else 4)) for sym in missing]
    scores2: set[int] = set()
    scores3: set[int] = set()
    trigger2: set[str] | None = None
    trigger3: set[str] | None = None
    for combination in itertools.product(*spaces):
        etfs = dict(snapshot.etfs)
        segments = list(snapshot.three_segment[True])
        for symbol, choice in zip(missing, combination, strict=True):
            if symbol == SPY:
                below20, below50, below200, ma5_below50, segment = choice
            else:
                below20, below50, ma5_below50, segment = choice
                below200 = False
            etfs[symbol], trace = _price_assumption(snapshot, symbol, below20, below50,
                                                     below200, ma5_below50, segment)
            segments.append(trace)
        assumed = dataclasses.replace(snapshot, etfs=etfs,
                                      three_segment={**snapshot.three_segment, True: tuple(segments)})
        a = v2m.score_price(assumed, True)
        b = v3r1.score_price(assumed)
        scores2.add(a.score)
        scores3.add(b.score)
        trigger2 = set(a.triggered_conditions) if trigger2 is None else trigger2 & set(a.triggered_conditions)
        trigger3 = set(b.triggered_conditions) if trigger3 is None else trigger3 & set(b.triggered_conditions)
    names = "、".join(missing)
    return (_price_dimension("价格", scores2, trigger2 or set(), names),
            _price_dimension("价格", scores3, trigger3 or set(), names))


def _spy_hypothesis(snapshot: MarketSnapshot, near_high: bool) -> MarketSnapshot:
    """构造只用于条件 (b) 价格部分的假设快照：SPY 收盘价 100，20日最高收盘价 100（成立）或 200（不成立）。"""
    fake = EtfSnapshot(SPY, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"),
                       Decimal("100"), Decimal("100"), Decimal("100"), ())
    return dataclasses.replace(snapshot, etfs={**snapshot.etfs, SPY: fake},
                               spy_window_max_close=Decimal("100") if near_high else Decimal("200"))


def v2m_breadth(snapshot: MarketSnapshot) -> DimensionScore:
    if SPY not in dict(snapshot.missing_etfs):
        return v2m.score_breadth(snapshot)
    yes = v2m.score_breadth(_spy_hypothesis(snapshot, True))
    no = v2m.score_breadth(_spy_hypothesis(snapshot, False))
    possible = tuple(sorted(set(yes.possible_scores) | set(no.possible_scores)))
    same = (yes.score, yes.possible_scores) == (no.score, no.possible_scores)
    detail = (f"条件 (b) 价格部分成立时 {yes.score if yes.score is not None else list(yes.possible_scores)}，"
              f"不成立时 {no.score if no.score is not None else list(no.possible_scores)}")
    calc = f"SPY 收盘价缺失，条件 (b) 的价格部分无法核验；分别假设成立与不成立计算：{detail}"
    if same and yes.score is not None:
        trig = yes.triggered_conditions if yes.triggered_conditions == no.triggered_conditions else ()
        return DimensionScore("广度", yes.score, (yes.score,), trig, calc + "，两种假设结果相同")
    return DimensionScore("广度", None, possible, (), calc,
                          pending_reason="SPY 收盘价缺失，条件 (b) 无法核验（数据源可能有问题）")


def score_versions(snapshot: MarketSnapshot) -> tuple[ScoreResult, ScoreResult]:
    """返回 (v2-M, v3-R1)。ETF 数据齐全时与 v2m.score、v3r1.score 完全相同。"""
    if not snapshot.missing_etfs:
        return v2m.score(snapshot), v3r1.score(snapshot)
    price2, price3 = enumerated_prices(snapshot)
    note = (missing_etf_message(snapshot) or "",)
    credit2 = v2m.score_credit(snapshot)
    a = assemble(v2m.VERSION, price2, v2m_breadth(snapshot), v2m.score_vix(snapshot), v2m.score_rates(snapshot),
                 credit2, notes=note, review_flags=v2m.revision_flags(snapshot, credit2))
    credit3 = v3r1.score_credit(snapshot)
    b = assemble(v3r1.VERSION, price3, v3r1.score_breadth(snapshot), v3r1.score_vix(snapshot),
                 v3r1.score_rates(snapshot), credit3, notes=note, review_flags=v3r1.revision_flags(snapshot, credit3))
    return a, b
