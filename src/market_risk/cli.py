"""命令行入口（SPEC 第8节）。"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Annotated, Any

import typer

from market_risk import calendar as mcal
from market_risk.config import load_holidays

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
