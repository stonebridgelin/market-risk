"""验证期与保留期的必报诊断 D-1、D-2、D-3（《验证期通过标准》修订三第五节；V1 设计稿定稿第六节）。纯计算。

只报告，不设门槛：不改变类别、稳定性警示、资格与后续动作；不得据以重算、重选或调整任何设定。
全部输入取自同一次运行的组合层结果（逐事件判定、提示段、逐日系统状态），不另读数据、不重算路径。

共同口径：对象为冻结候选的信号模拟；窗口为信号日 [f, E]；事件为按截止日生成的 R2 回调事件，两资产分别计算；
分母与 R2 相同（全部非左截断事件）；提示 = 系统状态为一级或二级；提示段与 r2.prompt_segments 定义相同。
整数判断一律用 R2 规则的分子分母（登记 3/5，即 60%）：5 × x ≥ 3 × 分母；恰为 60% 时“低于 60%”不成立。
计算无效时不计算本节（由调用方保证）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

from market_risk.wavewarn_v20.r2 import EventClass, EventJudgement, R2Result, R2Rule, Segment
from market_risk.wavewarn_v20.state_machine import Risk

NOT_EXIST = "不存在"
NOT_APPLICABLE = "不适用"
NO_SEGMENT = "否（无提示段）"
NOT_JUDGED = "不判断（R2 未满足）"
YES, NO = "是", "否"
PRE_WINDOW = "窗口前已启动"
OPEN_AT_END = "窗口末未结束"
RECLASSIFICATION_NOTE = ("“剔除单段”是重分类压力诊断：把 c(a, s) 个事件改按不达标计后重算比例；"
                         "不是删除该段后重跑策略的反事实结果")


class DiagnosticsError(ValueError):
    """诊断的输入不一致（如段不在窗口内、持续覆盖事件的归段之和不等于持续覆盖达标数）。"""


@dataclass(frozen=True)
class DiagnosticWindow:
    """诊断窗口：信号日 [first, last]（f 至 E），days 为窗口内全部信号日，升序。"""

    first: dt.date
    last: dt.date
    days: tuple[dt.date, ...]

    def __post_init__(self) -> None:
        days = tuple(self.days)
        object.__setattr__(self, "days", days)
        if not days or days[0] != self.first or days[-1] != self.last:
            raise DiagnosticsError("诊断窗口的信号日须恰自 f 起、至 E 止")
        if any(later <= earlier for earlier, later in pairwise(days)):
            raise DiagnosticsError("诊断窗口的信号日须严格升序")

    def count(self, start: dt.date, end: dt.date) -> int:
        """[start, end] 内的窗口信号日数。"""
        return sum(1 for day in self.days if start <= day <= end)


def _meets(rule: R2Rule, numerator: int, denominator: int) -> bool:
    """整数判断：rule.denominator × numerator ≥ rule.numerator × denominator（登记即 5 × x ≥ 3 × 分母）。"""
    return rule.denominator * numerator >= rule.numerator * denominator


def _non_truncated(result: R2Result) -> tuple[EventJudgement, ...]:
    return tuple(item for item in result.judgements if item.category is not EventClass.LEFT_TRUNCATED)


# ---------------------------------------------------------------------------
# D-1 新提示占比
# ---------------------------------------------------------------------------


def new_prompt_share(asset: str, result: R2Result, rule: R2Rule) -> dict:
    """每资产：新提示达标数、持续覆盖达标数、R2 达标数、分母；新提示占比是否 ≥ 60%（整数判断）；
    R2 满足而新提示占比低于 60% 时给出事实陈述（不是警示，不改变资格）。"""
    if not result.computable:
        raise DiagnosticsError(f"{asset} 的 R2 无法计算，诊断不计算")
    new = result.counts[EventClass.NEW]
    covered = result.counts[EventClass.COVERED]
    if new + covered != result.achieved or new != result.new_only:
        raise DiagnosticsError(f"{asset} 的 R2 达标数与分类计数不一致")
    share_meets = _meets(rule, new, result.denominator)
    statement = None
    if result.meets is True and not share_meets:
        statement = f"{asset} 的 R2 达标部分依赖持续覆盖；只计新提示时未达到 60%。"
    return {"asset": asset, "new_achieved": new, "covered_achieved": covered, "r2_achieved": result.achieved,
            "denominator": result.denominator, "r2_meets": result.meets,
            "new_share_at_least_60": share_meets,
            "integer_check": f"{rule.denominator} × {new} ≥ {rule.numerator} × {result.denominator}",
            "statement": statement}


# ---------------------------------------------------------------------------
# D-2 非正常状态总占用率
# ---------------------------------------------------------------------------


def abnormal_occupancy(states: Sequence[tuple[dt.date, Risk | None]], window: DiagnosticWindow) -> dict:
    """窗口内系统状态为一级或二级的信号日数 ÷ 窗口信号日总数；一级、二级分列。状态无法确定（None）的天数单列：
    计入分母，不计入分子。states 须恰好覆盖窗口的全部信号日（可多给窗口外的日期，不计入）。"""
    by_day = dict(states)
    if len(by_day) != len(states):
        raise DiagnosticsError("逐日系统状态中有重复日期")
    missing = [day for day in window.days if day not in by_day]
    if missing:
        raise DiagnosticsError(f"逐日系统状态缺少窗口内的信号日：{missing[0]} 等 {len(missing)} 日")
    values = [by_day[day] for day in window.days]
    total = len(values)
    level1 = sum(1 for value in values if value is Risk.LEVEL1)
    level2 = sum(1 for value in values if value is Risk.LEVEL2)
    unknown = sum(1 for value in values if value is None)
    normal = sum(1 for value in values if value is Risk.NORMAL)
    if level1 + level2 + unknown + normal != total:
        raise DiagnosticsError("逐日系统状态出现状态域以外的值")

    def ratio(count: int) -> float:
        return count / total

    return {"window_days": total, "abnormal_days": level1 + level2, "abnormal_ratio": ratio(level1 + level2),
            "level1_days": level1, "level1_ratio": ratio(level1), "level2_days": level2, "level2_ratio": ratio(level2),
            "undetermined_days": unknown, "normal_days": normal,
            "note": "状态无法确定的天数计入分母，不计入分子"}


# ---------------------------------------------------------------------------
# D-3 单段跨事件长挂
# ---------------------------------------------------------------------------


def _segment_row(segment: Segment, window: DiagnosticWindow, covered: Sequence[EventJudgement],
                 events: Sequence[EventJudgement]) -> dict:
    start, end = segment.start, segment.end
    c = sum(1 for item in covered if start <= item.event.peak <= end)
    e = sum(1 for item in events if item.event.peak <= end and item.event.trough >= start)
    flags = [*([PRE_WINDOW] if segment.pre_window else []), *([OPEN_AT_END] if end == window.last else [])]
    return {"start": start, "end": end, "L": window.count(start, end), "pre_window": segment.pre_window,
            "open_at_end": end == window.last, "flags": flags, "c": c, "e": e}


def _brief(row: dict) -> dict:
    return {"start": row["start"], "end": row["end"], "L": row["L"], "pre_window": row["pre_window"],
            "open_at_end": row["open_at_end"], "flags": row["flags"]}


def long_hang(asset: str, result: R2Result, segments: Sequence[Segment], window: DiagnosticWindow,
              rule: R2Rule) -> dict:
    """对每个提示段 s：c(a, s) 只计高点 P 落在 s 内的持续覆盖达标事件；e(a, s) 计 [P, Tr] 与 s 相交的非左截断事件；
    L(s) 只计窗口内信号日。断言 Σ_s c(a, s) = 持续覆盖达标数。“剔除单段”只在该资产 R2 满足时判断：
    5 × (R2 达标数 − c) < 3 × 分母 即触发（恰为 60% 不触发）。"""
    if not result.computable:
        raise DiagnosticsError(f"{asset} 的 R2 无法计算，诊断不计算")
    ordered = sorted(segments, key=lambda item: (item.start, item.end))
    for segment in ordered:
        if not window.first <= segment.start <= segment.end <= window.last:
            raise DiagnosticsError(f"提示段 {segment.start}—{segment.end} 不在诊断窗口 [f, E] 内")
    for earlier, later in pairwise(ordered):
        if later.start <= earlier.end:
            raise DiagnosticsError(f"提示段重叠：{earlier.start}—{earlier.end} 与 {later.start}—{later.end}")
    events = _non_truncated(result)
    covered = tuple(item for item in events if item.category is EventClass.COVERED)
    rows = [_segment_row(segment, window, covered, events) for segment in ordered]
    if sum(row["c"] for row in rows) != len(covered):
        raise DiagnosticsError(f"{asset} 的持续覆盖达标事件归段之和 {sum(row['c'] for row in rows)} "
                               f"不等于持续覆盖达标数 {len(covered)}")
    if len(covered) != result.counts[EventClass.COVERED]:
        raise DiagnosticsError(f"{asset} 的持续覆盖达标数与 R2 分类计数不一致")
    document: dict = {"asset": asset, "segments_count": len(rows), "segments": rows,
                      "e_at_least_2": sum(1 for row in rows if row["e"] >= 2)}
    if not rows:
        document.update({"c_max": 0, "c_max_segments": NOT_EXIST, "longest_L": None, "longest": NOT_EXIST,
                         "removal_check": {"r2_meets": result.meets, "result": NO_SEGMENT, "triggered": []}})
        return document
    c_max = max(row["c"] for row in rows)
    longest = max(row["L"] for row in rows)
    document.update({
        "c_max": c_max,
        "c_max_segments": NOT_APPLICABLE if c_max == 0 else [_brief(row) | {"c": row["c"]} for row in rows
                                                              if row["c"] == c_max],
        "longest_L": longest, "longest": [_brief(row) for row in rows if row["L"] == longest]})
    if result.meets is not True:
        document["removal_check"] = {"r2_meets": result.meets, "result": NOT_JUDGED, "triggered": []}
        return document
    triggered = [_brief(row) | {"c": row["c"]} for row in rows
                 if rule.denominator * (result.achieved - row["c"]) < rule.numerator * result.denominator]
    document["removal_check"] = {"r2_meets": True, "result": YES if triggered else NO, "triggered": triggered,
                                 "integer_check": f"{rule.denominator} × (R2 达标数 {result.achieved} − c) < "
                                                  f"{rule.numerator} × 分母 {result.denominator}"}
    return document


def warning_lines(hang: dict) -> list[str]:
    """第 3 小节的显著警示原文（逐资产、逐段）；未触发时为空。"""
    check = hang["removal_check"]
    if check["result"] != YES:
        return []
    return [f"**警示：{hang['asset']} 的 R2 资格依赖单一提示段（{row['start'].isoformat()}—{row['end'].isoformat()}，"
            f"{row['L']} 个信号日）对 {row['c']} 个事件的持续覆盖；剔除该段的持续覆盖后，R2 比例低于 60%。**"
            for row in check["triggered"]]


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def diagnostics(results: Mapping[str, R2Result], segments: Mapping[str, Sequence[Segment]],
                states: Sequence[tuple[dt.date, Risk | None]], window: DiagnosticWindow, rule: R2Rule,
                assets: Sequence[str] = ("SPX", "QQQ")) -> dict:
    """D-1、D-2、D-3 的完整报告，两资产分列；事实陈述与警示逐资产生成（不因整体类别而省略）。"""
    if set(results) != set(assets) or set(segments) != set(assets):
        raise DiagnosticsError("诊断须恰好给出两资产的 R2 结果与提示段")
    shares = [new_prompt_share(asset, results[asset], rule) for asset in assets]
    hangs = [long_hang(asset, results[asset], segments[asset], window, rule) for asset in assets]
    return {
        "window": {"first": window.first, "last": window.last, "signal_days": len(window.days)},
        "rule": {"numerator": rule.numerator, "denominator": rule.denominator},
        "d1_new_prompt_share": shares,
        "d2_abnormal_occupancy": abnormal_occupancy(states, window),
        "d3_long_hang": hangs,
        "statements": [item["statement"] for item in shares if item["statement"] is not None],
        "warnings": [line for hang in hangs for line in warning_lines(hang)],
        "reclassification_note": RECLASSIFICATION_NOTE,
        "effect": "只报告，不设门槛；不改变类别、资格、稳定性标注与后续动作；警示与稳定性警示分开列示",
    }
