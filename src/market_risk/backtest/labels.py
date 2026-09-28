"""回测的结果标签与回调事件标签（隔离数据：使用基准日之后的数据，评分代码不得读取）。

保留期屏蔽原则（SPEC 5.6/STORAGE，2026-09-27）：任何字段，只要其取值需要用到保留期的数据，未解锁时一律屏蔽。
- 结果标签：结果窗口结束于保留期的，不写出；
- 回调：高点在保留期的不写出；低点确认（未确认时为序列末尾）发生在保留期的，只写高点，状态"跨入保留期，未解锁"；
  收复前高的日期落在保留期的，留空并注明"在保留期，未解锁"；
- 回调窗口：不写保留期的行。
区间归属：按高点日期；低点确认（未确认为序列末尾）晚于所属区间末日的，标注"跨越区间边界"，不计入任一区间统计。
高点早于回测起点的回调标注"起点之前开始"，不计入开发期统计；其窗口从回测起点开始。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from market_risk.backtest.settings import BEFORE_START, DEVELOPMENT, VALIDATION, BacktestConfig
from market_risk.backtest.zigzag import CONFIRMED, Swing, zigzag
from market_risk.outcomes import Label, OutcomeError, compute_label, outcome_window

MASKED = "跨入保留期，未解锁"
HOLDOUT_NOTE = "在保留期，未解锁"
PCT_Q = Decimal("0.0001")


@dataclass(frozen=True)
class Episode:
    symbol: str
    level: Decimal
    high_date: dt.date
    high_close: Decimal
    low_date: dt.date | None           # 屏蔽时为 None
    low_close: Decimal | None
    drawdown_pct: Decimal | None       # 百分数（负数），4位小数
    trading_days: int | None
    grade: str
    status: str                        # 已确认 / 未确认 / 跨入保留期，未解锁
    confirm_date: dt.date | None
    recovery_date: dt.date | None
    recovery_note: str
    period: str                        # 开发期 / 验证期 / 保留期 / 起点之前
    before_start: bool
    crosses_boundary: bool
    counted: bool                      # 是否计入所属区间的统计（开发期、验证期的已确认回调，且未跨越边界）
    window_end: dt.date | None         # 回调窗口的最后一天（低点后 N 个交易日或序列末尾）

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.level}|{self.high_date}"


def pct(v: Decimal) -> Decimal:
    return (v * 100).quantize(PCT_Q, ROUND_HALF_UP)


def build_episodes(symbol: str, closes: Sequence[tuple[dt.date, Decimal]], cfg: BacktestConfig,
                   unlock: bool = False) -> list[Episode]:
    """在全部历史上按各层级运行 ZigZag，并应用区间归属、跨越边界与保留期屏蔽。"""
    if not closes:
        return []
    last = closes[-1][0]
    days = [d for d, _ in closes]
    index = {d: i for i, d in enumerate(days)}
    out = []
    for level in cfg.levels[symbol]:
        for s in zigzag(closes, level):
            out.extend(_episode(symbol, s, cfg, unlock, last, days, index))
    return out


def _episode(symbol: str, s: Swing, cfg: BacktestConfig, unlock: bool, last: dt.date,
             days: Sequence[dt.date], index: Mapping[dt.date, int]) -> list[Episode]:
    period = cfg.period_of(s.high_date)
    if cfg.in_holdout(s.high_date) and not unlock:
        return []
    needed = s.confirm_date or last                    # 确定低点所需数据的最后一天
    end = cfg.period_end(period)
    crosses = end is not None and needed > end
    window_end = days[min(index[s.low_date] + cfg.window_sessions, len(days) - 1)] \
        if s.status == CONFIRMED else last
    recovery, note = s.recovery_date, ""
    if recovery is not None and cfg.in_holdout(recovery) and not unlock:
        recovery, note = None, HOLDOUT_NOTE
    if cfg.in_holdout(needed) and not unlock:
        return [Episode(symbol, s.level, s.high_date, s.high_close, None, None, None, None, "", MASKED, None,
                        None, HOLDOUT_NOTE if s.recovery_date else "", period, period == BEFORE_START, crosses,
                        False, None)]
    counted = period in (DEVELOPMENT, VALIDATION) and not crosses and s.status == CONFIRMED
    return [Episode(symbol, s.level, s.high_date, s.high_close, s.low_date, s.low_close, pct(s.drawdown),
                    s.trading_days, cfg.grades[symbol].grade(-s.drawdown), s.status, s.confirm_date, recovery, note,
                    period, period == BEFORE_START, crosses, counted, window_end)]


Q6 = Decimal("0.000001")


@dataclass(frozen=True)
class WindowRow:
    symbol: str
    level: Decimal
    high_date: dt.date
    date: dt.date
    offset: int                         # 相对高点的交易日偏移（高点为 0）
    scores: Mapping[str, tuple[int | None, int, int, str | None]]   # 版本 → (总分, 下限, 上限, 阶段)
    # 以下四列依赖低点：低点未确认或被屏蔽的回调一律为 None（补充1，2026-09-27）
    offset_from_trough: int | None = None      # 相对低点的交易日偏移（低点为 0）
    drawdown_from_peak: Decimal | None = None  # 当天收盘价相对高点收盘价的跌幅（百分数，≤0 时为跌），6位小数
    decline_progress: Decimal | None = None    # (高点收盘价 − 当天收盘价) ÷ (高点收盘价 − 低点收盘价)，6位小数
    rebound_from_trough: Decimal | None = None  # 当天收盘价相对低点收盘价的涨幅（百分数），低点之前为 None


def window_measures(close: Decimal, high: Decimal, low: Decimal, offset_from_trough: int
                    ) -> tuple[Decimal, Decimal, Decimal | None]:
    """(相对高点的跌幅 %, 已完成跌幅比例, 相对低点的涨幅 %)：Decimal 计算，6位小数，ROUND_HALF_UP。"""
    dd = ((close / high - 1) * 100).quantize(Q6, ROUND_HALF_UP)
    progress = ((high - close) / (high - low)).quantize(Q6, ROUND_HALF_UP)
    rebound = None if offset_from_trough < 0 else ((close / low - 1) * 100).quantize(Q6, ROUND_HALF_UP)
    return dd, progress, rebound


def episode_windows(ep: Episode, days: Sequence[dt.date], scores: Mapping[tuple[dt.date, str], tuple],
                    versions: Sequence[str], cfg: BacktestConfig, unlock: bool = False,
                    closes: Mapping[dt.date, Decimal] | None = None) -> list[WindowRow]:
    """高点前 N 个交易日至低点后 N 个交易日（未确认到序列末尾；屏蔽的回调到验证期末）；只写有分数的日期。

    closes：该标的的收盘价（公布的两位小数），用于计算依赖低点的四列；低点未确认或被屏蔽时四列留空。
    """
    index = {d: i for i, d in enumerate(days)}
    h = index[ep.high_date]
    last = ep.window_end or days[-1]
    trough = ep.status == CONFIRMED and ep.low_date is not None and ep.low_close is not None
    rows = []
    for d in days[max(h - cfg.window_sessions, 0): index[last] + 1]:
        if d < cfg.start or (cfg.in_holdout(d) and not unlock):
            continue
        per = {v: scores[d, v] for v in versions if (d, v) in scores}
        if not per:
            continue
        extra: dict[str, object] = {}
        if trough and closes is not None and d in closes:
            ot = index[d] - index[ep.low_date]  # type: ignore[index]
            dd, progress, rebound = window_measures(closes[d], ep.high_close, ep.low_close, ot)  # type: ignore[arg-type]
            extra = {"offset_from_trough": ot, "drawdown_from_peak": dd, "decline_progress": progress,
                     "rebound_from_trough": rebound}
        rows.append(WindowRow(ep.symbol, ep.level, ep.high_date, d, index[d] - h, per, **extra))  # type: ignore[arg-type]
    return rows


@dataclass(frozen=True)
class OutcomeRow:
    base_date: dt.date
    window_start: dt.date
    window_end: dt.date
    label: Label | None
    period: str
    crosses_period: bool                # 结果窗口跨入下一区间：不计入本区间统计
    data_note: str = ""


def outcome_rows(bases: Sequence[dt.date], spx: Mapping[dt.date, Decimal], qqq: Mapping[dt.date, Decimal],
                 cfg: BacktestConfig, unlock: bool = False) -> list[OutcomeRow]:
    """结果窗口已结束的基准日；未解锁时屏蔽跨入保留期的窗口。

    窗口结束 = SPX、QQQ 两个序列的最新日期都不早于窗口最后一个交易日（取两者较早者比较）；
    否则为"窗口未结束"，不生成该行。窗口已结束但窗口内缺价时写一行空标签，data_note 注明"数据不齐"。
    """
    last = min(max(spx, default=dt.date.min), max(qqq, default=dt.date.min))
    out = []
    for base in bases:
        _, end = outcome_window(base)
        if end > last or (cfg.in_holdout(end) and not unlock):
            continue
        period = cfg.period_of(base)
        period_end = cfg.period_end(period)
        start, _ = outcome_window(base)
        try:
            lab = compute_label(base, spx, qqq)
            note = ""
        except OutcomeError as exc:
            lab = None
            note = f"数据不齐：{exc}"
        out.append(OutcomeRow(base, start, end, lab, period, period_end is not None and end > period_end, note))
    return out


@dataclass(frozen=True)
class LabelImpact:
    """指数争议日的标签实质影响检验：用另一来源的指数值重算受影响的结果标签与回调事件。"""

    symbol: str
    date: dt.date
    value_a: Decimal
    value_b: Decimal
    outcome_bases: int                         # 结果窗口包含该日（或以该日为基准日）的基准日数
    outcome_diffs: tuple[str, ...]
    episode_diffs: tuple[str, ...]
    incomplete_bases: tuple[dt.date, ...] = ()  # 两种来源都因数据不齐无法生成标签的基准日（不算差异）

    @property
    def material(self) -> bool:
        return bool(self.outcome_diffs or self.episode_diffs)


def label_impact(symbol: str, day: dt.date, value_b: Decimal, spx: Mapping[dt.date, Decimal],
                 qqq: Mapping[dt.date, Decimal], cfg: BacktestConfig, unlock: bool = False) -> LabelImpact:
    """symbol 为 SPX 时影响结果标签（标普500部分）与 SPX 回调事件；其他指数不参与标签（NDX），无影响。"""
    series = {"SPX": spx}.get(symbol)
    value_a = series[day] if series is not None else Decimal(0)
    if series is None:
        return LabelImpact(symbol, day, value_a, value_b, 0, (), ())
    alt = {**series, day: value_b}
    days = sorted(series)
    i = days.index(day)
    bases = [d for d in days[max(0, i - 20): i + 1] if d >= cfg.start]
    a = {r.base_date: r.label for r in outcome_rows(bases, spx, qqq, cfg, unlock)}
    b = {r.base_date: r.label for r in outcome_rows(bases, alt, qqq, cfg, unlock)}
    outcome_diffs = []
    incomplete = []
    for base in sorted(set(a) | set(b)):
        la, lb = a.get(base), b.get(base)
        if la is None and lb is None:
            incomplete.append(base)
        elif la is None or lb is None:
            outcome_diffs.append(f"{base}：只有一种来源能生成标签（另一种数据不齐）")
        elif (la.is_event, la.is_near_event, la.event_date) != (lb.is_event, lb.is_near_event, lb.event_date):
            outcome_diffs.append(f"{base}：事件 {la.is_event}/{lb.is_event}，"
                                 f"接近事件 {la.is_near_event}/{lb.is_near_event}")
    ea = {(e.level, e.high_date, e.low_date, e.status, e.grade) for e in build_episodes(symbol, sorted(series.items()),
                                                                                        cfg, unlock)}
    eb = {(e.level, e.high_date, e.low_date, e.status, e.grade) for e in build_episodes(symbol, sorted(alt.items()),
                                                                                        cfg, unlock)}
    episode_diffs = tuple(f"{'仅 Yahoo' if x in ea else '仅 TradingView'}：层级 {x[0]} 高点 {x[1]} 低点 {x[2]}"
                          for x in sorted(ea ^ eb, key=str))
    return LabelImpact(symbol, day, value_a, value_b, len(bases), tuple(outcome_diffs), episode_diffs,
                       tuple(incomplete))
