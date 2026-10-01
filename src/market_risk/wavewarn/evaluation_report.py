"""v1.2.1 开发期评价的报告正文与列说明（纯计算，只生成文本行）。"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.evaluation import (
    RANKED_MODELS,
    RECONCILE_TOLERANCE,
    SCOPE_CATEGORIES,
    PreparedEvaluation,
    ReferenceResult,
)
from market_risk.wavewarn.evaluation_tables import (
    MISS_PENALTY_COLUMN,
    STATUS_NOTE,
    ModelSummary,
    Row,
    decided_by,
    ranked,
)
from market_risk.wavewarn.ledgers import EVENT_CLASSES
from market_risk.wavewarn.loss import LossSettings


@dataclass(frozen=True)
class ReportTables:
    """报告用到的全部汇总行；逐日与逐事件明细只在 CSV 中。"""

    summaries: tuple[ModelSummary, ...]
    references: tuple[ReferenceResult, ...]
    scope_counts: tuple[Row, ...]
    event_classes: tuple[Row, ...]
    alert_summaries: tuple[Row, ...]
    missing: tuple[Row, ...]
    n_exit_summary: tuple[Row, ...]
    tau_n: dt.date
    last_interval_start: dt.date


def _num(value: object) -> str:
    """报告中的损失保留 6 位小数；CSV 保留完整精度。"""
    return f"{value:.6f}" if isinstance(value, Decimal) else str(value)


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(_num(cell) for cell in row) + " |" for row in rows)
    return lines


def _setting(row: ModelSummary) -> tuple[object, ...]:
    candidate = row.candidate
    return candidate.k, candidate.theta_p, candidate.q if candidate.q is not None else "—"


def header_lines(prepared: PreparedEvaluation, tables: ReportTables, settings: LossSettings) -> list[str]:
    """口径、起点与参数；写明本报告不是什么。"""
    params = settings.parameters
    spans = []
    for model in ("P0", "P1", "N", "N去B/DV"):
        dates = [states.convergence_date for states in prepared.states if states.candidate.model == model]
        spans.append(f"{model} {min(dates)} 至 {max(dates)}（{len(dates)} 组）")
    return [
        f"# v1.2.1 开发期评价（{STATUS_NOTE}）", "",
        f"**{STATUS_NOTE}。** 只使用截至 {prepared.config.development_end()} 的开发期数据；唯一退出版本为 E2；"
        "没有写锁定记录，没有运行验证期检验，C-map 暂未计算。下列数字只用于核对实现与排出开发期名次，"
        "不能称为历史预警效果，也不代表实盘有效。", "",
        "## 评价起点与参数", "",
        f"- t0（N 的输入首次全部具备）：{prepared.t0}；τ = max(t0 + 63 个交易日, 全部 45 组设定的系统收敛日)"
        f" = {prepared.tau}；第一个计入损失的区间 j₀ = max(τ, 最晚收敛日后第 2 个交易日) = "
        f"{prepared.first_loss_day}。",
        f"- 最后一个计入区间的起点：{tables.last_interval_start}；每个设定计入 "
        f"{tables.summaries[0].intervals} 个区间。回撤参考高点在 j₀ 当天按收盘价重置。",
        "- 系统收敛日：" + "；".join(spans) + "。逐组见 `convergence.csv`。",
        f"- 损失参数：η={params.eta}，κ_D={params.kappa_d}，β={params.beta}，κ_0={params.kappa_0}，"
        f"c_s={params.switch_fraction}（γ={params.gamma}），噪声下限={params.noise_floor}，μ={settings.mu}，"
        f"权重 SPX={settings.weight_spx}、QQQ={settings.weight_qqq}；次日收盘执行。",
        "- 全部模型共用同一个 τ、j₀ 与日期轴；被排除的价格区间仍计入执行非绿天数与计费切换次数。",
        "- 核对：对数按 Decimal 28 位有效数字计算，相加次序不同只在末位有舍入差。各设定“分项之和 − 逐日合计”"
        f"的最大绝对差为 {max(row.parts_gap for row in tables.summaries):.1E}（容差 {RECONCILE_TOLERANCE}）；"
        "逐日明细中同日两行合计与系统日度主损失按同一容差核对。", ""]


def reference_lines(references: Sequence[ReferenceResult]) -> list[str]:
    rows = [(row.name, row.exposure, row.total_loss, row.danger_loss, row.drawdown_loss,
             row.opportunity_loss, row.switch_cost, row.executed_non_green_days) for row in references]
    return ["### 参照行（恒定暴露、零切换；不参与选参与检验）", "",
            *_table(("参照", "暴露", "主损失", "危险项", "回撤项", "机会项", "切换项", "执行非绿天数"), rows), ""]


RANK_COLUMNS = ("名次", "K", "θ_P", "q", "主损失", "危险项", "回撤项", "机会项", "切换项", "执行非绿天数",
                "计费切换次数", "与下一名由何决出")


def ranking_lines(summaries: Sequence[ModelSummary]) -> list[str]:
    """每类模型内的开发期名次；解释性分解按登记顺序列出，不排名。"""
    lines = [f"### 排名表（{STATUS_NOTE}）", "",
             "每类模型内主损失最小者居首；并列依次比较执行非绿天数、计费切换次数、登记顺序。"
             "执行非绿天数 = 计入区间中执行灯色 S_{j−1} 非绿的天数（系统级计一次）；"
             "计费切换次数不含窗口末日的切换。", ""]
    for model in RANKED_MODELS:
        ordered = ranked(summaries, model)
        rows = [(index + 1, *_setting(row), row.total_loss, row.danger_loss, row.drawdown_loss,
                 row.opportunity_loss, row.switch_cost, row.executed_non_green_days, row.billed_switches,
                 decided_by(row, ordered[index + 1]) if index + 1 < len(ordered) else "—")
                for index, row in enumerate(ordered)]
        lines.extend([f"**{model}**", "", *_table(RANK_COLUMNS, rows), ""])
    rest = [row for row in summaries if row.candidate.model not in RANKED_MODELS]
    rows = [("—", *_setting(row), row.total_loss, row.danger_loss, row.drawdown_loss, row.opportunity_loss,
             row.switch_cost, row.executed_non_green_days, row.billed_switches, "不参与选参") for row in rest]
    lines.extend(["**N 去掉 B、DV（仅 P、PR、BW、V）：解释性分解，按登记顺序，不排名**", "",
                  *_table(RANK_COLUMNS, rows), ""])
    return lines


def scope_lines(scope_counts: Sequence[Row]) -> list[str]:
    """跨起点、完整纳入、仅因高点前 20 天不足而未进事件账的件数。"""
    return ["### 事件纳入范围（与模型设定无关）", "",
            "逐日危险项、回撤项、机会项按区间日期 ≥ j₀ 计入，不论事件高点位置：跨起点事件（P < j₀ ≤ Tr）"
            "只计 j₀ 之后的部分。“危险区间全程满暴露”罚项只纳入 P ≥ j₀ 的已确认事件；"
            "事件账五类另要求 P 前 20 个交易日不早于 τ。尾段（寻峰）没有事件，不在此表。", "",
            *_table(("资产", "事件数", *SCOPE_CATEGORIES, "罚项口径纳入", "事件账纳入"), scope_counts), "",
            "“完整纳入但高点前20天不足”即仅因高点前 20 天灯色不足而未进事件账的事件；"
            "罚项口径纳入 = 两种“完整纳入”之和。逐件见 `event_scope.csv`。", ""]


def n_exit_lines(rows: Sequence[Row], tau_n: dt.date) -> list[str]:
    body = [(row[1], row[2], row[3], row[4], f"{row[6]}/{row[7]}/{row[8]}", f"{row[9]}/{row[10]}/{row[11]}",
             f"{row[12]}：{_num(row[13])}/{_num(row[14])}/{_num(row[15])}",
             f"{row[16]}：{_num(row[17])}/{_num(row[18])}/{_num(row[19])}") for row in rows]
    return ["### N 各设定的 E2 退出代价描述（不作范围判定，不用于选参）", "",
            f"口径同 `exit_costs`；N 用自己 18 组设定确定的纳入门槛 τ_N = {tau_n}。"
            "R 为价格收复比例，半山腰跌幅为 `[P,Tr)` 内转绿执行价至最终低点的跌幅；空白表示没有样本。", "",
            *_table(("K", "θ_P", "q", "资产", "纳入/状态窗口不足/右截尾", "①/②/③", "半山腰 n：中位/P75/最大",
                     "R n：中位/P75/最大"), body), ""]


def main_loss_lines(tables: ReportTables) -> list[str]:
    return ["## 一、主损失", "", *reference_lines(tables.references), *ranking_lines(tables.summaries),
            "配对检验与稳健性报告属于验证期，本次没有运行。", "",
            *scope_lines(tables.scope_counts), *n_exit_lines(tables.n_exit_summary, tables.tau_n)]


def event_lines(rows: Sequence[Row]) -> list[str]:
    """每个设定一行，SPX、QQQ、合并事件各给五类件数。"""
    by_setting: dict[Row, dict[object, Row]] = {}
    for row in rows:
        by_setting.setdefault(row[:4], {})[row[4]] = row
    body = []
    for prefix, scopes in by_setting.items():
        cells = []
        for scope in ("SPX", "QQQ", "合并"):
            row = scopes[scope]
            cells.extend((row[7], "/".join(str(value) for value in row[8:13]), f"{row[13]}/{row[14]}"))
        body.append((*(cell if cell != "" else "—" for cell in prefix), *cells,
                     f"{scopes['SPX'][15]}/{scopes['QQQ'][15]}"))
    columns = [name for scope in ("SPX", "QQQ", "合并")
               for name in (f"{scope} 纳入", f"{scope} 五类", f"{scope} T0前信号/已执行")]
    return ["## 二、事件账", "",
            "五类依次为 " + "/".join(EVENT_CLASSES) + "，只统计纳入事件账的已确认事件（资产事件与合并事件"
            "都按各自的 P 前 20 个交易日不早于 τ 纳入）。“T0前信号/已执行”是 S_{T0−1} 非绿与 S_{T0−2} 非绿的件数。"
            "末列为首个警报段在最终低点之前就已转绿的事件数（SPX/QQQ）。"
            "转绿后 5、10、20 日表现、首次亮灯与解除时的通道逐件见 `event_ledger.csv`。", "",
            *_table(("模型", "K", "θ_P", "q", *columns, "低点前转绿"), body), ""]


def alert_lines(rows: Sequence[Row]) -> list[str]:
    body = [(*(cell if cell != "" else "—" for cell in row[:4]), row[4], f"{row[5]}/{row[6]}/{row[7]}",
             *row[8:]) for row in rows]
    return ["## 三、警报账", "",
            "警报段 = 连续的信号非绿日。保护效果 = 危险区间内因降低暴露而避开的下跌（加权对数跌幅）；"
            "占用成本 = 危险区间外的非绿天数与净机会成本。无事件警报段与任何事件的 [P−20, Tr+5] 都不重叠。"
            "逐段见 `alert_ledger.csv`；每年非绿比例、最长警报段、切换次数与各通道激活天数见 `yearly_alert.csv`。", "",
            *_table(("模型", "K", "θ_P", "q", "警报段数", "关联事件/无事件/尾段待定", "最长段（日）",
                     "信号非绿天数", "保护效果", "区间外非绿天数", "净机会成本"), body), ""]


def missing_lines(rows: Sequence[Row], settings_count: int) -> list[str]:
    """沿用日与被排除区间；各设定完全相同的行合并显示并注明设定数。"""
    grouped: Counter[Row] = Counter(row[4:] for row in rows)
    body = [(*key, f"{count}/{settings_count}") for key, count in grouped.items()]
    carried = sum(int(str(row[6])) for row in rows if row[4] == "系统")
    lines = ["## 四、缺值情况", "",
             *_table(("范围", "原因", "数量", "首日", "末日", "出现于多少个设定"), body), ""]
    if carried == 0:
        lines.append("全部设定在 j₀ 之后没有“沿用”日，即每个交易日全部通道的输入与降级所需输入都有效："
                     "各通道无效日数量为 0，因缺值推迟的降级次数为 0；SPX、QQQ 没有跨缺价区间。")
    else:
        lines.append(f"存在“沿用”日（各设定合计 {carried} 个设定日）。逐通道无效日数量与因缺值推迟的降级次数"
                     "尚未拆分，记“待补”，需负责人确认统计口径后补充。")
    lines.extend(["被排除的只是该资产在该区间的价格类损失；切换罚分照常计入。逐设定见 `missing_audit.csv`。", ""])
    return lines


def report_lines(prepared: PreparedEvaluation, tables: ReportTables, settings: LossSettings) -> list[str]:
    """四部分分别呈现：主损失、事件账、警报账、缺值。"""
    return [*header_lines(prepared, tables, settings), *main_loss_lines(tables),
            *event_lines(tables.event_classes), *alert_lines(tables.alert_summaries),
            *missing_lines(tables.missing, len(tables.summaries))]


def readme_lines(prepared: PreparedEvaluation, settings: LossSettings) -> list[str]:
    """逐日明细的列名、单位与公式，以及本目录各文件的用途。"""
    gamma = settings.parameters.gamma
    return [
        f"# 开发期 v1.2.1 评价输出说明（{STATUS_NOTE}）", "",
        f"只使用截至 {prepared.config.development_end()} 的开发期输入；不是历史预警效果。"
        f"t0={prepared.t0}，τ={prepared.tau}，j₀={prepared.first_loss_day}。", "",
        "## `daily_asset_intervals.csv`：每个模型设定 × 资产 × 区间一行", "",
        "- `date`→`next_date`：价格区间的起点与终点（交易日）。"
        "`close`、`next_close`：两端的不复权收盘价（指数点或美元）。",
        "- `signal_light`：当日收盘产生的信号灯色 S_j。`system_executed_light`：当日收盘执行的系统灯色 S_{j−1}，"
        "决定本区间暴露。`executed_light`：该资产当日收盘后的实际灯色（缺价日保持前值）。",
        f"- `exposure`：本区间暴露 e，绿=1、黄=η={settings.parameters.eta}、红=0（无量纲）。",
        "- `log_return` = ln(next_close ÷ close)（无量纲，对数收益）。",
        "- `dangerous`：是 = 区间属于该资产危险区间 [P, Tr)；否；未定 = 右截尾或尾段，不计价格损失。",
        "- `drawdown_increment`：ΔX，超过 2% 噪声下限的回撤纪录增量（对数，无量纲）。",
        "- `danger_loss_raw` = κ_D × e × max(−r, 0)（仅危险区间）；`drawdown_loss_raw` = κ_0 × e × ΔX（仅区间外）；"
        "`opportunity_loss_raw` = β × (1−e) × r（仅区间外，可为负）。三者是该资产**未乘权重**的原始值。",
        "- `weight`：资产权重 w_a。`weighted_price_loss` = w_a × (危险项 + 回撤项 + 机会项)。",
        "- `system_switch_count`：当日系统执行切换次数（0 或 1），两条资产行相同。",
        f"- `switch_cost_share` = γ × 计费切换次数 ÷ 2，γ={gamma}：按两条资产行均分，与 w_a 无关。"
        "窗口末日的切换两行都显示切换标记，但分摊额为 0（`terminal_switch_unbilled`=是）。",
        f"- `{MISS_PENALTY_COLUMN}` = μ × 该资产在本行触发的事件数。罚项属于某一资产的某一事件，"
        "整笔记在该资产区间 Tr−1 的那一行，另一资产同日记 0，不乘资产权重。"
        f"**当前 μ={settings.mu}，该列恒为 0。**",
        f"- `row_total` = `weighted_price_loss` + `switch_cost_share` + `{MISS_PENALTY_COLUMN}`。"
        "同一日期两行合计等于系统日度主损失；全部行合计等于该设定的主损失"
        f"（相加次序不同带来的 Decimal 末位舍入差不超过 {RECONCILE_TOLERANCE}）。配对检验使用两行合计的系统级损失差。",
        "- `excluded_reason`：跨缺价区间、右截尾（寻底）、尾段（寻峰）或窗口末日。被排除区间的价格项为 0，"
        "切换分摊额照常计入。",
        "- `data_status`、`active_channels`、`state_reason`：当日系统数据状态、激活的通道、状态机给出的原因。", "",
        "## 其他文件", "",
        "- `model_summary.csv`：每个设定的主损失与分项、执行非绿天数、计费切换次数、被排除区间数、沿用日数。",
        f"- `ranking.csv`：每类模型内的名次（{STATUS_NOTE}）；`reference_rows.csv`：始终绿、黄、红三条参照行。",
        "- `convergence.csv`：各设定系统收敛日与共同的 t0、τ、j₀。",
        "- `event_scope.csv`、`event_scope_counts.csv`：每个资产事件的纳入情形与件数。",
        "- `event_ledger.csv`、`event_class_summary.csv`：事件账逐件（资产事件与合并事件）与五类件数。",
        "- `alert_ledger.csv`、`alert_summary.csv`、`yearly_alert.csv`：警报账逐段、汇总与年度统计"
        "（年度非绿天数按信号灯色，年度切换含窗口末日那一次）。",
        "- `missing_audit.csv`：沿用日与被排除区间的数量及首末日期。",
        "- `n_exit_costs.csv`、`n_exit_cost_summary.csv`：N 各设定的 E2 退出代价描述，不作范围判定。",
        "- `开发期评价报告.md`：主损失、事件账、警报账、缺值四部分。", ""]
