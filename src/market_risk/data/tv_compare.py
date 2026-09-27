"""TradingView 导出数据与接口数据的比对（docs/TRADINGVIEW.md 第6.2、6.3节）。

- tv compare --symbol X：一个标的的重叠比对（OAS 开启长历史前必须先做，数值须完全一致）。
- tv crosscheck：全部 usage=crosscheck 且登记了 api_source 的标的，写 reports/tradingview_crosscheck.md。
比对只报告差异，不修改任何数据；TradingView 的 ETF、VIX、收益率数据不参与评分。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field

from market_risk.config import Settings, SymbolInfo
from market_risk.storage.paths import StoragePaths

MAX_LISTED = 20
# 股息调整检查（ETF 收盘价）：相对偏差 = TV / 接口 − 1
ADJ_MIN_REL = 0.0005        # 偏差小于 0.05% 视为一致（两位小数的舍入误差）
ADJ_NEG_SHARE = 0.8         # 有偏差的日期中 TV 偏低的比例 ≥ 80% 视为"系统性偏低"
ADJ_TREND = 0.005           # 最早 10% 日期的平均偏差比最近 10% 低 0.5 个百分点以上，视为"偏差随时间向前变大"
# 按时期统计：逐日回测区间从 2008-08-01（含）开始
BACKTEST_START = dt.date(2008, 8, 1)
DIFF_REPORT = 0.01          # 统计"差值绝对值超过 0.01"的天数
DIFF_LIST = 0.02            # 回测区间内差值绝对值超过 0.02 的日期全部列出（只对 LIST_SYMBOLS）
LIST_SYMBOLS = ("SPY", "QQQ", "RSP")


@dataclass(frozen=True)
class PeriodStats:
    """一个时期内的差异统计；差值 = TradingView − 接口。"""

    label: str
    overlap: int
    mismatches: int
    max_abs_diff: float
    over_report: int            # |差值| > 0.01
    positive: int
    negative: int


@dataclass(frozen=True)
class ThirdPartyCheck:
    """第三方不复权收盘价核对：判断 Yahoo 与 TradingView 哪一方正确。"""

    date: dt.date
    tv: float
    api: float
    third: float | None
    source: str

    @property
    def verdict(self) -> str:
        if self.third is None:
            return "第三方无数据"
        t = round(self.third, 2)
        tv_ok, api_ok = abs(t - self.tv) <= 0.005, abs(t - self.api) <= 0.005
        if tv_ok and api_ok:
            return "三方一致"
        if api_ok:
            return "Yahoo 正确"
        if tv_ok:
            return "TradingView 正确"
        return "三方都不同"


@dataclass
class CompareResult:
    symbol: str
    tv_symbol: str
    api_source: str
    tolerance: float
    overlap: int = 0
    first: dt.date | None = None
    last: dt.date | None = None
    mismatches: list[tuple[dt.date, float, float, float]] = field(default_factory=list)
    tv_only: list[dt.date] = field(default_factory=list)
    api_only: list[dt.date] = field(default_factory=list)
    error: str | None = None
    max_abs_diff: float = 0.0                   # 重叠日期的最大绝对差值（两位小数后）
    rel_first: float | None = None              # 最早 10% 重叠日期的平均相对偏差
    rel_last: float | None = None               # 最近 10% 重叠日期的平均相对偏差
    neg_share: float | None = None              # 有偏差的日期中 TV 偏低的比例
    check_adjustment: bool = False              # 是否做股息调整检查（接口为 Yahoo 不复权收盘价）
    overlap_before: int = 0                     # 2008-08-01 之前的重叠天数
    overlap_after: int = 0                      # 2008-08-01（含）之后的重叠天数
    not_applicable: str | None = None           # 口径不同、不做比对的原因
    third_party: list[ThirdPartyCheck] = field(default_factory=list)
    third_party_note: str | None = None         # 第三方核对未做或部分未做的说明

    def periods(self) -> list[PeriodStats]:
        """按时期分段：2008-08-01 之前、之后（逐日回测区间，含 2008-08-01）。"""
        out = []
        for label, keep, n in ((f"{BACKTEST_START} 之前", lambda d: d < BACKTEST_START, self.overlap_before),
                               (f"{BACKTEST_START} 起（回测区间）", lambda d: d >= BACKTEST_START, self.overlap_after)):
            diffs = [diff for d, _, _, diff in self.mismatches if keep(d)]
            out.append(PeriodStats(label, n, len(diffs), max((abs(x) for x in diffs), default=0.0),
                                   sum(1 for x in diffs if abs(x) > DIFF_REPORT + 1e-9),
                                   sum(1 for x in diffs if x > 0), sum(1 for x in diffs if x < 0)))
        return out

    def listed_dates(self) -> list[tuple[dt.date, float, float, float]]:
        """回测区间内差值绝对值超过 0.02 的日期（SPY、QQQ、RSP）。"""
        if self.symbol not in LIST_SYMBOLS:
            return []
        return [m for m in self.mismatches if m[0] >= BACKTEST_START and abs(m[3]) > DIFF_LIST + 1e-9]

    @property
    def adjustment_suspected(self) -> bool:
        """TV 收盘价相对接口系统性偏低，且偏差随时间向前变大：疑似开启了股息调整。"""
        if not self.check_adjustment or self.rel_first is None or self.rel_last is None or self.neg_share is None:
            return False
        return self.neg_share >= ADJ_NEG_SHARE and self.rel_first < self.rel_last - ADJ_TREND

    @property
    def ok(self) -> bool:
        return self.error is None and self.overlap > 0 and not self.mismatches

    @property
    def status(self) -> str:
        if self.not_applicable:
            return "不适用"
        if self.error:
            return "无法比对"
        if self.overlap == 0:
            return "无重叠"
        return "一致" if not self.mismatches else "有差异"


def compare_series(
    info: SymbolInfo, tv: Mapping[dt.date, float], api: Mapping[dt.date, float | None], tolerance: float
) -> CompareResult:
    """在两者日期范围的交集内逐日比对（按两位小数，差值超过容差为不一致）。"""
    res = CompareResult(info.symbol, info.tv_symbol, info.api_source or "", tolerance)
    api_valued = {d: float(v) for d, v in api.items() if v is not None}
    if not tv or not api_valued:
        res.error = "TradingView 或接口没有数据"
        return res
    lo, hi = max(min(tv), min(api_valued)), min(max(tv), max(api_valued))
    if lo > hi:
        return res
    common = sorted(d for d in tv if lo <= d <= hi and d in api_valued)
    res.overlap = len(common)
    res.overlap_before = sum(1 for d in common if d < BACKTEST_START)
    res.overlap_after = res.overlap - res.overlap_before
    res.first, res.last = (common[0], common[-1]) if common else (None, None)
    for d in common:
        a, b = round(tv[d], 2), round(api_valued[d], 2)
        res.max_abs_diff = max(res.max_abs_diff, round(abs(a - b), 4))
        if abs(a - b) > tolerance + 1e-9:
            res.mismatches.append((d, a, b, round(a - b, 4)))
    res.check_adjustment = (info.api_source or "").startswith("yahoo:")
    if res.check_adjustment and common:
        rel = [tv[d] / api_valued[d] - 1 for d in common if api_valued[d]]
        k = max(1, len(rel) // 10)
        res.rel_first, res.rel_last = sum(rel[:k]) / k, sum(rel[-k:]) / k
        off = [x for x in rel if abs(x) >= ADJ_MIN_REL]
        res.neg_share = sum(1 for x in off if x < 0) / len(off) if off else 0.0
    res.tv_only = sorted(d for d in tv if lo <= d <= hi and d not in api_valued)
    res.api_only = sorted(d for d in api_valued if lo <= d <= hi and d not in tv)
    return res


def fetch_api_series(
    spec: str, start: dt.date, end: dt.date, settings: Settings, paths: StoragePaths,
    api_key: str | None, refresh: bool = False,
) -> dict[dt.date, float | None]:  # pragma: no cover - 网络请求
    """按 api_source（如 yahoo:SPY、fred:BAMLH0A0HYM2、treasury:10Y、cboe:VIX）取接口数据。"""
    from market_risk.data import cboe, fred, prices, treasury

    source, _, key = spec.partition(":")
    retry = {"max_retries": settings.max_retries, "backoff_seconds": settings.backoff_seconds}
    if source == "yahoo":
        series, _ = prices.fetch_closes(paths, key, end, (end - start).days, refresh, **retry)
        return dict(series)
    if source == "fred":
        if not api_key:
            raise ValueError("缺少 FRED_API_KEY")
        series, _ = fred.fetch_series(paths, key, start, end, api_key, refresh=refresh, **retry)
        return dict(series)
    if source == "treasury":
        values, _, _ = treasury.fetch_ten_year(paths, start, end, api_key, refresh, **retry)
        return dict(values)
    if source == "cboe":
        series, _ = cboe.fetch_vix_history(paths, start, end, settings.cboe_vix_history_url, refresh, **retry)
        return dict(series)
    raise ValueError(f"未知的 api_source：{spec}")


def format_results(results: list[CompareResult], title: str, generated_at: str) -> str:
    lines = [f"# {title}", "", f"生成时间（UTC）：{generated_at}", "",
             "比对口径：两者日期范围交集内逐日比对收盘值（两位小数），差值超过容差为不一致。"
             "只报告差异，不修改数据；TradingView 的 ETF、VIX、收益率数据不参与评分。", "",
             "| 标的 | TV代码 | 接口 | 重叠区间 | 重叠天数 | 不一致 | 最大差值 | 最近不一致日 | 2010年起不一致 | "
             "仅TV有 | 仅接口有 | 结果 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        span = f"{r.first} 至 {r.last}" if r.first else "-"
        lines.append(f"| {r.symbol} | {r.tv_symbol} | {r.api_source} | {span} | {r.overlap} | "
                     f"{len(r.mismatches)} | {r.max_abs_diff:.2f} | {r.mismatches[-1][0] if r.mismatches else '-'} | "
                     f"{sum(1 for m in r.mismatches if m[0].year >= 2010)} | "
                     f"{len(r.tv_only)} | {len(r.api_only)} | {r.status} |")
    compared = [r for r in results if r.overlap]
    if compared:
        lines += ["", "## 按时期统计", "",
                  f"差值 = TradingView − 接口（两位小数）。不一致：|差值| 超过容差；"
                  f"\"> {DIFF_REPORT}\"：|差值| 超过 {DIFF_REPORT}。", "",
                  "| 标的 | 时期 | 重叠天数 | 不一致 | 最大 |差值| | |差值| > 0.01 | 正差值 | 负差值 |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in compared:
            for s in r.periods():
                lines.append(f"| {r.symbol} | {s.label} | {s.overlap} | {s.mismatches} | {s.max_abs_diff:.2f} | "
                             f"{s.over_report} | {s.positive} | {s.negative} |")
    listed = [r for r in results if r.listed_dates()]
    if listed or any(r.symbol in LIST_SYMBOLS for r in compared):
        lines += ["", f"## 回测区间内差值超过 {DIFF_LIST} 的日期（{'、'.join(LIST_SYMBOLS)}）与第三方核对", ""]
        for r in (x for x in compared if x.symbol in LIST_SYMBOLS):
            items = r.listed_dates()
            lines.append(f"### {r.symbol}（{len(items)} 个）")
            lines.append("")
            if not items:
                lines += ["无", ""]
                continue
            checks = {c.date: c for c in r.third_party}
            lines += ["| 日期 | TradingView | Yahoo | 差值 | 第三方 | 来源 | 判断 |", "|---|---|---|---|---|---|---|"]
            for d, a, b, diff in items:
                c = checks.get(d)
                third = "-" if c is None or c.third is None else f"{c.third:.2f}"
                lines.append(f"| {d} | {a:.2f} | {b:.2f} | {diff:+.2f} | {third} | {c.source if c else '-'} | "
                             f"{c.verdict if c else '未核对'} |")
            if r.third_party_note:
                lines.append(f"\n{r.third_party_note}")
            lines.append("")
    adj = [r for r in results if r.check_adjustment and r.rel_first is not None]
    if adj:
        lines += ["", "## 股息调整检查（ETF）", "",
                  "TradingView 若开启了股息调整，历史收盘价会相对 Yahoo 不复权 `Close` 系统性偏低，且越早偏差越大。"
                  f"判定：有偏差（≥{ADJ_MIN_REL:.2%}）的日期中 TV 偏低的比例 ≥ {ADJ_NEG_SHARE:.0%}，"
                  f"且最早 10% 日期的平均偏差比最近 10% 低 {ADJ_TREND:.1%} 以上。", "",
                  "| 标的 | 最早10%平均偏差 | 最近10%平均偏差 | 有偏差日中TV偏低比例 | 结论 |", "|---|---|---|---|---|"]
        for r in adj:
            verdict = "疑似开启了股息调整" if r.adjustment_suspected else "未见股息调整迹象"
            lines.append(f"| {r.symbol} | {r.rel_first:+.4%} | {r.rel_last:+.4%} | {r.neg_share:.0%} | {verdict} |")
    for r in results:
        if r.not_applicable:
            lines += ["", f"## {r.symbol}：不适用", "", f"- {r.not_applicable}"]
            continue
        if r.error or r.mismatches or r.tv_only or r.api_only:
            lines += ["", f"## {r.symbol}（容差 {r.tolerance}）"]
            if r.error:
                lines.append(f"- 无法比对：{r.error}")
            if r.mismatches:
                lines += ["", "| 日期 | TradingView | 接口 | 差值 |", "|---|---|---|---|"]
                lines += [f"| {d} | {a:.2f} | {b:.2f} | {diff:+.4f} |" for d, a, b, diff in r.mismatches[:MAX_LISTED]]
                if len(r.mismatches) > MAX_LISTED:
                    lines.append(f"\n（共 {len(r.mismatches)} 处，只列前 {MAX_LISTED} 处）")
            for label, ds in (("仅 TradingView 有数据的日期", r.tv_only), ("仅接口有数据的日期", r.api_only)):
                if ds:
                    shown = "、".join(str(d) for d in ds[:MAX_LISTED])
                    lines.append(f"- {label}（{len(ds)} 个）：{shown}{' 等' if len(ds) > MAX_LISTED else ''}")
    return "\n".join(lines) + "\n"
