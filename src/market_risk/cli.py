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
