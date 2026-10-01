"""命令行入口（SPEC 第8节）。

本模块只负责解析参数和格式化输出（CLAUDE.md 第12条）；业务逻辑全部在 services.py。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from market_risk import services
from market_risk.config import load_settings
from market_risk.models import MarketSnapshot
from market_risk.storage.runs import to_jsonable

app = typer.Typer(help="美股大盘风险评分：数据准备、机械计算与 prompt 生成", no_args_is_help=True)
tv_app = typer.Typer(help="TradingView 导出数据：导入、校验、列表、比对（docs/TRADINGVIEW.md）")
official_app = typer.Typer(help="正式记录（official.json）")
material_app = typer.Typer(help="资料管理")
breadth_app = typer.Typer(help="广度读数录入")
outcome_app = typer.Typer(help="风险事件标签（结果窗口结束后）")
audit_app = typer.Typer(help="数据审计（休市日历、债市休市日 OAS）")
data_app = typer.Typer(help="市场数据集 data/market/（评分输入）")
db_app = typer.Typer(help="数据库：SQL 导出、MySQL 兼容性验证")
backtest_app = typer.Typer(help="阶段6 逐日历史回测（docs/STORAGE.md 2.3）")
research_app = typer.Typer(help="独立研究与评估（不参与评分）")
wavewarn_app = typer.Typer(help="v1.2.1 波段预警研究（仅开发期工程评价）")
for sub, name in ((tv_app, "tv"), (official_app, "official"), (material_app, "material"),
                  (breadth_app, "breadth"), (outcome_app, "outcome"), (audit_app, "audit"),
                  (data_app, "data"), (db_app, "db"), (backtest_app, "backtest"),
                  (research_app, "research"), (wavewarn_app, "wavewarn")):
    app.add_typer(sub, name=name)

MATERIAL_TYPES_HELP = ("tiger_ai_background", "chatgpt_response", "claude_review", "notes", "screenshot", "other")
SubjectOpt = Annotated[str, typer.Option("--subject", help="MARKET 或股票代码")]
FrameworkOpt = Annotated[str, typer.Option("--framework", help="分析框架")]


def configure_output(streams: tuple[Any, ...] | None = None) -> None:
    """stdout、stderr 统一为 UTF-8（errors=replace）。

    Windows 下输出被重定向或经管道时，Python 按系统编码（中文系统为 GBK）写出，遇到"−""≤"等字符会报错。
    已是 UTF-8 的流不改动；不支持 reconfigure 的流（如测试替换的流）跳过。
    """
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


@app.callback()
def main() -> None:
    """美股大盘风险评分（v2-M 与 v3-R1 并行）。"""
    configure_output()


# ---------------------------------------------------------------------------
# 参数解析与公共输出
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> dt.date:
    try:
        if len(value) != 10:
            raise ValueError(value)
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def _opt_date(value: str | None) -> dt.date | None:
    return _parse_date(value) if value else None


def _parse_datetime(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"时间格式应为 ISO，如 2026-09-26T15:30-04:00：{value}") from exc
    if parsed.tzinfo is None:
        raise typer.BadParameter("导出时间必须带时区偏移，如 2026-09-26T15:30-04:00")
    return parsed


def _ctx() -> services.Context:
    return services.Context.from_settings(load_settings())


def _call[T](func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """调用业务函数；ServiceError 转为"错误：…"并以退出码 1 结束。"""
    try:
        return func(*args, **kwargs)
    except services.ServiceError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc


def _table(headers: list[str], rows: list[list[str]]) -> str:
    from market_risk.data.tradingview import format_table

    return format_table(headers, rows)


# ---------------------------------------------------------------------------
# 日期、数据、评分
# ---------------------------------------------------------------------------


@app.command()
def dates(date: Annotated[str, typer.Option("--date", help="基准日 YYYY-MM-DD")]) -> None:
    """计算基准日的全部日期参照（离线：债市日历暂按 holidays.yaml 推出）。"""
    preview = _call(services.preview_dates, _parse_date(date))
    typer.echo(json.dumps(to_jsonable(dataclasses.asdict(preview.refs)), ensure_ascii=False, indent=2))
    for note in preview.notes:
        typer.echo(f"注意：{note}", err=True)


@app.command()
def samples(year: Annotated[int, typer.Option("--year", help="年份")]) -> None:
    """按 SOP 9.2 列出每月最后一个周五（休市则取当月最后一个交易日）。"""
    for s in services.sample_dates(year):
        typer.echo(f"{s.date}（{s.weekday}）" + (f"（{s.note}）" if s.note else ""))


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
        ma200 = "" if e.ma200 is None else f"{e.ma200:.2f}"
        lines.append(f"{s:<5} {e.close:8.2f} {e.ma5:8.2f} {e.ma10:8.2f} {e.ma20:8.2f} "
                     f"{e.ma30:8.2f} {e.ma50:8.2f} {ma200:>8}")
    lines += [
        f"SPY 窗口最高收盘 {f(snap.spy_window_max_close)}   HYG/LQD {f(snap.hyg_lqd, 4)}",
        f"VIX {f(snap.vix)}（T−5 {f(snap.vix_t5)}）",
        f"10年期 y={f(snap.y)}  H={f(snap.h)}（{', '.join(map(str, snap.h_dates))}）  T−20 y={f(snap.y_t20)}",
        "",
        "三环节（d1 为 T−20 至 T−2，SOP 7.2）：",
    ]
    for res in snap.three_segment[True]:
        step1 = [f"{t.d1}({t.d1_close}<Lc {t.lc}@{t.lc_date})" for t in res.traces if t.step1]
        lines.append(f"  {res.symbol}：完成={res.completed}；第一步成立：{'、'.join(step1) or '无'}")
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
    """按需下载并更新数据集 data/market/（含正式样本的 ALFRED 基准日版本），截断到基准日并显示核对摘要。"""
    result = _call(services.fetch_data, _ctx(), _parse_date(date), mode, refresh, save_raw)
    if save_raw is not None:
        typer.echo(f"原始数据已保存到 {save_raw}", err=True)
    typer.echo(format_snapshot_summary(result.snapshot))
    typer.echo("\n数据来源：")
    for s in result.raw.sources:
        typer.echo(f"  - {s.source}:{s.key} {s.data_start}至{s.data_end} {s.rows}行 "
                   f"{'缓存' if s.from_cache else '下载'}于 {s.downloaded_at_utc}")


def format_run_outcome(outcome: Any) -> str:
    lines = [f"运行目录：{outcome.run_dir}", f"运行状态：{outcome.status}；{outcome.official_note}"]
    for r in outcome.results:
        dims = "、".join(f"{d.name}{d.score if d.score is not None else '待补'}" for d in r.dimensions)
        total = r.total if r.total is not None else f"{r.total_range[0]}–{r.total_range[1]}"
        lines.append(f"{r.version}：{dims}；总分 {total}；{r.stage or '范围跨越阶段'}；"
                     f"明确恶化={dict(r.clear_deterioration)['大盘明确恶化']}")
        lines += [f"  {x}" for x in (*r.review_flags, *r.notes)]
    lines += [f"提示：{m}" for m in outcome.messages]
    return "\n".join(lines)


@app.command()
def score(
    date: Annotated[str | None, typer.Option("--date", help="基准日；daily 模式默认为今天（美东）")] = None,
    s5fi: Annotated[float | None, typer.Option("--s5fi", help="基准日 S5FI（百分数）")] = None,
    s5tw: Annotated[float | None, typer.Option("--s5tw", help="基准日 S5TW（百分数）")] = None,
    s5fi_t5: Annotated[float | None, typer.Option("--s5fi-t5", help="5个交易日前 S5FI")] = None,
    s5tw_t5: Annotated[float | None, typer.Option("--s5tw-t5", help="5个交易日前 S5TW")] = None,
    mode: Annotated[str, typer.Option("--mode", help="backtest 或 daily")] = "backtest",
) -> None:  # pragma: no cover - 离线部分见 services 与 pipeline 测试
    """从 data/market/ 读取数据、计算两个版本的评分，生成运行目录下的全部输出（数据集未覆盖基准日时先运行 fetch）。"""
    breadth = services.BreadthInput(s5fi, s5tw, s5fi_t5, s5tw_t5)
    outcome = _call(services.score_date, _ctx(), _opt_date(date), mode, breadth)
    typer.echo(format_run_outcome(outcome))


def format_data_build(report: Any) -> str:
    r = report.result
    rows = [[name, str(e["rows"]), f"{e['first_date']} 至 {e['last_date']}",
             "、".join(f"{k} {v}" for k, v in e["sources"].items()), "是" if name in r.changed else ""]
            for name, e in r.series.items()]
    lines = [f"数据集截止：{report.end}（{'离线：只用现有缓存' if report.offline else '按需下载'}）",
             _table(["序列", "行数", "起止日期", "来源（行数）", "本次更新"], rows)]
    if report.cutoff_note:
        lines.append(f"说明：{report.cutoff_note}")
    lines += [f"说明：{n}" for n in r.notes]
    if r.fetch_failures:
        lines.append("【警告】OAS 本次未更新：FRED 获取失败")
    if r.revisions:
        verb = "已按 --accept-revisions 更新" if r.accepted else "未自动覆盖，保留旧值"
        lines.append(f"历史修订 {len(r.revisions)} 处（{verb}），清单：{report.revisions_path}")
    else:
        lines.append("没有历史修订。")
    if r.missing_old_dates:
        lines.append(f"新下载缺少的旧日期 {len(r.missing_old_dates)} 处（继续保留），清单：{report.revisions_path}")
    lines.append(f"manifest：{r.manifest_path}")
    return "\n".join(lines)


@data_app.command("build")
def data_build(
    offline: Annotated[bool, typer.Option("--offline", help="只用现有缓存生成，不联网")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略缓存，强制重新下载")] = False,
    accept_revisions: Annotated[bool, typer.Option("--accept-revisions", help="接受历史修订，用新值覆盖")] = False,
    end: Annotated[str | None, typer.Option("--end", help="截止日期（默认：已完整收盘的最近交易日）")] = None,
) -> None:
    """由接口缓存（按需下载）、TradingView 清洗结果和手工录入生成 data/market/ 与 manifest.json。"""
    report = _call(services.data_build, _ctx(), offline, refresh, accept_revisions, _opt_date(end))
    typer.echo(format_data_build(report))
    if report.result.fetch_failures:
        typer.echo("【警告】OAS 本次未更新：FRED 获取失败", err=True)
    if report.result.revisions and not accept_revisions:
        raise typer.Exit(code=2)


@app.command()
def validate() -> None:
    """用 tests/fixtures 中的历史样本比对程序值与截图值（离线）。"""
    report = services.validate_samples(load_settings())
    rows = [[c.sample, c.item, c.screenshot, c.program, c.diff, "一致" if c.ok else "不一致"] for c in report.checks]
    typer.echo(_table(["样本", "项目", "截图", "程序", "差值", "结果"], rows))
    n = len(report.checks)
    typer.echo(f"\n共 {n} 项，一致 {n - report.mismatches}，不一致 {report.mismatches}。")
    if not report.ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# TradingView
# ---------------------------------------------------------------------------


@tv_app.command("import")
def tv_import(
    directory: Annotated[Path, typer.Option("--dir", help="原始文件目录 raw/<导出日期>/")],
    export_date: Annotated[str | None, typer.Option("--export-date", help="导出日期（默认取目录名）")] = None,
    export_time: Annotated[
        str | None,
        typer.Option("--export-time", help="首次导入必填：带时区的导出时刻；登记到 manifest 后重建沿用"),
    ] = None,
) -> None:
    """导入并校验一个目录下的全部 TradingView 导出文件，打印汇总表。"""
    from market_risk.data import tradingview as tv

    result = _call(services.tv_import, _ctx(), directory, _opt_date(export_date),
                   _parse_datetime(export_time) if export_time else None)
    typer.echo(tv.format_import_summary(result))
    if any(r.status == tv.FAILED for r in result.reports) or result.merge_errors:
        raise typer.Exit(code=2)


@tv_app.command("validate")
def tv_validate(symbol: Annotated[str | None, typer.Option("--symbol", help="只校验该标的")] = None) -> None:
    """按 manifest 重新读取并校验已导入的原始文件，重建清洗结果。"""
    from market_risk.data import tradingview as tv

    result = services.tv_validate(_ctx(), symbol)
    typer.echo("尚未导入任何 TradingView 文件" if result is None else tv.format_import_summary(result))


@tv_app.command("list")
def tv_list() -> None:
    """列出已导入的标的、起止日期、校验状态。"""
    from market_risk.data import tradingview as tv

    typer.echo(tv.format_list(services.tv_list(_ctx())))


@tv_app.command("compare")
def tv_compare(
    symbol: Annotated[str, typer.Option("--symbol", help="标的，如 BAMLH0A0HYM2")],
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略接口缓存")] = False,
) -> None:  # pragma: no cover - 联网
    """与接口数据做重叠比对，结果写入 reports/tradingview_compare_<标的>.md。"""
    report = _call(services.tv_compare, _ctx(), symbol, refresh)
    typer.echo(report.text)
    typer.echo(f"已写入 {report.path}")


@tv_app.command("quality")
def tv_quality() -> None:
    """广度指标的早期数据质量检查（只报告），写 reports/tradingview_data_quality.md。"""
    report = services.tv_quality(_ctx())
    typer.echo(report.text)
    typer.echo(f"已写入 {report.path}")


@tv_app.command("crosscheck")
def tv_crosscheck(
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略接口缓存")] = False,
) -> None:  # pragma: no cover - 联网
    """全部 crosscheck 标的与接口数据比对，写入 reports/tradingview_crosscheck.md。"""
    report = _call(services.tv_crosscheck, _ctx(), refresh)
    typer.echo(report.text)
    typer.echo(f"已写入 {report.path}")


@tv_app.command("review-prices")
def tv_review_prices(
    refresh: Annotated[bool, typer.Option("--refresh", help="刷新审计用指数和除息记录")] = False,
) -> None:
    """按固定残差法核查争议收盘价，写入 reports/price_dispute_review.md。"""
    report = _call(services.review_price_disputes, _ctx(), refresh)
    typer.echo(report.text)
    typer.echo(f"已写入 {report.path}")


# ---------------------------------------------------------------------------
# 正式记录、资料、广度、结果标签
# ---------------------------------------------------------------------------


@official_app.command("set")
def official_set(
    date: Annotated[str, typer.Option("--date")],
    run: Annotated[str, typer.Option("--run", help="运行编号 run_...")],
    subject: SubjectOpt = "MARKET",
    framework: FrameworkOpt = "risk_scoring",
) -> None:
    """手动指定正式记录（reviewed 重置为 false，复核后用 official confirm）。"""
    pointer = _call(services.official_set, _ctx(), _parse_date(date), run, subject, framework)
    typer.echo(f"正式记录已设为 {pointer['run_id']}（reviewed=false）")


@official_app.command("confirm")
def official_confirm(
    date: Annotated[str, typer.Option("--date")],
    subject: SubjectOpt = "MARKET",
    framework: FrameworkOpt = "risk_scoring",
) -> None:
    """把当前正式记录标记为已复核（reviewed=true）。"""
    pointer = _call(services.official_confirm, _ctx(), _parse_date(date), subject, framework)
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
    target = _call(services.material_add, _ctx(), _parse_date(date), kind, file, subject, source, note)
    typer.echo(f"已登记：{target}")


@material_app.command("list")
def material_list(
    subject: SubjectOpt = "MARKET",
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """列出资料。"""
    rows = services.material_list(_ctx(), subject, _opt_date(start), _opt_date(end))
    if not rows:
        typer.echo("没有资料")
        return
    typer.echo(_table(["对象", "日期", "类型", "文件", "来源", "说明"],
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
    ctx, day = _ctx(), _parse_date(date)
    old = services.breadth_existing(ctx, day)
    if (overwrite and not yes and old is not None and (old.s5fi, old.s5tw) != (round(s5fi, 2), round(s5tw, 2))
            and not typer.confirm(f"{day} 已有读数 S5FI={old.s5fi}、S5TW={old.s5tw}，确认覆盖？")):
        raise typer.Exit(code=1)
    result = _call(services.breadth_add, ctx, day, s5fi, s5tw, note, overwrite)
    typer.echo("已写入" if result.changed else "读数相同，无变更")


def format_outcome(result: services.OutcomeResult) -> str:
    o = result.outcome

    def pct(v: float | None) -> str:
        return "-" if v is None else f"{v}%"

    lines = [
        f"{o.base_date}：基准日口径跌幅 标普500 {o.spx_drawdown_from_base}%、QQQ {o.qqq_drawdown_from_base}%，"
        f"风险事件={'是' if o.is_event else '否'}（结果窗口 {o.window_start} 至 {o.window_end}）",
        f"  参考：峰谷回撤 标普500 {pct(o.spx_peak_to_trough_drawdown)}、QQQ {pct(o.qqq_peak_to_trough_drawdown)}；"
        f"接近事件={'是' if o.near_event else '否'}",
    ]
    return "\n".join(lines + [f"【差异】{d}" for d in result.differences])


@outcome_app.command("compute")
def outcome_compute(
    date: Annotated[str, typer.Option("--date")],
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
) -> None:  # pragma: no cover - 联网
    """结果窗口结束后，用标普500指数与 QQQ 的收盘价计算风险事件标签（SOP 9.3）。"""
    typer.echo(format_outcome(_call(services.outcome_compute, _ctx(), _parse_date(date), refresh)))


@outcome_app.command("add")
def outcome_add(
    date: Annotated[str, typer.Option("--date")],
    spx: Annotated[float, typer.Option("--spx", help="标普500最低收盘价跌幅（百分数，如 -5.2）")],
    qqq: Annotated[float, typer.Option("--qqq", help="QQQ 最低收盘价跌幅（百分数）")],
    event_date: Annotated[str | None, typer.Option("--event-date", help="首次达到门槛的日期（可选）")] = None,
    spx_peak: Annotated[
        float | None, typer.Option("--spx-peak-to-trough", help="标普500峰谷回撤（可选，参考）")
    ] = None,
    qqq_peak: Annotated[float | None, typer.Option("--qqq-peak-to-trough", help="QQQ 峰谷回撤（可选，参考）")] = None,
) -> None:
    """人工录入风险事件标签（与程序计算值不一致时报告差异）。"""
    result = _call(services.outcome_add, _ctx(), _parse_date(date), spx, qqq, _opt_date(event_date),
                   spx_peak, qqq_peak)
    typer.echo("已录入：" + format_outcome(result))


@audit_app.command("calendar")
def audit_calendar(
    start_year: Annotated[int, typer.Option("--start-year", help="holidays.yaml 的起始年份")] = 2008,
    oas_start_year: Annotated[int, typer.Option("--oas-start-year", help="OAS 清单的起始年份")] = 1997,
    write: Annotated[bool, typer.Option("--write", help="写入 config/holidays.yaml")] = False,
) -> None:  # pragma: no cover - 联网（下载财政部年度数据）
    """补齐休市日历并与 SIFMA 常见规则对照；列出债市休市日 OAS 数值不同的日期，写 reports/holiday_calendar_audit.md。"""
    report = _call(services.audit_calendar, _ctx(), start_year, oas_start_year, write)
    typer.echo(report.text)
    written = f" 与 {report.yaml_path}" if report.yaml_path else "（未写 holidays.yaml，加 --write 写入）"
    typer.echo(f"已写入 {report.path}{written}")


# ---------------------------------------------------------------------------
# 旧记录、数据库、统计
# ---------------------------------------------------------------------------


@app.command("import-legacy")
def import_legacy_cmd(
    excel: Annotated[Path | None, typer.Option("--excel", help="默认 data/legacy/backtest_record_legacy.xlsx")] = None,
) -> None:
    """导入截图时代的样本（设为正式记录、reviewed=true），与程序记录对照。"""
    result = _call(services.import_legacy, _ctx(), excel)
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
    """由 results/、data/manual/、data/materials/ 重建数据库。"""
    ctx = _ctx()
    counts = services.rebuild_database(ctx)
    typer.echo(f"已重建 {ctx.db_url}：" + "，".join(f"{t} {n}" for t, n in counts.items()))


@app.command()
def stats(
    framework: FrameworkOpt = "risk_scoring",
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """按 SOP 9.4 统计两个版本，写 reports/backtest_stats.md 与 reports/backtest_history.xlsx。"""
    report = services.run_stats(_ctx(), framework, _opt_date(start), _opt_date(end))
    typer.echo(report.text)
    typer.echo(f"已写入 {report.markdown_path} 与 {report.workbook_path}")


@db_app.command("export-sql")
def db_export_sql() -> None:
    """导出 db/sql/ 下的 SQL 文件（表结构由 Alembic 最新版本导出，参考表由 YAML 生成；不得手工修改）。"""
    for p in _call(services.db_export_sql, _ctx()):
        typer.echo(f"已写出 {p}")


def format_verify(report: Any) -> str:
    lines = [f"数据库：{report.dialect}；Alembic 版本：{report.alembic_version}"]
    lines += [f"{k}：{v}" for k, v in report.settings.items()]
    rows = [[t.table, str(t.sqlite_rows), str(t.target_rows), "一致" if t.equal else "不一致"] for t in report.tables]
    lines.append(_table(["表", "SQLite 行数", "目标库行数", "结果"], rows))
    for t in report.tables:
        lines += [f"  {t.table} 差异示例：{s}" for s in t.samples]
    if report.exported_sql_ok is not None:
        lines.append("导出的 SQL（01_schema、02_base_data）建库：" + ("通过" if report.exported_sql_ok else "不通过"))
        lines += [f"  {n}" for n in report.exported_sql_notes]
    lines.append("结论：" + ("与 SQLite 逐表逐行一致" if report.ok else "存在差异"))
    return "\n".join(lines)


@db_app.command("verify-mysql")
def db_verify_mysql() -> None:
    """在 .env 的 MYSQL_VERIFY_URL（专用、可清空的库）上执行迁移与 rebuild-db，与 SQLite 逐表逐行比对。"""
    report = _call(services.db_verify_mysql, _ctx())
    if report is None:
        typer.echo("未设置 MYSQL_VERIFY_URL，跳过 MySQL 兼容性验证。")
        return
    typer.echo(format_verify(report))
    if not report.ok:
        raise typer.Exit(code=1)


@backtest_app.command("run")
def backtest_run(
    date_from: Annotated[str | None, typer.Option("--from", help="起始日（默认为配置的回测起点 2008-08-11）")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="终止日（默认为评分用价格序列的最新日期）")] = None,
    versions: Annotated[str, typer.Option("--versions", help="v2-M、v3-R1，逗号分隔")] = "v2-M,v3-R1",
    unlock_holdout: Annotated[bool, typer.Option("--unlock-holdout", help="写出保留期的标签（记录解锁时间）")] = False,
) -> None:
    """逐日计算两个版本的评分与指标，并生成结果标签与回调事件标签，写入新的回测运行目录。"""
    def progress(n: int, total: int) -> None:
        typer.echo(f"  已计算 {n}/{total} 个交易日", err=True)

    report = _call(services.backtest_run, _ctx(), _opt_date(date_from), _opt_date(date_to),
                   tuple(v.strip() for v in versions.split(",") if v.strip()), unlock_holdout, progress=progress)
    typer.echo(f"运行目录：{report.run_dir}")
    typer.echo(f"区间：{report.start} 至 {report.end}（{report.days} 个交易日）；耗时 {report.runtime_seconds:.1f} 秒")
    typer.echo("行数：" + "，".join(f"{k} {v}" for k, v in report.counts.items()))


@backtest_app.command("official")
def backtest_official(run: Annotated[str, typer.Option("--run", help="回测运行编号")]) -> None:
    """设置正式回测指针（backtests/official.json），并重写 backtests/.gitignore（只提交正式回测的运行目录）。"""
    pointer = _call(services.backtest_set_official, _ctx(), run)
    typer.echo(f"正式回测：{pointer['run_id']}（{pointer['set_at_utc']}）")


@backtest_app.command("report")
def backtest_report(run: Annotated[str | None, typer.Option("--run", help="默认为正式回测")] = None) -> None:
    """生成 reports/backtest_baseline.md（只用开发期与验证期；不计算评估指标）。"""
    report = _call(services.backtest_report, _ctx(), run)
    typer.echo(f"已写入 {report.path}（运行 {report.run_id}）")


@research_app.command("pullback-features")
def research_pullback_features() -> None:
    """生成小周期危险时段、冻结规则表现及开发期前瞻特征报告。"""
    result = _call(services.research_pullback_features, _ctx())
    typer.echo(f"已写入 {result.report_path}；危险时段 {result.danger_count}，"
               f"双指数 {result.combined_count}，特征 {result.feature_count} 项。")


@research_app.command("audit-pullback-development")
def research_audit_pullback_development() -> None:
    """补充原口径开发期候选日重复映射与逐特征缺值审计。"""
    result = _call(services.research_audit_pullback_development, _ctx())
    typer.echo(f"已写入 {result.summary_path} 与 {result.detail_path}；"
               f"重复映射候选日 {result.duplicate_candidate_dates} 个，特征 {result.feature_count} 项。")


@research_app.command("zz-v121-sides")
def research_zz_v121_sides() -> None:
    """运行 ZZ v1.2.1 开发期 B、DV 分侧前瞻研究。"""
    result = _call(services.research_zz_v121_sides, _ctx())
    typer.echo(f"已写入 {result.report_path}；ZZ 危险时段 {result.danger_periods} 个。")


@wavewarn_app.command("calibrate-exit-development")
def wavewarn_calibrate_exit_development() -> None:
    """生成开发期 E 退出代价校准；不用于选择参数。"""
    path = _call(services.wavewarn_calibrate_exit_development, _ctx())
    typer.echo(f"已写入 {path}")


@wavewarn_app.command("evaluate-development")
def wavewarn_evaluate_development() -> None:
    """运行开发期 E2 工程评价；不生成锁定记录或验证期结果。"""
    prepared, summaries = _call(services.wavewarn_evaluate_development, _ctx())
    typer.echo(f"已写入开发期工程评价：{len(summaries)} 个设定；"
               f"t0={prepared.t0}，τ={prepared.tau}，j₀={prepared.first_loss_day}。")


@backtest_app.command("zigzag-check")
def backtest_zigzag_check(
    symbol: Annotated[str, typer.Option("--symbol", help="SPX 或 QQQ")],
    level: Annotated[str, typer.Option("--level", help="层级，如 0.05")],
    date_from: Annotated[str, typer.Option("--from", help="起始日")],
    date_to: Annotated[str, typer.Option("--to", help="终止日")],
) -> None:
    """只输出由收盘价计算的价格波段（高点、低点、跌幅、分级、状态）；不涉及分数、预警状态或结果标签。"""
    rows = _call(services.zigzag_check, _ctx(), symbol, level, _parse_date(date_from), _parse_date(date_to))
    typer.echo(f"{symbol.upper()} ZigZag 层级 {level}（收盘价；全部历史上识别，"
               f"列出与 {date_from} 至 {date_to} 有交集的回调）")
    typer.echo(_table(["高点日", "高点收盘", "低点日", "低点收盘", "跌幅", "分级", "状态"],
                      [[str(r.high_date), f"{r.high_close:.2f}", str(r.low_date), f"{r.low_close:.2f}",
                        f"{r.drawdown_pct:.2f}%", r.grade, r.status] for r in rows]))
    typer.echo("说明：使用收盘价；图表波段线若使用日内最高价与最低价，日期与点位可能略有差异。")


@backtest_app.command("index-impact")
def backtest_index_impact() -> None:
    """指数争议日（SPX、NDX）对结果标签与回调事件的实质影响检验，写 reports/index_dispute_label_impact.md。"""
    report = _call(services.index_dispute_label_impact, _ctx())
    typer.echo(report.text)
    typer.echo(f"已写入 {report.path}")
