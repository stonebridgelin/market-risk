"""v1.4 开发期评价的报告正文与输出说明（纯计算，只生成文本行）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.calibration import shown
from market_risk.wavewarn.config_v14 import DELAY_QQQ, DELAY_SPX, NON_GREEN, V14Config
from market_risk.wavewarn.evaluation import SCOPE_CATEGORIES, PreparedEvaluation
from market_risk.wavewarn.evaluation_report import alert_lines, event_lines, missing_lines, storage_lines
from market_risk.wavewarn.evaluation_tables import Row
from market_risk.wavewarn.evaluation_v13_report import num, reference_lines, share, table
from market_risk.wavewarn.evaluation_v14 import V14Result, setting_label
from market_risk.wavewarn.evaluation_v14_tables import STATUS_NOTE, TIER_TEXT, trace_rows, yes_no
from market_risk.wavewarn.feasibility_v14 import TierTrace
from market_risk.wavewarn.loss import LossSettings
from market_risk.wavewarn.timing import ReferenceRow, caution_gap
from market_risk.wavewarn.v14_model import FULL, NO_TREND

DISCLOSURE = ("**披露声明。** v1.4 是在看过 v1.2.1、v1.3 两版开发期结果与诊断之后设计的，"
              "开发期结论不能视为事先登记的证据。v1.4 是验证期之前的最后一次设计修订；"
              "验证期结果出来后不得修改 v1.4，任何改进另立新版本，且只能声称“验证期二次使用”。"
              "验证期（2017—2022）与保留期（2023 年起）没有运行、没有读取。"
              "本报告是开发期工程运行，未锁定，没有写正式锁定记录。下列数字只用于核对实现与执行登记的选择程序，"
              "不能称为历史预警效果，也不代表实盘有效。")
COMMANDS = ("uv run market-risk wavewarn evaluate-v14-development", "uv run market-risk wavewarn v14-extended-history")


@dataclass(frozen=True)
class V14ReportTables:
    """报告用到的全部结果；逐日明细只在 CSV 中。"""

    results: tuple[V14Result, ...]
    references: tuple[ReferenceRow, ...]
    trace: TierTrace
    scope_counts: tuple[Row, ...]
    event_classes: tuple[Row, ...]
    alert_summaries: tuple[Row, ...]
    missing: tuple[Row, ...]


def header_lines(prepared: PreparedEvaluation, tables: V14ReportTables, settings: LossSettings,
                 config: V14Config) -> list[str]:
    params, limits = settings.parameters, config.limits
    labels = list(dict.fromkeys(setting_label(row.candidate) for row in tables.results))
    spans = []
    for label in labels:
        dates = [row.summary.convergence_date for row in tables.results if setting_label(row.candidate) == label]
        spans.append(f"{label} {min(dates)} 至 {max(dates)}（{len(dates)} 组）")
    return [
        f"# v1.4 开发期评价（{STATUS_NOTE}）", "", DISCLOSURE, "",
        "登记文件：`docs/research/波段预警研究规格_v1.4_修订登记.md`。"
        f"只使用截至 {prepared.config.development_end()} 的开发期数据。"
        f"入口命令：`{COMMANDS[0]}`；补充历史：`{COMMANDS[1]}`。", "",
        "## 模型、评价起点与参数", "",
        f"- v1.4：趋势层 MR（SPX 收盘价低于其 {config.mr_window} 日均线为红）+ 回调层 P、PR（两资产）+ "
        "压力层 BW（两资产）、V；去掉 B、DV。解除规则 F 只看当日：红→黄要求全部红灯通道当日有效且未激活、"
        "两指数 Q ≥ K；黄→绿要求全部通道当日有效且未激活、两指数 Q ≥ K；每天最多降一级。",
        f"- t0 = {prepared.t0}；τ = max(t0 + 63 个交易日, 全部 {len(tables.results)} 组设定的系统收敛日) = "
        f"{prepared.tau}；j₀ = {prepared.first_loss_day}；计入区间数 |J| = {tables.results[0].timing.intervals}。",
        "- 参与统一 τ 与 j₀ 的模型及其系统收敛日：" + "；".join(spans) + "。逐组见 `convergence.csv`。",
        f"- 损失参数同 v1.2.1：η={params.eta}，κ_D={params.kappa_d}，β={params.beta}，κ_0={params.kappa_0}，"
        f"γ={params.gamma}，噪声下限={params.noise_floor}，μ={settings.mu}，权重各 {settings.weight_spx}。",
        f"- 可行条件：(1) 非绿占比 ≤ {limits.non_green_share_max}；(2)(3) SPX、QQQ 的转绿延迟中位数各 ≤ "
        f"{limits.green_delay_median_max} 个交易日。转绿延迟 = 首个绿灯执行日 g 与最终低点 Tr 之间的交易日数，"
        "只对类别②事件计算，纳入门槛为高点 P ≥ τ。R、保守口径与半山腰转绿照常报告，不作判定。",
        "- 择时得分 T = L − [ē·L_G + (1−ē)·L_R]，T 越小越好。", ""]


def trace_lines(tables: V14ReportTables, config: V14Config) -> list[str]:
    trace = tables.trace
    note = f"，标注“{trace.note}”" if trace.note else "，三条可行条件全部满足"
    return ["## 选择过程追踪", "",
            *table(("步骤", "事项", "结果"), trace_rows(trace, config.tiers)), "",
            f"**选定设定：{trace.selected.key}；落在第 {trace.tier} 级（{TIER_TEXT[trace.tier - 1]}）{note}。**"
            "各级候选数按该级条件各自统计，不互斥。选定设定尚未锁定，待负责人确认锁定记录草稿。", ""]


DETAIL_COLUMNS = ("设定", "K", "θ_P", "q", "主损失 L", "ē", "T", "非绿占比", "切换", "SPX 转绿延迟 中位/P75/最大",
                  "SPX ①/②/③", "QQQ 转绿延迟 中位/P75/最大", "QQQ ①/②/③", "SPX R 中位/P75", "QQQ R 中位/P75",
                  "条件(1)/(2)/(3)")


def _short(value: Decimal | None) -> str:
    return shown(value)[:6] or "—"


def _days(value: Decimal | None) -> str:
    """天数：整数不带小数，其余最多两位小数。"""
    return "—" if value is None else f"{value:.2f}".rstrip("0").rstrip(".")


def detail_row(result: V14Result) -> Row:
    candidate, timing = result.candidate, result.timing
    cells: list[object] = []
    for symbol in ("SPX", "QQQ"):
        delay = result.delays[symbol]
        cells.extend((f"{_days(delay.delays.median)}/{_days(delay.delays.p75)}/{_days(delay.delays.maximum)}",
                      f"{delay.class_1}/{delay.class_2}/{delay.class_3}"))
    rebounds = [f"{_short(result.rebounds[symbol].main.median)}/{_short(result.rebounds[symbol].main.p75)}"
                for symbol in ("SPX", "QQQ")]
    flags = "/".join(yes_no(result.flags[name]) for name in (NON_GREEN, DELAY_SPX, DELAY_QQQ))
    return (setting_label(candidate), candidate.k, candidate.theta_p, candidate.q if candidate.q is not None else "—",
            result.summary.total_loss, timing.mean_exposure, timing.score, share(timing.non_green_share),
            result.summary.billed_switches, *cells, *rebounds, flags)


def settings_lines(results: Sequence[V14Result], selected_key: str) -> list[str]:
    """候选九组、去掉 MR 九组、三个中位对照各一张表。"""
    groups = (("v1.4 候选（九组）", [row for row in results if row.candidate.model == FULL]),
              ("“v1.4 去掉 MR”（九组，描述性对照，不参与选择）",
               [row for row in results if row.candidate.model == NO_TREND]),
              ("P1·E2、N′·X2、N·E2 的中位设定（描述性对照，不参与选择）",
               [row for row in results if row.candidate.model not in (FULL, NO_TREND)]))
    lines = ["## 全部设定", "",
             "转绿延迟单位为交易日；“①/②/③”为纳入事件的三类件数；“条件”一列依次为非绿占比、SPX 转绿延迟、"
             "QQQ 转绿延迟是否满足。R 为主口径（类别②）；保守口径与半山腰转绿见 `settings_summary.csv`。", ""]
    for title, rows in groups:
        chosen = [row for row in rows if row.key == selected_key]
        mark = f"选定设定为 K={chosen[0].candidate.k}、θ_P={chosen[0].candidate.theta_p}。" if chosen else ""
        lines.extend([f"**{title}**　{mark}", "", *table(DETAIL_COLUMNS, [detail_row(row) for row in rows]), ""])
    return lines


def comparison_lines(tables: V14ReportTables) -> list[str]:
    """选定设定与 200 日均线参照行并列；只作描述，不是检验结果。"""
    chosen = next(row for row in tables.results if row.key == tables.trace.selected.key)
    average = tables.references[3]
    green, red = tables.references[0].evaluated.total_loss, tables.references[2].evaluated.total_loss
    gap = caution_gap(chosen.timing.mean_exposure, average.timing.mean_exposure, green, red)
    rows = [("选定的 v1.4 设定", chosen.summary.total_loss, chosen.timing.mean_exposure, chosen.timing.score,
             share(chosen.timing.non_green_share), chosen.summary.billed_switches),
            (average.name, average.evaluated.total_loss, average.timing.mean_exposure, average.timing.score,
             share(average.timing.non_green_share), average.evaluated.billed_switches)]
    return ["## 选定设定与 200 日均线（开发期，只作描述）", "",
            *table(("行", "主损失 L", "ē", "T", "非绿占比", "计费切换次数"), rows), "",
            f"- T 之差（v1.4 − 200日均线）= {num(chosen.timing.score - average.timing.score)}；"
            f"主损失之差 = {num(chosen.summary.total_loss - average.evaluated.total_loss)}；"
            f"“谨慎程度差” (ē_V4 − ē_MA)(L_G − L_R) = {num(gap)}。",
            "- “同等平均暴露时，v1.4 是否在 200 日均线之外带来额外的择时价值”只能由锁定后对验证期运行一次的主检验回答，"
            "本次没有运行。主检验的日度差函数已实现并用构造数据测试。", ""]


def report_lines(prepared: PreparedEvaluation, tables: V14ReportTables, settings: LossSettings,
                 config: V14Config) -> list[str]:
    scope = ["## 事件纳入范围（主损失与事件账，同 v1.2.1 规则）", "",
             *table(("资产", "事件数", *SCOPE_CATEGORIES, "罚项口径纳入", "事件账纳入"), tables.scope_counts), "",
             "以下事件账与警报账只列选定的 v1.4 设定；本目录中对应的文件名带 `_selected` 后缀。", ""]
    return [*header_lines(prepared, tables, settings, config), *reference_lines(tables.references),
            *trace_lines(tables, config), *comparison_lines(tables),
            *settings_lines(tables.results, tables.trace.selected.key), *scope,
            *event_lines(tables.event_classes), *alert_lines(tables.alert_summaries),
            *missing_lines(tables.missing, len(tables.results)),
            "## 尚未实现的验证期项目", "",
            "登记第五节要求在验证期描述性报告“SPX 跌幅 15% 以上的每个 ZZ 事件中的执行暴露与回撤”。"
            "这是验证期才用到的输出，本批没有实现，也没有运行验证期。", ""]


def readme_lines(prepared: PreparedEvaluation, daily_sha256: str) -> list[str]:
    return [
        f"# v1.4 开发期评价输出说明（{STATUS_NOTE}）", "",
        "登记文件：`docs/research/波段预警研究规格_v1.4_修订登记.md`。只使用开发期数据，不是历史预警效果。"
        f"t0={prepared.t0}，τ={prepared.tau}，j₀={prepared.first_loss_day}。入口命令：`{COMMANDS[0]}`。", "",
        "## 文件", "",
        "- `settings_summary.csv`：全部 21 组设定（v1.4 九组、去掉 MR 九组、三个中位对照）的主损失与分项、ē、"
        "同暴露基准、T、非绿占比、SPX 与 QQQ 的转绿延迟（中位数、P75、最大值、类别①②③件数）、R（主口径与保守口径）、"
        "半山腰转绿、三项条件是否满足；`role` 区分候选与描述性对照。",
        "- `selection_trace.csv`：三级选择程序的逐步追踪。`reference_rows.csv`：四条参照行。",
        "- `convergence.csv`：各组系统收敛日与共同的 t0、τ、j₀。`event_scope_counts.csv`：事件纳入件数。",
        "- `daily_selected.csv`（入库为 `.csv.gz`）：选定设定与四条参照行的逐日明细；列同 v1.3 的 `daily_selected.csv`"
        "（见 `reports/research/wavewarn_v13/evaluation_development/README.md`）。",
        "- `event_ledger_selected.csv`、`event_class_summary_selected.csv`、`alert_ledger_selected.csv`、"
        "`alert_summary_selected.csv`、`yearly_alert_selected.csv`：选定设定的事件账与警报账。",
        "- `missing_audit.csv`：全部设定的沿用日与被排除区间。`开发期评价报告.md`：报告（含披露声明）。",
        f"- `extended_history/`：补充历史（v1.4 纯价格版），由 `{COMMANDS[1]}` 写入。", "",
        *storage_lines("daily_selected.csv", daily_sha256, COMMANDS[0])]
