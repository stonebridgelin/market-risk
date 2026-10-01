"""v1.3 补充历史的表格行与报告正文（纯计算）。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal

from market_risk.wavewarn.calibration import CalibrationTables, event_section, fixed_section, shown
from market_risk.wavewarn.config_v13 import ExtendedHistoryWindows
from market_risk.wavewarn.evaluation import PreparedEvaluation
from market_risk.wavewarn.evaluation_tables import Row
from market_risk.wavewarn.extended_history import CROSSING_END, P0WindowResult, P0WindowRow
from market_risk.wavewarn.feasibility import AssetRebound
from market_risk.wavewarn.timing import ReferenceRow

P0_HEADER = ("k", "theta_p", "convergence_date", "intervals", "total_loss", "danger_loss", "drawdown_loss",
             "opportunity_loss", "switch_cost", "mean_exposure", "benchmark_loss", "timing_score",
             "non_green_share", "executed_non_green_days", "billed_switches",
             *(f"{symbol}_{name}" for symbol in ("spx", "qqq")
               for name in ("included", "class_1", "class_2", "class_3", "half_way_n", "half_way_median",
                            "half_way_max", "r_median", "r_p75", "r_conservative_median",
                            "r_conservative_p75")))
EXIT_HEADER = ("k", "theta_p", "symbol", "peak_date", "trough_date", "inclusion", "rebound_class",
               "half_way_green_count", "deepest_decline", "rebound_recovery", "first_green_from_trough")
CROSSING_HEADER = ("symbol", "peak_date", "t0_date", "trough_date", "status")


def _rebound_cells(rebound: AssetRebound) -> Row:
    return (rebound.included, rebound.class_1, rebound.class_2, rebound.class_3, rebound.half_way.n,
            shown(rebound.half_way.median), shown(rebound.half_way.maximum), shown(rebound.main.median),
            shown(rebound.main.p75), shown(rebound.conservative.median), shown(rebound.conservative.p75))


def p0_row(row: P0WindowRow) -> Row:
    summary, timing = row.summary, row.timing
    return (row.candidate.k, row.candidate.theta_p, summary.convergence_date, timing.intervals,
            summary.total_loss, summary.danger_loss, summary.drawdown_loss, summary.opportunity_loss,
            summary.switch_cost, timing.mean_exposure, timing.benchmark_loss, timing.score,
            timing.non_green_share, summary.executed_non_green_days, summary.billed_switches,
            *_rebound_cells(row.rebounds["SPX"]), *_rebound_cells(row.rebounds["QQQ"]))


def exit_rows(result: P0WindowResult) -> tuple[Row, ...]:
    return tuple((candidate.k, candidate.theta_p, cost.symbol, cost.peak_date, cost.trough_date, cost.inclusion,
                  cost.rebound_class or "", cost.half_way_green_count,
                  cost.deepest_decline if cost.deepest_decline is not None else "",
                  cost.rebound_recovery if cost.rebound_recovery is not None else "",
                  cost.first_green_from_trough or "") for candidate, cost in result.exit_costs)


def crossing_rows(result: P0WindowResult) -> tuple[Row, ...]:
    return tuple((symbol, event.peak_date, event.t0_date, event.trough_date, CROSSING_END)
                 for symbol, events in result.crossing.items() for event in events)


def _num(value: object) -> str:
    return f"{value:.6f}" if isinstance(value, Decimal) else str(value)


def _share(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(_num(cell) for cell in row) + " |" for row in rows)
    return lines


def fixed_delay_lines(windows: ExtendedHistoryWindows, included: Mapping[str, int],
                      tables: CalibrationTables) -> list[str]:
    """固定延迟校准一节：写明窗口只限定事件、不限定价格查询。"""
    return [
        "## 一、固定延迟校准", "",
        f"SPX {windows.fixed_delay_start['SPX']} 至 {windows.fixed_delay_end}、QQQ "
        f"{windows.fixed_delay_start['QQQ']} 至 {windows.fixed_delay_end} 的已确认事件：高点 P 不早于窗口起点、"
        f"低点 Tr 不晚于窗口末日。SPX 纳入 {included['SPX']} 件，QQQ 纳入 {included['QQQ']} 件。算法与任务 D 相同，"
        "m = 0、1、2、3、5、8、10、15。", "",
        "**窗口只限定纳入哪些事件，不限定价格查询**：执行日 Tr+m 超出窗口末日时，读取窗口之后的价格"
        "（截至 2016-12-30，仍属开发期）。判断顺序不变：先看 Tr+m 是否不早于同资产下一事件 T0（取自标签文件），"
        "是则归类别③；否则计算 R。", "",
        *event_section(tables.event_rows), *fixed_section(tables.fixed_summary), ""]


def reference_lines(references: Sequence[ReferenceRow]) -> list[str]:
    rows = [(row.name, row.evaluated.total_loss, row.timing.mean_exposure, row.timing.score,
             _share(row.timing.non_green_share), row.evaluated.billed_switches) for row in references]
    return ["### 参照行（同一窗口、同一损失函数与 j₀′）", "",
            *_table(("参照", "主损失 L", "ē", "T", "非绿占比", "计费切换次数"), rows), ""]


def _asset_text(rebound: AssetRebound) -> tuple[str, str, str]:
    counts = f"{rebound.included}：{rebound.class_1}/{rebound.class_2}/{rebound.class_3}"
    half = (f"{rebound.half_way.n}：{_num(rebound.half_way.median)}/{_num(rebound.half_way.maximum)}"
            if rebound.half_way.n else "0")
    recovery = (f"{_num(rebound.main.median)}/{_num(rebound.main.p75)}"
                f"（{shown(rebound.conservative.median)[:8]}/{shown(rebound.conservative.p75)[:8]}）"
                if rebound.main.n else "无样本")
    return counts, half, recovery


def p0_lines(prepared: PreparedEvaluation, start: dt.date, result: P0WindowResult) -> list[str]:
    """P0 稳健性一节：起点、参照行、九组设定、跨窗口末端的事件。"""
    dates = [row.summary.convergence_date for row in result.rows]
    body = [(row.candidate.k, row.candidate.theta_p, row.summary.total_loss, row.timing.mean_exposure,
             row.timing.score, _share(row.timing.non_green_share), row.summary.billed_switches,
             *_asset_text(row.rebounds["SPX"]), *_asset_text(row.rebounds["QQQ"])) for row in result.rows]
    crossing = crossing_rows(result)
    carried = sum(int(str(row[6])) for row in result.missing if row[4] == "系统")
    excluded = sorted({(row[4], row[5], row[6], row[7], row[8]) for row in result.missing if row[4] != "系统"},
                      key=str)
    return [
        "## 二、P0 稳健性", "",
        f"窗口 {start} 至 {prepared.inputs.days[-1]}。t0′ = {prepared.t0}（窗口内两资产 P0 所需输入首次全部具备："
        f"收盘价、63 日高点与回撤、20 日新低窗口、MA50）；τ′ = {prepared.tau}；j₀′ = {prepared.first_loss_day}；"
        f"计入区间数 {result.rows[0].timing.intervals}。九组 P0 的系统收敛日为 {min(dates)} 至 {max(dates)}。"
        "状态与执行只算到窗口末日；危险标签取自完整标签文件。"
        "SPX 的滚动窗口（63 日高点、均线）用到了窗口起点之前的 SPX 价格。", "",
        *reference_lines(result.references),
        "### 九组 P0", "",
        "“纳入：①/②/③”为退出代价纳入事件数（P ≥ τ′ 且 Tr ≤ 窗口末日）及三类件数；到窗口末日仍未转绿的归类别③。"
        "“半山腰”为发生半山腰转绿的事件数：最深跌幅的中位/最大。R 为主口径中位/P75（保守口径）。", "",
        *_table(("K", "θ_P", "主损失 L", "ē", "T", "非绿占比", "切换", "SPX 纳入：①/②/③", "SPX 半山腰",
                 "SPX R", "QQQ 纳入：①/②/③", "QQQ 半山腰", "QQQ R"), body), "",
        f"### {CROSSING_END}", "",
        *( _table(("资产", "高点 P", "T0", "低点 Tr", "处理"), crossing) if crossing
           else ["没有高点在窗口内、低点在窗口之后的事件。"]), "",
        "这类事件在窗口内的危险区间照常计入主损失，但不计入半山腰转绿与 R，也不判定“危险区间全程满暴露”。", "",
        "### 缺值", "",
        f"九组 P0 的“沿用”日合计 {carried} 个设定日。被排除的资产区间（各设定去重后）：",
        *([f"- {scope}：{reason}，{count} 个，{first} 至 {last}" for scope, reason, count, first, last in excluded]
          or ["- 无。"]), ""]


def report_lines(prepared: PreparedEvaluation, windows: ExtendedHistoryWindows, included: Mapping[str, int],
                 tables: CalibrationTables, result: P0WindowResult) -> list[str]:
    return [
        "# v1.3 补充历史（描述性，不参与选参与检验）", "",
        "登记依据：`docs/research/波段预警研究规格_v1.3_修订登记.md` 第六节与“实施细则”第 2 条。"
        "本报告与开发期评价分开；只使用 2016-12-30 及以前的数据，ZZ 标签取自现有开发期标签文件。"
        "数字只作描述，不能称为历史预警效果。", "",
        *fixed_delay_lines(windows, included, tables), *p0_lines(prepared, windows.p0_start, result)]
