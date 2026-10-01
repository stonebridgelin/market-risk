"""v1.2.1 开发期评价的读写边界：读配置与输入、调用纯计算、写出 CSV 与报告。

只执行开发期；不写锁定记录，不运行验证期。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.evaluation import (
    REFERENCE_LIGHTS,
    SYMBOLS,
    PreparedEvaluation,
    development_labels,
    development_scope_rule,
    evaluate_candidate,
    prepare_from_inputs,
    reference_evaluation,
)
from market_risk.wavewarn.evaluation_report import ReportTables, readme_lines, report_lines
from market_risk.wavewarn.evaluation_tables import (
    ALERT_HEADER,
    ALERT_SUMMARY_HEADER,
    CONVERGENCE_HEADER,
    DAILY_HEADER,
    EVENT_CLASS_HEADER,
    EVENT_HEADER,
    MISSING_HEADER,
    N_EXIT_HEADER,
    N_EXIT_SUMMARY_HEADER,
    RANKING_HEADER,
    REFERENCE_HEADER,
    SCOPE_COUNT_HEADER,
    SCOPE_HEADER,
    SUMMARY_HEADER,
    YEARLY_HEADER,
    CandidateTables,
    Row,
    candidate_tables,
    convergence_rows,
    event_scope_counts,
    event_scope_rows,
    n_exit_costs,
    n_exit_rows,
    n_exit_summary_row,
    n_exit_tau,
    ranking_rows,
    reference_rows,
    setting_prefix,
    summary_row,
)
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent, merge_zz_events
from market_risk.wavewarn.loss import configured_loss_settings

OUTPUT_DIR = "reports/research/wavewarn_v121/evaluation_development"
REPORT_NAME = "开发期评价报告.md"
STREAMED = {"daily_asset_intervals": DAILY_HEADER, "event_ledger": EVENT_HEADER, "alert_ledger": ALERT_HEADER,
            "yearly_alert": YEARLY_HEADER, "missing_audit": MISSING_HEADER}


@dataclass(frozen=True)
class EvaluationRun:
    """服务层返回值：输出位置与共同起点，可直接序列化。"""

    output: Path
    settings: int
    t0: dt.date
    tau: dt.date
    first_loss_day: dt.date


def file_sha256(path: Path) -> str:
    """分块读取，返回大写十六进制 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def write_csv(path: Path, header: Sequence[str], rows: Sequence[Row]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerows(rows)


def _write_streamed(destination: Path, prepared: PreparedEvaluation, events: Mapping[str, Sequence[ZZEvent]],
                    unknown: UnknownLabels) -> tuple[CandidateTables, ...]:
    """逐个设定计算并写出明细；返回各设定的汇总行（不含逐日明细）。"""
    merged = merge_zz_events(tuple(event for symbol in SYMBOLS for event in events[symbol]))
    rule = development_scope_rule(prepared.tau, prepared.first_loss_day)
    weights = configured_loss_settings(prepared.config).weights
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.first_loss_day):]
    results: list[CandidateTables] = []
    with ExitStack() as stack:
        writers = {}
        for stem, header in STREAMED.items():
            file = stack.enter_context((destination / f"{stem}.csv").open("w", encoding="utf-8", newline=""))
            writers[stem] = csv.writer(file)
            writers[stem].writerow(header)
        for states in prepared.states:
            evaluated = evaluate_candidate(prepared, states, events, unknown)
            if evaluated.days != axis:
                raise ValueError("模型评价日期轴不一致")
            tables = candidate_tables(prepared, states, evaluated, events, merged, rule, weights, unknown)
            writers["daily_asset_intervals"].writerows(tables.daily)
            writers["event_ledger"].writerows(tables.events)
            writers["alert_ledger"].writerows(tables.alerts)
            writers["yearly_alert"].writerows(tables.yearly)
            writers["missing_audit"].writerows(tables.missing)
            # 逐日明细已写出，不再留在内存中。
            results.append(replace(tables, daily=(), events=(), alerts=(), yearly=()))
    return tuple(results)


def _write_n_exit(destination: Path, prepared: PreparedEvaluation,
                  events: Mapping[str, Sequence[ZZEvent]]) -> tuple[dt.date, tuple[Row, ...]]:
    """N 的 18 组 E2 退出代价描述：逐事件与汇总。"""
    tau_n = n_exit_tau(prepared)
    details: list[Row] = []
    summary: list[Row] = []
    for states in prepared.states:
        if states.candidate.model != "N":
            continue
        prefix = setting_prefix(states.candidate)
        for symbol in SYMBOLS:
            costs = n_exit_costs(prepared, states, symbol, events[symbol], tau_n)
            details.extend(n_exit_rows(prefix, costs, tau_n))
            summary.append(n_exit_summary_row(prefix, symbol, costs, tau_n))
    write_csv(destination / "n_exit_costs.csv", N_EXIT_HEADER, details)
    write_csv(destination / "n_exit_cost_summary.csv", N_EXIT_SUMMARY_HEADER, summary)
    return tau_n, tuple(summary)


def write_development_evaluation(prepared: PreparedEvaluation, destination: Path) -> ReportTables:
    """45 组设定共用 t0、τ、j₀；明细、汇总、排名、参照行与报告全部写入 destination。"""
    if destination.exists():
        raise FileExistsError(f"评价目录已存在，拒绝覆盖：{destination}")
    destination.mkdir(parents=True)
    events, unknown = development_labels(prepared)
    rule = development_scope_rule(prepared.tau, prepared.first_loss_day)
    settings = configured_loss_settings(prepared.config)
    results = _write_streamed(destination, prepared, events, unknown)
    summaries = tuple(row.summary for row in results)
    references = tuple(reference_evaluation(prepared, name, light, events, unknown)
                       for name, light in REFERENCE_LIGHTS)
    scope_counts = event_scope_counts(prepared.inputs.days, events, rule)
    tau_n, n_exit_summary = _write_n_exit(destination, prepared, events)
    tables = ReportTables(
        summaries, references, scope_counts, tuple(row for item in results for row in item.event_classes),
        tuple(item.alert_summary for item in results), tuple(row for item in results for row in item.missing),
        n_exit_summary, tau_n, prepared.inputs.days[-2])
    write_csv(destination / "model_summary.csv", SUMMARY_HEADER, [summary_row(prepared, row) for row in summaries])
    write_csv(destination / "ranking.csv", RANKING_HEADER, ranking_rows(summaries))
    write_csv(destination / "reference_rows.csv", REFERENCE_HEADER, reference_rows(references))
    write_csv(destination / "convergence.csv", CONVERGENCE_HEADER, convergence_rows(prepared))
    write_csv(destination / "event_scope.csv", SCOPE_HEADER, event_scope_rows(prepared.inputs.days, events, rule))
    write_csv(destination / "event_scope_counts.csv", SCOPE_COUNT_HEADER, scope_counts)
    write_csv(destination / "event_class_summary.csv", EVENT_CLASS_HEADER, tables.event_classes)
    write_csv(destination / "alert_summary.csv", ALERT_SUMMARY_HEADER, tables.alert_summaries)
    (destination / REPORT_NAME).write_text("\n".join(report_lines(prepared, tables, settings)), encoding="utf-8")
    daily_sha256 = file_sha256(destination / "daily_asset_intervals.csv")
    (destination / "README.md").write_text("\n".join(readme_lines(prepared, settings, daily_sha256)),
                                           encoding="utf-8")
    return tables


def run_development_evaluation(root: Path) -> EvaluationRun:
    """供 services 调用；先写入临时目录，全部成功后再改名，目标目录已存在则拒绝覆盖。"""
    output = root / OUTPUT_DIR
    if output.exists():
        raise FileExistsError(f"评价目录已存在，拒绝覆盖：{output}")
    config = load_wavewarn_config(root / "config/wavewarn_v121.yaml")
    prepared = prepare_from_inputs(config, load_development_inputs(root, config.development_end()))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".evaluation_development_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        tables = write_development_evaluation(prepared, staging)
        staging.rename(output)
    return EvaluationRun(output, len(tables.summaries), prepared.t0, prepared.tau, prepared.first_loss_day)
