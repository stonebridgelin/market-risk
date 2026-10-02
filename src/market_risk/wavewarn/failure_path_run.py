"""失败路径分析（T2）的读写边界：装载输入、与已入库输出做一致性检查、写出账本与报告。

- 开发期：输入截至 2016-12-30；对象为修正后的 v1.4 选定设定、200 日均线、带缓冲带的 200 日均线、满仓。
- 迁移评估：只读 SPX、QQQ 截至 2009-09-30 的收盘价；事件标签按 2009-09-30 截止另行生成，不用开发期标签。
各对象的状态序列由现有纯函数重新生成，生成结果与已入库输出重叠的部分必须逐行一致，否则报错停下。
不重跑、不改写任何已入库的评价输出。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn.buffered_ma import BUFFERED_MA200, buffered_ma_states
from market_risk.wavewarn.config_failure_path import FailurePathConfig, load_failure_path_config
from market_risk.wavewarn.config_v14 import Round2Config, ValidationConfig, load_round2_config, load_validation_config
from market_risk.wavewarn.diagnostics_round2 import (
    MA200,
    exposure_levels,
    setting_states,
    window_offset,
)
from market_risk.wavewarn.diagnostics_round2 import (
    executed_lights as existing_executed_lights,
)
from market_risk.wavewarn.diagnostics_round2_run import HASH_FILE, ROUND2_CONFIG
from market_risk.wavewarn.evaluation import SYMBOLS, CandidateStates, PreparedEvaluation, period_labels
from market_risk.wavewarn.evaluation_run import file_sha256, write_csv
from market_risk.wavewarn.export import read_zz_events
from market_risk.wavewarn.extended_history_v14 import prepare_price_window
from market_risk.wavewarn.extended_nav import extended_nav, signal_object
from market_risk.wavewarn.extended_nav_report import nav_daily
from market_risk.wavewarn.extended_nav_run import NAV_PLACES, load_price_inputs
from market_risk.wavewarn.failure_path import GREEN, PathError, executed_lights
from market_risk.wavewarn.failure_path_analysis import ObjectAnalysis, ObjectInput, WindowInput, analyse_window
from market_risk.wavewarn.failure_path_context import ClassTotal, MergedEvent, annual_returns
from market_risk.wavewarn.failure_path_report import (
    CASE_HEADER,
    CHANNEL_HEADER,
    ENVIRONMENT_HEADER,
    EVENT_HEADER,
    INTERVAL_HEADER,
    POSITION_HEADER,
    RECONCILIATION_HEADER,
    REPORT_NAME,
    SEGMENT_HEADER,
    SUMMARY_HEADER,
    case_table,
    channel_note,
    channel_table,
    environment_table,
    event_table,
    interval_table,
    md_table,
    position_table,
    reconciliation_table,
    report_lines,
    segment_table,
    summary_table,
)
from market_risk.wavewarn.inputs import load_inputs_until
from market_risk.wavewarn.labels_zz import MergedZZEvent, UnknownLabels, ZZEvent, merge_zz_events
from market_risk.wavewarn.lock_guard import find_git, find_gzip, run_git
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.timing import ma200_states
from market_risk.wavewarn.v14_model import FULL, prepare_v14
from market_risk.wavewarn.validation_output import gzip_file
from market_risk.wavewarn.validation_run import CONFIG_FILE

FAILURE_PATH_CONFIG = "config/wavewarn_v14_failure_path.yaml"
DEVELOPMENT, MIGRATION = "development", "migration"
SELECTED_V14, PRICE_ONLY_NAME, FULLY_INVESTED = "修正后的 v1.4（K=5，θ_P=2.5%）", "纯价格版本（K=5，θ_P=2.5%）", "满仓"
TAIL_REASON = "尾段（寻峰）"
INTERVALS = "intervals.csv"
PRICE_FILES = {"SPX": "data/market/daily/SPX.csv", "QQQ": "data/market/daily/QQQ.csv"}


@dataclass(frozen=True)
class WindowOutput:
    """一个窗口的全部结果与写入报告的附加说明。"""

    name: str                                  # 子目录名
    title: str
    window: WindowInput
    results: tuple[ObjectAnalysis, ...]
    checks: dict[str, object]                  # 一致性检查的记录
    notes: tuple[str, ...]
    labels: dict[str, object] | None           # 迁移评估另行生成的标签；开发期为空
    channel_texts: tuple[str | None, ...]      # 各对象通道同时出现统计的加注（对照没有通道，为空）


@dataclass(frozen=True)
class FailurePathRun:
    output: Path
    windows: tuple[tuple[str, int, int], ...]  # （窗口，区间数，选定设定的非绿执行段数）
    report_sha256: tuple[str, ...]


# ---------------------------------------------------------------------------
# 事件标签
# ---------------------------------------------------------------------------

def member_key(event: ZZEvent) -> str:
    return f"{event.symbol}:{event.peak_date.isoformat()}:{event.trough_date.isoformat()}"


def merged_event(number: int, group: MergedZZEvent) -> MergedEvent:
    """P、T0 取成员最早的，Tr 取最晚的，End 取最晚的结束日；任一成员右截尾则合并事件右截尾、End 为空。"""
    censored = any(event.right_censored for event in group.members)
    ends = [event.end_date for event in group.members]
    if not censored and any(end is None for end in ends):
        raise PathError("已确认的事件缺少结束日")
    return MergedEvent(number, group.peak_date, group.t0_date, group.trough_date,
                       None if censored else max(end for end in ends if end is not None), censored,
                       tuple(member_key(event) for event in group.members))


def merged_events(events: Mapping[str, Sequence[ZZEvent]]) -> tuple[MergedEvent, ...]:
    ordered = sorted((event for symbol in SYMBOLS for event in events[symbol]),
                     key=lambda event: (event.peak_date, event.symbol))
    return tuple(merged_event(number, group) for number, group in enumerate(merge_zz_events(ordered), start=1))


def stored_merged_events(root: Path, config: FailurePathConfig, cutoff: dt.date) -> tuple[MergedEvent, ...]:
    """已入库的合并事件标签文件，按 members 字段关联单资产事件取 End。

    先核对文件里的 P、T0、Tr 是否符合“最早、最早、最晚”的口径，不符即报错停下。
    """
    assets = read_zz_events(root / config.stored.events, cutoff)
    by_key = {member_key(event): event for symbol in SYMBOLS for event in assets[symbol]}
    result = []
    with (root / config.stored.merged).open(encoding="utf-8-sig", newline="") as file:
        for number, row in enumerate(csv.DictReader(file), start=1):
            members = tuple(by_key[key] for key in row["members"].split(";"))
            expected = (min(event.peak_date for event in members), min(event.t0_date for event in members),
                        max(event.trough_date for event in members))
            stored = tuple(dt.date.fromisoformat(row[name]) for name in ("peak_date", "t0_date", "trough_date"))
            if stored != expected or len(members) != int(row["member_count"]):
                raise PathError(f"合并事件文件第 {number} 行的 P、T0、Tr 或成员数与口径不符")
            result.append(merged_event(number, MergedZZEvent(*stored, members)))
    return tuple(result)


def stored_tail_unknown(root: Path, config: FailurePathConfig) -> dict[str, frozenset[dt.date]]:
    days: dict[str, set[dt.date]] = {symbol: set() for symbol in SYMBOLS}
    with (root / config.stored.unknown).open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row["reason"] == TAIL_REASON:
                days[row["symbol"]].add(dt.date.fromisoformat(row["date"]))
    return {symbol: frozenset(values) for symbol, values in days.items()}


def tail_unknown(unknown: UnknownLabels) -> dict[str, frozenset[dt.date]]:
    return {symbol: frozenset(day for day, reason in unknown.reasons_by_asset[symbol].items()
                              if reason == TAIL_REASON) for symbol in SYMBOLS}


# ---------------------------------------------------------------------------
# 公共装配
# ---------------------------------------------------------------------------

def year_ends(first: int, last: int) -> dict[int, dt.date]:
    """各日历年的最后一个交易日（由交易日历给出，不读取价格）。"""
    return {year: stock_trading_days(dt.date(year, 12, 1), dt.date(year, 12, 31))[-1]
            for year in range(first, last + 1)}


def object_input(name: str, states: CandidateStates, with_channels: bool) -> ObjectInput:
    return ObjectInput(name, tuple(row.light for row in states.rows),
                       tuple(tuple(row.active_channels) for row in states.rows) if with_channels else None)


def window_input(label: str, prepared: PreparedEvaluation, events: tuple[MergedEvent, ...],
                 tail: Mapping[str, frozenset[dt.date]], config: FailurePathConfig,
                 cases: tuple[tuple[dt.date, dt.date], ...]) -> WindowInput:
    days = prepared.inputs.days
    axis = days[days.index(prepared.t0):]
    offset = window_offset(prepared)
    window_days = axis[offset:]
    closes: dict[str, tuple[float, ...]] = {}
    for symbol in SYMBOLS:
        values = [prepared.inputs.series[symbol].get(day) for day in window_days]
        if any(value is None for value in values):
            raise PathError(f"{symbol} 在评价窗口内缺收盘价；缺失记待补，不插值")
        closes[symbol] = tuple(float(value) for value in values)  # type: ignore[arg-type]
    settings = configured_loss_settings(prepared.config)
    spx = {day: float(value) for day, value in prepared.inputs.series["SPX"].items() if value is not None}
    annual = annual_returns(spx, year_ends(window_days[0].year - 1, window_days[-1].year), days[-1])
    return WindowInput(label, axis, offset, closes, {symbol: float(settings.weights[symbol]) for symbol in SYMBOLS},
                       {light: float(level) for light, level in exposure_levels(settings.parameters.eta).items()},
                       events, days, tail, annual, config.pre_peak_days, cases)


def check_lights(prepared: PreparedEvaluation, states: CandidateStates, item: ObjectInput, window: WindowInput) -> None:
    """本模块的执行灯色须与现有的执行规则（第二轮诊断同一条）逐日相同。"""
    count = len(window.days) - 1
    if executed_lights(item.signals, window.offset, count) != existing_executed_lights(prepared, states)[:count]:
        raise PathError(f"{item.name} 的执行灯色与现有执行规则不一致")


def constant_green(window_axis: Sequence[dt.date]) -> ObjectInput:
    return ObjectInput(FULLY_INVESTED, (GREEN,) * len(window_axis), None)


# ---------------------------------------------------------------------------
# 开发期
# ---------------------------------------------------------------------------

def check_development_daily(root: Path, config: FailurePathConfig, prepared: PreparedEvaluation,
                            objects: Mapping[str, ObjectInput], window: WindowInput) -> dict[str, object]:
    """重新生成的状态与已入库的修正后逐日明细重叠的列逐行比较：日期、信号灯色、系统执行灯色、激活通道、收盘价。"""
    with gzip.open(root / config.stored.development_daily, "rt", encoding="utf-8", newline="") as file:
        stored = list(csv.DictReader(io.StringIO(file.read())))
    days, compared = window.days, {}
    for model, item in objects.items():
        for symbol in SYMBOLS:
            rows = [row for row in stored if (row["model"], row["symbol"]) == (model, symbol)]
            if len(rows) != len(days):
                raise PathError(f"已入库逐日明细里 {model}·{symbol} 的行数与评价窗口不符")
            for index, row in enumerate(rows):
                position = window.offset + index
                expected = [days[index].isoformat(), item.signals[position],
                            (GREEN, *item.signals)[position], str(prepared.inputs.series[symbol][days[index]])]
                actual = [row["date"], row["signal_light"], row["system_executed_light"], row["close"]]
                if item.active is not None:
                    expected.append(";".join(item.active[position]))
                    actual.append(row["active_channels"])
                if Decimal(actual[3]) != Decimal(expected[3]) or actual[:3] + actual[4:] != expected[:3] + expected[4:]:
                    raise PathError(f"重新生成的 {model}·{symbol} 与已入库逐日明细在 {row['date']} 不一致")
            compared[f"{model}·{symbol}"] = len(rows)
    return {"file": config.stored.development_daily, "sha256": file_sha256(root / config.stored.development_daily),
            "columns": ["date", "signal_light", "system_executed_light", "close", "active_channels（只对 v1.4）"],
            "rows_compared": compared, "result": "逐行一致"}


def check_buffered(root: Path, config: FailurePathConfig, prepared: PreparedEvaluation,
                   states: CandidateStates) -> dict[str, object]:
    """带缓冲带均线没有已入库的逐日明细：与已入库参照行的平均暴露、计费切换次数比较。"""
    stored = json.loads((root / config.stored.buffered_reference).read_text(encoding="utf-8"))["rows"][BUFFERED_MA200]
    levels = exposure_levels(configured_loss_settings(prepared.config).parameters.eta)
    rebuilt = signal_object(BUFFERED_MA200, prepared, states, levels)
    if (str(rebuilt.mean_exposure), len(rebuilt.switches)) != (stored["mean_exposure"], stored["billed_switches"]):
        raise PathError("带缓冲带均线的平均暴露或切换次数与已入库参照行不一致")
    return {"file": config.stored.buffered_reference, "mean_exposure": stored["mean_exposure"],
            "billed_switches": stored["billed_switches"], "result": "一致"}


def development_output(root: Path, validation: ValidationConfig, round2: Round2Config,
                       config: FailurePathConfig) -> WindowOutput:
    end = validation.model.base.development_end()
    prepared = prepare_v14(validation.model, load_inputs_until(root, end, validation.vix3m_file))
    states = {SELECTED_V14: setting_states(prepared, FULL, validation),
              MA200: ma200_states(prepared, validation.model.mr_window),
              BUFFERED_MA200: buffered_ma_states(prepared, validation.model.mr_window, round2.buffer_band)}
    window = window_input("开发期", prepared, stored_merged_events(root, config, end),
                          stored_tail_unknown(root, config), config, config.development_case_periods)
    objects = [object_input(name, item, name == SELECTED_V14) for name, item in states.items()]
    for item in objects:
        check_lights(prepared, states[item.name], item, window)
    checks = {"daily_detail": check_development_daily(root, config, prepared,
                                                      {"v1.4": objects[0], MA200: objects[1]}, window),
              "buffered_ma200": check_buffered(root, config, prepared, states[BUFFERED_MA200])}
    results = analyse_window([*objects, constant_green(window.axis)], window, config.settings, config.environment)
    notes = ["## 事件标签与一致性检查", "",
             f"- 事件：已入库的合并事件标签文件 `{config.stored.merged}`（标签截止日 {end}），"
             f"按 `members` 字段关联 `{config.stored.events}` 的单资产事件。口径：P 取成员最早的 `peak_date`，"
             "T0 取最早的 `t0_date`，Tr 取最晚的 `trough_date`，End 取最晚的 `end_date`；"
             f"任一成员右截尾则合并事件右截尾、End 留空。已核对文件里 {len(window.events)} 个合并事件的 P、T0、Tr "
             f"全部符合这一口径；其中右截尾的事件 {sum(event.right_censored for event in window.events)} 个。",
             f"- 尾段未定：`{config.stored.unknown}` 中原因为“{TAIL_REASON}”的日期，按资产分别给出。",
             "- 一致性检查：各对象的状态序列由现有纯函数重新生成。修正后的 v1.4 与 200 日均线的日期、信号灯色、"
             "系统执行灯色、收盘价（v1.4 另加激活通道）与已入库的修正后逐日明细逐行一致；"
             "带缓冲带的 200 日均线的平均暴露与计费切换次数与已入库参照行一致。详见 `consistency_checks.json`。", ""]
    return WindowOutput(DEVELOPMENT, "失败路径分析：开发期（修正后的 v1.4 选定设定）", window, results, checks,
                        tuple(notes), None, channel_texts(results, None))


# ---------------------------------------------------------------------------
# 迁移评估
# ---------------------------------------------------------------------------

def stored_asset_rows(root: Path, config: FailurePathConfig) -> list[dict[str, str]]:
    with (root / config.stored.events).open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def check_labels_against_development(root: Path, config: FailurePathConfig, cutoff: dt.date,
                                     events: Mapping[str, Sequence[ZZEvent]],
                                     merged: Sequence[MergedEvent]) -> dict[str, object]:
    """结束日不晚于 cutoff 的事件（单资产与合并事件），P、T0、Tr、End 及成员须与开发期标签逐个相同。"""
    limit = cutoff.isoformat()
    rows = stored_asset_rows(root, config)
    stored = {(row["symbol"], row["peak_date"], row["t0_date"], row["trough_date"], row["end_date"])
              for row in rows if row["end_date"] and row["end_date"] <= limit}
    generated = {(event.symbol, event.peak_date.isoformat(), event.t0_date.isoformat(),
                  event.trough_date.isoformat(), event.end_date.isoformat())
                 for symbol in SYMBOLS for event in events[symbol]
                 if event.end_date is not None and event.end_date <= cutoff}
    ends = {f"{row['symbol']}:{row['peak_date']}:{row['trough_date']}": row["end_date"] for row in rows}
    stored_merged = set()
    with (root / config.stored.merged).open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            end = max(ends[key] for key in row["members"].split(";"))
            if end <= limit:
                stored_merged.add((row["peak_date"], row["t0_date"], row["trough_date"], end, row["members"]))
    generated_merged = {(event.peak.isoformat(), event.t0.isoformat(), event.trough.isoformat(),
                         event.end.isoformat(), ";".join(event.members))
                        for event in merged if event.end is not None and event.end <= cutoff}
    if stored != generated or stored_merged != generated_merged:
        raise PathError("按截止日生成的标签与开发期标签在结束日不晚于截止日的事件上不一致")
    return {"asset_events_compared": len(generated), "merged_events_compared": len(generated_merged),
            "result": "逐个相同"}


def crossing_usage(root: Path, config: FailurePathConfig, window_days: Sequence[dt.date]) -> list[dict[str, object]]:
    """只核查、不重跑：开发期标签里高点不晚于窗口末日、低点在其后的事件，其危险区间 [P, Tr) 覆盖的窗口内区间。

    此前 v1.3、v1.4 补充历史的损失计算用的是开发期标签（实施细则写明“危险标签取自完整标签”），
    危险标记按 loss.py 的 dangerous_interval 取区间起点日 d 满足 P ≤ d < Tr 的区间。这里只按日期清点，不重算损失。
    """
    end = window_days[-1].isoformat()
    result = []
    for row in stored_asset_rows(root, config):
        if row["peak_date"] <= end < row["trough_date"]:
            days = [day.isoformat() for day in window_days[:-1]
                    if row["peak_date"] <= day.isoformat() < row["trough_date"]]
            result.append({"symbol": row["symbol"], "peak_date": row["peak_date"], "t0_date": row["t0_date"],
                           "trough_date": row["trough_date"], "end_date": row["end_date"],
                           "dangerous_intervals_in_window": len(days), "interval_start_dates": days})
    return result


def check_migration_nav(root: Path, config: FailurePathConfig, prepared: PreparedEvaluation,
                        validation: ValidationConfig, round2: Round2Config,
                        objects: Sequence[ObjectInput], window: WindowInput) -> dict[str, object]:
    """重新生成的状态经现有净值函数得到的逐日净值（12 位小数）须与已入库的迁移评估净值逐行、逐列相同。"""
    result = extended_nav(prepared, validation, round2)
    header, rows = nav_daily(result, NAV_PLACES)
    with gzip.open(root / config.stored.migration_nav, "rt", encoding="utf-8", newline="") as file:
        stored = list(csv.reader(io.StringIO(file.read())))
    rebuilt = [list(header), *([str(cell) for cell in row] for row in rows)]
    if stored != rebuilt:
        raise PathError("重新生成的迁移评估逐日净值与已入库的 nav_daily 不一致")
    count = len(window.days) - 1
    for item, existing in zip(objects, result.objects, strict=True):
        if executed_lights(item.signals, window.offset, count) != existing.lights[:count]:
            raise PathError(f"{item.name} 的执行灯色与迁移评估的对象不一致")
    return {"file": config.stored.migration_nav, "sha256": file_sha256(root / config.stored.migration_nav),
            "rows_compared": len(rows), "columns_compared": len(header), "result": "逐行、逐列相同（12 位小数）"}


def label_tables(events: Mapping[str, Sequence[ZZEvent]], merged: Sequence[MergedEvent],
                 unknown: UnknownLabels) -> dict[str, tuple[tuple[str, ...], list[tuple[object, ...]]]]:
    ordered = sorted((event for symbol in SYMBOLS for event in events[symbol]),
                     key=lambda event: (event.peak_date, event.symbol))
    asset_rows = [(event.symbol, event.peak_date, event.t0_date, event.trough_date, event.end_date or "",
                   event.peak_close, event.trough_close, "是" if event.right_censored else "否") for event in ordered]
    unknown_rows = sorted(((symbol, day, unknown.reasons_by_asset[symbol][day], unknown.label_end)
                           for symbol in SYMBOLS for day in unknown.days_by_asset[symbol]),
                          key=lambda row: (row[1], row[0]))
    return {"zz_events.csv": (("symbol", "peak_date", "t0_date", "trough_date", "end_date", "peak_close",
                               "trough_close", "right_censored"), asset_rows),
            "zz_merged.csv": (EVENT_HEADER, list(event_table(merged))),
            "zz_unknown.csv": (("symbol", "date", "reason", "label_end"), unknown_rows)}


def crossing_state(item: Mapping[str, object], events: Mapping[str, Sequence[ZZEvent]],
                   unknown: UnknownLabels) -> str:
    """开发期标签里的跨界事件，在按截止日生成的标签中的状态。"""
    symbol, peak = str(item["symbol"]), dt.date.fromisoformat(str(item["peak_date"]))
    found = [event for event in events[symbol] if event.peak_date == peak]
    if found:
        event = found[0]
        return (f"右截尾事件（T0 {event.t0_date}，暂定低点 {event.trough_date}）" if event.right_censored
                else f"已确认事件（End {event.end_date}）")
    tail = sorted(day for day, reason in unknown.reasons_by_asset[symbol].items() if reason == TAIL_REASON)
    if tail and tail[0] <= peak:
        return f"不是事件：截止日时仍在寻峰，{symbol} 自 {tail[0]} 起为“{TAIL_REASON}”未定区间"
    return "不是事件，也不在尾段未定区间内"


def channel_texts(results: Sequence[ObjectAnalysis],
                  reference: Sequence[ClassTotal] | None) -> tuple[str | None, ...]:
    """各对象通道同时出现统计的加注；reference 为开发期选定设定的同一统计（只在迁移评估报告里对照方向）。"""
    return tuple(None if item.totals is None else channel_note(item.totals, reference, "开发期") for item in results)


def migration_output(root: Path, validation: ValidationConfig, round2: Round2Config, config: FailurePathConfig,
                     reference: Sequence[ClassTotal]) -> WindowOutput:
    model = validation.model
    cutoff = model.history_end
    inputs = load_price_inputs(root, cutoff, validation.vix3m_file)
    prepared = prepare_price_window(model, inputs)
    chosen = [states for states in prepared.states
              if (states.candidate.k, states.candidate.theta_p) == (validation.locked_k, validation.locked_theta)]
    if len(chosen) != 1:
        raise PathError("纯价格版九组中找不到唯一的冻结参数设定")
    states = {PRICE_ONLY_NAME: chosen[0], MA200: ma200_states(prepared, model.mr_window),
              BUFFERED_MA200: buffered_ma_states(prepared, model.mr_window, round2.buffer_band)}
    events, unknown = period_labels(model.base, inputs, cutoff)
    merged = merged_events(events)
    window = window_input("迁移评估", prepared, merged, tail_unknown(unknown), config, ())
    objects = [object_input(name, item, name == PRICE_ONLY_NAME) for name, item in states.items()]
    for item in objects:
        check_lights(prepared, states[item.name], item, window)
    crossing = crossing_usage(root, config, window.days)
    for item in crossing:
        item["state_in_cutoff_labels"] = crossing_state(item, events, unknown)
    checks: dict[str, object] = {
        "nav_daily": check_migration_nav(root, config, prepared, validation, round2, objects, window),
        "labels_vs_development": check_labels_against_development(root, config, cutoff, events, merged),
        "crossing_events_in_development_labels": crossing}
    git = find_git(validation.git_executable)
    labels = {"tables": label_tables(events, merged, unknown),
              "meta": {"label_version": "zz-v121", "label_cutoff": cutoff.isoformat(),
                       "price_axis_start": inputs.days[0].isoformat(),
                       "asset_events": sum(len(events[symbol]) for symbol in SYMBOLS), "merged_events": len(merged),
                       "right_censored_events": sum(event.right_censored for symbol in SYMBOLS
                                                    for event in events[symbol]),
                       "thresholds": {symbol: [str(value) for value in pair]
                                      for symbol, pair in model.base.zz_thresholds().items()},
                       "code_commit": run_git(git, root, ("rev-parse", "HEAD")).stdout.decode().strip(),
                       "worktree_dirty": bool(run_git(git, root, ("status", "--porcelain")).stdout.strip()),
                       "inputs": {name: {"sha256": file_sha256(root / name),
                                         "note": f"读取在 {cutoff} 之后的第一行之前停止"}
                                  for name in PRICE_FILES.values()}}}
    results = analyse_window([*objects, constant_green(window.axis)], window, config.settings, config.environment)
    affected = sum(int(str(item["dangerous_intervals_in_window"])) for item in crossing)
    crossing_rows = [(item["symbol"], item["peak_date"], item["t0_date"], item["trough_date"], item["end_date"],
                      item["dangerous_intervals_in_window"], item["state_in_cutoff_labels"]) for item in crossing]
    usage = ("没有使用：无影响。" if not affected else
             f"使用了：{affected} 个资产区间被标为危险（见上表）。相关补充历史的损失与择时得分，"
             "在窗口末端用到了截止日之后的标签信息；本批不重跑，待负责人裁决。")
    compared = checks["labels_vs_development"]
    notes = ["## 事件标签与一致性检查", "",
             f"- 事件：按 {cutoff} 截止另行生成的标签（`labels/`），用现有的标签函数，在截到 {cutoff} 的 SPX、QQQ "
             f"收盘价上生成；价格起点与现有标签生成相同（{inputs.days[0]}）。迁移评估部分只使用这份标签，"
             "不使用开发期标签。合并事件口径同开发期：P、T0 取成员最早的，Tr 取最晚的，End 取最晚的结束日；"
             "任一成员右截尾则合并事件右截尾、End 留空。",
             f"- 与开发期标签的核对：结束日不晚于 {cutoff} 的 {compared['asset_events_compared']} "  # type: ignore[index]
             f"个单资产事件与 {compared['merged_events_compared']} 个合并事件，"  # type: ignore[index]
             "P、T0、Tr、End 及成员与开发期标签逐个相同。",
             "- 一致性检查：各对象的状态序列由现有纯函数重新生成，经现有净值函数得到的逐日净值（12 位小数）"
             "与已入库的迁移评估 `nav_daily` 逐行、逐列相同。详见 `consistency_checks.json`。", "",
             "## 开发期标签里跨过截止日的事件", "",
             f"开发期标签（截止 2016-12-30）里，高点不晚于 {cutoff}、低点在其后的事件，"
             "以及它在按截止日生成的标签中的状态：", "",
             *md_table(("资产", "高点 P", "T0", "低点 Tr", "End", "窗口内被其危险区间 [P, Tr) 覆盖的区间数",
                        f"在 {cutoff} 截止的标签中的状态"), crossing_rows),
             "只核查、不重跑的检查：此前 v1.3、v1.4 补充历史的损失计算是否使用了这一跨界事件的危险标记"
             "（当时的实施细则写明“危险标签取自完整标签”，危险区间取区间起点日 d 满足 P ≤ d < Tr）。"
             f"结果：{usage}这里只按日期清点，没有重算损失，也没有修改任何已入库的历史输出。", ""]
    return WindowOutput(MIGRATION, "失败路径分析：冻结参数的纯价格版本迁移评估", window, results, checks,
                        tuple(notes), labels, channel_texts(results, reference))


# ---------------------------------------------------------------------------
# 写出
# ---------------------------------------------------------------------------

def write_window(destination: Path, item: WindowOutput, config: FailurePathConfig, gzip_executable: str) -> str:
    """写出一个窗口的账本、汇总、对账、一致性检查与报告；返回报告的 SHA-256。

    逐区间账本用 gzip -n 压缩入库，压缩前后的 SHA-256 都记在哈希清单里。
    """
    destination.mkdir(parents=True)
    tables = [("segments.csv", SEGMENT_HEADER, segment_table(item.results)),
              ("classification_summary.csv", SUMMARY_HEADER, summary_table(item.results)),
              ("channel_cooccurrence.csv", CHANNEL_HEADER, channel_table(item.results)),
              ("event_position_summary.csv", POSITION_HEADER, position_table(item.results)),
              ("environment_summary.csv", ENVIRONMENT_HEADER, environment_table(item.results)),
              ("reconciliation.csv", RECONCILIATION_HEADER, reconciliation_table(item.results, config.settings)),
              (INTERVALS, INTERVAL_HEADER, interval_table(item.results)),
              ("merged_events.csv", EVENT_HEADER, event_table(item.window.events))]
    if item.window.case_periods:
        tables.append(("case_periods.csv", CASE_HEADER, case_table(item.results)))
    for name, header, rows in tables:
        write_csv(destination / name, header, rows)
    packed = gzip_file(destination / INTERVALS, gzip_executable)
    if item.labels is not None:
        folder = destination / "labels"
        folder.mkdir()
        for name, (header, rows) in item.labels["tables"].items():  # type: ignore[union-attr]
            write_csv(folder / name, header, rows)
        (folder / "label_meta.json").write_text(json.dumps(item.labels["meta"], ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
    (destination / "consistency_checks.json").write_text(
        json.dumps(item.checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = destination / REPORT_NAME
    report.write_text("\n".join(report_lines(item.title, item.window, item.results, config.settings, item.notes,
                                             item.channel_texts)), encoding="utf-8")
    hashes = {path.relative_to(destination).as_posix(): file_sha256(path)
              for path in sorted(destination.rglob("*")) if path.is_file()}
    hashes[f"{INTERVALS}（压缩前，不入库）"] = packed.raw
    (destination / HASH_FILE).write_text(json.dumps(hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return file_sha256(report)


def run_v14_failure_path(root: Path) -> FailurePathRun:
    """失败路径分析；输出目录已存在时拒绝覆盖。全部窗口写齐后才把临时目录改名为输出目录。"""
    validation = load_validation_config(root / CONFIG_FILE)
    round2 = load_round2_config(root / ROUND2_CONFIG)
    config = load_failure_path_config(root / FAILURE_PATH_CONFIG)
    output = root / config.output
    if output.exists():
        raise FileExistsError(f"失败路径分析目录已存在，拒绝覆盖：{output}")
    if config.environment.bear_markets != validation.model.bear_markets:
        raise PathError("熊市区间与 v1.4 配置登记的不一致")
    gzip_executable = find_gzip(validation.gzip_executable)
    development = development_output(root, validation, round2, config)
    selected = development.results[0].totals
    if selected is None:
        raise PathError("开发期选定设定缺少通道同时出现统计")
    windows = (development, migration_output(root, validation, round2, config, selected))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        reports = tuple(write_window(staging / item.name, item, config, gzip_executable) for item in windows)
        staging.rename(output)
    return FailurePathRun(output, tuple((item.window.label, len(item.window.days) - 1, len(item.results[0].segments))
                                        for item in windows), reports)
