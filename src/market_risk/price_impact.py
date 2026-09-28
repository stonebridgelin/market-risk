"""争议收盘价的实质影响检验（第二部分第1项）。

对一个争议日期，分别用两种来源的收盘价，重新计算"用到该价格"的全部基准日的 v2-M 与 v3-R1 分数：
范围为争议日至之后第 199 个交易日（MA200 的回看长度最长，覆盖当日收盘比较、各均线、20日最高收盘价与三环节窗口）。
比较每个维度的分数、可能取值与待补状态；全部相同记为"无实质影响"。

评分走与单日评分相同的路径（market.raw_inputs_from_series → snapshot → scoring），输入截断到基准日（含）；
本模块只调用评分代码，评分代码不引用本模块。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from market_risk import calendar as mcal
from market_risk.config import DataDecision, Settings
from market_risk.data.market import MarketSeries, raw_inputs_from_series
from market_risk.data.snapshot import build_snapshot
from market_risk.models import ScoreResult
from market_risk.prepare import score_versions as prepared_score_versions
from market_risk.storage.paths import StoragePaths

MA_LOOKBACK_SESSIONS = 199          # MA200：基准日及之前 199 个交易日


@dataclass(frozen=True)
class DimensionDiff:
    base_date: dt.date
    version: str
    dimension: str
    score_a: int | None
    score_b: int | None
    possible_a: tuple[int, ...]
    possible_b: tuple[int, ...]


@dataclass
class ImpactResult:
    symbol: str
    date: dt.date
    value_a: float                 # 来源 A（Yahoo 原值）
    value_b: float                 # 来源 B（TradingView）
    first_base: dt.date
    last_base: dt.date
    checked: int = 0
    diffs: list[DimensionDiff] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def material(self) -> bool:
        return bool(self.diffs)

    @property
    def affected_dates(self) -> list[dt.date]:
        return sorted({d.base_date for d in self.diffs})

    @property
    def verdict(self) -> str:
        if self.errors:
            return "无法检验（部分）" if self.checked else "无法检验"
        return "有实质影响" if self.material else "无实质影响"


def score_versions(raw: object) -> tuple[ScoreResult, ScoreResult]:
    snap = build_snapshot(raw, allow_missing_etfs=True)  # type: ignore[arg-type]
    return prepared_score_versions(snap)


def _dims(result: ScoreResult) -> dict[str, tuple[int | None, tuple[int, ...]]]:
    return {d.name: (d.score, tuple(d.possible_scores)) for d in result.dimensions}


def compare_scores(base: dt.date, a: Sequence[ScoreResult], b: Sequence[ScoreResult]) -> list[DimensionDiff]:
    """逐版本、逐维度比较分数与可能取值（待补时 score 为空，可能取值即待补范围）。"""
    out = []
    for ra, rb in zip(a, b, strict=True):
        da, db = _dims(ra), _dims(rb)
        for name in da:
            if da[name] != db[name]:
                out.append(DimensionDiff(base, ra.version, name, da[name][0], db[name][0], da[name][1], db[name][1]))
    return out


def dispute_impact(
    series: MarketSeries, paths: StoragePaths, settings: Settings, symbol: str, day: dt.date,
    value_a: float, value_b: float, horizon: int = MA_LOOKBACK_SESSIONS,
    scorer: Callable[[object], Sequence[ScoreResult]] = score_versions,
    decisions: tuple[DataDecision, ...] = (),
) -> ImpactResult:
    """series 中该标的该日的收盘价分别替换为 value_a、value_b，比较受影响基准日的评分。"""
    last_available = series.dates[symbol][-1]
    bases = [d for d in mcal.stock_trading_days(day, mcal.shift_trading_days(day, horizon)) if d <= last_available]
    res = ImpactResult(symbol, day, value_a, value_b, bases[0], bases[-1])
    series_a = series.replace_values(symbol, {day: value_a})
    series_b = series.replace_values(symbol, {day: value_b})
    for base in bases:
        try:
            a = scorer(raw_inputs_from_series(series_a, paths, settings, base, decisions=decisions,
                                              revision_check=False))
            b = scorer(raw_inputs_from_series(series_b, paths, settings, base, decisions=decisions,
                                              revision_check=False))
        except Exception as exc:  # 个别基准日无法计分时记录原因，不中断其他日期
            res.errors.append(f"{base}：{type(exc).__name__}: {exc}")
            continue
        res.checked += 1
        res.diffs += compare_scores(base, a, b)
    return res


def render_impact(results: Sequence[ImpactResult], label_a: str = "Yahoo", label_b: str = "TradingView") -> list[str]:
    lines = ["## 实质影响检验", "",
             f"对每个争议日期，分别用 {label_a}（来源原值）与 {label_b} 的收盘价，重新计算争议日至之后第 "
             f"{MA_LOOKBACK_SESSIONS} 个交易日（MA200 的回看长度）每个基准日的 v2-M 与 v3-R1 分数，"
             "比较每个维度的分数、可能取值与待补状态。评分路径与单日评分相同，输入截断到基准日（含）。", "",
             f"| 标的 | 日期 | {label_a} | {label_b} | 基准日范围 | 检验天数 | 结论 | 受影响基准日 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.symbol} | {r.date} | {r.value_a:.2f} | {r.value_b:.2f} | {r.first_base} 至 {r.last_base} | "
                     f"{r.checked} | {r.verdict} | {len(r.affected_dates)} |")
    for r in results:
        if not (r.diffs or r.errors):
            continue
        lines += ["", f"### {r.symbol} {r.date}", ""]
        if r.diffs:
            lines += ["| 基准日 | 版本 | 维度 | " + label_a + " 分数（可能取值） | " + label_b + " 分数（可能取值） |",
                      "|---|---|---|---|---|"]
            for d in r.diffs:
                def fmt(score: int | None, possible: tuple[int, ...]) -> str:
                    return f"{score}" if score is not None else f"待补 {list(possible)}"
                lines.append(f"| {d.base_date} | {d.version} | {d.dimension} | {fmt(d.score_a, d.possible_a)} | "
                             f"{fmt(d.score_b, d.possible_b)} |")
        if r.errors:
            lines += ["", f"无法计分的基准日 {len(r.errors)} 个：" + "；".join(r.errors[:5])
                      + (" 等" if len(r.errors) > 5 else "")]
    return lines
