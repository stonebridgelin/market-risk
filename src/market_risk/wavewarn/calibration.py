"""开发期 E 退出代价的固定延迟参照；不产生候选规则或选参结果。"""

from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.exit_costs import _read_events, event_inclusion
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import ZZEvent

DELAYS = (0, 1, 2, 3, 5, 8, 10, 15)
INFINITY = Decimal("Infinity")
DelayClass = Literal["②", "③", "后续窗口不足"]


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


def _shown(value: Decimal | None) -> str:
    if value is None:
        return ""
    return "超限" if value.is_infinite() else str(value)


def _distribution_cells(values: Distribution) -> tuple[str | int, ...]:
    return (values.n, _shown(values.minimum), _shown(values.p25), _shown(values.median),
            _shown(values.p75), _shown(values.maximum))


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerows(rows)


def run_exit_calibration(root: Path) -> Path:
    """基于既有 τ_E 与纳入事件，输出开发期描述性校准。"""
    config = load_wavewarn_config(root / "config/wavewarn_v121.yaml")
    inputs = load_development_inputs(root, config.development_end())
    base = root / "reports/research/wavewarn_v121"
    with (base / "exit_cost_summary_development.csv").open(encoding="utf-8-sig", newline="") as file:
        summary_rows = list(csv.DictReader(file))
    tau_values = {dt.date.fromisoformat(row["tau_e"]) for row in summary_rows}
    if len(tau_values) != 1:
        raise ValueError("退出代价汇总的 τ_E 不一致")
    tau_e = next(iter(tau_values))
    with (base / "convergence_diagnostics.csv").open(encoding="utf-8-sig", newline="") as file:
        t0_values = {dt.date.fromisoformat(row["t0"]) for row in csv.DictReader(file)
                     if row["scenario"].startswith("P1-")}
    if len(t0_values) != 1:
        raise ValueError("P1 的 t0 不一致")
    t0 = next(iter(t0_values))
    events = _read_events(base / "zz_events_development.csv", config.development_end())
    included: dict[str, tuple[tuple[ZZEvent, dt.date | None], ...]] = {}
    event_rows: list[tuple[object, ...]] = []
    fixed_rows: list[tuple[object, ...]] = []
    fixed_grouped: dict[tuple[str, int], list[FixedDelayResult]] = defaultdict(list)
    for symbol in ("SPX", "QQQ"):
        selected = []
        for index, event in enumerate(events[symbol]):
            if event_inclusion(event, t0, tau_e) != "纳入":
                continue
            if event.end_date is None:
                raise ValueError("纳入事件缺少结束日")
            next_t0 = events[symbol][index + 1].t0_date if index + 1 < len(events[symbol]) else None
            selected.append((event, next_t0))
            end_close = inputs.series[symbol].get(event.end_date)
            if end_close is None:
                raise ValueError(f"事件结束日缺价：{symbol} {event.end_date}")
            drop = (event.peak_close - event.trough_close) / event.peak_close
            end_recovery = (end_close - event.trough_close) / (event.peak_close - event.trough_close)
            event_rows.append((symbol, event.peak_date, event.trough_date, event.end_date, next_t0 or "",
                               event.peak_close, event.trough_close, end_close, drop, end_recovery))
            for delay in DELAYS:
                item = fixed_delay_reference(inputs.days, inputs.series[symbol], event, next_t0, delay)
                fixed_grouped[(symbol, delay)].append(item)
                fixed_rows.append((symbol, event.peak_date, event.trough_date, delay, item.classification,
                                   item.execution_date or "", item.recovery if item.recovery is not None else ""))
        included[symbol] = tuple(selected)
    _write_csv(base / "E退出校准_事件.csv",
               ("symbol", "peak_date", "trough_date", "end_date", "next_t0", "peak_close", "trough_close",
                "end_close", "event_decline_fraction", "r_end"), event_rows)
    _write_csv(base / "E退出校准_固定延迟明细.csv",
               ("symbol", "peak_date", "trough_date", "m", "classification", "execution_date", "r"), fixed_rows)
    fixed_summary = []
    for symbol in ("SPX", "QQQ"):
        for delay in DELAYS:
            items = fixed_grouped[(symbol, delay)]
            finite = [item.recovery for item in items if item.recovery is not None]
            three = sum(item.classification == "③" for item in items)
            short = sum(item.classification == "后续窗口不足" for item in items)
            if len(finite) + three + short != len(included[symbol]):
                raise ValueError("固定延迟三类未穷尽纳入事件")
            median_ok, both_ok, conservative_ok, main, conservative = threshold_comparisons(finite, three)
            fixed_summary.append((symbol, delay, len(included[symbol]), len(finite), three, short,
                                  *_distribution_cells(main), *_distribution_cells(conservative),
                                  median_ok, both_ok, conservative_ok))
    fixed_header = ("symbol", "m", "included", "class_2", "class_3", "window_short",
                    "main_n", "main_min", "main_p25", "main_median", "main_p75", "main_max",
                    "conservative_n", "conservative_min", "conservative_p25", "conservative_median",
                    "conservative_p75", "conservative_max", "median_le_060", "both_limits", "conservative_both")
    _write_csv(base / "E退出校准_固定延迟汇总.csv", fixed_header, fixed_summary)
    event_map = {(event.symbol, event.peak_date, event.trough_date): event
                 for symbol in ("SPX", "QQQ") for event, _ in included[symbol]}
    actual_grouped: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
    actual_counts: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    actual_rows: list[tuple[object, ...]] = []
    with (base / "exit_cost_events_development.csv").open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row["inclusion"] != "纳入":
                continue
            key = (row["symbol"], dt.date.fromisoformat(row["peak_date"]),
                   dt.date.fromisoformat(row["trough_date"]))
            event = event_map.get(key)
            if event is None:
                raise ValueError("实际退出事件与校准事件不一致")
            group = (row["scenario"], row["k"], row["theta_p"], row["symbol"])
            classification = row["rebound_class"]
            if classification == "②低点或之后转绿":
                green = dt.date.fromisoformat(row["first_green_from_trough"])
                delay = inputs.days.index(green) - inputs.days.index(event.trough_date)
                actual_grouped[group].append(delay)
                actual_counts[group][0 if green <= event.end_date else 1] += 1
                actual_rows.append((*group, event.peak_date, event.trough_date, green, delay,
                                    "g≤End" if green <= event.end_date else "g>End"))
            elif classification == "③下一事件前未转绿":
                actual_counts[group][2] += 1
    _write_csv(base / "E退出校准_实际延迟明细.csv",
               ("scenario", "k", "theta_p", "symbol", "peak_date", "trough_date", "g", "delay_days",
                "relative_to_end"), actual_rows)
    actual_summary = []
    for group in sorted(actual_counts):
        values = distribution(tuple(Decimal(value) for value in actual_grouped[group]))
        actual_summary.append((*group, actual_counts[group][0], actual_counts[group][1],
                               actual_counts[group][2], *_distribution_cells(values)))
    _write_csv(base / "E退出校准_实际延迟汇总.csv",
               ("scenario", "k", "theta_p", "symbol", "g_le_end", "g_gt_end", "class_3",
                "delay_n", "delay_min", "delay_p25", "delay_median", "delay_p75", "delay_max"), actual_summary)
    lines = ["# E 退出代价校准（开发期描述性参照）", "",
             "仅使用截至 2016-12-30 的价格、既有 ZZ 标签和退出代价纳入事件；不计算主损失、"
             "不挑选 K、θ_P、q 或 E，也不构成验证期或实盘效果。", "",
             f"统一 τ_E：{tau_e}。SPX 纳入 {len(included['SPX'])} 件；QQQ 纳入 {len(included['QQQ'])} 件。", "",
             "R 与事件下跌幅度均为比例（1=100%）。类别③在保守分布中仅作正无穷排序，"
             "不是实际 R；线性分位数插值涉及③时记“超限”。后续窗口不足不进入分布。", "",
             "## 事件结束日与下跌幅度", "",
             "| 资产 | 指标 | n | 最小 | P25 | 中位 | P75 | 最大 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for symbol in ("SPX", "QQQ"):
        own = [row for row in event_rows if row[0] == symbol]
        for name, position in (("R_end", 9), ("事件跌幅", 8)):
            cells = _distribution_cells(distribution(tuple(row[position] for row in own)))
            lines.append(f"| {symbol} | {name} | " + " | ".join(map(str, cells)) + " |")
    lines.extend(["", "## 固定延迟参照", "", "Tr+m 为执行日，m 仅取 0/1/2/3/5/8/10/15 个交易日。"
                  "先以同资产下一事件 T0 判③，再以开发期末判后续窗口不足。"
                  "这不是候选规则，不改变任何登记。", "",
                  "| 资产 | m | ②/③/窗口不足 | 主 R 中位/P75 | 保守 R 中位/P75 | "
                  "仅中位合格 | 双条件合格 | 保守双条件合格 |",
                  "|---|---:|---|---|---|---|---|---|"])
    for row in fixed_summary:
        lines.append(f"| {row[0]} | {row[1]} | {row[3]}/{row[4]}/{row[5]} | "
                     f"{row[9]}/{row[10]} | {row[15]}/{row[16]} | {row[18]} | {row[19]} | {row[20]} |")
    lines.extend(["", "各资产在已登记离散 m 下的描述性边界；未计算的天数不作推断，非单调时也不插值：", "",
                  "| 资产 | 口径 | 满足的全部 m | 最大满足 m | 最小不满足 m |",
                  "|---|---|---|---:|---:|"])
    for symbol in ("SPX", "QQQ"):
        own = [row for row in fixed_summary if row[0] == symbol]
        for name, position in (("仅中位数≤0.60", 18), ("中位数≤0.60且P75≤1.00", 19),
                               ("保守双条件", 20)):
            top, bottom, all_passing = delay_boundary([(row[1], row[position]) for row in own])
            lines.append(f"| {symbol} | {name} | {','.join(map(str, all_passing)) or '无'} | "
                         f"{top if top is not None else '无'} | {bottom if bottom is not None else '无'} |")
    lines.extend(["", "## 实际首个绿灯延迟", "", "②类事件的 g−Tr 以交易日数计；③未在下一事件前转绿，"
                  "不进入延迟分布。各组完整分位数见 `E退出校准_实际延迟汇总.csv`。", "",
                  "| 场景 | K | θ_P | 资产 | ② g≤End / g>End | ③ | 延迟 n/中位/P75/最大 |",
                  "|---|---:|---:|---|---|---:|---|"])
    for row in actual_summary:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]}/{row[5]} | {row[6]} "
                     f"| {row[7]}/{row[10]}/{row[11]}/{row[12]} |")
    lines.extend(["", "逐事件结束日 R、固定参照与实际延迟的明细均在同目录 CSV，可按日期、资产和设定复算。", ""])
    report = base / "E退出代价_校准.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(run_exit_calibration(Path.cwd()))
