"""v1.4 验证期评价的表格行与四部分报告（纯计算）。同一生成器用于正式验证期与开发期演练。

主检验按《v1.4 检验口径与诊断补充登记》：正式口径（联合重抽样）在前，条件性对照（固定 ē）在后。
"""

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
from market_risk.wavewarn.joint_test import JointResult
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
              "且只能声称“验证期二次使用”。主检验的统计口径在查看开发期结果之后、正式验证期运行之前修订过一次"
              "（检验口径与诊断补充登记）。保留期（2023 年起）没有读取。")
DEVELOPMENT_ONLY = "本报告仅使用开发期数据；诊断不改变模型规则、参数、选定设定与γ。统计口径修订已另行登记。"
FORMAL, CONDITIONAL = "正式口径：联合重抽样", "条件性对照：固定 ē"
MAIN_OBJECT, PRICE_OBJECT = "T", "T价格"


@dataclass(frozen=True)
class LockInfo:
    """报告开头的锁定记录信息；演练时为草稿。"""

    path: str
    sha256: str
    code_commit: str
    formal: bool


TEST_HEADER = ("object", "sample", "method", "intervals", "statistic", "per_interval", "p_value", "ci_low", "ci_high",
               "block_length", "seed", "resamples", "mean_model", "mean_baseline")
GAP_HEADER = ("object", "sample", "block_length", "seed", "resamples", "gap_mean", "gap_std", "gap_min",
              "gap_q025", "gap_q25", "gap_median", "gap_q75", "gap_q975", "gap_max", "theta_std_formal",
              "theta_std_conditional", "max_identity_residual")


def samples(result: MainTestResult) -> tuple[tuple[str, JointResult | None], ...]:
    """主设定与各项以重抽样给出 p 值的敏感性，顺序固定。"""
    return (("主设定", result.main), ("区块较短", result.short_block), ("区块较长", result.long_block),
            (f"前半（区间起点早于 {result.split}）", result.first_half),
            (f"后半（区间起点不早于 {result.split}）", result.second_half),
            ("删除置零日期并重算（只在置零日期超过 1% 时运行）", result.without_zero))


def _method_row(name: str, sample: str, method: str, paired: PairedResult, joint: JointResult) -> Row:
    return (name, sample, method, paired.sample_count, paired.total_difference, paired.per_interval, paired.p_value,
            paired.ci_low, paired.ci_high, paired.block_length, paired.seed, paired.resamples,
            joint.point.mean_model, joint.point.mean_baseline)


def test_rows(name: str, result: MainTestResult) -> tuple[Row, ...]:
    """每个样本两行：正式口径在前，条件性对照在后；未运行的样本留空行。"""
    rows: list[Row] = []
    for sample, joint in samples(result):
        if joint is None:
            rows.append((name, sample, "未运行", 0, *("",) * 10))
            continue
        rows.append(_method_row(name, sample, FORMAL, joint.formal, joint))
        rows.append(_method_row(name, sample, CONDITIONAL, joint.conditional, joint))
    return tuple(rows)


def gap_rows(name: str, result: MainTestResult) -> tuple[Row, ...]:
    """(Δē*_V4 − Δē*_MA)·A* 的分布、两种口径下 θ* 的标准差与恒等式的最大残差。"""
    return tuple((name, sample, joint.formal.block_length, joint.formal.seed, joint.formal.resamples,
                  joint.gap.mean, joint.gap.std, *joint.gap.quantiles, joint.gap.std_formal,
                  joint.gap.std_conditional, joint.gap.max_identity_residual)
                 for sample, joint in samples(result) if joint is not None)


DIFFERENCE_HEADER = ("date", "d", *(name for name, _ in COMPONENTS), "zeroed")
LEAVE_ONE_HEADER = ("peak_date", "trough_date", "source", "removed_intervals", "remaining_intervals",
                    "statistic_after_deletion", "deletion_sign_changed", "statistic_after_zeroing",
                    "zeroing_sign_changed")
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
    flag = lambda value: "是" if value else "否"  # noqa: E731
    return tuple((item.peak_date, item.trough_date, item.source, item.removed, item.remaining,
                  item.recomputed if item.recomputed is not None else "", flag(item.recomputed_sign_changed),
                  item.zeroed_total, flag(item.zeroed_sign_changed)) for item in result.leave_one)


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
        lines.extend([DEVELOPMENT_ONLY, "",
                      f"**{window.title}。** 本报告把评价窗口设为开发期，用与验证期相同的流程完整运行一次，"
                      "只用来发现流程问题；不得据此修改任何规则或设定。没有读取 2016-12-30 之后的数据。", ""])
    lines.extend([
        f"- {record}：`{lock.path}`，SHA-256 `{lock.sha256}`；记录中的代码提交号 `{lock.code_commit}`。",
        f"- 运行对象：锁定的 v1.4 设定（K={config.locked_k}、θ_P={config.locked_theta}）、200 日均线、"
        "始终绿/黄/红，以及登记第六节的全部描述性对照。只运行这一组锁定设定，不在本窗口上重新选择。",
        f"- 披露：{DISCLOSURE}",
        "- 推断目标：v1.4 作为规则程序、相对 200 日均线的安全代理择时比较。主检验比较的是在既定样本与损失口径下的"
        "期望安全代理得分差，不直接证明规则未来有效。T 是安全代理择时得分，不是实际收益层面的择时能力。",
        f"- 措辞规则：正式口径主设定 p < {config.significance} 才可写“{SUPPORTED}”，否则写“{NOT_SUPPORTED}”"
        "（两句沿用 v1.4 修订登记；其中“择时价值”指安全代理择时得分）。",
        f"- 模型自 t0 = {prepared.t0} 连续运行至 {prepared.inputs.days[-1]}；计入区间起点自 "
        f"{window.first_interval} 起（回撤参考高点在该日重置），共 {len(result.differences)} 个区间；"
        f"事件按高点 P ≥ {window.event_floor} 归属。{window.split_note}。", ""])
    return lines


TEST_COLUMNS = ("检验", "区间数", "θ̂", "θ̂ ÷ 区间数", "p", "95% 区间（θ* 的 2.5%、97.5% 分位）", "区块", "种子")


def _test_table(result: MainTestResult, formal: bool) -> list[str]:
    rows: list[Row] = []
    for sample, joint in samples(result):
        if joint is None:
            rows.append((sample, 0, "—", "—", "—", "—", "—", "—"))
            continue
        item = joint.formal if formal else joint.conditional
        rows.append((sample, item.sample_count, item.total_difference, item.per_interval, item.p_value,
                     f"[{num(item.ci_low)}, {num(item.ci_high)}]", item.block_length, item.seed))
    return table(TEST_COLUMNS, rows)


def _gap_table(result: MainTestResult) -> list[str]:
    rows = [(sample, joint.gap.mean, joint.gap.std, joint.gap.quantiles[1], joint.gap.quantiles[3],
             joint.gap.quantiles[5], joint.gap.std_formal, joint.gap.std_conditional,
             f"{joint.gap.max_identity_residual:.1E}")
            for sample, joint in samples(result) if joint is not None]
    return table(("检验", "差的均值", "差的标准差", "2.5% 分位", "中位", "97.5% 分位", "θ* 标准差（重算）",
                  "θ* 标准差（固定）", "恒等式最大残差"), rows)


def method_lines(result: MainTestResult) -> list[str]:
    """两种口径的检验表与恒等式核对；主检验与 T价格 的解释性检验共用。"""
    return [f"**{FORMAL}**（每次重抽样重算两模型各自的 ē 与同暴露基准；前后两半与删除置零日期各自重算、"
            "各自完整重抽样，不要求相加等于全期）", "", *_test_table(result, True), "",
            f"**{CONDITIONAL}**（原登记方法：ē 取该样本原样本的值，重抽样时视为常数；只作对照，不参与判定）", "",
            *_test_table(result, False), "",
            "恒等式核对：同一组索引上 θ*固定 − θ*重算 = (Δē*_V4 − Δē*_MA)·A*，A* = ΣG* − ΣR*，Δē* = ē* − ē原样本。"
            "下表为这个差在全部重抽样上的分布；固定法相对重算法高估还是低估波动取决于协方差，不作先验判断。", "",
            *_gap_table(result), ""]


def sensitivity_lines(test: MainTestResult, params: PairedParameters) -> list[str]:
    """分项分解、逐事件剔除与置零日期。"""
    deleted = sum(item.recomputed_sign_changed for item in test.leave_one)
    zeroed = sum(item.zeroed_sign_changed for item in test.leave_one)
    pad = params.event_padding
    return [
        "按损失分项的分解（各项之和等于 θ̂）：" + "；".join(
            f"{name} {num(value)}" for name, value in test.components.items()) + "。", "",
        f"逐事件剔除：与评价窗口相交的合并事件 {len(test.leave_one)} 件，每次只剔除一个事件——删除区间起点在 "
        f"[P−{pad}, Tr+{pad}] 交易日内（裁剪到窗口）的日期，重算 ē 与基准，只报告点估计与剩余区间数；"
        f"其中使点估计变号的 {deleted} 件。另把同一范围在原固定基准下的贡献置零，作为描述性归因；"
        f"其中使统计量变号的 {zeroed} 件。逐件见 `leave_one_event.csv`。"
        "剔除后的比较是冻结信号路径的样本敏感性，不等于“历史上没有该事件时策略会怎样运行”。", "",
        f"置零日期（有资产区间被排除、价格类损失记 0 的日期）{test.zero_dates} 个，占 {share(test.zero_share)}；"
        "这些日期保留在样本中，并计入 ē 的分母（登记选择：平均暴露按全部计入区间计算）。"
        + ("超过 1%，已另报删除这些日期并重算 ē 与基准后的结果（删除后区块跨越了不相邻的日期）。"
           if test.without_zero else "未超过 1%，不作删除敏感性。"), ""]


def main_test_lines(result: WindowEvaluation, config: ValidationConfig, params: PairedParameters) -> list[str]:
    """第一部分：唯一主检验与全部敏感性；正式口径在前。"""
    test = result.main_test
    verdict = conclusion(test, config.significance)
    prefix = "（演练，不构成任何证据）" if result.window.rehearsal else ""
    formal, conditional = test.main.formal, test.main.conditional
    return [
        "## 一、主检验：v1.4 对 200 日均线的安全代理择时得分检验", "",
        "检验对象是安全代理择时得分 T（含 γ 的切换罚分）：T_M = L_M − [ē_M·ΣG + (1−ē_M)·ΣR]，θ̂ = T_V4 − T_MA，"
        "负值表示 v1.4 更好。单侧检验，原假设 θ ≥ 0。平稳自助法（环形），"
        f"重抽样 {params.resamples} 次；p = (1 + #{{θ*−θ̂ ≤ θ̂}}) ÷ (重抽样次数 + 1)；区间为 θ* 的 2.5% 与 97.5% 分位数。"
        "百分位区间与中心化单侧 p 值不是同一套反演程序，临界情况下二者的判定不要求完全一致。"
        "正式口径已由推断目标确定，不得按哪种方法更显著来选择。", "",
        f"- ē_V4 = {num(test.mean_model)}，ē_MA = {num(test.mean_baseline)}；"
        f"“谨慎程度差” (ē_V4 − ē_MA)(ΣG − ΣR) = {num(test.caution_gap)}（只作描述）。",
        f"- **{FORMAL}，主设定：θ̂ = {num(formal.total_difference)}，p = {num(formal.p_value)}。"
        f"按措辞规则{prefix}：{verdict}。**",
        f"- {CONDITIONAL}，主设定：Σd = {num(conditional.total_difference)}，p = {num(conditional.p_value)}"
        "（只作对照，不参与判定）。",
        *(f"- 稳健性标注：{note}。" for note in fragile_notes(test, config.significance)),
        "", *method_lines(test), *sensitivity_lines(test, params)]


def explanatory_lines(result: WindowEvaluation) -> list[str]:
    """T价格（未计切换罚分）的同样检验：只在开发期演练中出现，只作解释。"""
    test = result.explanatory
    if test is None:
        return []
    return ["### T价格（未计切换罚分）的同样检验：只作解释，不是第二个主检验", "",
            "正式检验对象是 T；正式口径已由推断目标确定，不得按哪种方法更显著来选择。"
            "T价格 = T − 切换项，两模型的日损失都不含切换罚分；ē、基准、索引、种子与上面完全相同。"
            "这一节不形成确认性结论，验证期不输出。", "",
            f"- θ̂（T价格）= {num(test.main.formal.total_difference)}；"
            f"正式口径 p = {num(test.main.formal.p_value)}，条件性对照 p = {num(test.main.conditional.p_value)}。",
            "", *method_lines(test)]


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
    """第二部分：损失与安全代理择时得分（只作描述）。"""
    references = [(row.name, "—", "—", "—", row.evaluated.total_loss, row.timing.mean_exposure, row.timing.score,
                   share(row.timing.non_green_share), row.evaluated.billed_switches, "—", "—", "—", "—")
                  for row in result.references]
    locked = result.locked_states.candidate
    rows = [_loss_row(row, setting_label(row.candidate) + ("（锁定设定）" if row.candidate == locked else ""))
            for row in result.rows]
    return ["## 二、损失与安全代理择时得分（描述性）", "",
            "T = L − [ē·L_G + (1−ē)·L_R]，是安全代理择时得分。转绿延迟与 R 只对类别②事件：类别②中位数是有条件的统计，"
            "不代表全部事件的恢复速度；把类别③按无穷计入只能解释为“在下一事件前未转绿”的保守综合口径，"
            "不代表真实等待时间无穷。保守口径与半山腰转绿见 `settings_summary.csv`。"
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
    """四部分：主检验、损失与择时得分、事件账与警报账、大跌事件；末尾附缺值情况。"""
    retitle = {"## 二、事件账": "### 事件账", "## 三、警报账": "### 警报账", "## 四、缺值情况": "## 附：缺值情况"}
    ledgers = ["## 三、事件账与警报账", "",
               "只列锁定的 v1.4 设定；逐件、逐段见带 `_selected` 后缀的文件。", "",
               *event_lines(event_classes), *alert_lines(alert_summaries)]
    lines = [*header_lines(result, lock, config), *main_test_lines(result, config, params),
             *explanatory_lines(result), *loss_lines(result), *ledgers, *drop_lines(result, config),
             *missing_lines(result.missing, len(result.rows))]
    return [retitle.get(line, line) for line in lines]
