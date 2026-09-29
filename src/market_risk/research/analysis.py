"""小周期波段前瞻研究的编排、统计与可复核文件输出。"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.research.features import FeatureEngine, high_position_days
from market_risk.research.groups import (
    Candidate,
    Observation,
    classify_candidates,
    observations,
    uncovered_before_starts,
)
from market_risk.research.io import ResearchInputs, load_inputs
from market_risk.research.pullback import (
    DEVELOPMENT_END,
    DangerPeriod,
    build_danger_periods,
    lead_lag,
    rule_performance,
)
from market_risk.research.quality import SINGLE_VALUE_END, single_value_stage
from market_risk.research.statistics import (
    SEED,
    auc,
    bh_adjust,
    bootstrap_auc,
    direction,
    mann_whitney_p,
    percentile,
    wilson,
)
from market_risk.storage.paths import StoragePaths

RUN_ID = "run_20260928T112013Z_2cc8969"
THRESHOLDS = (Decimal("0.99"), Decimal("0.995"), Decimal("0.98"))
SPLIT = dt.date(2011, 12, 31)


@dataclass(frozen=True)
class ResearchResult:
    report_path: Path
    danger_count: int
    combined_count: int
    feature_count: int
    auc_rows: tuple[Mapping[str, object], ...]


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "|".join(_cell(item) for item in value)
    return str(value)


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        raise ValueError(f"没有可写入的研究明细：{path.name}")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _cell(row.get(key)) for key in fields})


def _distribution(values: Sequence[float | Decimal | int]) -> str:
    numeric = [float(value) for value in values]
    if not numeric:
        return "n=0"
    return (f"n={len(numeric)}；最小={min(numeric):.4f}；P25={percentile(numeric, 0.25):.4f}；"
            f"中位={percentile(numeric, 0.5):.4f}；P75={percentile(numeric, 0.75):.4f}；"
            f"最大={max(numeric):.4f}")


def _period_rows(periods: Sequence[DangerPeriod]) -> list[dict[str, object]]:
    return [{"start": period.start, "end": period.end, "period": period.period,
             "source": period.source, "start_symbol": period.start_symbol, "end_symbol": period.end_symbol,
             "sessions": period.sessions, "spx_top": period.spx_top, "qqq_top": period.qqq_top,
             "spx_bottom": period.spx_bottom, "qqq_bottom": period.qqq_bottom,
             "spx_max_drawdown_pct": period.spx_drawdown_pct, "qqq_max_drawdown_pct": period.qqq_drawdown_pct,
             "member_count": len(period.members),
             "members": tuple(f"{e.symbol}:{e.high_date}→{e.low_date}" for e in period.members)}
            for period in periods]


def _overlaps(a: dt.date, b: dt.date, c: dt.date, d: dt.date) -> bool:
    return a <= d and c <= b


def _small_nonbear(period: DangerPeriod, bear_spans: Sequence[tuple[dt.date, dt.date]]) -> bool:
    fall = period.spx_drawdown_pct if period.spx_top else period.qqq_drawdown_pct
    return fall is not None and -fall < 10 and not any(
        _overlaps(period.start, period.end, start, end) for start, end in bear_spans)


def _quality_cutoff(engine: FeatureEngine, feature: str, day: dt.date) -> bool:
    """相关源的最早依赖日期仍处单值阶段时，排除该日的敏感性观测。"""
    return single_value_stage(engine, feature, day)


def _value_for_feature(engine: FeatureEngine, observation: Observation, name: str,
                       clean_single_value: bool = False) -> float | None:
    if not clean_single_value or not any(name.startswith(symbol) for symbol in SINGLE_VALUE_END):
        value = observation.values[name]
        return float(value) if value is not None else None
    values = [engine.values(day)[name] for day in observation.dates if not _quality_cutoff(engine, name, day)]
    good = [value for value in values if value is not None]
    return float(sum(good) / Decimal(len(good))) if good else None


def _auc_for(observations_: Sequence[Observation], name: str, engine: FeatureEngine,
             clean_single_value: bool = False) -> tuple[float | None, int, int]:
    sample = [_value_for_feature(engine, obs, name, clean_single_value) for obs in observations_
              if obs.kind == "样本组"]
    control = [_value_for_feature(engine, obs, name, clean_single_value) for obs in observations_
               if obs.kind == "对照组"]
    valid_sample = [value for value in sample if value is not None]
    valid_control = [value for value in control if value is not None]
    return auc(valid_sample, valid_control), len(valid_sample), len(valid_control)


def _feature_stats(engine: FeatureEngine, obs: Sequence[Observation], threshold: Decimal) -> list[dict[str, object]]:
    if not obs:
        return []
    names = tuple(obs[0].values)
    result = []
    for name in names:
        samples = [(item, _value_for_feature(engine, item, name)) for item in obs if item.kind == "样本组"]
        controls = [(item, _value_for_feature(engine, item, name)) for item in obs if item.kind == "对照组"]
        sample_values = [value for _, value in samples if value is not None]
        control_values = [value for _, value in controls if value is not None]
        main = auc(sample_values, control_values)
        interval = bootstrap_auc(sample_values, control_values, SEED)
        p = mann_whitney_p(sample_values, control_values)
        subperiod = []
        for start, end in ((dt.date(2008, 8, 11), SPLIT), (dt.date(2012, 1, 1), DEVELOPMENT_END)):
            subset = [item for item in obs if start <= (item.period_start or item.dates[0]) <= end]
            subperiod.append(_auc_for(subset, name, engine)[0])
        loo = []
        for sample, value in samples:
            if value is not None:
                loo.append(auc([other for item, other in samples if item.key != sample.key and other is not None],
                               control_values))
        groups = []
        group_counts = []
        for first in ("SPX", "QQQ", "同日"):
            group_samples = [value for item, value in samples if item.top_first == first and value is not None]
            groups.append(auc(group_samples, control_values))
            group_counts.append(len(group_samples))
        clean, clean_n, clean_control_n = _auc_for(obs, name, engine, clean_single_value=True)
        directions = [direction(x) for x in (*subperiod, *loo, *groups)]
        consistent = main is not None and direction(main) != "等于50%" and all(
            value == direction(main) for value in directions)
        result.append({"threshold": threshold, "feature": name, "sample_n": len(sample_values),
                       "control_n": len(control_values), "auc": main, "auc_ci_low": interval[0],
                       "auc_ci_high": interval[1], "p_value": p, "subperiod_early_auc": subperiod[0],
                       "subperiod_late_auc": subperiod[1], "subperiod_direction_same":
                       all(direction(x) == direction(main) for x in subperiod),
                       "leave_one_out_direction_same": all(direction(x) == direction(main) for x in loo),
                       "spx_first_auc": groups[0], "qqq_first_auc": groups[1], "same_day_auc": groups[2],
                       "spx_first_n": group_counts[0], "qqq_first_n": group_counts[1],
                       "same_day_n": group_counts[2],
                       "top_group_direction_same": all(direction(x) == direction(main) for x in groups),
                       "all_direction_same": consistent, "clean_single_value_auc": clean,
                       "clean_sample_n": clean_n, "clean_control_n": clean_control_n,
                       "sample_missing_days": sum(len(item.dates) - item.effective_days[name] for item, _ in samples),
                       "control_missing_days": sum(len(item.dates) - item.effective_days[name] for item, _ in controls),
                       "data_quality_note": "HIGN/LOWN缺口不插值" if name.startswith("HIGN_minus_LOWN") else
                       "单值K线阶段另列剔除结果" if any(name.startswith(s) for s in SINGLE_VALUE_END) else "",
                       "sample_insufficient": len(sample_values) < 8})
        if len(sample_values) < 8:
            for field in ("subperiod_direction_same", "leave_one_out_direction_same",
                          "top_group_direction_same", "all_direction_same"):
                result[-1][field] = None
    adjusted = bh_adjust([row["p_value"] for row in result])
    for row, q in zip(result, adjusted, strict=True):
        row["bh_q"] = q
        row["bh_q_0_10"] = q is not None and q <= 0.10 if not row["sample_insufficient"] else None
    return result


def _candidate_rows(candidates: Sequence[Candidate]) -> list[dict[str, object]]:
    return [{"date": item.date, "category": item.category, "target_starts": item.target_starts,
             "inside_danger": item.in_danger, "future_5_complete": item.complete_5,
             "future_20_complete": item.complete_20} for item in candidates]


def _observation_rows(observations_: Sequence[Observation]) -> list[dict[str, object]]:
    return [{"key": item.key, "kind": item.kind, "dates": item.dates, "raw_days": len(item.dates),
             "period_start": item.period_start, "top_first": item.top_first,
             **item.values, **{f"{name}_effective_days": count for name, count in item.effective_days.items()}}
            for item in observations_]


def _wilson_rows(candidates: Sequence[Candidate], inputs: ResearchInputs, periods: Sequence[DangerPeriod],
                 development_days: Sequence[dt.date]) -> list[dict[str, object]]:
    index = {day: i for i, day in enumerate(development_days)}
    starts = {period.start for period in periods if period.period == "开发期"}
    rows = []
    for version in ("v2-M", "v3-R1"):
        condition_days = [item.date for item in candidates if item.complete_5
                          and int(inputs.scores[item.date, version]["total_min"]) >= 3]
        positives = sum(any(day in starts for day in development_days[index[start] + 1:index[start] + 6])
                        for start in condition_days)
        lower, upper = wilson(positives, len(condition_days))
        rows.append({"version": version, "condition_days": len(condition_days), "future_start_days": positives,
                     "proportion": positives / len(condition_days) if condition_days else None,
                     "wilson_low": lower, "wilson_high": upper})
    return rows


def _markdown_report(inputs: ResearchInputs, periods: Sequence[DangerPeriod], lead_rows: Sequence[dict[str, object]],
                     performance: Sequence[dict[str, object]], candidates: Sequence[Candidate],
                     obs: Sequence[Observation], feature_rows: Sequence[dict[str, object]],
                     sensitivity: Sequence[dict[str, object]], wilson_rows: Sequence[dict[str, object]],
                     engine: FeatureEngine, development_days: Sequence[dt.date]) -> str:
    lines = ["# 小周期波段高点前瞻特征分析报告", "", f"数据：正式回测 `{inputs.run_id}`；"
             "先后与滞后统计截至2022-12-30，特征比较仅用开发期。",
             "本报告只描述事后评估事实，不构造信号规则。危险时段起止及分组均为事后信息。", "",
             "## 第1节 两个指数的先后与死锁风险", "",
             f"危险时段共 {len(periods)} 段：双指数 {sum(p.source == '双指数' for p in periods)} 段；"
             f"仅 SPX {sum(p.source == 'SPX' for p in periods)} 段；"
             f"仅 QQQ {sum(p.source == 'QQQ' for p in periods)} 段。", ""]
    lines.append(f"跨区间边界未计入的回调 {len(inputs.excluded_episodes)} 段：" +
                 ("；".join(f"{item['symbol']} {item['high_date']} 至 {item['low_date'] or '低点未解锁'}"
                           for item in inputs.excluded_episodes) if inputs.excluded_episodes else "无") + "。")
    lines.append("")
    lines.append(f"`episode_windows.csv` 截至验证期共核对 {inputs.episode_window_rows} 行，"
                 "其评分上下限与 `daily_scores.csv` 一致。")
    lines.append("")
    for segment in ("开发期", "验证期", "合计"):
        rows = [row for row in lead_rows if segment == "合计" or row["period"] == segment]
        lines.append(f"### {segment}（双指数 n={len(rows)}）")
        lines.append("")
        for kind, field in (("见顶", "top"), ("见底", "bottom")):
            count = Counter(str(row[f"{field}_first"]) for row in rows)
            lines.append(f"{kind}：SPX 先 {count['SPX']}、QQQ 先 {count['QQQ']}、同日 {count['同日']}。"
                         f"相差交易日：{_distribution([int(row[f'{field}_gap_days']) for row in rows])}。")
        rebounds = [row["early_bottom_rebound_pct"] for row in rows
                    if row["early_bottom_rebound_pct"] is not None]
        lines.append(f"先见底指数至后见底日的反弹（%，同日见底留空）：{_distribution(rebounds)}。")
        lines.append("")
    late = [row for row in lead_rows if int(row["bottom_gap_days"]) > 20]
    lines.extend(["见底相差超过20个交易日的时段：" + ("；".join(
        f"{row['start']} 至 {row['end']}（{row['bottom_gap_days']}日）" for row in late) if late else "无") + "。", ""])
    largest = sorted((row for row in lead_rows if row["early_bottom_rebound_pct"] is not None),
                     key=lambda row: row["early_bottom_rebound_pct"], reverse=True)[:5]
    lines.append("取晚机会成本反弹最大的五例：" + "；".join(
        f"{row['start']} 至 {row['end']}（{row['bottom_first']}先见底，"
        f"反弹{row['early_bottom_rebound_pct']:.4f}%）" for row in largest) + "。")
    lines.append("")
    multiple = [p for p in periods if sum(e.symbol == "SPX" for e in p.members) > 1
                or sum(e.symbol == "QQQ" for e in p.members) > 1]
    lines.append("同一指数含多段回调的时段：")
    lines.extend(f"- {p.start} 至 {p.end}：" + "；".join(f"{e.symbol} {e.high_date} 至 {e.low_date}" for e in p.members)
                 for p in multiple)
    lines.extend(["", "## 第2节 现有规则的滞后程度", "",
                  "预警以总分下限≥3判定；明确降级以总分上限<3判定；跨越3分的范围计入未知日。"
                  "第一个危险时段的前20日评分窗口不完整，只检索可得评分日。", ""])
    for segment in ("开发期", "验证期"):
        lines.extend([f"### {segment}", ""])
        for version in ("v2-M", "v3-R1"):
            rows = [row for row in performance if row["period"] == segment and row["version"] == version]
            offsets = [int(row["warning_offset"]) for row in rows if row["warning_offset"] is not None]
            uncensored = [int(row["warning_offset"]) for row in rows if row["warning_offset"] is not None
                          and not row["already_warning_at_window_start"]]
            early = [row for row in rows if row["early_downgrade"]]
            lines.append(f"{version}：n={len(rows)}；未预警 {sum(row['unwarned'] for row in rows)}；"
                         f"未预警比例 {sum(row['unwarned'] for row in rows) / len(rows):.2%}；"
                         f"首次≥3分偏移（含窗口首日已预警）：{_distribution(offsets)}；"
                         f"排除窗口首日已预警：{_distribution(uncensored)}；"
                         f"覆盖率：{_distribution([row['coverage'] for row in rows])}；"
                         f"过早降级 {len(early)}/{len(rows)}（{len(early) / len(rows):.2%}）；"
                         f"未知日 {sum(int(row['unknown_days']) for row in rows)}。")
            for grade in ("小回调", "修正", "熊市"):
                subset = [row for row in rows if row["grade"] == grade]
                spx_progress = [row["spx_first_warning_progress"] for row in subset
                                if row["spx_first_warning_progress"] is not None]
                qqq_progress = [row["qqq_first_warning_progress"] for row in subset
                                if row["qqq_first_warning_progress"] is not None]
                spx_tail = [row["spx_downgrade_to_min_pct"] for row in subset
                            if row["spx_downgrade_to_min_pct"] is not None]
                qqq_tail = [row["qqq_downgrade_to_min_pct"] for row in subset
                            if row["qqq_downgrade_to_min_pct"] is not None]
                lines.append(f"- {grade} n={len(subset)}；未预警 {sum(row['unwarned'] for row in subset)}；"
                             f"未预警比例 {sum(row['unwarned'] for row in subset) / len(subset):.2%}；"
                             f"覆盖率 {_distribution([row['coverage'] for row in subset])}；"
                             f"过早降级 {sum(row['early_downgrade'] for row in subset)}/{len(subset)}；"
                             f"首次预警 SPX 进度 {_distribution(spx_progress)}；"
                             f"QQQ 进度 {_distribution(qqq_progress)}；"
                             f"降级后 SPX 最低收盘跌幅（%） {_distribution(spx_tail)}；"
                             f"QQQ {_distribution(qqq_tail)}。")
            nonbear = [row for row in rows if row["small_nonbear"]]
            nonbear_spx_progress = [row["spx_first_warning_progress"] for row in nonbear
                                    if row["spx_first_warning_progress"] is not None]
            nonbear_qqq_progress = [row["qqq_first_warning_progress"] for row in nonbear
                                    if row["qqq_first_warning_progress"] is not None]
            nonbear_spx_tail = [row["spx_downgrade_to_min_pct"] for row in nonbear
                                if row["spx_downgrade_to_min_pct"] is not None]
            nonbear_qqq_tail = [row["qqq_downgrade_to_min_pct"] for row in nonbear
                                if row["qqq_downgrade_to_min_pct"] is not None]
            lines.append(f"- 非熊市背景下的小回调 n={len(nonbear)}；未预警 {sum(row['unwarned'] for row in nonbear)}；"
                     f"覆盖率 {_distribution([row['coverage'] for row in nonbear])}；"
                         f"过早降级 {sum(row['early_downgrade'] for row in nonbear)}；"
                         f"首次预警 SPX 进度 {_distribution(nonbear_spx_progress)}；"
                         f"QQQ 进度 {_distribution(nonbear_qqq_progress)}；"
                         f"降级后 SPX 最低收盘跌幅（%） {_distribution(nonbear_spx_tail)}；"
                         f"QQQ {_distribution(nonbear_qqq_tail)}。")
        lines.append("")
    category_counts = Counter(item.category for item in candidates)
    sample_obs = [item for item in obs if item.kind == "样本组"]
    control_obs = [item for item in obs if item.kind == "对照组"]
    total_pre, missing_pre = uncovered_before_starts(engine, periods, development_days,
                                                     high_position_days(engine, development_days, Decimal("0.99")))
    lines.extend(["## 第3节 高点前瞻特征（开发期）", "",
                  f"高位阈值0.99；候选日 {len(candidates)}；样本原始日 {category_counts['样本组']}、"
                  f"危险时段观测值 {len(sample_obs)}；对照原始日 {category_counts['对照组']}、"
                  f"高位簇观测值 {len(control_obs)}。",
                  "候选日分组：" + "；".join(f"{key} {value}" for key, value in sorted(category_counts.items())) + "。",
                  f"危险起点前1至5日共有 {total_pre} 个不同交易日，其中 {missing_pre} 日"
                  f"（{missing_pre / total_pre:.2%}）不满足高位条件。", ""])
    for row in wilson_rows:
        lines.append(f"{row['version']} 总分下限≥3：未来1至5日有危险起点 {row['future_start_days']}/"
                     f"{row['condition_days']}；Wilson 95%区间 {row['wilson_low']:.4f} 至 {row['wilson_high']:.4f}。")
    lines.extend(["", f"AUC：P(样本>对照)+0.5P(并列)；2000次簇重抽样，随机种子 {SEED}。"
                  "p值为 Mann–Whitney U 双侧并列校正正态近似；全部特征统一作 BH 调整（q=0.10）。",
                  f"特征共 {len(feature_rows)} 项；BH q≤0.10 共 "
                  f"{sum(bool(row['bh_q_0_10']) for row in feature_rows)} 项。"
                  "有效样本观测值不足8项者只列数字，不解读。", ""])
    lines.append("| 特征 | 样本/对照 | AUC [95%区间] | 子时期/逐段剔除/见顶组方向 | "
                 "阈值0.995/0.98 AUC | 剔除单值期 AUC | BH q |")
    lines.append("|---|---:|---:|---|---:|---:|---:|")
    for row in feature_rows:
        status = ("样本不足，不解读" if row["sample_insufficient"] else "/".join(
            "一致" if row[key] else "不一致" for key in
            ("subperiod_direction_same", "leave_one_out_direction_same", "top_group_direction_same")))
        first_alt = row["threshold_0_995_auc"]
        second_alt = row["threshold_0_98_auc"]
        clean = row["clean_single_value_auc"]
        lines.append(f"| {row['feature']} | {row['sample_n']}/{row['control_n']} | "
                     f"{row['auc']:.4f} [{row['auc_ci_low']:.4f}, {row['auc_ci_high']:.4f}] | "
                     f"{status} | {first_alt:.4f}/{second_alt:.4f} | "
                     f"{clean:.4f} | {row['bh_q']:.4g} |")
    lines.extend(["", "参数敏感性：阈值0.995及0.98的逐特征 AUC 与方向变化见 `特征统计.csv`。",
                  "单值K线阶段剔除后的逐特征 AUC 及有效观测数亦见该明细。", "",
                  "## 第4节 数据质量与局限", "",
                  "S5FI 单值阶段至2010-08-31、S5TW 与 NDTW 至2010-10-01（均含当天）。"
                  "相关特征的敏感性结果排除依赖这些日期的候选日。HIGN、LOWN 缺口不插值，"
                  "每个观测值保留有效日数。R2 系列早期缺失，VIX3M 只从2009-09-18起取用。",
                  "危险时段及标签由事后价格确定；样本量、来源修订和聚类方式限制解释范围。"
                  "验证期不用于特征比较，保留期不读取。", "",
                  "## 第5节 供负责人参考的观察（非结论）", "",
                  "以下只列达到预登记一致性口径、且有效样本观测值至少8个的数值；不据此设计规则。", ""])
    eligible = sorted((row for row in feature_rows if row["all_direction_same"] and not row["sample_insufficient"]),
                      key=lambda row: abs(row["auc"] - 0.5), reverse=True)
    if eligible:
        lines.extend(f"- {row['feature']}：AUC {row['auc']:.4f}，有效样本/对照 "
                     f"{row['sample_n']}/{row['control_n']}。" for row in eligible[:5])
    else:
        lines.append("- 无同时满足上述条件的特征。")
    lines.append("")
    return "\n".join(lines)


def run_analysis(paths: StoragePaths) -> ResearchResult:
    """只读取指定正式回测与截至验证期的数据，写入独立研究目录。"""
    inputs = load_inputs(paths, RUN_ID)
    periods = build_danger_periods(inputs.episodes, inputs.market, inputs.days)
    counts = Counter(period.source for period in periods)
    if (len(periods), counts["双指数"], counts["SPX"], counts["QQQ"]) != (57, 42, 11, 4):
        raise ValueError(f"危险时段数量与交接核对不符：{len(periods)} / {dict(counts)}")
    lead_rows = [row for period in periods if (row := lead_lag(period, inputs.market, inputs.days))]
    performance = []
    for period in periods:
        for version in ("v2-M", "v3-R1"):
            row = rule_performance(period, version, inputs.scores, inputs.market, inputs.days)
            row["small_nonbear"] = _small_nonbear(period, inputs.bear_spans)
            performance.append(row)
    engine = FeatureEngine.create(inputs)
    development_days = tuple(day for day in inputs.days if day <= DEVELOPMENT_END)
    groupings = {threshold: classify_candidates(engine, periods, development_days, threshold)
                 for threshold in THRESHOLDS}
    observed = {threshold: observations(engine, periods, groupings[threshold], development_days)
                for threshold in THRESHOLDS}
    stats = {threshold: _feature_stats(engine, observed[threshold], threshold) for threshold in THRESHOLDS}
    feature_rows = stats[Decimal("0.99")]
    by_feature = {threshold: {row["feature"]: row for row in stats[threshold]} for threshold in THRESHOLDS[1:]}
    for row in feature_rows:
        for threshold in THRESHOLDS[1:]:
            variant = by_feature[threshold][row["feature"]]
            label = str(threshold).replace(".", "_")
            row[f"threshold_{label}_auc"] = variant["auc"]
            row[f"threshold_{label}_direction_same"] = (
                direction(variant["auc"]) == direction(row["auc"])) \
                if not row["sample_insufficient"] and not variant["sample_insufficient"] else None
            row[f"threshold_{label}_sample_n"] = variant["sample_n"]
            row[f"threshold_{label}_control_n"] = variant["control_n"]
    wilson_rows = _wilson_rows(groupings[Decimal("0.99")], inputs, periods, development_days)
    out = paths.reports_dir / "research"
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "危险时段清单.csv", _period_rows(periods))
    if inputs.excluded_episodes:
        _write_csv(out / "边界排除回调.csv", inputs.excluded_episodes)
    _write_csv(out / "双指数先后.csv", lead_rows)
    _write_csv(out / "各时段现有规则表现.csv", performance)
    _write_csv(out / "高位候选日分组.csv", _candidate_rows(groupings[Decimal("0.99")]))
    _write_csv(out / "特征观测值.csv", _observation_rows(observed[Decimal("0.99")]))
    _write_csv(out / "特征统计.csv", feature_rows)
    _write_csv(out / "Wilson区间.csv", wilson_rows)
    report = out / "小周期波段高点前瞻特征分析报告.md"
    report.write_text(_markdown_report(inputs, periods, lead_rows, performance, groupings[Decimal("0.99")],
                                       observed[Decimal("0.99")], feature_rows, stats[Decimal("0.995")],
                                       wilson_rows, engine, development_days), encoding="utf-8")
    return ResearchResult(report, len(periods), counts["双指数"], len(feature_rows), tuple(feature_rows))
