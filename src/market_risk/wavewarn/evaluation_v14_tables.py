"""v1.4 开发期评价的表格行（纯计算）：全部设定汇总、选择过程追踪、收敛日。"""

from __future__ import annotations

from collections.abc import Sequence

from market_risk.wavewarn.calibration import shown
from market_risk.wavewarn.config_v14 import DELAY_QQQ, DELAY_SPX, NON_GREEN, SelectionTier
from market_risk.wavewarn.evaluation import Candidate, PreparedEvaluation
from market_risk.wavewarn.evaluation_tables import Row
from market_risk.wavewarn.evaluation_v14 import V14Result, setting_label
from market_risk.wavewarn.feasibility import AssetRebound
from market_risk.wavewarn.feasibility_v14 import GreenDelay, TierTrace
from market_risk.wavewarn.v14_model import FULL

STATUS_NOTE = "开发期工程运行，未锁定"
SETTING_HEADER = ("model", "exit_version", "k", "theta_p", "q")
ASSET_FIELDS = ("included", "class_1", "class_2", "class_3", "green_delay_median", "green_delay_p75",
                "green_delay_max", "r_median", "r_p75", "r_conservative_median", "r_conservative_p75",
                "half_way_n", "half_way_median", "half_way_max")
SUMMARY_HEADER = (*SETTING_HEADER, "role", "registration_order", "convergence_date", "intervals", "total_loss",
                  "danger_loss", "drawdown_loss", "opportunity_loss", "switch_cost", "miss_penalty",
                  "mean_exposure", "benchmark_loss", "timing_score", "non_green_share",
                  "executed_non_green_days", "billed_switches", "carried_days",
                  *(f"{symbol}_{name}" for symbol in ("spx", "qqq") for name in ASSET_FIELDS),
                  "non_green_ok", "green_delay_spx_ok", "green_delay_qqq_ok", "all_conditions_ok", "selected_as")
ROLE_CANDIDATE = "候选"
ROLE_CONTROL = "描述性对照（不参与选择）"


def setting_prefix(candidate: Candidate) -> Row:
    return (setting_label(candidate), candidate.exit_version, candidate.k, candidate.theta_p,
            candidate.q if candidate.q is not None else "")


def yes_no(value: bool) -> str:
    return "是" if value else "否"


def asset_cells(delay: GreenDelay, rebound: AssetRebound) -> Row:
    """三类件数、转绿延迟分布、R（主口径与保守口径）与半山腰转绿。"""
    return (delay.included, delay.class_1, delay.class_2, delay.class_3, shown(delay.delays.median),
            shown(delay.delays.p75), shown(delay.delays.maximum), shown(rebound.main.median),
            shown(rebound.main.p75), shown(rebound.conservative.median), shown(rebound.conservative.p75),
            rebound.half_way.n, shown(rebound.half_way.median), shown(rebound.half_way.maximum))


def summary_row(result: V14Result, selected_key: str) -> Row:
    summary, timing, flags = result.summary, result.timing, result.flags
    candidate = result.candidate
    return (*setting_prefix(candidate), ROLE_CANDIDATE if candidate.model == FULL else ROLE_CONTROL,
            candidate.order, summary.convergence_date, timing.intervals, summary.total_loss, summary.danger_loss,
            summary.drawdown_loss, summary.opportunity_loss, summary.switch_cost, summary.miss_penalty,
            timing.mean_exposure, timing.benchmark_loss, timing.score, timing.non_green_share,
            summary.executed_non_green_days, summary.billed_switches, summary.carried_days,
            *asset_cells(result.delays["SPX"], result.rebounds["SPX"]),
            *asset_cells(result.delays["QQQ"], result.rebounds["QQQ"]),
            yes_no(flags[NON_GREEN]), yes_no(flags[DELAY_SPX]), yes_no(flags[DELAY_QQQ]),
            yes_no(all(flags.values())), "选定" if result.key == selected_key else "")


def summary_rows(results: Sequence[V14Result], selected_key: str) -> tuple[Row, ...]:
    return tuple(summary_row(result, selected_key) for result in results)


TRACE_HEADER = ("step", "item", "value")
TIER_TEXT = ("①三条全部满足", "②只满足非绿占比上限", "③全部候选")


def trace_rows(trace: TierTrace, tiers: Sequence[SelectionTier]) -> tuple[Row, ...]:
    """每一级的候选数、最终所在级别与标注、选定设定。"""
    rows: list[Row] = [(f"第{index + 1}级", f"{TIER_TEXT[index]} 的候选数（共 9 组）", size)
                       for index, size in enumerate(trace.sizes)]
    rows.extend([("结果", "最终所在级别", f"第{trace.tier}级：{TIER_TEXT[trace.tier - 1]}"),
                 ("结果", "标注", tiers[trace.tier - 1].note or "无（三条全部满足）"),
                 ("结果", "选定设定（该级中择时得分 T 最小）", trace.selected.key),
                 ("结果", "选定设定的 T", trace.selected.score)])
    return tuple(rows)


CONVERGENCE_HEADER = (*SETTING_HEADER, "convergence_date", "t0", "tau", "first_loss_day")


def convergence_rows(prepared: PreparedEvaluation) -> tuple[Row, ...]:
    return tuple((*setting_prefix(states.candidate), states.convergence_date, prepared.t0, prepared.tau,
                  prepared.first_loss_day) for states in prepared.states)
