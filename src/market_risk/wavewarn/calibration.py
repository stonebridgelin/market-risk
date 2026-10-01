"""开发期 E 退出代价的固定延迟参照（纯计算）；不产生候选规则或选参结果。

读取与写出在 calibration_run.py；本模块不读写文件，也不导入读写模块。
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from market_risk.wavewarn.labels_zz import ZZEvent

DELAYS = (0, 1, 2, 3, 5, 8, 10, 15)
INFINITY = Decimal("Infinity")
DelayClass = Literal["②", "③", "后续窗口不足"]
EventWithNext = tuple[ZZEvent, dt.date | None]
Row = tuple[object, ...]


@dataclass(frozen=True)
class FixedDelayResult:
    symbol: str
    peak_date: dt.date
    trough_date: dt.date
    delay: int
    classification: DelayClass
    execution_date: dt.date | None
    recovery: Decimal | None


@dataclass(frozen=True)
class Distribution:
    n: int
    minimum: Decimal | None
    p25: Decimal | None
    median: Decimal | None
    p75: Decimal | None
    maximum: Decimal | None


@dataclass(frozen=True)
class CalibrationTables:
    """校准的全部表格行；由边界层写出。"""

    event_rows: tuple[Row, ...]
    fixed_rows: tuple[Row, ...]
    fixed_summary: tuple[Row, ...]
    actual_rows: tuple[Row, ...]
    actual_summary: tuple[Row, ...]


def linear_percentile(values: Sequence[Decimal], probability: Decimal) -> Decimal | None:
    """线性插值；正无穷参与非零权重的插值结果仍为正无穷。"""
    if not values:
        return None
    if not Decimal(0) <= probability <= Decimal(1):
        raise ValueError("分位数概率须在 0 至 1 之间")
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * probability
    left = int(position)
    fraction = position - left
    if fraction == 0:
        return ordered[left]
    if ordered[left].is_infinite() or ordered[left + 1].is_infinite():
        return INFINITY
    return ordered[left] * (1 - fraction) + ordered[left + 1] * fraction


def distribution(values: Sequence[Decimal]) -> Distribution:
    """同时支持有限 R 与保守口径中的“超限”哨兵。"""
    return Distribution(len(values), min(values, default=None), linear_percentile(values, Decimal("0.25")),
                        linear_percentile(values, Decimal("0.5")), linear_percentile(values, Decimal("0.75")),
                        max(values, default=None))


def fixed_delay_reference(days: Sequence[dt.date], closes: Mapping[dt.date, Decimal], event: ZZEvent,
                          next_t0: dt.date | None, delay: int) -> FixedDelayResult:
    """先判下一 T0，后判期末；不为窗口外日期读取价格。"""
    if delay < 0 or event.trough_date not in days:
        raise ValueError("固定延迟或低点日期错误")
    position = days.index(event.trough_date) + delay
    execution = days[position] if position < len(days) else None
    if next_t0 is not None and (execution is None or execution >= next_t0):
        return FixedDelayResult(event.symbol, event.peak_date, event.trough_date, delay, "③", execution, None)
    if execution is None:
        return FixedDelayResult(event.symbol, event.peak_date, event.trough_date, delay,
                                "后续窗口不足", None, None)
    close = closes.get(execution)
    if close is None:
        raise ValueError(f"固定延迟参照缺少 {event.symbol} {execution} 收盘价；不得填补")
    if event.peak_close == event.trough_close:
        raise ValueError("事件高低点相等，R 分母为零")
    recovery = (close - event.trough_close) / (event.peak_close - event.trough_close)
    return FixedDelayResult(event.symbol, event.peak_date, event.trough_date, delay, "②", execution, recovery)


def threshold_comparisons(finite: Sequence[Decimal], class_three: int) -> tuple[bool | None, bool | None, bool | None,
                                                                                 Distribution, Distribution]:
    """三个描述性判断；③仅在保守列用正无穷排序，不伪造 R。"""
    main = distribution(finite)
    conservative = distribution((*finite, *(INFINITY for _ in range(class_three))))
    median_ok = main.median <= Decimal("0.60") if main.median is not None else None
    both_ok = (main.median <= Decimal("0.60") and main.p75 <= Decimal("1.00")
               if main.median is not None and main.p75 is not None else None)
    conservative_ok = (conservative.median <= Decimal("0.60") and conservative.p75 <= Decimal("1.00")
                       if conservative.median is not None and conservative.p75 is not None else None)
    return median_ok, both_ok, conservative_ok, main, conservative


def delay_boundary(rows: Sequence[tuple[int, bool | None]]) -> tuple[int | None, int | None, tuple[int, ...]]:
    """只在已登记离散 m 上找满足最大值与不满足最小值。"""
    passing = tuple(delay for delay, result in rows if result is True)
    failing = tuple(delay for delay, result in rows if result is False)
    return max(passing, default=None), min(failing, default=None), passing


def shown(value: Decimal | None) -> str:
    """分布表的显示值：缺失留空，正无穷显示“超限”。"""
    if value is None:
        return ""
    return "超限" if value.is_infinite() else str(value)


def distribution_cells(values: Distribution) -> tuple[str | int, ...]:
    return (values.n, shown(values.minimum), shown(values.p25), shown(values.median),
            shown(values.p75), shown(values.maximum))


def event_end_row(event: ZZEvent, next_t0: dt.date | None, end_close: Decimal) -> Row:
    """事件结束日的 R_end 与事件下跌幅度（均为比例）。"""
    if event.end_date is None:
        raise ValueError("纳入事件缺少结束日")
    drop = (event.peak_close - event.trough_close) / event.peak_close
    end_recovery = (end_close - event.trough_close) / (event.peak_close - event.trough_close)
    return (event.symbol, event.peak_date, event.trough_date, event.end_date, next_t0 or "",
            event.peak_close, event.trough_close, end_close, drop, end_recovery)


def fixed_delay_results(days: Sequence[dt.date], closes: Mapping[dt.date, Decimal],
                        selected: Sequence[EventWithNext]) -> tuple[FixedDelayResult, ...]:
    """每个纳入事件 × 每个登记延迟一条，事件在外层、延迟在内层。"""
    return tuple(fixed_delay_reference(days, closes, event, next_t0, delay)
                 for event, next_t0 in selected for delay in DELAYS)


def fixed_delay_row(item: FixedDelayResult) -> Row:
    return (item.symbol, item.peak_date, item.trough_date, item.delay, item.classification,
            item.execution_date or "", item.recovery if item.recovery is not None else "")


def fixed_delay_summary_row(symbol: str, delay: int, items: Sequence[FixedDelayResult],
                            included: int) -> Row:
    """某资产某延迟的②/③/窗口不足计数、主分布、保守分布与三种口径判断。"""
    finite = [item.recovery for item in items if item.recovery is not None]
    three = sum(item.classification == "③" for item in items)
    short = sum(item.classification == "后续窗口不足" for item in items)
    if len(finite) + three + short != included:
        raise ValueError("固定延迟三类未穷尽纳入事件")
    median_ok, both_ok, conservative_ok, main, conservative = threshold_comparisons(finite, three)
    return (symbol, delay, included, len(finite), three, short,
            *distribution_cells(main), *distribution_cells(conservative),
            median_ok, both_ok, conservative_ok)


def actual_delay_tables(exit_rows: Sequence[Mapping[str, str]],
                        events: Mapping[tuple[str, dt.date, dt.date], ZZEvent],
                        days: Sequence[dt.date]) -> tuple[tuple[Row, ...], tuple[Row, ...]]:
    """实际首个绿灯执行日 g 相对 Tr 的延迟；②按 g≤End 与 g>End 分列，③只计数。"""
    grouped: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
    counts: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    rows: list[Row] = []
    for row in exit_rows:
        if row["inclusion"] != "纳入":
            continue
        key = (row["symbol"], dt.date.fromisoformat(row["peak_date"]), dt.date.fromisoformat(row["trough_date"]))
        event = events.get(key)
        if event is None or event.end_date is None:
            raise ValueError("实际退出事件与校准事件不一致")
        group = (row["scenario"], row["k"], row["theta_p"], row["symbol"])
        if row["rebound_class"] == "②低点或之后转绿":
            green = dt.date.fromisoformat(row["first_green_from_trough"])
            delay = days.index(green) - days.index(event.trough_date)
            grouped[group].append(delay)
            counts[group][0 if green <= event.end_date else 1] += 1
            rows.append((*group, event.peak_date, event.trough_date, green, delay,
                         "g≤End" if green <= event.end_date else "g>End"))
        elif row["rebound_class"] == "③下一事件前未转绿":
            counts[group][2] += 1
    summary = tuple((*group, counts[group][0], counts[group][1], counts[group][2],
                     *distribution_cells(distribution(tuple(Decimal(value) for value in grouped[group]))))
                    for group in sorted(counts))
    return tuple(rows), summary


def calibration_tables(days: Sequence[dt.date], closes: Mapping[str, Mapping[dt.date, Decimal]],
                       included: Mapping[str, Sequence[EventWithNext]],
                       exit_rows: Sequence[Mapping[str, str]]) -> CalibrationTables:
    """由纳入事件、价格与既有逐事件退出代价行生成全部校准表。"""
    event_rows: list[Row] = []
    fixed_rows: list[Row] = []
    fixed_summary: list[Row] = []
    for symbol in ("SPX", "QQQ"):
        for event, next_t0 in included[symbol]:
            end_close = closes[symbol].get(event.end_date) if event.end_date is not None else None
            if end_close is None:
                raise ValueError(f"事件结束日缺价：{symbol} {event.end_date}")
            event_rows.append(event_end_row(event, next_t0, end_close))
        results = fixed_delay_results(days, closes[symbol], included[symbol])
        fixed_rows.extend(fixed_delay_row(item) for item in results)
        for delay in DELAYS:
            fixed_summary.append(fixed_delay_summary_row(
                symbol, delay, [item for item in results if item.delay == delay], len(included[symbol])))
    event_map = {(event.symbol, event.peak_date, event.trough_date): event
                 for symbol in ("SPX", "QQQ") for event, _ in included[symbol]}
    actual_rows, actual_summary = actual_delay_tables(exit_rows, event_map, days)
    return CalibrationTables(tuple(event_rows), tuple(fixed_rows), tuple(fixed_summary),
                             actual_rows, actual_summary)


def event_section(event_rows: Sequence[Row]) -> list[str]:
    lines = ["## 事件结束日与下跌幅度", "",
             "| 资产 | 指标 | n | 最小 | P25 | 中位 | P75 | 最大 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for symbol in ("SPX", "QQQ"):
        own = [row for row in event_rows if row[0] == symbol]
        for name, position in (("R_end", 9), ("事件跌幅", 8)):
            cells = distribution_cells(distribution(tuple(row[position] for row in own)))  # type: ignore[misc]
            lines.append(f"| {symbol} | {name} | " + " | ".join(map(str, cells)) + " |")
    return lines


def fixed_section(fixed_summary: Sequence[Row]) -> list[str]:
    lines = ["", "## 固定延迟参照", "", "Tr+m 为执行日，m 仅取 0/1/2/3/5/8/10/15 个交易日。"
             "先以同资产下一事件 T0 判③，再以开发期末判后续窗口不足。"
             "这不是候选规则，不改变任何登记。"
             "此规则只适用于固定延迟参照；实际转绿到窗口末日仍未转绿仍归类别③。", "",
             "| 资产 | m | ②/③/窗口不足 | 主 R 中位/P75 | 保守 R 中位/P75 | "
             "仅中位合格 | 双条件合格 | 保守双条件合格 |",
             "|---|---:|---|---|---|---|---|---|"]
    for row in fixed_summary:
        lines.append(f"| {row[0]} | {row[1]} | {row[3]}/{row[4]}/{row[5]} | "
                     f"{row[9]}/{row[10]} | {row[15]}/{row[16]} | {row[18]} | {row[19]} | {row[20]} |")
    lines.extend(["", "各资产在已登记离散 m 下的描述性边界；未计算的天数不作推断，非单调时也不插值：", "",
                  "| 资产 | 口径 | 满足的全部 m | 最大满足 m | 最小不满足 m |",
                  "|---|---|---|---:|---:|"])
    for symbol in ("SPX", "QQQ"):
        own = [row for row in fixed_summary if row[0] == symbol]
        for name, position in (("仅中位数≤0.60", 18), ("中位数≤0.60且P75≤1.00", 19), ("保守双条件", 20)):
            top, bottom, all_passing = delay_boundary([(row[1], row[position]) for row in own])  # type: ignore[misc]
            lines.append(f"| {symbol} | {name} | {','.join(map(str, all_passing)) or '无'} | "
                         f"{top if top is not None else '无'} | {bottom if bottom is not None else '无'} |")
    return lines


def _actual_section(actual_summary: Sequence[Row]) -> list[str]:
    lines = ["", "## 实际首个绿灯延迟", "", "②类事件的 g−Tr 以交易日数计；③未在下一事件前转绿，"
             "不进入延迟分布。各组完整分位数见 `E退出校准_实际延迟汇总.csv`。", "",
             "| 场景 | K | θ_P | 资产 | ② g≤End / g>End | ③ | 延迟 n/中位/P75/最大 |",
             "|---|---:|---:|---|---|---:|---|"]
    for row in actual_summary:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]}/{row[5]} | {row[6]} "
                     f"| {row[7]}/{row[10]}/{row[11]}/{row[12]} |")
    lines.extend(["", "逐事件结束日 R、固定参照与实际延迟的明细均在同目录 CSV，可按日期、资产和设定复算。", ""])
    return lines


def report_lines(tau_e: dt.date, included: Mapping[str, int], tables: CalibrationTables) -> list[str]:
    """校准报告正文（Markdown 行）。"""
    lines = ["# E 退出代价校准（开发期描述性参照）", "",
             "仅使用截至 2016-12-30 的价格、既有 ZZ 标签和退出代价纳入事件；不计算主损失、"
             "不挑选 K、θ_P、q 或 E，也不构成验证期或实盘效果。", "",
             f"统一 τ_E：{tau_e}。SPX 纳入 {included['SPX']} 件；QQQ 纳入 {included['QQQ']} 件。", "",
             "R 与事件下跌幅度均为比例（1=100%）。类别③在保守分布中仅作正无穷排序，"
             "不是实际 R；线性分位数插值涉及③时记“超限”。后续窗口不足不进入分布。", ""]
    return [*lines, *event_section(tables.event_rows), *fixed_section(tables.fixed_summary),
            *_actual_section(tables.actual_summary)]
