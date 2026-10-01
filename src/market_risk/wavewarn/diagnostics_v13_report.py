"""v1.3 描述性诊断的表格行与报告正文（纯计算）。"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal

from market_risk.wavewarn.bottleneck import (
    CATEGORIES,
    CATEGORY_HEADER,
    CLASS_2_JUDGED,
    CONDITION_HEADER,
    category_row,
    condition_rows,
)
from market_risk.wavewarn.calibration import shown
from market_risk.wavewarn.condition_trace import NOT_RED
from market_risk.wavewarn.diagnostics_v13 import (
    BOTTLENECK_GROUPS,
    REASON_GROUPS,
    DevelopmentDiagnostics,
    ExtendedDiagnostics,
    SettingDiagnostics,
    group_label,
    group_of,
    is_median,
)
from market_risk.wavewarn.evaluation import SYMBOLS, PreparedEvaluation
from market_risk.wavewarn.evaluation_v13_tables import SETTING_HEADER, setting_prefix
from market_risk.wavewarn.feasibility import AssetRebound
from market_risk.wavewarn.lighting_reasons import MAIN_CLASSES, REASON_HEADER, reason_rows
from market_risk.wavewarn.period_stats import Row

NOTICE = "描述性诊断，不改变任何登记"
SETTING_COLUMNS = (*SETTING_HEADER, "median_setting")
CATEGORY_FILE_HEADER = (*SETTING_COLUMNS, *CATEGORY_HEADER)
CONDITION_FILE_HEADER = (*SETTING_COLUMNS, *CONDITION_HEADER)
EVENT_FILE_HEADER = (*SETTING_COLUMNS, "symbol", "peak_date", "trough_date", "search_end", "category",
                     "signal_days", "execution_days", "bottlenecks", "bottleneck_start_days")
FIRST_DAYS_HEADER = (*SETTING_COLUMNS, "symbol", "peak_date", "trough_date", "condition", "first_days")
REASON_FILE_HEADER = (*SETTING_COLUMNS, "non_green_days", *REASON_HEADER)
MA200_HEADER = ("scope", "row", "symbol", "included", "class_1", "class_2", "class_3", "half_way_events",
                "half_way_incidence", "half_way_decline_median", "half_way_decline_max", "r_median", "r_p75",
                "r_conservative_median", "r_conservative_p75")


def _prefix(item: SettingDiagnostics) -> Row:
    return (*setting_prefix(item.candidate), "是" if is_median(item.candidate) else "否")


def _blank(value: object | None) -> object:
    return "" if value is None else value


def bottleneck_tables(settings: Sequence[SettingDiagnostics]) -> dict[str, tuple[Row, ...]]:
    """瓶颈四组全部设定的四张表：类别件数、各条件、逐事件、逐事件逐条件的首次成立天数。"""
    tables: dict[str, list[Row]] = {"categories": [], "conditions": [], "events": [], "first_days": []}
    for item in (row for row in settings if group_of(row.candidate) in BOTTLENECK_GROUPS):
        prefix = _prefix(item)
        for symbol in SYMBOLS:
            tables["categories"].append((*prefix, *category_row(symbol, item.bottlenecks)))
            tables["conditions"].extend((*prefix, *row) for row in condition_rows(
                symbol, item.bottlenecks, item.listed, item.candidates))
        for event in item.bottlenecks:
            tables["events"].append((*prefix, event.symbol, event.peak_date, event.trough_date, event.search_end,
                                     event.category, _blank(event.signal_days), _blank(event.execution_days),
                                     "；".join(event.bottlenecks), _blank(event.bottleneck_days)))
            tables["first_days"].extend((*prefix, event.symbol, event.peak_date, event.trough_date, name,
                                         _blank(days)) for name, days in event.first_days.items())
    return {name: tuple(rows) for name, rows in tables.items()}


def reason_table(settings: Sequence[SettingDiagnostics]) -> tuple[Row, ...]:
    return tuple((*_prefix(item), item.reasons.non_green, *row)
                 for item in settings if item.reasons is not None for row in reason_rows(item.reasons))


def ma200_rows(scope: str, name: str, rebounds: Mapping[str, AssetRebound]) -> tuple[Row, ...]:
    """与模型相同的事件指标：三类件数、半山腰转绿发生率与跌幅、R 的中位数与 P75。"""
    rows: list[Row] = []
    for symbol in SYMBOLS:
        item = rebounds[symbol]
        incidence = Decimal(item.half_way.n) / item.included if item.included else ""
        rows.append((scope, name, symbol, item.included, item.class_1, item.class_2, item.class_3,
                     item.half_way.n, incidence, shown(item.half_way.median), shown(item.half_way.maximum),
                     shown(item.main.median), shown(item.main.p75), shown(item.conservative.median),
                     shown(item.conservative.p75)))
    return tuple(rows)


def _num(value: object) -> str:
    """整数值（天数等）不带小数；其余小数保留 4 位。CSV 保留完整精度。"""
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else f"{value:.4f}"
    return str(value)


def _share(value: object) -> str:
    return f"{value * 100:.1f}%" if isinstance(value, Decimal) else str(value)


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(_num(cell) for cell in row) + " |" for row in rows)
    return lines


def _median_of(settings: Sequence[SettingDiagnostics], group: tuple[str, str]) -> SettingDiagnostics:
    return next(item for item in settings if group_of(item.candidate) == group and is_median(item.candidate))


def _group_bottleneck_lines(item: SettingDiagnostics) -> list[str]:
    """一个中位设定：两资产的类别件数与各条件的天数分布、瓶颈件数。"""
    lines = [f"### {group_label(item.candidate)}（中位设定）", ""]
    lines.extend(_table(("资产", "纳入", *CATEGORIES, "并列事件数"),
                        [category_row(symbol, item.bottlenecks) for symbol in SYMBOLS]))
    lines.append("")
    for symbol in SYMBOLS:
        rows = [(row[1], row[2], f"{row[3]}/{row[4]}", row[5], row[6], row[7], row[8])
                for row in condition_rows(symbol, item.bottlenecks, item.listed, item.candidates)]
        lines.extend([f"{symbol} 事件：", "",
                      *_table(("条件", "瓶颈候选", "成立/未成立或不适用", "天数中位", "天数 P75", "天数最大",
                               "成为瓶颈的事件数"), rows), ""])
    return lines


def _all_settings_bottleneck_lines(settings: Sequence[SettingDiagnostics]) -> list[str]:
    """每组把全部设定的瓶颈件数相加；并列事件各计一次，所以各条件之和可大于判瓶颈件数。"""
    lines = ["### 全部设定的汇总", "",
             "每组把该组全部设定（P1、N′ 各 9 组，N 18 组）的结果相加。“判瓶颈”为判定瓶颈的事件·设定数；"
             "并列时各条件各计一次。逐设定见 `bottleneck_conditions.csv`。", ""]
    for group in BOTTLENECK_GROUPS:
        own = [item for item in settings if group_of(item.candidate) == group]
        body = []
        for symbol in SYMBOLS:
            events = [event for item in own for event in item.bottlenecks if event.symbol == symbol]
            hits = Counter(name for event in events for name in event.bottlenecks)
            judged = sum(event.category == CLASS_2_JUDGED for event in events)
            body.append((symbol, len(events), judged,
                         "；".join(f"{name} {count}" for name, count in hits.most_common()) or "—"))
        lines.extend([f"**{group_label(own[0].candidate)}**（{len(own)} 组设定）", "",
                      *_table(("资产", "纳入事件·设定数", "判瓶颈", "各条件成为瓶颈的次数"), body), ""])
    return lines


def bottleneck_lines(settings: Sequence[SettingDiagnostics]) -> list[str]:
    lines = [
        "## 一、转绿瓶颈分解（开发期）", "",
        "对高点 P ≥ τ 的每个纳入事件，从低点 Tr 起记录各条件首次成立的交易日数，搜索范围为 "
        "[Tr, 同资产下一事件 T0 的前一日]（没有下一事件则到窗口末日）；Tr 当天已成立记 0，范围内从未成立记空。"
        "(8) 的信号日与执行日只有类别②事件才有，信号日为 Tr−1 时记 −1。", "",
        "瓶颈只对“绿灯信号日不早于 Tr”的类别②事件判定：对每个候选条件，找出包含绿灯信号日的那一段连续成立期，"
        "取其起点；起点最晚的条件为瓶颈，起点同日时各计一次。P1·E2 用“全部通道连续5天有效且未激活”代替 (2)(3)，"
        "(2) 只列天数；(6) 只在 E2 下是候选；(7) 在事件内出现过红灯时是候选。", "",
        f"条件 {NOT_RED} 的口径（负责人 2026-10-01 确认）：以“系统不处于红灯”判定。"
        "瓶颈取包含信号日那段非红期的起点，且仅在其前一日的红灯不早于事件高点 P 时参与；"
        "首次成立天数只在事件内（[P, 搜索上限]）出现过红灯时填写，红灯在低点前已结束记 0。", ""]
    for group in BOTTLENECK_GROUPS:
        lines.extend(_group_bottleneck_lines(_median_of(settings, group)))
    return [*lines, *_all_settings_bottleneck_lines(settings)]


def reason_lines(settings: Sequence[SettingDiagnostics]) -> list[str]:
    """亮灯时间分解：中位设定的主分类与细分原因，另给全部设定的主分类范围。"""
    lines = [
        "## 二、亮灯时间分解（开发期）", "",
        "对每个执行非绿区间，原因取信号日（前一交易日）当天的状态。主分类互斥、合计 100%："
        "A 有通道激活；B 无通道激活但转绿（或红转黄）条件未满足；C 降级限速（当日刚由红降黄，且黄转绿条件已全部满足）。"
        "细分原因多标签计数，**各项占比之和可超过 100%**。", ""]
    main_rows = []
    for group in REASON_GROUPS:
        item = _median_of(settings, group)
        counts = item.reasons
        assert counts is not None
        main_rows.append((group_label(item.candidate), counts.non_green,
                          *(_share(Decimal(counts.main[name]) / counts.non_green) for name in MAIN_CLASSES)))
    lines.extend(["### 主分类（中位设定）", "", *_table(("模型·版本", "执行非绿天数", *MAIN_CLASSES), main_rows), ""])
    for group in REASON_GROUPS:
        item = _median_of(settings, group)
        assert item.reasons is not None
        rows = [(row[1], row[2], _share(row[3])) for row in reason_rows(item.reasons) if row[0] == "细分原因"]
        lines.extend([f"### {group_label(item.candidate)}（中位设定）的细分原因", "",
                      *_table(("原因", "天数", "占非绿天数"), rows), ""])
    spread = []
    for group in REASON_GROUPS:
        own = [item.reasons for item in settings if group_of(item.candidate) == group and item.reasons]
        cells = []
        for name in MAIN_CLASSES:
            shares = [Decimal(counts.main[name]) / counts.non_green for counts in own]
            cells.append(f"{_share(min(shares))}–{_share(max(shares))}")
        label = group_label(next(item.candidate for item in settings if group_of(item.candidate) == group))
        spread.append((label, len(own), *cells))
    lines.extend(["### 全部设定的主分类范围", "",
                  *_table(("模型·版本", "设定数", *MAIN_CLASSES), spread), "",
                  "逐设定的主分类与细分原因见 `lighting_reasons.csv`。", ""])
    return lines


def _rebound_rows(rows: Sequence[Row]) -> list[Row]:
    pair = lambda first, second: f"{str(first)[:8] or '—'}/{str(second)[:8] or '—'}"  # noqa: E731
    return [(row[0], row[1], row[2], row[3], f"{row[4]}/{row[5]}/{row[6]}", f"{row[7]}（{_share(row[8])}）",
             pair(row[9], row[10]), pair(row[11], row[12]), pair(row[13], row[14])) for row in rows]


def ma200_lines(event_rows: Sequence[Row], bear_rows: Sequence[Row], extremes: Mapping[str, str]) -> list[str]:
    """200日均线参照的事件表现与两次熊市对照。"""
    bear = [(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]) for row in bear_rows]
    return [
        "## 三、200日均线参照的事件表现", "",
        "纳入事件与模型相同：开发期为 P ≥ τ 的已确认事件；补充历史为 P ≥ τ′ 且 Tr ≤ 2009-09-30 的已确认事件。"
        "各组中位设定并列作对照。R 为主口径（类别②）的中位数/P75；保守口径把类别③按超限计。", "",
        *_table(("范围", "行", "资产", "纳入", "①/②/③", "半山腰事件数（发生率）", "半山腰跌幅 中位/最大",
                 "R 中位/P75", "保守 R 中位/P75"), _rebound_rows(event_rows)), "",
        "### 补充历史中的两次熊市", "",
        "期间按 SPX 收盘价的高点到低点划定，两个资产共用。" + extremes["note"],
        "回撤按每个资产的累计对数收益 Σ e_j·r_j 计算（对数单位）；始终绿一行即指数本身。"
        "主损失为期间内各区间的主损失之和。", "",
        *_table(("期间", "行", "区间数", "平均执行暴露", "期间主损失", "SPX 按暴露累计对数收益", "SPX 最大回撤",
                 "QQQ 按暴露累计对数收益", "QQQ 最大回撤"), bear), ""]


def yearly_lines(development: Sequence[Row], extended: Sequence[Row]) -> list[str]:
    columns = ("行", "年份", "区间数", "主损失", "ē", "择时得分 T", "非绿占比")
    render = lambda rows: [(*row[:6], _share(row[6])) for row in rows]  # noqa: E731
    return [
        "## 四、逐年对照", "",
        "区间按起点日期归年。逐年 T 用当年自己的平均执行暴露与当年的 L_G、L_R，**各年 T 之和不等于全期 T**；"
        "每行末尾另列“全期”。不完整年份照列：开发期的 2009 年只有 1 个区间（j₀=2009-12-31），"
        "补充历史的 1999 年自 j₀′ 起、2009 年至窗口末日。区间数很少的年份数值没有参考意义。", "",
        "### 开发期（2009-12-31 至 2016-12-30）", "", *_table(columns, render(development)), "",
        "### 补充历史（1999-09-07 至 2009-09-30，只有不依赖广度的 P0 与 200 日均线）", "",
        *_table(columns, render(extended)), "",
        "全部设定的逐年结果见 `yearly_all_settings.csv`。", ""]


def report_lines(prepared: PreparedEvaluation, window: PreparedEvaluation, development: DevelopmentDiagnostics,
                 extended: ExtendedDiagnostics, ma200_event_rows: Sequence[Row],
                 extremes: Mapping[str, str]) -> list[str]:
    return [
        f"# v1.3 描述性诊断（{NOTICE}）", "",
        f"**{NOTICE}。** 不改任何规则，不选参，不锁定，没有运行验证期，没有读取保留期研究数据。"
        "需要“代表设定”的地方一律用网格中位设定 K=5、θ_P=2%、q=10%，并另附全部设定的汇总，以避免挑选。"
        "数字只作描述，不能称为历史预警效果。", "",
        f"开发期：t0={prepared.t0}，τ={prepared.tau}，j₀={prepared.first_loss_day}，窗口末日 "
        f"{prepared.inputs.days[-1]}。补充历史：t0′={window.t0}，τ′={window.tau}，j₀′={window.first_loss_day}，"
        f"窗口末日 {window.inputs.days[-1]}。", "",
        *bottleneck_lines(development.settings), *reason_lines(development.settings),
        *ma200_lines(ma200_event_rows, extended.bear_markets, extremes),
        *yearly_lines(development.yearly, extended.yearly)]


def extreme_note(days: Sequence[dt.date], closes: Mapping[dt.date, Decimal],
                 periods: Sequence[tuple[str, dt.date, dt.date]], margin: int = 60) -> str:
    """QQQ 自身的极值日：在每个期间前后各 margin 个交易日内，取最高收盘日及其后的最低收盘日。"""
    parts = []
    for label, start, end in periods:
        first, last = max(days.index(start) - margin, 0), min(days.index(end) + margin, len(days) - 1)
        scoped = [(day, closes[day]) for day in days[first:last + 1] if day in closes]
        peak = max(scoped, key=lambda item: item[1])[0]
        trough = min((item for item in scoped if item[0] >= peak), key=lambda item: item[1])[0]
        parts.append(f"{label}：QQQ 自身的最高收盘日为 {peak}、其后最低收盘日为 {trough}")
    return "QQQ 自身的极值日略有不同（在各期间前后 60 个交易日内查找）——" + "；".join(parts) + "。"
