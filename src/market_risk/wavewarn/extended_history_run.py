"""v1.3 补充历史的读写边界：读配置、开发期价格与现有 ZZ 标签文件，写出 CSV 与单独的报告。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.calibration import DELAYS, calibration_tables
from market_risk.wavewarn.calibration_run import FIXED_HEADER
from market_risk.wavewarn.config_v13 import load_v13_config
from market_risk.wavewarn.evaluation import SYMBOLS
from market_risk.wavewarn.evaluation_run import write_csv
from market_risk.wavewarn.evaluation_tables import MISSING_HEADER
from market_risk.wavewarn.evaluation_v13_run import OUTPUT_DIR as EVALUATION_DIR
from market_risk.wavewarn.evaluation_v13_tables import REFERENCE_HEADER, reference_row
from market_risk.wavewarn.export import read_zz_events
from market_risk.wavewarn.extended_history import (
    evaluate_p0_window,
    prepare_p0_window,
    truncate_inputs,
    window_events,
)
from market_risk.wavewarn.extended_history_report import (
    CROSSING_HEADER,
    EXIT_HEADER,
    P0_HEADER,
    crossing_rows,
    exit_rows,
    p0_row,
    report_lines,
)
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import build_unknown_labels

LABEL_FILE = "reports/research/wavewarn_v121/zz_events_development.csv"
SUBDIRECTORY = "extended_history"
REPORT_NAME = "补充历史报告.md"


@dataclass(frozen=True)
class ExtendedHistoryRun:
    output: Path
    t0: dt.date
    tau: dt.date
    first_loss_day: dt.date


def run_extended_history(root: Path) -> ExtendedHistoryRun:
    """写入开发期评价目录下的 extended_history/；该子目录已存在时拒绝覆盖。"""
    parent = root / EVALUATION_DIR
    output = parent / SUBDIRECTORY
    if not parent.exists():
        raise ValueError("请先运行 wavewarn evaluate-v13-development，再运行补充历史")
    if output.exists():
        raise FileExistsError(f"补充历史目录已存在，拒绝覆盖：{output}")
    config = load_v13_config(root / "config/wavewarn_v13.yaml")
    end = config.base.development_end()
    inputs = load_development_inputs(root, end)
    events = read_zz_events(root / LABEL_FILE, end)
    windows = config.windows
    if DELAYS != (0, 1, 2, 3, 5, 8, 10, 15):
        raise ValueError("固定延迟与登记不一致")
    included = {symbol: window_events(events[symbol], windows.fixed_delay_start[symbol], windows.fixed_delay_end)
                for symbol in SYMBOLS}
    closes = {symbol: inputs.series[symbol] for symbol in SYMBOLS}
    tables = calibration_tables(inputs.days, closes, included, ())
    unknown = build_unknown_labels(events, inputs.days, closes, end)
    prepared = prepare_p0_window(config.base, truncate_inputs(inputs, windows.p0_end), windows.p0_start)
    result = evaluate_p0_window(prepared, events, unknown, config.ma200_window)
    output.mkdir()
    write_csv(output / "fixed_delay_events.csv",
              ("symbol", "peak_date", "trough_date", "end_date", "next_t0", "peak_close", "trough_close",
               "end_close", "event_decline_fraction", "r_end"), tables.event_rows)
    write_csv(output / "fixed_delay_detail.csv",
              ("symbol", "peak_date", "trough_date", "m", "classification", "execution_date", "r"),
              tables.fixed_rows)
    write_csv(output / "fixed_delay_summary.csv", FIXED_HEADER, tables.fixed_summary)
    write_csv(output / "p0_summary.csv", P0_HEADER, [p0_row(row) for row in result.rows])
    write_csv(output / "p0_exit_costs.csv", EXIT_HEADER, exit_rows(result))
    write_csv(output / "p0_crossing_events.csv", CROSSING_HEADER, crossing_rows(result))
    write_csv(output / "p0_missing_audit.csv", MISSING_HEADER, result.missing)
    write_csv(output / "reference_rows.csv", REFERENCE_HEADER, [reference_row(row) for row in result.references])
    lines = report_lines(prepared, windows, {symbol: len(rows) for symbol, rows in included.items()},
                         tables, result)
    (output / REPORT_NAME).write_text("\n".join(lines), encoding="utf-8")
    return ExtendedHistoryRun(output, prepared.t0, prepared.tau, prepared.first_loss_day)

