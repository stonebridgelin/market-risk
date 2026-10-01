"""v1.4 开发期评价的读写边界：读配置与输入、调用纯计算、写出 CSV 与报告。

只执行开发期；不写锁定记录，不运行验证期。输出另存 reports/research/wavewarn_v14/，不触碰 v1.2.1、v1.3 的输出。
"""

from __future__ import annotations

import datetime as dt
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.config_v14 import V14Config, load_v14_config
from market_risk.wavewarn.evaluation import SYMBOLS, PreparedEvaluation, development_scope_rule
from market_risk.wavewarn.evaluation_run import file_sha256, write_csv
from market_risk.wavewarn.evaluation_tables import (
    ALERT_HEADER,
    ALERT_SUMMARY_HEADER,
    EVENT_CLASS_HEADER,
    EVENT_HEADER,
    MISSING_HEADER,
    SCOPE_COUNT_HEADER,
    YEARLY_HEADER,
    CandidateTables,
    Row,
    candidate_tables,
    event_scope_counts,
)
from market_risk.wavewarn.evaluation_v13_tables import (
    REFERENCE_HEADER,
    V13_DAILY_HEADER,
    reference_prefix,
    reference_row,
    v13_daily_rows,
)
from market_risk.wavewarn.evaluation_v14 import V14Evaluation, evaluate_v14, setting_label
from market_risk.wavewarn.evaluation_v14_report import V14ReportTables, readme_lines, report_lines
from market_risk.wavewarn.evaluation_v14_tables import (
    CONVERGENCE_HEADER,
    SETTING_HEADER,
    SUMMARY_HEADER,
    TRACE_HEADER,
    convergence_rows,
    setting_prefix,
    summary_rows,
    trace_rows,
)
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import merge_zz_events
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.v14_model import prepare_v14

OUTPUT_DIR = "reports/research/wavewarn_v14/evaluation_development"
CONFIG_FILE = "config/wavewarn_v14.yaml"
REPORT_NAME = "开发期评价报告.md"


@dataclass(frozen=True)
class V14Run:
    """服务层返回值：输出位置、共同起点与选择结果，可直接序列化。"""

    output: Path
    settings: int
    t0: dt.date
    tau: dt.date
    first_loss_day: dt.date
    selected: str
    tier: int
    note: str


def _with_prefix(rows: Sequence[Row], prefix: Row) -> list[Row]:
    """把 v1.2.1 表格行的四列设定换成五列。"""
    return [(*prefix, *row[4:]) for row in rows]


def _selected_tables(prepared: PreparedEvaluation, result: V14Evaluation) -> CandidateTables:
    """选定设定的事件账、警报账与逐日明细行。"""
    merged = merge_zz_events(tuple(event for symbol in SYMBOLS for event in result.events[symbol]))
    rule = development_scope_rule(prepared.tau, prepared.first_loss_day)
    weights = configured_loss_settings(prepared.config).weights
    return candidate_tables(prepared, result.selected_states, result.selected, result.events, merged, rule,
                            weights, result.unknown)


def _write_selected(destination: Path, prepared: PreparedEvaluation, result: V14Evaluation,
                    tables: CandidateTables) -> None:
    """选定设定与四条参照行的逐日明细；选定设定的事件账、警报账。"""
    prefix = setting_prefix(result.selected_states.candidate)
    weights = configured_loss_settings(prepared.config).weights
    daily = _with_prefix(tables.daily, prefix)
    for reference in result.references:
        daily.extend(v13_daily_rows(reference_prefix(reference.name), reference.evaluated, weights,
                                    prepared.inputs.series))
    write_csv(destination / "daily_selected.csv", V13_DAILY_HEADER, daily)
    for name, header, rows in (("event_ledger_selected", EVENT_HEADER, tables.events),
                               ("event_class_summary_selected", EVENT_CLASS_HEADER, tables.event_classes),
                               ("alert_ledger_selected", ALERT_HEADER, tables.alerts),
                               ("alert_summary_selected", ALERT_SUMMARY_HEADER, [tables.alert_summary]),
                               ("yearly_alert_selected", YEARLY_HEADER, tables.yearly)):
        write_csv(destination / f"{name}.csv", (*SETTING_HEADER, *header[4:]), _with_prefix(rows, prefix))


def write_v14_evaluation(prepared: PreparedEvaluation, config: V14Config, destination: Path) -> V14Evaluation:
    """全部设定汇总、选择追踪、参照行、选定设定的明细与报告，全部写入 destination。"""
    if destination.exists():
        raise FileExistsError(f"评价目录已存在，拒绝覆盖：{destination}")
    destination.mkdir(parents=True)
    result = evaluate_v14(prepared, config)
    settings = configured_loss_settings(prepared.config)
    selected = _selected_tables(prepared, result)
    scope_counts = event_scope_counts(prepared.inputs.days, result.events,
                                      development_scope_rule(prepared.tau, prepared.first_loss_day))
    write_csv(destination / "settings_summary.csv", SUMMARY_HEADER,
              summary_rows(result.results, result.trace.selected.key))
    write_csv(destination / "selection_trace.csv", TRACE_HEADER, trace_rows(result.trace, config.tiers))
    write_csv(destination / "reference_rows.csv", REFERENCE_HEADER,
              [reference_row(row) for row in result.references])
    write_csv(destination / "convergence.csv", CONVERGENCE_HEADER, convergence_rows(prepared))
    write_csv(destination / "event_scope_counts.csv", SCOPE_COUNT_HEADER, scope_counts)
    write_csv(destination / "missing_audit.csv", MISSING_HEADER, result.missing)
    _write_selected(destination, prepared, result, selected)
    label = setting_label(result.selected_states.candidate)
    tables = V14ReportTables(result.results, result.references, result.trace, scope_counts,
                             tuple((label, *row[1:]) for row in selected.event_classes),
                             ((label, *selected.alert_summary[1:]),), result.missing)
    (destination / REPORT_NAME).write_text("\n".join(report_lines(prepared, tables, settings, config)),
                                           encoding="utf-8")
    daily_sha256 = file_sha256(destination / "daily_selected.csv")
    (destination / "README.md").write_text("\n".join(readme_lines(prepared, daily_sha256)), encoding="utf-8")
    return result


def run_v14_development(root: Path) -> V14Run:
    """供 services 调用；先写入临时目录，全部成功后再改名，目标目录已存在则拒绝覆盖。"""
    output = root / OUTPUT_DIR
    if output.exists():
        raise FileExistsError(f"评价目录已存在，拒绝覆盖：{output}")
    config = load_v14_config(root / CONFIG_FILE)
    prepared = prepare_v14(config, load_development_inputs(root, config.base.development_end()))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".evaluation_development_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        result = write_v14_evaluation(prepared, config, staging)
        staging.rename(output)
    trace = result.trace
    return V14Run(output, len(result.results), prepared.t0, prepared.tau, prepared.first_loss_day,
                  trace.selected.key, trace.tier, trace.note)
