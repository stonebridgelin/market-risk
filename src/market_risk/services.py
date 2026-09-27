"""业务入口（CLAUDE.md 第12条）：命令行、以后的 FastAPI 接口、Vue 前端与 LangChain Agent 都调用本模块。

约定：
- 每个函数只做业务，不打印、不读命令行参数；输入为明确的参数，返回数据类或可序列化为 JSON 的结构
  （`storage.runs.to_jsonable` 可把返回值转为 JSON）；
- 参数或状态不满足时抛出 ServiceError（信息为中文，可直接展示给用户）；
- 数据库读写经由 storage 模块。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from market_risk import calendar as mcal
from market_risk.config import (
    MarketHolidays,
    Settings,
    SymbolInfo,
    load_data_decisions,
    load_holidays,
    load_settings,
    load_symbols,
)
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


@dataclass(frozen=True)
class DataBuildReport:
    result: Any                    # market.BuildResult
    end: dt.date
    offline: bool
    revisions_path: Path | None    # 有修订时写出的清单


def data_build(ctx: Context, offline: bool = False, refresh: bool = False, accept_revisions: bool = False,
               end: dt.date | None = None, only: Collection[str] | None = None,
               collect: Callable[..., Any] | None = None, now: dt.datetime | None = None) -> DataBuildReport:
    """由接口缓存（按需下载）、TradingView 清洗结果和手工录入生成 data/market/（B1）。

    collect：测试时注入，签名 (end) -> (序列列表, 广度冲突)。
    """
    from market_risk.data import market
    from market_risk.data.cache import DataFetchError

    day = end or market.last_completed_trading_day(now)
    if collect is None:
        if offline:
            from market_risk.data.market_build import collect_offline

            def collect(e: dt.date) -> Any:
                return collect_offline(ctx.paths, ctx.settings, e, only)
        else:  # pragma: no cover - 联网
            from market_risk.config import get_fred_api_key
            from market_risk.data.market_build import collect_online

            key = get_fred_api_key()

            def collect(e: dt.date) -> Any:
                return collect_online(ctx.paths, ctx.settings, e, key, refresh, only)
    try:
        series, conflicts = collect(day)
    except DataFetchError as exc:
        raise ServiceError(f"数据集生成失败：{exc}") from exc
    names = {s.name for s in series}
    result = market.build_dataset(ctx.paths, series, accept_revisions,
                                  conflicts if names & {"S5FI", "S5TW"} else None, now)
    rev_path = None
    if result.revisions:
        stamp = (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
        _write_report(ctx.paths.market_revisions_md, market.render_revisions(result, stamp))
        rev_path = ctx.paths.market_revisions_md
    return DataBuildReport(result, day, offline, rev_path)


def load_market_inputs(ctx: Context, base: dt.date, mode: str = "backtest") -> Any:
    """从 data/market/ 组装基准日的 RawInputs；数据集未覆盖基准日时报错（提示先运行 fetch）。"""
    from market_risk.data import market

    try:
        return market.load_raw_inputs(ctx.paths, ctx.settings, base, mode, load_data_decisions())
    except market.MarketDataError as exc:
        raise ServiceError(str(exc)) from exc


def fetch_vintage(ctx: Context, base: dt.date, refresh: bool = False) -> str | None:  # pragma: no cover - 联网
    """为正式样本取得 OAS 的 ALFRED 基准日版本，存入 data/market/vintage/（B1-3）。返回说明（未取得时）。"""
    from market_risk.config import get_fred_api_key
    from market_risk.data import fred, market
    from market_risk.data.cache import DataFetchError

    if not ctx.settings.oas_revision_check:
        return None
    start = base - dt.timedelta(days=market.DAILY_LOOKBACK_DAYS)
    rows = market.read_series_file(ctx.paths.market_daily_file(ctx.settings.oas_series))[1]
    if not any(r["source"] == "fred" for d, r in rows.items() if start <= d <= base):
        return None     # 早于 FRED 提供的三年：不做历史修订比对（load_raw_inputs 会注明）
    try:
        values, info = fred.fetch_series(ctx.paths, ctx.settings.oas_series, start, base, get_fred_api_key(),
                                         realtime=base, refresh=refresh, max_retries=ctx.settings.max_retries,
                                         backoff_seconds=ctx.settings.backoff_seconds)
    except DataFetchError as exc:
        return f"ALFRED 基准日版本无法取得（{exc}），不做历史修订比对；不影响计分"
    market.write_vintage(ctx.paths, ctx.settings.oas_series, base, values, info)
    return None


def fetch_data(ctx: Context, base: dt.date, mode: str = "backtest", refresh: bool = False,
               save_raw: Path | None = None) -> FetchResult:  # pragma: no cover - 联网
    """fetch = 按需下载并生成数据集（B1-2）+ 正式样本的 ALFRED 基准日版本，再从 data/market/ 截断到基准日生成快照。"""
    from market_risk.data import market
    from market_risk.data.raw_io import save_raw_inputs
    from market_risk.data.snapshot import build_snapshot

    _check_mode(mode)
    notes: list[str] = []
    if refresh or market.coverage_error(ctx.paths, ctx.settings, base):
        report = data_build(ctx, refresh=refresh)
        if report.revisions_path:
            notes.append(f"数据集发现 {len(report.result.revisions)} 处历史修订，"
                         f"未自动覆盖（见 {report.revisions_path}）")
    vintage_note = fetch_vintage(ctx, base, refresh)
    raw = load_market_inputs(ctx, base, mode)
    extra = [n for n in (*notes, vintage_note) if n]
    if extra:
        import dataclasses

        raw = dataclasses.replace(raw, notes=(*raw.notes, *extra))
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
               breadth: BreadthInput | None = None) -> Any:
    """从 data/market/ 读取数据（不直接读接口缓存）、计算两个版本的评分，生成运行目录下的全部输出。

    传入的广度读数先写入 data/manual/breadth.csv，再离线更新数据集中的 S5FI、S5TW。
    数据集未覆盖基准日时报错，提示先运行 fetch。返回 pipeline.RunOutcome。
    """
    day = resolve_base_date(base, mode)
    breadth = breadth or BreadthInput()
    record_breadth_inputs(ctx, day, breadth)
    if any(v is not None for v in (breadth.s5fi, breadth.s5tw, breadth.s5fi_t5, breadth.s5tw_t5)):
        data_build(ctx, offline=True, only=("S5FI", "S5TW"), end=max(day, _last_completed()))
    return score_raw(ctx, load_market_inputs(ctx, day, mode))


def _last_completed() -> dt.date:
    from market_risk.data.market import last_completed_trading_day

    return last_completed_trading_day()


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

    settings = settings or load_settings()
    paths = StoragePaths(settings.storage_root)
    checks = validate_all(settings, market_paths=paths if paths.market_manifest.exists() else None)
    return ValidationReport(checks, sum(not c.ok for c in checks))


# ---------------------------------------------------------------------------
# TradingView
# ---------------------------------------------------------------------------


def tv_import(ctx: Context, directory: Path, export_date: dt.date | None = None,
              export_time: dt.datetime | None = None, symbols: Mapping[str, SymbolInfo] | None = None) -> Any:
    """导入并校验一个目录下的全部导出文件。返回 tradingview.ImportResult。"""
    from market_risk.data import tradingview as tv

    try:
        return tv.import_directory(directory, ctx.paths, symbols or load_symbols(), export_date, export_time,
                                   decisions=load_data_decisions())
    except tv.TradingViewError as exc:
        raise ServiceError(str(exc)) from exc


def tv_validate(ctx: Context, symbol: str | None = None) -> Any | None:
    """按 manifest 重新校验并重建清洗结果；尚未导入时返回 None。"""
    from market_risk.data import tradingview as tv

    manifest = tv.read_manifest(ctx.paths.tv_manifest)
    if not manifest:
        return None
    result = tv.rebuild(ctx.paths, load_symbols(), manifest, decisions=load_data_decisions())
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
        if info.crosscheck_note:
            base.not_applicable = info.crosscheck_note
            results.append(base)
            continue
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


ThirdPartyLoader = Callable[[str, dt.date, dt.date], Mapping[dt.date, float | None]]


def _default_third_party(ctx: Context, refresh: bool) -> tuple[ThirdPartyLoader | None, str]:  # pragma: no cover
    """第三方不复权收盘价：Tiingo（需 .env 中的 TIINGO_API_KEY；未设置时不核对）。"""
    from market_risk.config import get_tiingo_api_key
    from market_risk.data import tiingo

    key = get_tiingo_api_key()
    if key is None:
        return None, "未配置 TIINGO_API_KEY，第三方核对未做"

    def load(ticker: str, start: dt.date, end: dt.date) -> Mapping[dt.date, float | None]:
        return tiingo.fetch_closes(ctx.paths, ticker, start, end, key, refresh,
                                   ctx.settings.max_retries, ctx.settings.backoff_seconds)

    return load, "Tiingo close"


def third_party_checks(results: list[Any], infos: Mapping[str, SymbolInfo],
                       loader: ThirdPartyLoader | None, source: str) -> None:
    """对回测区间内差值超过 0.02 的日期取第三方不复权收盘价（先用已知读数验证不复权），结果写入各 CompareResult。"""
    from market_risk.data.tiingo import verify_unadjusted
    from market_risk.data.tv_compare import ThirdPartyCheck

    for r in results:
        items = r.listed_dates()
        if not items:
            continue
        if loader is None:
            r.third_party_note = source
            continue
        known = infos[r.symbol].known_values or {}
        start = min(items[0][0], *known) if known else items[0][0]
        end = max(items[-1][0], *known) if known else items[-1][0]
        try:
            closes = loader(r.symbol, start, end)
        except Exception as exc:  # 第三方失败只写入报告
            r.third_party_note = f"第三方数据获取失败：{exc}"
            continue
        problems = verify_unadjusted(closes, known) if known else ["没有已知读数，无法验证是否不复权"]
        if problems:
            r.third_party_note = f"{source} 未通过不复权验证（{'；'.join(problems)}），不采用"
            continue
        r.third_party_note = f"{source} 已用已知读数验证为不复权（{len(known)} 个读数全部相符）"
        r.third_party = [ThirdPartyCheck(d, a, b, closes.get(d), source) for d, a, b, _ in items]


def tv_crosscheck(ctx: Context, refresh: bool = False, loader: ApiLoader | None = None,
                  now: dt.datetime | None = None,
                  third_party: tuple[ThirdPartyLoader | None, str] | None = None) -> CompareReport:
    """全部 crosscheck 标的比对，写 reports/tradingview_crosscheck.md。"""
    from market_risk.data.tv_compare import format_results

    infos = [s for s in load_symbols().values() if s.usage == "crosscheck" and s.api_source]
    results = compare_symbols(ctx, infos, loader or _default_api_loader(ctx, refresh))
    tp_loader, tp_source = third_party or _default_third_party(ctx, refresh)
    third_party_checks(results, {s.symbol: s for s in infos}, tp_loader, tp_source)
    stamp = (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
    text = format_results(results, "TradingView 交叉校验", stamp)
    _write_report(ctx.paths.tv_crosscheck_md, text)
    return CompareReport(results, text, ctx.paths.tv_crosscheck_md)


@dataclass(frozen=True)
class QualityReport:
    results: list[Any]             # tv_quality.QualityResult
    text: str
    path: Path


def tv_quality(ctx: Context, now: dt.datetime | None = None) -> QualityReport:
    """广度指标（symbols.yaml 中 category=breadth）的早期数据质量检查，写 reports/tradingview_data_quality.md。"""
    from market_risk.config import load_holidays
    from market_risk.data import tradingview as tv
    from market_risk.data.tv_quality import analyze, compare_versions, missing_trading_days, render

    results = []
    for info in sorted(load_symbols().values(), key=lambda s: s.symbol):
        if info.category != "breadth":
            continue
        bars = tv.read_processed_bars(ctx.paths, info.symbol)
        if bars:
            results.append(analyze(info.symbol, bars))
    move = None
    ice, alt = tv.read_processed(ctx.paths, "MOVE_ICE"), tv.read_processed(ctx.paths, "MOVE_TVC")
    if ice and alt:
        from market_risk.data.calendar_audit import sifma_rule_holidays

        # holidays.yaml 从 2008 年起；更早的年份按 SIFMA 常见规则
        known = load_holidays().bond_holidays
        first_year = min(known).year if known else max(ice).year + 1
        early = {d for y in range(min(ice).year, first_year) for d in sifma_rule_holidays(y)}
        move = compare_versions("MOVE_ICE（ICE_DLY:MOVE）", "MOVE_TVC（TVC:MOVE）", ice, alt, known | early)
    gaps = {s: missing_trading_days(c) for s in ("HIGN", "LOWN", "VIX3M", "SKEW")
            if (c := tv.read_processed(ctx.paths, s))}
    stamp = (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
    text = render(results, stamp, move, gaps)
    _write_report(ctx.paths.tv_quality_md, text)
    return QualityReport(results, text, ctx.paths.tv_quality_md)


# ---------------------------------------------------------------------------
# 休市日历审计与债市休市日 OAS 清单
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarAuditReport:
    audit: Any                     # calendar_audit.CalendarAudit
    oas_differences: list[Any]     # calendar_audit.HolidayOasDiff
    text: str
    path: Path
    yaml_text: str
    yaml_path: Path | None         # 写入 holidays.yaml 时为其路径


TreasuryLoader = Callable[[int, int], set[dt.date]]


def _default_treasury_loader(ctx: Context) -> TreasuryLoader:  # pragma: no cover - 联网
    from market_risk.data.treasury import fetch_treasury_year

    def load(first_year: int, last_year: int) -> set[dt.date]:
        today = dt.datetime.now(dt.UTC).date()
        days: set[dt.date] = set()
        for year in range(first_year, last_year + 1):
            values, _ = fetch_treasury_year(ctx.paths, year, min(dt.date(year, 12, 31), today),
                                            max_retries=ctx.settings.max_retries,
                                            backoff_seconds=ctx.settings.backoff_seconds)
            days |= {d for d, v in values.items() if v is not None}
        return days

    return load


def audit_calendar(ctx: Context, start_year: int = 2008, oas_start_year: int = 1997, write_yaml: bool = False,
                   loader: TreasuryLoader | None = None, holidays_path: Path | None = None,
                   now: dt.datetime | None = None) -> CalendarAuditReport:
    """补齐休市日历并与 SIFMA 常见规则对照；列出 1997 年以来债市休市日 OAS 数值不同的情况（只列出，不修正）。"""
    from market_risk.config import DEFAULT_HOLIDAYS_PATH
    from market_risk.data import calendar_audit as ca
    from market_risk.data import tradingview as tv

    today = (now or dt.datetime.now(dt.UTC)).date()
    treasury_days = (loader or _default_treasury_loader(ctx))(min(start_year, oas_start_year), today.year)
    if not treasury_days:
        raise ServiceError("没有取得财政部数据")
    end = max(treasury_days)
    audit = ca.audit_calendar(treasury_days, dt.date(start_year, 1, 1), end)
    decisions = load_data_decisions()
    diffs = []
    for symbol in (ctx.settings.oas_series, "BAMLC0A0CM"):
        series = tv.read_processed(ctx.paths, symbol)
        if series:
            diffs += ca.oas_holiday_differences(symbol, series, treasury_days, dt.date(oas_start_year, 1, 1),
                                                min(end, max(series)), decisions)
    diffs.sort(key=lambda x: (x.date, x.symbol))
    stamp = (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
    text = ca.render_audit(audit, diffs, stamp, dt.date(oas_start_year, 1, 1))
    _write_report(ctx.paths.holiday_audit_md, text)

    target = holidays_path or DEFAULT_HOLIDAYS_PATH
    current = load_holidays(target) if target.exists() else MarketHolidays(frozenset(), frozenset(), frozenset(),
                                                                            frozenset())
    manual = {"stock_holidays": current.stock_holidays, "stock_early_closes": current.stock_early_closes,
              "bond_holidays": current.bond_holidays, "bond_early_closes": current.bond_early_closes}
    yaml_text = ca.render_holidays_yaml(audit, manual)
    if write_yaml:
        target.write_text(yaml_text, encoding="utf-8")
    return CalendarAuditReport(audit, diffs, text, ctx.paths.holiday_audit_md, yaml_text,
                               target if write_yaml else None)


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
