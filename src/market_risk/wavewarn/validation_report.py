"""v1.4 验证期评价的表格行与四部分报告（纯计算）。同一生成器用于正式验证期与开发期演练。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.config import PairedParameters
from market_risk.wavewarn.config_v14 import ValidationConfig
from market_risk.wavewarn.evaluation_report import alert_lines, event_lines, missing_lines
from market_risk.wavewarn.evaluation_tables import Row
from market_risk.wavewarn.evaluation_v13_report import num, share, table
from market_risk.wavewarn.evaluation_v14 import setting_label
from market_risk.wavewarn.evaluation_v14_tables import ASSET_FIELDS, asset_cells
from market_risk.wavewarn.main_test import (
    COMPONENTS,
    NOT_SUPPORTED,
    SUPPORTED,
    MainTestResult,
    conclusion,
    fragile_notes,
)
from market_risk.wavewarn.paired_test import PairedResult
from market_risk.wavewarn.validation_flow import WindowEvaluation, WindowRow

DISCLOSURE = ("v1.4 是在看过 v1.2.1、v1.3 两版开发期结果与诊断之后设计的，开发期结论不能视为事先登记的证据。"
              "v1.4 是验证期之前的最后一次设计修订；验证期结果出来后不得修改 v1.4，任何改进另立新版本，"
              "且只能声称“验证期二次使用”。保留期（2023 年起）没有读取。")


@dataclass(frozen=True)
class LockInfo:
    """报告开头的锁定记录信息；演练时为草稿。"""

    path: str
    sha256: str
    code_commit: str
    formal: bool


TEST_HEADER = ("test", "intervals", "statistic", "per_interval", "p_value", "ci_low", "ci_high", "block_length",
               "seed", "resamples")


def test_row(name: str, result: PairedResult | None) -> Row:
    if result is None:
        return (name, 0, "", "", "", "", "", "", "", "")
    return (name, result.sample_count, result.total_difference, result.per_interval, result.p_value, result.ci_low,
            result.ci_high, result.block_length, result.seed, result.resamples)


def test_rows(result: MainTestResult) -> tuple[Row, ...]:
    """主设定与各项以重抽样给出 p 值的敏感性。"""
    return (test_row("主设定", result.main), test_row("区块较短", result.short_block),
            test_row("区块较长", result.long_block),
            test_row(f"前半（区间起点早于 {result.split}）", result.first_half),
            test_row(f"后半（区间起点不早于 {result.split}）", result.second_half),
            test_row("删除置零日期（只在置零日期超过 1% 时运行）", result.without_zero))


DIFFERENCE_HEADER = ("date", "d", *(name for name, _ in COMPONENTS), "zeroed")
LEAVE_ONE_HEADER = ("peak_date", "trough_date", "source", "statistic_after_zeroing", "sign_changed")
SUMMARY_HEADER = ("model", "exit_version", "k", "theta_p", "q", "role", "intervals", "total_loss", "danger_loss",
                  "drawdown_loss", "opportunity_loss", "switch_cost", "miss_penalty", "mean_exposure",
                  "benchmark_loss", "timing_score", "non_green_share", "executed_non_green_days", "billed_switches",
                  "carried_days", *(f"{symbol}_{name}" for symbol in ("spx", "qqq") for name in ASSET_FIELDS))
DROP_HEADER = ("peak_date", "trough_date", "right_censored", "spx_decline", "row", "intervals", "mean_exposure",
               "total_loss", "spx_exposed_log_return", "spx_max_drawdown", "qqq_exposed_log_return",
               "qqq_max_drawdown")


def difference_rows(result: WindowEvaluation) -> tuple[Row, ...]:
    return tuple((row.date, row.total, *(row.components[name] for name, _ in COMPONENTS),
                  "是" if row.zeroed else "否") for row in result.differences)


def leave_one_rows(result: MainTestResult) -> tuple[Row, ...]:
    return tuple((item.peak_date, item.trough_date, item.source, item.total, "是" if item.sign_changed else "否")
                 for item in result.leave_one)


def summary_row(row: WindowRow, locked: bool) -> Row:
    candidate, summary, timing = row.candidate, row.summary, row.timing
    return (setting_label(candidate), candidate.exit_version, candidate.k, candidate.theta_p,
            candidate.q if candidate.q is not None else "", "锁定设定" if locked else "描述性对照",
            timing.intervals, summary.total_loss, summary.danger_loss, summary.drawdown_loss,
            summary.opportunity_loss, summary.switch_cost, summary.miss_penalty, timing.mean_exposure,
            timing.benchmark_loss, timing.score, timing.non_green_share, summary.executed_non_green_days,
            summary.billed_switches, summary.carried_days,
            *asset_cells(row.delays["SPX"], row.rebounds["SPX"]),
            *asset_cells(row.delays["QQQ"], row.rebounds["QQQ"]))


def summary_rows(result: WindowEvaluation) -> tuple[Row, ...]:
    return tuple(summary_row(row, row.candidate == result.locked_states.candidate) for row in result.rows)


def drop_rows(result: WindowEvaluation) -> tuple[Row, ...]:
    return tuple((drop.event.peak_date, drop.event.trough_date, "是" if drop.event.right_censored else "否",
                  drop.decline, *row[1:]) for drop in result.big_drops for row in drop.rows)


def header_lines(result: WindowEvaluation, lock: LockInfo, config: ValidationConfig) -> list[str]:
    """标题、锁定记录、披露与措辞规则。"""
    prepared, window = result.prepared, result.window
    record = ("正式锁定记录" if lock.formal else "锁定记录草稿（未锁定）")
    lines = [f"# v1.4 {window.title}", ""]
    if window.rehearsal:
        lines.extend([f"**{window.title}。** 本报告把评价窗口设为开发期，用与验证期相同的流程完整运行一次，"
                      "只用来发现流程问题；不得据此修改任何规则或设定。没有读取 2016-12-30 之后的数据。", ""])
    lines.extend([
        f"- {record}：`{lock.path}`，SHA-256 `{lock.sha256}`；记录中的代码提交号 `{lock.code_commit}`。",
        f"- 运行对象：锁定的 v1.4 设定（K={config.locked_k}、θ_P={config.locked_theta}）、200 日均线、"
        "始终绿/黄/红，以及登记第六节的全部描述性对照。只运行这一组锁定设定，不在本窗口上重新选择。",
        f"- 披露：{DISCLOSURE}",
        f"- 措辞规则：主设定 p < {config.significance} 才可写“{SUPPORTED}”，否则写“{NOT_SUPPORTED}”。",
        f"- 模型自 t0 = {prepared.t0} 连续运行至 {prepared.inputs.days[-1]}；计入区间起点自 "
        f"{window.first_interval} 起（回撤参考高点在该日重置），共 {len(result.differences)} 个区间；"
        f"事件按高点 P ≥ {window.event_floor} 归属。{window.split_note}。", ""])
    return lines


def _test_table(result: MainTestResult) -> list[str]:
    rows = [(row[0], row[1], row[2], row[3], row[4], f"[{num(row[5])}, {num(row[6])}]" if row[1] else "—", row[7],
             row[8]) for row in test_rows(result)]
    return table(("检验", "区间数", "Σd", "Σd ÷ 区间数", "p", "95% 区间（重抽样的 2.5%、97.5% 分位）", "区块", "种子"),
                 rows)


def main_test_lines(result: WindowEvaluation, config: ValidationConfig, params: PairedParameters) -> list[str]:
    """第一部分：唯一主检验与全部敏感性。"""
    test = result.main_test
    verdict = conclusion(test, config.significance)
    notes = fragile_notes(test, config.significance)
    flips = sum(item.sign_changed for item in test.leave_one)
    prefix = "（演练，不构成任何证据）" if result.window.rehearsal else ""
    lines = [
        "## 一、主检验：v1.4 对 200 日均线的择时得分配对检验", "",
        "d_j = [ℓ_{V4,j} − ē_{V4} g_j − (1−ē_{V4}) r_j] − [ℓ_{MA,j} − ē_{MA} g_j − (1−ē_{MA}) r_j]；"
        "统计量 Σd_j = T_V4 − T_MA，负值表示 v1.4 更好。原假设：v1.4 的择时不优于 200 日均线。平稳自助法（环形），"
        f"重抽样 {params.resamples} 次；p = (1 + #{{T*−T ≤ T}}) ÷ (重抽样次数 + 1)。"
        "d_j 只按整个评价窗口的 ē 计算一次，各项敏感性都在同一条序列上取子集、置零或删除。", "",
        f"- ē_V4 = {num(test.mean_model)}，ē_MA = {num(test.mean_baseline)}；"
        f"“谨慎程度差” (ē_V4 − ē_MA)(L_G − L_R) = {num(test.caution_gap)}（只作描述）。",
        f"- **主设定：Σd = {num(test.main.total_difference)}，p = {num(test.main.p_value)}。"
        f"按措辞规则{prefix}：{verdict}。**",
        *(f"- 稳健性标注：{note}。" for note in notes),
        "", *_test_table(test), "",
        "按损失分项的分解（各项之和等于 Σd）：" + "；".join(
            f"{name} {num(value)}" for name, value in test.components.items()) + "。", "",
        f"逐事件剔除：与评价窗口相交的合并事件 {len(test.leave_one)} 件，逐一把 [P−{params.event_padding}, "
        f"Tr+{params.event_padding}] 内的 d_j 置零后重算 Σd；其中使统计量变号的 {flips} 件。逐件见 "
        "`leave_one_event.csv`。", "",
        f"置零日期（有资产区间被排除、价格类损失记 0 的日期）{test.zero_dates} 个，占 {share(test.zero_share)}；"
        + ("超过 1%，已另报删除这些日期后的结果（删除后区块跨越了不相邻的日期）。" if test.without_zero
           else "未超过 1%，不作删除敏感性。"), ""]
    return lines


LOSS_COLUMNS = ("行", "K", "θ_P", "q", "主损失 L", "ē", "T", "非绿占比", "切换", "SPX 转绿延迟中位（①/②/③）",
                "QQQ 转绿延迟中位（①/②/③）", "SPX R 中位/P75", "QQQ R 中位/P75")


def _days(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}".rstrip("0").rstrip(".")


def _loss_row(row: WindowRow, name: str) -> Row:
    cells = [f"{_days(row.delays[symbol].delays.median)}（{row.delays[symbol].class_1}/"
             f"{row.delays[symbol].class_2}/{row.delays[symbol].class_3}）" for symbol in ("SPX", "QQQ")]
    recovery = [f"{_days(row.rebounds[symbol].main.median)}/{_days(row.rebounds[symbol].main.p75)}"
                for symbol in ("SPX", "QQQ")]
    candidate = row.candidate
    return (name, candidate.k, candidate.theta_p, candidate.q if candidate.q is not None else "—",
            row.summary.total_loss, row.timing.mean_exposure, row.timing.score, share(row.timing.non_green_share),
            row.summary.billed_switches, *cells, *recovery)


def loss_lines(result: WindowEvaluation) -> list[str]:
    """第二部分：损失与择时（只作描述）。"""
    references = [(row.name, "—", "—", "—", row.evaluated.total_loss, row.timing.mean_exposure, row.timing.score,
                   share(row.timing.non_green_share), row.evaluated.billed_switches, "—", "—", "—", "—")
                  for row in result.references]
    locked = result.locked_states.candidate
    rows = [_loss_row(row, setting_label(row.candidate) + ("（锁定设定）" if row.candidate == locked else ""))
            for row in result.rows]
    return ["## 二、损失与择时（描述性）", "",
            "T = L − [ē·L_G + (1−ē)·L_R]。转绿延迟与 R 只对类别②事件；保守口径与半山腰转绿见 `settings_summary.csv`。"
            "除锁定设定与 200 日均线的主检验外，其余各行只作描述，不作确认性结论。", "",
            *table(LOSS_COLUMNS, [*references, *rows]), ""]


def drop_lines(result: WindowEvaluation, config: ValidationConfig) -> list[str]:
    """第四部分：SPX 大跌事件中的执行暴露与回撤。"""
    lines = ["## 四、大跌事件", "",
             f"SPX 按收盘价从高点到低点跌幅不低于 {share(config.big_drop_threshold)} 的 ZZ 事件"
             f"（高点 P ≥ {result.window.event_floor}；期末右截尾事件标注“暂定低点”）。"
             "每个事件取区间起点在 [P, Tr) 内的区间，两个资产共用；"
             "回撤按每个资产的累计对数收益 Σ e_j·r_j 计算（对数单位），始终绿一行即指数本身。", ""]
    if not result.big_drops:
        return [*lines, "评价窗口内没有符合条件的事件。", ""]
    rows = [(row[0], share(drop.decline), row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8])
            for drop in result.big_drops for row in drop.rows]
    return [*lines, *table(("事件（P 至 Tr）", "SPX 跌幅", "行", "区间数", "平均执行暴露", "期间主损失",
                            "SPX 按暴露累计对数收益", "SPX 最大回撤", "QQQ 按暴露累计对数收益", "QQQ 最大回撤"), rows),
            ""]


def report_lines(result: WindowEvaluation, lock: LockInfo, config: ValidationConfig, params: PairedParameters,
                 event_classes: Sequence[Row], alert_summaries: Sequence[Row]) -> list[str]:
    """四部分：主检验、损失与择时、事件账与警报账、大跌事件；末尾附缺值情况。"""
    retitle = {"## 二、事件账": "### 事件账", "## 三、警报账": "### 警报账", "## 四、缺值情况": "## 附：缺值情况"}
    ledgers = ["## 三、事件账与警报账", "",
               "只列锁定的 v1.4 设定；逐件、逐段见带 `_selected` 后缀的文件。", "",
               *event_lines(event_classes), *alert_lines(alert_summaries)]
    lines = [*header_lines(result, lock, config), *main_test_lines(result, config, params), *loss_lines(result),
             *ledgers, *drop_lines(result, config), *missing_lines(result.missing, len(result.rows))]
    return [retitle.get(line, line) for line in lines]
