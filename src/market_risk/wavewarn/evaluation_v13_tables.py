"""v1.3 开发期评价的表格行（纯计算）：全部设定汇总、参照行、选择过程追踪、选定设定的逐日明细。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from market_risk.wavewarn.calibration import shown
from market_risk.wavewarn.evaluation import Candidate, CandidateEvaluation, PreparedEvaluation
from market_risk.wavewarn.evaluation_tables import DAILY_HEADER, Row, daily_rows
from market_risk.wavewarn.evaluation_v13 import SettingResult, display_model, display_version
from market_risk.wavewarn.feasibility import AssetRebound, SelectionItem, SelectionTrace
from market_risk.wavewarn.timing import ReferenceRow

STATUS_NOTE = "开发期工程运行，未锁定"
SETTING_HEADER = ("model", "exit_version", "k", "theta_p", "q")
REBOUND_FIELDS = ("included", "class_1", "class_2", "class_3", "r_median", "r_p75", "r_conservative_median",
                  "r_conservative_p75", "half_way_n", "half_way_median", "half_way_max")
SUMMARY_HEADER = (*SETTING_HEADER, "registration_order", "convergence_date", "intervals", "total_loss",
                  "danger_loss", "drawdown_loss", "opportunity_loss", "switch_cost", "miss_penalty",
                  "mean_exposure", "benchmark_loss", "timing_score", "non_green_share",
                  "executed_non_green_days", "billed_switches", "carried_days",
                  *(f"{symbol}_{name}" for symbol in ("spx", "qqq") for name in REBOUND_FIELDS),
                  "non_green_ok", "spx_r_median_ok", "spx_r_p75_ok", "qqq_r_median_ok", "qqq_r_p75_ok",
                  "feasible", "infeasible_reason", "selected_as")


def setting_prefix(candidate: Candidate) -> Row:
    return (display_model(candidate.model), display_version(candidate), candidate.k, candidate.theta_p,
            candidate.q if candidate.q is not None else "")


def condition(value: bool | None) -> str:
    """可行条件的一项：是、否，或该资产类别②无样本。"""
    return "无样本" if value is None else "是" if value else "否"


def rebound_cells(rebound: AssetRebound) -> Row:
    """主口径与保守口径的 R 中位数、P75；保守口径涉及类别③时显示“超限”。"""
    return (rebound.included, rebound.class_1, rebound.class_2, rebound.class_3, shown(rebound.main.median),
            shown(rebound.main.p75), shown(rebound.conservative.median), shown(rebound.conservative.p75),
            rebound.half_way.n, shown(rebound.half_way.median), shown(rebound.half_way.maximum))


def summary_row(result: SettingResult, selected_as: str) -> Row:
    summary, timing, check = result.summary, result.timing, result.feasibility
    return (*setting_prefix(result.candidate), result.candidate.order, summary.convergence_date, timing.intervals,
            summary.total_loss, summary.danger_loss, summary.drawdown_loss, summary.opportunity_loss,
            summary.switch_cost, summary.miss_penalty, timing.mean_exposure, timing.benchmark_loss, timing.score,
            timing.non_green_share, summary.executed_non_green_days, summary.billed_switches, summary.carried_days,
            *rebound_cells(result.rebounds["SPX"]), *rebound_cells(result.rebounds["QQQ"]),
            condition(check.non_green_ok), condition(check.median_ok["SPX"]), condition(check.p75_ok["SPX"]),
            condition(check.median_ok["QQQ"]), condition(check.p75_ok["QQQ"]), condition(check.feasible),
            check.note, selected_as)


def selected_labels(trace: SelectionTrace, p0_best: SelectionItem | None) -> dict[str, str]:
    """设定 key → 在汇总表“selected_as”列显示的文字。"""
    labels: dict[str, str] = {}
    if trace.p1 is not None:
        labels[trace.p1.key] = "选定 P1"
    if trace.final_n is not None:
        labels[trace.final_n.key] = "选定 N′（后备）" if trace.n_prime_used else "选定 N"
    if p0_best is not None:
        labels[p0_best.key] = "P0 可行设定中 T 最小（只作描述）"
    return labels


def summary_rows(results: Sequence[SettingResult], labels: Mapping[str, str]) -> tuple[Row, ...]:
    return tuple(summary_row(result, labels.get(result.key, "")) for result in results)


REFERENCE_HEADER = ("reference", "intervals", "total_loss", "danger_loss", "drawdown_loss", "opportunity_loss",
                    "switch_cost", "miss_penalty", "mean_exposure", "benchmark_loss", "timing_score",
                    "non_green_share", "executed_non_green_days", "billed_switches")


def reference_row(reference: ReferenceRow) -> Row:
    daily = reference.evaluated.daily_losses
    parts = [sum((getattr(row, name) for row in daily), Decimal(0))
             for name in ("danger_loss", "drawdown_loss", "opportunity_loss", "switch_cost", "full_exposure_cost")]
    timing = reference.timing
    return (reference.name, timing.intervals, reference.evaluated.total_loss, *parts, timing.mean_exposure,
            timing.benchmark_loss, timing.score, timing.non_green_share,
            reference.evaluated.executed_non_green_days, reference.evaluated.billed_switches)


TRACE_HEADER = ("step", "item", "value")


def _item_text(item: SelectionItem | None) -> str:
    return "无" if item is None else f"{item.key}（T={item.score}）"


def trace_rows(trace: SelectionTrace, p0_best: SelectionItem | None) -> tuple[Row, ...]:
    """选择程序的逐步追踪；P0 的一行只作描述，单列在最后。"""
    rows: list[Row] = [("①", f"{version} 下可行的 P1 组数（共 9 组）", count)
                       for version, count in trace.feasible_p1]
    rows.append(("②", "选定退出版本（按 E2→X1→X2 取第一个至少一组 P1 可行者，不以 T 挑选）",
                 trace.exit_version or "无"))
    if trace.exit_version is not None:
        rows.extend([("③", "选定 P1（可行设定中 T 最小）", _item_text(trace.p1)),
                     ("④", "同一版本下可行的 N 组数（共 18 组）", trace.feasible_n),
                     ("④", "选定 N（可行设定中 T 最小）", _item_text(trace.n)),
                     ("第五节", "是否启用 N′ 后备", "是" if trace.n_prime_used else "否")])
        if trace.n_prime_used:
            rows.extend([("第五节", "同一版本下可行的 N′ 组数（共 9 组）", trace.feasible_n_prime),
                         ("第五节", "选定 N′（可行设定中 T 最小）", _item_text(trace.n_prime))])
    rows.append(("结果", "程序是否停止", trace.stopped or "未停止"))
    rows.append(("⑤", "P0（原护栏，只作描述）可行设定中 T 最小", _item_text(p0_best)))
    return tuple(rows)


V13_DAILY_HEADER = (*SETTING_HEADER, *DAILY_HEADER[4:])


def v13_daily_rows(prefix: Row, evaluated: CandidateEvaluation, weights: Mapping[str, Decimal],
                   prices: Mapping[str, Mapping]) -> tuple[Row, ...]:
    """逐日明细沿用 v1.2.1 的列，只把设定列换成含退出版本的五列。"""
    return tuple((*prefix, *row[4:]) for row in daily_rows(evaluated, weights, prices))


def reference_prefix(name: str) -> Row:
    return name, "参照行", "", "", ""


CONVERGENCE_HEADER = (*SETTING_HEADER, "convergence_date", "t0", "tau", "first_loss_day")


def convergence_rows(prepared: PreparedEvaluation) -> tuple[Row, ...]:
    return tuple((*setting_prefix(states.candidate), states.convergence_date, prepared.t0, prepared.tau,
                  prepared.first_loss_day) for states in prepared.states)
