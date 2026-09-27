"""争议价格的离线残差审查；双侧窗口只用于数据审计，不得用于基准日评分。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from statistics import median

from market_risk import calendar as cal
from market_risk.config import PROJECT_ROOT, ConfigError, _read_yaml

Series = Mapping[dt.date, Decimal]


@dataclass(frozen=True)
class ReviewConfig:
    window_sessions: int
    mad_scale: Decimal
    sigma_floor: Decimal
    abnormal_z: Decimal
    normal_z: Decimal
    high_abnormal_z: Decimal
    high_normal_z: Decimal
    reversal_min: Decimal
    reversal_max: Decimal
    index_tolerance: Decimal
    agree_tolerance: Decimal            # 多数一致：两来源"一致"的容差（两日都≤）
    differ_tolerance: Decimal           # 多数一致：Yahoo 与另外两方"不一致"的容差（争议日>）
    benchmarks: Mapping[str, str]
    index_tickers: Mapping[str, str]
    cases: tuple[tuple[str, dt.date], ...]


def load_review_config(path: Path = PROJECT_ROOT / "config" / "price_dispute_review.yaml") -> ReviewConfig:
    raw = _read_yaml(path)
    names = ("mad_scale", "sigma_floor", "abnormal_z", "normal_z", "high_abnormal_z", "high_normal_z",
             "reversal_min", "reversal_max", "index_tolerance", "agree_tolerance", "differ_tolerance")
    try:
        numbers = {name: Decimal(str(raw[name])) for name in names}
        if any(not v.is_finite() or v <= 0 for v in numbers.values()):
            raise ValueError("参数必须为有限正数")
        config = ReviewConfig(int(raw["window_sessions"]), **numbers, benchmarks=raw["benchmarks"],
                              index_tickers=raw["index_tickers"],
                              cases=tuple((c["symbol"], dt.date.fromisoformat(str(c["date"]))) for c in raw["cases"]))
        if (config.window_sessions < 2 or config.reversal_min > config.reversal_max
                or config.high_abnormal_z <= config.abnormal_z or config.high_normal_z > config.normal_z):
            raise ValueError("窗口或门槛次序错误")
        if len(set(config.cases)) != len(config.cases) or not config.cases:
            raise ValueError("争议日期为空或重复")
        for symbol, _ in config.cases:
            if config.benchmarks[symbol] not in config.index_tickers:
                raise ValueError("缺少指数接口代码")
        return config
    except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
        raise ConfigError(f"价格审查配置错误：{exc}") from exc


def published_prices(values: Mapping[dt.date, float | None]) -> dict[dt.date, Decimal]:
    """与原交叉校验一致，按公布的两位小数读取价格；派生量不取整。"""
    return {d: Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for d, v in values.items() if v is not None}


@dataclass(frozen=True)
class ResidualStats:
    e0: Decimal
    e1: Decimal
    sigma: Decimal
    sample_count: int
    excluded_dividends: tuple[dt.date, ...]

    @property
    def z0(self) -> Decimal:
        return abs(self.e0) / self.sigma

    @property
    def z1(self) -> Decimal:
        return abs(self.e1) / self.sigma

    @property
    def reversal(self) -> Decimal | None:
        return abs(self.e1 / self.e0) if self.e0 else None

    def abnormal(self, config: ReviewConfig) -> bool:
        ratio = self.reversal
        return (self.z0 > config.abnormal_z and self.e0 * self.e1 < 0 and ratio is not None
                and config.reversal_min <= ratio <= config.reversal_max)


def residual_stats(
    day: dt.date, prices: Series, index: Series, dividends: frozenset[dt.date], config: ReviewConfig,
) -> ResidualStats:
    """前后各N个股票交易日；额外取前一日仅用于计算第一天收益率。缺数不缩短窗口。"""
    days = cal.stock_trading_days(cal.shift_trading_days(day, -config.window_sessions - 1),
                                 cal.shift_trading_days(day, config.window_sessions))
    next_day = cal.shift_trading_days(day, 1)
    for name, series in (("ETF", prices), ("指数", index)):
        missing = [d for d in days if d not in series or not series[d].is_finite() or series[d] <= 0]
        if missing:
            raise ValueError(f"{name} 缺失或无效：{missing[0]}")
    residuals = {t: prices[t] / prices[p] - index[t] / index[p] for p, t in pairwise(days)}
    normal = [v for t, v in residuals.items() if t not in {day, next_day} and t not in dividends]
    if not normal:
        raise ValueError("排除后无正常波动样本")
    center = median(normal)
    sigma = max(config.sigma_floor, config.mad_scale * median([abs(v - center) for v in normal]))
    return ResidualStats(residuals[day], residuals[next_day], sigma, len(normal),
                         tuple(sorted(dividends & set(residuals) - {day, next_day})))


def classify(tv: ResidualStats, yahoo: ResidualStats, config: ReviewConfig) -> tuple[str, str, str]:
    for name, suspect, other in (("TradingView", tv, yahoo), ("Yahoo", yahoo, tv)):
        if suspect.abnormal(config) and max(other.z0, other.z1) <= config.normal_z:
            confidence = ("高" if suspect.z0 > config.high_abnormal_z
                          and max(other.z0, other.z1) <= config.high_normal_z else "中")
            return f"{name} 有误", confidence, "该来源超过异常门槛且次日反向回归，另一来源两日均在正常门槛内"
    return "无法判定", "不适用", "未同时满足单方异常、次日反向回归及另一方两日正常的完整条件"


@dataclass(frozen=True)
class ReviewResult:
    symbol: str
    date: dt.date
    next_date: dt.date
    benchmark: str
    tv: ResidualStats | None
    yahoo: ResidualStats | None
    index_differences: tuple[Decimal, ...]
    verdict: str
    confidence: str
    reason: str


def review_case(
    symbol: str, day: dt.date, tv: Series, yahoo: Series, index: Series, yahoo_index: Series,
    dividends: frozenset[dt.date], config: ReviewConfig,
) -> ReviewResult:
    next_day = cal.shift_trading_days(day, 1)
    a = b = None
    diffs: tuple[Decimal, ...] = ()
    try:
        ex_div = sorted({day, next_day} & dividends)
        if ex_div:   # 不复权收盘价在除息日下跌一个分红额，会混入残差
            raise ValueError(f"争议日或次日为除息日（{', '.join(map(str, ex_div))}），分红会混入残差")
        a = residual_stats(day, tv, index, dividends, config)
        b = residual_stats(day, yahoo, index, dividends, config)
        for t in (day, next_day):
            if t not in yahoo_index or not yahoo_index[t].is_finite() or yahoo_index[t] <= 0:
                raise ValueError(f"Yahoo 指数缺失或无效：{t}")
        diffs = tuple(abs(index[t] - yahoo_index[t]) for t in (day, next_day))
        if max(diffs) > config.index_tolerance:
            verdict, confidence, reason = "无法判定", "不适用", "指数自身核对差值超过容差，否决残差法判定"
        else:
            verdict, confidence, reason = classify(a, b, config)
    except ValueError as exc:
        verdict, confidence, reason = "无法判定", "不适用", str(exc)
    return ReviewResult(symbol, day, next_day, config.benchmarks[symbol], a, b, diffs,
                        verdict, confidence, reason)


def explain(result: ReviewResult, config: ReviewConfig, adjacent: tuple[dt.date, ...] = ()) -> str:
    """由计算结果生成"为什么无法判定"的说明（不写死具体日期）。"""
    if result.tv is None or result.yahoo is None:
        return result.reason
    parts = []
    for name, s, other in (("TradingView", result.tv, result.yahoo), ("Yahoo", result.yahoo, result.tv)):
        ratio = s.reversal
        opposite = s.e0 * s.e1 < 0
        in_range = ratio is not None and config.reversal_min <= ratio <= config.reversal_max
        other_max = max(other.z0, other.z1)
        if s.abnormal(config):
            parts.append(f"{name} 满足异常条件，但另一来源两日 z 最大为 {other_max:.2f}，超过 {config.normal_z}")
        elif s.z0 > config.abnormal_z:
            why = ("两日残差同号" if not opposite
                   else f"回归幅度比 {ratio:.2f} 不在 [{config.reversal_min},{config.reversal_max}]")
            parts.append(f"{name} 的 z(d)={s.z0:.2f} 超过 {config.abnormal_z}，但{why}")
        elif s.z0 > config.normal_z:
            shape = (f"次日反向回归（幅度比 {ratio:.2f}）" if opposite and in_range
                     else "两日残差同号" if not opposite else f"反向但幅度比 {ratio:.2f} 不在区间内")
            parts.append(f"{name} 的 z(d)={s.z0:.2f}，{shape}，但未超过异常门槛 {config.abnormal_z}")
    if not parts:
        parts.append(f"两个来源的 z(d) 均不超过 {config.normal_z}（TradingView {result.tv.z0:.2f}，"
                     f"Yahoo {result.yahoo.z0:.2f}），残差法看不出哪一方异常")
    if result.index_differences and max(result.index_differences) > config.index_tolerance:
        parts.append("指数核对差值超过容差")
    if adjacent:
        parts.append(f"与相邻交易日 {'、'.join(map(str, adjacent))} 同为争议日，单日错误的假设有局限")
    return "；".join(parts)


@dataclass(frozen=True)
class MajorityResult:
    """第三方证据的"多数一致"判定（SPEC 6.0 争议处理原则；口径 2026-09-27 确认）。"""

    source: str
    tv: tuple[Decimal | None, Decimal | None]         # 争议日、次日
    yahoo: tuple[Decimal | None, Decimal | None]
    third: tuple[Decimal | None, Decimal | None]
    verdict: str
    suggested: Decimal | None = None


def majority_verdict(tv: tuple[Decimal | None, Decimal | None], yahoo: tuple[Decimal | None, Decimal | None],
                     third: tuple[Decimal | None, Decimal | None], config: ReviewConfig,
                     source: str = "Tiingo") -> MajorityResult:
    """三处口径：
    1. TradingView 与第三方两日都相差 ≤agree，且争议日 Yahoo 与两者都相差 >differ → 多数一致：Yahoo 有误；
    2. 第三方与 Yahoo 两日都相差 ≤agree → Yahoo 正确；
    3. 其余组合一律无法判定。
    """
    if any(v is None for v in (*tv, *yahoo, *third)):
        return MajorityResult(source, tv, yahoo, third, "第三方或来源数据缺失，无法判定")

    def agree(a: tuple, b: tuple) -> bool:
        return all(abs(x - y) <= config.agree_tolerance for x, y in zip(a, b, strict=True))

    yahoo_differs = (abs(yahoo[0] - tv[0]) > config.differ_tolerance  # type: ignore[operator]
                     and abs(yahoo[0] - third[0]) > config.differ_tolerance)  # type: ignore[operator]
    if agree(tv, third) and yahoo_differs:
        # 建议值取两者一致的数值；两者相差不超过容差但不完全相同时不给建议值，由负责人选定
        suggested = tv[0] if tv[0] == third[0] else None
        return MajorityResult(source, tv, yahoo, third, "多数一致：Yahoo 有误", suggested)
    if agree(yahoo, third):
        return MajorityResult(source, tv, yahoo, third, "Yahoo 正确")
    return MajorityResult(source, tv, yahoo, third, "无法判定")


@dataclass(frozen=True)
class ImpliedPrice:
    """由对应指数推算的隐含价格：争议日指数 × 前后交易日 ETF/指数比值的平均。只作证据展示，不改变判定。"""

    etf_source: str
    prev_day: dt.date
    next_day: dt.date
    ratio_prev: Decimal | None
    ratio_next: Decimal | None
    value: Decimal | None
    notes: tuple[str, ...] = ()


def implied_price(day: dt.date, etf: Series, index: Series, etf_source: str,
                  dividends: frozenset[dt.date] = frozenset(),
                  disputed: frozenset[dt.date] = frozenset()) -> ImpliedPrice:
    prev_day, next_day = cal.shift_trading_days(day, -1), cal.shift_trading_days(day, 1)

    def ratio(t: dt.date) -> Decimal | None:
        return etf[t] / index[t] if t in etf and t in index and index[t] else None

    rp, rn = ratio(prev_day), ratio(next_day)
    notes = [f"{t} 为除息日，比值含分红影响" for t in (prev_day, next_day) if t in dividends]
    notes += [f"{t} 本身也是争议日" for t in (prev_day, next_day) if t in disputed]
    value = (rp + rn) / 2 * index[day] if rp is not None and rn is not None and day in index else None
    if value is None:
        notes.append("相邻交易日或当日指数缺数，无法推算")
    return ImpliedPrice(etf_source, prev_day, next_day, rp, rn, value, tuple(notes))


def disposition(result: ReviewResult, majority: MajorityResult | None, impact_verdict: str | None,
                corrected: str | None, config: ReviewConfig) -> str:
    """处理结论：由修正条目、残差法、多数一致与实质影响检验的结果生成。"""
    if corrected is not None:
        return f"已修正为 {corrected}（config/data_decisions.yaml）"
    note = ""
    y = result.yahoo
    reversal_shape = (y is not None and y.e0 * y.e1 < 0 and y.reversal is not None
                      and config.reversal_min <= y.reversal <= config.reversal_max
                      and config.normal_z < y.z0 <= config.abnormal_z)
    if reversal_shape and majority is not None and majority.verdict == "Yahoo 正确":
        note = (f"；注：Yahoo 残差呈单日异常、次日反转形态（z(d)={y.z0:.2f}，未超过 {config.abnormal_z}），"  # type: ignore[union-attr]
                "但第三方与 Yahoo 一致，按规则判 Yahoo 正确；不为此修改规则或门槛")
    impact = f"，{impact_verdict}" if impact_verdict else ""
    # 建议修正的条件（负责人事先约定）：(a) 残差法判 Yahoo 有误且置信为中或高；(b) 多数一致判 Yahoo 有误
    if (result.verdict == "Yahoo 有误" and result.confidence in ("中", "高")) or (
            majority is not None and majority.verdict == "多数一致：Yahoo 有误"):
        return f"满足建议修正条件，待负责人批准{impact}"
    if majority is not None and majority.verdict == "Yahoo 正确":
        return f"保留 Yahoo 原值：多数一致规则判 Yahoo 正确{impact}{note}"
    return f"保留 Yahoo 原值：无法判定{impact}"


def render_review(results: tuple[ReviewResult, ...], config: ReviewConfig, provenance: list[str],
                  third_party: Mapping[tuple[str, dt.date], str],
                  corrections: tuple[tuple[str, dt.date, str, str, str], ...] = (),
                  extra_sections: tuple[str, ...] = (),
                  majority: Mapping[tuple[str, dt.date], MajorityResult] | None = None,
                  implied: Mapping[tuple[str, dt.date], tuple[ImpliedPrice, ...]] | None = None,
                  impact_verdicts: Mapping[tuple[str, dt.date], str] | None = None) -> str:
    """corrections：config/data_decisions.yaml 中已批准的修正（标的、日期、修正值、证据、裁定日期）；
    third_party：第三方核对的说明（不复权验证等）；majority：多数一致判定；implied：隐含价格证据。"""
    majority = majority or {}
    implied = implied or {}
    impact_verdicts = impact_verdicts or {}
    lines = ["# 争议收盘价审查", "", "## 方法与边界", "",
             "这是事后数据审计：双侧窗口含争议日之后的数据，只用于核查输入质量，不参与当日评分。",
             "收益率及残差用 Decimal 计算，不先取整；e、σ 以下用基点展示（1bp=0.0001），z 为 |e|/σ。",
             f"e = ETF 日收益率 − TradingView 指数日收益率；正常窗口为前后各{config.window_sessions}个交易日，",
             "排除争议日、次日及该 ETF 除息日，首个残差另需前一日收盘。缺数据不缩短窗口、不插值；"
             "争议日或次日本身是除息日时无法判定。",
             f"σ=max({config.sigma_floor}, {config.mad_scale}×MAD)。异常要求 z(d)>{config.abnormal_z}，",
             f"两日残差异号，|e(d+1)|/|e(d)|∈[{config.reversal_min},{config.reversal_max}]；",
             f"同时另一来源两日 z≤{config.normal_z} 才判错。高置信须前者 z(d)>{config.high_abnormal_z}，",
             f"后者两日 z≤{config.high_normal_z}，否则为中。指数任一天差>{config.index_tolerance}点则无法判定。",
             f"参照指数：{'；'.join(f'{k} 对应 {v}' for k, v in sorted(config.benchmarks.items()))}。"
             "其余情况均无法判定，第三方证据单列，不覆盖固定算法结论。",
             f"第三方（多数一致，SPEC 6.0）：TradingView 与第三方两日都相差 ≤{config.agree_tolerance}，"
             f"且争议日 Yahoo 与两者都相差 >{config.differ_tolerance} → 多数一致：Yahoo 有误；"
             f"第三方与 Yahoo 两日都相差 ≤{config.agree_tolerance} → Yahoo 正确；其余组合一律无法判定。"
             "Yahoo 与 Tiingo 可能共享上游数据，两者一致不构成两份独立证据。",
             "隐含价格：争议日指数 × 前后交易日 ETF/指数比值的平均（指数取 TradingView），是独立证据，只作展示，"
             "不改变判定。",
             "参数与日期清单：`config/price_dispute_review.yaml`。", "",
             "## 结论", "", "| 标的 | 日期 | 判定 | 置信程度 | 依据 |", "|---|---|---|---|---|"]
    lines += [f"| {r.symbol} | {r.date} | {r.verdict} | {r.confidence} | {r.reason} |" for r in results]
    lines += ["", "## 已批准的价格修正（config/data_decisions.yaml，decision: correct）", ""]
    if corrections:
        lines += ["| 标的 | 日期 | 修正值 | 证据 | 裁定日期 |", "|---|---|---|---|---|"]
        lines += [f"| {s} | {d} | {v} | {e} | {when} |" for s, d, v, e, when in corrections]
    else:
        lines.append("无。")
    lines += ["", "## 逐日证据", ""]
    for r in results:
        lines += [f"### {r.symbol} {r.date}（次日 {r.next_date}，参照 {r.benchmark}）", "",
                  "| 来源 | e(d) bp | e(d+1) bp | σ bp | z(d) | z(d+1) | 回归幅度比 | 异号 | 异常条件 | 样本数 |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for name, s in (("TradingView", r.tv), ("Yahoo", r.yahoo)):
            if s is None:
                lines.append(f"| {name} | 缺数 | | | | | | | | |")
                continue
            ratio = f"{s.reversal:.6f}" if s.reversal is not None else "无定义"
            lines.append(f"| {name} | {s.e0 * 10000:.6f} | {s.e1 * 10000:.6f} | {s.sigma * 10000:.6f} | "
                         f"{s.z0:.6f} | {s.z1:.6f} | {ratio} | {'是' if s.e0 * s.e1 < 0 else '否'} | "
                         f"{'是' if s.abnormal(config) else '否'} | {s.sample_count} |")
        excluded = ", ".join(map(str, r.tv.excluded_dividends)) if r.tv else "未完成"
        lines += ["", f"正常样本排除的除息日：{excluded or '无'}。",
                  f"指数核对 |TV−Yahoo|（d / d+1，点）：{' / '.join(map(str, r.index_differences)) or '缺失'}。",
                  f"判定：{r.verdict}；置信程度：{r.confidence}。{r.reason}。",
                  f"第三方：{third_party.get((r.symbol, r.date), '未核对')}。"]
        m = majority.get((r.symbol, r.date))
        if m is not None:
            def two(v: tuple) -> str:
                return " / ".join("缺失" if x is None else f"{x:.2f}" for x in v)
            lines.append(f"多数一致（争议日 / 次日）：TradingView {two(m.tv)}；Yahoo {two(m.yahoo)}；"
                         f"{m.source} {two(m.third)} → {m.verdict}"
                         + (f"，建议值 {m.suggested:.2f}" if m.suggested is not None else "") + "。")
        for ip in implied.get((r.symbol, r.date), ()):
            val = "无法推算" if ip.value is None else f"{ip.value:.2f}"
            rp = "缺失" if ip.ratio_prev is None else f"{ip.ratio_prev:.6f}"
            rn = "缺失" if ip.ratio_next is None else f"{ip.ratio_next:.6f}"
            lines.append(f"隐含价格（ETF 取 {ip.etf_source}）：{val}（{ip.prev_day} 比值 {rp}，{ip.next_day} 比值 {rn}"
                         + (f"；{'；'.join(ip.notes)}" if ip.notes else "") + "）。")
        lines.append("")
    for section in extra_sections:
        lines += [section, ""]
    fixed = {(s, d): v for s, d, v, _, _ in corrections}
    lines += ["## 处理结论（由修正条目、残差法、多数一致与实质影响检验生成）", "",
              "| 标的 | 日期 | 残差法 | 多数一致 | 实质影响 | 处理 |", "|---|---|---|---|---|---|"]
    for r in results:
        key = (r.symbol, r.date)
        m = majority.get(key)
        handled = disposition(r, m, impact_verdicts.get(key), fixed.get(key), config)
        lines.append(f"| {r.symbol} | {r.date} | {r.verdict}（{r.confidence}） | {m.verdict if m else '未核对'} | "
                     f"{impact_verdicts.get(key, '未检验')} | {handled} |")
    lines.append("")
    uncertain = [r for r in results if r.verdict == "无法判定"]
    cases = {(r.symbol, r.date) for r in results}
    lines += ["## 无法判定的原因（由计算结果生成）", ""]
    if not uncertain:
        lines.append("无。")
    for r in uncertain:
        prev = cal.shift_trading_days(r.date, -1)
        adjacent = tuple(d for d in (prev, r.next_date) if (r.symbol, d) in cases)
        lines.append(f"- {r.symbol} {r.date}：{explain(r, config, adjacent)}。")
    lines += ["", f"本次 {len(results)} 项中有 {len(uncertain)} 项无法判定。无法判定不表示两方均正确；"
              "固定门槛不因个别日期调整。未运行全历史正常日的误判率扫描，争议样本结论不是算法准确率估计。",
              "", "## 输入版本与来源", "", *[f"- {p}" for p in provenance], ""]
    return "\n".join(lines)
