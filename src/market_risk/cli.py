"""命令行入口（SPEC 第8节）。"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from market_risk import calendar as mcal
from market_risk.config import get_fred_api_key, load_holidays, load_settings
from market_risk.data.snapshot import build_snapshot
from market_risk.models import MarketSnapshot
from market_risk.storage.paths import StoragePaths

app = typer.Typer(help="美股大盘风险评分：数据准备、机械计算与 prompt 生成", no_args_is_help=True)


@app.callback()
def main() -> None:
    """美股大盘风险评分（v2-M 与 v3-R1 并行）。"""


def _parse_date(value: str) -> dt.date:
    try:
        if len(value) != 10:
            raise ValueError(value)
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dt.date):
        return obj.isoformat()
    if isinstance(obj, tuple | list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


@app.command()
def dates(date: Annotated[str, typer.Option("--date", help="基准日 YYYY-MM-DD")]) -> None:
    """计算基准日的全部日期参照（离线：债市日历暂按 holidays.yaml 推出）。"""
    base = _parse_date(date)
    holidays = load_holidays()
    # 债市日历覆盖范围：足够覆盖 O6 与 T−45 的区间
    bond_cal = mcal.bond_calendar_from_holidays(
        holidays, base - dt.timedelta(days=120), base
    )
    try:
        refs = mcal.compute_date_references(base, bond_cal)
    except mcal.CalendarError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    result = _jsonable(dataclasses.asdict(refs))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    typer.echo(
        "注意：债市营业日暂按 config/holidays.yaml 推出，未经财政部数据核实；"
        "v2-M 的 O6 需接入 FRED 观测列表后才能确定。",
        err=True,
    )
    for note in mcal.check_stock_calendar(holidays, refs.three_segment_query_start, base):
        typer.echo(f"日历核对：{note}", err=True)


tv_app = typer.Typer(help="TradingView 导出数据：导入、校验、列表（docs/TRADINGVIEW.md）")
app.add_typer(tv_app, name="tv")


def _parse_datetime(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"时间格式应为 ISO，如 2026-09-26T15:30-04:00：{value}") from exc
    if parsed.tzinfo is None:
        raise typer.BadParameter("导出时间必须带时区偏移，如 2026-09-26T15:30-04:00")
    return parsed


@tv_app.command("import")
def tv_import(
    directory: Annotated[Path, typer.Option("--dir", help="原始文件目录 raw/<导出日期>/")],
    export_date: Annotated[
        str | None, typer.Option("--export-date", help="导出日期（默认取目录名）")
    ] = None,
    export_time: Annotated[
        str | None,
        typer.Option("--export-time", help="导出时间（带时区；默认取文件修改时间），用于判断不完整K线"),
    ] = None,
) -> None:
    """导入并校验一个目录下的全部 TradingView 导出文件，打印汇总表。"""
    from market_risk.config import load_symbols
    from market_risk.data import tradingview as tv

    settings = load_settings()
    paths = StoragePaths(settings.storage_root)
    try:
        result = tv.import_directory(
            directory,
            paths,
            load_symbols(),
            _parse_date(export_date) if export_date else None,
            _parse_datetime(export_time) if export_time else None,
        )
    except tv.TradingViewError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(tv.format_import_summary(result))
    if any(r.status == tv.FAILED for r in result.reports) or result.merge_errors:
        raise typer.Exit(code=2)


@tv_app.command("validate")
def tv_validate(
    symbol: Annotated[str | None, typer.Option("--symbol", help="只校验该标的")] = None,
) -> None:
    """按 manifest 重新读取并校验已导入的原始文件，重建清洗结果。"""
    from market_risk.config import load_symbols
    from market_risk.data import tradingview as tv

    paths = StoragePaths(load_settings().storage_root)
    manifest = tv.read_manifest(paths.tv_manifest)
    if not manifest:
        typer.echo("尚未导入任何 TradingView 文件")
        return
    result = tv.rebuild(paths, load_symbols(), manifest)
    if symbol:
        result.reports = [r for r in result.reports if r.symbol.upper() == symbol.upper()]
    typer.echo(tv.format_import_summary(result))


@tv_app.command("list")
def tv_list() -> None:
    """列出已导入的标的、起止日期、校验状态。"""
    from market_risk.data import tradingview as tv

    typer.echo(tv.format_list(StoragePaths(load_settings().storage_root)))



def _run_compare(infos: list, paths: StoragePaths, settings: Any, refresh: bool) -> list:  # pragma: no cover - 联网
    from market_risk.data import tradingview as tv
    from market_risk.data.tv_compare import CompareResult, compare_series, fetch_api_series

    try:
        key: str | None = get_fred_api_key()
    except Exception:
        key = None
    results = []
    for info in infos:
        series = tv.read_processed(paths, info.symbol)
        if not series:
            results.append(CompareResult(info.symbol, info.tv_symbol, info.api_source or "", info.tolerance,
                                         error="尚未导入该标的的 TradingView 数据"))
            continue
        try:
            api = fetch_api_series(info.api_source, min(series), max(series), settings, paths, key, refresh)
        except Exception as exc:
            results.append(CompareResult(info.symbol, info.tv_symbol, info.api_source or "", info.tolerance,
                                         error=f"接口数据获取失败：{exc}"))
            continue
        results.append(compare_series(info, series, api, info.tolerance))
    return results


@tv_app.command("compare")
def tv_compare(
    symbol: Annotated[str, typer.Option("--symbol", help="标的，如 BAMLH0A0HYM2")],
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略接口缓存")] = False,
) -> None:  # pragma: no cover - 联网
    """与接口数据做重叠比对，结果写入 reports/tradingview_compare_<标的>.md。"""
    from market_risk.config import load_symbols
    from market_risk.data.tv_compare import format_results

    settings = load_settings()
    paths = StoragePaths(settings.storage_root)
    infos = [s for s in load_symbols().values() if s.symbol.upper() == symbol.upper()]
    if not infos or not infos[0].api_source:
        typer.echo(f"错误：{symbol} 未在 config/symbols.yaml 登记 api_source", err=True)
        raise typer.Exit(code=1)
    results = _run_compare(infos, paths, settings, refresh)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    text = format_results(results, f"TradingView 与接口数据重叠比对：{infos[0].symbol}", now)
    out = paths.tv_compare_md(infos[0].symbol)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(text)
    typer.echo(f"已写入 {out}")


@tv_app.command("crosscheck")
def tv_crosscheck(
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略接口缓存")] = False,
) -> None:  # pragma: no cover - 联网
    """全部 crosscheck 标的与接口数据比对，写入 reports/tradingview_crosscheck.md。"""
    from market_risk.config import load_symbols
    from market_risk.data.tv_compare import format_results

    settings = load_settings()
    paths = StoragePaths(settings.storage_root)
    infos = [s for s in load_symbols().values() if s.usage == "crosscheck" and s.api_source]
    results = _run_compare(infos, paths, settings, refresh)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    text = format_results(results, "TradingView 交叉校验", now)
    paths.tv_crosscheck_md.parent.mkdir(parents=True, exist_ok=True)
    paths.tv_crosscheck_md.write_text(text, encoding="utf-8")
    typer.echo(text)
    typer.echo(f"已写入 {paths.tv_crosscheck_md}")

def format_snapshot_summary(snap: MarketSnapshot) -> str:
    """快照的文字摘要（供 fetch 命令核对数据）。"""
    r = snap.refs

    def f(x: float | None, nd: int = 2) -> str:
        return "缺失" if x is None else f"{x:.{nd}f}"

    lines = [
        f"基准日 {r.base_date}（提前收盘={r.is_early_close}）  T−5 {r.t_minus_5}  "
        f"T−20 {r.t_minus_20}  窗口 {r.window_start}至{r.window_end}",
        f"OAS：v3-R1 O1={r.oas_o1} {f(snap.oas_o1)}  O6={r.oas_o6_v3r1} {f(snap.oas_o6_v3r1)}；"
        f"v2-M O1={r.oas_o1_v2m} {f(snap.oas_o1_v2m)}  O6={r.oas_o6_v2m} {f(snap.oas_o6_v2m)}",
        "",
        "标的   收盘     MA5      MA10     MA20     MA30     MA50     MA200",
    ]
    for s, e in snap.etfs.items():
        lines.append(
            f"{s:<5} {e.close:8.2f} {e.ma5:8.2f} {e.ma10:8.2f} {e.ma20:8.2f} "
            f"{e.ma30:8.2f} {e.ma50:8.2f} {e.ma200:8.2f}"
        )
    lines += [
        f"SPY 窗口最高收盘 {snap.spy_window_max_close:.2f}   HYG/LQD {f(snap.hyg_lqd, 4)}",
        f"VIX {f(snap.vix)}（T−5 {f(snap.vix_t5)}）",
        f"10年期 y={f(snap.y)}  H={f(snap.h)}（{', '.join(map(str, snap.h_dates))}）  "
        f"T−20 y={f(snap.y_t20)}",
        "",
        "三环节（d1 含 T−20）：",
    ]
    for res in snap.three_segment[True]:
        step1 = [f"{t.d1}({t.d1_close}<Lc {t.lc}@{t.lc_date})" for t in res.traces if t.step1]
        lines.append(
            f"  {res.symbol}：完成={res.completed}；第一步成立：{'、'.join(step1) or '无'}"
        )
    if snap.data_notes:
        lines += ["", "数据说明："] + [f"  - {n}" for n in snap.data_notes]
    return "\n".join(lines)


@app.command()
def fetch(
    date: Annotated[str, typer.Option("--date", help="基准日 YYYY-MM-DD")],
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略缓存，强制重新下载")] = False,
    mode: Annotated[str, typer.Option("--mode", help="backtest 或 daily")] = "backtest",
    save_raw: Annotated[
        Path | None, typer.Option("--save-raw", help="把原始数据另存到该目录（制作离线测试数据用）")
    ] = None,
) -> None:  # pragma: no cover - 网络请求
    """下载基准日所需的全部数据，截断到基准日并显示核对摘要。"""
    from market_risk.data.fetch import fetch_raw_inputs
    from market_risk.data.raw_io import save_raw_inputs

    base = _parse_date(date)
    if mode not in {"backtest", "daily"}:
        raise typer.BadParameter("--mode 只能是 backtest 或 daily")
    settings = load_settings()
    paths = StoragePaths(settings.storage_root)
    raw = fetch_raw_inputs(base, settings, paths, get_fred_api_key(), mode, refresh)
    if save_raw is not None:
        save_raw_inputs(raw, save_raw)
        typer.echo(f"原始数据已保存到 {save_raw}", err=True)
    snap = build_snapshot(
        raw, settings.scored_symbols, tuple(settings.reference_symbols[:2]), load_holidays()
    )
    typer.echo(format_snapshot_summary(snap))
    typer.echo("\n数据来源：")
    for s in raw.sources:
        typer.echo(
            f"  - {s.source}:{s.key} {s.data_start}至{s.data_end} {s.rows}行 "
            f"{'缓存' if s.from_cache else '下载'}于 {s.downloaded_at_utc}"
        )


# ---------------------------------------------------------------------------
# 阶段4：score / validate / samples
# ---------------------------------------------------------------------------


def _record_breadth(paths: StoragePaths, day: dt.date, s5fi: float | None, s5tw: float | None) -> None:
    """命令行传入的广度读数同时写入 data/manual/breadth.csv（SPEC 6.5）。"""
    from market_risk.data.breadth import BreadthError, make_reading, upsert_breadth

    if s5fi is None and s5tw is None:
        return
    if s5fi is None or s5tw is None:
        raise typer.BadParameter(f"{day} 的 S5FI 与 S5TW 需要同时提供")
    try:
        upsert_breadth(paths.breadth_csv, make_reading(day, s5fi, s5tw, "score 命令录入"))
    except BreadthError as exc:
        typer.echo(f"错误：{exc}（如需修改，请用 breadth add --overwrite）", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def score(
    date: Annotated[str | None, typer.Option("--date", help="基准日；daily 模式默认为今天（美东）")] = None,
    s5fi: Annotated[float | None, typer.Option("--s5fi", help="基准日 S5FI（百分数）")] = None,
    s5tw: Annotated[float | None, typer.Option("--s5tw", help="基准日 S5TW（百分数）")] = None,
    s5fi_t5: Annotated[float | None, typer.Option("--s5fi-t5", help="5个交易日前 S5FI")] = None,
    s5tw_t5: Annotated[float | None, typer.Option("--s5tw-t5", help="5个交易日前 S5TW")] = None,
    mode: Annotated[str, typer.Option("--mode", help="backtest 或 daily")] = "backtest",
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略缓存，强制重新下载")] = False,
) -> None:  # pragma: no cover - 联网；离线部分见 pipeline 测试
    """下载数据、计算两个版本的评分，生成运行目录下的全部输出。"""
    from market_risk.data.cache import today_new_york
    from market_risk.data.fetch import fetch_raw_inputs
    from market_risk.pipeline import run_scoring
    from market_risk.storage.runs import git_info

    if mode not in {"backtest", "daily"}:
        raise typer.BadParameter("--mode 只能是 backtest 或 daily")
    if date is None and mode != "daily":
        raise typer.BadParameter("回测模式必须提供 --date")
    base = _parse_date(date) if date else today_new_york()
    if not mcal.is_stock_trading_day(base):
        typer.echo(f"错误：{base} 不是股票交易日", err=True)
        raise typer.Exit(code=1)
    settings = load_settings()
    paths = StoragePaths(settings.storage_root)
    _record_breadth(paths, base, s5fi, s5tw)
    _record_breadth(paths, mcal.shift_trading_days(base, -5), s5fi_t5, s5tw_t5)
    raw = fetch_raw_inputs(base, settings, paths, get_fred_api_key(), mode, refresh)
    outcome = run_scoring(raw, settings, paths, git_info(settings.storage_root), load_holidays())
    _print_outcome(outcome)


def _print_outcome(outcome: Any) -> None:
    typer.echo(f"运行目录：{outcome.run_dir}")
    typer.echo(f"运行状态：{outcome.status}；{outcome.official_note}")
    for r in outcome.results:
        dims = "、".join(f"{d.name}{d.score if d.score is not None else '待补'}" for d in r.dimensions)
        total = r.total if r.total is not None else f"{r.total_range[0]}–{r.total_range[1]}"
        typer.echo(f"{r.version}：{dims}；总分 {total}；{r.stage or '范围跨越阶段'}；"
                   f"明确恶化={dict(r.clear_deterioration)['大盘明确恶化']}")
        for f in (*r.review_flags, *r.notes):
            typer.echo(f"  {f}")
    for m in outcome.messages:
        typer.echo(f"提示：{m}")


@app.command()
def validate() -> None:
    """用 tests/fixtures 中的历史样本比对程序值与截图值（离线）。"""
    from market_risk.data.tradingview import format_table
    from market_risk.validation import validate_all

    checks = validate_all(load_settings())
    rows = [[c.sample, c.item, c.screenshot, c.program, c.diff, "一致" if c.ok else "不一致"] for c in checks]
    typer.echo(format_table(["样本", "项目", "截图", "程序", "差值", "结果"], rows))
    bad = [c for c in checks if not c.ok]
    typer.echo(f"\n共 {len(checks)} 项，一致 {len(checks) - len(bad)}，不一致 {len(bad)}。")
    if bad:
        raise typer.Exit(code=1)


@app.command()
def samples(year: Annotated[int, typer.Option("--year", help="年份")]) -> None:
    """按 SOP 9.2 列出每月最后一个周五（休市则取当月最后一个交易日）。"""
    for d in mcal.monthly_sample_dates(year):
        note = "" if d.weekday() == 4 else "（最后一个周五休市，取当月最后一个交易日）"
        typer.echo(f"{d}（周{'一二三四五六日'[d.weekday()]}）{note}")


# ---------------------------------------------------------------------------
# 阶段4.5：正式记录、资料、广度录入、结果标签、import-legacy、数据库、统计（STORAGE 第7节）
# ---------------------------------------------------------------------------

official_app = typer.Typer(help="正式记录（official.json）")
material_app = typer.Typer(help="资料管理")
breadth_app = typer.Typer(help="广度读数录入")
outcome_app = typer.Typer(help="风险事件标签（结果窗口结束后）")
app.add_typer(official_app, name="official")
app.add_typer(material_app, name="material")
app.add_typer(breadth_app, name="breadth")
app.add_typer(outcome_app, name="outcome")

MATERIAL_TYPES_HELP = ("tiger_ai_background", "chatgpt_response", "claude_review", "notes", "screenshot", "other")
SubjectOpt = Annotated[str, typer.Option("--subject", help="MARKET 或股票代码")]
FrameworkOpt = Annotated[str, typer.Option("--framework", help="分析框架")]


def _paths() -> StoragePaths:
    return StoragePaths(load_settings().storage_root)


def _rebuild_db(paths: StoragePaths) -> None:
    from market_risk.storage import db

    db.rebuild(paths)


@official_app.command("set")
def official_set(
    date: Annotated[str, typer.Option("--date")],
    run: Annotated[str, typer.Option("--run", help="运行编号 run_...")],
    subject: SubjectOpt = "MARKET",
    framework: FrameworkOpt = "risk_scoring",
) -> None:
    """手动指定正式记录（reviewed 重置为 false，复核后用 official confirm）。"""
    from market_risk.storage.runs import set_official

    paths = _paths()
    try:
        pointer = set_official(paths, subject, framework, _parse_date(date), run, "manual")
    except FileNotFoundError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    _rebuild_db(paths)
    typer.echo(f"正式记录已设为 {pointer['run_id']}（reviewed=false）")


@official_app.command("confirm")
def official_confirm(
    date: Annotated[str, typer.Option("--date")],
    subject: SubjectOpt = "MARKET",
    framework: FrameworkOpt = "risk_scoring",
) -> None:
    """把当前正式记录标记为已复核（reviewed=true）。"""
    from market_risk.storage.runs import confirm_official

    paths = _paths()
    try:
        pointer = confirm_official(paths, subject, framework, _parse_date(date))
    except FileNotFoundError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    _rebuild_db(paths)
    typer.echo(f"{date} 的正式记录 {pointer['run_id']} 已确认（reviewed=true）")


@material_app.command("add")
def material_add(
    date: Annotated[str, typer.Option("--date")],
    kind: Annotated[str, typer.Option("--type", help="资料类型：" + " / ".join(MATERIAL_TYPES_HELP))],
    file: Annotated[Path, typer.Option("--file", help="要登记的文件")],
    subject: SubjectOpt = "MARKET",
    source: Annotated[str, typer.Option("--source")] = "",
    note: Annotated[str, typer.Option("--note")] = "",
) -> None:
    """把资料复制到 data/materials/<对象>/<年>/<日期>/ 并登记索引。"""
    from market_risk.materials import MaterialError, add_material

    paths = _paths()
    try:
        target = add_material(paths, subject, _parse_date(date), kind, file, source, note)
    except MaterialError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    _rebuild_db(paths)
    typer.echo(f"已登记：{target}")


@material_app.command("list")
def material_list(
    subject: SubjectOpt = "MARKET",
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """列出资料。"""
    from market_risk.data.tradingview import format_table
    from market_risk.materials import list_materials

    rows = list_materials(_paths(), subject, _parse_date(start) if start else None, _parse_date(end) if end else None)
    if not rows:
        typer.echo("没有资料")
        return
    typer.echo(format_table(["对象", "日期", "类型", "文件", "来源", "说明"],
                            [[r["subject"], r["base_date"], r["type"], r["file_path"], r["source"], r["note"]]
                             for r in rows]))


@breadth_app.command("add")
def breadth_add(
    date: Annotated[str, typer.Option("--date")],
    s5fi: Annotated[float, typer.Option("--s5fi")],
    s5tw: Annotated[float, typer.Option("--s5tw")],
    note: Annotated[str, typer.Option("--note")] = "",
    overwrite: Annotated[bool, typer.Option("--overwrite", help="同一日期已有不同读数时覆盖")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="覆盖时不再询问")] = False,
) -> None:
    """写入 data/manual/breadth.csv；同一日期重复录入时提示确认。"""
    from market_risk.data.breadth import BreadthError, make_reading, read_breadth, upsert_breadth

    paths = _paths()
    day = _parse_date(date)
    reading = make_reading(day, s5fi, s5tw, note)
    old = read_breadth(paths.breadth_csv).get(day)
    if old is not None and (old.s5fi, old.s5tw) != (reading.s5fi, reading.s5tw):
        if not overwrite:
            typer.echo(f"错误：{day} 已有读数 S5FI={old.s5fi}、S5TW={old.s5tw}；如需修改请加 --overwrite", err=True)
            raise typer.Exit(code=1)
        if not yes and not typer.confirm(f"{day} 已有读数 S5FI={old.s5fi}、S5TW={old.s5tw}，确认覆盖？"):
            raise typer.Exit(code=1)
    try:
        changed = upsert_breadth(paths.breadth_csv, reading, overwrite=True)
    except BreadthError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("已写入" if changed else "读数相同，无变更")


@outcome_app.command("compute")
def outcome_compute(
    date: Annotated[str, typer.Option("--date")],
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
) -> None:  # pragma: no cover - 联网
    """结果窗口结束后，用标普500指数与 QQQ 的收盘价计算风险事件标签（SOP 9.3）。"""
    from market_risk.data.cache import cached_series, today_new_york
    from market_risk.data.prices import close_series_from_frame, yfinance_download
    from market_risk.outcomes import (
        OutcomeError,
        compute_outcome,
        outcome_window,
        record_outcome,
        window_finished,
    )

    settings = load_settings()
    paths = StoragePaths(settings.storage_root)
    base = _parse_date(date)
    today = today_new_york()
    if not window_finished(base, today):
        typer.echo(f"错误：{base} 的结果窗口到 {outcome_window(base)[1]} 才结束，现在不得计算标签", err=True)
        raise typer.Exit(code=1)
    end = outcome_window(base)[1]
    series = {}
    for key, yahoo in (("GSPC", "^GSPC"), ("QQQ", "QQQ")):
        def _dl(y: str = yahoo) -> dict:
            return close_series_from_frame(yfinance_download(y, base, end + dt.timedelta(days=1)), y)

        series[key], _ = cached_series(paths, "yahoo", f"{key}_outcome", base, end, f"yfinance {yahoo}", _dl,
                                       refresh, settings.max_retries, settings.backoff_seconds)
    try:
        outcome = compute_outcome(base, series["GSPC"], series["QQQ"], today)
    except OutcomeError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    diffs = record_outcome(paths.outcomes_csv, outcome)
    _rebuild_db(paths)
    typer.echo(f"{base}：标普500最低收盘跌幅 {outcome.spx_min_close_drawdown}%，QQQ {outcome.qqq_min_close_drawdown}%，"
               f"风险事件={'是' if outcome.is_event else '否'}"
               f"（结果窗口 {outcome.window_start} 至 {outcome.window_end}）")
    for d in diffs:
        typer.echo(f"【差异】{d}")


@outcome_app.command("add")
def outcome_add(
    date: Annotated[str, typer.Option("--date")],
    spx: Annotated[float, typer.Option("--spx", help="标普500最低收盘价跌幅（百分数，如 -5.2）")],
    qqq: Annotated[float, typer.Option("--qqq", help="QQQ 最低收盘价跌幅（百分数）")],
    event_date: Annotated[str | None, typer.Option("--event-date", help="首次达到门槛的日期（可选）")] = None,
) -> None:
    """人工录入风险事件标签（与程序计算值不一致时报告差异）。"""
    from market_risk.data.cache import today_new_york
    from market_risk.outcomes import (
        QQQ_THRESHOLD,
        SPX_THRESHOLD,
        Outcome,
        outcome_window,
        record_outcome,
        window_finished,
    )

    paths = _paths()
    base = _parse_date(date)
    if not window_finished(base, today_new_york()):
        typer.echo(f"错误：{base} 的结果窗口尚未结束", err=True)
        raise typer.Exit(code=1)
    start, end = outcome_window(base)
    o = Outcome("MARKET", base, start, end, spx, qqq, spx <= SPX_THRESHOLD or qqq <= QQQ_THRESHOLD,
                _parse_date(event_date) if event_date else None, "manual",
                dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
    for d in record_outcome(paths.outcomes_csv, o):
        typer.echo(f"【差异】{d}")
    _rebuild_db(paths)
    typer.echo(f"已录入：{base} 风险事件={'是' if o.is_event else '否'}")


@app.command("import-legacy")
def import_legacy_cmd(
    excel: Annotated[Path | None, typer.Option("--excel", help="默认 data/legacy/backtest_record_legacy.xlsx")] = None,
) -> None:
    """导入截图时代的样本（设为正式记录、reviewed=true），与程序记录对照。"""
    from market_risk.legacy import LegacyError, import_legacy
    from market_risk.storage.runs import git_info

    paths = _paths()
    try:
        result = import_legacy(excel or paths.legacy_excel, paths, git_info(paths.root))
    except LegacyError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    _rebuild_db(paths)
    for d, run_id in result.imported:
        typer.echo(f"已导入 {d}：{run_id}（正式记录，reviewed=true）")
    for s in result.skipped:
        typer.echo(f"跳过：{s}")
    typer.echo(f"变更记录导入 {result.changelog_rows} 条复核记录")
    typer.echo("公式重算校验：" + ("全部与 Excel 缓存值一致" if not result.formula_checks else ""))
    for p in result.formula_checks:
        typer.echo(f"  - {p}")
    for m in result.metric_differences:
        typer.echo(f"数值差异：{m}")
    if result.score_differences:
        typer.echo("【需用户判断】截图记录与程序记录的维度分数不同：")
        for d in result.score_differences:
            typer.echo(f"  - {d}")
        raise typer.Exit(code=3)
    typer.echo("截图记录与程序记录的维度分数全部一致")


@app.command("rebuild-db")
def rebuild_db_cmd() -> None:
    """由 results/、data/manual/、data/materials/ 重建 SQLite 数据库。"""
    from market_risk.storage import db

    paths = _paths()
    path = db.rebuild(paths)
    counts = {t: len(v) for t, v in db.dump(path).items()}
    typer.echo(f"已重建 {path}：" + "，".join(f"{t} {n}" for t, n in counts.items()))


@app.command()
def stats(
    framework: FrameworkOpt = "risk_scoring",
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """按 SOP 9.4 统计两个版本，写 reports/backtest_stats.md 与 reports/backtest_history.xlsx。"""
    from market_risk.stats import run_stats

    paths = _paths()
    text, _ = run_stats(paths, framework, _parse_date(start) if start else None, _parse_date(end) if end else None)
    typer.echo(text)
    typer.echo(f"已写入 {paths.backtest_stats_md} 与 {paths.backtest_history_xlsx}")
