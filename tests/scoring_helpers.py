"""评分测试的辅助函数：以样本4（五项均为0分）的快照为底，按需替换字段。"""

from __future__ import annotations

import dataclasses
import datetime as dt
from functools import cache

from conftest import load_sample_raw

from market_risk.data.snapshot import build_snapshot
from market_risk.models import (
    BreadthReading,
    EtfSnapshot,
    MarketSnapshot,
    OasVintageValue,
    ThreeSegmentResult,
    ThreeSegmentTrace,
)

BASE = dt.date(2025, 11, 28)
T5 = dt.date(2025, 11, 20)


@cache
def base_snapshot() -> MarketSnapshot:
    raw = load_sample_raw("2025-11-28", {BASE: BreadthReading(BASE, 58.44, 76.73)})
    return build_snapshot(dataclasses.replace(raw, oas_vintage=None))


def etf(symbol: str, close: float, ma5: float, ma20: float, ma50: float, ma200: float = 1.0):
    return EtfSnapshot(symbol, close, ma5, 0.0, ma20, 0.0, ma50, ma200, ())


# 默认：三只都在 MA20、MA50 之上，MA5 在 MA50 之上
DEFAULT_ETFS = {
    "SPY": etf("SPY", 100, 100, 99, 98, 90),
    "QQQ": etf("QQQ", 100, 100, 99, 98, 90),
    "RSP": etf("RSP", 100, 100, 99, 98, 90),
}


def completed_three_segment(symbol: str) -> ThreeSegmentResult:
    d1 = dt.date(2025, 11, 20)
    trace = ThreeSegmentTrace(symbol, d1, 95.0, 100.0, dt.date(2025, 10, 30), True,
                              (dt.date(2025, 11, 24),), True, True)
    return ThreeSegmentResult(symbol, True, (trace,))


def no_three_segment() -> dict[bool, tuple[ThreeSegmentResult, ...]]:
    empty = tuple(ThreeSegmentResult(s, True, ()) for s in ("SPY", "QQQ", "RSP"))
    return {True: empty, False: empty}


def make(
    etfs: dict[str, EtfSnapshot] | None = None,
    three_segment: dict[bool, tuple[ThreeSegmentResult, ...]] | None = None,
    spy_window_max: float | None = None,
    f: float | None = 58.44,
    w: float | None = 76.73,
    f5: float | None = None,
    w5: float | None = None,
    v: float | None = 16.35,
    v5: float | None = 26.42,
    y: float | None = 4.02,
    h_other: float = 4.17,
    y20: float | None = 4.11,
    o1: float | None = 3.00,
    o6: float | None = 3.17,
    lag: int = 1,
    vintage: tuple[OasVintageValue, ...] = (),
) -> MarketSnapshot:
    """构造评分用快照。v2-M 与 v3-R1 的 O1/O6 取相同数值。"""
    snap = base_snapshot()
    refs = dataclasses.replace(snap.refs, o1_v2m_lag_stock_days=lag)
    yields = {refs.window_days[0]: h_other}
    if y is not None:
        yields[BASE] = y
    if y20 is not None:
        yields[refs.t_minus_20] = y20
    etf_map = etfs or DEFAULT_ETFS
    return dataclasses.replace(
        snap,
        refs=refs,
        etfs=etf_map,
        three_segment=three_segment or no_three_segment(),
        spy_window_max_close=spy_window_max if spy_window_max is not None else 1000.0,
        breadth=None if f is None or w is None else BreadthReading(BASE, f, w),
        breadth_t5=None if f5 is None or w5 is None else BreadthReading(T5, f5, w5),
        vix=v,
        vix_t5=v5,
        yields=yields,
        y=y,
        h=max(h_other, y) if y is not None else h_other,
        y_t20=y20,
        oas_o1=o1,
        oas_o6_v3r1=o6,
        oas_o1_v2m=o1,
        oas_o6_v2m=o6,
        oas_vintage=vintage,
    )
