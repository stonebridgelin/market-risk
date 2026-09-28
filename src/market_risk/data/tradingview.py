"""TradingView 导出数据的读取、校验与合并（docs/TRADINGVIEW.md 第3、5节）。

- 原始文件只读：data/manual/tradingview/raw/<导出日期>/，保留 TradingView 默认文件名。
- 清洗结果另存：data/processed/tradingview/<标的>.csv，可随时由原始文件重建。
- 日期换算（第5.1节）：不能统一转成 UTC 再取日期。
  ISO 时间直接取其时区偏移下的本地日期；UNIX 时间戳按该标的的时区（默认 America/New_York）换算后取日期。
- 路径一律由 storage/paths.py 生成。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from market_risk import calendar as mcal
from market_risk.config import DataDecision, SymbolInfo
from market_risk.storage.paths import StoragePaths

NEW_YORK = ZoneInfo("America/New_York")
TOLERANCE = 0.005
MARKET_CLOSE = dt.time(16, 0)
HISTORY_GAP_DAYS = 10
MAX_LISTED = 8

PASSED, WARNING, FAILED = "passed", "warning", "failed"
STATUS_TEXT = {PASSED: "通过", WARNING: "警告", FAILED: "失败"}
_LEVEL_ORDER = {"info": 0, WARNING: 1, FAILED: 2}

MANIFEST_FIELDS = [
    "file_path", "export_date", "export_time", "tv_symbol", "symbol", "timeframe", "first_date", "last_date",
    "rows", "time_format", "sha256", "imported_at", "validation_status", "notes",
]

# 文件名：<交易所>_<代码>, <周期>[_随机后缀][ (n)].csv，如 "INDEX_S5FI, 1D.csv"
_FILENAME_RE = re.compile(
    r"^(?P<prefix>[^,]+),\s*(?P<tf>[0-9]*[A-Za-z]+)(?:_[0-9A-Za-z]+)?(?:\s*\(\d+\))?\.csv$"
)
_DAILY = {"1D", "D"}
# 延迟数据源前缀：如 ICE_DLY、CBOE_DLY、SP_DLY、NASDAQ_DLY（通用规则，不逐个硬编码）
_DELAYED_PREFIX_RE = re.compile(r"^([A-Za-z0-9]+_DLY)_(.+)$", re.IGNORECASE)
_BASE_COLUMNS = {"open", "high", "low", "close"}


class TradingViewError(ValueError):
    """文件无法解析（格式、文件名）。"""


@dataclass(frozen=True)
class Issue:
    level: str       # info / warning / failed
    message: str


@dataclass(frozen=True)
class Bar:
    date: dt.date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    extras: tuple[tuple[str, float | None], ...] = ()


@dataclass
class FileReport:
    """一个原始文件的读取与校验结果。"""

    file_path: str
    file_name: str
    export_date: dt.date
    tv_symbol: str = ""
    symbol: str = ""
    timeframe: str = ""
    time_format: str = ""
    sha256: str = ""
    bars: dict[dt.date, Bar] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def status(self) -> str:
        levels = {i.level for i in self.issues}
        if FAILED in levels:
            return FAILED
        return WARNING if WARNING in levels else PASSED

    @property
    def first_date(self) -> dt.date | None:
        return min(self.bars) if self.bars else None

    @property
    def last_date(self) -> dt.date | None:
        return max(self.bars) if self.bars else None

    def add(self, level: str, message: str) -> None:
        self.issues.append(Issue(level, message))


# ---------------------------------------------------------------------------
# 文件名与时间
# ---------------------------------------------------------------------------


def parse_filename(name: str, symbols: Mapping[str, SymbolInfo]) -> tuple[str, str]:
    """从文件名解析 (tv_symbol, 周期)。INDEX_S5FI → INDEX:S5FI。

    延迟数据源前缀（交易所_DLY）整体作为数据源：ICE_DLY_MOVE → ICE_DLY:MOVE、CBOE_DLY_VIX3M → CBOE_DLY:VIX3M。
    """
    m = _FILENAME_RE.match(name)
    if not m:
        raise TradingViewError(
            f"无法解析文件名 {name!r}：应为 TradingView 默认文件名，如 \"INDEX_S5FI, 1D.csv\""
        )
    prefix, tf = m.group("prefix").strip(), m.group("tf").upper()
    for info in symbols.values():
        if prefix in info.filename_aliases:
            return info.tv_symbol, tf
    if "_" not in prefix:
        raise TradingViewError(
            f"文件名 {name!r} 中没有\"交易所_代码\"：请在 config/symbols.yaml 的 filename_aliases 中登记"
        )
    delayed = _DELAYED_PREFIX_RE.match(prefix)
    exchange, code = (delayed.group(1), delayed.group(2)) if delayed else prefix.split("_", 1)
    return f"{exchange}:{code}".upper(), tf


def parse_time(value: str, tz: ZoneInfo) -> tuple[dt.date, str, dt.datetime | None]:
    """把时间列换算为日期，返回 (日期, 格式, 本地时刻)。

    - ISO：直接取时区偏移下的本地日期（不转 UTC）；
    - UNIX：按标的时区换算后取日期。
    """
    v = value.strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        ts = float(v)
        if abs(ts) > 1e11:  # 毫秒
            ts /= 1000
        local = dt.datetime.fromtimestamp(ts, tz)
        return local.date(), "unix", local
    try:
        parsed = dt.datetime.fromisoformat(v)
    except ValueError as exc:
        raise TradingViewError(f"无法识别的时间：{value!r}") from exc
    return parsed.date(), "iso", parsed


def _number(v: str | None) -> float | None:
    if v is None:
        return None
    s = v.strip()
    if s == "" or s.lower() in {"nan", "null", "none"}:
        return None
    x = float(s)
    return None if math.isnan(x) else x


def _extra_name(raw: str, used: set[str]) -> str:
    base = "extra_" + (re.sub(r"\W+", "_", raw.strip(), flags=re.ASCII).strip("_").lower() or "col")
    name, n = base, 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


# ---------------------------------------------------------------------------
# 读取与校验单个文件
# ---------------------------------------------------------------------------


def _match_by_code(tv_symbol: str, symbols: Mapping[str, SymbolInfo]) -> SymbolInfo | None:
    """交易所前缀不同（如 BATS:SPY 与登记的 AMEX:SPY）时，按代码部分唯一匹配。"""
    code = tv_symbol.split(":", 1)[-1]
    found = [s for s in symbols.values() if s.tv_symbol.split(":", 1)[-1] == code]
    return found[0] if len(found) == 1 else None


def _unregistered(tv_symbol: str) -> SymbolInfo:
    code = tv_symbol.split(":", 1)[-1]
    symbol = re.sub(r"[^A-Za-z0-9_\-]", "_", code)
    return SymbolInfo(symbol=symbol, tv_symbol=tv_symbol)


def invalid_rows(decisions: Iterable[DataDecision]) -> dict[str, dict[dt.date, str]]:
    """已裁定日期表中的无效数据（decision=invalid）：{标的: {日期: 理由}}。"""
    out: dict[str, dict[dt.date, str]] = {}
    for d in decisions:
        if d.decision == "invalid":
            out.setdefault(d.symbol, {})[d.date] = d.reason
    return out


def read_file(
    path: Path,
    export_date: dt.date,
    symbols: Mapping[str, SymbolInfo],
    root: Path,
    export_time: dt.datetime | None = None,
    invalid: Mapping[str, Mapping[dt.date, str]] | None = None,
) -> FileReport:
    """读取并校验一个导出文件（第5.1、5.2节）。不修改原始文件。

    invalid：已裁定为无效数据的行（按标的），读取后排除，并在说明中列出。
    """
    data = path.read_bytes()
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    report = FileReport(rel, path.name, export_date, sha256=hashlib.sha256(data).hexdigest())

    try:
        report.tv_symbol, report.timeframe = parse_filename(path.name, symbols)
    except TradingViewError as exc:
        report.add(FAILED, str(exc))
        return report
    info = symbols.get(report.tv_symbol)
    if info is None:
        info = _match_by_code(report.tv_symbol, symbols)
        if info is not None:
            report.add("info", f"文件的交易所前缀与登记不同：{report.tv_symbol} 按已登记的 {info.tv_symbol} 处理")
            report.tv_symbol = info.tv_symbol
    if info is None:
        info = _unregistered(report.tv_symbol)
        report.add(WARNING, f"{report.tv_symbol} 未在 config/symbols.yaml 登记（按 reference 保存）")
    report.symbol = info.symbol
    if report.timeframe not in _DAILY:
        report.add(FAILED, f"周期为 {report.timeframe}，不是日线（1D）")
        return report

    try:
        _read_rows(report, data, ZoneInfo(info.timezone))
    except (TradingViewError, ValueError) as exc:
        report.add(FAILED, f"读取失败：{exc}")
        return report
    for d, reason in sorted((invalid or {}).get(report.symbol, {}).items()):
        if report.bars.pop(d, None) is not None:
            report.add("info", f"按已裁定日期表排除无效数据 {d}（{reason}）")
    if not report.bars:
        report.add(FAILED, "文件中没有数据行")
        return report

    _check_incomplete_last_bar(report, path, export_time)
    _check_known_values(report, info)
    _check_calendar(report, info)
    _check_ranges(report, info)
    _check_history(report, info)
    return report


def _read_rows(report: FileReport, data: bytes, tz: ZoneInfo) -> None:
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise TradingViewError("空文件") from exc
    names = [h.strip() for h in header]
    lower = [n.lower() for n in names]
    if "time" not in lower or "close" not in lower:
        raise TradingViewError(f"缺少 time 或 close 列：{names}")
    used: set[str] = set()
    columns: list[str] = []
    for n, lo in zip(names, lower, strict=True):
        if lo in _BASE_COLUMNS or lo == "time":
            columns.append(lo)
        elif lo in {"volume", "vol"} and "volume" not in columns:
            columns.append("volume")
        else:
            columns.append(_extra_name(n, used))

    formats: set[str] = set()
    utc_midnight = 0
    seen: list[dt.date] = []
    for lineno, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != len(columns):
            raise TradingViewError(f"第{lineno}行列数为 {len(row)}，表头为 {len(columns)} 列")
        rec = dict(zip(columns, row, strict=True))
        d, fmt, local = parse_time(rec["time"], tz)
        formats.add(fmt)
        if fmt == "unix" and local is not None and local.minute == 0 and local.hour in (19, 20):
            utc_midnight += 1
        seen.append(d)
        if d in report.bars:
            report.add(FAILED, f"日期重复：{d}（第{lineno}行）")
            continue
        report.bars[d] = Bar(
            date=d,
            open=_number(rec.get("open")),
            high=_number(rec.get("high")),
            low=_number(rec.get("low")),
            close=_number(rec.get("close")),
            volume=_number(rec.get("volume")),
            extras=tuple((c, _number(rec[c])) for c in columns if c.startswith("extra_")),
        )
    report.time_format = "/".join(sorted(formats)) or "-"
    if seen != sorted(seen):
        report.add(WARNING, "日期未按时间顺序排列（已按日期排序保存）")
    missing_close = sorted(d for d, b in report.bars.items() if b.close is None)
    if missing_close:
        report.add(FAILED, f"收盘值为空的日期 {len(missing_close)} 个：{_fmt_dates(missing_close)}")
    if utc_midnight > len(seen) / 2:
        report.add(
            WARNING,
            "多数 UNIX 时间戳是 UTC 午夜：按美东换算会早一天。若同时报\"疑似日期偏移\"，"
            "请在 config/symbols.yaml 中把该标的的 timezone 设为 UTC",
        )


def _check_incomplete_last_bar(
    report: FileReport, path: Path, export_time: dt.datetime | None
) -> None:
    """第5.2节第3条：最后一根K线日期等于导出日期、且导出时间在美东16:00之前，排除。"""
    last = report.last_date
    if last is None or last != report.export_date:
        return
    if export_time is None:
        raise TradingViewError(f"{report.file_path} 最后一根K线日期等于导出日期 {last}，"
                               "导出时刻未记录；请人工登记导出时刻")
    when = export_time
    local = when.astimezone(NEW_YORK)
    if local.date() == last and local.time() < MARKET_CLOSE:
        del report.bars[last]
        report.add(
            WARNING,
            f"最后一根K线 {last} 不完整（导出时间美东 {local:%H:%M}，早于16:00），已排除",
        )
    else:
        report.add("info", f"最后一根K线 {last}：导出时间美东 {local:%Y-%m-%d %H:%M}，视为完整")


def _check_known_values(report: FileReport, info: SymbolInfo) -> None:
    """第5.2节第2条：known_values 对齐（容差 0.005）；与相邻日期对上时报"疑似日期偏移"。"""
    known = info.known_values or {}
    if not known:
        return
    days = sorted(report.bars)
    first, last = days[0], days[-1]
    ok = 0
    shifts: list[str] = []
    for d, k in sorted(known.items()):
        bar = report.bars.get(d)
        if bar is not None and bar.close is not None and abs(bar.close - k) <= TOLERANCE:
            ok += 1
            continue
        neighbor = _matching_neighbor(days, report.bars, d, k)
        if neighbor is not None:
            direction = "后一行" if neighbor > d else "前一行"
            shifts.append(direction)
            report.add(
                FAILED,
                f"疑似日期偏移：已知读数 {d}={k} 出现在 {neighbor}（{direction}）",
            )
        elif bar is not None:
            report.add(FAILED, f"已知读数不符：{d} 程序读到 {bar.close}，已知 {k}")
        elif first <= d <= last:
            report.add(FAILED, f"已知读数日期 {d} 不在数据中（位于导出范围内）")
        else:
            report.add("info", f"已知读数日期 {d} 不在导出范围 {first} 至 {last} 内，未校验")
    if ok:
        report.add("info", f"已知读数核对通过 {ok}/{len(known)} 个")
    if len(shifts) >= 2 and len(set(shifts)) == 1:
        report.add(
            FAILED,
            f"疑似日期整体偏移（数值都落在{shifts[0]}）：请检查 symbols.yaml 的 timezone 或导出的时间格式",
        )


def _matching_neighbor(
    days: list[dt.date], bars: Mapping[dt.date, Bar], d: dt.date, k: float
) -> dt.date | None:
    """d 前后相邻的数据行中，收盘值与已知读数相符的日期。"""
    before = [x for x in days if x < d][-1:]
    after = [x for x in days if x > d][:1]
    for x in (*before, *after):
        c = bars[x].close
        if c is not None and abs(c - k) <= TOLERANCE:
            return x
    return None


def _check_calendar(report: FileReport, info: SymbolInfo) -> None:
    """第5.2节第4条：与 NYSE 交易日对比。"""
    if info.calendar == "none" or not report.bars:
        return
    first, last = min(report.bars), max(report.bars)
    trading = set(mcal.stock_trading_days(first, last))
    have = set(report.bars)
    missing = sorted(trading - have)
    extra = sorted(have - trading)
    if info.calendar == "bond":
        month_end = [d for d in extra if d.weekday() >= 5 and (d + dt.timedelta(days=1)).month != d.month]
        weekend = [d for d in extra if d.weekday() >= 5 and d not in month_end]
        holiday = [d for d in extra if d.weekday() < 5]
        if month_end:
            report.add("info", f"月末周末观测 {len(month_end)} 个（FRED ICE 系列属正常）：{_fmt_dates(month_end)}")
        if holiday:
            report.add("info", f"股市休市日有数据 {len(holiday)} 个（债市营业日，属正常）："
                               f"{_fmt_dates(holiday)}")
        if weekend:
            report.add(WARNING, f"周末有数据 {len(weekend)} 个：{_fmt_dates(weekend)}")
        if missing:
            report.add("info", f"NYSE 交易日无数据 {len(missing)} 个（可能为债市休市）："
                               f"{_fmt_dates(missing)}")
        return
    if missing:
        report.add(WARNING, f"缺少 NYSE 交易日 {len(missing)} 个：{_fmt_dates(missing)}")
    if extra:
        report.add(WARNING, f"非 NYSE 交易日有数据 {len(extra)} 个：{_fmt_dates(extra)}")


def _check_ranges(report: FileReport, info: SymbolInfo) -> None:
    """第5.2节第5条：百分比类在 0–100；价格类为正数。"""
    if info.unit is None or info.unit == "net":  # net：上涨减下跌等差值，可为负数
        return
    bad: list[dt.date] = []
    for d, b in report.bars.items():
        vals = [v for v in (b.open, b.high, b.low, b.close) if v is not None]
        if info.unit == "percent":
            ok = all(0 <= v <= 100 for v in vals)
        elif info.unit == "count":
            ok = all(v >= 0 for v in vals)
        else:
            ok = all(v > 0 for v in vals)
        if not ok:
            bad.append(d)
    if bad:
        rule = "0–100" if info.unit == "percent" else "正数"
        report.add(FAILED, f"数值不在合理范围（{rule}）的日期 {len(bad)} 个：{_fmt_dates(sorted(bad))}")


def _check_history(report: FileReport, info: SymbolInfo) -> None:
    """第5.2节第6条：最早日期与标的起始日期对比，提示历史是否完整加载。"""
    first = report.first_date
    if first is None:
        return
    if info.inception is None:
        report.add("info", f"最早日期 {first}；未登记起始日期，请确认是否已加载全部历史")
    elif (first - info.inception).days > HISTORY_GAP_DAYS:
        report.add(
            WARNING,
            f"历史可能未完整加载：最早 {first}，标的起始 {info.inception}"
            "（在图表上向左拖动加载全部历史后再导出）",
        )


def _fmt_dates(dates: Iterable[dt.date]) -> str:
    ds = list(dates)
    shown = "、".join(str(d) for d in ds[:MAX_LISTED])
    return shown + (f" 等（共{len(ds)}个）" if len(ds) > MAX_LISTED else "")


# ---------------------------------------------------------------------------
# manifest 与合并
# ---------------------------------------------------------------------------


def read_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {row["file_path"]: row for row in csv.DictReader(f)}


def write_manifest(path: Path, rows: Mapping[str, Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({k: rows[key].get(k, "") for k in MANIFEST_FIELDS})


def manifest_row(report: FileReport, imported_at: str, export_time: str) -> dict[str, str]:
    notes = "；".join(i.message for i in report.issues if i.level != "info")
    return {
        "file_path": report.file_path,
        "export_date": report.export_date.isoformat(),
        "export_time": export_time,
        "tv_symbol": report.tv_symbol,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "first_date": str(report.first_date or ""),
        "last_date": str(report.last_date or ""),
        "rows": str(len(report.bars)),
        "time_format": report.time_format,
        "sha256": report.sha256,
        "imported_at": imported_at,
        "validation_status": report.status,
        "notes": notes,
    }


def merge_reports(reports: Iterable[FileReport]) -> tuple[dict[str, dict[dt.date, tuple[Bar, str]]], list[str]]:
    """第5.3节：按标的合并；重叠日期的数值必须一致（容差 0.005），不一致时报错、不写入该标的。

    校验失败的文件不参与合并。返回 ({标的: {日期: (K线, 来源文件)}}, 错误)。
    """
    merged: dict[str, dict[dt.date, tuple[Bar, str]]] = {}
    conflicts: dict[str, list[str]] = {}
    for rep in sorted(reports, key=lambda r: r.file_path):
        if rep.status == FAILED or not rep.symbol:
            continue
        target = merged.setdefault(rep.symbol, {})
        for d, bar in rep.bars.items():
            if d not in target:
                target[d] = (bar, rep.file_path)
                continue
            old, src = target[d]
            diffs = [
                f"{name} {a} vs {b}"
                for name, a, b in (("open", old.open, bar.open), ("high", old.high, bar.high),
                                   ("low", old.low, bar.low), ("close", old.close, bar.close))
                if a is not None and b is not None and abs(a - b) > TOLERANCE
            ]
            if diffs:
                conflicts.setdefault(rep.symbol, []).append(
                    f"{d}：{src} 与 {rep.file_path} 不一致（{'；'.join(diffs)}）"
                )
    errors: list[str] = []
    for symbol, items in conflicts.items():
        merged.pop(symbol, None)
        errors.append(
            f"{symbol}：多次导出在重叠日期数值不一致，未写入清洗结果（不自动覆盖）："
            + "；".join(items[:MAX_LISTED])
            + (f" 等（共{len(items)}处）" if len(items) > MAX_LISTED else "")
        )
    return merged, errors


def write_processed(paths: StoragePaths, symbol: str, rows: Mapping[dt.date, tuple[Bar, str]]) -> Path:
    out = paths.tv_processed_file(symbol)
    out.parent.mkdir(parents=True, exist_ok=True)
    extra_cols: list[str] = []
    for bar, _ in rows.values():
        for name, _v in bar.extras:
            if name not in extra_cols:
                extra_cols.append(name)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["date", "open", "high", "low", "close", "volume", *extra_cols, "source_file"])
        for d in sorted(rows):
            bar, src = rows[d]
            extras = dict(bar.extras)
            writer.writerow([
                d.isoformat(),
                *("" if v is None else repr(v)
                  for v in (bar.open, bar.high, bar.low, bar.close, bar.volume)),
                *("" if extras.get(c) is None else repr(extras[c]) for c in extra_cols),
                src,
            ])
    return out


def read_processed(paths: StoragePaths, symbol: str) -> dict[dt.date, float]:
    """读取清洗结果的收盘值（供以后评分与交叉校验使用）。"""
    path = paths.tv_processed_file(symbol)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {
            dt.date.fromisoformat(r["date"]): float(r["close"])
            for r in csv.DictReader(f) if r["close"]
        }


def read_processed_bars(paths: StoragePaths, symbol: str) -> dict[dt.date, Bar]:
    """读取清洗结果的完整K线（开高低收；缺失为 None），用于数据质量检查。"""
    path = paths.tv_processed_file(symbol)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {
            dt.date.fromisoformat(r["date"]): Bar(
                dt.date.fromisoformat(r["date"]), _number(r.get("open")), _number(r.get("high")),
                _number(r.get("low")), _number(r.get("close")), _number(r.get("volume")),
            )
            for r in csv.DictReader(f) if r["close"]
        }


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    reports: list[FileReport]
    merge_errors: list[str]
    processed: dict[str, int]          # 标的 → 清洗结果行数
    manifest_path: Path


def export_date_from_dir(directory: Path) -> dt.date:
    try:
        return dt.date.fromisoformat(directory.name)
    except ValueError as exc:
        raise TradingViewError(
            f"目录名 {directory.name!r} 不是导出日期（应为 YYYY-MM-DD），或用 --export-date 指定"
        ) from exc


def import_directory(
    directory: Path,
    paths: StoragePaths,
    symbols: Mapping[str, SymbolInfo],
    export_date: dt.date | None = None,
    export_time: dt.datetime | None = None,
    now: dt.datetime | None = None,
    decisions: Iterable[DataDecision] = (),
) -> ImportResult:
    """导入一个目录下的全部 CSV：校验、更新 manifest、重建清洗结果。原始文件不做任何修改。"""
    directory = directory.resolve()
    raw_root = paths.tv_raw_root.resolve()
    if raw_root not in directory.parents and directory != raw_root:
        raise TradingViewError(f"原始文件应放在 {paths.tv_raw_root}/<导出日期>/ 下，当前为 {directory}")
    if not directory.is_dir():
        raise TradingViewError(f"目录不存在：{directory}")
    export_date = export_date or export_date_from_dir(directory)
    files = sorted(p for p in directory.iterdir() if p.suffix.lower() == ".csv")
    if not files:
        raise TradingViewError(f"{directory} 下没有 CSV 文件")

    imported_at = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat(timespec="seconds")
    manifest = read_manifest(paths.tv_manifest)
    new_reports = []
    invalid = invalid_rows(decisions)
    for p in files:
        rel = p.relative_to(paths.root).as_posix()
        old = manifest.get(rel)
        registered = old.get("export_time") if old else None
        if registered and registered != "未记录":
            effective_time = dt.datetime.fromisoformat(registered)
            time_text = registered
        elif export_time is not None:
            effective_time = export_time
            time_text = export_time.astimezone(NEW_YORK).isoformat(timespec="seconds")
        elif old is not None:
            effective_time = None
            time_text = "未记录"
        else:
            raise TradingViewError(f"新导入文件 {rel} 必须用 --export-time 登记带时区的导出时刻")
        rep = read_file(p, export_date, symbols, paths.root, effective_time, invalid)
        row = manifest_row(rep, imported_at, time_text)
        if old is not None and old.get("sha256") and old["sha256"] != rep.sha256:
            rep.add(FAILED, "原始文件与上次导入时不同（sha256 不符），原始文件不得修改")
            row = {**manifest_row(rep, imported_at, time_text), "sha256": old["sha256"]}  # 保留原哈希
        manifest[rep.file_path] = row
        new_reports.append(rep)

    return rebuild(paths, symbols, manifest, new_reports, export_time, decisions)


def rebuild(
    paths: StoragePaths,
    symbols: Mapping[str, SymbolInfo],
    manifest: Mapping[str, Mapping[str, str]],
    fresh: list[FileReport] | None = None,
    export_time: dt.datetime | None = None,
    decisions: Iterable[DataDecision] = (),
) -> ImportResult:
    """按 manifest 中的全部原始文件重建清洗结果（本次新读取的报告优先使用）。

    重新读取的文件按当前解析规则与登记更新 manifest 的标的名（保留原导入时间与原哈希）；
    不再对应任何原始文件的清洗结果（如旧规则下的错误标的名）一并删除。
    """
    by_path = {r.file_path: r for r in fresh or []}
    all_reports: list[FileReport] = []
    rows_out: dict[str, dict[str, str]] = {k: dict(v) for k, v in manifest.items()}
    invalid = invalid_rows(decisions)
    for rel, row in manifest.items():
        rep = by_path.get(rel)
        if rep is None:
            p = paths.root / rel
            if not p.exists():
                continue
            registered = row.get("export_time") or "未记录"
            effective_time = None if registered == "未记录" else dt.datetime.fromisoformat(registered)
            rep = read_file(p, dt.date.fromisoformat(row["export_date"]), symbols, paths.root,
                            effective_time, invalid)
            if row.get("sha256") and row["sha256"] != rep.sha256:
                rep.add(FAILED, "原始文件与上次导入时不同（sha256 不符），原始文件不得修改")
            rows_out[rel] = {**manifest_row(rep, row.get("imported_at", ""), registered),
                             "sha256": row.get("sha256") or rep.sha256}
        all_reports.append(rep)
    merged, errors = merge_reports(all_reports)
    processed = {}
    for symbol, rows in merged.items():
        write_processed(paths, symbol, rows)
        processed[symbol] = len(rows)
    _remove_orphans(paths, {r.symbol for r in all_reports if r.symbol})
    write_manifest(paths.tv_manifest, rows_out)
    return ImportResult(fresh if fresh is not None else all_reports, errors, processed,
                        paths.tv_manifest)


def _remove_orphans(paths: StoragePaths, symbols: set[str]) -> list[Path]:
    """删除不对应任何原始文件的清洗结果（清洗结果可随时由原始文件重建）。"""
    removed = []
    if paths.tv_processed_dir.is_dir():
        for p in paths.tv_processed_dir.glob("*.csv"):
            if p.stem not in symbols:
                p.unlink()
                removed.append(p)
    return removed


# ---------------------------------------------------------------------------
# 汇总表
# ---------------------------------------------------------------------------


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _width(s))


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(_width(h), *(_width(r[i]) for r in rows)) if rows else _width(h)
              for i, h in enumerate(headers)]
    line = "  ".join(_pad(h, w) for h, w in zip(headers, widths, strict=True))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(_pad(c, w) for c, w in zip(r, widths, strict=True)) for r in rows]
    return "\n".join([line, sep, *body])


def format_import_summary(result: ImportResult) -> str:
    """tv import 的汇总表：标的、起止日期、行数、校验结果、问题说明。"""
    rows = []
    for i, r in enumerate(result.reports, start=1):
        main = next((x.message for x in r.issues if x.level == FAILED), None) or next(
            (x.message for x in r.issues if x.level == WARNING), "")
        if len(main) > 40:
            main = main[:39] + "…"
        span = f"{r.first_date} 至 {r.last_date}" if r.bars else "-"
        rows.append([str(i), r.symbol or "?", r.tv_symbol or "?", span, str(len(r.bars)),
                     r.time_format or "-", STATUS_TEXT[r.status], main])
    parts = [
        "TradingView 导入汇总",
        format_table(["#", "标的", "TV代码", "起止日期", "行数", "时间格式", "结果", "主要问题"], rows),
        "",
        "详细说明：",
    ]
    for i, r in enumerate(result.reports, start=1):
        parts.append(f"[{i}] {r.file_name}（{STATUS_TEXT[r.status]}）")
        issues = sorted(r.issues, key=lambda x: -_LEVEL_ORDER[x.level])
        if not issues:
            parts.append("    无问题")
        for x in issues:
            tag = {"info": "说明", WARNING: "警告", FAILED: "失败"}[x.level]
            parts.append(f"    [{tag}] {x.message}")
    if result.merge_errors:
        parts += ["", "合并错误："] + [f"  - {e}" for e in result.merge_errors]
    n = {s: sum(1 for r in result.reports if r.status == s) for s in (PASSED, WARNING, FAILED)}
    parts += [
        "",
        f"结论：{len(result.reports)} 个文件，通过 {n[PASSED]}，警告 {n[WARNING]}，失败 {n[FAILED]}。",
    ]
    failed = [r.file_name for r in result.reports if r.status == FAILED]
    if failed:
        parts.append("需要检查或重新导出：" + "、".join(failed))
    if result.processed:
        parts.append(
            "清洗结果：" + "、".join(f"{s}（{n_}行）" for s, n_ in sorted(result.processed.items()))
        )
    parts.append(f"manifest：{result.manifest_path}")
    return "\n".join(parts)


def imported_symbols(paths: StoragePaths) -> list[dict[str, Any]]:
    """按标的汇总已导入的文件（tv list 的数据）。"""
    manifest = read_manifest(paths.tv_manifest)
    by_symbol: dict[str, list[Mapping[str, str]]] = {}
    for row in manifest.values():
        by_symbol.setdefault(row["symbol"] or row["tv_symbol"], []).append(row)
    out = []
    for symbol in sorted(by_symbol):
        items = by_symbol[symbol]
        firsts = [r["first_date"] for r in items if r["first_date"]]
        lasts = [r["last_date"] for r in items if r["last_date"]]
        statuses = {r["validation_status"] for r in items}
        status = FAILED if FAILED in statuses else WARNING if WARNING in statuses else PASSED
        processed = 0
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$", symbol):
            processed = len(read_processed(paths, symbol))
        out.append({"symbol": symbol, "tv_symbol": items[0]["tv_symbol"], "files": len(items),
                    "first_date": min(firsts) if firsts else None, "last_date": max(lasts) if lasts else None,
                    "processed_rows": processed, "status": status})
    return out


def format_list(rows: list[dict[str, Any]]) -> str:
    """tv list 的表格。"""
    if not rows:
        return "尚未导入任何 TradingView 文件"
    return format_table(
        ["标的", "TV代码", "文件数", "起止日期", "清洗后行数", "校验"],
        [[r["symbol"], r["tv_symbol"], str(r["files"]), f"{r['first_date'] or '-'} 至 {r['last_date'] or '-'}",
          str(r["processed_rows"]), STATUS_TEXT[r["status"]]] for r in rows],
    )
