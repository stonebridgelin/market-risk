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
