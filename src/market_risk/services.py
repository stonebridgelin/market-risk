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
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from market_risk.data.price_review import ReviewConfig, ReviewResult
    from market_risk.data.price_review_inputs import ReviewInputs


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


def research_pullback_features(ctx: Context) -> Any:
    """运行小周期波段前瞻研究；研究依赖只在调用时加载。"""
    from market_risk.research.analysis import run_analysis

    try:
        return run_analysis(ctx.paths)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def research_audit_pullback_development(ctx: Context) -> Any:
    """核对原口径开发期观测与缺值，不重算验证期结果。"""
    from market_risk.research.development_audit import audit_development

    try:
        return audit_development(ctx.paths)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def research_zz_v121_sides(ctx: Context) -> Any:
    """开发期 ZZ 口径 B、DV 分侧预登记研究；不执行模型损失或选参。"""
    from market_risk.research.zz_v121 import run_zz_side_study

    try:
        return run_zz_side_study(ctx.paths)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def wavewarn_calibrate_exit_development(ctx: Context) -> Path:
    """生成开发期退出代价的描述性校准，不参与模型选参。"""
    from market_risk.wavewarn.calibration_run import run_exit_calibration

    try:
        return run_exit_calibration(ctx.paths.root)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def wavewarn_evaluate_development(ctx: Context) -> Any:
    """运行 v1.2.1 开发期 E2 工程评价；研究依赖只在调用时加载。"""
    from market_risk.wavewarn.evaluation_run import run_development_evaluation

    try:
        return run_development_evaluation(ctx.paths.root)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def wavewarn_evaluate_v13_development(ctx: Context) -> Any:
    """运行 v1.3 开发期工程评价（择时得分、可行条件、选择程序）；不锁定，不运行验证期。"""
    from market_risk.wavewarn.evaluation_v13_run import run_v13_development

    try:
        return run_v13_development(ctx.paths.root)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def wavewarn_extended_history(ctx: Context) -> Any:
    """运行 v1.3 的补充历史（固定延迟校准与 P0 稳健性，2009-09-30 及以前）；只作描述。"""
    from market_risk.wavewarn.extended_history_run import run_extended_history

    try:
        return run_extended_history(ctx.paths.root)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def wavewarn_diagnose_v13(ctx: Context) -> Any:
    """运行 v1.3 的描述性诊断（转绿瓶颈、亮灯原因、200日均线参照、逐年对照）；不改变任何登记。"""
    from market_risk.wavewarn.diagnostics_v13_run import run_v13_diagnostics

    try:
        return run_v13_diagnostics(ctx.paths.root)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def wavewarn_evaluate_v14_development(ctx: Context) -> Any:
    """运行 v1.4 开发期工程评价（MR、解除规则 F、三级选择程序）；不锁定，不运行验证期。"""
    from market_risk.wavewarn.evaluation_v14_run import run_v14_development

    try:
        return run_v14_development(ctx.paths.root)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def wavewarn_v14_extended_history(ctx: Context) -> Any:
    """运行 v1.4 的补充历史（纯价格版与 200 日均线、两次熊市）；只作描述，不改变选择。"""
    from market_risk.wavewarn.extended_history_v14_run import run_v14_extended_history

    try:
        return run_v14_extended_history(ctx.paths.root)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


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
    cutoff_note: str = ""


def data_build(ctx: Context, offline: bool = False, refresh: bool = False, accept_revisions: bool = False,
               end: dt.date | None = None, only: Collection[str] | None = None,
               collect: Callable[..., Any] | None = None, now: dt.datetime | None = None,
               decisions: tuple[Any, ...] | None = None) -> DataBuildReport:
    """由接口缓存（按需下载）、TradingView 清洗结果和手工录入生成 data/market/（B1）。

    collect：测试时注入，签名 (end) -> (序列列表, 广度冲突)；decisions：裁定表（默认读取 config/data_decisions.yaml）。
    """
    from market_risk.data import market
    from market_risk.data.cache import DataFetchError

    try:
        day, cutoff_note = market.resolve_build_end(end, now)
    except market.MarketDataError as exc:
        raise ServiceError(str(exc)) from exc
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
    from market_risk.data.breadth import BreadthError

    try:
        series, conflicts = collect(day)
    except (DataFetchError, BreadthError) as exc:
        raise ServiceError(f"数据集生成失败：{exc}") from exc
    names = {s.name for s in series}
    from market_risk.config import ConfigError

    try:
        result = market.build_dataset(ctx.paths, series, accept_revisions,
                                      conflicts if names & {"S5FI", "S5TW"} else None, now,
                                      decisions=load_data_decisions() if decisions is None else decisions)
    except (ConfigError, ValueError, market.MarketDataError) as exc:   # 裁定表有误：显示原因，不抛异常栈
        raise ServiceError(f"数据集生成失败（config/data_decisions.yaml 或数据集有误）：{exc}") from exc
    rev_path = None
    if result.revisions or result.missing_old_dates:
        stamp = (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
        _write_report(ctx.paths.market_revisions_md, market.render_revisions(result, stamp))
        rev_path = ctx.paths.market_revisions_md
    return DataBuildReport(result, day, offline, rev_path, cutoff_note)


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
               save_raw: Path | None = None, now: dt.datetime | None = None) -> FetchResult:  # pragma: no cover - 联网
    """fetch = 按需下载并生成数据集（B1-2）+ 正式样本的 ALFRED 基准日版本，再从 data/market/ 截断到基准日生成快照。"""
    from market_risk.data import market
    from market_risk.data.raw_io import save_raw_inputs
    from market_risk.data.snapshot import build_snapshot

    _check_mode(mode)
    resolve_base_date(base, mode, now)
    notes: list[str] = []
    if refresh or market.coverage_error(ctx.paths, ctx.settings, base):
        report = data_build(ctx, refresh=refresh, now=now)
        if report.result.revisions:
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


def resolve_base_date(base: dt.date | None, mode: str, now: dt.datetime | None = None) -> dt.date:
    from market_risk.data.market import NEW_YORK, MarketDataError, validate_score_base

    _check_mode(mode)
    if base is None and mode != "daily":
        raise ServiceError("回测模式必须提供基准日")
    day = base or (now or dt.datetime.now(dt.UTC)).astimezone(NEW_YORK).date()
    try:
        validate_score_base(day, now)
    except MarketDataError as exc:
        raise ServiceError(str(exc)) from exc
    return day


def score_raw(ctx: Context, raw: Any, git: Any = None) -> Any:
    """对已取得的原始数据评分并写运行目录（离线可用）。返回 pipeline.RunOutcome。"""
    from market_risk.pipeline import run_scoring
    from market_risk.storage import runs

    return run_scoring(raw, ctx.settings, ctx.paths, git or runs.git_info(ctx.paths.root), load_holidays())


def score_date(ctx: Context, base: dt.date | None, mode: str = "backtest",
               breadth: BreadthInput | None = None, now: dt.datetime | None = None) -> Any:
    """从 data/market/ 读取数据（不直接读接口缓存）、计算两个版本的评分，生成运行目录下的全部输出。

    传入的广度读数先写入 data/manual/breadth.csv，再离线更新数据集中的 S5FI、S5TW。
    数据集未覆盖基准日时报错，提示先运行 fetch。返回 pipeline.RunOutcome。
    """
    day = resolve_base_date(base, mode, now)
    breadth = breadth or BreadthInput()
    record_breadth_inputs(ctx, day, breadth)
    if any(v is not None for v in (breadth.s5fi, breadth.s5tw, breadth.s5fi_t5, breadth.s5tw_t5)):
        data_build(ctx, offline=True, only=("S5FI", "S5TW"), end=max(day, _last_completed(now)), now=now)
    return score_raw(ctx, load_market_inputs(ctx, day, mode))


def _last_completed(now: dt.datetime | None = None) -> dt.date:
    from market_risk.data.market import last_completed_trading_day

    return last_completed_trading_day(now)


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


@dataclass(frozen=True)
class PriceReviewReport:
    results: tuple[ReviewResult, ...]
    text: str
    path: Path
    impacts: tuple[Any, ...] = ()          # price_impact.ImpactResult
    majority: Mapping[tuple[str, dt.date], Any] = field(default_factory=dict)   # price_review.MajorityResult
    implied: Mapping[tuple[str, dt.date], Any] = field(default_factory=dict)    # price_review.ImpliedPrice


def review_price_disputes(
    ctx: Context, refresh: bool = False, config: ReviewConfig | None = None, inputs: ReviewInputs | None = None,
    third_party: tuple[ThirdPartyLoader | None, str] | None = None, run_impact: bool = True,
) -> PriceReviewReport:
    """固定残差法审查 + 第三方多数一致 + 隐含价格 + 实质影响检验；网络数据只用于审计，不调用标签。"""
    from market_risk.data.price_review import (
        ReviewResult,
        implied_price,
        load_review_config,
        majority_verdict,
        published_prices,
        render_review,
        review_case,
    )
    from market_risk.data.price_review_inputs import collect_review_inputs
    from market_risk.data.tiingo import verify_unadjusted
    from market_risk.price_impact import render_impact

    config = config or load_review_config()
    inputs = inputs or collect_review_inputs(ctx.paths, ctx.settings, config, refresh)
    infos = {info.symbol: info for info in load_symbols().values()}
    results = []
    implied = {}
    for symbol, day in config.cases:
        benchmark = config.benchmarks[symbol]
        error = inputs.errors.get(symbol) or inputs.errors.get(benchmark)
        if symbol not in inputs.dividends:
            error = error or "除息记录未取得，不能假定无除息日"
        if error:
            result = ReviewResult(symbol, day, mcal.shift_trading_days(day, 1), benchmark, None, None, (),
                                  "无法判定", "不适用", error)
        else:
            result = review_case(symbol, day, inputs.tv.get(symbol, {}), inputs.yahoo.get(symbol, {}),
                                 inputs.tv.get(benchmark, {}), inputs.yahoo_indices.get(benchmark, {}),
                                 inputs.dividends.get(symbol, frozenset()), config)
        results.append(result)
        disputed = frozenset(d for s, d in config.cases if s == symbol and d != day)
        index = inputs.tv.get(benchmark, {})
        dividends = inputs.dividends.get(symbol, frozenset())
        implied[symbol, day] = tuple(
            implied_price(day, etf, index, name, dividends, disputed)
            for name, etf in (("TradingView", inputs.tv.get(symbol, {})), ("Yahoo", inputs.yahoo.get(symbol, {}))))

    # 第三方：先用已知读数验证不复权，再按多数一致口径判定（争议日与次日）
    loader, source = third_party if third_party is not None else _default_third_party(ctx, refresh)
    notes: dict[tuple[str, dt.date], str] = {}
    majority = {}
    for symbol in sorted({s for s, _ in config.cases}):
        days = [d for s, d in config.cases if s == symbol]
        if loader is None:
            notes.update({(symbol, d): source for d in days})
            continue
        known = infos[symbol].known_values or {}
        end = max([mcal.shift_trading_days(max(days), 1), *known])
        try:
            third = published_prices(loader(symbol, min(days), end))
        except Exception as exc:  # 第三方失败只写入报告
            notes.update({(symbol, d): f"第三方数据获取失败：{exc}" for d in days})
            continue
        problems = verify_unadjusted({d: float(v) for d, v in third.items()}, known) if known \
            else ["没有已知读数，无法验证是否不复权"]
        if problems:
            notes.update({(symbol, d): f"{source} 未通过不复权验证（{'；'.join(problems)}），不采用" for d in days})
            continue
        for day in days:
            nxt = mcal.shift_trading_days(day, 1)
            notes[symbol, day] = f"{source} 已用已知读数验证为不复权（{len(known)} 个读数全部相符）"
            majority[symbol, day] = majority_verdict(
                tuple(inputs.tv.get(symbol, {}).get(t) for t in (day, nxt)),  # type: ignore[arg-type]
                tuple(inputs.yahoo.get(symbol, {}).get(t) for t in (day, nxt)),  # type: ignore[arg-type]
                tuple(third.get(t) for t in (day, nxt)), config, source)  # type: ignore[arg-type]

    corrections = tuple((d.symbol, d.date, str(d.corrected_value), d.evidence_source or "", str(d.decided_on))
                        for d in load_data_decisions() if d.decision == "correct")
    impacts = price_dispute_impacts(ctx, config, inputs) if run_impact else ()
    sections = ("\n".join(render_impact(impacts)),) if impacts else ()
    verdicts = {(i.symbol, i.date): i.verdict for i in impacts}
    text = render_review(tuple(results), config, list(inputs.provenance), notes, corrections, sections,
                         majority, implied, verdicts)
    _write_report(ctx.paths.price_dispute_review_md, text)
    return PriceReviewReport(tuple(results), text, ctx.paths.price_dispute_review_md, tuple(impacts),
                             majority, implied)


def price_dispute_impacts(ctx: Context, config: ReviewConfig, inputs: ReviewInputs) -> tuple[Any, ...]:
    """实质影响检验：每个争议日期分别用 Yahoo 来源原值与 TradingView 收盘价计分并比较（第二部分第1项）。"""
    from market_risk.data.market import MarketDataError, load_market_series
    from market_risk.price_impact import dispute_impact

    try:
        series = load_market_series(ctx.paths, ctx.settings)
    except MarketDataError as exc:
        raise ServiceError(f"实质影响检验需要数据集：{exc}") from exc
    out = []
    for symbol, day in config.cases:
        a, b = inputs.yahoo.get(symbol, {}).get(day), inputs.tv.get(symbol, {}).get(day)
        if a is None or b is None:
            continue
        out.append(dispute_impact(series, ctx.paths, ctx.settings, symbol, day, a, b,
                                  decisions=load_data_decisions()))
    return tuple(out)


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


def _default_close_loader(ctx: Context, refresh: bool) -> CloseLoader:
    """结果标签的收盘价从数据集 data/market/ 读取（SPX = Yahoo ^GSPC，QQQ 含已批准的人工修正）；不联网。"""
    from market_risk.data.market import MarketDataError, read_series_file

    def load(name: str, start: dt.date, end: dt.date) -> Mapping[dt.date, float]:
        path = ctx.paths.market_daily_file(name)
        if not path.exists():
            raise MarketDataError(f"数据集缺少 {name}，请先运行 market-risk data build")
        rows = read_series_file(path, exact=True)[1]
        return {d: r["value"] for d, r in rows.items() if start <= d <= end and r["value"] is not None}

    return load


def outcome_compute(ctx: Context, base: dt.date, refresh: bool = False, loader: CloseLoader | None = None,
                    today: dt.date | None = None) -> OutcomeResult:
    """结果窗口结束后，用标普500指数与 QQQ 的收盘价计算风险事件标签（SOP 9.3）。

    收盘价读取数据集的 SPX、QQQ（refresh 参数保留兼容，不再联网）；计算为 Decimal，取整前判定事件与接近事件。
    """
    from market_risk.data.cache import today_new_york
    from market_risk.outcomes import OutcomeError, compute_outcome, outcome_window, record_outcome, window_finished

    today = today or today_new_york()
    _, end = outcome_window(base)
    if not window_finished(base, today):
        raise ServiceError(f"{base} 的结果窗口到 {end} 才结束，现在不得计算标签")
    load = loader or _default_close_loader(ctx, refresh)
    try:
        outcome = compute_outcome(base, load("SPX", base, end), load("QQQ", base, end), today)
    except (OutcomeError, ValueError) as exc:
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


# ---------------------------------------------------------------------------
# 数据库：SQL 导出与 MySQL 兼容性验证（docs/decisions/0001）
# ---------------------------------------------------------------------------


def db_export_sql(ctx: Context) -> list[Path]:
    """由 Alembic 最新表结构与 config/ 下的 YAML 导出 db/sql/ 的 SQL 文件。"""
    from market_risk.storage import sql_export

    try:
        return sql_export.export_sql(ctx.paths.db_sql_dir)
    except (sql_export.SchemaMismatchError, ValueError) as exc:
        raise ServiceError(str(exc)) from exc


def db_verify_mysql(ctx: Context, url: str | None = None) -> Any | None:
    """在 MYSQL_VERIFY_URL 指向的专用库上验证兼容性；未设置时返回 None（跳过，不报错）。

    连接地址不输出到任何地方；返回 mysql_verify.VerifyReport。
    """
    from market_risk.config import get_optional_secret
    from market_risk.storage import mysql_verify

    url = url or get_optional_secret("MYSQL_VERIFY_URL")
    if not url:
        return None
    url = mysql_verify.with_driver(url)
    try:
        return mysql_verify.verify(ctx.paths, url, ctx.paths.db_sql_dir)
    except mysql_verify.VerifyError as exc:
        raise ServiceError(f"MySQL 兼容性验证失败：{exc}") from exc


# ---------------------------------------------------------------------------
# 阶段6：逐日历史回测
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestRunReport:
    run_id: str
    run_dir: Path
    start: dt.date
    end: dt.date
    days: int
    runtime_seconds: float
    counts: dict[str, int]              # 各文件行数


def _corrections_used(ctx: Context) -> list[tuple[str, dt.date]]:
    """数据集中已应用的人工修正（标的, 日期），用于"使用人工修正值"标记。"""
    from market_risk.data.market import read_manifest

    out = []
    for name, entry in read_manifest(ctx.paths).get("series", {}).items():
        out += [(name, dt.date.fromisoformat(c["date"])) for c in entry.get("corrections", [])]
    return sorted(out)


def backtest_run(ctx: Context, start: dt.date | None = None, end: dt.date | None = None,
                 versions: Collection[str] = ("v2-M", "v3-R1"), unlock_holdout: bool = False,
                 git: Any = None, now: dt.datetime | None = None,
                 progress: Callable[[int, int], None] | None = None) -> BacktestRunReport:
    """逐日计算评分与指标，再计算结果标签与回调事件标签，写入新的回测运行目录（不覆盖）。"""
    from market_risk.backtest import output
    from market_risk.backtest.engine import VERSIONS, price_decimal, run_backtest
    from market_risk.backtest.labels import build_episodes, episode_windows, outcome_rows
    from market_risk.backtest.settings import load_backtest_config
    from market_risk.data.market import MarketDataError, load_market_series, manifest_sha256
    from market_risk.storage import backtests, runs
    from market_risk.storage.paths import make_run_id, unique_run_id

    bad = set(versions) - set(VERSIONS)
    if bad or not versions:
        raise ServiceError(f"版本只能是 {'、'.join(VERSIONS)}")
    cfg = load_backtest_config()
    if start is not None and start < cfg.start:
        raise ServiceError(f"--from {start} 早于回测配置起点 {cfg.start}，请重新指定")
    try:
        series = load_market_series(ctx.paths, ctx.settings)
        for name in ("SPX", "QQQ"):
            if name not in series.rows:
                series.rows[name] = load_market_series_extra(ctx, name)  # type: ignore[index]
    except MarketDataError as exc:
        raise ServiceError(str(exc)) from exc
    git = git or runs.git_info(ctx.paths.root)
    created = now or dt.datetime.now(dt.UTC)
    existing = backtests.list_runs(ctx.paths)
    run_id = unique_run_id(make_run_id(created, git.commit or runs.UNKNOWN_COMMIT), existing)

    result = run_backtest(series, ctx.paths, ctx.settings, cfg, start, end, tuple(versions),
                          _corrections_used(ctx), progress, load_data_decisions())
    t_labels = dt.datetime.now(dt.UTC)
    bases = [d.date for d in result.days]
    spx = dict(price_decimal(series, "SPX"))
    qqq = dict(price_decimal(series, "QQQ"))
    outcomes = outcome_rows(bases, spx, qqq, cfg, unlock_holdout)
    scores = output.score_index(result.days)
    episodes, windows = [], []
    for sym, closes in (("SPX", sorted(spx.items())), ("QQQ", sorted(qqq.items()))):
        days = [d for d, _ in closes]
        for ep in build_episodes(sym, closes, cfg, unlock_holdout):
            episodes.append(ep)
            if ep.period != "保留期" or unlock_holdout:
                windows += episode_windows(ep, days, scores, result.versions, cfg, unlock_holdout, dict(closes))

    run_dir = ctx.paths.backtest_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    backtests.write_csv(backtests.run_file(ctx.paths, run_id, "daily_scores"), backtests.DAILY_SCORE_FIELDS,
                        output.daily_score_rows(result.days))
    fields, metric_rows = output.daily_metric_rows(result.days)
    backtests.write_csv(backtests.run_file(ctx.paths, run_id, "daily_metrics"), fields, metric_rows)
    backtests.write_csv(backtests.run_file(ctx.paths, run_id, "outcomes"), backtests.OUTCOME_FIELDS,
                        [output.outcome_row(o) for o in outcomes])
    backtests.write_csv(backtests.run_file(ctx.paths, run_id, "episodes"), backtests.EPISODE_FIELDS,
                        [output.episode_row(e) for e in episodes])
    backtests.write_csv(backtests.run_file(ctx.paths, run_id, "windows"), backtests.WINDOW_FIELDS,
                        [output.window_row(w) for w in windows])
    runtime = result.runtime_seconds + (dt.datetime.now(dt.UTC) - t_labels).total_seconds()
    meta = {
        "run_id": run_id, "created_at_utc": created.astimezone(dt.UTC).isoformat(timespec="seconds"),
        "git_commit": git.commit, "git_dirty": git.dirty,
        "market_manifest_sha256": manifest_sha256(ctx.paths),
        "rule_versions": list(result.versions), "versions": list(result.versions),
        "start": result.start, "end": result.end, "days": len(result.days),
        "start_reason": ("S5FI、S5TW 都有数值从 2008-08-04 起，且基准日的 T−5 须有广度读数："
                         "第一个满足的基准日为 2008-08-11"),
        "periods": {"development": list(cfg.development), "validation": list(cfg.validation),
                    "holdout_start": cfg.holdout_start},
        "config": {"path": "config/backtest.yaml", "config_version": cfg.config_version, "sha256": cfg.sha256},
        "runtime_seconds": round(runtime, 1),
        "holdout_unlocked": unlock_holdout,
        "holdout_unlocked_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds") if unlock_holdout else None,
        "corrections_used": [{"symbol": s, "date": d} for s, d in _corrections_used(ctx)],
        "counts": {"daily_scores": sum(len(d.results) for d in result.days), "daily_metrics": len(result.days),
                   "outcomes": len(outcomes), "pullback_episodes": len(episodes), "episode_windows": len(windows)},
    }
    backtests.write_meta(ctx.paths, run_id, meta)
    (run_dir / "README.md").write_text(output.readme_text(meta), encoding="utf-8")
    return BacktestRunReport(run_id, run_dir, result.start, result.end, len(result.days), runtime, meta["counts"])


def load_market_series_extra(ctx: Context, name: str) -> dict[dt.date, dict[str, Any]]:
    from market_risk.data.market import MarketDataError, read_series_file

    path = ctx.paths.market_daily_file(name)
    if not path.exists():
        raise MarketDataError(f"数据集缺少 {name}，请先运行 market-risk data build")
    return read_series_file(path, exact=True)[1]


def backtest_set_official(ctx: Context, run_id: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """设置正式回测指针，并重写 backtests/.gitignore（只放行正式回测的运行目录）。"""
    from market_risk.storage import backtests

    try:
        pointer = backtests.set_official(ctx.paths, run_id, now=now)
    except FileNotFoundError as exc:
        raise ServiceError(str(exc)) from exc
    _rebuild_db(ctx)
    return pointer


@dataclass(frozen=True)
class BacktestReport:
    run_id: str
    text: str
    path: Path


def backtest_report(ctx: Context, run_id: str | None = None) -> BacktestReport:
    """由正式回测（或指定运行）生成 reports/backtest_baseline.md（只用开发期与验证期）。"""
    from market_risk.backtest.report import render_baseline
    from market_risk.backtest.settings import load_backtest_config
    from market_risk.storage import backtests

    run_id = run_id or (backtests.read_official(ctx.paths) or {}).get("run_id")
    if not run_id:
        raise ServiceError("尚未设置正式回测；请用 --run 指定运行编号，或先运行 backtest official")
    if run_id not in backtests.list_runs(ctx.paths):
        raise ServiceError(f"回测运行目录不存在：{run_id}")
    meta = backtests.read_meta(ctx.paths, run_id)
    text = render_baseline(meta, load_backtest_config(),
                           backtests.read_csv(backtests.run_file(ctx.paths, run_id, "daily_scores")),
                           backtests.read_csv(backtests.run_file(ctx.paths, run_id, "episodes")),
                           backtests.read_csv(backtests.run_file(ctx.paths, run_id, "outcomes")))
    _write_report(ctx.paths.backtest_baseline_md, text)
    return BacktestReport(run_id, text, ctx.paths.backtest_baseline_md)


def zigzag_check(ctx: Context, symbol: str, level: str, start: dt.date, end: dt.date) -> list[Any]:
    """只输出由收盘价计算的价格波段（不涉及分数、预警状态或结果标签，允许包含保留期）。"""
    from decimal import Decimal, InvalidOperation

    from market_risk.backtest.settings import load_backtest_config
    from market_risk.backtest.zigzag_check import price_swings

    symbol = symbol.upper()
    cfg = load_backtest_config()
    if symbol not in cfg.grades:
        raise ServiceError(f"标的只能是 {'、'.join(sorted(cfg.grades))}")
    try:
        lv = Decimal(level)
    except InvalidOperation as exc:
        raise ServiceError(f"层级格式错误：{level}（如 0.05）") from exc
    if not (0 < lv < 1):
        raise ServiceError("层级应在 0 与 1 之间，如 0.05")
    if not ctx.paths.market_daily_file(symbol).exists():
        raise ServiceError(f"数据集缺少 {symbol}，请先运行 market-risk data build")
    return price_swings(ctx.paths, symbol, lv, cfg.grades[symbol], start, end)


@dataclass(frozen=True)
class IndexImpactReport:
    impacts: tuple[Any, ...]            # backtest.labels.LabelImpact
    text: str
    path: Path


def index_dispute_label_impact(ctx: Context, tolerance: str = "0.02") -> IndexImpactReport:
    """指数争议日（回测区间内 SPX、NDX 与 TradingView 两位小数差值绝对值>0.02 点）对结果标签与回调事件的实质影响。

    分别用 Yahoo（数据集）与 TradingView 的指数值计算，比较是否有差异；只报告，不修正。
    """
    from decimal import Decimal

    from market_risk.backtest.engine import price_decimal
    from market_risk.backtest.labels import label_impact
    from market_risk.backtest.settings import load_backtest_config
    from market_risk.data import tradingview
    from market_risk.data.market import load_market_series
    from market_risk.outcomes import published

    cfg = load_backtest_config()
    series = load_market_series(ctx.paths, ctx.settings)
    for name in ("SPX", "QQQ", "NDX"):
        if name not in series.rows:
            series.rows[name] = load_market_series_extra(ctx, name)  # type: ignore[index]
    spx, qqq = dict(price_decimal(series, "SPX")), dict(price_decimal(series, "QQQ"))
    tol = Decimal(tolerance)
    impacts = []
    for name in ("SPX", "NDX"):
        yahoo = dict(price_decimal(series, name))
        tv = {d: published(v) for d, v in tradingview.read_processed(ctx.paths, name).items()}
        for d in sorted(set(yahoo) & set(tv)):
            if d >= cfg.start and abs(tv[d] - yahoo[d]) > tol:
                imp = label_impact(name, d, tv[d], spx, qqq, cfg)
                impacts.append(imp if name == "SPX" else
                               type(imp)(name, d, yahoo[d], tv[d], 0, (), ()))
    lines = ["# 指数争议日的标签实质影响检验", "",
             f"回测区间（{cfg.start} 起）内 SPX、NDX 与 TradingView 两位小数差值绝对值 >{tolerance} 点的日期，"
             "分别用 Yahoo（数据集）与 TradingView 的指数值计算受影响的结果标签（结果窗口包含该日的基准日）"
             "与回调事件（全部层级），比较是否有差异。NDX 不参与结果标签与回调事件，无影响。只报告，不修正。", "",
             "| 指数 | 日期 | Yahoo | TradingView | 受影响基准日数 | 结果标签差异 | 回调事件差异 | 结论 |",
             "|---|---|---|---|---|---|---|---|"]
    for i in impacts:
        incomplete = ("（两种来源均数据不齐、无法生成标签：" + "、".join(map(str, i.incomplete_bases)) + "）"
                      if i.incomplete_bases else "")
        lines.append(f"| {i.symbol} | {i.date} | {i.value_a} | {i.value_b} | {i.outcome_bases} | "
                     f"{'；'.join(i.outcome_diffs) or '无'}{incomplete}"
                     f" | {'；'.join(i.episode_diffs) or '无'} | "
                     f"{'有实质影响' if i.material else '无实质影响'} |")
    text = "\n".join(lines) + "\n"
    path = ctx.paths.reports_dir / "index_dispute_label_impact.md"
    _write_report(path, text)
    return IndexImpactReport(tuple(impacts), text, path)
