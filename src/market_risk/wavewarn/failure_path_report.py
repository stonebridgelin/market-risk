"""失败路径分析的表格行与报告正文（纯计算，只生成行与文本，描述性，不参与任何判定）。"""

from __future__ import annotations

import math
from collections.abc import Sequence

from market_risk.wavewarn.failure_path import (
    DRAGGED,
    GREEN,
    IMPROVED,
    NEUTRAL,
    START_BEFORE,
    START_RED,
    START_YELLOW,
    START_YELLOW_RED,
    PathSettings,
)
from market_risk.wavewarn.failure_path_analysis import ObjectAnalysis, WindowInput
from market_risk.wavewarn.failure_path_context import ENVIRONMENTS, POSITIONS, MergedEvent

Row = tuple[object, ...]
REPORT_NAME = "失败路径分析报告.md"
OPENING = ("本报告仅使用各窗口授权截止日以内的数据；只作描述，不改变模型规则、参数、选定设定与γ；"
           "不构成任何机制的选择依据，机制选择另由负责人裁决。")
CHANNEL_TITLE = "损失时段与哪些通道激活、重叠及系统保持规则同时出现"
NOT_APPLICABLE = "不适用"
OUTCOMES = (IMPROVED, DRAGGED, NEUTRAL)
START_TYPES = (START_YELLOW, START_YELLOW_RED, START_RED, START_BEFORE)

SEGMENT_HEADER = ("object", "segment", "start_execution_date", "end_execution_date", "start_signal_date",
                  "end_signal_date", "left_truncated", "unclosed", "length", "D", "exp_D_minus_1", "mean_exposure",
                  "full_log_return", "strategy_log_return", "full_max_drawdown", "strategy_max_drawdown", "short",
                  "yellow_to_red", "red_to_yellow", "round_trips", "outcome", "start_type", "prior_start_date",
                  "prior_start_transition", "event_position", "position_event", "matching_events",
                  "environment_at_start")
SUMMARY_HEADER = ("object", "dimension", "category", "segments", "days", "D_sum")
CHANNEL_HEADER = ("object", "class", "days", "d_sum")
POSITION_HEADER = ("object", "category", "segments", "days", "D_sum")
ENVIRONMENT_HEADER = ("object", "label", "segments_by_start_date", "non_green_days", "d_sum", "intervals")
CASE_HEADER = ("object", "start", "end", "intervals", "non_green_days", "d_sum", "strategy_log_return",
               "full_log_return", "strategy_max_drawdown", "full_max_drawdown", "mean_exposure", "segments")
RECONCILIATION_HEADER = ("object", "total_d", "segments_D", "channel_classes_d", "log_wealth_ratio",
                         "segments_minus_total", "classes_minus_segments", "total_minus_log_wealth_ratio",
                         "tolerance", "passed")
INTERVAL_HEADER = ("object", "date", "next_date", "signal_date", "executed_light", "exposure", "full_return",
                   "strategy_return", "d", "channel_class", "environment")
EVENT_HEADER = ("event", "peak_date", "t0_date", "trough_date", "end_date", "right_censored", "members")


def flag(value: bool) -> str:
    return "是" if value else "否"


def blank(value: object) -> object:
    return "" if value is None else value


def segment_table(results: Sequence[ObjectAnalysis]) -> tuple[Row, ...]:
    rows: list[Row] = []
    for item in results:
        for row, position in zip(item.segments, item.positions, strict=True):
            span = row.span
            rows.append((item.path.name, row.number, row.start_date, row.end_date, blank(row.start_signal_date),
                         blank(row.end_signal_date), flag(span.left_truncated), flag(span.unclosed), row.length,
                         repr(row.gap), repr(row.gap_simple), repr(row.mean_exposure), repr(row.full_log_return),
                         repr(row.strategy_log_return), repr(row.full_drawdown), repr(row.strategy_drawdown),
                         flag(row.short), row.yellow_to_red, row.red_to_yellow, row.round_trips, row.outcome,
                         row.start_type, row.prior.date if row.prior else "", row.prior.transition if row.prior else "",
                         position.category, blank(position.event), ";".join(str(number) for number in position.matches),
                         item.environments[span.start]))
    return tuple(rows)


def _grouped(item: ObjectAnalysis, key: Sequence[str], categories: Sequence[str]) -> list[tuple[str, int, int, float]]:
    """按类别汇总段数、天数与原始 D 之和；中性带内的值不改成零。"""
    return [(name, sum(value == name for value in key),
             sum(row.length for row, value in zip(item.segments, key, strict=True) if value == name),
             math.fsum(row.gap for row, value in zip(item.segments, key, strict=True) if value == name))
            for name in categories]


def summary_table(results: Sequence[ObjectAnalysis]) -> tuple[Row, ...]:
    """按结果、按启动方式、以及两者交叉的汇总。"""
    rows: list[Row] = []
    for item in results:
        outcomes = [row.outcome for row in item.segments]
        starts = [row.start_type for row in item.segments]
        crossed = [f"{a}｜{b}" for a, b in zip(outcomes, starts, strict=True)]
        for dimension, key, categories in (
                ("按结果", outcomes, OUTCOMES), ("按启动方式", starts, START_TYPES),
                ("结果×启动方式", crossed, [f"{a}｜{b}" for a in OUTCOMES for b in START_TYPES])):
            rows.extend((item.path.name, dimension, name, count, days, repr(total))
                        for name, count, days, total in _grouped(item, key, categories))
        short = [row for row in item.segments if row.short]
        rows.append((item.path.name, "短段", "段长不超过规定的收益区间数", len(short),
                     sum(row.length for row in short), repr(math.fsum(row.gap for row in short))))
    return tuple(rows)


def channel_table(results: Sequence[ObjectAnalysis]) -> tuple[Row, ...]:
    rows: list[Row] = []
    for item in results:
        if item.totals is None:
            rows.append((item.path.name, NOT_APPLICABLE, "", ""))
        else:
            rows.extend((item.path.name, total.name, total.days, repr(total.gap)) for total in item.totals)
    return tuple(rows)


def position_table(results: Sequence[ObjectAnalysis]) -> tuple[Row, ...]:
    return tuple((item.path.name, name, count, days, repr(total)) for item in results
                 for name, count, days, total in _grouped(item, [p.category for p in item.positions], POSITIONS))


def environment_rows(item: ObjectAnalysis) -> list[tuple[str, int, int, float, int]]:
    """天数与 Σ d_j 按区间归类，段数按起始执行日归类。"""
    path = item.path
    result = []
    for name in ENVIRONMENTS:
        cells = [index for index, label in enumerate(item.environments) if label == name]
        result.append((name, sum(item.environments[row.span.start] == name for row in item.segments),
                       sum(path.lights[index] != GREEN for index in cells),
                       math.fsum(path.gaps[index] for index in cells), len(cells)))
    return result


def environment_table(results: Sequence[ObjectAnalysis]) -> tuple[Row, ...]:
    return tuple((item.path.name, name, segments, days, repr(total), intervals) for item in results
                 for name, segments, days, total, intervals in environment_rows(item))


def case_table(results: Sequence[ObjectAnalysis]) -> tuple[Row, ...]:
    return tuple((item.path.name, case.start, case.end, case.intervals, case.non_green, repr(case.gap),
                  repr(case.strategy_log_return), repr(case.full_log_return), repr(case.strategy_drawdown),
                  repr(case.full_drawdown), repr(case.mean_exposure), ";".join(str(n) for n in case.segments))
                 for item in results for case in item.cases)


def reconciliation_table(results: Sequence[ObjectAnalysis], settings: PathSettings) -> tuple[Row, ...]:
    rows: list[Row] = []
    for item in results:
        check = item.reconciliation
        differences = check.differences()
        rows.append((item.path.name, repr(check.total_gap), repr(check.segment_gap),
                     NOT_APPLICABLE if check.class_gap is None else repr(check.class_gap), repr(check.wealth_gap),
                     repr(differences[0]), NOT_APPLICABLE if check.class_gap is None else repr(differences[1]),
                     repr(differences[2]), repr(settings.tolerance),
                     flag(all(abs(value) <= settings.tolerance for value in differences))))
    return tuple(rows)


def interval_table(results: Sequence[ObjectAnalysis]) -> tuple[Row, ...]:
    rows: list[Row] = []
    for item in results:
        path = item.path
        for index in range(len(path.lights)):
            channel = NOT_APPLICABLE if item.classes is None else blank(item.classes[index])
            rows.append((path.name, path.days[index], path.days[index + 1], blank(path.signal_days[index]),
                         path.lights[index], repr(path.exposures[index]), repr(path.full[index]),
                         repr(path.strategy[index]), repr(path.gaps[index]), channel, item.environments[index]))
    return tuple(rows)


def event_table(events: Sequence[MergedEvent]) -> tuple[Row, ...]:
    return tuple((event.number, event.peak, event.t0, event.trough, blank(event.end), flag(event.right_censored),
                  ";".join(event.members)) for event in events)


def num(value: float) -> str:
    return f"{value:.6f}".replace("-0.000000", "0.000000")


def md_table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
            *("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows), ""]


def overview_lines(results: Sequence[ObjectAnalysis]) -> list[str]:
    rows = []
    for item in results:
        path, total = item.path, item.reconciliation.total_gap
        non_green = sum(light != GREEN for light in path.lights)
        rows.append((path.name, len(path.lights), non_green, len(item.segments),
                     sum(row.span.left_truncated for row in item.segments),
                     sum(row.span.unclosed for row in item.segments),
                     num(math.fsum(path.exposures) / len(path.lights)), num(total), num(math.expm1(total))))
    return [*md_table(("对象", "区间数", "非绿执行区间", "非绿执行段", "左截断", "未闭合", "平均暴露",
                       "全窗口 Σd_j", "exp(Σd_j) − 1"), rows),
            "Σd_j = ln(W_策略,末 ÷ W_满仓,末)；exp(Σd_j) − 1 是期末净值比减 1。"
            "各对象按同一本账列出，不作比较选优。", ""]


def object_lines(item: ObjectAnalysis) -> list[str]:
    lines = [f"### {item.path.name}", ""]
    if not item.segments:
        return [*lines, "没有非绿执行段。", ""]
    outcomes, starts = [row.outcome for row in item.segments], [row.start_type for row in item.segments]
    lines += ["按结果（D > 中性带为“相对满仓收益改善”，D < −中性带为“相对满仓收益拖累”，其余“接近零”；"
              "汇总用原始 D，中性带内的值不改成零）：", "",
              *md_table(("类别", "段数", "天数", "ΣD"),
                        [(name, count, days, num(total)) for name, count, days, total
                         in _grouped(item, outcomes, OUTCOMES)]),
              "按启动方式：", "",
              *md_table(("类别", "段数", "天数", "ΣD"),
                        [(name, count, days, num(total)) for name, count, days, total
                         in _grouped(item, starts, START_TYPES)])]
    short = [row for row in item.segments if row.short]
    lines += [f"短段（段长不超过规定的收益区间数）{len(short)} 段，共 {sum(row.length for row in short)} 天，"
              f"ΣD = {num(math.fsum(row.gap for row in short))}。段内黄红往返次数合计 "
              f"{sum(row.round_trips for row in item.segments)}。", ""]
    lines += [f"{CHANNEL_TITLE}：", ""]
    if item.totals is None:
        lines += [f"{NOT_APPLICABLE}（对照没有通道）。", ""]
    else:
        lines += md_table(("类别", "天数", "Σd_j"), [(row.name, row.days, num(row.gap)) for row in item.totals])
    lines += ["段相对事件的位置（按每段起始执行日）：", "",
              *md_table(("位置", "段数", "天数", "ΣD"),
                        [(name, count, days, num(total)) for name, count, days, total
                         in _grouped(item, [p.category for p in item.positions], POSITIONS)]),
              "市场环境（天数与 Σd_j 按区间归类，段数按起始执行日归类）：", "",
              *md_table(("标签", "段数", "非绿天数", "Σd_j", "窗口内区间数"),
                        [(name, segments, days, num(total), intervals)
                         for name, segments, days, total, intervals in environment_rows(item)])]
    return lines


def case_lines(results: Sequence[ObjectAnalysis]) -> list[str]:
    rows = [(item.path.name, f"{case.start} 至 {case.end}", case.intervals, case.non_green, num(case.gap),
             num(case.strategy_log_return), num(case.full_log_return), num(case.strategy_drawdown),
             num(case.full_drawdown), num(case.mean_exposure), "、".join(str(n) for n in case.segments) or "无")
            for item in results for case in item.cases]
    if not rows:
        return []
    return ["## 案例段（只作个案描述）", "",
            "这两段是看过结果之后选出的，因为修正前的 v1.4 与 200 日均线的最大回撤落在其中，不代表典型行情。"
            "取区间起点日 d 满足“起 ≤ d < 止”的全部区间。", "",
            *md_table(("对象", "期间", "区间数", "非绿天数", "Σd_j", "策略对数收益", "满仓对数收益", "策略最大回撤",
                       "满仓最大回撤", "平均暴露", "相交的非绿段编号"), rows)]


def reconciliation_lines(results: Sequence[ObjectAnalysis], settings: PathSettings) -> list[str]:
    rows = []
    for item in results:
        first, second, third = item.reconciliation.differences()
        rows.append((item.path.name, f"{first:.3e}",
                     NOT_APPLICABLE if item.reconciliation.class_gap is None else f"{second:.3e}", f"{third:.3e}"))
    return ["## 对账（验收条件）", "",
            f"float64，绝对误差不超过 {settings.tolerance:g}；任何一项不满足程序即报错停下、不写出结果。", "",
            "1. 全部非绿执行段的 D 之和 = 全窗口 Σd_j；",
            "2. 通道同时出现统计五类的 Σd_j 之和 = 全部非绿段的 D 之和（对照没有通道，不适用）；",
            "3. 全窗口 Σd_j = ln(W_策略,末 ÷ W_满仓,末)，两个期末净值由收盘价独立逐日重建"
            "（相同日期轴、价格与初始净值）。",
            "", *md_table(("对象", "(1) 之差", "(2) 之差", "(3) 之差"), rows)]


def method_lines(window: WindowInput, settings: PathSettings) -> list[str]:
    days = window.days
    return [
        "## 口径", "",
        f"- 窗口：{days[0]} 至最后一个 next_date ≤ {days[-1]} 的区间，共 {len(days) - 1} 个区间。"
        "区间 j 为第 j 日收盘到第 j+1 日收盘。",
        "- R_{a,j} = C_{a,j+1} ÷ C_{a,j} − 1；R_j^满仓 = ½(R_{SPX,j} + R_{QQQ,j})；R_j^策略 = e_j·R_j^满仓，"
        "e_j = x(S_{j−1})（第 j 日收盘执行、由第 j−1 日信号决定：绿 1、黄 0.5、红 0），"
        "不使用第 j 日收盘后才产生的信号；d_j = ln(1 + R_j^策略) − ln(1 + R_j^满仓)。",
        "- 价格收益模拟口径：每日收盘按总暴露、两资产各半再平衡，现金收益为 0，不含分红与费用。"
        "任何 1 + R 不大于 0 时程序报错停下。",
        "- 非绿执行段：执行灯色连续为非绿的最长区间串 [u, v)，一律按执行日记录，信号日另列。"
        "未闭合的段终点取最后一个计入区间的后一交易日收盘；窗口第一个计入区间即为非绿的标“左截断”。"
        "段长按收益区间数。D = Σd_j 可以相加；exp(D) − 1 不可相加。",
        f"- 按结果：D > {settings.neutral_band:g} 为“相对满仓收益改善”，D < −{settings.neutral_band:g} "
        "为“相对满仓收益拖累”，其余为“接近零”——落在预先规定的报告中性带内，不表示结果严格相同，"
        "也不表示差异只来自舍入。",
        "- 按启动方式：(a) 绿→黄启动、段内未出现红灯；(b) 绿→黄启动、段内出现过红灯；(c) 绿→红直接启动；"
        "(d) 窗口开始前已启动。属于 (d) 的段另列窗口前的实际启动方式，不把窗口第一天的颜色当成启动转换。",
        f"- 短段：段长不超过 {settings.short_segment} 个收益区间。黄红往返次数取“黄→红次数”与“红→黄次数”的较小值。",
        "- 通道同时出现统计：每个非绿区间 j 按信号日 j−1 的通道状态归入唯一一类"
        "（系统保持、只有回调层、只有趋势层、只有压力层、多层同时）；同层通道对两个资产取并集。"
        "已入库逐日明细里每一行的 active_channels 与同一行的 signal_light 是同一天（该行日期）的状态，"
        "所以区间 j 取日期 j−1 那一行。“系统保持”只作描述，不表示这种保持合理；没有做逐通道的反事实回测。",
        f"- 段相对事件的位置：按起始执行日 s，用半开区间——确认前 [P, T0)、确认后 [T0, Tr)、低点后 [Tr, End)、"
        f"高点前{window.lookback}日 [P 前第 {window.lookback} 个交易日, P)；End 当日不属于该事件。"
        "先匹配正在发生的事件（多个时取高点最晚的），再匹配高点前的（多个时取高点最早的），都不匹配归事件外。",
        "- 市场环境：熊市优先（区间起点日 d 满足 P ≤ d < Tr）；其余按所在日历年的 SPX 价格指数年度收益"
        "（当年最后一个收盘价 ÷ 上一年最后一个收盘价 − 1）分上涨年、下跌年、平淡年；平淡年不等同于震荡市。"
        "完整日历年没有在授权截止日以内结束的，标“完整年度分类不可得”。标签只作描述，不进入任何信号与选择。",
        "- 分类只作描述，不是因果证明。“改善”只指相对满仓的对数收益差为正。", ""]


def report_lines(title: str, window: WindowInput, results: Sequence[ObjectAnalysis], settings: PathSettings,
                 notes: Sequence[str]) -> list[str]:
    """一个窗口的报告；notes 为读写边界给出的附加小节（标签口径、一致性检查、输入与哈希）。"""
    return [f"# {title}", "", OPENING, "", *method_lines(window, settings), "## 对象概览", "",
            *overview_lines(results), "## 各对象的分类汇总", "",
            *(line for item in results for line in object_lines(item)), *case_lines(results),
            *reconciliation_lines(results, settings), *notes]
