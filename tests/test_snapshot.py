"""快照构建测试：离线样本数据与合成数据。

样本期望值来自 SPEC 第9节（截图读数）；阶段4另有完整的回归测试。
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import load_sample_raw, synthetic_raw

from market_risk.config import load_holidays
from market_risk.data.raw_io import load_raw_inputs, save_raw_inputs
from market_risk.data.snapshot import DataIntegrityError, build_snapshot
from market_risk.models import BreadthReading

D = dt.date


def test_sample4_snapshot_values():
    base = D(2025, 11, 28)
    reading = BreadthReading(base, 58.44, 76.73)
    snap = build_snapshot(load_sample_raw("2025-11-28", {base: reading}), holidays=load_holidays())
    spy = snap.etfs["SPY"]
    assert spy.close == 683.39
    for got, want in ((spy.ma5, 673.17), (spy.ma20, 672.90), (spy.ma50, 670.44),
                      (spy.ma200, 616.06)):
        assert abs(got - want) <= 0.02
    assert (snap.vix, snap.vix_t5) == (16.35, 26.42)
    assert (snap.y, snap.h, snap.h_date, snap.y_t20) == (4.02, 4.17, D(2025, 11, 5), 4.11)
    assert snap.h_dates == (D(2025, 11, 5),)
    assert (snap.oas_o1, snap.oas_o6_v3r1) == (3.00, 3.17)
    assert snap.breadth == reading and snap.breadth_t5 is None
    assert snap.refs.is_early_close
    # 利率窗口：10-31 至 11-28 的股票交易日中，11-11 债市休市，共19个观测，另含 T−20
    assert len(snap.yields) == 20 and D(2025, 11, 11) not in snap.yields
    assert snap.yields[D(2025, 10, 30)] == 4.11
    # 三环节第一步成立的候选（SPEC 样本4）
    step1 = {r.symbol: [t.d1 for t in r.traces if t.step1] for r in snap.three_segment[True]}
    assert step1 == {
        "SPY": [D(2025, 11, 17), D(2025, 11, 18), D(2025, 11, 20)],
        "QQQ": [D(2025, 11, 17), D(2025, 11, 18), D(2025, 11, 20)],
        "RSP": [D(2025, 11, 17), D(2025, 11, 19), D(2025, 11, 20)],
    }
    assert not any(r.completed for r in snap.three_segment[True])
    assert snap.hyg_lqd is not None


def test_sample3_three_segment_and_lc():
    snap = build_snapshot(load_sample_raw("2025-10-31"))
    traces = {r.symbol: [t for t in r.traces if t.step1] for r in snap.three_segment[True]}
    (spy,) = traces["SPY"]
    assert (spy.d1, spy.d1_close, spy.lc, spy.lc_date) == (
        D(2025, 10, 10), 653.02, 657.41, D(2025, 9, 12)
    )
    (rsp,) = traces["RSP"]
    assert (rsp.d1, rsp.d1_close, rsp.lc, rsp.lc_date) == (
        D(2025, 10, 10), 185.12, 186.72, D(2025, 9, 25)
    )
    assert traces["QQQ"] == []
    # 10-13 债市休市：利率窗口19个观测
    assert D(2025, 10, 13) in snap.refs.bond_holidays_in_window
    assert (snap.y, snap.h, snap.h_date, snap.y_t20) == (4.11, 4.18, D(2025, 10, 6), 4.13)


def test_raw_inputs_roundtrip(tmp_path):
    raw = load_sample_raw("2025-09-26")
    save_raw_inputs(raw, tmp_path / "copy")
    again = load_raw_inputs(tmp_path / "copy")
    assert again == raw


# ---- 合成数据 ----


def test_missing_trading_day_raises():
    base = D(2025, 11, 28)
    raw = synthetic_raw(base)
    spy = dict(raw.closes["SPY"])
    del spy[D(2025, 11, 20)]
    with pytest.raises(DataIntegrityError, match="缺"):
        build_snapshot(dataclasses.replace(raw, closes={**raw.closes, "SPY": spy}))
    no_base = {d: v for d, v in raw.closes["QQQ"].items() if d != base}
    with pytest.raises(DataIntegrityError, match="没有基准日"):
        build_snapshot(dataclasses.replace(raw, closes={**raw.closes, "QQQ": no_base}))
    without_rsp = {k: v for k, v in raw.closes.items() if k != "RSP"}
    with pytest.raises(DataIntegrityError, match="缺少 RSP"):
        build_snapshot(dataclasses.replace(raw, closes=without_rsp))


def test_base_date_bond_holiday_leaves_y_missing():
    """基准日债市休市、股市开市（2025-10-13）：y 缺失，记待补说明（SPEC 5.6 第5条）。"""
    base = D(2025, 10, 13)
    raw = synthetic_raw(base, treasury_overrides={base: None}, oas_overrides={base: None})
    snap = build_snapshot(raw)
    assert snap.y is None
    assert snap.h == 4.00  # 其余19个观测
    assert any("债市休市" in n for n in snap.data_notes)
    assert base in snap.refs.bond_holidays_in_window


def test_t_minus_20_bond_holiday_leaves_y_t20_missing():
    base = D(2025, 11, 10)  # T−20 = 2025-10-13（哥伦布日）
    raw = synthetic_raw(base, treasury_overrides={D(2025, 10, 13): None})
    snap = build_snapshot(raw)
    assert snap.refs.t_minus_20 == D(2025, 10, 13)
    assert snap.y_t20 is None
    assert any("T−20" in n and "不用相邻日替代" in n for n in snap.data_notes)


def test_daily_mode_unpublished_treasury():
    base = D(2025, 11, 26)
    raw = synthetic_raw(base, treasury_overrides={base: None}, mode="daily")
    snap = build_snapshot(raw)
    assert snap.y is None
    assert base not in snap.refs.bond_holidays_in_window
    assert any("尚未发布" in n for n in snap.data_notes)


def test_rate_window_excludes_good_friday_2026():
    """SPEC 5.6 第9条：基准日 2026-04-24，04-03 的财政部数值不计入利率窗口，并注明。"""
    base = D(2026, 4, 24)
    raw = synthetic_raw(base, treasury_overrides={D(2026, 4, 3): 9.99})
    snap = build_snapshot(raw)
    assert (snap.refs.window_start, snap.refs.window_end) == (D(2026, 3, 27), base)
    assert D(2026, 4, 3) not in snap.yields
    assert snap.h == 4.00
    assert any("2026-04-03" in n and "不计入利率窗口" in n for n in snap.data_notes)


def test_v3r1_o6_on_good_friday_without_oas():
    """基准日 2026-04-13：v3-R1 的 O6 为 04-03；FRED 在该日无 OAS 数值时报告。"""
    base = D(2026, 4, 13)
    raw = synthetic_raw(base, oas_overrides={D(2026, 4, 3): None})
    snap = build_snapshot(raw)
    assert snap.refs.oas_o6_v3r1 == D(2026, 4, 3)
    assert snap.oas_o6_v3r1 is None
    assert any("O6（2026-04-03）没有 OAS 数值" in n for n in snap.data_notes)
    assert snap.refs.oas_o6_v2m == D(2026, 4, 2)
    assert snap.oas_o6_v2m == 3.00


def test_missing_o1_oas_value_noted():
    base = D(2025, 11, 26)
    raw = synthetic_raw(base, oas_overrides={D(2025, 11, 25): None})
    snap = build_snapshot(raw)
    assert snap.oas_o1 is None
    assert any("数据滞后" in n for n in snap.data_notes)
    assert snap.oas_o1_v2m == 3.00 and snap.refs.oas_o1_v2m == D(2025, 11, 24)


def test_alfred_vintage_checks():
    base = D(2025, 11, 26)
    raw = synthetic_raw(base)
    vintage_missing_o1 = {D(2025, 11, 24): 3.0}
    snap = build_snapshot(dataclasses.replace(raw, oas_vintage=vintage_missing_o1))
    assert snap.oas_o1_published is False
    assert any("不在基准日" in n for n in snap.data_notes)

    vintage = {D(2025, 11, 18): 3.10, D(2025, 11, 25): 3.00, base: 2.9}
    snap = build_snapshot(dataclasses.replace(raw, oas_vintage=vintage))
    assert snap.oas_o1_published is True
    assert any("不反映" in n for n in snap.data_notes)
    assert any("已被修订" in n for n in snap.data_notes)  # O6=11-18：3.10 → 3.00

    snap = build_snapshot(raw)
    assert snap.oas_o1_published is None
    assert any("未进行 ALFRED" in n for n in snap.data_notes)


def test_three_segment_dependency_note():
    base = D(2025, 11, 28)
    days = sorted(synthetic_raw(base).closes["SPY"])
    overrides = {days[-21]: 95.0, days[-11]: 98.0, base: 90.0}  # 只有 d1=T−20 能完成
    snap = build_snapshot(synthetic_raw(base, close_overrides={"SPY": overrides}))
    spy_with = snap.three_segment[True][0]
    spy_without = snap.three_segment[False][0]
    assert spy_with.completed and not spy_without.completed
    assert any("三环节结果依赖口径" in n for n in snap.data_notes)


def test_missing_ratio_symbol_and_breadth_noted():
    base = D(2025, 11, 28)
    raw = synthetic_raw(base)
    closes = {k: v for k, v in raw.closes.items() if k != "LQD"}
    snap = build_snapshot(dataclasses.replace(raw, closes=closes))
    assert snap.hyg_lqd is None
    assert any("HYG/LQD" in n for n in snap.data_notes)
    assert any("S5FI" in n for n in snap.data_notes)
