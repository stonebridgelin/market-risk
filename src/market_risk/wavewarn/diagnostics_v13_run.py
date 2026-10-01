"""v1.3 描述性诊断的读写边界：读配置、开发期输入与现有 ZZ 标签文件，写出 CSV 与报告。

不改变任何登记；输出到 reports/research/wavewarn_v13/diagnostics/，目录已存在时拒绝覆盖。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.config_v13 import V13Config, load_v13_config
from market_risk.wavewarn.diagnostics_v13 import (
    BEAR_MARKETS,
    DevelopmentDiagnostics,
    ExtendedDiagnostics,
    TraceContext,
    development_diagnostics,
    extended_diagnostics,
)
from market_risk.wavewarn.diagnostics_v13_report import (
    CATEGORY_FILE_HEADER,
    CONDITION_FILE_HEADER,
    EVENT_FILE_HEADER,
    FIRST_DAYS_HEADER,
    MA200_HEADER,
    REASON_FILE_HEADER,
    bottleneck_tables,
    extreme_note,
    ma200_rows,
    reason_table,
    report_lines,
)
from market_risk.wavewarn.evaluation import PreparedEvaluation, development_labels, grid_features
from market_risk.wavewarn.evaluation_run import write_csv
from market_risk.wavewarn.evaluation_v13 import prepare_v13
from market_risk.wavewarn.export import read_zz_events
from market_risk.wavewarn.extended_history import prepare_p0_window, truncate_inputs
from market_risk.wavewarn.extended_history_run import LABEL_FILE
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import build_unknown_labels
from market_risk.wavewarn.period_stats import PERIOD_HEADER, YEARLY_HEADER, Row
from market_risk.wavewarn.timing import MA200_REFERENCE

OUTPUT_DIR = "reports/research/wavewarn_v13/diagnostics"
REPORT_NAME = "诊断报告.md"


@dataclass(frozen=True)
class DiagnosticsRun:
    output: Path
    settings: int


def _development(config: V13Config, inputs: DevelopmentInputs) -> tuple[PreparedEvaluation, DevelopmentDiagnostics]:
    """开发期：全部 117 组共用的 τ、j₀ 与 v1.3 评价相同，只对诊断涉及的设定重放条件。"""
    prepared = prepare_v13(config, inputs)
    features, ratios = grid_features(config.base, inputs)
    events, unknown = development_labels(prepared)
    context = TraceContext(prepared, features, ratios, config.base.require_channel_selection(), events, unknown)
    return prepared, development_diagnostics(context, config.ma200_window)


def _extended(root: Path, config: V13Config,
              inputs: DevelopmentInputs) -> tuple[PreparedEvaluation, ExtendedDiagnostics]:
    """补充历史：标签取自现有开发期标签文件，状态与执行只算到窗口末日。"""
    end = config.base.development_end()
    events = read_zz_events(root / LABEL_FILE, end)
    unknown = build_unknown_labels(events, inputs.days, {symbol: inputs.series[symbol] for symbol in events}, end)
    window = prepare_p0_window(config.base, truncate_inputs(inputs, config.windows.p0_end),
                               config.windows.p0_start)
    return window, extended_diagnostics(window, events, unknown, config.ma200_window)


def _ma200_event_rows(development: DevelopmentDiagnostics, extended: ExtendedDiagnostics) -> list[Row]:
    rows: list[Row] = list(ma200_rows("开发期", MA200_REFERENCE, development.ma200))
    for label, rebounds in development.median_rebounds.items():
        rows.extend(ma200_rows("开发期", f"{label} 中位设定", rebounds))
    rows.extend(ma200_rows("补充历史", MA200_REFERENCE, extended.ma200))
    rows.extend(ma200_rows("补充历史", "P0 中位设定", extended.p0_median))
    return rows


def write_diagnostics(destination: Path, prepared: PreparedEvaluation, window: PreparedEvaluation,
                      development: DevelopmentDiagnostics, extended: ExtendedDiagnostics, qqq_note: str) -> None:
    """四项诊断的 CSV 与报告。"""
    tables = bottleneck_tables(development.settings)
    write_csv(destination / "bottleneck_categories.csv", CATEGORY_FILE_HEADER, tables["categories"])
    write_csv(destination / "bottleneck_conditions.csv", CONDITION_FILE_HEADER, tables["conditions"])
    write_csv(destination / "bottleneck_events.csv", EVENT_FILE_HEADER, tables["events"])
    write_csv(destination / "bottleneck_first_days.csv", FIRST_DAYS_HEADER, tables["first_days"])
    write_csv(destination / "lighting_reasons.csv", REASON_FILE_HEADER, reason_table(development.settings))
    event_rows = _ma200_event_rows(development, extended)
    write_csv(destination / "ma200_events.csv", MA200_HEADER, event_rows)
    write_csv(destination / "bear_markets.csv", PERIOD_HEADER, extended.bear_markets)
    scoped = lambda scope, rows: [(scope, *row) for row in rows]  # noqa: E731
    write_csv(destination / "yearly_median.csv", ("scope", *YEARLY_HEADER),
              [*scoped("开发期", development.yearly), *scoped("补充历史", extended.yearly)])
    write_csv(destination / "yearly_all_settings.csv", ("scope", *YEARLY_HEADER),
              [*scoped("开发期", development.yearly_all), *scoped("补充历史", extended.yearly_all)])
    lines = report_lines(prepared, window, development, extended, event_rows, {"note": qqq_note})
    (destination / REPORT_NAME).write_text("\n".join(lines), encoding="utf-8")


def run_v13_diagnostics(root: Path) -> DiagnosticsRun:
    """供 services 调用；先写入临时目录，全部成功后再改名。"""
    output = root / OUTPUT_DIR
    if output.exists():
        raise FileExistsError(f"诊断目录已存在，拒绝覆盖：{output}")
    config = load_v13_config(root / "config/wavewarn_v13.yaml")
    inputs = load_development_inputs(root, config.base.development_end())
    prepared, development = _development(config, inputs)
    window, extended = _extended(root, config, inputs)
    qqq_note = extreme_note(inputs.days, inputs.series["QQQ"], BEAR_MARKETS)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".diagnostics_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        write_diagnostics(staging, prepared, window, development, extended, qqq_note)
        staging.rename(output)
    return DiagnosticsRun(output, len(development.settings))
