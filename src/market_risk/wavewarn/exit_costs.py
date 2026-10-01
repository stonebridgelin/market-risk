"""E1/E2/E3 的开发期退出代价；只作逐事件描述，不参与选参。"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.execution import ExecutionDay, execute_asset
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import ZZEvent
from market_risk.wavewarn.ledgers import classify_asset_event

ReboundClass = Literal["①低点前已绿", "②低点或之后转绿", "③下一事件前未转绿"]


@dataclass(frozen=True)
class GreenExecution:
    signal_date: dt.date
    execution_date: dt.date
    signal_close: Decimal | None
    execution_close: Decimal
    timing: str
    rebound_from_trough_percent: Decimal
    decline_to_trough: Decimal | None
    signal_decline_to_trough: Decimal | None
    role: str


@dataclass(frozen=True)
class ExitCostEvent:
    symbol: str
    peak_date: dt.date
    trough_date: dt.date
    inclusion: str
    rebound_class: ReboundClass | None
    green_at_trough_subclass: str | None
    half_way_green_count: int
    deepest_decline: Decimal | None
    re_alerted_before_trough: bool | None
    rebound_recovery: Decimal | None
    raw_rebound_percent: Decimal | None
    first_green_from_trough: dt.date | None
    green_executions: tuple[GreenExecution, ...]


def common_exit_start(days: Sequence[dt.date], t0: dt.date,
                      convergence_dates: Sequence[dt.date]) -> dt.date:
    """τ_E 为 t0+63 交易日与 27 套 P1 系统收敛日之最大者。"""
    if len(convergence_dates) != 27:
        raise ValueError("τ_E 需要 P1 九组乘 E1/E2/E3 的 27 个收敛日")
    if t0 not in days or days.index(t0) + 63 >= len(days):
        raise ValueError("t0 后不足 63 个交易日")
    if any(day not in days or day < t0 for day in convergence_dates):
        raise ValueError("P1 系统收敛日缺失或早于 t0")
    return max(days[days.index(t0) + 63], *convergence_dates)


def event_inclusion(event: ZZEvent, t0: dt.date, tau_e: dt.date) -> str:
    """右截尾优先独立计数；已确认事件按高点日期决定纳入。"""
    if event.right_censored:
        return "右截尾"
    if event.peak_date < tau_e:
        if event.trough_date < t0:
            return "状态窗口不足：t0 前"
        if event.peak_date < t0:
            return "状态窗口不足：跨 t0"
        return "状态窗口不足：t0 至 τ_E"
    return "纳入"


def event_ledger_inclusion(event: ZZEvent, days: Sequence[dt.date], t0: dt.date,
                           tau_e: dt.date) -> str:
    """五类事件账还须有从 P 前20日开始、且不早于 τ_E 的完整灯色。"""
    basic = event_inclusion(event, t0, tau_e)
    if basic != "纳入":
        return basic
    positions = {day: index for index, day in enumerate(days)}
    if event.peak_date not in positions or tau_e not in positions:
        raise ValueError("事件账高点或 τ_E 不在交易日轴")
    peak = positions[event.peak_date]
    if peak < 20 or peak - 20 < positions[tau_e]:
        return "状态窗口不足：P−20 早于 τ_E"
    return "纳入"


def exit_cost_for_event(days: Sequence[dt.date], executions: Sequence[ExecutionDay],
                        closes: Sequence[Decimal | None], event: ZZEvent,
                        next_t0: dt.date | None, t0: dt.date,
                        tau_e: dt.date) -> ExitCostEvent:
    """按最后下跌区间的执行状态 S_(Tr−2) 分类；R 包含 g=Tr 的零值。"""
    if not (len(days) == len(executions) == len(closes)) or tuple(days) != tuple(sorted(set(days))):
        raise ValueError("退出代价输入时间轴不一致")
    inclusion = event_inclusion(event, t0, tau_e)
    empty = ExitCostEvent(event.symbol, event.peak_date, event.trough_date, inclusion,
                          None, None, 0, None, None, None, None, None, ())
    if inclusion != "纳入":
        return empty
    positions = {day: index for index, day in enumerate(days)}
    if event.peak_date not in positions or event.trough_date not in positions:
        raise ValueError("纳入事件的高点与低点须位于执行时间轴")
    peak, trough = positions[event.peak_date], positions[event.trough_date]
    if closes[peak] is None or closes[trough] is None:
        raise ValueError("纳入事件的高点与低点收盘价不可缺失")
    if closes[peak] != event.peak_close or closes[trough] != event.trough_close:
        raise ValueError("事件价格与开发期价格序列不一致")
    all_green_indices = [index for index in range(peak, len(days))
                         if index > 0 and executions[index].executed == "绿"
                         and executions[index - 1].executed != "绿"]
    green_indices = [index for index in all_green_indices if next_t0 is None or days[index] < next_t0]
    rows: list[GreenExecution] = []
    for index in green_indices:
        close = closes[index]
        if close is None:
            raise ValueError("转绿执行日缺少收盘价")
        signal_close = closes[index - 1]
        timing = "早于低点" if index < trough else "低点当日" if index == trough else "低点之后"
        decline = 1 - event.trough_close / close if index < trough else None
        signal_decline = (1 - event.trough_close / signal_close
                          if index < trough and signal_close is not None else None)
        rows.append(GreenExecution(days[index - 1], days[index], signal_close, close, timing,
                                   (close / event.trough_close - 1) * 100,
                                   decline, signal_decline,
                                   "本事件[P,Tr)内" if index < trough else "低点后窗口"))
    before = tuple(row for row in rows if row.execution_date < event.trough_date)
    re_alerted = (any(executions[index].executed != "绿"
                      for row in before for index in range(positions[row.execution_date] + 1, trough))
                  if before else None)
    if trough < 1 or peak < 1:
        raise ValueError("分类需要 P−1 与 Tr−1 的执行灯色")
    if executions[trough - 1].executed == "绿":
        category: ReboundClass = "①低点前已绿"
        continuous = all(executions[index].executed == "绿" for index in range(peak - 1, trough))
        subclass = "①a 高点前已绿并持续" if continuous else "①b 低点前转绿"
        if not continuous and not before:
            raise ValueError("类别①b 缺少 [P,Tr) 内转绿执行切换")
        first_after = None
        recovery = raw_rebound = None
    else:
        subclass = None
        future = next((row for row in rows if row.execution_date >= event.trough_date), None)
        first_late = next((days[index] for index in all_green_indices if index >= trough), None)
        first_after = first_late
        if future is not None and (next_t0 is None or future.execution_date < next_t0):
            category = "②低点或之后转绿"
            recovery = (future.execution_close - event.trough_close) / (event.peak_close - event.trough_close)
            raw_rebound = (future.execution_close / event.trough_close - 1) * 100
        else:
            category = "③下一事件前未转绿"
            recovery = raw_rebound = None
    return ExitCostEvent(event.symbol, event.peak_date, event.trough_date, inclusion,
                         category, subclass, len(before),
                         max((row.decline_to_trough for row in before if row.decline_to_trough is not None),
                             default=None),
                         re_alerted, recovery, raw_rebound, first_after, tuple(rows))


def event_denominators(events: Sequence[ZZEvent], rows: Sequence[ExitCostEvent],
                       tail_pending: int = 0) -> Mapping[str, int]:
    """四类互斥分母；期末寻峰尾段没有 ZZEvent，单独传入计数。"""
    if len(events) != len(rows) or tail_pending < 0:
        raise ValueError("事件与退出代价数量不一致")
    result = {"纳入": 0, "状态窗口不足": 0, "右截尾": 0, "尾段未定": tail_pending}
    for event, row in zip(events, rows, strict=True):
        if event.symbol != row.symbol or event.peak_date != row.peak_date:
            raise ValueError("事件与退出代价未对齐")
        key = "状态窗口不足" if row.inclusion.startswith("状态窗口不足") else row.inclusion
        if key not in result or key == "尾段未定":
            raise ValueError("退出代价事件归类错误")
        result[key] += 1
    return result


def independent_green_execution_count(rows: Sequence[ExitCostEvent]) -> int:
    """同场景同资产内，跨事件重复出现的转绿执行日只计一次。"""
    symbols = {row.symbol for row in rows}
    if len(symbols) > 1:
        raise ValueError("去重须按单资产分别执行")
    return len({item.execution_date for row in rows for item in row.green_executions})


def rebound_values(rows: Sequence[ExitCostEvent], exclude_trough_day: bool = False) -> tuple[Decimal, ...]:
    """主口径含 g=Tr 的 R=0；敏感性仅剔除这些事件。"""
    return tuple(row.rebound_recovery for row in rows
                 if row.rebound_recovery is not None
                 and (not exclude_trough_day or row.first_green_from_trough != row.trough_date))


def _linear_percentile(values: Sequence[Decimal], probability: Decimal) -> Decimal | None:
    """描述性分位点采用有序样本的线性插值。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    left = int(position)
    fraction = position - left
    return ordered[left] if fraction == 0 else ordered[left] * (1 - fraction) + ordered[left + 1] * fraction


def _read_events(path: Path, cutoff: dt.date) -> dict[str, tuple[ZZEvent, ...]]:
    result: dict[str, list[ZZEvent]] = {"SPX": [], "QQQ": []}
    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            peak = dt.date.fromisoformat(row["peak_date"])
            if peak > cutoff:
                raise ValueError("ZZ 事件超出开发期标签截止日")
            end = dt.date.fromisoformat(row["end_date"]) if row["end_date"] else None
            event = ZZEvent(row["symbol"], peak, dt.date.fromisoformat(row["t0_date"]),
                            dt.date.fromisoformat(row["trough_date"]), end,
                            Decimal(row["peak_close"]), Decimal(row["trough_close"]),
                            row["right_censored"] == "是")
            result[event.symbol].append(event)
    return {symbol: tuple(sorted(events, key=lambda event: event.peak_date))
            for symbol, events in result.items()}


def run_development_exit_costs(root: Path) -> Path:
    """只输出开发期 P1 三种 E 的描述性代价；不组合 N 或计算主损失。"""
    config = load_wavewarn_config(root / "config/wavewarn_v121.yaml")
    business = config.require_business_parameters()
    inputs = load_development_inputs(root, config.development_end())
    base = root / "reports/research/wavewarn_v121"
    convergence: list[dt.date] = []
    t0_values: set[dt.date] = set()
    with (base / "convergence_diagnostics.csv").open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if not row["scenario"].startswith("P1-"):
                continue
            t0_values.add(dt.date.fromisoformat(row["t0"]))
            if not row["system_convergence_date"]:
                raise ValueError("P1 某组未收敛，不能计算统一 τ_E")
            convergence.append(dt.date.fromisoformat(row["system_convergence_date"]))
    if len(t0_values) != 1:
        raise ValueError("P1 的 t0 不一致")
    t0 = next(iter(t0_values))
    tau_e = common_exit_start(inputs.days, t0, convergence)
    events = _read_events(base / "zz_events_development.csv", config.development_end())
    tail_symbols: set[str] = set()
    with (base / "zz_unknown_development.csv").open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row["reason"] == "尾段（寻峰）":
                tail_symbols.add(row["symbol"])
    grouped: dict[tuple[str, str, str], dict[dt.date, str]] = {}
    with (base / "development_state_diagnostics.csv").open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row["scenario"].startswith("P1-"):
                day = dt.date.fromisoformat(row["date"])
                if day > config.development_end():
                    raise ValueError("P1 诊断越过开发期")
                grouped.setdefault((row["scenario"], row["k"], row["theta_p"]), {})[day] = row["light"]
    if len(grouped) != 27:
        raise ValueError("P1 E1/E2/E3 诊断须恰有 27 组")
    axis = inputs.days[inputs.days.index(t0):]
    destination = base / "exit_cost_events_development.csv"
    summary_path = base / "exit_cost_summary_development.csv"
    green_path = base / "green_executions_development.csv"
    cross_path = base / "exit_cost_class1a_ledger_cross.csv"
    summaries = []
    with (destination.open("w", encoding="utf-8", newline="") as details,
          green_path.open("w", encoding="utf-8", newline="") as green_file,
          cross_path.open("w", encoding="utf-8", newline="") as cross_file):
        writer = csv.writer(details)
        green_writer = csv.writer(green_file)
        cross_writer = csv.writer(cross_file)
        cross_writer.writerow(("scenario", "k", "theta_p", "symbol", "peak_date", "trough_date",
                               "event_ledger_inclusion", "event_ledger_class"))
        writer.writerow(("scenario", "k", "theta_p", "symbol", "peak_date", "trough_date", "inclusion",
                         "rebound_class", "green_at_trough_subclass", "half_way_green_count",
                         "deepest_decline", "re_alerted_before_trough", "rebound_recovery",
                         "raw_rebound_percent", "first_green_from_trough", "tau_e"))
        green_writer.writerow(("scenario", "k", "theta_p", "symbol", "peak_date", "trough_date",
                               "signal_date", "execution_date", "signal_close", "execution_close", "timing",
                               "rebound_from_trough_percent", "decline_to_trough", "signal_decline_to_trough",
                               "role"))
        for (scenario, k, theta), signals in sorted(grouped.items()):
            if set(signals) != set(axis):
                raise ValueError("P1 状态日轴与开发期交易日不一致")
            lights = tuple(signals[day] for day in axis)
            for symbol in ("SPX", "QQQ"):
                closes = tuple(inputs.series[symbol].get(day) for day in axis)
                executions = execute_asset(axis, lights, closes, business.eta)
                asset_events = events[symbol]
                rows = []
                for index, event in enumerate(asset_events):
                    next_t0 = asset_events[index + 1].t0_date if index + 1 < len(asset_events) else None
                    row = exit_cost_for_event(axis, executions, closes, event, next_t0, t0, tau_e)
                    rows.append(row)
                    writer.writerow((scenario, k, theta, symbol, event.peak_date, event.trough_date,
                                     row.inclusion, row.rebound_class or "", row.green_at_trough_subclass or "",
                                     row.half_way_green_count,
                                     row.deepest_decline if row.deepest_decline is not None else "",
                                     row.re_alerted_before_trough if row.re_alerted_before_trough is not None else "",
                                     row.rebound_recovery if row.rebound_recovery is not None else "",
                                     row.raw_rebound_percent if row.raw_rebound_percent is not None else "",
                                     row.first_green_from_trough or "", tau_e))
                    if row.green_at_trough_subclass == "①a 高点前已绿并持续":
                        ledger_inclusion = event_ledger_inclusion(event, axis, t0, tau_e)
                        ledger_class = (classify_asset_event(axis, lights, event).classification
                                        if ledger_inclusion == "纳入" else None)
                        cross_writer.writerow((scenario, k, theta, symbol, event.peak_date, event.trough_date,
                                               ledger_inclusion, ledger_class or ""))
                    for item in row.green_executions:
                        green_writer.writerow((scenario, k, theta, symbol, event.peak_date, event.trough_date,
                                               item.signal_date, item.execution_date,
                                               item.signal_close if item.signal_close is not None else "",
                                               item.execution_close, item.timing,
                                               item.rebound_from_trough_percent,
                                               item.decline_to_trough if item.decline_to_trough is not None else "",
                                               item.signal_decline_to_trough
                                               if item.signal_decline_to_trough is not None else "", item.role))
                denominators = event_denominators(asset_events, rows, int(symbol in tail_symbols))
                included = [row for row in rows if row.inclusion == "纳入"]
                classes = Counter(row.rebound_class for row in included)
                subclasses = Counter(row.green_at_trough_subclass for row in included)
                drops = [row.deepest_decline for row in included if row.deepest_decline is not None]
                rebounds = rebound_values(included)
                rebound_without_trough_day = rebound_values(included, exclude_trough_day=True)
                g_at_trough = sum(row.rebound_class == "②低点或之后转绿"
                                  and row.first_green_from_trough == row.trough_date for row in included)
                summaries.append((scenario, k, theta, symbol, tau_e, denominators, classes, subclasses,
                                  drops, rebounds, rebound_without_trough_day, g_at_trough,
                                  independent_green_execution_count(included)))
    with summary_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("scenario", "k", "theta_p", "symbol", "tau_e", "included", "state_insufficient",
                         "right_censored", "tail_pending", "class_1", "class_1a", "class_1b", "class_2",
                         "class_3", "class_1_rate", "class_2_rate", "class_3_rate",
                         "g_at_trough_count", "independent_green_executions", "half_way_event_n",
                         "half_way_incidence",
                         "half_way_drop_median", "half_way_drop_p75",
                         "half_way_drop_max", "r_n", "r_median", "r_p75", "r_max",
                         "r_without_g_at_trough_n",
                         "r_without_g_at_trough_median", "r_without_g_at_trough_p75",
                         "r_without_g_at_trough_max", "conservative_over_limit_count"))
        for (scenario, k, theta, symbol, tau_e, denom, classes, subclasses, drops, rebounds,
             rebound_without_trough_day, g_at_trough, independent_count) in summaries:
            included = denom["纳入"]
            class_1 = classes["①低点前已绿"]
            class_2 = classes["②低点或之后转绿"]
            class_3 = classes["③下一事件前未转绿"]
            if class_1 + class_2 + class_3 != included:
                raise ValueError("退出代价三类未穷尽纳入事件")
            if subclasses["①a 高点前已绿并持续"] + subclasses["①b 低点前转绿"] != class_1:
                raise ValueError("类别①a/①b 未穷尽类别①")
            writer.writerow((scenario, k, theta, symbol, tau_e, denom["纳入"], denom["状态窗口不足"],
                             denom["右截尾"], denom["尾段未定"], class_1,
                             subclasses["①a 高点前已绿并持续"], subclasses["①b 低点前转绿"],
                             class_2, class_3,
                             Decimal(class_1) / included if included else "",
                             Decimal(class_2) / included if included else "",
                             Decimal(class_3) / included if included else "",
                             g_at_trough, independent_count, len(drops),
                             Decimal(len(drops)) / included if included else "",
                             _linear_percentile(drops, Decimal("0.5"))
                             if drops else "",
                             _linear_percentile(drops, Decimal("0.75"))
                             if drops else "", max(drops) if drops else "", len(rebounds),
                             _linear_percentile(rebounds, Decimal("0.5"))
                             if rebounds else "",
                             _linear_percentile(rebounds, Decimal("0.75"))
                             if rebounds else "",
                             max(rebounds) if rebounds else "", len(rebound_without_trough_day),
                             _linear_percentile(rebound_without_trough_day, Decimal("0.5"))
                             if rebound_without_trough_day else "",
                             _linear_percentile(rebound_without_trough_day, Decimal("0.75"))
                             if rebound_without_trough_day else "",
                             max(rebound_without_trough_day) if rebound_without_trough_day else "",
                             class_3))
    report = base / "E1_E2_E3退出代价开发期描述.md"
    with summary_path.open(encoding="utf-8", newline="") as file:
        summary_rows = list(csv.DictReader(file))
    lines = ["# E1/E2/E3 退出代价：开发期描述", "",
             "仅使用截至 2016-12-30 的开发期价格、ZZ 标签及 P1 状态诊断；不比较主损失、不选择 E/K/θ_P，"
             "不能解释为验证期或实盘预警效果。三种 E 与九组 P1 共用 τ_E=" + tau_e.isoformat() + "。", "",
             "类别锚点为 Tr−1→Tr 区间的执行状态 S_{Tr−2}。①a 高点前已绿并持续，①b 低点前转绿；"
             "② 低点或之后且下一 T0 前转绿；③ 下一 T0 前未转绿。类别与半山腰转绿发生率独立。", "",
             "四类分母（纳入、状态窗口不足、右截尾、尾段未定）逐场景资产并列；"
             "早于 τ_E 的事件列于逐事件 CSV。尾段未定不是已确认事件。", "",
             "| 场景 | K | θ_P | 资产 | 纳入/状态不足/右截尾/尾段 | ①a/①b/②/③ | ①/②/③占纳入比例 |",
             "|---|---:|---:|---|---|---|---|"]
    for row in summary_rows:
        counts = "/".join(row[name] for name in ("included", "state_insufficient", "right_censored", "tail_pending"))
        classes = "/".join(row[name] for name in ("class_1a", "class_1b", "class_2", "class_3"))
        rates = "/".join(row[name] for name in ("class_1_rate", "class_2_rate", "class_3_rate"))
        lines.append(f"| {row['scenario']} | {row['k']} | {row['theta_p']} | {row['symbol']} "
                     f"| {counts} | {classes} | {rates} |")
    lines.extend(["", "半山腰转绿：执行日 `[P,Tr)` 内非绿→绿；发生率以纳入事件为分母，"
                  "跌幅分布只以发生事件为样本。R 为价格收复比例，主口径包含 g=Tr 的 R=0；"
                  "对照剔除 g=Tr。第③类在保守接受范围检查中按超限计，"
                  "但 N 未锁定、P1 参数未选定，所以本表不作范围通过/超出判定。", "",
                  "| 场景 | K | θ_P | 资产 | 半山腰发生率(n) | 半山腰跌幅 中位/P75/最大 | "
                  "②的R n/中位/P75/最大 | g=Tr数 | 剔除g=Tr的R n/中位/P75/最大 | ③保守超限数 | 独立转绿次数 |",
                  "|---|---:|---:|---|---|---|---|---:|---|---:|---:|"])
    for row in summary_rows:
        drop = "/".join(row[name] for name in ("half_way_drop_median", "half_way_drop_p75",
                                                  "half_way_drop_max"))
        recovery = "/".join(row[name] for name in ("r_n", "r_median", "r_p75", "r_max"))
        sensitivity = "/".join(row[name] for name in ("r_without_g_at_trough_n",
                                                         "r_without_g_at_trough_median",
                                                         "r_without_g_at_trough_p75",
                                                         "r_without_g_at_trough_max"))
        lines.append(f"| {row['scenario']} | {row['k']} | {row['theta_p']} | {row['symbol']} "
                     f"| {row['half_way_incidence']} ({row['half_way_event_n']}) | {drop} | {recovery} "
                     f"| {row['g_at_trough_count']} | {sensitivity} | {row['conservative_over_limit_count']} "
                     f"| {row['independent_green_executions']} |")
    cross_counts: Counter[str] = Counter()
    with cross_path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            cross_counts[row["event_ledger_class"] or row["event_ledger_inclusion"]] += 1
    lines.extend(["", ("①a 与五类事件账交叉（仅 P−20 不早于 τ_E 时给五类）："
                   + "；".join(f"{name} {count}" for name, count in sorted(cross_counts.items()))
                   if cross_counts else "①a 与五类事件账交叉：本次开发期诊断没有①a事件，交叉表为空。"), "",
                  "逐事件、每次转绿、①a 交叉与逐场景统计分别见同目录 CSV。"
                  "转绿价相对最终低点涨幅为事后描述值，单位百分点；R 是占事件下跌幅度的比例。", ""])
    report.write_text("\n".join(lines), encoding="utf-8")
    return summary_path
