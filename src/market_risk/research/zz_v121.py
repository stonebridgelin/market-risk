"""v1.2.1 ZZ 口径的 B、DV 分侧预登记开发期研究。"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from market_risk.research.analysis import RUN_ID, _candidate_rows, _feature_stats, _observation_rows, _write_csv
from market_risk.research.features import FeatureEngine
from market_risk.research.groups import Observation, classify_candidates, observations
from market_risk.research.io import ResearchInputs, load_development_feature_inputs
from market_risk.research.pullback import START, DangerPeriod, Episode, build_danger_periods
from market_risk.research.quality import feature_quality
from market_risk.research.statistics import auc, direction
from market_risk.storage.paths import StoragePaths

THRESHOLD = Decimal("0.99")
ZZ_LABEL_END = dt.date(2016, 12, 30)
FEATURES = {"SPX": ("S5TW_change_20", "S5TW_change_10"),
            "QQQ": ("NDTW_change_20", "NDTW_change_10")}


@dataclass(frozen=True)
class SideResult:
    symbol: str
    candidate_count: int
    sample_days: int
    sample_observations: int
    control_days: int
    control_observations: int
    unknown_candidate_days: int
    decisions: dict[str, str]
    categories: dict[str, int]


@dataclass(frozen=True)
class ZZStudyResult:
    report_path: Path
    danger_periods: int
    sides: tuple[SideResult, ...]


def _read_episodes(path: Path) -> tuple[Episode, ...]:
    """只读已生成的开发期 ZZ 研究标签，不导入 wavewarn 代码。"""
    result = []
    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            peak = dt.date.fromisoformat(row["peak_date"])
            trough = dt.date.fromisoformat(row["trough_date"])
            if peak > ZZ_LABEL_END or trough > ZZ_LABEL_END:
                raise ValueError("ZZ 研究标签越过开发期截止日")
            if trough >= START:
                result.append(Episode(row["symbol"], peak, trough, Decimal(row["peak_close"])))
    return tuple(result)


def _read_unknown(path: Path) -> frozenset[dt.date]:
    """两资产任一危险归属未定即从候选比较中排除。"""
    days: set[dt.date] = set()
    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row.get("label_end") != ZZ_LABEL_END.isoformat():
                raise ValueError("尾段未定清单的 ZZ 标签截止日与开发期不一致")
            day = dt.date.fromisoformat(row["date"])
            if day > ZZ_LABEL_END:
                raise ValueError("尾段未定清单越过开发期截止日")
            if day >= START:
                days.add(day)
    return frozenset(days)


def _selected_observations(rows: tuple[Observation, ...], features: tuple[str, str]
                           ) -> tuple[Observation, ...]:
    return tuple(replace(row, values={name: row.values[name] for name in features},
                         effective_days={name: row.effective_days[name] for name in features}) for row in rows)


def _quality_rows(engine: FeatureEngine, symbol: str, observed: tuple[Observation, ...],
                  features: tuple[str, str]) -> list[dict[str, object]]:
    rows = []
    for observation in observed:
        for day in observation.dates:
            values = engine.values(day)
            for feature in features:
                quality = feature_quality(engine, feature, day, values[feature])
                rows.append({"side": symbol, "observation": observation.key, "kind": observation.kind,
                             "date": day, "feature": feature, "value": values[feature],
                             "missing": quality.missing, "primary_reason": quality.primary_reason,
                             "all_reasons": quality.all_reasons,
                             "single_value_stage": quality.single_value_stage})
    return rows


def _leave_one_out_rows(symbol: str, observed: tuple[Observation, ...],
                        features: tuple[str, str]) -> list[dict[str, object]]:
    """保留每一段删除后的 AUC，供三项一致性检验逐行复核。"""
    rows = []
    for feature in features:
        samples = [(item.key, float(item.values[feature])) for item in observed
                   if item.kind == "样本组" and item.values[feature] is not None]
        controls = [float(item.values[feature]) for item in observed
                    if item.kind == "对照组" and item.values[feature] is not None]
        for omitted, _ in samples:
            remaining = [value for key, value in samples if key != omitted]
            value = auc(remaining, controls)
            rows.append({"side": symbol, "feature": feature, "omitted_period_start": omitted,
                         "remaining_sample_n": len(remaining), "control_n": len(controls),
                         "auc": value, "direction": direction(value)})
    return rows


def _decision(row: dict[str, object]) -> str:
    """严格执行规格第四节：四个方向条件都满足才保留。"""
    if row["sample_insufficient"]:
        raise ValueError("预登记去留遇到有效样本少于8个，须由负责人确认解读边界")
    if row["auc"] is None:
        raise ValueError("预登记去留缺少有效 AUC，须由负责人确认解读边界")
    return "保留" if (float(row["auc"]) < 0.5 and row["subperiod_direction_same"]
                    and row["leave_one_out_direction_same"] and row["top_group_direction_same"]
                    ) else "移除"


def _side(engine: FeatureEngine, periods: tuple[DangerPeriod, ...], days: tuple[dt.date, ...],
          unknown: frozenset[dt.date], symbol: str, output: Path
          ) -> tuple[SideResult, list[dict[str, object]], list[dict[str, object]]]:
    features = FEATURES[symbol]
    candidates = classify_candidates(engine, periods, days, THRESHOLD, symbol, unknown)
    observed = _selected_observations(observations(engine, periods, candidates, days), features)
    stats = _feature_stats(engine, observed, THRESHOLD)
    for row in stats:
        row["side"] = symbol
        row["channel"] = "B" if str(row["feature"]).endswith("_20") else "DV"
        row["preregistered_decision"] = _decision(row)
    _write_csv(output / f"{symbol}_高位候选日分组.csv", _candidate_rows(candidates))
    _write_csv(output / f"{symbol}_特征观测值.csv", _observation_rows(observed))
    loo_rows = _leave_one_out_rows(symbol, observed, features)
    _write_csv(output / f"{symbol}_逐时段剔除AUC.csv", loo_rows)
    for row in stats:
        omitted = [float(item["auc"]) for item in loo_rows if item["feature"] == row["feature"]
                   and item["auc"] is not None]
        row["leave_one_out_auc_min"] = min(omitted) if omitted else None
        row["leave_one_out_auc_max"] = max(omitted) if omitted else None
    quality = _quality_rows(engine, symbol, observed, features)
    _write_csv(output / f"{symbol}_特征缺值明细.csv", quality)
    counts = Counter(row.category for row in candidates)
    result = SideResult(symbol, len(candidates), counts["样本组"],
                        sum(item.kind == "样本组" for item in observed), counts["对照组"],
                        sum(item.kind == "对照组" for item in observed),
                        counts["期末尾段危险归属未定"],
                        {str(row["channel"]): str(row["preregistered_decision"]) for row in stats},
                        dict(counts))
    return result, stats, quality


def run_zz_side_study(paths: StoragePaths) -> ZZStudyResult:
    """只读开发期；结果另存，不覆盖原口径前瞻报告。"""
    inputs: ResearchInputs = load_development_feature_inputs(paths, RUN_ID)
    source = paths.reports_dir / "research" / "wavewarn_v121"
    episodes = _read_episodes(source / "zz_events_development.csv")
    unknown = _read_unknown(source / "zz_unknown_development.csv")
    periods = build_danger_periods(episodes, inputs.market, inputs.days)
    days = tuple(day for day in inputs.days if START <= day <= ZZ_LABEL_END)
    engine = FeatureEngine.create(inputs)
    output = paths.reports_dir / "research" / "zz_v121"
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "ZZ危险时段清单.csv", [
        {"start": row.start, "end": row.end, "source": row.source,
         "members": tuple(f"{item.symbol}:{item.high_date}→{item.low_date}" for item in row.members),
         "spx_top": row.spx_top, "qqq_top": row.qqq_top,
         "spx_bottom": row.spx_bottom, "qqq_bottom": row.qqq_bottom}
        for row in periods])
    sides = []
    all_stats = []
    quality_rows = []
    for symbol in ("SPX", "QQQ"):
        side, stats, quality = _side(engine, periods, days, unknown, symbol, output)
        sides.append(side)
        all_stats.extend(stats)
        quality_rows.extend(quality)
    _write_csv(output / "分侧特征统计.csv", all_stats)
    summary = []
    for symbol in ("SPX", "QQQ"):
        for feature in FEATURES[symbol]:
            rows = [row for row in quality_rows if row["side"] == symbol and row["feature"] == feature]
            missing = Counter(str(row["primary_reason"]) for row in rows if row["missing"])
            summary.append({"side": symbol, "feature": feature, "raw_days": len(rows),
                            "valid_days": sum(not row["missing"] for row in rows),
                            "single_value_stage_days": sum(bool(row["single_value_stage"]) for row in rows),
                            **{f"missing_{reason}": missing[reason] for reason in (
                                "序列未开始", "窗口不足", "缺口", "单值阶段")}})
    _write_csv(output / "分侧缺值审计.csv", summary)
    lines = ["# ZZ 4%/5%（SPX）、5%/6.5%（QQQ）B、DV 分侧前瞻研究", "",
             "预登记规则的开发期结果，待负责人审核。仅使用 2008-08-11 至 2016-12-30 的候选日与特征；"
             "ZZ 标签也只使用开发期及更早价格。结果不构成历史预警效果或实盘有效性。", "",
             f"共 {len(periods)} 个 ZZ 危险时段；任一资产尾段归属未定的交易日 {len(unknown)} 天。", "",
             "| 侧 | 高位候选日 | 样本原始日/时段 | 对照原始日/簇 | 尾段未定候选日 | B | DV |",
             "|---|---:|---:|---:|---:|---|---|"]
    for side in sides:
        lines.append(f"| {side.symbol} | {side.candidate_count} | {side.sample_days}/{side.sample_observations} "
                     f"| {side.control_days}/{side.control_observations} | {side.unknown_candidate_days} "
                     f"| {side.decisions['B']} | {side.decisions['DV']} |")
    lines.extend(["", "分组明细（包括所有不纳入比较的候选日；同一日若对应多个时段，样本日可重复映射）："])
    for side in sides:
        lines.append(f"- {side.symbol}：" + "；".join(f"{name} {count} 日"
                     for name, count in sorted(side.categories.items())))
    lines.extend(["", "AUC=P(样本>对照)+0.5×P(并列)；每侧的 B=ΔW20、DV=ΔW10。"
                  "区间以时段和簇为单位、种子 20260928 重抽样 2000 次。", "",
                  "| 侧 | 通道 | AUC | 95%区间 | 样本/对照有效观测 | 子期一致 | "
                  "逐事件剔除一致 | 先见顶分组一致 | 单值阶段剔除AUC | 判定 |",
                  "|---|---|---:|---|---:|---|---|---|---:|---|"])
    for row in all_stats:
        lines.append(f"| {row['side']} | {row['channel']} | {row['auc']} | "
                     f"[{row['auc_ci_low']}, {row['auc_ci_high']}] | {row['sample_n']}/{row['control_n']} "
                     f"| {row['subperiod_direction_same']} | {row['leave_one_out_direction_same']} "
                     f"| {row['top_group_direction_same']} | {row['clean_single_value_auc']} "
                     f"| {row['preregistered_decision']} |")
    lines.extend(["", "三项一致性检验的数值依据：", "",
                  "| 侧 | 通道 | 早/晚子期AUC | 逐时段剔除AUC范围 | SPX先 AUC/n | QQQ先 AUC/n | "
                  "同日 AUC/n | p | BH q |",
                  "|---|---|---|---|---|---|---|---:|---:|"])
    for row in all_stats:
        lines.append(f"| {row['side']} | {row['channel']} | "
                     f"{row['subperiod_early_auc']}/{row['subperiod_late_auc']} | "
                     f"{row['leave_one_out_auc_min']} 至 {row['leave_one_out_auc_max']} | "
                     f"{row['spx_first_auc']}/{row['spx_first_n']} | "
                     f"{row['qqq_first_auc']}/{row['qqq_first_n']} | "
                     f"{row['same_day_auc']}/{row['same_day_n']} | {row['p_value']} | {row['bh_q']} |")
    lines.extend(["", "单值K线阶段截至 2010-10-01（含当天），分别以 S5TW、NDTW 所依赖的日期判定；"
                  "剔除后 AUC 仅作敏感性，不用于改变预登记判定。逐日原因见缺值明细，"
                  "逐观测值有效日数见特征观测值。", "",
                  "| 侧 | 特征 | 原始日 | 有效日 | 涉及单值阶段日 | 序列未开始/窗口不足/缺口/单值阶段缺失 |",
                  "|---|---|---:|---:|---:|---|"])
    for row in summary:
        lines.append(f"| {row['side']} | {row['feature']} | {row['raw_days']} | {row['valid_days']} | "
                     f"{row['single_value_stage_days']} | "
                     f"{row['missing_序列未开始']}/{row['missing_窗口不足']}/"
                     f"{row['missing_缺口']}/{row['missing_单值阶段']} |")
    lines.append("")
    report = output / "B_DV分侧去留报告.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return ZZStudyResult(report, len(periods), tuple(sides))
