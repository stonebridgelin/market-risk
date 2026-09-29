"""原口径前瞻研究的开发期补充审计，不重新计算验证期评估。"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.config import load_holidays
from market_risk.research.features import FeatureEngine
from market_risk.research.io import load_development_feature_inputs
from market_risk.research.pullback import DEVELOPMENT_END
from market_risk.research.quality import REASON_ORDER, feature_quality
from market_risk.storage.paths import StoragePaths

RUN_ID = "run_20260928T112013Z_2cc8969"
START_MARK = "<!-- development-audit:start -->"
END_MARK = "<!-- development-audit:end -->"


@dataclass(frozen=True)
class DevelopmentAuditResult:
    report_path: Path
    summary_path: Path
    detail_path: Path
    duplicate_candidate_dates: int
    feature_count: int


def _read_dev_csv(path: Path, field: str) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    for row in rows:
        if field == "dates":
            dates = (dt.date.fromisoformat(value) for value in row[field].split("|") if value)
        else:
            dates = (dt.date.fromisoformat(row[field]),)
        if any(day > DEVELOPMENT_END for day in dates):
            raise ValueError(f"开发期补充审计拒绝读取验证期观测：{path.name}")
    return rows


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]], fields: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _replace_block(text: str, before_heading: str, body: str) -> str:
    block = f"{START_MARK}\n{body.rstrip()}\n{END_MARK}\n\n"
    if START_MARK in text:
        start = text.index(START_MARK)
        end = text.index(END_MARK, start) + len(END_MARK)
        return text[:start] + block.rstrip() + text[end:]
    if before_heading not in text:
        raise ValueError(f"原口径报告缺少章节：{before_heading}")
    return text.replace(before_heading, block + before_heading, 1)


def _reason_detail(engine: FeatureEngine, feature: str, day: dt.date,
                   bond_holidays: frozenset[dt.date]) -> str:
    if feature.startswith(("v2m_", "v3r1_")):
        version = "v2-M" if feature.startswith("v2m_") else "v3-R1"
        field = feature.split("_", 1)[1]
        raw = engine.inputs.scores.get((day, version), {}).get(field, "")
        if raw.startswith("待补"):
            return "评分维度待补；债市休市日" if day in bond_holidays else "评分维度待补"
    return ""


def audit_development(paths: StoragePaths) -> DevelopmentAuditResult:
    """以既有开发期观测值为准核对并分类缺值，只修改报告第3、4节。"""
    inputs = load_development_feature_inputs(paths, RUN_ID)
    engine = FeatureEngine.create(inputs)
    out = paths.reports_dir / "research"
    observed_rows = _read_dev_csv(out / "特征观测值.csv", "dates")
    candidate_rows = _read_dev_csv(out / "高位候选日分组.csv", "date")
    feature_names = tuple(engine.values(dt.date(2015, 10, 30)))
    duplicate_days = [row["date"] for row in candidate_rows
                      if row["category"] == "样本组" and len(row["target_starts"].split("|")) > 1]
    bond_holidays = load_holidays().bond_holidays
    cache: dict[dt.date, dict[str, Decimal | None]] = {}
    summary: dict[tuple[str, str], Counter[str]] = {}
    detail: list[dict[str, object]] = []
    for row in observed_rows:
        kind, key = row["kind"], row["key"]
        dates = tuple(dt.date.fromisoformat(value) for value in row["dates"].split("|") if value)
        for feature in feature_names:
            key_stats = summary.setdefault((feature, kind), Counter())
            key_stats["observations"] += 1
            good: list[Decimal] = []
            for day in dates:
                if day not in cache:
                    cache[day] = engine.values(day)
                values = cache[day]
                value = values[feature]
                quality = feature_quality(engine, feature, day, value)
                if value is not None:
                    good.append(value)
                    key_stats["effective_days"] += 1
                else:
                    key_stats["missing_days"] += 1
                    assert quality.primary_reason is not None
                    key_stats[f"primary_{quality.primary_reason}"] += 1
                    for reason in quality.all_reasons:
                        key_stats[f"all_{reason}"] += 1
                if quality.single_value_stage:
                    key_stats["single_value_days"] += 1
                if quality.missing or quality.single_value_stage:
                    detail.append({"observation": key, "kind": kind, "date": day.isoformat(),
                                   "feature": feature, "missing": "是" if quality.missing else "否",
                                   "primary_reason": quality.primary_reason or "",
                                   "all_reasons": "|".join(quality.all_reasons),
                                   "reason_detail": _reason_detail(engine, feature, day, bond_holidays),
                                   "single_value_stage": "是" if quality.single_value_stage else "否"})
            if len(good) != int(row[f"{feature}_effective_days"]):
                raise ValueError(f"既有观测有效日数不一致：{key} / {feature}")
            original = Decimal(row[feature]) if row[feature] else None
            recomputed = sum(good) / Decimal(len(good)) if good else None
            if original != recomputed:
                raise ValueError(f"既有观测特征值不一致：{key} / {feature}")
            if good:
                key_stats["effective_observations"] += 1
            else:
                key_stats["missing_observations"] += 1
    rows = []
    for feature in feature_names:
        record: dict[str, object] = {"feature": feature}
        for kind, prefix in (("样本组", "sample"), ("对照组", "control")):
            counts = summary.get((feature, kind), Counter())
            for field in ("observations", "effective_observations", "missing_observations",
                          "effective_days", "missing_days", "single_value_days"):
                record[f"{prefix}_{field}"] = counts[field]
            for reason in REASON_ORDER:
                record[f"{prefix}_primary_{reason}"] = counts[f"primary_{reason}"]
                record[f"{prefix}_all_{reason}"] = counts[f"all_{reason}"]
            if counts["missing_days"] != sum(counts[f"primary_{reason}"] for reason in REASON_ORDER):
                raise ValueError(f"缺值主因未能逐日归类：{feature} / {kind}")
        rows.append(record)
    summary_path = out / "特征缺值审计.csv"
    detail_path = out / "特征缺值明细.csv"
    _write_csv(summary_path, rows, tuple(rows[0]))
    _write_csv(detail_path, detail,
               ("observation", "kind", "date", "feature", "missing", "primary_reason",
                "all_reasons", "reason_detail", "single_value_stage"))
    report_path = out / "小周期波段高点前瞻特征分析报告.md"
    report = report_path.read_text(encoding="utf-8")
    # 新增内容只涉及开发期；旧报告第1、2节和验证期 CSV 保持不变。
    development_block = ("### 开发期补充审计\n\n"
                         f"同一高位候选日映射多个危险时段的日期有 {len(duplicate_days)} 个"
                         f"（日期：{'、'.join(duplicate_days) if duplicate_days else '无'}）。"
                         "样本观测按时段平均，重复映射在各时段分别计入。\n")
    report = _replace_block(report, "## 第4节 数据质量与局限", development_block)
    quality_block = ("### 逐特征缺值审计（开发期）\n\n"
                     "危险时段仅计算参与指数的最大跌幅；单指数时段未参与指数留空。"
                     f"共 {len(feature_names)} 项特征；`特征缺值审计.csv` 逐项列出样本与对照的有效"
                     "观测数、缺失观测数、有效日数和缺失日数。`特征缺值明细.csv` 保留每个"
                     "缺失日及单值阶段质量标记。缺失可同时有多个原因；互斥主因按"
                     "“序列未开始→窗口不足→缺口→单值阶段”优先归类。单值阶段若有数值，"
                     "只作质量标记，不计缺失。“缺口”包括评分字段不可用，不等同于原始"
                     "序列缺行；逐日明细的原因细节会标明评分维度待补及债市休市日。"
                     "按观测值逐日计数，因此同一天若属于两个"
                     "危险时段样本，会在两个观测内分别计数。\n")
    # 两处补充使用不同的标记，以便复算时幂等替换。
    quality_start, quality_end = "<!-- quality-audit:start -->", "<!-- quality-audit:end -->"
    quality_marked = f"{quality_start}\n{quality_block.rstrip()}\n{quality_end}\n\n"
    if quality_start in report:
        start = report.index(quality_start)
        end = report.index(quality_end, start) + len(quality_end)
        report = report[:start] + quality_marked.rstrip() + report[end:]
    else:
        report = report.replace("## 第5节 供负责人参考的观察（非结论）",
                                quality_marked + "## 第5节 供负责人参考的观察（非结论）", 1)
    report_path.write_text(report, encoding="utf-8")
    return DevelopmentAuditResult(report_path, summary_path, detail_path, len(duplicate_days), len(feature_names))
