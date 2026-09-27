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
