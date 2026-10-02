"""旧补充历史的标签影响量化：读写边界。

只读 SPX、QQQ 截至窗口末日（2009-09-30）的收盘价、开发期标签文件与已入库的旧补充历史输出；
只写量化目录，不修改任何已入库的历史输出。原标签下的重算须与已入库输出一致，
逐区间的局部调整之和须等于汇总的变化，否则报错停下。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from market_risk.wavewarn.calibration import calibration_tables
from market_risk.wavewarn.calibration_run import FIXED_HEADER
from market_risk.wavewarn.config_v13 import load_v13_config
from market_risk.wavewarn.config_v14 import load_validation_config
from market_risk.wavewarn.diagnostics_round2_run import HASH_FILE
from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    CandidateEvaluation,
    PreparedEvaluation,
    evaluate_candidate,
    period_labels,
)
from market_risk.wavewarn.evaluation_run import file_sha256, write_csv
from market_risk.wavewarn.evaluation_v13_tables import REFERENCE_HEADER, reference_row
from market_risk.wavewarn.export import read_zz_events
from market_risk.wavewarn.extended_history import evaluate_p0_window, prepare_p0_window, window_events
from market_risk.wavewarn.extended_history_report import EXIT_HEADER, P0_HEADER, exit_rows, p0_row
from market_risk.wavewarn.extended_history_v14 import (
    SUMMARY_HEADER,
    evaluate_price_window,
    prepare_price_window,
    summary_row,
)
from market_risk.wavewarn.extended_nav_run import load_price_inputs
from market_risk.wavewarn.label_cutoff_impact import (
    ImpactError,
    ObjectImpact,
    label_differences,
    local_adjustment,
    local_total,
    object_impact,
    reconcile,
)
from market_risk.wavewarn.label_cutoff_impact_report import (
    COMPARISON_HEADER,
    DIFFERENCE_HEADER,
    LOCAL_HEADER,
    OBJECT_HEADER,
    RANKING_HEADER,
    RECONCILIATION_HEADER,
    REPORT_NAME,
    VersionImpact,
    changed_directions,
    comparison_rows,
    difference_rows,
    local_rows,
    object_rows,
    ranking_rows,
    reconciliation_rows,
    report_lines,
)
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.lock_guard import find_git, run_git
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.period_stats import PERIOD_HEADER
from market_risk.wavewarn.timing import ReferenceRow, TimingResult, reference_rows, timing_result
from market_risk.wavewarn.validation_run import CONFIG_FILE

IMPACT_CONFIG = "config/wavewarn_v14_label_cutoff.yaml"
V13_CONFIG = "config/wavewarn_v13.yaml"
UNCHANGED = "无变化"
Labels = tuple[Mapping[str, Sequence[ZZEvent]], UnknownLabels]
Evaluated = tuple[CandidateEvaluation, TimingResult]
Row = tuple[object, ...]


@dataclass(frozen=True)
class ImpactConfig:
    tolerance: Decimal
    output: str
    events: str
    unknown: str
    stored: Mapping[str, str]


def load_impact_config(path: Path) -> ImpactConfig:
    with path.open(encoding="utf-8") as file:
        raw: dict[str, Any] = yaml.safe_load(file)
    if not isinstance(raw, dict) or raw.get("version") != "v1.4-label-cutoff-impact":
        raise ValueError("标签影响量化配置的版本错误")
    labels = raw.get("development_labels", {})
    config = ImpactConfig(Decimal(str(raw.get("tolerance"))), str(raw.get("output")), str(labels.get("events")),
                          str(labels.get("unknown")), {str(k): str(v) for k, v in raw.get("stored", {}).items()})
    if config.tolerance != Decimal("1e-10") or set(config.stored) != {"v13", "v14"}:
        raise ValueError("核对容差或已入库输出的清单与规格不一致")
    return config


@dataclass(frozen=True)
class ImpactRun:
    output: Path
    differences: tuple[tuple[str, int], ...]   # （版本，归类不同的资产区间数）
    report_sha256: str


def development_labels(root: Path, config: ImpactConfig, label_end: dt.date) -> Labels:
    """旧补充历史当时使用的开发期标签：事件取自标签文件，尾段未定取自同目录的未定文件（都在 2016 年，窗口之外）。"""
    events = read_zz_events(root / config.events, label_end)
    days: dict[str, set[dt.date]] = {symbol: set() for symbol in SYMBOLS}
    reasons: dict[str, dict[dt.date, str]] = {symbol: {} for symbol in SYMBOLS}
    with (root / config.unknown).open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            day = dt.date.fromisoformat(row["date"])
            days[row["symbol"]].add(day)
            reasons[row["symbol"]][day] = row["reason"]
    return events, UnknownLabels(label_end, {symbol: frozenset(values) for symbol, values in days.items()}, reasons)


def as_text(header: Sequence[str], rows: Sequence[Row]) -> list[list[str]]:
    """按 write_csv 的写法把行变成文本，再读回来，便于与已入库的 CSV 逐格比较。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return list(csv.reader(io.StringIO(buffer.getvalue())))


def stored_text(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.reader(file))


def check_stored(folder: Path, tables: Mapping[str, tuple[Sequence[str], Sequence[Row]]]) -> dict[str, str]:
    """原标签下的重算须与已入库的旧输出逐格相同，否则报错停下。"""
    result = {}
    for name, (header, rows) in tables.items():
        if as_text(header, rows) != stored_text(folder / name):
            raise ImpactError(f"原标签下的重算与已入库的 {name} 不一致")
        result[name] = "逐格相同"
    return result


def evaluations(prepared: PreparedEvaluation, labels: Labels, ma_window: int
                ) -> tuple[tuple[ReferenceRow, ...], tuple[Evaluated, ...]]:
    """四条参照行与全部设定在一套标签下的评价（与旧补充历史同一条计算路径）。"""
    events, unknown = labels
    references = reference_rows(prepared, events, unknown, ma_window)
    eta = configured_loss_settings(prepared.config).parameters.eta
    green, red = references[0].evaluated.total_loss, references[2].evaluated.total_loss
    settings = []
    for states in prepared.states:
        evaluated = evaluate_candidate(prepared, states, events, unknown)
        settings.append((evaluated, timing_result(evaluated, eta, green, red)))
    return references, tuple(settings)


def impacts(prepared: PreparedEvaluation, names: Sequence[str], old: Labels, new: Labels, ma_window: int,
            tolerance: Decimal) -> tuple[tuple, tuple[ObjectImpact, ...], tuple[ObjectImpact, ...], dict]:
    """逐日比对归类、逐对象核算，并核对局部调整之和与汇总的变化。"""
    weights = configured_loss_settings(prepared.config).weights
    references_old, settings_old = evaluations(prepared, old, ma_window)
    references_new, settings_new = evaluations(prepared, new, ma_window)
    differences = label_differences(references_old[0].evaluated.asset_losses,
                                    references_new[0].evaluated.asset_losses)
    green, red = (local_total(local_adjustment(references_old[index].evaluated, references_new[index].evaluated,
                                               differences, weights)) for index in (0, 2))
    reference_items = tuple(
        object_impact(before.name, "参照行", (before.evaluated, before.timing), (after.evaluated, after.timing),
                      differences, weights, green, red)
        for before, after in zip(references_old, references_new, strict=True))
    setting_items = tuple(object_impact(name, "候选设定", before, after, differences, weights, green, red)
                          for name, before, after in zip(names, settings_old, settings_new, strict=True))
    gaps = {item.name: reconcile(item, tolerance) for item in (*reference_items, *setting_items)}
    return differences, setting_items, reference_items, gaps


def same_or(count: int, text: str) -> str:
    return UNCHANGED if count == 0 else f"有变化：{text}"


def row_changes(old: Sequence[Row], new: Sequence[Row]) -> int:
    """两张表里不同的行数；行数不等时把多出的行也计入。"""
    return sum(a != b for a, b in zip(old, new, strict=False)) + abs(len(old) - len(new))


def fixed_events_text(old: Sequence[Row], new: Sequence[Row]) -> str:
    """固定延迟校准的事件表：两套标签下纳入的事件相同时，只可能在“下一事件 T0”一列不同。"""
    if len(old) != len(new):
        return f"纳入的事件数不同：{len(old)} → {len(new)}"
    changed = [(a, b) for a, b in zip(old, new, strict=True) if a != b]
    if not changed:
        return UNCHANGED
    def cells(first: Row, second: Row) -> str:
        return "、".join(f"{x or '空'} → {y or '空'}" for x, y in zip(first, second, strict=True) if x != y)

    detail = "；".join(f"{a[0]} 高点 {a[1]} 的事件的“下一事件 T0”一列 {cells(a, b)}" for a, b in changed)
    return (f"纳入的事件数相同（{len(old)} 件）；有 {len(changed)} 行不同：{detail}。"
            "这一列只在固定延迟的执行日不早于下一事件 T0 时才起作用，汇总与逐事件明细都没有变化")


def excluded_text(old: Sequence[Any], new: Sequence[Any]) -> str:
    before = sorted({row.summary.excluded_asset_intervals for row in old})
    after = sorted({row.summary.excluded_asset_intervals for row in new})
    return f"开发期标签下 {before}；截止日标签下 {after}（尾段未定的资产区间不计价格损失）"


def window_text(prepared: PreparedEvaluation, start: dt.date) -> str:
    days = prepared.inputs.days
    count = len(days) - 1 - days.index(prepared.first_loss_day)
    return (f"窗口 {start} 至 {days[-1]}；t0′ = {prepared.t0}，τ′ = {prepared.tau}，j₀′ = {prepared.first_loss_day}；"
            f"计入区间数 {count}。")


def conclusions(differences: Sequence, flips: int, crossing: bool) -> tuple[str, ...]:
    """原报告正文里与标签有关的文字结论，在截止日标签下是否仍然成立。"""
    return (
        "“危险标签取自完整标签文件”：这正是本次核算纠正的口径；截止日标签下，窗口末端的这些区间不再有危险标签。",
        "“跨窗口末端，不纳入”一节列出的 SPX 事件（高点 2009-09-22）：" + (
            "截止日标签下它不是事件（截止日时仍在寻峰），这一节在截止日标签下没有条目。" if crossing
            else "两套标签下相同。"),
        f"“缺值”一节的“被排除的资产区间：无”：截止日标签下有 {len(differences)} 个资产区间因尾段未定而不计价格损失。",
        "报告的其余正文都是表格与口径说明，没有比较优劣的文字结论。九组设定之间的排序、与参照行的比较方向见上面两小节"
        f"（比较方向改变 {flips} 项）。")


def v13_impact(root: Path, config: ImpactConfig, validation_file: str) -> VersionImpact:
    """v1.3 补充历史：P0 九组与四条参照行；另核对固定延迟校准、退出代价与跨窗口末端事件。"""
    v13 = load_v13_config(root / V13_CONFIG)
    windows = v13.windows
    inputs = load_price_inputs(root, windows.p0_end, validation_file)
    prepared = prepare_p0_window(v13.base, inputs, windows.p0_start)
    old = development_labels(root, config, v13.base.development_end())
    new = period_labels(v13.base, inputs, windows.p0_end)
    names = [f"P0 K={states.candidate.k} θ_P={states.candidate.theta_p}" for states in prepared.states]
    differences, settings, references, gaps = impacts(prepared, names, old, new, v13.ma200_window, config.tolerance)
    before = evaluate_p0_window(prepared, old[0], old[1], v13.ma200_window)
    after = evaluate_p0_window(prepared, new[0], new[1], v13.ma200_window)
    closes = {symbol: inputs.series[symbol] for symbol in SYMBOLS}
    fixed = [calibration_tables(inputs.days, closes,
                                {symbol: window_events(events[symbol], windows.fixed_delay_start[symbol],
                                                       windows.fixed_delay_end) for symbol in SYMBOLS}, ())
             for events in (old[0], new[0])]
    stored = check_stored(root / config.stored["v13"], {
        "p0_summary.csv": (P0_HEADER, [p0_row(row) for row in before.rows]),
        "reference_rows.csv": (REFERENCE_HEADER, [reference_row(row) for row in before.references]),
        "p0_exit_costs.csv": (EXIT_HEADER, exit_rows(before)),
        "fixed_delay_summary.csv": (FIXED_HEADER, fixed[0].fixed_summary)})
    rebounds = sum(a.rebounds != b.rebounds for a, b in zip(before.rows, after.rows, strict=True))
    exits = sum(a != b for a, b in zip(exit_rows(before), exit_rows(after), strict=True))
    crossing = sum(len(events) for events in before.crossing.values())
    others = {
        "退出代价的纳入件数、三类件数、R 与半山腰转绿（九组 P0）": same_or(rebounds, f"{rebounds} 组设定不同"),
        "逐事件退出代价（`p0_exit_costs.csv`）": same_or(exits, f"{exits} 行不同"),
        "固定延迟校准的汇总（`fixed_delay_summary.csv`）": same_or(
            row_changes(fixed[0].fixed_summary, fixed[1].fixed_summary), "汇总行不同"),
        "固定延迟校准的逐事件明细（`fixed_delay_detail.csv`）": same_or(
            row_changes(fixed[0].fixed_rows, fixed[1].fixed_rows), "明细行不同"),
        "固定延迟校准的事件表（`fixed_delay_events.csv`）": fixed_events_text(fixed[0].event_rows, fixed[1].event_rows),
        "跨窗口末端的事件": (f"开发期标签下 {crossing} 件；截止日标签下 "
                      f"{sum(len(events) for events in after.crossing.values())} 件"),
        "每组设定被排除的资产区间数": excluded_text(before.rows, after.rows),
    }
    version = VersionImpact("v1.3 补充历史（P0 稳健性）", window_text(prepared, windows.p0_start), differences,
                            settings, references, gaps, stored, others, ())
    return VersionImpact(version.name, version.window, differences, settings, references, gaps, stored, others,
                         conclusions(differences, len(changed_directions(version)), bool(crossing)))


def v14_impact(root: Path, config: ImpactConfig, validation_config: Path) -> VersionImpact:
    """v1.4 补充历史：纯价格版九组与四条参照行；另核对两次熊市、转绿延迟、R 与跨窗口末端事件。"""
    validation = load_validation_config(validation_config)
    model = validation.model
    inputs = load_price_inputs(root, model.history_end, validation.vix3m_file)
    prepared = prepare_price_window(model, inputs)
    old = development_labels(root, config, model.base.development_end())
    new = period_labels(model.base, inputs, model.history_end)
    names = [f"v1.4 纯价格版 K={states.candidate.k} θ_P={states.candidate.theta_p}" for states in prepared.states]
    differences, settings, references, gaps = impacts(prepared, names, old, new, model.mr_window, config.tolerance)
    before = evaluate_price_window(prepared, model, old[0], old[1])
    after = evaluate_price_window(prepared, model, new[0], new[1])
    stored = check_stored(root / config.stored["v14"], {
        "price_only_summary.csv": (SUMMARY_HEADER, [summary_row(row) for row in before.rows]),
        "reference_rows.csv": (REFERENCE_HEADER, [reference_row(row) for row in before.references]),
        "bear_markets.csv": (PERIOD_HEADER, before.bear_markets)})
    delays = sum((a.delays, a.rebounds) != (b.delays, b.rebounds)
                 for a, b in zip(before.rows, after.rows, strict=True))
    bears = sum(a != b for a, b in zip(before.bear_markets, after.bear_markets, strict=True))
    crossing = sum(len(events) for events in before.crossing.values())
    others = {
        "两次熊市的执行暴露、期间主损失与回撤（`bear_markets.csv`）": same_or(bears, f"{bears} 行不同"),
        "转绿延迟、R、半山腰转绿与三类件数（九组设定）": same_or(delays, f"{delays} 组设定不同"),
        "跨窗口末端的事件": (f"开发期标签下 {crossing} 件；截止日标签下 "
                      f"{sum(len(events) for events in after.crossing.values())} 件"),
        "每组设定被排除的资产区间数": excluded_text(before.rows, after.rows),
    }
    version = VersionImpact("v1.4 补充历史（纯价格版）", window_text(prepared, model.history_start), differences,
                            settings, references, gaps, stored, others, ())
    return VersionImpact(version.name, version.window, differences, settings, references, gaps, stored, others,
                         conclusions(differences, len(changed_directions(version)), bool(crossing)))


def write_impact(destination: Path, versions: Sequence[VersionImpact], config: ImpactConfig,
                 meta: Mapping[str, object]) -> str:
    tables = (("label_differences.csv", DIFFERENCE_HEADER, difference_rows(versions)),
              ("objects.csv", OBJECT_HEADER, object_rows(versions)),
              ("local_adjustments.csv", LOCAL_HEADER, local_rows(versions)),
              ("reconciliation.csv", RECONCILIATION_HEADER, reconciliation_rows(versions, config.tolerance)),
              ("comparisons.csv", COMPARISON_HEADER, comparison_rows(versions)),
              ("rankings.csv", RANKING_HEADER, ranking_rows(versions)))
    for name, header, rows in tables:
        write_csv(destination / name, header, rows)
    (destination / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notes = ["## 输入与代码", "",
             f"- 代码提交 `{meta['code_commit']}`（工作区{'有' if meta['worktree_dirty'] else '无'}未提交改动）。",
             *(f"- `{name}`：SHA-256 `{digest}`" for name, digest in meta["inputs"].items()),  # type: ignore[union-attr]
             "- SPX、QQQ 的读取在 2009-09-30 之后的第一行之前停止；截止日标签用现有标签函数在这些价格上生成。", ""]
    report = destination / REPORT_NAME
    report.write_text("\n".join(report_lines(versions, config.tolerance, notes)), encoding="utf-8")
    hashes = {path.name: file_sha256(path) for path in sorted(destination.iterdir())}
    (destination / HASH_FILE).write_text(json.dumps(hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return file_sha256(report)


def run_label_cutoff_impact(root: Path) -> ImpactRun:
    """旧补充历史的标签影响量化；输出目录已存在时拒绝覆盖。"""
    config = load_impact_config(root / IMPACT_CONFIG)
    output = root / config.output
    if output.exists():
        raise FileExistsError(f"标签影响量化目录已存在，拒绝覆盖：{output}")
    validation = load_validation_config(root / CONFIG_FILE)
    versions = (v13_impact(root, config, validation.vix3m_file), v14_impact(root, config, root / CONFIG_FILE))
    git = find_git(validation.git_executable)
    files = ("data/market/daily/SPX.csv", "data/market/daily/QQQ.csv", config.events, config.unknown)
    meta = {"code_commit": run_git(git, root, ("rev-parse", "HEAD")).stdout.decode().strip(),
            "worktree_dirty": bool(run_git(git, root, ("status", "--porcelain")).stdout.strip()),
            "inputs": {name: file_sha256(root / name) for name in files}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        digest = write_impact(staging, versions, config, meta)
        staging.rename(output)
    return ImpactRun(output, tuple((version.name, len(version.differences)) for version in versions), digest)
