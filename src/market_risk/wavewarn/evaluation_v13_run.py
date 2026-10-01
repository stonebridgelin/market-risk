"""v1.3 开发期评价的读写边界：读配置与输入、调用纯计算、写出 CSV 与报告。

只执行开发期；不写锁定记录，不运行验证期。输出另存 reports/research/wavewarn_v13/，不触碰 v1.2.1 的输出。
"""

from __future__ import annotations

import datetime as dt
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.config_v13 import V13Config, load_v13_config
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
from market_risk.wavewarn.evaluation_v13 import (
    V13Evaluation,
    display_model,
    display_version,
    evaluate_v13,
    prepare_v13,
)
from market_risk.wavewarn.evaluation_v13_report import V13ReportTables, readme_lines, report_lines
from market_risk.wavewarn.evaluation_v13_tables import (
    CONVERGENCE_HEADER,
    REFERENCE_HEADER,
    SETTING_HEADER,
    SUMMARY_HEADER,
    TRACE_HEADER,
    V13_DAILY_HEADER,
    convergence_rows,
    reference_prefix,
    reference_row,
    selected_labels,
    setting_prefix,
    summary_rows,
    trace_rows,
    v13_daily_rows,
)
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import merge_zz_events
from market_risk.wavewarn.loss import configured_loss_settings

OUTPUT_DIR = "reports/research/wavewarn_v13/evaluation_development"
REPORT_NAME = "开发期评价报告.md"
COMMAND = "uv run market-risk wavewarn evaluate-v13-development"


@dataclass(frozen=True)
class V13Run:
    """服务层返回值：输出位置、共同起点与选择结果，可直接序列化。"""

    output: Path
    settings: int
    t0: dt.date
    tau: dt.date
    first_loss_day: dt.date
    exit_version: str | None
    selected_p1: str | None
    selected_n: str | None
    n_prime_used: bool
    stopped: str


def _with_prefix(rows: Sequence[Row], prefix: Row) -> list[Row]:
    """把 v1.2.1 表格行的四列设定换成含退出版本的五列。"""
    return [(*prefix, *row[4:]) for row in rows]


def _with_label(rows: Sequence[Row], label: str) -> list[Row]:
    """报告复用 v1.2.1 的表格函数：把模型列写成“模型·版本”。"""
    return [(label, *row[1:]) for row in rows]


def _selected_tables(prepared: PreparedEvaluation, result: V13Evaluation) -> list[tuple[Row, str, CandidateTables]]:
    """选定设定的事件账、警报账与逐日明细行。"""
    merged = merge_zz_events(tuple(event for symbol in SYMBOLS for event in result.events[symbol]))
    rule = development_scope_rule(prepared.tau, prepared.first_loss_day)
    weights = configured_loss_settings(prepared.config).weights
    return [(setting_prefix(states.candidate),
             f"{display_model(states.candidate.model)}·{display_version(states.candidate)}",
             candidate_tables(prepared, states, evaluated, result.events, merged, rule, weights, result.unknown))
            for states, evaluated in result.selected]


def _write_selected(destination: Path, prepared: PreparedEvaluation, result: V13Evaluation,
                    selected: Sequence[tuple[Row, str, CandidateTables]]) -> None:
    """只对选定的 P1、N（或 N′）与四条参照行写逐日明细；事件账、警报账只写选定模型。"""
    weights = configured_loss_settings(prepared.config).weights
    daily: list[Row] = []
    for prefix, _, tables in selected:
        daily.extend(_with_prefix(tables.daily, prefix))
    for reference in result.references:
        daily.extend(v13_daily_rows(reference_prefix(reference.name), reference.evaluated, weights,
                                    prepared.inputs.series))
    write_csv(destination / "daily_selected.csv", V13_DAILY_HEADER, daily)
    for name, header, field in (("event_ledger_selected", EVENT_HEADER, "events"),
                                ("event_class_summary_selected", EVENT_CLASS_HEADER, "event_classes"),
                                ("alert_ledger_selected", ALERT_HEADER, "alerts"),
                                ("yearly_alert_selected", YEARLY_HEADER, "yearly")):
        rows = [row for prefix, _, tables in selected for row in _with_prefix(getattr(tables, field), prefix)]
        write_csv(destination / f"{name}.csv", (*SETTING_HEADER, *header[4:]), rows)
    write_csv(destination / "alert_summary_selected.csv", (*SETTING_HEADER, *ALERT_SUMMARY_HEADER[4:]),
              [row for prefix, _, tables in selected for row in _with_prefix([tables.alert_summary], prefix)])


def write_v13_evaluation(prepared: PreparedEvaluation, config: V13Config, destination: Path) -> V13Evaluation:
    """全部设定汇总、选择追踪、参照行、选定设定的明细与报告，全部写入 destination。"""
    if destination.exists():
        raise FileExistsError(f"评价目录已存在，拒绝覆盖：{destination}")
    destination.mkdir(parents=True)
    result = evaluate_v13(prepared, config)
    settings = configured_loss_settings(prepared.config)
    selected = _selected_tables(prepared, result)
    scope_counts = event_scope_counts(prepared.inputs.days, result.events,
                                      development_scope_rule(prepared.tau, prepared.first_loss_day))
    labels = selected_labels(result.trace, result.p0_best)
    write_csv(destination / "settings_summary.csv", SUMMARY_HEADER, summary_rows(result.results, labels))
    write_csv(destination / "selection_trace.csv", TRACE_HEADER, trace_rows(result.trace, result.p0_best))
    write_csv(destination / "reference_rows.csv", REFERENCE_HEADER,
              [reference_row(row) for row in result.references])
    write_csv(destination / "convergence.csv", CONVERGENCE_HEADER, convergence_rows(prepared))
    write_csv(destination / "event_scope_counts.csv", SCOPE_COUNT_HEADER, scope_counts)
    write_csv(destination / "missing_audit.csv", MISSING_HEADER, result.missing)
    _write_selected(destination, prepared, result, selected)
    tables = V13ReportTables(
        result.results, result.references, result.trace, result.p0_best, scope_counts,
        tuple(row for _, label, item in selected for row in _with_label(item.event_classes, label)),
        tuple(row for _, label, item in selected for row in _with_label([item.alert_summary], label)),
        result.missing, config.limits)
    (destination / REPORT_NAME).write_text("\n".join(report_lines(prepared, tables, settings)), encoding="utf-8")
    daily_sha256 = file_sha256(destination / "daily_selected.csv")
    (destination / "README.md").write_text(
        "\n".join(readme_lines(prepared, settings, daily_sha256, COMMAND)), encoding="utf-8")
    return result


def run_v13_development(root: Path) -> V13Run:
    """供 services 调用；先写入临时目录，全部成功后再改名，目标目录已存在则拒绝覆盖。"""
    output = root / OUTPUT_DIR
    if output.exists():
        raise FileExistsError(f"评价目录已存在，拒绝覆盖：{output}")
    config = load_v13_config(root / "config/wavewarn_v13.yaml")
    prepared = prepare_v13(config, load_development_inputs(root, config.base.development_end()))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".evaluation_development_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        result = write_v13_evaluation(prepared, config, staging)
        staging.rename(output)
    trace = result.trace
    return V13Run(output, len(result.results), prepared.t0, prepared.tau, prepared.first_loss_day,
                  trace.exit_version, trace.p1.key if trace.p1 else None,
                  trace.final_n.key if trace.final_n else None, trace.n_prime_used, trace.stopped)
