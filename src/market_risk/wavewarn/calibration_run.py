"""开发期 E 退出代价校准的读写边界：读取输入、调用纯计算、写出 CSV 与报告。"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.calibration import CalibrationTables, EventWithNext, calibration_tables, report_lines
from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.exit_costs import event_inclusion
from market_risk.wavewarn.export import read_zz_events
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import ZZEvent

BASE = "reports/research/wavewarn_v121"
FIXED_HEADER = ("symbol", "m", "included", "class_2", "class_3", "window_short",
                "main_n", "main_min", "main_p25", "main_median", "main_p75", "main_max",
                "conservative_n", "conservative_min", "conservative_p25", "conservative_median",
                "conservative_p75", "conservative_max", "median_le_060", "both_limits", "conservative_both")


@dataclass(frozen=True)
class CalibrationInputs:
    inputs: DevelopmentInputs
    tau_e: dt.date
    t0: dt.date
    events: Mapping[str, tuple[ZZEvent, ...]]
    exit_rows: tuple[Mapping[str, str], ...]


def _read_dict_rows(path: Path, encoding: str = "utf-8-sig") -> list[dict[str, str]]:
    with path.open(encoding=encoding, newline="") as file:
        return list(csv.DictReader(file))


def _single_date(values: set[dt.date], message: str) -> dt.date:
    if len(values) != 1:
        raise ValueError(message)
    return next(iter(values))


def read_calibration_inputs(root: Path) -> CalibrationInputs:
    """读取开发期价格、既有 τ_E、P1 的 t0、ZZ 事件与逐事件退出代价行。"""
    config = load_wavewarn_config(root / "config/wavewarn_v121.yaml")
    inputs = load_development_inputs(root, config.development_end())
    base = root / BASE
    tau_e = _single_date({dt.date.fromisoformat(row["tau_e"])
                          for row in _read_dict_rows(base / "exit_cost_summary_development.csv")},
                         "退出代价汇总的 τ_E 不一致")
    t0 = _single_date({dt.date.fromisoformat(row["t0"])
                       for row in _read_dict_rows(base / "convergence_diagnostics.csv")
                       if row["scenario"].startswith("P1-")}, "P1 的 t0 不一致")
    events = read_zz_events(base / "zz_events_development.csv", config.development_end())
    exit_rows = tuple(_read_dict_rows(base / "exit_cost_events_development.csv"))
    return CalibrationInputs(inputs, tau_e, t0, events, exit_rows)


def included_events(events: Mapping[str, Sequence[ZZEvent]], t0: dt.date,
                    tau_e: dt.date) -> dict[str, tuple[EventWithNext, ...]]:
    """与 exit_costs 相同的纳入事件（P ≥ τ_E，非右截尾），并带上同资产下一事件的 T0。"""
    result: dict[str, tuple[EventWithNext, ...]] = {}
    for symbol in ("SPX", "QQQ"):
        selected = []
        for index, event in enumerate(events[symbol]):
            if event_inclusion(event, t0, tau_e) != "纳入":
                continue
            next_t0 = events[symbol][index + 1].t0_date if index + 1 < len(events[symbol]) else None
            selected.append((event, next_t0))
        result[symbol] = tuple(selected)
    return result


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerows(rows)


def write_calibration(base: Path, tables: CalibrationTables, lines: Sequence[str]) -> Path:
    """写出五份 CSV 与报告，返回报告路径。"""
    _write_csv(base / "E退出校准_事件.csv",
               ("symbol", "peak_date", "trough_date", "end_date", "next_t0", "peak_close", "trough_close",
                "end_close", "event_decline_fraction", "r_end"), tables.event_rows)
    _write_csv(base / "E退出校准_固定延迟明细.csv",
               ("symbol", "peak_date", "trough_date", "m", "classification", "execution_date", "r"),
               tables.fixed_rows)
    _write_csv(base / "E退出校准_固定延迟汇总.csv", FIXED_HEADER, tables.fixed_summary)
    _write_csv(base / "E退出校准_实际延迟明细.csv",
               ("scenario", "k", "theta_p", "symbol", "peak_date", "trough_date", "g", "delay_days",
                "relative_to_end"), tables.actual_rows)
    _write_csv(base / "E退出校准_实际延迟汇总.csv",
               ("scenario", "k", "theta_p", "symbol", "g_le_end", "g_gt_end", "class_3",
                "delay_n", "delay_min", "delay_p25", "delay_median", "delay_p75", "delay_max"),
               tables.actual_summary)
    report = base / "E退出代价_校准.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def run_exit_calibration(root: Path) -> Path:
    """基于既有 τ_E 与纳入事件，输出开发期描述性校准。"""
    data = read_calibration_inputs(root)
    included = included_events(data.events, data.t0, data.tau_e)
    closes = {symbol: data.inputs.series[symbol] for symbol in ("SPX", "QQQ")}
    tables = calibration_tables(data.inputs.days, closes, included, data.exit_rows)
    lines = report_lines(data.tau_e, {symbol: len(rows) for symbol, rows in included.items()}, tables)
    return write_calibration(root / BASE, tables, lines)
