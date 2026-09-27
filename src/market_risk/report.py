"""输出（SPEC 第7节；位置按 docs/STORAGE.md）：运行目录下的 JSON / CSV / prompt / 摘要。

- prompt 的第六至八节规则全文从 docs/SOP.md 原样截取 7.1、7.2、7.3，不在模板中手写。
- 生成 prompt 的部分不得读取结果标签（STORAGE 第5节）。
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from market_risk import calendar as mcal
from market_risk.config import PROJECT_ROOT
from market_risk.data.raw_io import save_raw_inputs
from market_risk.data.snapshot import RawInputs
from market_risk.metrics import metrics_from_snapshot_json
from market_risk.models import MarketSnapshot, NearThresholdItem, ScoreResult
from market_risk.scoring.common import exact, fixed2, show
from market_risk.storage.paths import INPUT_FILES, RUN_FILES
from market_risk.storage.runs import to_jsonable, write_json

SOP_PATH = PROJECT_ROOT / "docs" / "SOP.md"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
PROMPT_TEMPLATE = "prompt_backtest.md.j2"
WEEKDAYS = "一二三四五六日"
MA_PERIODS = (5, 10, 20, 30, 50, 200)


# ---------------------------------------------------------------------------
# SOP 规则全文
# ---------------------------------------------------------------------------


def extract_sop_section(sop_text: str, number: str) -> str:
    """按章节标题（如 "7.1"）截取 SOP 正文，不含标题行，到下一个同级或更高级标题为止。"""
    lines = sop_text.splitlines()
    pattern = re.compile(rf"^(#+)\s+{re.escape(number)}\s")
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        level = len(m.group(1))
        body: list[str] = []
        for nxt in lines[i + 1 :]:
            h = re.match(r"^(#+)\s", nxt)
            if h and len(h.group(1)) <= level:
                break
            body.append(nxt)
        return "\n".join(body).strip()
    raise ValueError(f"SOP 中找不到第 {number} 节")


def load_rules(sop_path: Path = SOP_PATH) -> dict[str, str]:
    text = sop_path.read_text(encoding="utf-8")
    return {n: extract_sop_section(text, n) for n in ("7.1", "7.2", "7.3")}


# ---------------------------------------------------------------------------
# 逐日数据
# ---------------------------------------------------------------------------


def _rolling_ma(closes: Mapping[dt.date, float], day: dt.date, period: int) -> float | None:
    """截至 day（含）的简单均线；day 无收盘价或数据不足时为 None（逐日表中留空）。"""
    if day not in closes:
        return None
    series = [v for d, v in sorted(closes.items()) if d <= day]
    return sum(series[-period:]) / period if len(series) >= period else None


def daily_data_rows(
    raw: RawInputs, snapshot: MarketSnapshot, symbols: Iterable[str], scored: Iterable[str]
) -> list[dict[str, Any]]:
    """T−45 至基准日的逐日数据（SPEC 第7节第2项）：收盘价、均线、VIX、财政部10年期、OAS。缺失留空。"""
    base = snapshot.refs.base_date
    days = mcal.stock_trading_days(snapshot.refs.three_segment_query_start, base)
    scored = list(scored)
    rows = []
    for d in days:
        row: dict[str, Any] = {"date": d.isoformat()}
        for s in symbols:
            v = raw.closes.get(s, {}).get(d)
            row[f"{s}_close"] = v
        for s in scored:
            closes = {k: v for k, v in raw.closes[s].items() if v is not None and k <= base}
            for p in MA_PERIODS:
                ma = _rolling_ma(closes, d, p)
                row[f"{s}_ma{p}"] = None if ma is None else round(ma, 4)
        row["vix"] = raw.vix_fred.get(d)
        row["treasury_10y"] = raw.treasury.get(d)
        row["oas"] = raw.oas.get(d) if d < base else None  # 基准日当天的 OAS 不使用
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0]) if rows else [])
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: "" if r.get(k) is None else r[k] for k in fields})


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportContext:
    """生成输出所需的全部内容（不含结果标签）。"""

    raw: RawInputs
    snapshot: MarketSnapshot
    results: tuple[ScoreResult, ...]
    near: tuple[NearThresholdItem, ...]
    d1_includes_t_minus_20: bool
    breadth_source: str | None = None   # 为 None 时按读数的 source 自动说明


def _holidays_text(snapshot: MarketSnapshot) -> str:
    r = snapshot.refs
    stock, bond = set(r.stock_holidays_in_window), set(r.bond_holidays_in_window)
    parts = []
    for d in sorted(stock | bond):
        if d in stock and d in bond:
            parts.append(f"{d} 股债均休市（不计入股票交易日与债市营业日）")
        elif d in stock:
            parts.append(f"{d} 股市休市、债市开市（不计入股票交易日；该日财政部数值不计入利率窗口）")
        else:
            parts.append(f"{d} 债市休市、股市开市（利率窗口少一个观测，不插值；不计为债市营业日）")
    return "；".join(parts) if parts else "无"


BREADTH_SOURCES = {
    "tradingview": "TradingView 导出数据（INDEX:S5FI、INDEX:S5TW 日线收盘值）",
    "manual": "手工录入（data/manual/breadth.csv）",
    "screenshot": "截图读数",
}


def _breadth_source_text(snap: MarketSnapshot) -> str:
    readings = [r for r in (snap.breadth, snap.breadth_t5) if r is not None]
    if not readings:
        return "缺失"
    return "；".join(sorted({f"{r.date}：{BREADTH_SOURCES.get(r.source, r.source)}" for r in readings}))


def prompt_context(ctx: ReportContext, rules: Mapping[str, str]) -> dict[str, Any]:
    snap, refs = ctx.snapshot, ctx.snapshot.refs
    base = refs.base_date
    etfs = [
        {"symbol": s, "close": fixed2(exact(e.close)), "ma5": fixed2(exact(e.ma5)),
         "ma20": fixed2(exact(e.ma20)), "ma50": fixed2(exact(e.ma50)), "ma200": fixed2(exact(e.ma200))}
        for s, e in snap.etfs.items()
    ]
    flag = ctx.d1_includes_t_minus_20
    traces = []
    for res in snap.three_segment[flag]:
        for t in res.traces:
            traces.append({
                "symbol": t.symbol, "d1": t.d1, "d1_close": fixed2(t.d1_close), "lc": fixed2(t.lc),
                "lc_date": t.lc_date, "step1": t.step1, "step3": t.step3, "completed": t.completed,
                "d2_text": "、".join(str(d) for d in t.d2_dates) or "无",
            })
    completed = [r.symbol for r in snap.three_segment[flag] if r.completed]
    other = [r.symbol for r in snap.three_segment[not flag] if r.completed]
    conclusion = f"程序结论：完成三环节的标的：{'、'.join(completed) or '无'}。"
    if set(completed) != set(other):
        conclusion += (f"【注意】参考口径（d1 候选{'不含' if flag else '含'} T−20）下完成的标的为："
                       f"{'、'.join(other) or '无'}（三环节结果依赖口径，计分以 SOP 7.2 为准）。")
    daily_closes = []
    for d in mcal.stock_trading_days(refs.three_segment_query_start, base):
        daily_closes.append({"date": d, **{s: fixed2(e_closes.get(d)) for s, e_closes in
                                           ((s, dict(snap.etfs[s].closes)) for s in snap.etfs)}})
    b, b5 = snap.breadth, snap.breadth_t5
    breadth = {
        "f": fixed2(b.s5fi) if b else "缺失",
        "w": fixed2(b.s5tw) if b else "缺失",
        "t5": f"S5FI {fixed2(b5.s5fi)}%，S5TW {fixed2(b5.s5tw)}%" if b5 else "未提供（规则上需要时请按待补处理）",
    }
    yields = []
    window = set(refs.window_days)
    for d in sorted({refs.t_minus_20, *refs.window_days}):
        note = "T−20" if d == refs.t_minus_20 else ""
        if d in snap.yields:
            yields.append({"date": d, "value": fixed2(snap.yields[d]), "note": note})
        elif d in window or d == refs.t_minus_20:
            yields.append({"date": d, "value": "无数值",
                           "note": "债市休市，不插值" + ("；T−20" if note else "")})
    weekend = [d for d in (refs.oas_o1_v2m, refs.oas_o6_v2m) if d and d.weekday() >= 5]
    same_o6 = refs.oas_o6_v2m == refs.oas_o6_v3r1 and refs.oas_o1_v2m == refs.oas_o1
    oas_note = ("两套规则的 O1、O6 相同。" if same_o6 else
                "两套规则的 O1/O6 不同：v2-M 按 FRED 所列有数值观测计数（月末周末观测计入、债市休市日沿用值不计），"
                "v3-R1 按债市营业日计数。")
    if weekend:
        oas_note += f"v2-M 使用了月末周末观测：{'、'.join(str(d) for d in weekend)}。"
    return {
        "mode": snap.mode,
        "base_date": base,
        "weekday": f"周{WEEKDAYS[base.weekday()]}",
        "is_early_close": refs.is_early_close,
        "refs": refs,
        "v3r1_sequence": "、".join(str(d) for d in refs.oas_o1_to_o6_sequence),
        "holidays_text": _holidays_text(snap),
        "breadth_source": ctx.breadth_source or _breadth_source_text(snap),
        "etfs": etfs,
        "spy_window_max": fixed2(snap.spy_window_max_close),
        "d1_scope": "T−20 至 T−2" if flag else "T−19 至 T−2",
        "traces": traces,
        "three_segment_conclusion": conclusion,
        "daily_closes": daily_closes,
        "breadth": breadth,
        "vix": fixed2(snap.vix),
        "vix_t5": fixed2(snap.vix_t5),
        "yields": yields,
        "oas": {"o1_v3": fixed2(snap.oas_o1), "o6_v3": fixed2(snap.oas_o6_v3r1),
                "o1_v2": fixed2(snap.oas_o1_v2m), "o6_v2": fixed2(snap.oas_o6_v2m), "note": oas_note},
        "hyg_lqd": "缺失" if snap.hyg_lqd is None else f"{snap.hyg_lqd:.4f}",
        "data_notes": list(snap.data_notes),
        "rules_71": rules["7.1"],
        "rules_72": rules["7.2"],
        "rules_73": rules["7.3"],
    }


def render_prompt(ctx: ReportContext, rules: Mapping[str, str] | None = None,
                  template_dir: Path = TEMPLATE_DIR) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        autoescape=False,  # 输出 Markdown 文本，不是 HTML
    )
    return env.get_template(PROMPT_TEMPLATE).render(**prompt_context(ctx, rules or load_rules()))


# ---------------------------------------------------------------------------
# summary.md
# ---------------------------------------------------------------------------


def _score_text(d: Any) -> str:
    return str(d.score) if d.score is not None else f"待补（可能 {'/'.join(map(str, d.possible_scores))}）"


def render_summary(ctx: ReportContext, status: str, official_note: str = "") -> str:
    snap, refs = ctx.snapshot, ctx.snapshot.refs
    lines = [f"# {refs.base_date} 大盘风险评分摘要（{'每日' if snap.mode == 'daily' else '回测'}）", ""]
    lines += [f"运行状态：{status}", ""]
    if official_note:
        lines += [f"正式记录：{official_note}", ""]
    names = [d.name for d in ctx.results[0].dimensions]
    lines += ["## 分项分数", "", "| 维度 | " + " | ".join(r.version for r in ctx.results) + " |",
              "|---|" + "---|" * len(ctx.results)]
    for i, name in enumerate(names):
        lines.append(f"| {name} | " + " | ".join(_score_text(r.dimensions[i]) for r in ctx.results) + " |")
    totals = []
    for r in ctx.results:
        total = str(r.total) if r.total is not None else f"{r.total_range[0]}–{r.total_range[1]}"
        totals.append(f"{total}（{r.stage or '范围跨越阶段，不给唯一阶段'}）")
    lines.append("| **总分（阶段）** | " + " | ".join(totals) + " |")
    lines += ["", "## 大盘明确恶化证据链", "", "| 项目 | " + " | ".join(r.version for r in ctx.results) + " |",
              "|---|" + "---|" * len(ctx.results)]
    for i, (item, _) in enumerate(ctx.results[0].clear_deterioration):
        lines.append(f"| {item} | " + " | ".join(r.clear_deterioration[i][1] for r in ctx.results) + " |")
    lines += ["", "## 计算过程", ""]
    for r in ctx.results:
        lines.append(f"### {r.version}")
        for d in r.dimensions:
            lines.append(f"- **{d.name}**（{_score_text(d)}）：{d.calculation}")
        lines.append("")
    flags = [f for r in ctx.results for f in (*r.review_flags, *r.notes)]
    if flags:
        lines += ["## 需要注意", ""] + [f"- {f}" for f in flags] + [""]
    lines += ["## 贴近门槛的读数", ""]
    if ctx.near:
        lines += ["| 项目 | 数值 | 门槛 | 差距 |", "|---|---|---|---|"]
        lines += [f"| {n.item} | {show(n.value)} | {fixed2(n.threshold)} | {show(n.gap)} {n.unit} |"
                  for n in ctx.near]
    else:
        lines.append("无")
    lines += ["", "## 数据问题", ""]
    lines += [f"- {n}" for n in snap.data_notes] or ["无"]
    if snap.mode == "daily":
        lines += ["", f"本日是否为本周最后一个交易日：{'是' if refs.is_last_trading_day_of_week else '否'}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 写入运行目录
# ---------------------------------------------------------------------------


def snapshot_json(snap: MarketSnapshot) -> dict[str, Any]:
    """MarketSnapshot（不含逐日序列与三环节遍历）。"""
    data = to_jsonable(snap)
    for e in data["etfs"].values():
        e.pop("closes", None)
    data.pop("three_segment", None)
    return data


def three_segment_rows(snap: MarketSnapshot) -> list[dict[str, Any]]:
    rows = []
    for flag in (True, False):
        for res in snap.three_segment[flag]:
            for t in res.traces:
                rows.append({
                    "d1_includes_t_minus_20": flag, "symbol": t.symbol, "d1": t.d1,
                    "d1_close": t.d1_close, "lc": t.lc, "lc_date": t.lc_date, "step1": t.step1,
                    "d2_dates": " ".join(str(d) for d in t.d2_dates), "step3": t.step3,
                    "completed": t.completed,
                })
    return rows


def write_inputs(run_dir: Path, ctx: ReportContext, reference_symbols: Iterable[str]) -> None:
    """inputs/：当次实际使用的数据（STORAGE 第1节第4条），另存全部原始序列以便复现。"""
    raw, snap = ctx.raw, ctx.snapshot
    base = snap.refs.base_date
    inputs = run_dir / "inputs"
    symbols = [*snap.etfs, *[s for s in reference_symbols if s in raw.closes]]
    write_csv(inputs / INPUT_FILES["daily_data"], daily_data_rows(raw, snap, symbols, list(snap.etfs)))
    fred_rows = [{"series": "VIXCLS", "date": d, "value": v} for d, v in sorted(raw.vix_fred.items())
                 if d <= base]
    fred_rows += [{"series": "BAMLH0A0HYM2", "date": d, "value": v} for d, v in sorted(raw.oas.items())
                  if d <= base]
    fred_rows += [{"series": "BAMLH0A0HYM2_vintage", "date": d, "value": v}
                  for d, v in sorted((raw.oas_vintage or {}).items()) if d <= base]
    write_csv(inputs / INPUT_FILES["fred_observations"], fred_rows, ["series", "date", "value"])
    source = next((s.source for s in raw.sources if s.key in ("10Y", "DGS10")), "treasury")
    write_csv(inputs / INPUT_FILES["treasury_yields"],
              [{"date": d, "value": v, "source": source} for d, v in sorted(raw.treasury.items()) if d <= base],
              ["date", "value", "source"])
    breadth_rows = [{"date": r.date, "s5fi": r.s5fi, "s5tw": r.s5tw, "source": r.source}
                    for r in (snap.breadth, snap.breadth_t5) if r is not None]
    write_csv(inputs / INPUT_FILES["breadth"], breadth_rows, ["date", "s5fi", "s5tw", "source"])
    save_raw_inputs(raw, inputs / "raw")


def write_run_outputs(
    run_dir: Path,
    ctx: ReportContext,
    status: str,
    reference_symbols: Iterable[str] = ("HYG", "LQD"),
    official_note: str = "",
    rules: Mapping[str, str] | None = None,
) -> dict[str, Path]:
    """写入 SPEC 第7节全部输出（meta.json 由调用方写）。返回 {名称: 路径}。"""
    snap = ctx.snapshot
    write_inputs(run_dir, ctx, reference_symbols)
    write_json(run_dir / RUN_FILES["dates"], snap.refs)
    write_json(run_dir / RUN_FILES["snapshot"], snapshot_json(snap))
    write_json(run_dir / RUN_FILES["metrics"], metrics_from_snapshot_json(snapshot_json(snap)))
    write_json(run_dir / RUN_FILES["scores"], {
        "d1_includes_t_minus_20": ctx.d1_includes_t_minus_20,
        "results": list(ctx.results),
        "near_threshold": list(ctx.near),
        "three_segment": {str(k).lower(): v for k, v in snap.three_segment.items()},
    })
    write_csv(run_dir / RUN_FILES["three_segment"], three_segment_rows(snap))
    (run_dir / RUN_FILES["prompt"]).write_text(render_prompt(ctx, rules), encoding="utf-8")
    (run_dir / RUN_FILES["summary"]).write_text(render_summary(ctx, status, official_note), encoding="utf-8")
    return {k: run_dir / v for k, v in RUN_FILES.items() if k not in ("meta", "legacy")}
