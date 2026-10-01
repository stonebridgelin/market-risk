"""v1.4 补充历史的读写边界：读配置、开发期价格与现有 ZZ 标签文件，写出 CSV 与单独的报告。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.config_v14 import load_v14_config
from market_risk.wavewarn.evaluation import SYMBOLS
from market_risk.wavewarn.evaluation_run import write_csv
from market_risk.wavewarn.evaluation_tables import MISSING_HEADER
from market_risk.wavewarn.evaluation_v13_tables import REFERENCE_HEADER, reference_row
from market_risk.wavewarn.evaluation_v14_report import COMMANDS
from market_risk.wavewarn.evaluation_v14_run import CONFIG_FILE
from market_risk.wavewarn.evaluation_v14_run import OUTPUT_DIR as EVALUATION_DIR
from market_risk.wavewarn.export import read_zz_events
from market_risk.wavewarn.extended_history import truncate_inputs
from market_risk.wavewarn.extended_history_run import LABEL_FILE
from market_risk.wavewarn.extended_history_v14 import (
    CROSSING_HEADER,
    SUMMARY_HEADER,
    crossing_rows,
    evaluate_price_window,
    prepare_price_window,
    report_lines,
    summary_row,
)
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import build_unknown_labels
from market_risk.wavewarn.period_stats import PERIOD_HEADER

SUBDIRECTORY = "extended_history"
REPORT_NAME = "补充历史报告.md"


@dataclass(frozen=True)
class V14HistoryRun:
    output: Path
    t0: dt.date
    tau: dt.date
    first_loss_day: dt.date


def run_v14_extended_history(root: Path) -> V14HistoryRun:
    """写入 v1.4 开发期评价目录下的 extended_history/；该子目录已存在时拒绝覆盖。"""
    parent = root / EVALUATION_DIR
    output = parent / SUBDIRECTORY
    if not parent.exists():
        raise ValueError("请先运行 wavewarn evaluate-v14-development，再运行补充历史")
    if output.exists():
        raise FileExistsError(f"补充历史目录已存在，拒绝覆盖：{output}")
    config = load_v14_config(root / CONFIG_FILE)
    end = config.base.development_end()
    inputs = load_development_inputs(root, end)
    events = read_zz_events(root / LABEL_FILE, end)
    unknown = build_unknown_labels(events, inputs.days, {symbol: inputs.series[symbol] for symbol in SYMBOLS}, end)
    prepared = prepare_price_window(config, truncate_inputs(inputs, config.history_end))
    result = evaluate_price_window(prepared, config, events, unknown)
    output.mkdir()
    write_csv(output / "price_only_summary.csv", SUMMARY_HEADER, [summary_row(row) for row in result.rows])
    write_csv(output / "reference_rows.csv", REFERENCE_HEADER, [reference_row(row) for row in result.references])
    write_csv(output / "bear_markets.csv", PERIOD_HEADER, result.bear_markets)
    write_csv(output / "crossing_events.csv", CROSSING_HEADER, crossing_rows(result))
    write_csv(output / "missing_audit.csv", MISSING_HEADER, result.missing)
    (output / REPORT_NAME).write_text("\n".join(report_lines(prepared, config, result, COMMANDS[1])),
                                      encoding="utf-8")
    return V14HistoryRun(output, prepared.t0, prepared.tau, prepared.first_loss_day)
