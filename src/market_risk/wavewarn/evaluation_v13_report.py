"""v1.3 开发期评价的报告正文与列说明（纯计算，只生成文本行）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.calibration import shown
from market_risk.wavewarn.config_v13 import FeasibilityLimits
from market_risk.wavewarn.evaluation import SCOPE_CATEGORIES, PreparedEvaluation
from market_risk.wavewarn.evaluation_report import alert_lines, event_lines, missing_lines, storage_lines
from market_risk.wavewarn.evaluation_tables import MISS_PENALTY_COLUMN, Row
from market_risk.wavewarn.evaluation_v13 import VERSIONED_MODELS, SettingResult, display_model, display_version
from market_risk.wavewarn.evaluation_v13_tables import STATUS_NOTE, condition, trace_rows
from market_risk.wavewarn.feasibility import SelectionItem, SelectionTrace
from market_risk.wavewarn.loss import LossSettings
from market_risk.wavewarn.timing import ReferenceRow, caution_gap

DISCLOSURE = ("**披露声明。** 择时得分、可行条件、退出版本 X1 与 X2、N′ 后备（登记第二至五节）是在 2026-10-01 "
              "看过 v1.2.1 开发期结果之后登记的，其中 N′ 后备是在看过非绿占比之后登记的。"
              "验证期（2017—2022）与保留期（2023 年起）没有运行、没有读取；验证期检验仍只运行一次。"
              "本报告是开发期工程运行，未锁定，没有写锁定记录。下列数字只用于核对实现与执行登记的选择程序，"
              "不能称为历史预警效果，也不代表实盘有效。")


@dataclass(frozen=True)
class V13ReportTables:
    """报告用到的全部结果；逐日明细只在 CSV 中。"""

    results: tuple[SettingResult, ...]
    references: tuple[ReferenceRow, ...]
    trace: SelectionTrace
    p0_best: SelectionItem | None
    scope_counts: tuple[Row, ...]
    event_classes: tuple[Row, ...]
    alert_summaries: tuple[Row, ...]
    missing: tuple[Row, ...]
    limits: FeasibilityLimits


def num(value: object) -> str:
    """报告中的小数保留 6 位；CSV 保留完整精度。"""
    return f"{value:.6f}" if isinstance(value, Decimal) else str(value)


def share(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(num(cell) for cell in row) + " |" for row in rows)
    return lines


def header_lines(prepared: PreparedEvaluation, tables: V13ReportTables, settings: LossSettings) -> list[str]:
    params, limits = settings.parameters, tables.limits
    spans = []
    for model, version in (("P0", "P0护栏"), *((model, version) for version in ("E2", "X1", "X2")
                                             for model in VERSIONED_MODELS)):
        dates = [row.summary.convergence_date for row in tables.results
                 if row.candidate.model == model and display_version(row.candidate) == version]
        spans.append(f"{display_model(model)}·{version} {min(dates)} 至 {max(dates)}（{len(dates)} 组）")
    return [
        f"# v1.3 开发期评价（{STATUS_NOTE}）", "", DISCLOSURE, "",
        "登记文件：`docs/research/波段预警研究规格_v1.3_修订登记.md`。"
        f"只使用截至 {prepared.config.development_end()} 的开发期数据。", "",
        "## 评价起点与参数", "",
        f"- t0 = {prepared.t0}；τ = max(t0 + 63 个交易日, 全部 {len(tables.results)} 组设定的系统收敛日) = "
        f"{prepared.tau}；j₀ = {prepared.first_loss_day}；计入区间数 |J| = {tables.results[0].timing.intervals}。",
        "- 参与统一 τ 与 j₀ 的模型为 P0、P1×3、N×3、N′×3。各组系统收敛日：" + "；".join(spans)
        + "。逐组见 `convergence.csv`。",
        f"- 损失参数同 v1.2.1：η={params.eta}，κ_D={params.kappa_d}，β={params.beta}，κ_0={params.kappa_0}，"
        f"γ={params.gamma}，噪声下限={params.noise_floor}，μ={settings.mu}，权重各 {settings.weight_spx}。",
        f"- 可行条件：非绿占比 ≤ {limits.non_green_share_max}；R 中位数 ≤ {limits.r_median_max} 且 "
        f"P75 ≤ {limits.r_p75_max}（主口径，类别②，SPX、QQQ 分别成立）。R 的事件纳入门槛为高点 P ≥ τ。",
        "- 择时得分 T = L − [ē·L_G + (1−ē)·L_R]，ē 为计入区间上执行暴露的平均；T 越小越好，"
        "T<0 表示优于同等平均暴露的恒定持仓。", ""]


def reference_lines(references: Sequence[ReferenceRow]) -> list[str]:
    rows = [(row.name, row.evaluated.total_loss, row.timing.mean_exposure, row.timing.benchmark_loss,
             row.timing.score, share(row.timing.non_green_share), row.evaluated.billed_switches)
            for row in references]
    return ["## 参照行（不参与选参与检验）", "",
            *table(("参照", "主损失 L", "ē", "同暴露基准", "T", "非绿占比", "计费切换次数"), rows), "",
            "始终绿、黄、红的 T 按定义为 0（表中为 Decimal 末位舍入差）。200日均线参照：SPX 收盘价不低于其 "
            "200 日均线为绿、否则为红，两资产共用，次日收盘执行，切换罚分照常。分项见 `reference_rows.csv`。", ""]


def trace_lines(tables: V13ReportTables) -> list[str]:
    trace = tables.trace
    lines = ["## 选择过程追踪", "", *table(("步骤", "事项", "结果"), trace_rows(trace, tables.p0_best)), ""]
    if trace.stopped:
        lines.extend([f"**选择程序停止：{trace.stopped}。** 按登记不放宽条件，本次到此为止。", ""])
    elif trace.n_prime_used:
        lines.extend(["**最终用于与 P1 比较的是 N′（后备），不是 N。** 该后备在看过非绿占比后登记。", ""])
    else:
        lines.extend(["最终用于与 P1 比较的是 N（未启用 N′ 后备）。", ""])
    return lines


DETAIL_COLUMNS = ("模型", "版本", "K", "θ_P", "q", "主损失 L", "ē", "T", "非绿占比", "切换",
                  "SPX R 中位/P75（保守）", "QQQ R 中位/P75（保守）", "可行", "不可行原因")


def _rebound_text(result: SettingResult, symbol: str) -> str:
    rebound = result.rebounds[symbol]
    main = f"{num(rebound.main.median)}/{num(rebound.main.p75)}" if rebound.main.n else "无样本"
    return (f"{main}（{shown(rebound.conservative.median)[:8]}/{shown(rebound.conservative.p75)[:8]}）"
            f" n={rebound.class_2}")


def detail_row(result: SettingResult) -> Row:
    candidate, timing = result.candidate, result.timing
    return (display_model(candidate.model), display_version(candidate), candidate.k, candidate.theta_p,
            candidate.q if candidate.q is not None else "—", result.summary.total_loss, timing.mean_exposure,
            timing.score, share(timing.non_green_share), result.summary.billed_switches,
            _rebound_text(result, "SPX"), _rebound_text(result, "QQQ"),
            condition(result.feasibility.feasible), result.feasibility.note or "—")


def selected_lines(tables: V13ReportTables) -> list[str]:
    """选定的 P1 与 N（或 N′）并列，另给只作描述的几项。"""
    trace = tables.trace
    by_key = {row.key: row for row in tables.results}
    chosen = [by_key[item.key] for item in (trace.p1, trace.final_n, tables.p0_best) if item is not None]
    lines = ["## 选定设定", ""]
    if not chosen:
        return [*lines, "没有选定任何设定。", ""]
    lines.extend([*table(DETAIL_COLUMNS, [detail_row(row) for row in chosen]), ""])
    if trace.p1 is not None and trace.final_n is not None:
        p1, n = by_key[trace.p1.key], by_key[trace.final_n.key]
        green, red = tables.references[0].evaluated.total_loss, tables.references[2].evaluated.total_loss
        gap = caution_gap(n.timing.mean_exposure, p1.timing.mean_exposure, green, red)
        lines.extend([
            "只作描述（开发期，不是检验结果）：", "",
            f"- T 之差（N 侧 − P1）= {num(n.timing.score - p1.timing.score)}；"
            f"原主损失之差 = {num(n.summary.total_loss - p1.summary.total_loss)}。",
            f"- “谨慎程度差” (ē_N − ē_P1)(L_G − L_R) = {num(gap)}；ē 分别为 {num(n.timing.mean_exposure)}、"
            f"{num(p1.timing.mean_exposure)}。",
            "- 是否“同等平均暴露时 N 的择时优于 P1”只能由锁定后对验证期运行一次的主检验回答，本次没有运行。", ""])
    return lines


def all_settings_lines(results: Sequence[SettingResult]) -> list[str]:
    """全部 117 组：P0 一张表，其后每个退出版本下 P1、N、N′ 各一张。"""
    lines = ["## 全部设定", "",
             "R 列为“主口径中位数/P75（保守口径中位数/P75）”，n 为类别②事件数；保守口径把类别③按超限计，只报告不判定。"
             "完整字段见 `settings_summary.csv`。", ""]
    groups = [("P0", "P0护栏"), *((model, version) for version in ("E2", "X1", "X2") for model in VERSIONED_MODELS)]
    for model, version in groups:
        own = [row for row in results
               if row.candidate.model == model and display_version(row.candidate) == version]
        feasible = sum(row.feasibility.feasible for row in own)
        lines.extend([f"**{display_model(model)}·{version}**（可行 {feasible}/{len(own)}）", "",
                      *table(DETAIL_COLUMNS, [detail_row(row) for row in own]), ""])
    return lines


def scope_lines(scope_counts: Sequence[Row]) -> list[str]:
    return ["## 事件纳入范围（主损失与事件账，同 v1.2.1 规则）", "",
            *table(("资产", "事件数", *SCOPE_CATEGORIES, "罚项口径纳入", "事件账纳入"), scope_counts), ""]


def report_lines(prepared: PreparedEvaluation, tables: V13ReportTables, settings: LossSettings) -> list[str]:
    """披露声明、起点、参照行、选择过程、选定设定、全部设定；事件账、警报账只给选定设定。"""
    ledgers: list[str] = []
    if tables.event_classes:
        ledgers = [*event_lines(tables.event_classes), *alert_lines(tables.alert_summaries)]
    return [*header_lines(prepared, tables, settings), *reference_lines(tables.references),
            *trace_lines(tables), *selected_lines(tables), *all_settings_lines(tables.results),
            *scope_lines(tables.scope_counts),
            "以下事件账与警报账只列选定的 P1 与 N（或 N′）；表中“模型”列后的版本见上文。", "", *ledgers,
            *missing_lines(tables.missing, len(tables.results))]


def readme_lines(prepared: PreparedEvaluation, settings: LossSettings, daily_sha256: str,
                 command: str) -> list[str]:
    """本目录各文件的用途；逐日明细的列与 v1.2.1 相同，只多一列退出版本。"""
    return [
        f"# v1.3 开发期评价输出说明（{STATUS_NOTE}）", "",
        "登记文件：`docs/research/波段预警研究规格_v1.3_修订登记.md`。只使用开发期数据，不是历史预警效果。"
        f"t0={prepared.t0}，τ={prepared.tau}，j₀={prepared.first_loss_day}。", "",
        "## 文件", "",
        "- `settings_summary.csv`：全部 117 组设定（P0、P1×3、N×3、N′×3）的主损失与分项、ē（`mean_exposure`）、"
        "同暴露基准（`benchmark_loss`）、T（`timing_score`）、非绿占比、SPX 与 QQQ 的 R 中位数与 P75"
        "（主口径 `r_median`/`r_p75`，保守口径 `r_conservative_*`，“超限”表示涉及类别③）、各可行条件是否满足。",
        "- `selection_trace.csv`：选择程序的逐步追踪。`reference_rows.csv`：始终绿、黄、红与 200 日均线四条参照行。",
        "- `convergence.csv`：各组系统收敛日与共同的 t0、τ、j₀。`event_scope_counts.csv`：事件纳入件数。",
        "- `daily_selected.csv`（入库为 `.csv.gz`）：只含选定的 P1、N（或 N′）与四条参照行的逐日明细。",
        "- `event_ledger_selected.csv`、`event_class_summary_selected.csv`、`alert_ledger_selected.csv`、"
        "`alert_summary_selected.csv`、`yearly_alert_selected.csv`：选定模型的事件账与警报账。",
        "- `missing_audit.csv`：全部设定的沿用日与被排除区间。`开发期评价报告.md`：报告（含披露声明）。", "",
        "## `daily_selected.csv` 的列", "",
        "设定列为 `model`、`exit_version`、`k`、`theta_p`、`q`（参照行的 `exit_version` 为“参照行”，其余三列为空）；"
        "其后各列的含义、单位与公式同 `reports/research/wavewarn_v121/evaluation_development/README.md`：",
        "- `exposure` 由 `system_executed_light`（S_{j−1}）决定：绿 1、黄 "
        f"{settings.parameters.eta}、红 0；`row_total` = `weighted_price_loss` + `switch_cost_share` + "
        f"`{MISS_PENALTY_COLUMN}`；同日两行合计为系统日度主损失。",
        "- 由本文件可重算：主损失 L = 全部行 `row_total` 之和；ē = 计入区间（`next_date` 非空）上 `exposure` 的平均"
        "（取任一资产的行）；非绿占比 = 计入区间中 `system_executed_light` 非绿的比例；"
        "T = L − [ē·L_G + (1−ē)·L_R]，L_G、L_R 为始终绿、始终红两条参照行的 L。", "",
        *storage_lines("daily_selected.csv", daily_sha256, command)]
