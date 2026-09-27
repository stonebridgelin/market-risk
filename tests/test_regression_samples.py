"""回归测试（SPEC 第9节阶段4）：4个历史样本的程序值与截图读数。

容差：收盘价与均线 ≤ 0.02；财政部、OAS、VIX 完全一致；分数完全一致。
期望值来自人工核对，不得为通过测试而修改；差异应报告给用户。
"""

from __future__ import annotations

import pytest

from market_risk.config import load_settings
from market_risk.data.snapshot import build_snapshot
from market_risk.pipeline import score_snapshot
from market_risk.validation import compare_sample, load_expected, sample_raw, validate_all

SPEC = load_expected()
SETTINGS = load_settings()


@pytest.mark.parametrize("sample", list(SPEC["samples"]))
def test_sample_matches_screenshot(sample):
    exp = SPEC["samples"][sample]
    snap = build_snapshot(sample_raw(sample, exp), SETTINGS.scored_symbols)
    results, _ = score_snapshot(snap, SETTINGS)
    checks = compare_sample(sample, exp, snap, results, SPEC["tolerance_price"])
    bad = [(c.item, c.screenshot, c.program, c.diff) for c in checks if not c.ok]
    assert bad == []
    assert len(checks) >= 25


def test_validate_all_counts():
    checks = validate_all(SETTINGS)
    assert {c.sample for c in checks} == set(SPEC["samples"])
    assert all(c.ok for c in checks)


def test_compare_reports_differences():
    """差异会被列出（而不是被掩盖）：人为改动期望值，确认比对表标为不一致。"""
    import copy

    exp = copy.deepcopy(SPEC["samples"]["2025-11-28"])
    exp["etfs"]["SPY"][3] = 670.50   # MA50 截图值改大 0.06，超过容差
    exp["vix"] = 16.36
    exp["scores"]["v2-M"] = [0, 0, 0, 0, 1]
    snap = build_snapshot(sample_raw("2025-11-28", exp), SETTINGS.scored_symbols)
    results, _ = score_snapshot(snap, SETTINGS)
    bad = {c.item for c in compare_sample("2025-11-28", exp, snap, results, 0.02) if not c.ok}
    assert bad == {"SPY MA50", "VIX", "v2-M 五项分数"}
