"""汇总全部输入、截断到基准日（含），生成 MarketSnapshot（SPEC 6.1、第4节）。

本模块是纯计算：输入为已获取的原始序列（RawInputs），不联网、不读写文件，
不读取结果标签（docs/STORAGE.md 第5节）。构建完成后断言任何日期都不晚于基准日。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field

from market_risk import calendar as mcal
from market_risk.config import MarketHolidays
from market_risk.data.cboe import resolve_vix
from market_risk.indicators import (
    InsufficientDataError,
    p2,
    ratio,
    simple_moving_average,
    three_segment_both,
    window_max,
)
from market_risk.models import (
    BreadthReading,
    EtfSnapshot,
    MarketSnapshot,
    OasVintageValue,
    SourceInfo,
    ThreeSegmentResult,
)

Series = Mapping[dt.date, float | None]
MA_PERIODS = (5, 10, 20, 30, 50, 200)


class DataIntegrityError(ValueError):
    """数据缺日、日期对不上等问题：报错，不插值、不替代（SPEC 0.5）。"""


class LookaheadError(AssertionError):
    """快照中出现晚于基准日的数据。"""


@dataclass(frozen=True)
class RawInputs:
    """已获取的原始数据（可能含基准日之后的数据，由 build_snapshot 截断）。"""

    base_date: dt.date
    closes: dict[str, Series]                   # 各 ETF 不复权收盘价
    vix_fred: Series
    vix_cboe: Series | None                     # 备用源未取得时为 None
    treasury: Series                            # 财政部 10 年期（或 DGS10 备用源）
    treasury_coverage_start: dt.date            # 财政部数据请求的起点（债市日历覆盖起点）
    oas: Series                                 # FRED 当前版本，"." 为 None
    oas_vintage: Series | None                  # ALFRED 基准日版本；未核验时为 None
    breadth: dict[dt.date, BreadthReading]
    sources: tuple[SourceInfo, ...] = ()
    notes: tuple[str, ...] = ()
    mode: str = "backtest"


@dataclass
class _Notes:
    items: list[str] = field(default_factory=list)

    def add(self, note: str | None) -> None:
        if note and note not in self.items:
            self.items.append(note)


def truncate(series: Series, base_date: dt.date) -> dict[dt.date, float | None]:
    """截断到基准日（含）。"""
    return {d: v for d, v in series.items() if d <= base_date}


def _valued(series: Series) -> dict[dt.date, float]:
    return {d: float(v) for d, v in series.items() if v is not None and v == v}


def check_contiguous(symbol: str, closes: Mapping[dt.date, float], base_date: dt.date) -> None:
    """收盘价日期必须与 NYSE 交易日一一对应，且包含基准日；否则报错。"""
    if base_date not in closes:
        raise DataIntegrityError(f"{symbol} 没有基准日 {base_date} 的收盘价")
    first = min(closes)
    expected = mcal.stock_trading_days(first, base_date)
    have = sorted(closes)
    missing = sorted(set(expected) - set(have))
    extra = sorted(set(have) - set(expected))
    if missing or extra:
        raise DataIntegrityError(
            f"{symbol} 收盘价日期与 NYSE 交易日不一致：缺 {missing[:5]}，多 {extra[:5]}"
        )


def build_snapshot(
    raw: RawInputs,
    scored_symbols: tuple[str, ...] = ("SPY", "QQQ", "RSP"),
    ratio_symbols: tuple[str, str] = ("HYG", "LQD"),
    holidays: MarketHolidays | None = None,
) -> MarketSnapshot:
    base = raw.base_date
    notes = _Notes(list(raw.notes))

    # ---- 1. 截断到基准日（含）----
    closes = {s: _valued(truncate(c, base)) for s, c in raw.closes.items()}
    vix_fred = truncate(raw.vix_fred, base)
    vix_cboe = None if raw.vix_cboe is None else truncate(raw.vix_cboe, base)
    treasury = _valued(truncate(raw.treasury, base))
    oas = truncate(raw.oas, base)
    oas_vintage = None if raw.oas_vintage is None else truncate(raw.oas_vintage, base)
    breadth = {d: r for d, r in raw.breadth.items() if d <= base}

    # ---- 2. 日期参照 ----
    if raw.mode == "daily" and base not in treasury:
        coverage_end = base - dt.timedelta(days=1)
        notes.add(f"财政部尚未发布基准日 {base} 的 10 年期数值，利率维度记待补")
    else:
        coverage_end = base
    bond_cal = mcal.bond_calendar_from_dates(treasury, raw.treasury_coverage_start, coverage_end)
    refs = mcal.compute_date_references(base, bond_cal, oas)
    notes.add(mcal.o6_difference_note(refs))
    if holidays is not None:
        for n in mcal.check_bond_calendar(bond_cal, holidays, refs.t_minus_20, base):
            notes.add(f"债市日历核对：{n}")
        for n in mcal.check_stock_calendar(holidays, refs.three_segment_query_start, base):
            notes.add(f"股市日历核对：{n}")

    # ---- 3. ETF ----
    etfs: dict[str, EtfSnapshot] = {}
    for symbol in scored_symbols:
        if symbol not in closes:
            raise DataIntegrityError(f"缺少 {symbol} 的收盘价")
        c = closes[symbol]
        check_contiguous(symbol, c, base)
        mas = {p: simple_moving_average(c, base, p) for p in MA_PERIODS}
        etfs[symbol] = EtfSnapshot(
            symbol=symbol,
            close=p2(c[base]),
            ma5=mas[5], ma10=mas[10], ma20=mas[20], ma30=mas[30], ma50=mas[50], ma200=mas[200],
            closes=tuple(sorted(c.items())),
        )
    three_segment: dict[bool, tuple[ThreeSegmentResult, ...]] = {True: (), False: ()}
    for symbol in scored_symbols:
        both = three_segment_both(symbol, closes[symbol], base)
        for flag in (True, False):
            three_segment[flag] = (*three_segment[flag], both[flag])
    pairs = zip(three_segment[True], three_segment[False], strict=True)
    if any(a.completed != b.completed for a, b in pairs):
        notes.add("三环节结果依赖口径：d1 是否包含 T−20 会改变结果（SPEC 5.6 第1条）")

    spy = closes[scored_symbols[0]]
    spy_window_max = max(p2(spy[d]) for d in refs.window_days)

    hyg_lqd: float | None = None
    a, b = ratio_symbols
    if base in closes.get(a, {}) and base in closes.get(b, {}):
        hyg_lqd = ratio(closes[a][base], closes[b][base])
    else:
        notes.add(f"{a}/{b} 缺少基准日收盘价（仅作参考，不影响计分）")

    # ---- 4. 广度 ----
    breadth_now = breadth.get(base)
    breadth_t5 = breadth.get(refs.t_minus_5)
    if breadth_now is None:
        notes.add(f"缺少基准日 {base} 的 S5FI、S5TW 读数")

    # ---- 5. VIX（FRED → Cboe，SPEC 5.6 第7条）----
    vix, vix_notes = resolve_vix(base, vix_fred, vix_cboe)
    vix_t5, vix5_notes = resolve_vix(refs.t_minus_5, vix_fred, vix_cboe)
    for n in (*vix_notes, *vix5_notes):
        notes.add(n)

    # ---- 6. 利率（窗口只按股票交易日，SPEC 5.6 第5、6、9条）----
    included, excluded = mcal.rate_window_observations(treasury, refs.window_days)
    for d in excluded:
        notes.add(f"财政部 {d} 的数值 {treasury[d]} 位于股市休市日，不计入利率窗口")
    y = included.get(base)
    if y is None and raw.mode != "daily":
        notes.add(f"基准日 {base} 无财政部数值（债市休市），利率维度记待补")
    h: float | None = None
    h_dates: tuple[dt.date, ...] = ()
    if included:
        h, h_dates = window_max(included)
    y_t20 = treasury.get(refs.t_minus_20)
    if y_t20 is None:
        notes.add(f"T−20（{refs.t_minus_20}）无财政部数值，Δy 缺失，不用相邻日替代")
    yields = dict(included)
    if y_t20 is not None:
        yields[refs.t_minus_20] = y_t20

    # ---- 7. OAS ----
    oas_valued = _valued(oas)
    oas_o1 = oas_valued.get(refs.oas_o1)
    oas_o6_v3 = oas_valued.get(refs.oas_o6_v3r1)
    if oas_o1 is None:
        notes.add(f"FRED 在 O1（{refs.oas_o1}）没有 OAS 数值：v3-R1 信用记待补（数据滞后）")
    if oas_o6_v3 is None:
        notes.add(f"FRED 在 v3-R1 的 O6（{refs.oas_o6_v3r1}）没有 OAS 数值：信用维度可能记待补")
    oas_o1_v2 = None if refs.oas_o1_v2m is None else oas_valued.get(refs.oas_o1_v2m)
    oas_o6_v2 = None if refs.oas_o6_v2m is None else oas_valued.get(refs.oas_o6_v2m)

    # 债市休市日的 OAS 观测（SPEC 5.6 第10条）：沿用值排除；与前一观测不同的保留并报告
    span_start = min(d for d in (refs.oas_o6_v3r1, refs.oas_o6_v2m) if d is not None)
    for h_obs in mcal.bond_holiday_observations(oas, bond_cal):
        if not (span_start <= h_obs.date < base):
            continue
        if h_obs.carried_forward:
            notes.add(
                f"OAS {h_obs.date}（债市休市日）为沿用值 {h_obs.value}"
                f"（同 {h_obs.previous_date}），v2-M 不计为观测"
            )
        else:
            notes.add(
                f"【需人工判断】OAS {h_obs.date} 为债市休市日，但 FRED 数值 {h_obs.value} "
                f"与前一个观测（{h_obs.previous_date}：{h_obs.previous_value}）不同；"
                "未自动排除，v2-M 仍计为观测"
            )

    # 历史修订比对（SPEC 5.6 第3条）：ALFRED 版本日期不能代表真实发布时间
    vintage_values: list[OasVintageValue] = []
    if oas_vintage is not None:
        vintage_valued = _valued(oas_vintage)
        points = (
            ("v3-R1 O1", refs.oas_o1),
            ("v3-R1 O6", refs.oas_o6_v3r1),
            ("v2-M O1", refs.oas_o1_v2m),
            ("v2-M O6", refs.oas_o6_v2m),
        )
        for label, d in points:
            if d is None:
                continue
            item = OasVintageValue(label, d, vintage_valued.get(d), oas_valued.get(d))
            vintage_values.append(item)
            if item.revised:
                notes.add(
                    f"OAS 历史修订：{label}（{d}）基准日版本 {item.vintage_value}，"
                    f"当前版本 {item.current_value}；计分使用当前版本"
                )
        if base in vintage_valued:
            notes.add(
                "ALFRED 基准日版本已包含基准日当天的观测：该系列的版本日期不能代表真实发布时间，"
                "发布时点按\"次日发布\"假设处理（基准日当天观测不使用）"
            )
    else:
        notes.add("未取得 OAS 基准日版本（ALFRED），未做历史修订比对；不影响计分")

    snapshot = MarketSnapshot(
        refs=refs,
        etfs=etfs,
        breadth=breadth_now,
        breadth_t5=breadth_t5,
        vix=vix,
        vix_t5=vix_t5,
        yields=yields,
        y=y,
        h=h,
        h_date=h_dates[0] if h_dates else None,
        h_dates=h_dates,
        y_t20=y_t20,
        oas_o1=oas_o1,
        oas_o6_v3r1=oas_o6_v3,
        oas_o1_v2m=oas_o1_v2,
        oas_o6_v2m=oas_o6_v2,
        oas_vintage=tuple(vintage_values),
        hyg_lqd=hyg_lqd,
        spy_window_max_close=spy_window_max,
        three_segment=three_segment,
        data_notes=tuple(notes.items),
        mode=raw.mode,
    )
    assert_no_lookahead(snapshot)
    return snapshot


# 日期参照中本来就指向基准日之后的字段（只计算日期，不含行情）
_FUTURE_REF_FIELDS = {"outcome_window_start", "outcome_window_end"}


def assert_no_lookahead(snapshot: MarketSnapshot) -> None:
    """断言快照中任何参与计算的日期都不晚于基准日。"""
    base = snapshot.refs.base_date
    found: list[str] = []

    def check(label: str, d: dt.date | None) -> None:
        if d is not None and d > base:
            found.append(f"{label}={d}")

    for name, value in vars(snapshot.refs).items():
        if name in _FUTURE_REF_FIELDS:
            continue
        if isinstance(value, dt.date):
            check(f"refs.{name}", value)
        elif isinstance(value, tuple):
            for d in value:
                check(f"refs.{name}", d)
    for symbol, etf in snapshot.etfs.items():
        for d, _ in etf.closes:
            check(f"{symbol}.closes", d)
    for d in snapshot.yields:
        check("yields", d)
    for d in snapshot.h_dates:
        check("h_dates", d)
    for r in (snapshot.breadth, snapshot.breadth_t5):
        if r is not None:
            check("breadth", r.date)
    for results in snapshot.three_segment.values():
        for res in results:
            for t in res.traces:
                check(f"{res.symbol}.three_segment.d1", t.d1)
                check(f"{res.symbol}.three_segment.lc_date", t.lc_date)
                for d in t.d2_dates:
                    check(f"{res.symbol}.three_segment.d2", d)
    if found:
        raise LookaheadError(f"快照包含基准日 {base} 之后的数据：{found[:10]}")


__all__ = [
    "DataIntegrityError",
    "InsufficientDataError",
    "LookaheadError",
    "RawInputs",
    "assert_no_lookahead",
    "build_snapshot",
    "check_contiguous",
    "truncate",
]
