"""公布精度和均线等号边界：不经二进制浮点数判定。"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

from conftest import synthetic_raw

from market_risk.backtest.engine import run_backtest
from market_risk.backtest.settings import load_backtest_config
from market_risk.config import PROJECT_ROOT, load_settings
from market_risk.data.market import load_market_series, read_series_file
from market_risk.data.snapshot import build_snapshot
from market_risk.indicators import simple_moving_average
from market_risk.near_threshold import _add
from market_risk.precision import published_price
from market_risk.scoring import v2m, v3r1
from market_risk.storage.paths import StoragePaths


def test_rsp_ma20_equal_on_2024_04_05() -> None:
    """独立复核确认的原有浮点误判：RSP 166.39 恰等于 MA20 166.39。"""
    paths = StoragePaths(PROJECT_ROOT)
    settings = load_settings()
    series = load_market_series(paths, settings)
    day = dt.date(2024, 4, 5)
    (result,) = run_backtest(series, paths, settings, load_backtest_config(), start=day, end=day).days
    assert result.metrics["RSP.close"] == Decimal("166.39")
    assert result.metrics["RSP.ma20"] == Decimal("166.39")
    assert {(version, score.price.score, score.total) for version, score in result.results.items()} == {
        ("v2-M", 0, 5), ("v3-R1", 0, 5)}


def test_decimal_moving_average_equal_to_close() -> None:
    base = dt.date(2024, 4, 5)
    days = [base - dt.timedelta(days=i) for i in range(20)]
    values = {day: Decimal("166.39") for day in days}
    assert simple_moving_average(values, base, 20) == Decimal("166.39")


def test_market_price_reads_csv_decimal_without_float(tmp_path) -> None:
    path = tmp_path / "SPY.csv"
    path.write_text("date,value,source\n2024-04-05,166.3850,yahoo\n", encoding="utf-8")
    rows = read_series_file(path, exact=True)[1]
    assert rows[dt.date(2024, 4, 5)]["value"] == Decimal("166.3850")
    assert published_price(rows[dt.date(2024, 4, 5)]["value"]) == Decimal("166.39")


def test_near_threshold_has_no_float_epsilon() -> None:
    items = []
    _add(items, "测试", Decimal("1.0000000001"), Decimal(0), Decimal("1.0000000001"), "点", 1.0)
    assert not items


def test_price_equal_boundaries() -> None:
    """MA20、MA50、MA200 的收盘等值和 MA5=MA50 均走严格小于的另一侧。"""
    base = dt.date(2025, 11, 28)
    original = build_snapshot(synthetic_raw(base))
    price = Decimal("100.00")
    etfs = {sym: dataclasses.replace(e, close=price, ma5=price, ma20=price, ma50=price, ma200=price)
            for sym, e in original.etfs.items()}
    snap = dataclasses.replace(original, etfs=etfs)
    assert v2m.score_price(snap, True).score == 0
    assert v3r1.score_price(snap).score == 0

    # 两只已低于 MA50，但 MA5 恰等于 MA50：v2-M 为2，v3-R1 不满足持续性条件。
    changed = {**etfs, **{sym: dataclasses.replace(etfs[sym], close=Decimal("99.99"))
                         for sym in ("QQQ", "RSP")}}
    snap = dataclasses.replace(snap, etfs=changed)
    assert v2m.score_price(snap, True).score == 2
    assert v3r1.score_price(snap).score == 1
