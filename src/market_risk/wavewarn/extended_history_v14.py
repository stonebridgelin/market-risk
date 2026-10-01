"""v1.4 补充历史（纯计算）：1999-03-10 至 2009-09-30 的“v1.4 纯价格版”（MR + P + PR，解除规则 F）。

只作描述，不改变选择。t0′、τ′、j₀′ 的规则同 v1.3 补充历史；窗口边界同 v1.3 登记的实施细则第 2 条。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from market_risk.wavewarn.calibration import shown
from market_risk.wavewarn.config_v14 import V14Config
from market_risk.wavewarn.convergence import loss_start
from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    Candidate,
    PreparedEvaluation,
    evaluate_candidate,
    first_loss_interval,
)
from market_risk.wavewarn.evaluation_tables import ModelSummary, Row, missing_rows, model_summary
from market_risk.wavewarn.evaluation_v13_report import num, share, table
from market_risk.wavewarn.evaluation_v14_tables import ASSET_FIELDS, asset_cells
from market_risk.wavewarn.extended_history import (
    CROSSING_END,
    crossing_events,
    p0_first_complete_day,
    window_exit_costs,
)
from market_risk.wavewarn.feasibility import AssetRebound, asset_rebound
from market_risk.wavewarn.feasibility_v14 import GreenDelay, green_delay
from market_risk.wavewarn.features import asset_features
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.period_stats import period_row
from market_risk.wavewarn.timing import ReferenceRow, TimingResult, reference_rows, timing_result
from market_risk.wavewarn.v14_model import PRICE_ONLY, V14Features, v14_grid, v14_states


def prepare_price_window(config: V14Config, inputs: DevelopmentInputs) -> PreparedEvaluation:
    """九组纯价格版的状态、收敛日与 τ′、j₀′；inputs 须已截断到窗口末日。

    t0′ 取窗口内两资产的收盘价、63 日高点与回撤、20 日新低窗口、MA50 首次全部具备的交易日（同 v1.3）。
    SPX 的滚动窗口（含 MR 的 200 日均线）可以用到窗口起点之前的 SPX 价格。
    """
    base = config.base
    fixed, quantile = base.fixed_parameters(), base.candidate_sets().q[0]
    spx, qqq = (asset_features(inputs.days, inputs.series[symbol], {}, quantile, fixed) for symbol in SYMBOLS)
    t0 = p0_first_complete_day(inputs.days, spx, qqq, config.history_start)
    features = V14Features(spx, qqq, (None,) * len(inputs.days))
    states = tuple(v14_states(candidate, inputs.days, features, t0, fixed, config.mr_window)
                   for candidate in v14_grid(config, (PRICE_ONLY,), 0))
    dates = [item.convergence_date for item in states]
    tau = loss_start(inputs.days, t0, dates, fixed)
    return PreparedEvaluation(base, inputs, t0, tau, first_loss_interval(inputs.days, tau, dates), states)


@dataclass(frozen=True)
class WindowRow:
    """一组纯价格版设定在补充历史窗口内的描述性结果。"""

    candidate: Candidate
    summary: ModelSummary
    timing: TimingResult
    rebounds: Mapping[str, AssetRebound]
    delays: Mapping[str, GreenDelay]


@dataclass(frozen=True)
class WindowResult:
    references: tuple[ReferenceRow, ...]
    rows: tuple[WindowRow, ...]
    crossing: Mapping[str, tuple[ZZEvent, ...]]
    missing: tuple[Row, ...]
    bear_markets: tuple[Row, ...]


def evaluate_price_window(prepared: PreparedEvaluation, config: V14Config,
                          events: Mapping[str, Sequence[ZZEvent]], unknown: UnknownLabels) -> WindowResult:
    """九组设定与四条参照行在窗口内的结果，以及两次熊市的执行暴露与回撤。"""
    references = reference_rows(prepared, events, unknown, config.mr_window)
    eta = configured_loss_settings(prepared.config).parameters.eta
    green, red = references[0].evaluated.total_loss, references[2].evaluated.total_loss
    rows: list[WindowRow] = []
    missing: list[Row] = []
    named = [(references[0].name, references[0].evaluated), (references[3].name, references[3].evaluated)]
    for states in prepared.states:
        evaluated = evaluate_candidate(prepared, states, events, unknown)
        costs = {symbol: window_exit_costs(prepared, states, symbol, events[symbol]) for symbol in SYMBOLS}
        candidate = states.candidate
        rows.append(WindowRow(candidate, model_summary(states, evaluated),
                              timing_result(evaluated, eta, green, red),
                              {symbol: asset_rebound(symbol, costs[symbol]) for symbol in SYMBOLS},
                              {symbol: green_delay(symbol, prepared.inputs.days, costs[symbol])
                               for symbol in SYMBOLS}))
        missing.extend(missing_rows(("v1.4 纯价格版", candidate.k, candidate.theta_p, ""), evaluated))
        named.append((f"v1.4 纯价格版 K={candidate.k} θ_P={candidate.theta_p}", evaluated))
    bears = tuple(period_row(f"{start} 至 {end}", name, evaluated, eta, start, end)
                  for start, end in config.bear_markets for name, evaluated in named)
    return WindowResult(references, tuple(rows),
                        {symbol: crossing_events(prepared, events[symbol]) for symbol in SYMBOLS},
                        tuple(missing), bears)


SUMMARY_HEADER = ("k", "theta_p", "convergence_date", "intervals", "total_loss", "danger_loss", "drawdown_loss",
                  "opportunity_loss", "switch_cost", "mean_exposure", "benchmark_loss", "timing_score",
                  "non_green_share", "executed_non_green_days", "billed_switches",
                  *(f"{symbol}_{name}" for symbol in ("spx", "qqq") for name in ASSET_FIELDS))
CROSSING_HEADER = ("symbol", "peak_date", "t0_date", "trough_date", "status")


def summary_row(row: WindowRow) -> Row:
    summary, timing = row.summary, row.timing
    return (row.candidate.k, row.candidate.theta_p, summary.convergence_date, timing.intervals,
            summary.total_loss, summary.danger_loss, summary.drawdown_loss, summary.opportunity_loss,
            summary.switch_cost, timing.mean_exposure, timing.benchmark_loss, timing.score,
            timing.non_green_share, summary.executed_non_green_days, summary.billed_switches,
            *asset_cells(row.delays["SPX"], row.rebounds["SPX"]),
            *asset_cells(row.delays["QQQ"], row.rebounds["QQQ"]))


def crossing_rows(result: WindowResult) -> tuple[Row, ...]:
    return tuple((symbol, event.peak_date, event.t0_date, event.trough_date, CROSSING_END)
                 for symbol, events in result.crossing.items() for event in events)


def _asset_text(row: WindowRow, symbol: str) -> tuple[str, str, str]:
    delay, rebound = row.delays[symbol], row.rebounds[symbol]
    days = "/".join("—" if value is None else f"{value:.2f}".rstrip("0").rstrip(".")
                    for value in (delay.delays.median, delay.delays.p75, delay.delays.maximum))
    recovery = (f"{shown(rebound.main.median)[:6]}/{shown(rebound.main.p75)[:6]}" if rebound.main.n else "无样本")
    declines = "/".join(shown(value)[:6] or "—" for value in (rebound.half_way.median, rebound.half_way.maximum))
    half = f"{rebound.half_way.n}：{declines}"
    return f"{days}（{delay.class_1}/{delay.class_2}/{delay.class_3}）", recovery, half


def report_lines(prepared: PreparedEvaluation, config: V14Config, result: WindowResult, command: str) -> list[str]:
    """补充历史报告：起点、参照行、九组设定、两次熊市、跨窗口末端事件与缺值。"""
    dates = [row.summary.convergence_date for row in result.rows]
    references = [(row.name, row.evaluated.total_loss, row.timing.mean_exposure, row.timing.score,
                   share(row.timing.non_green_share), row.evaluated.billed_switches) for row in result.references]
    body = [(row.candidate.k, row.candidate.theta_p, row.summary.total_loss, row.timing.mean_exposure,
             row.timing.score, share(row.timing.non_green_share), row.summary.billed_switches,
             *_asset_text(row, "SPX"), *_asset_text(row, "QQQ")) for row in result.rows]
    crossing = crossing_rows(result)
    carried = sum(int(str(row[6])) for row in result.missing if row[4] == "系统")
    excluded = sorted({tuple(str(cell) for cell in row[4:]) for row in result.missing if row[4] != "系统"})
    return [
        "# v1.4 补充历史（描述性，不改变选择）", "",
        "登记依据：`docs/research/波段预警研究规格_v1.4_修订登记.md` 第六节。只能运行“v1.4 纯价格版”"
        "（MR + 两资产 P、PR，解除规则 F），因为这段历史没有广度与期限结构数据。只使用 2016-12-30 及以前的数据，"
        f"ZZ 标签取自现有开发期标签文件。数字只作描述，不能称为历史预警效果。入口命令：`{command}`。", "",
        f"窗口 {config.history_start} 至 {prepared.inputs.days[-1]}。t0′ = {prepared.t0}；τ′ = {prepared.tau}；"
        f"j₀′ = {prepared.first_loss_day}；计入区间数 {result.rows[0].timing.intervals}。九组设定的系统收敛日为 "
        f"{min(dates)} 至 {max(dates)}。状态与执行只算到窗口末日；危险标签取自完整标签文件；"
        "转绿延迟、R 与半山腰转绿只纳入 P ≥ τ′ 且 Tr ≤ 窗口末日的已确认事件，到窗口末日仍未转绿的归类别③。"
        "SPX 的滚动窗口（含 200 日均线）用到了窗口起点之前的 SPX 价格。", "",
        "## 参照行", "", *table(("参照", "主损失 L", "ē", "T", "非绿占比", "计费切换次数"), references), "",
        "## v1.4 纯价格版九组", "",
        "转绿延迟为“中位/P75/最大（①/②/③ 件数）”，单位交易日；R 为主口径中位/P75；"
        "半山腰为“发生事件数：最深跌幅中位/最大”。", "",
        *table(("K", "θ_P", "主损失 L", "ē", "T", "非绿占比", "切换", "SPX 转绿延迟", "SPX R", "SPX 半山腰",
                "QQQ 转绿延迟", "QQQ R", "QQQ 半山腰"), body), "",
        "## 两次熊市的执行暴露与回撤", "",
        "期间按 SPX 收盘价的高点到低点划定，两个资产共用。回撤按每个资产的累计对数收益 Σ e_j·r_j 计算（对数单位）；"
        "始终绿一行即指数本身。", "",
        *table(("期间", "行", "区间数", "平均执行暴露", "期间主损失", "SPX 按暴露累计对数收益", "SPX 最大回撤",
                "QQQ 按暴露累计对数收益", "QQQ 最大回撤"), [tuple(num(cell) for cell in row)
                                                        for row in result.bear_markets]), "",
        f"## {CROSSING_END}", "",
        *(table(("资产", "高点 P", "T0", "低点 Tr", "处理"), crossing) if crossing
          else ["没有高点在窗口内、低点在窗口之后的事件。"]), "",
        "## 缺值", "",
        f"九组设定的“沿用”日合计 {carried} 个设定日。被排除的资产区间（各设定去重后）：",
        *([f"- {'，'.join(item)}" for item in excluded] or ["- 无。"]), ""]
