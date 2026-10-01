"""ZZ 分侧前瞻研究：构造数据的期望值均在注释中手工推算。"""

from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.research.features import FeatureEngine, high_position_days
from market_risk.research.groups import classify_candidates
from market_risk.research.io import ResearchInputs
from market_risk.research.zz_v121 import _decision, _read_episodes, _read_unknown


def _engine() -> tuple[FeatureEngine, tuple[dt.date, ...]]:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(85))
    market = {"SPX": {day: Decimal(100) for day in days},
              "QQQ": {day: Decimal(100) for day in days}, "UST10Y": {}}
    # 第60天 QQQ=98，低于此前最高100的99%边界99；SPX同日仍为100。
    market["QQQ"][days[60]] = Decimal(98)
    inputs = ResearchInputs("synthetic", {}, {}, (), (), (), 0, market, {}, days)
    return FeatureEngine(inputs, days, {day: i for i, day in enumerate(days)}, (), {}), days


def test_qqq_side_high_position_uses_qqq_not_spx_and_excludes_tail() -> None:
    engine, days = _engine()
    spx = high_position_days(engine, days, Decimal("0.99"), "SPX")
    qqq = high_position_days(engine, days, Decimal("0.99"), "QQQ")
    # 含当日60日窗口从第59天开始；第60天 QQQ=98<99，仅SPX满足。
    assert days[60] in spx and days[60] not in qqq
    grouped = classify_candidates(engine, (), days, Decimal("0.99"), "QQQ",
                                  frozenset({days[61]}))
    # 第61天 QQQ 恢复100，虽为高位日，但落在明示的期末未定集合，不得成为对照。
    assert next(row.category for row in grouped if row.date == days[61]) == "期末尾段危险归属未定"
    assert all(row.date != days[61] or row.category not in ("样本组", "对照组") for row in grouped)


def test_unknown_dates_from_both_assets_are_excluded_without_reading_later_values(tmp_path: Path) -> None:
    path = tmp_path / "zz_unknown_development.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("symbol", "date", "reason", "label_end"))
        writer.writerow(("SPX", "2016-12-29", "尾段（寻峰）", "2016-12-30"))
        writer.writerow(("QQQ", "2016-12-28", "右截尾（寻底）", "2016-12-30"))
    # 两资产任一未知的日期均进入系统级排除集合，共28、29两天。
    assert _read_unknown(path) == frozenset({dt.date(2016, 12, 28), dt.date(2016, 12, 29)})
    with path.open("a", encoding="utf-8", newline="") as file:
        csv.writer(file).writerow(("SPX", "2017-01-03", "越界", "2016-12-30"))
    with pytest.raises(ValueError, match="越过开发期"):
        _read_unknown(path)


def test_preregistered_b_dv_decision_requires_all_three_directions() -> None:
    row: dict[str, object] = {"sample_insufficient": False, "auc": 0.40,
                              "subperiod_direction_same": True,
                              "leave_one_out_direction_same": True,
                              "top_group_direction_same": True}
    # AUC 0.40<0.50 且三项一致为保留；任一一致性失败即移除。
    assert _decision(row) == "保留"
    row["top_group_direction_same"] = False
    assert _decision(row) == "移除"


def test_qqq_side_feature_is_unchanged_by_future_extreme_value() -> None:
    engine, days = _engine()
    current = days[60]
    series = {day: Decimal(index) for index, day in enumerate(days)}
    inputs = engine.inputs
    inputs.tradingview["NDTW"] = series
    # 第60天−第40天=60−40=20；把第70天改到极端值不应改变第60天特征。
    before = engine.change("NDTW", current, 20)
    series[days[70]] = Decimal("-999999")
    assert before == engine.change("NDTW", current, 20) == Decimal(20)


def test_zz_event_reader_rejects_post_development_event(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("symbol", "peak_date", "trough_date", "peak_close"))
        writer.writerow(("SPX", "2016-12-20", "2017-01-03", "100"))
    with pytest.raises(ValueError, match="越过开发期"):
        _read_episodes(path)
