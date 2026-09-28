"""市场数据集 data/market/（docs/decisions/0001-数据存储架构.md，STORAGE 2.1）。

- 每个序列一个 CSV（data/market/daily/<序列>.csv），提交 git；评分与逐日回测一律从这里读取，
  接口缓存（data/cache/）只作为 `data build` 的输入。
- 列：date, value, source；ETF 另含 open, high, low, close, volume（value = close）。
- 数值：Yahoo 价格四舍五入到4位小数；FRED、财政部、Cboe、TradingView、手工录入保存来源原值。
- 与上一版数据集相比，已有日期的数值被修订时列出修订清单，**不自动覆盖**（保留旧值），
  确认后用 `data build --accept-revisions` 更新。新下载中没有的旧日期保留（如 FRED 只提供 ICE 系列最近三年）。
- manifest.json 记录每个序列的来源、下载时间、行数、起止日期、sha256。
- ALFRED 基准日版本只为正式样本（月末样本、每日前瞻运行）生成：data/market/vintage/<序列>_<基准日>.csv。
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import io
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from market_risk import calendar as mcal
from market_risk.config import DataDecision, Settings
from market_risk.data.cache import NEW_YORK, Series, utc_now
from market_risk.models import BreadthReading, SourceInfo
from market_risk.storage.paths import StoragePaths

ETF_COLUMNS = ["value", "open", "high", "low", "close", "volume", "source"]
VALUE_COLUMNS = ["value", "source"]
PRICE_FIELDS = ("value", "open", "high", "low", "close")

# 各序列的起点（B1-1）：ETF 从上市日（yfinance 从最早数据开始返回）；VIXCLS、财政部从 1990 年；OAS 从 1996-12-31
ETF_START = dt.date(1990, 1, 1)
VIX_START = dt.date(1990, 1, 1)
TREASURY_START = dt.date(1990, 1, 1)
OAS_START = dt.date(1996, 12, 31)
VIX_CBOE = "VIX_CBOE"
TREASURY_SERIES = "UST10Y"
BREADTH_SERIES = ("S5FI", "S5TW")

# 修订判定（B1-4）：Yahoo 价格按4位小数比较，成交量按整数；其他来源按两位小数
ETF_DECIMALS = 4
VALUE_DECIMALS = 2
# 数据集只写入已完整收盘的交易日：收盘后留 15 分钟
CLOSE_BUFFER = dt.timedelta(minutes=15)

# scoring 读取窗口（与原先按基准日下载的区间一致）
DAILY_LOOKBACK_DAYS = 100
BREADTH_CONFLICT_DAYS = 15


class MarketDataError(RuntimeError):
    """数据集缺失或未覆盖基准日。"""


Row = dict[str, Any]


@dataclass
class NewSeries:
    """一次生成得到的某个序列（尚未与上一版合并）。"""

    name: str
    kind: str                                   # etf / value
    rows: dict[dt.date, Row]
    primary: str                                # 主来源：数值相同时来源改为主来源
    inputs: list[SourceInfo] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    frequency: str = "daily"                    # daily / weekly
    revisable: bool = False                     # 整体替换为最新下载（只用于 reference 序列）

    @property
    def columns(self) -> list[str]:
        return ETF_COLUMNS if self.kind == "etf" else VALUE_COLUMNS

    @property
    def decimals(self) -> int:
        return ETF_DECIMALS if self.kind == "etf" else VALUE_DECIMALS


@dataclass(frozen=True)
class Revision:
    series: str
    date: dt.date
    column: str
    old: Any
    new: Any


@dataclass
class BuildResult:
    series: dict[str, dict[str, Any]]           # 序列 → manifest 条目
    revisions: list[Revision]
    changed: list[str]                          # 内容有变化的序列
    notes: list[str]
    accepted: bool
    manifest_path: Any = None
    replaced: dict[str, dict[str, int]] = field(default_factory=dict)   # revisable 序列：修订、新增、删除的条数


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------


def _fmt(column: str, v: Any) -> str:
    if v is None:
        return ""
    if column == "source":
        return str(v)
    if column == "volume":
        return str(int(v))
    return repr(float(v))


def series_text(rows: Mapping[dt.date, Row], columns: list[str]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["date", *columns])
    for d in sorted(rows):
        writer.writerow([d.isoformat(), *(_fmt(c, rows[d].get(c)) for c in columns)])
    return out.getvalue()


def read_series_file(path: Any, *, exact: bool = False) -> tuple[list[str], dict[dt.date, Row]]:
    """读取数据集序列；计分或标签使用 exact 保留 CSV 的原始十进制文本。"""
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        columns = [c for c in reader.fieldnames or [] if c != "date"]
        rows: dict[dt.date, Row] = {}
        for r in reader:
            rows[dt.date.fromisoformat(r["date"])] = {
                c: (r[c] if c == "source" else ((Decimal(r[c]) if exact else float(r[c])) if r[c] else None))
                for c in columns}
    return columns, rows


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 合并与修订检查（纯函数）
# ---------------------------------------------------------------------------


def _same(column: str, a: Any, b: Any, decimals: int) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if column == "source":
        return a == b
    if column == "volume":
        return int(a) == int(b)
    return round(float(a), decimals) == round(float(b), decimals)


def merge_rows(
    new: NewSeries, old: Mapping[dt.date, Row] | None, accept_revisions: bool = False,
) -> tuple[dict[dt.date, Row], list[Revision]]:
    """与上一版合并：新日期直接加入；已有日期数值不同 → 修订（不接受时保留旧值）；
    新数据中没有的旧日期保留。数值相同时，来源只在新来源为主来源时更新。"""
    if not old:
        return dict(new.rows), []
    merged: dict[dt.date, Row] = dict(old)
    revisions: list[Revision] = []
    value_columns = [c for c in new.columns if c != "source"]
    for d, row in new.rows.items():
        prev = old.get(d)
        if prev is None:
            merged[d] = row
            continue
        diffs = [c for c in value_columns if not _same(c, prev.get(c), row.get(c), new.decimals)]
        if diffs:
            revisions += [Revision(new.name, d, c, prev.get(c), row.get(c)) for c in diffs]
            if accept_revisions:
                merged[d] = row
        elif prev.get("source") != row.get("source") and row.get("source") == new.primary:
            merged[d] = row
    return merged, revisions


def replace_rows(new: NewSeries, old: Mapping[dt.date, Row] | None) -> tuple[dict[dt.date, Row], dict[str, int]]:
    """revisable 序列：整体替换为最新下载的完整序列（不与旧版本逐日混合），统计本次修订条数。

    新下载为空（例如离线且无缓存）时保留上一版，不清空。
    """
    old = old or {}
    if not new.rows:
        return dict(old), {"revised": 0, "added": 0, "removed": 0}
    value_columns = [c for c in new.columns if c != "source"]
    revised = sum(1 for d, row in new.rows.items() if d in old
                  and any(not _same(c, old[d].get(c), row.get(c), new.decimals) for c in value_columns))
    return dict(new.rows), {"revised": revised, "added": len(set(new.rows) - set(old)),
                            "removed": len(set(old) - set(new.rows))}


def build_dataset(
    paths: StoragePaths,
    new_series: Iterable[NewSeries],
    accept_revisions: bool = False,
    breadth_conflicts: Mapping[dt.date, str] | None = None,
    now: dt.datetime | None = None,
    decisions: tuple[DataDecision, ...] = (),
) -> BuildResult:
    """把新数据与 data/market/ 的上一版合并并写入；有未接受的修订时保留旧值并列出清单。"""
    manifest = read_manifest(paths)
    entries: dict[str, dict[str, Any]] = dict(manifest.get("series", {}))
    revisions: list[Revision] = []
    changed: list[str] = []
    notes: list[str] = []
    stamp = (now or utc_now()).astimezone(dt.UTC).isoformat(timespec="seconds")
    from market_risk.data.corrections import apply_corrections, restore_originals

    # 全部预检后再写文件，裁定无效时不得留下已写入的部分序列。
    prepared = []
    replaced: dict[str, dict[str, int]] = {}
    incoming = list(new_series)
    rebuilding = {s.name for s in incoming}
    affected = {d.symbol for d in decisions if d.decision == "correct"}
    affected |= {name for name, entry in entries.items() if entry.get("corrections")}
    for name in sorted(affected - rebuilding):
        if name not in entries:
            raise MarketDataError(f"修正标的不存在：{name}")
        # 即使本次没有取得该序列的新缓存，也须在已有来源原值上应用/撤销裁定。
        incoming.append(NewSeries(name, entries[name]["kind"], {}, "",
                                  frequency=entries[name].get("frequency", "daily")))
    for s in incoming:
        path = paths.market_series_file(s.name, s.frequency)
        old = read_series_file(path)[1] if path.exists() else None
        prev = entries.get(s.name, {})
        if s.revisable:
            if any(d.symbol == s.name and d.decision == "correct" for d in decisions):
                raise MarketDataError(f"revisable 序列不能有人工修正：{s.name}")
            merged, replaced[s.name] = replace_rows(s, old)
            if s.rows:
                c = replaced[s.name]
                notes.append(f"{s.name}：来源会修订历史，已整体替换为最新下载的完整序列"
                             f"（本次修订 {c['revised']} 条，新增 {c['added']} 条，删除 {c['removed']} 条）")
            prepared.append((s, path, prev, merged, [], []))
            continue
        old = restore_originals(old or {}, prev.get("corrections", []))
        merged, revs = merge_rows(s, old, accept_revisions)
        merged, audit = apply_corrections(s.name, s.kind, merged, decisions)
        prepared.append((s, path, prev, merged, revs, audit))
    for s, path, prev, merged, revs, audit in prepared:
        revisions += revs
        notes += s.notes
        text = series_text(merged, s.columns)
        digest = sha256_text(text)
        if prev.get("sha256") != digest or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            changed.append(s.name)
        sources: dict[str, int] = {}
        for r in merged.values():
            sources[str(r.get("source"))] = sources.get(str(r.get("source")), 0) + 1
        downloaded = max((i.downloaded_at_utc for i in s.inputs if i.downloaded_at_utc), default=None)
        if not s.rows and prev:
            downloaded = prev.get("downloaded_at_utc")
        if downloaded is None and s.name in changed:
            downloaded = stamp          # 只有本地输入（TradingView、手工录入）：记录生成时间
        entries[s.name] = {
            "file": path.relative_to(paths.root).as_posix(),
            "kind": s.kind,
            "sources": dict(sorted(sources.items())),
            "downloaded_at_utc": downloaded if s.name in changed or not prev else prev.get("downloaded_at_utc"),
            "inputs": sorted({i.cache_file or i.url for i in s.inputs}) if (s.name in changed and s.inputs) or not prev
            else prev.get("inputs", []),
            "rows": len(merged),
            "first_date": min(merged).isoformat() if merged else None,
            "last_date": max(merged).isoformat() if merged else None,
            "sha256": digest,
            **({"frequency": s.frequency} if s.frequency != "daily" else {}),
            # revisable：本版本即最新下载的完整序列，downloaded_at_utc 为版本标识
            **({"revisable": True, "last_replacement": replaced[s.name]} if s.revisable else {}),
            **({"corrections": audit} if audit else {}),
        }
    new_manifest = {
        **manifest,
        "series": dict(sorted(entries.items())),
        "breadth_conflicts": {d.isoformat(): m for d, m in sorted((breadth_conflicts or {}).items())}
        if breadth_conflicts is not None else manifest.get("breadth_conflicts", {}),
    }
    if new_manifest != manifest:
        new_manifest["generated_at_utc"] = stamp
        write_manifest(paths, new_manifest)
    return BuildResult(entries, revisions, changed, notes, accept_revisions, paths.market_manifest, replaced)


def read_manifest(paths: StoragePaths) -> dict[str, Any]:
    if not paths.market_manifest.exists():
        return {}
    return json.loads(paths.market_manifest.read_text(encoding="utf-8"))


def write_manifest(paths: StoragePaths, manifest: Mapping[str, Any]) -> None:
    paths.market_manifest.parent.mkdir(parents=True, exist_ok=True)
    ordered = {k: manifest[k] for k in ("generated_at_utc", "series", "vintage", "breadth_conflicts")
               if k in manifest}
    ordered.update({k: v for k, v in manifest.items() if k not in ordered})
    paths.market_manifest.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def manifest_sha256(paths: StoragePaths) -> str | None:
    """数据集版本：manifest.json 的 sha256（写入运行目录 meta.json）。"""
    if not paths.market_manifest.exists():
        return None
    return hashlib.sha256(paths.market_manifest.read_bytes()).hexdigest()


def render_revisions(result: BuildResult, generated_at: str) -> str:
    lines = ["# 市场数据集历史修订清单", "",
             f"生成时间（UTC）：{generated_at}。与上一版 data/market/ 相比，已有日期的数值发生变化。",
             "",
             ("已按 `--accept-revisions` 更新为新值。" if result.accepted else
              "**未自动覆盖**：数据集保留旧值；确认后运行 `market-risk data build --accept-revisions` 更新。"),
             "", "| 序列 | 日期 | 列 | 旧值 | 新值 |", "|---|---|---|---|---|"]
    lines += [f"| {r.series} | {r.date} | {r.column} | {r.old} | {r.new} |" for r in result.revisions]
    if not result.revisions:
        lines.append("| 无 | | | | |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 收集新数据（接口缓存、TradingView 清洗结果、手工录入）
# ---------------------------------------------------------------------------


def last_completed_trading_day(now: dt.datetime | None = None) -> dt.date:
    """已完整收盘的最近一个交易日（美东；收盘后留 15 分钟，提前收盘日按 13:00）。"""
    local = (now or utc_now()).astimezone(NEW_YORK)
    day = local.date()
    if mcal.is_stock_trading_day(day):
        close = dt.time(13, 0) if mcal.is_early_close(day) else dt.time(16, 0)
        if local >= dt.datetime.combine(day, close, NEW_YORK) + CLOSE_BUFFER:
            return day
    d = day - dt.timedelta(days=1)
    while not mcal.is_stock_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def etf_series(name: str, rows: Mapping[dt.date, Mapping[str, float | None]], end: dt.date,
               inputs: list[SourceInfo]) -> NewSeries:
    out = {d: {**r, "value": r["close"], "source": "yahoo"} for d, r in rows.items() if d <= end}
    return NewSeries(name, "etf", out, "yahoo", inputs)


def value_series(name: str, values: Mapping[dt.date, float | None], source: str, end: dt.date,
                 inputs: list[SourceInfo], start: dt.date | None = None) -> NewSeries:
    out = {d: {"value": v, "source": source} for d, v in values.items()
           if d <= end and (start is None or d >= start)}
    return NewSeries(name, "value", out, source, inputs)


def vix_series(name: str, fred: Mapping[dt.date, float | None], cboe: Mapping[dt.date, float | None] | None,
               end: dt.date, inputs: list[SourceInfo]) -> NewSeries:
    """VIXCLS：FRED 原值；FRED 缺失（"." 或没有该日期）而 Cboe 有值时按 SPEC 5.6 第7条用 Cboe 补充，来源标为 cboe。"""
    rows: dict[dt.date, Row] = {d: {"value": v, "source": "fred"} for d, v in fred.items() if d <= end}
    for d, v in (cboe or {}).items():
        if d <= end and v is not None and (d not in rows or rows[d]["value"] is None) and d >= min(fred, default=d):
            rows[d] = {"value": v, "source": "cboe"}
    return NewSeries(name, "value", rows, "fred", inputs)


def oas_series(name: str, fred: Mapping[dt.date, float | None], tv: Mapping[dt.date, float] | None,
               end: dt.date, inputs: list[SourceInfo]) -> NewSeries:
    """OAS：FRED 原值；更早（早于 FRED 返回的第一个观测）的日期由 TradingView 补充（TRADINGVIEW 6.2）。"""
    rows: dict[dt.date, Row] = {d: {"value": v, "source": "fred"} for d, v in fred.items() if d <= end}
    first = min(fred) if fred else end + dt.timedelta(days=1)
    for d, v in (tv or {}).items():
        if OAS_START <= d < first and d <= end:
            rows[d] = {"value": v, "source": "tradingview"}
    return NewSeries(name, "value", rows, "fred", inputs)


def local_input(paths: StoragePaths, path: Any, source: str) -> SourceInfo:
    """本地输入文件（TradingView 清洗结果、手工录入）在 manifest 中的记录。"""
    rel = path.relative_to(paths.root).as_posix()
    return SourceInfo(source, path.stem, rel, "", 0, None, None, True, rel)


def breadth_series(readings: Mapping[dt.date, BreadthReading], end: dt.date,
                   paths: StoragePaths | None = None) -> list[NewSeries]:
    """S5FI、S5TW：TradingView 清洗结果优先，手工录入补充（SPEC 6.5）。"""
    out = []
    for name, attr in zip(BREADTH_SERIES, ("s5fi", "s5tw"), strict=True):
        rows = {d: {"value": getattr(r, attr), "source": r.source} for d, r in readings.items() if d <= end}
        inputs = [] if paths is None else [local_input(paths, paths.tv_processed_file(name), "tradingview"),
                                           local_input(paths, paths.breadth_csv, "manual")]
        out.append(NewSeries(name, "value", rows, "tradingview", inputs))
    return out


# ---------------------------------------------------------------------------
# 评分输入：从数据集组装 RawInputs
# ---------------------------------------------------------------------------


def _load(paths: StoragePaths, name: str) -> dict[dt.date, Row]:
    path = paths.market_daily_file(name)
    if not path.exists():
        raise MarketDataError(f"数据集缺少 {path.relative_to(paths.root).as_posix()}，请先运行 market-risk data build")
    return read_series_file(path, exact=True)[1]


def _window(rows: Mapping[dt.date, Row], start: dt.date, base: dt.date) -> dict[dt.date, Row]:
    """截断到基准日（含）：数据集包含基准日之后的数据，这里必须截断（防未来信息）。"""
    return {d: r for d, r in rows.items() if start <= d <= base}


def _source_info(paths: StoragePaths, manifest: Mapping[str, Any], name: str,
                 window: Mapping[dt.date, Row]) -> SourceInfo:
    entry = manifest.get("series", {}).get(name, {})
    rel = paths.market_daily_file(name).relative_to(paths.root).as_posix()
    return SourceInfo(source="market", key=name, url=rel, downloaded_at_utc=entry.get("downloaded_at_utc") or "",
                      rows=len(window), data_start=min(window) if window else None,
                      data_end=max(window) if window else None, from_cache=True, cache_file=rel)


@dataclass(frozen=True)
class MarketSeries:
    """一次性加载的数据集（各序列的全部行）；按基准日截取窗口时用二分查找，不重复读文件。

    只是读取缓冲：组装评分输入时必须经 raw_inputs_from_series 截断到基准日（含）。
    """

    rows: Mapping[str, Mapping[dt.date, Row]]
    dates: Mapping[str, tuple[dt.date, ...]]
    manifest: Mapping[str, Any]
    breadth: Mapping[dt.date, BreadthReading]

    def window(self, name: str, start: dt.date, end: dt.date) -> dict[dt.date, Row]:
        import bisect

        days = self.dates.get(name, ())
        lo, hi = bisect.bisect_left(days, start), bisect.bisect_right(days, end)
        rows = self.rows[name]
        return {d: rows[d] for d in days[lo:hi]}

    def replace_values(self, name: str, values: Mapping[dt.date, float | Decimal]) -> MarketSeries:
        """替换某序列若干日期的 value/close（用于比较两种价格来源的计分；不改动文件）。"""
        rows = {**self.rows[name]}
        for d, v in values.items():
            rows[d] = {**rows[d], "value": v, "close": v}
        return dataclasses.replace(self, rows={**self.rows, name: rows})


def load_market_series(paths: StoragePaths, settings: Settings) -> MarketSeries:
    """读取数据集中评分所需的全部序列（缺少必需序列时报错）。"""
    names = [*settings.scored_symbols, *settings.reference_symbols, settings.vix_series, TREASURY_SERIES,
             settings.oas_series, *BREADTH_SERIES]
    rows = {name: _load(paths, name) for name in names}
    if paths.market_daily_file(VIX_CBOE).exists():
        rows[VIX_CBOE] = _load(paths, VIX_CBOE)
    fi, tw = rows["S5FI"], rows["S5TW"]
    breadth = {}
    for d in sorted(set(fi) | set(tw)):
        a, b = fi.get(d), tw.get(d)
        f = None if a is None else a["value"]
        w = None if b is None else b["value"]
        if f is None and w is None:
            continue
        primary = a if f is not None else b
        breadth[d] = BreadthReading(d, f, w, primary["source"] or "manual",
                                    "TradingView 导出" if primary["source"] == "tradingview" else "")
    return MarketSeries(rows, {n: tuple(sorted(r)) for n, r in rows.items()}, read_manifest(paths), breadth)


def coverage_error(paths: StoragePaths, settings: Settings, base: dt.date,
                   series: MarketSeries | None = None) -> str | None:
    """数据集是否已更新到基准日（以评分 ETF 收盘价为准）；未覆盖时返回说明（提示先运行 fetch）。

    基准日晚于全部评分 ETF 的最新日期 → 未覆盖；已覆盖但某只 ETF 当日或回看窗口内缺数据，
    不在此报错，由快照记为 missing_etfs、价格维度记待补（阶段6，2026-09-27 确认）。
    """
    lasts = []
    for sym in settings.scored_symbols:
        if series is not None:
            rows: Mapping[dt.date, Row] | None = series.rows.get(sym)
        else:
            path = paths.market_daily_file(sym)
            rows = read_series_file(path)[1] if path.exists() else None
        if rows is None:
            return f"数据集缺少 {sym}"
        if rows:
            lasts.append(max(rows))
    latest = max(lasts, default=None)
    if latest is None or base > latest:
        return f"数据集未覆盖基准日 {base}（评分 ETF 最新日期 {latest}）"
    return None


def load_raw_inputs(
    paths: StoragePaths, settings: Settings, base: dt.date, mode: str = "backtest",
    decisions: tuple = (), revision_check: bool | None = None,
) -> Any:
    """从 data/market/ 组装基准日的 RawInputs（窗口与原先按基准日下载的区间相同）。"""
    err = coverage_error(paths, settings, base)
    if err:
        raise MarketDataError(f"{err}，请先运行 market-risk fetch --date {base}（或 data build）")
    return raw_inputs_from_series(load_market_series(paths, settings), paths, settings, base, mode,
                                  decisions, revision_check)


def raw_inputs_from_series(
    series: MarketSeries, paths: StoragePaths, settings: Settings, base: dt.date, mode: str = "backtest",
    decisions: tuple = (), revision_check: bool | None = None,
) -> Any:
    """由已加载的数据集组装基准日的 RawInputs：所有序列截断到基准日（含）。单日评分与回测共用。"""
    from market_risk.data import fred
    from market_risk.data.snapshot import RawInputs
    from market_risk.precision import published_price

    manifest = series.manifest
    start = base - dt.timedelta(days=DAILY_LOOKBACK_DAYS)
    sources: list[SourceInfo] = []
    notes: list[str] = []

    closes: dict[str, Series] = {}
    close_start = base - dt.timedelta(days=settings.lookback_calendar_days)
    for sym in (*settings.scored_symbols, *settings.reference_symbols):
        w = series.window(sym, close_start, base)
        closes[sym] = {d: published_price(r["value"]) for d, r in w.items() if r["value"] is not None}
        sources.append(_source_info(paths, manifest, sym, w))

    vix_w = series.window(settings.vix_series, start, base)
    vix_fred: Series = {d: r["value"] for d, r in vix_w.items() if r["source"] == "fred"}
    sources.append(_source_info(paths, manifest, settings.vix_series, vix_w))
    vix_cboe: Series | None = None
    if VIX_CBOE in series.rows:
        cboe_w = series.window(VIX_CBOE, start, base)
        vix_cboe = {d: r["value"] for d, r in cboe_w.items()}
        sources.append(_source_info(paths, manifest, VIX_CBOE, cboe_w))
    else:
        notes.append("数据集没有 Cboe VIX 备用源，只使用 FRED VIXCLS")

    t_w = series.window(TREASURY_SERIES, start, base)
    treasury = {d: r["value"] for d, r in t_w.items() if r["value"] is not None}
    if any(r["source"] != "treasury" for r in t_w.values()):
        notes.append("财政部收益率主源获取失败的日期改用备用源 FRED DGS10（见数据集 source 列）")
    sources.append(_source_info(paths, manifest, TREASURY_SERIES, t_w))

    oas_w = series.window(settings.oas_series, start, base)
    oas: Series = {d: r["value"] for d, r in oas_w.items()}
    sources.append(_source_info(paths, manifest, settings.oas_series, oas_w))
    fred_oas = {d: r["value"] for d, r in oas_w.items() if r["source"] == "fred"}
    tv_dates = sorted(d for d, r in oas_w.items() if r["source"] == "tradingview")
    if tv_dates:
        ice = fred.ice_history_note(settings.oas_series, start, fred_oas) if fred_oas else (
            f"FRED 未返回 {settings.oas_series} 在 {start} 至 {base} 的观测（FRED 只提供 ICE 系列最近三年）")
        if ice:
            notes.append(ice)
        notes.append(f"OAS {tv_dates[0]} 至 {tv_dates[-1]} 共 {len(tv_dates)} 个观测来自 TradingView 导出数据"
                     "（FRED API 只提供最近三年；已按 oas.long_history_source=tradingview 使用）")

    check = settings.oas_revision_check if revision_check is None else revision_check
    oas_vintage: Series | None = None
    vintage_file = paths.market_vintage_file(settings.oas_series, base)
    if check and vintage_file.exists():
        v_rows = read_series_file(vintage_file)[1]
        oas_vintage = {d: r["value"] for d, r in v_rows.items() if d <= base}
        sources.append(SourceInfo("market", f"{settings.oas_series}_vintage{base:%Y%m%d}",
                                  vintage_file.relative_to(paths.root).as_posix(),
                                  manifest.get("vintage", {}).get(vintage_file.name, {}).get("downloaded_at_utc", ""),
                                  len(oas_vintage), min(oas_vintage, default=None), max(oas_vintage, default=None),
                                  True, vintage_file.relative_to(paths.root).as_posix()))
    elif check and not fred_oas:
        notes.append("FRED API 没有该区间的 OAS（早于三年），不做历史修订比对；不影响计分")

    breadth = {d: r for d, r in series.breadth.items() if d <= base}
    for d, msg in sorted(manifest.get("breadth_conflicts", {}).items()):
        day = dt.date.fromisoformat(d)
        if base - dt.timedelta(days=BREADTH_CONFLICT_DAYS) <= day <= base:
            notes.append(msg)

    return RawInputs(
        base_date=base, closes=closes, vix_fred=vix_fred, vix_cboe=vix_cboe, treasury=treasury,
        treasury_coverage_start=start, oas=oas, oas_vintage=oas_vintage, breadth=breadth,
        sources=tuple(sources), notes=tuple(notes), mode=mode, decisions=decisions,
        oas_symbol=settings.oas_series,
    )


def load_breadth_readings(paths: StoragePaths, base: dt.date | None = None) -> dict[dt.date, BreadthReading]:
    """S5FI、S5TW 都有数值的日期组成读数（截断到基准日）。"""
    fi = {d: r for d, r in _load(paths, "S5FI").items() if base is None or d <= base}
    tw = {d: r for d, r in _load(paths, "S5TW").items() if base is None or d <= base}
    out = {}
    for d in sorted(set(fi) & set(tw)):
        a, b = fi[d], tw[d]
        if a["value"] is None or b["value"] is None:
            continue
        out[d] = BreadthReading(d, a["value"], b["value"], a["source"] or "manual",
                                "TradingView 导出" if a["source"] == "tradingview" else "")
    return out


def write_vintage(paths: StoragePaths, series: str, base: dt.date, values: Mapping[dt.date, float | None],
                  info: SourceInfo | None) -> None:
    """保存 ALFRED 基准日版本（只为正式样本生成），并登记到 manifest 的 vintage 部分。"""
    path = paths.market_vintage_file(series, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = series_text({d: {"value": v, "source": "alfred"} for d, v in values.items() if d <= base},
                       VALUE_COLUMNS)
    path.write_text(text, encoding="utf-8")
    manifest = read_manifest(paths)
    vintage = dict(manifest.get("vintage", {}))
    vintage[path.name] = {
        "file": path.relative_to(paths.root).as_posix(), "series": series, "base_date": base.isoformat(),
        "downloaded_at_utc": info.downloaded_at_utc if info else None, "rows": len(values),
        "sha256": sha256_text(text),
    }
    if manifest.get("vintage", {}).get(path.name) != vintage[path.name]:
        manifest["vintage"] = dict(sorted(vintage.items()))
        manifest["generated_at_utc"] = utc_now().isoformat(timespec="seconds")
        write_manifest(paths, manifest)


FetchAll = Callable[..., list[NewSeries]]
