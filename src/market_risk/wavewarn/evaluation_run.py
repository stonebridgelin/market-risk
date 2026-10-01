"""v1.2.1 开发期评价文件编排；参数排名只依据登记主损失。"""

from __future__ import annotations

import csv
import datetime as dt
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.evaluation import (
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    development_labels,
    evaluate_candidate,
    prepare_from_inputs,
)
from market_risk.wavewarn.evaluation_output import (
    ALERT_HEADER,
    DAILY_HEADER,
    EVENT_HEADER,
    YEARLY_HEADER,
    missing_counts,
    write_alert_rows,
    write_column_readme,
    write_daily_details,
    write_event_rows,
    write_n_exit_costs,
)
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.loss import configured_loss_settings


@dataclass(frozen=True)
class EvaluationSummary:
    model: str
    k: int
    theta_p: Decimal
    q: Decimal | None
    order: int
    convergence_date: dt.date
    t0: dt.date
    tau: dt.date
    first_loss_day: dt.date
    trading_days: int
    total_loss: Decimal
    danger_loss: Decimal
    drawdown_loss: Decimal
    opportunity_loss: Decimal
    switch_cost: Decimal
    full_exposure_cost: Decimal
    signal_non_green_days: int
    executed_switches: int
    excluded_asset_intervals: int
    data_carried_days: int
    event_counts: Counter[str]
    alert_counts: Counter[str]


SUMMARY_HEADER = ("model", "k", "theta_p", "q", "registration_order", "convergence_date", "t0", "tau",
                  "first_loss_day", "trading_days", "total_loss", "danger_loss", "drawdown_loss",
                  "opportunity_loss", "switch_cost", "full_exposure_cost", "signal_non_green_days",
                  "executed_switches", "excluded_asset_intervals", "data_carried_days")


def _summary(prepared: PreparedEvaluation, evaluated: CandidateEvaluation,
             convergence_date: dt.date, event_counts: Counter[str],
             alert_counts: Counter[str]) -> EvaluationSummary:
    daily = evaluated.daily_losses
    settings = configured_loss_settings(prepared.config)
    if settings.parameters.eta != Decimal("0.5"):
        raise ValueError("本次开发期评价的 η 与负责人确认不一致")
    return EvaluationSummary(
        evaluated.candidate.model, evaluated.candidate.k, evaluated.candidate.theta_p,
        evaluated.candidate.q, evaluated.candidate.order, convergence_date, prepared.t0,
        prepared.tau, prepared.first_loss_day, len(evaluated.days), evaluated.total_loss,
        sum((row.danger_loss for row in daily), Decimal(0)),
        sum((row.drawdown_loss for row in daily), Decimal(0)),
        sum((row.opportunity_loss for row in daily), Decimal(0)),
        sum((row.switch_cost for row in daily), Decimal(0)),
        sum((row.full_exposure_cost for row in daily), Decimal(0)),
        sum(light != "绿" for light in evaluated.signals),
        sum(row.switched for row in evaluated.executions["SPX"][:-1]),
        sum(bool(row.excluded_reason) for rows in evaluated.asset_losses.values() for row in rows),
        sum(status == "沿用" for status in evaluated.status), event_counts, alert_counts)


def _summary_row(row: EvaluationSummary) -> tuple[object, ...]:
    return (row.model, row.k, row.theta_p, row.q if row.q is not None else "", row.order,
            row.convergence_date, row.t0, row.tau, row.first_loss_day, row.trading_days,
            row.total_loss, row.danger_loss, row.drawdown_loss, row.opportunity_loss,
            row.switch_cost, row.full_exposure_cost, row.signal_non_green_days,
            row.executed_switches, row.excluded_asset_intervals, row.data_carried_days)


def _candidate_writers(stack: ExitStack, destination: Path) -> dict[str, csv.writer]:
    """只在边界层打开输出文件并写表头。"""
    schemas = {
        "daily_asset_intervals": DAILY_HEADER,
        "event_ledger": EVENT_HEADER,
        "alert_ledger": ALERT_HEADER,
        "yearly_alert": YEARLY_HEADER,
        "missing_audit": ("model", "k", "theta_p", "q", "reason", "days_or_intervals"),
    }
    writers = {}
    for stem, header in schemas.items():
        file = stack.enter_context((destination / f"{stem}.csv").open("w", encoding="utf-8", newline=""))
        writers[stem] = csv.writer(file)
        writers[stem].writerow(header)
    return writers


def _write_candidate(prepared: PreparedEvaluation, states: CandidateStates,
                     events: Mapping[str, Sequence[ZZEvent]], unknown: UnknownLabels,
                     weights: Mapping[str, Decimal], writers: Mapping[str, csv.writer]) -> EvaluationSummary:
    """逐个候选流式计算并写出；各计算函数保持独立。"""
    evaluated = evaluate_candidate(prepared, states, events, unknown)
    expected = prepared.inputs.days[prepared.inputs.days.index(prepared.first_loss_day):]
    if evaluated.days != expected:
        raise ValueError("模型评价日期轴不一致")
    write_daily_details(writers["daily_asset_intervals"], evaluated, weights, prepared.inputs.series)
    event_counts = write_event_rows(writers["event_ledger"], prepared, states, events)
    alert_counts = write_alert_rows(writers["alert_ledger"], writers["yearly_alert"],
                                    evaluated, events, weights)
    for reason, count in sorted(missing_counts(evaluated).items()):
        writers["missing_audit"].writerow((states.candidate.model, states.candidate.k,
                                            states.candidate.theta_p,
                                            states.candidate.q if states.candidate.q is not None else "",
                                            reason, count))
    return _summary(prepared, evaluated, states.convergence_date, event_counts, alert_counts)


def _write_summary_files(destination: Path, summaries: Sequence[EvaluationSummary]) -> None:
    """汇总与收敛日期按候选登记顺序导出。"""
    with (destination / "model_summary.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(SUMMARY_HEADER)
        writer.writerows(_summary_row(row) for row in summaries)
    with (destination / "convergence.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("model", "k", "theta_p", "q", "convergence_date", "t0", "tau", "first_loss_day"))
        writer.writerows((row.model, row.k, row.theta_p, row.q if row.q is not None else "",
                          row.convergence_date, row.t0, row.tau, row.first_loss_day)
                         for row in summaries)


def write_candidate_evaluation(prepared: PreparedEvaluation, destination: Path) -> tuple[EvaluationSummary, ...]:
    """45 组模型共享 t0/τ/j₀，逐日、事件、警报与缺值分别输出。"""
    if destination.exists():
        raise FileExistsError(f"评价目录已存在，拒绝覆盖：{destination}")
    destination.mkdir(parents=True)
    events, unknown = development_labels(prepared)
    weights = configured_loss_settings(prepared.config).weights
    with ExitStack() as stack:
        writers = _candidate_writers(stack, destination)
        summaries = tuple(_write_candidate(prepared, states, events, unknown, weights, writers)
                          for states in prepared.states)
    _write_summary_files(destination, summaries)
    tau_n, n_exit = write_n_exit_costs(destination / "n_exit_costs.csv", prepared, events)
    with (destination / "n_exit_cost_summary.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("k", "theta_p", "q", "symbol", "included", "state_insufficient", "right_censored",
                         "halfway_n", "halfway_median", "halfway_p75", "halfway_max",
                         "r_n", "r_median", "r_p75", "r_max", "tau_n"))
        writer.writerows((*row, tau_n) for row in n_exit)
    write_column_readme(destination / "README.md")
    return tuple(summaries)


def prepare_and_write(root: Path) -> tuple[PreparedEvaluation, tuple[EvaluationSummary, ...]]:
    """供 services 调用；这里只执行开发期，目标目录已存在则拒绝覆盖。"""
    output = root / "reports/research/wavewarn_v121/evaluation_development"
    if output.exists():
        raise FileExistsError(f"评价目录已存在，拒绝覆盖：{output}")
    config = load_wavewarn_config(root / "config/wavewarn_v121.yaml")
    inputs = load_development_inputs(root, config.development_end())
    prepared = prepare_from_inputs(config, inputs)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".evaluation_development_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        summaries = write_candidate_evaluation(prepared, staging)
        staging.rename(output)
    return prepared, summaries
