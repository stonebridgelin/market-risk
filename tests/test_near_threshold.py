"""贴近门槛的读数（SPEC 第7节）。"""

from __future__ import annotations

from scoring_helpers import make
from test_scoring_v2m import sample_snapshot

from market_risk.config import load_settings
from market_risk.near_threshold import near_threshold_items

CFG = load_settings().near_threshold


def _items(snap):
    return {i.item: [(t.threshold, round(t.gap, 2)) for t in near_threshold_items(snap, CFG)
                     if t.item == i.item] for i in near_threshold_items(snap, CFG)}


def test_sample2_near_threshold():
    """样本2：OAS 恰好 +5bp、S5TW 51.09%、RSP 仅高于 MA20 0.24 美元（SOP 10.1 备注）。"""
    items = _items(sample_snapshot("2025-09-26"))
    assert items["ΔOAS（v2-M）"] == [(5.0, 0.0)]
    assert items["ΔOAS（v3-R1）"] == [(5.0, 0.0)]
    assert items["S5TW（W）"] == [(50.0, 1.09)]
    assert items["RSP 收盘价 vs MA20"] == [(188.3315, 0.13)]  # 均线为精确值，不取整


def test_sample3_near_threshold():
    items = _items(sample_snapshot("2025-10-31"))
    assert items["S5FI（F）"] == [(40.0, 0.15)]
    assert items["L=min(F,W)"] == [(40.0, -1.44)]
    assert items["VIX"] == [(18.0, -0.56)]
    assert "RSP MA5 vs MA50" in items


def test_yield_and_g_near_threshold():
    items = _items(make(y=4.30, h_other=4.31, v=21.0, v5=16.5))
    assert items["y vs H"] == [(4.31, -1.0)]
    assert items["VIX 变化率 g"] == [(30.0, -2.73)]
    assert items["VIX"] == [(20.0, 1.0)]
    # y 与 H 相差 3bp：不列出
    assert "y vs H" not in _items(make(y=4.28, h_other=4.31))


def test_missing_values_are_skipped():
    items = _items(make(f=None, w=None, v=None, o1=None, y=None))
    assert not any(k.startswith(("S5", "L=", "VIX", "ΔOAS", "y vs")) for k in items)
