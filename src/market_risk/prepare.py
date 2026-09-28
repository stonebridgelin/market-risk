"""评分准备层：单日评分与逐日回测共用（阶段6，2026-09-27 确认）。

正常情况直接调用冻结的 v2m.score、v3r1.score。
数据集已覆盖基准日、但某只评分 ETF 当日或回看窗口内缺数据时（snapshot.missing_etfs 非空）：
- 价格维度记待补（可能取值 0、1、2）；
- v2-M 广度的条件 (b) 需要 SPY 收盘价与20日最高收盘价：SPY 缺失时，分别假设条件 (b) 的价格部分"成立"与
  "不成立"，各调用一次冻结的 v2m.score_breadth，取两次结果的并集作为可能取值（两次相同则给确定分数）；
- QQQ、RSP 缺失不影响广度；v3-R1 广度不用价格；其余维度照常调用冻结的维度函数，再用 common.assemble 汇总。
本模块不写任何判定逻辑，只组织对冻结规则函数的调用。
"""

from __future__ import annotations

import dataclasses

from market_risk.models import DimensionScore, EtfSnapshot, MarketSnapshot, ScoreResult
from market_risk.scoring import v2m, v3r1
from market_risk.scoring.common import assemble

PRICE_POSSIBLE = (0, 1, 2)
SPY = "SPY"


def missing_etf_message(snapshot: MarketSnapshot) -> str | None:
    """命令行醒目提示用。"""
    if not snapshot.missing_etfs:
        return None
    detail = "；".join(reason for _, reason in snapshot.missing_etfs)
    return f"【数据源可能有问题】{detail}。价格维度记待补（数据集已覆盖基准日，请核查数据源）"


def pending_price(snapshot: MarketSnapshot) -> DimensionScore:
    names = "、".join(sym for sym, _ in snapshot.missing_etfs)
    return DimensionScore(
        name="价格", score=None, possible_scores=PRICE_POSSIBLE, triggered_conditions=(),
        calculation=f"{names} 当日或回看窗口内缺收盘价，价格维度无法计算",
        pending_reason=f"{names} 收盘价缺失（数据源可能有问题）",
    )


def _spy_hypothesis(snapshot: MarketSnapshot, near_high: bool) -> MarketSnapshot:
    """构造只用于条件 (b) 价格部分的假设快照：SPY 收盘价 100，20日最高收盘价 100（成立）或 200（不成立）。"""
    fake = EtfSnapshot(SPY, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, ())
    return dataclasses.replace(snapshot, etfs={**snapshot.etfs, SPY: fake},
                               spy_window_max_close=100.0 if near_high else 200.0)


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
    price = pending_price(snapshot)
    note = (missing_etf_message(snapshot) or "",)
    credit2 = v2m.score_credit(snapshot)
    a = assemble(v2m.VERSION, price, v2m_breadth(snapshot), v2m.score_vix(snapshot), v2m.score_rates(snapshot),
                 credit2, notes=note, review_flags=v2m.revision_flags(snapshot, credit2))
    credit3 = v3r1.score_credit(snapshot)
    b = assemble(v3r1.VERSION, price, v3r1.score_breadth(snapshot), v3r1.score_vix(snapshot),
                 v3r1.score_rates(snapshot), credit3, notes=note, review_flags=v3r1.revision_flags(snapshot, credit3))
    return a, b
