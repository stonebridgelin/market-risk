"""业务入口（CLAUDE.md 第12条）：命令行、以后的 FastAPI 接口、Vue 前端与 LangChain Agent 都调用本模块。

约定：
- 每个函数只做业务，不打印、不读命令行参数；输入为明确的参数，返回数据类或可序列化为 JSON 的结构
  （`storage.runs.to_jsonable` 可把返回值转为 JSON）；
- 参数或状态不满足时抛出 ServiceError（信息为中文，可直接展示给用户）；
- 数据库读写经由 storage 模块。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from market_risk import calendar as mcal
from market_risk.config import MarketHolidays, Settings, SymbolInfo, load_holidays, load_settings, load_symbols
from market_risk.models import BreadthReading, DateReferences, MarketSnapshot
from market_risk.storage.paths import MARKET, RISK_SCORING, StoragePaths

MODES = ("backtest", "daily")


class ServiceError(ValueError):
    """参数或状态不满足（信息可直接展示给用户）。"""


@dataclass(frozen=True)
class Context:
    """一次调用所需的配置与存储位置。"""

    settings: Settings
    paths: StoragePaths
    db_url: str

    @classmethod
    def from_settings(cls, settings: Settings | None = None, db_url: str | None = None) -> Context:
        from market_risk.storage import db

        settings = settings or load_settings()
        return cls(settings, StoragePaths(settings.storage_root), db_url or db.resolve_database_url(settings))


def _rebuild_db(ctx: Context) -> None:
    from market_risk.storage import db

    db.rebuild(ctx.paths, ctx.db_url)


# ---------------------------------------------------------------------------
# 日期与样本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatesPreview:
    refs: DateReferences
    notes: tuple[str, ...]


def preview_dates(base: dt.date, holidays: MarketHolidays | None = None) -> DatesPreview:
    """离线计算日期参照：债市日历暂按 holidays.yaml 推出（正式计算以财政部数据为准）。"""
    holidays = holidays or load_holidays()
    bond_cal = mcal.bond_calendar_from_holidays(holidays, base - dt.timedelta(days=120), base)
    try:
        refs = mcal.compute_date_references(base, bond_cal)
    except mcal.CalendarError as exc:
        raise ServiceError(str(exc)) from exc
    notes = [
        "债市营业日暂按 config/holidays.yaml 推出，未经财政部数据核实；v2-M 的 O6 需接入 FRED 观测列表后才能确定。",
        *(f"日历核对：{n}" for n in mcal.check_stock_calendar(holidays, refs.three_segment_query_start, base)),
    ]
    return DatesPreview(refs, tuple(notes))


@dataclass(frozen=True)
class SampleDate:
    date: dt.date
    weekday: str
    note: str


def sample_dates(year: int) -> list[SampleDate]:
    """SOP 9.2：每月最后一个周五；若该日休市，取该月最后一个交易日。"""
    return [
        SampleDate(d, f"周{'一二三四五六日'[d.weekday()]}",
                   "" if d.weekday() == 4 else "最后一个周五休市，取当月最后一个交易日")
        for d in mcal.monthly_sample_dates(year)
    ]


# ---------------------------------------------------------------------------
# 数据与评分
# ---------------------------------------------------------------------------


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ServiceError("mode 只能是 backtest 或 daily")


@dataclass(frozen=True)
class FetchResult:
    raw: Any                       # RawInputs
    snapshot: MarketSnapshot


def fetch_data(ctx: Context, base: dt.date, mode: str = "backtest", refresh: bool = False,
               save_raw: Path | None = None) -> FetchResult:  # pragma: no cover - 联网
    """下载基准日所需的全部数据，截断到基准日并生成快照。"""
    from market_risk.config import get_fred_api_key
    from market_risk.data.fetch import fetch_raw_inputs
    from market_risk.data.raw_io import save_raw_inputs
    from market_risk.data.snapshot import build_snapshot

    _check_mode(mode)
    raw = fetch_raw_inputs(base, ctx.settings, ctx.paths, get_fred_api_key(), mode, refresh)
    if save_raw is not None:
        save_raw_inputs(raw, save_raw)
    snap = build_snapshot(raw, ctx.settings.scored_symbols, tuple(ctx.settings.reference_symbols[:2]),
                          load_holidays())
    return FetchResult(raw, snap)


@dataclass(frozen=True)
class BreadthInput:
    """命令行或接口传入的广度读数（基准日与 5 日前，均可缺省）。"""

    s5fi: float | None = None
    s5tw: float | None = None
    s5fi_t5: float | None = None
    s5tw_t5: float | None = None


def record_breadth_inputs(ctx: Context, base: dt.date, breadth: BreadthInput, note: str = "score 录入") -> None:
    """传入的广度读数写入 data/manual/breadth.csv（SPEC 6.5）；已有不同读数时报错。"""
    from market_risk.data.breadth import BreadthError, make_reading, upsert_breadth

    for day, f, w in ((base, breadth.s5fi, breadth.s5tw),
                      (mcal.shift_trading_days(base, -5), breadth.s5fi_t5, breadth.s5tw_t5)):
        if f is None and w is None:
            continue
        if f is None or w is None:
            raise ServiceError(f"{day} 的 S5FI 与 S5TW 需要同时提供")
        try:
            upsert_breadth(ctx.paths.breadth_csv, make_reading(day, f, w, note))
        except BreadthError as exc:
            raise ServiceError(f"{exc}（如需修改，请用 breadth add --overwrite）") from exc


def resolve_base_date(base: dt.date | None, mode: str) -> dt.date:
    from market_risk.data.cache import today_new_york

    _check_mode(mode)
    if base is None and mode != "daily":
        raise ServiceError("回测模式必须提供基准日")
    day = base or today_new_york()
    if not mcal.is_stock_trading_day(day):
        raise ServiceError(f"{day} 不是股票交易日")
    return day


def score_raw(ctx: Context, raw: Any, git: Any = None) -> Any:
    """对已取得的原始数据评分并写运行目录（离线可用）。返回 pipeline.RunOutcome。"""
    from market_risk.pipeline import run_scoring
    from market_risk.storage import runs

    return run_scoring(raw, ctx.settings, ctx.paths, git or runs.git_info(ctx.paths.root), load_holidays())


def score_date(ctx: Context, base: dt.date | None, mode: str = "backtest",
               breadth: BreadthInput | None = None, refresh: bool = False) -> Any:  # pragma: no cover - 联网
    """下载数据、计算两个版本的评分，生成运行目录下的全部输出。返回 pipeline.RunOutcome。"""
    from market_risk.config import get_fred_api_key
    from market_risk.data.fetch import fetch_raw_inputs

    day = resolve_base_date(base, mode)
    record_breadth_inputs(ctx, day, breadth or BreadthInput())
    raw = fetch_raw_inputs(day, ctx.settings, ctx.paths, get_fred_api_key(), mode, refresh)
    return score_raw(ctx, raw)


@dataclass(frozen=True)
class ValidationReport:
    checks: list[Any]              # validation.Check
    mismatches: int

    @property
    def ok(self) -> bool:
        return self.mismatches == 0


def validate_samples(settings: Settings | None = None) -> ValidationReport:
    """用离线样本比对程序值与截图读数（SPEC 第9节阶段4）。"""
    from market_risk.validation import validate_all

    checks = validate_all(settings or load_settings())
    return ValidationReport(checks, sum(not c.ok for c in checks))


# ---------------------------------------------------------------------------
# TradingView
# ---------------------------------------------------------------------------


def tv_import(ctx: Context, directory: Path, export_date: dt.date | None = None,
              export_time: dt.datetime | None = None, symbols: Mapping[str, SymbolInfo] | None = None) -> Any:
    """导入并校验一个目录下的全部导出文件。返回 tradingview.ImportResult。"""
    from market_risk.data import tradingview as tv

    try:
        return tv.import_directory(directory, ctx.paths, symbols or load_symbols(), export_date, export_time)
    except tv.TradingViewError as exc:
        raise ServiceError(str(exc)) from exc


def tv_validate(ctx: Context, symbol: str | None = None) -> Any | None:
    """按 manifest 重新校验并重建清洗结果；尚未导入时返回 None。"""
    from market_risk.data import tradingview as tv

    manifest = tv.read_manifest(ctx.paths.tv_manifest)
    if not manifest:
        return None
    result = tv.rebuild(ctx.paths, load_symbols(), manifest)
    if symbol:
        result.reports = [r for r in result.reports if r.symbol.upper() == symbol.upper()]
    return result


def tv_list(ctx: Context) -> list[dict[str, Any]]:
    from market_risk.data import tradingview as tv

    return tv.imported_symbols(ctx.paths)


@dataclass(frozen=True)
class CompareReport:
    results: list[Any]             # tv_compare.CompareResult
    text: str
    path: Path


ApiLoader = Callable[[SymbolInfo, dt.date, dt.date], Mapping[dt.date, float | None]]


def _default_api_loader(ctx: Context, refresh: bool) -> ApiLoader:  # pragma: no cover - 联网
    from market_risk.config import ConfigError, get_fred_api_key
    from market_risk.data.tv_compare import fetch_api_series

    try:
        key: str | None = get_fred_api_key()
    except ConfigError:
        key = None

    def load(info: SymbolInfo, start: dt.date, end: dt.date) -> Mapping[dt.date, float | None]:
        return fetch_api_series(info.api_source or "", start, end, ctx.settings, ctx.paths, key, refresh)

    return load


def compare_symbols(ctx: Context, infos: list[SymbolInfo], loader: ApiLoader) -> list[Any]:
    """TradingView 清洗结果与接口数据逐日比对（接口数据由 loader 提供，便于离线测试）。"""
    from market_risk.data import tradingview as tv
    from market_risk.data.tv_compare import CompareResult, compare_series

    results = []
    for info in infos:
        series = tv.read_processed(ctx.paths, info.symbol)
        base = CompareResult(info.symbol, info.tv_symbol, info.api_source or "", info.tolerance)
        if not series:
            base.error = "尚未导入该标的的 TradingView 数据"
            results.append(base)
            continue
        try:
            api = loader(info, min(series), max(series))
        except Exception as exc:  # 接口失败写入报告，不中断其他标的
            base.error = f"接口数据获取失败：{exc}"
            results.append(base)
            continue
        results.append(compare_series(info, series, api, info.tolerance))
    return results


def _write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tv_compare(ctx: Context, symbol: str, refresh: bool = False, loader: ApiLoader | None = None,
               now: dt.datetime | None = None) -> CompareReport:
    """一个标的的重叠比对，写 reports/tradingview_compare_<标的>.md。"""
    from market_risk.data.tv_compare import format_results

    infos = [s for s in load_symbols().values() if s.symbol.upper() == symbol.upper()]
    if not infos or not infos[0].api_source:
        raise ServiceError(f"{symbol} 未在 config/symbols.yaml 登记 api_source")
    results = compare_symbols(ctx, infos, loader or _default_api_loader(ctx, refresh))
    stamp = (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
    text = format_results(results, f"TradingView 与接口数据重叠比对：{infos[0].symbol}", stamp)
    path = ctx.paths.tv_compare_md(infos[0].symbol)
    _write_report(path, text)
    return CompareReport(results, text, path)


def tv_crosscheck(ctx: Context, refresh: bool = False, loader: ApiLoader | None = None,
                  now: dt.datetime | None = None) -> CompareReport:
    """全部 crosscheck 标的比对，写 reports/tradingview_crosscheck.md。"""
    from market_risk.data.tv_compare import format_results

    infos = [s for s in load_symbols().values() if s.usage == "crosscheck" and s.api_source]
    results = compare_symbols(ctx, infos, loader or _default_api_loader(ctx, refresh))
    stamp = (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
    text = format_results(results, "TradingView 交叉校验", stamp)
    _write_report(ctx.paths.tv_crosscheck_md, text)
    return CompareReport(results, text, ctx.paths.tv_crosscheck_md)


# ---------------------------------------------------------------------------
# 正式记录、资料、广度
# ---------------------------------------------------------------------------


def official_set(ctx: Context, base: dt.date, run_id: str, subject: str = MARKET,
                 framework: str = RISK_SCORING) -> dict[str, Any]:
    from market_risk.storage.runs import set_official

    try:
        pointer = set_official(ctx.paths, subject, framework, base, run_id, "manual")
    except FileNotFoundError as exc:
        raise ServiceError(str(exc)) from exc
    _rebuild_db(ctx)
    return pointer


def official_confirm(ctx: Context, base: dt.date, subject: str = MARKET,
                     framework: str = RISK_SCORING) -> dict[str, Any]:
    from market_risk.storage.runs import confirm_official

    try:
        pointer = confirm_official(ctx.paths, subject, framework, base)
    except FileNotFoundError as exc:
        raise ServiceError(str(exc)) from exc
    _rebuild_db(ctx)
    return pointer


def material_add(ctx: Context, base: dt.date, kind: str, file: Path, subject: str = MARKET,
                 source: str = "", note: str = "") -> Path:
    from market_risk.materials import MaterialError, add_material

    try:
        target = add_material(ctx.paths, subject, base, kind, file, source, note)
    except MaterialError as exc:
        raise ServiceError(str(exc)) from exc
    _rebuild_db(ctx)
    return target


def material_list(ctx: Context, subject: str = MARKET, start: dt.date | None = None,
                  end: dt.date | None = None) -> list[dict[str, str]]:
    from market_risk.materials import list_materials

    return list_materials(ctx.paths, subject, start, end)


def breadth_existing(ctx: Context, day: dt.date) -> BreadthReading | None:
    from market_risk.data.breadth import read_breadth

    return read_breadth(ctx.paths.breadth_csv).get(day)


@dataclass(frozen=True)
class BreadthAddResult:
    changed: bool
    previous: BreadthReading | None


def breadth_add(ctx: Context, day: dt.date, s5fi: float, s5tw: float, note: str = "",
                overwrite: bool = False) -> BreadthAddResult:
    """写入手工广度读数；同一日期已有不同读数时，overwrite=False 则报错（由调用方确认后再覆盖）。"""
    from market_risk.data.breadth import BreadthError, make_reading, upsert_breadth

    try:
        reading = make_reading(day, s5fi, s5tw, note)
    except BreadthError as exc:
        raise ServiceError(str(exc)) from exc
    old = breadth_existing(ctx, day)
    if old is not None and (old.s5fi, old.s5tw) != (reading.s5fi, reading.s5tw) and not overwrite:
        raise ServiceError(f"{day} 已有读数 S5FI={old.s5fi}、S5TW={old.s5tw}；如需修改请加 --overwrite")
    return BreadthAddResult(upsert_breadth(ctx.paths.breadth_csv, reading, overwrite=True), old)


# ---------------------------------------------------------------------------
# 结果标签
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeResult:
    outcome: Any                   # outcomes.Outcome
    differences: list[str] = field(default_factory=list)


CloseLoader = Callable[[str, dt.date, dt.date], Mapping[dt.date, float]]


def _default_close_loader(ctx: Context, refresh: bool) -> CloseLoader:  # pragma: no cover - 联网
    from market_risk.data.cache import cached_series
    from market_risk.data.prices import close_series_from_frame, yfinance_download

    def load(yahoo: str, start: dt.date, end: dt.date) -> Mapping[dt.date, float]:
        key = yahoo.replace("^", "")

        def _dl() -> dict:
            return close_series_from_frame(yfinance_download(yahoo, start, end + dt.timedelta(days=1)), yahoo)

        series, _ = cached_series(ctx.paths, "yahoo", f"{key}_outcome", start, end, f"yfinance {yahoo}", _dl,
                                  refresh, ctx.settings.max_retries, ctx.settings.backoff_seconds)
        return {d: v for d, v in series.items() if v is not None}

    return load


def outcome_compute(ctx: Context, base: dt.date, refresh: bool = False, loader: CloseLoader | None = None,
                    today: dt.date | None = None) -> OutcomeResult:
    """结果窗口结束后，用标普500指数与 QQQ 的收盘价计算风险事件标签（SOP 9.3）。"""
    from market_risk.data.cache import today_new_york
    from market_risk.outcomes import OutcomeError, compute_outcome, outcome_window, record_outcome, window_finished

    today = today or today_new_york()
    _, end = outcome_window(base)
    if not window_finished(base, today):
        raise ServiceError(f"{base} 的结果窗口到 {end} 才结束，现在不得计算标签")
    load = loader or _default_close_loader(ctx, refresh)
    try:
        outcome = compute_outcome(base, load("^GSPC", base, end), load("QQQ", base, end), today)
    except OutcomeError as exc:
        raise ServiceError(str(exc)) from exc
    diffs = record_outcome(ctx.paths.outcomes_csv, outcome)
    _rebuild_db(ctx)
    return OutcomeResult(outcome, diffs)


def outcome_add(ctx: Context, base: dt.date, spx: float, qqq: float, event_date: dt.date | None = None,
                spx_peak_to_trough: float | None = None, qqq_peak_to_trough: float | None = None,
                today: dt.date | None = None) -> OutcomeResult:
    """人工录入风险事件标签（与程序计算值不一致时返回差异）。"""
    from market_risk.data.cache import today_new_york
    from market_risk.outcomes import (
        QQQ_THRESHOLD,
        SPX_THRESHOLD,
        Outcome,
        outcome_window,
        record_outcome,
        window_finished,
    )

    if not window_finished(base, today or today_new_york()):
        raise ServiceError(f"{base} 的结果窗口尚未结束")
    start, end = outcome_window(base)
    o = Outcome(MARKET, base, start, end, spx, qqq, spx <= SPX_THRESHOLD or qqq <= QQQ_THRESHOLD, event_date,
                "manual", dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                spx_peak_to_trough, qqq_peak_to_trough)
    diffs = record_outcome(ctx.paths.outcomes_csv, o)
    _rebuild_db(ctx)
    return OutcomeResult(o, diffs)


# ---------------------------------------------------------------------------
# 旧记录、数据库、统计
# ---------------------------------------------------------------------------


def import_legacy(ctx: Context, excel: Path | None = None, git: Any = None) -> Any:
    """导入截图时代的样本。返回 legacy.LegacyImportResult（score_differences 非空时需用户判断）。"""
    from market_risk.legacy import LegacyError
    from market_risk.legacy import import_legacy as _import
    from market_risk.storage import runs

    try:
        result = _import(excel or ctx.paths.legacy_excel, ctx.paths, git or runs.git_info(ctx.paths.root))
    except LegacyError as exc:
        raise ServiceError(str(exc)) from exc
    _rebuild_db(ctx)
    return result


def rebuild_database(ctx: Context) -> dict[str, int]:
    """由文件重建数据库，返回各表行数。"""
    from market_risk.storage import db

    db.rebuild(ctx.paths, ctx.db_url)
    return {t: len(v) for t, v in db.dump(ctx.db_url).items()}


@dataclass(frozen=True)
class StatsReport:
    text: str
    samples: list[Any]             # stats.Sample
    markdown_path: Path
    workbook_path: Path


def run_stats(ctx: Context, framework: str = RISK_SCORING, start: dt.date | None = None,
              end: dt.date | None = None, now: dt.datetime | None = None) -> StatsReport:
    from market_risk.stats import run_stats as _run

    text, samples = _run(ctx.paths, ctx.db_url, framework, start, end, now)
    return StatsReport(text, samples, ctx.paths.backtest_stats_md, ctx.paths.backtest_history_xlsx)
