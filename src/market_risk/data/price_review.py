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
    benchmarks: Mapping[str, str]
    index_tickers: Mapping[str, str]
    cases: tuple[tuple[str, dt.date], ...]


def load_review_config(path: Path = PROJECT_ROOT / "config" / "price_dispute_review.yaml") -> ReviewConfig:
    raw = _read_yaml(path)
    names = ("mad_scale", "sigma_floor", "abnormal_z", "normal_z", "high_abnormal_z", "high_normal_z",
             "reversal_min", "reversal_max", "index_tolerance")
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


def render_review(results: tuple[ReviewResult, ...], config: ReviewConfig, provenance: list[str],
                  third_party: Mapping[tuple[str, dt.date], str]) -> str:
    lines = ["# 争议收盘价审查（任务1）", "", "## 方法与边界", "",
             "这是事后数据审计：双侧窗口含争议日之后的数据，只用于核查输入质量，不参与当日评分。",
             "收益率及残差用 Decimal 计算，不先取整；e、σ 以下用基点展示（1bp=0.0001），z 为 |e|/σ。",
             f"e = ETF 日收益率 − TradingView 指数日收益率；正常窗口为前后各{config.window_sessions}个交易日，",
             "排除争议日、次日及该 ETF 除息日，首个残差另需前一日收盘。缺数据不缩短窗口、不插值。",
             f"σ=max({config.sigma_floor}, {config.mad_scale}×MAD)。异常要求 z(d)>{config.abnormal_z}，",
             f"两日残差异号，|e(d+1)|/|e(d)|∈[{config.reversal_min},{config.reversal_max}]；",
             f"同时另一来源两日 z≤{config.normal_z} 才判错。高置信须前者 z(d)>{config.high_abnormal_z}，",
             f"后者两日 z≤{config.high_normal_z}，否则为中。指数任一天差>{config.index_tolerance}点则无法判定。",
             "SPY、RSP 对应 SPX；QQQ 对应 NDX。其余情况均无法判定，第三方证据单列，不覆盖固定算法结论。",
             "参数与日期清单：`config/price_dispute_review.yaml`。未添加任何实际 correct 条目。", "",
             "## 结论", "", "| 标的 | 日期 | 判定 | 置信程度 | 依据 |", "|---|---|---|---|---|"]
    lines += [f"| {r.symbol} | {r.date} | {r.verdict} | {r.confidence} | {r.reason} |" for r in results]
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
                  f"第三方：{third_party.get((r.symbol, r.date), '未核对')}。", ""]
    uncertain = sum(r.verdict == "无法判定" for r in results)
    lines += ["## 待负责人统一处理", "",
              f"本次 {len(results)} 项中有 {uncertain} 项无法判定。覆盖率低已在开发前报告；负责人随后授权先完成开发，",
              "把问题留在本报告统一处理。此授权不等于批准调参或实际修正价格。",
              "", "- 建议保持既定门槛，保留无法判定结论；无法判定不表示两方均正确。",
              "- SPY 2012-01-20、QQQ 2010-11-26 的 Yahoo 残差有反向回归，但未超过6σ，不能放宽门槛判错。",
              "- RSP 2016-06-24 两日残差同号，不符合反向回归条件。",
              "- SPY 2015-08-21 与下一交易日 08-24 均为争议日，单日错误假设有局限；不扩展算法，保留两项证据。",
              "- 请负责人决定是否批准实际修正条目、是否补充独立证据，以及将来是否另行修订审计门槛。",
              "- 未运行全历史正常日的误判率扫描；上述争议样本结论不是算法准确率估计。",
              "", "## 输入版本与来源", "", *[f"- {p}" for p in provenance], ""]
    return "\n".join(lines)
