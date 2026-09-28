"""阶段6 逐日历史回测引擎。

- 一次性加载数据集（market.load_market_series），逐个 NYSE 交易日按与单日评分相同的路径计算：
  market.raw_inputs_from_series（截断到基准日）→ snapshot.build_snapshot（记录 ETF 缺价）
  → prepare.score_versions（冻结的 v2m、v3r1）→ metrics.metric_values；不另写任何判定逻辑。
- 每日 flags：按实际参与计算的日期，记录类型、标的、日期与用途。
- 评分全部计算完成后，才计算结果标签与回调事件标签（labels、zigzag；隔离数据）。
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from market_risk import calendar as mcal
from market_risk.backtest.settings import BacktestConfig
from market_risk.config import DataDecision, Settings
from market_risk.data.market import MarketSeries, raw_inputs_from_series
from market_risk.data.snapshot import build_snapshot
from market_risk.metrics import metric_values
from market_risk.models import MarketSnapshot, ScoreResult
from market_risk.prepare import score_versions
from market_risk.storage.paths import StoragePaths

VERSIONS = ("v2-M", "v3-R1")

# 各版本价格数据的用途：(用途, 适用标的, 回看交易日数 k：基准日为 0，含 T−k)（2026-09-27 核实评分代码）
PRICE_USAGE: dict[str, tuple[tuple[str, tuple[str, ...], int], ...]] = {
    "v2-M": (("当日收盘比较", ("SPY", "QQQ", "RSP"), 0), ("MA20", ("SPY", "QQQ", "RSP"), 19),
             ("MA50", ("SPY", "QQQ", "RSP"), 49), ("MA200", ("SPY",), 199), ("20日最高收盘价", ("SPY",), 19),
             # 三环节：d1 为 T−20 至 T−2，Lc 取 d1 之前20个交易日（最早 T−40），d2 在 d1 与 T 之间
             ("三环节窗口", ("SPY", "QQQ", "RSP"), 40)),
    "v3-R1": (("当日收盘比较", ("SPY", "QQQ", "RSP"), 0), ("MA5", ("SPY", "QQQ", "RSP"), 4),
              ("MA20", ("SPY", "QQQ", "RSP"), 19), ("MA50", ("SPY", "QQQ", "RSP"), 49), ("MA200", ("SPY",), 199)),
}


@dataclass(frozen=True)
class DayResult:
    date: dt.date
    results: Mapping[str, ScoreResult]                 # 版本 → 结果
    flags: Mapping[str, tuple[str, ...]]               # 版本 → flags（类型|标的|日期|用途）
    metrics: Mapping[str, object]                      # metrics.metric_values
    alt_scores: Mapping[str, str] = field(default_factory=dict)   # 结果依赖数据源：另一来源价格下的分数


@dataclass
class BacktestResult:
    start: dt.date
    end: dt.date
    versions: tuple[str, ...]
    days: list[DayResult]
    runtime_seconds: float = 0.0


def trading_index(series: MarketSeries, symbols: Iterable[str]) -> tuple[list[dt.date], dict[dt.date, int]]:
    """NYSE 交易日序列（覆盖数据集中评分 ETF 的全部日期），用于按交易日计算回看范围。"""
    first = min(series.dates[s][0] for s in symbols)
    last = max(series.dates[s][-1] for s in symbols)
    days = mcal.stock_trading_days(first, last)
    return days, {d: i for i, d in enumerate(days)}


def day_flags(version: str, snap: MarketSnapshot, result: ScoreResult, series: MarketSeries,
              settings: Settings, cfg: BacktestConfig, idx: Mapping[dt.date, int],
              corrections: Sequence[tuple[str, dt.date]]) -> tuple[str, ...]:
    base = snap.refs.base_date
    flags: list[str] = []
    i = idx[base]
    for label, symbols, k in PRICE_USAGE[version]:
        for sym, d in corrections:
            if sym in symbols and d in idx and 0 <= i - idx[d] <= k:
                flags.append(f"使用人工修正值|{sym}|{d}|{label}")
        for sd in cfg.source_dependent:
            if sd.symbol in symbols and sd.date in idx and 0 <= i - idx[sd.date] <= k:
                flags.append(f"结果依赖数据源|{sd.symbol}|{sd.date}|{label}")
    for sym, day in snap.non_trading_prices:
        flags.append(f"价格序列含非交易日行|{sym}|{day}|评分回看窗口")
    for which, reading in (("当日广度", snap.breadth), ("T−5 广度", snap.breadth_t5)):
        if reading is None:
            continue
        for sym, end in cfg.single_value_phase_end.items():
            if reading.date <= end:
                flags.append(f"广度单值K线阶段|{sym}|{reading.date}|{which}")
    refs = snap.refs
    points = (("O1", refs.oas_o1), ("O6", refs.oas_o6_v3r1)) if version == "v3-R1" else \
        (("O1", refs.oas_o1_v2m), ("O6", refs.oas_o6_v2m))
    oas_rows = series.rows[settings.oas_series]
    for label, d in points:
        if d is not None and d in oas_rows and oas_rows[d]["source"] == "tradingview":
            flags.append(f"OAS来自TradingView长历史|{settings.oas_series}|{d}|{label}")
    flags += [f"维度待补|-|{base}|{dim.name}" for dim in result.dimensions if dim.score is None]
    return tuple(flags)


def score_label(result: ScoreResult) -> str:
    return str(result.total) if result.total is not None else f"{result.total_range[0]}–{result.total_range[1]}"


def run_backtest(
    series: MarketSeries, paths: StoragePaths, settings: Settings, cfg: BacktestConfig,
    start: dt.date | None = None, end: dt.date | None = None, versions: Sequence[str] = VERSIONS,
    corrections: Sequence[tuple[str, dt.date]] = (), progress: Callable[[int, int], None] | None = None,
    decisions: tuple[DataDecision, ...] = (),
) -> BacktestResult:
    """逐日计算 [start, end] 每个 NYSE 交易日的评分与指标（不计算标签）。

    start 默认为配置的回测起点；end 默认为评分用价格序列的最新日期。corrections：已批准人工修正的（标的, 日期）。
    """
    t0 = time.perf_counter()
    scored = tuple(settings.scored_symbols)
    days_all, idx = trading_index(series, scored)
    first = max(start or cfg.start, cfg.start)
    last_price = max(series.dates[s][-1] for s in scored)
    last = min(end or last_price, last_price)
    bases = [d for d in days_all if first <= d <= last]
    wanted = tuple(v for v in VERSIONS if v in versions)
    out: list[DayResult] = []
    for n, base in enumerate(bases, 1):
        raw = raw_inputs_from_series(series, paths, settings, base, decisions=decisions, revision_check=False)
        snap = build_snapshot(raw, scored, tuple(settings.reference_symbols[:2]),  # type: ignore[arg-type]
                              allow_missing_etfs=True)
        pair = dict(zip(VERSIONS, score_versions(snap), strict=True))
        results = {v: pair[v] for v in wanted}
        flags = {v: day_flags(v, snap, results[v], series, settings, cfg, idx, corrections) for v in wanted}
        alt = _alternative_scores(series, paths, settings, cfg, base, flags, wanted, decisions)
        completed = {r.symbol: r.completed for r in snap.three_segment[True]}
        metrics = metric_values(_snapshot_dict(snap), completed)
        out.append(DayResult(base, results, flags, metrics, alt))
        if progress is not None and n % 250 == 0:
            progress(n, len(bases))
    return BacktestResult(first, last, wanted, out, time.perf_counter() - t0)


def _alternative_scores(series: MarketSeries, paths: StoragePaths, settings: Settings, cfg: BacktestConfig,
                        base: dt.date, flags: Mapping[str, tuple[str, ...]], versions: Sequence[str],
                        decisions: tuple[DataDecision, ...] = (),
                        ) -> dict[str, str]:
    """结果依赖数据源：用另一来源的价格重算当日分数并保存（配置为空时不计算）。"""
    if not any(f.startswith("结果依赖数据源") for fs in flags.values() for f in fs):
        return {}
    alt = series
    for sd in cfg.source_dependent:
        alt = alt.replace_values(sd.symbol, {sd.date: sd.alternative})
    raw = raw_inputs_from_series(alt, paths, settings, base, decisions=decisions, revision_check=False)
    snap = build_snapshot(raw, tuple(settings.scored_symbols), tuple(settings.reference_symbols[:2]),  # type: ignore[arg-type]
                          allow_missing_etfs=True)
    pair = dict(zip(VERSIONS, score_versions(snap), strict=True))
    return {v: f"{[d.score for d in pair[v].dimensions]} 总分 {score_label(pair[v])}" for v in versions}


def _snapshot_dict(snap: MarketSnapshot) -> dict[str, object]:
    """从原始 Decimal 快照提取指标，不经过 JSON 浮点序列化。"""
    from market_risk.report import snapshot_metrics_input

    return snapshot_metrics_input(snap)


def price_decimal(series: MarketSeries, name: str) -> list[tuple[dt.date, Decimal]]:
    """标签与回调用的收盘价：按公布的两位小数读取（ROUND_HALF_UP，与结果标签相同）。"""
    from market_risk.outcomes import published

    return [(d, published(r["value"])) for d, r in sorted(series.rows[name].items()) if r["value"] is not None]
