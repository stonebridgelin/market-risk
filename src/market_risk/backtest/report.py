"""基础统计报告 reports/backtest_baseline.md（阶段6）。

只使用开发期与验证期；只描述事实，不计算命中率、提前量、覆盖率、过早解除等评估指标（留给阶段6.5）。
读取回测运行目录中的文件（含隔离的标签文件）；本模块不属于评分路径。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from market_risk.backtest.settings import DEVELOPMENT, VALIDATION, BacktestConfig

VERSIONS = ("v2-M", "v3-R1")
DIMS = (("price", "价格"), ("breadth", "广度"), ("vix", "VIX"), ("rates", "利率"), ("credit", "信用"))
STAGES = ("早期信号", "中期确认信号", "高风险", "范围跨越阶段")


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _pct(n: int, total: int) -> str:
    return "-" if not total else f"{n / total * 100:.1f}%"


def render_baseline(meta: Mapping[str, Any], cfg: BacktestConfig, scores: Sequence[Mapping[str, str]],
                    episodes: Sequence[Mapping[str, str]]) -> str:
    periods = {DEVELOPMENT: cfg.development, VALIDATION: cfg.validation}
    in_scope = [r for r in scores if cfg.period_of(_d(r["date"])) in periods]
    by_version: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for r in in_scope:
        by_version[r["version"]].append(r)
    versions = [v for v in VERSIONS if v in by_version]
    lines = ["# 逐日历史回测：基础统计", "",
             f"运行：`{meta['run_id']}`；区间 {meta['start']} 至 {meta['end']}（{meta['days']} 个交易日）；"
             f"耗时 {meta['runtime_seconds']} 秒；代码 {str(meta['git_commit'])[:7]}；"
             f"数据集 manifest sha256 {str(meta['market_manifest_sha256'])[:12]}…",
             "", f"起点说明：{meta.get('start_reason', '')}。",
             "", f"本报告只使用开发期（{cfg.development[0]} 至 {cfg.development[1]}）与验证期"
                 f"（{cfg.validation[0]} 至 {cfg.validation[1]}）；保留期（{cfg.holdout_start} 起）不参与任何统计。"
                 "只描述事实，不计算命中率、提前量、覆盖率、过早解除等评估指标。", ""]

    # ---- 1. 覆盖情况 ----
    lines += ["## 1. 覆盖情况", ""]
    lines += ["| 版本 | 区间 | 交易日 | " + " | ".join(f"{n}待补" for _, n in DIMS) + " |",
              "|---|---|---|" + "---|" * len(DIMS)]
    for v in versions:
        for p in periods:
            rows = [r for r in by_version[v] if cfg.period_of(_d(r["date"])) == p]
            cells = [_pct(sum(r[k].startswith("待补") for r in rows), len(rows)) for k, _ in DIMS]
            lines.append(f"| {v} | {p} | {len(rows)} | " + " | ".join(cells) + " |")
    lines += ["", "flags（有该类标记的天数；一天可有多类）：", "",
              "| 版本 | flag 类型 | 开发期 | 验证期 |", "|---|---|---|---|"]
    for v in versions:
        counts: dict[str, Counter] = defaultdict(Counter)
        for r in by_version[v]:
            kinds = {f.split("|", 1)[0] for f in r["flags"].split("；") if f}
            for k in kinds:
                counts[k][cfg.period_of(_d(r["date"]))] += 1
        for k in sorted(counts):
            lines.append(f"| {v} | {k} | {counts[k][DEVELOPMENT]} | {counts[k][VALIDATION]} |")

    # ---- 2. 分数分布与阶段（按年份）----
    lines += ["", "## 2. 分数分布、阶段、明确恶化与总分≥3（按年份）", "",
              "总分≥3 按 SOP 9.4：总分下限≥3 为\"是\"，上限<3 为\"否\"，其余为\"未知\"（单独计数）。"
              "明确恶化的\"无法核验\"单独计数。", ""]
    for v in versions:
        lines += [f"### {v}", "",
                  "| 年份 | 交易日 | " + " | ".join(f"总分{i}" for i in range(11)) + " | 总分为范围 | "
                  + " | ".join(STAGES) + " | 明确恶化 是 | 明确恶化 无法核验 | 总分≥3 是 | 总分≥3 未知 |",
                  "|---|---|" + "---|" * (11 + 1 + len(STAGES) + 4)]
        years: dict[int, list[Mapping[str, str]]] = defaultdict(list)
        for r in by_version[v]:
            years[_d(r["date"]).year].append(r)
        for y in sorted(years):
            rows = years[y]
            totals = Counter(r["total"] for r in rows)
            stages = Counter(r["stage"] for r in rows)
            lines.append(
                f"| {y} | {len(rows)} | " + " | ".join(str(totals[str(i)]) for i in range(11))
                + f" | {totals['']} | " + " | ".join(str(stages[s]) for s in STAGES)
                + f" | {sum(r['clear_deterioration'] == '是' for r in rows)}"
                + f" | {sum(r['clear_deterioration'] == '无法核验' for r in rows)}"
                + f" | {sum(r['alert'] == '是' for r in rows)} | {sum(r['alert'] == '未知' for r in rows)} |")
        lines.append("")

    # ---- 3. 两个版本的差异 ----
    if len(versions) == 2:
        a = {r["date"]: r for r in by_version[versions[0]]}
        b = {r["date"]: r for r in by_version[versions[1]]}
        common = sorted(set(a) & set(b))
        diff_total = [d for d in common if (a[d]["total_min"], a[d]["total_max"]) != (b[d]["total_min"],
                                                                                   b[d]["total_max"])]
        lines += ["## 3. 两个版本分数不同的天数", "",
                  f"共同交易日 {len(common)} 天，总分（或范围）不同 {len(diff_total)} 天。"
                  "按维度（分数或待补范围不同的天数）：", "",
                  "| 维度 | 开发期 | 验证期 |", "|---|---|---|"]
        for k, n in DIMS:
            c = Counter(cfg.period_of(_d(d)) for d in common if a[d][k] != b[d][k])
            lines.append(f"| {n} | {c[DEVELOPMENT]} | {c[VALIDATION]} |")
        lines.append("")

    # ---- 4. 回调事件清单 ----
    lines += ["## 4. 回调事件清单（开发期、验证期）", "",
              "按高点日期归属区间。同一段下跌可同时出现在多个层级（有意设计，不去重）。"
              "计入统计的是已确认、未跨越区间边界的回调；"
              "跨越区间边界、起点之前开始、未确认、跨入保留期的回调单独列出。", ""]
    groups: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for e in episodes:
        groups[e["symbol"], e["level"]].append(e)
    header = "| 区间 | 高点日 | 高点收盘 | 低点日 | 低点收盘 | 跌幅 | 交易日数 | 分级 | 收复前高 |"
    for (sym, level), eps in sorted(groups.items(), key=lambda kv: (kv[0][0] != "SPX", kv[0][0],
                                                                     float(kv[0][1]))):
        counted = [e for e in eps if e["counted"] == "是"]
        lines += [f"### {sym} {level}% 层级（计入统计 {len(counted)} 段）", "", header,
                  "|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {e['period']} | {e['high_date']} | {e['high_close']} | {e['low_date']} | {e['low_close']} | "
                  f"{e['drawdown_pct']}% | {e['trading_days']} | {e['grade']} | "
                  f"{e['recovery_date'] or e['recovery_note']} |"
                  for e in counted] or ["| 无 | | | | | | | | |"]
        other = [e for e in eps if e["counted"] != "是" and e["period"] in (DEVELOPMENT, VALIDATION, "起点之前")]
        if other:
            lines += ["", "单独列出（不计入统计）：", "", "| 情况 | 区间 | 高点日 | 低点日 | 跌幅 | 状态 |",
                      "|---|---|---|---|---|---|"]
            for e in other:
                why = ("起点之前开始" if e["before_start"] == "是" else "跨越区间边界" if e["crosses_boundary"] == "是"
                       else e["status"])
                lines.append(f"| {why} | {e['period']} | {e['high_date']} | {e['low_date'] or '-'} | "
                             f"{e['drawdown_pct'] + '%' if e['drawdown_pct'] else '-'} | {e['status']} |")
        lines.append("")

    # ---- 5. 回调期间的分数概况 ----
    idx: dict[tuple[str, str], Mapping[str, str]] = {(r["date"], r["version"]): r for r in in_scope}
    lines += ["## 5. 回调期间的分数概况（只描述，不评估）", "",
              "区间为高点日至低点日（含两端）。最高总分取总分确定的日子；总分≥3 占比按 SOP 9.4 为\"是\"的天数计，"
              "\"未知\"另列。", ""]
    for (sym, level), eps in sorted(groups.items(), key=lambda kv: (kv[0][0] != "SPX", kv[0][0],
                                                                     float(kv[0][1]))):
        counted = [e for e in eps if e["counted"] == "是"]
        if not counted:
            continue
        head = "| 高点日 | 低点日 | 跌幅 | 分级 | 天数 | " + " | ".join(
            f"{v} 最高总分 | {v} 总分≥3占比 | {v} 未知" for v in versions) + " |"
        lines += [f"### {sym} {level}% 层级", "", head, "|---|---|---|---|---|" + "---|---|---|" * len(versions)]
        for e in counted:
            days = sorted({d for (d, _) in idx if e["high_date"] <= d <= e["low_date"]})
            cells = []
            for v in versions:
                rows = [idx[d, v] for d in days if (d, v) in idx]
                totals = [int(r["total"]) for r in rows if r["total"]]
                cells += [str(max(totals)) if totals else "-", _pct(sum(r["alert"] == "是" for r in rows), len(rows)),
                          str(sum(r["alert"] == "未知" for r in rows))]
            lines.append(f"| {e['high_date']} | {e['low_date']} | {e['drawdown_pct']}% | {e['grade']} | {len(days)} | "
                         + " | ".join(cells) + " |")
        lines.append("")

    # ---- 6. 标普500 5% 层级每年的回调次数 ----
    spx5 = [e for e in episodes if e["symbol"] == "SPX" and e["level"] == "5"]
    lines += ["## 6. 标普500 5% 层级每年的回调次数（开发期、验证期，按高点年份）", "",
              "只列事实，不下结论。计入统计的为已确认、未跨越区间边界的回调；跨越边界与起点之前开始的另列。", "",
              "| 年份 | 计入统计的回调次数 | 另列（跨越边界、起点之前开始等） |", "|---|---|---|"]
    per_year = Counter(_d(e["high_date"]).year for e in spx5 if e["counted"] == "是")
    other_year = Counter(_d(e["high_date"]).year for e in spx5
                         if e["counted"] != "是" and e["period"] in (DEVELOPMENT, VALIDATION))
    for y in range(cfg.development[0].year, cfg.validation[1].year + 1):
        lines.append(f"| {y} | {per_year[y]} | {other_year[y]} |")
    return "\n".join(lines) + "\n"
