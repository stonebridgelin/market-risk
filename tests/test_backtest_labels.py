"""回测标签：区间归属、跨越边界、保留期屏蔽、回调窗口四列（补充1）、结果标签屏蔽。构造数据，不读真实目录。"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal as D

from market_risk import calendar as mcal
from market_risk.backtest.labels import (
    HOLDOUT_NOTE,
    MASKED,
    build_episodes,
    episode_windows,
    outcome_rows,
    window_measures,
)
from market_risk.backtest.output import outcome_row
from market_risk.backtest.report import render_baseline
from market_risk.backtest.settings import load_backtest_config

CFG = load_backtest_config()
SPX_ONLY_5 = dataclasses.replace(CFG, levels={"SPX": (D("0.05"),), "QQQ": (D("0.07"),)}, window_sessions=2)


def path(start: dt.date, values: list[str]) -> list[tuple[dt.date, D]]:
    days = mcal.stock_trading_days(start, start + dt.timedelta(days=len(values) * 3))[:len(values)]
    return list(zip(days, (D(v) for v in values), strict=True))


def test_episode_in_development_is_counted():
    closes = path(dt.date(2012, 3, 1), ["100", "94", "99", "100"])
    (ep,) = build_episodes("SPX", closes, SPX_ONLY_5)
    assert (ep.period, ep.status, ep.counted, ep.crosses_boundary) == ("开发期", "已确认", True, False)
    assert ep.drawdown_pct == D("-6.0000") and ep.grade == "小回调" and ep.trading_days == 1


def test_crossing_development_to_validation_not_counted():
    """高点在开发期、低点确认在验证期：全部字段照常写出，标注跨越区间边界，不计入任一区间统计。"""
    closes = path(dt.date(2016, 12, 27), ["100", "94", "93", "92", "91", "99"])
    (ep,) = build_episodes("SPX", closes, SPX_ONLY_5)
    assert ep.period == "开发期" and ep.crosses_boundary and not ep.counted
    assert ep.low_close == D("91") and ep.drawdown_pct is not None


def test_confirmation_in_holdout_is_masked():
    """低点在验证期但确认发生在保留期：只写高点，状态"跨入保留期，未解锁"。"""
    closes = path(dt.date(2022, 12, 23), ["100", "94", "93", "92", "93", "97", "99"])
    (ep,) = build_episodes("SPX", closes, SPX_ONLY_5)
    assert ep.status == MASKED and ep.low_date is None and ep.drawdown_pct is None and ep.grade == ""
    assert ep.period == "验证期" and not ep.counted
    (unlocked,) = build_episodes("SPX", closes, SPX_ONLY_5, unlock=True)
    assert unlocked.status == "已确认" and unlocked.low_close == D("92")


def test_high_in_holdout_not_written_and_recovery_masked():
    closes = path(dt.date(2023, 2, 1), ["100", "94", "99", "101"])
    assert build_episodes("SPX", closes, SPX_ONLY_5) == []
    early = path(dt.date(2022, 10, 3), ["100", "94", "99"]) + path(dt.date(2023, 1, 3), ["101"])
    (ep,) = build_episodes("SPX", early, SPX_ONLY_5)
    assert ep.status == "已确认" and ep.recovery_date is None and ep.recovery_note == HOLDOUT_NOTE


def test_before_start_episode_flagged():
    closes = path(dt.date(2008, 7, 1), ["100", "94", "99"])
    (ep,) = build_episodes("SPX", closes, SPX_ONLY_5)
    assert ep.before_start and ep.period == "起点之前" and not ep.counted


def test_window_measures_at_high_trough_and_after():
    """补充1：高点当天比例 0、低点当天 1、低点后随反弹减小；低点之前涨幅为空。"""
    high, low = D("100"), D("90")
    assert window_measures(D("100"), high, low, -3) == (D("0.000000"), D("0.000000"), None)
    assert window_measures(D("95"), high, low, -1) == (D("-5.000000"), D("0.500000"), None)
    assert window_measures(D("90"), high, low, 0) == (D("-10.000000"), D("1.000000"), D("0.000000"))
    assert window_measures(D("94.5"), high, low, 2) == (D("-5.500000"), D("0.550000"), D("5.000000"))
    assert window_measures(D("91"), high, low, 1)[1] == D("0.900000")
    # 6位小数，ROUND_HALF_UP
    assert window_measures(D("93.33"), D("100"), D("91"), -1)[1] == D("0.741111")


def test_episode_windows_rows_and_blank_columns():
    closes = path(dt.date(2012, 3, 1), ["100", "101", "96", "94", "99", "100", "100"])
    days = [d for d, _ in closes]
    scores = {(d, v): (1, 1, 1, "早期信号") for d in days for v in ("v2-M", "v3-R1")}
    (ep,) = build_episodes("SPX", closes, SPX_ONLY_5)
    rows = episode_windows(ep, days, scores, ("v2-M", "v3-R1"), SPX_ONLY_5, closes=dict(closes))
    by = {r.date: r for r in rows}
    hi, lo = days[1], days[3]
    assert by[hi].offset == 0 and by[hi].decline_progress == D("0") and by[hi].offset_from_trough == -2
    assert by[lo].offset_from_trough == 0 and by[lo].decline_progress == D("1") and by[lo].rebound_from_trough == 0
    assert by[days[0]].rebound_from_trough is None and by[days[0]].offset == -1
    assert max(by) == days[5]                                     # 低点后 2 个交易日
    # 未确认：四列留空
    tail = path(dt.date(2012, 3, 1), ["100", "101", "96", "94"])
    (open_ep,) = build_episodes("SPX", tail, SPX_ONLY_5)
    rows = episode_windows(open_ep, [d for d, _ in tail], scores, ("v2-M",), SPX_ONLY_5, closes=dict(tail))
    assert rows and all(r.offset_from_trough is None and r.drawdown_from_peak is None and r.decline_progress is None
                        and r.rebound_from_trough is None for r in rows)


def test_episode_windows_progress_outside_high_to_trough():
    """高点之前低于本段低点、低点之后超过高点时，输出未截断的进度。"""
    closes = path(dt.date(2012, 3, 1), ["90", "101", "96", "94", "99", "102"])
    days = [d for d, _ in closes]
    scores = {(d, "v2-M"): (1, 1, 1, "早期信号") for d in days}
    (ep,) = build_episodes("SPX", closes, SPX_ONLY_5)
    rows = episode_windows(ep, days, scores, ("v2-M",), SPX_ONLY_5, closes=dict(closes))
    by = {r.date: r for r in rows}
    assert by[days[0]].decline_progress == D("1.571429")  # (101−90)÷(101−94)
    assert by[days[-1]].decline_progress == D("-0.142857")  # (101−102)÷(101−94)


def test_masked_episode_windows_stop_at_validation_end():
    closes = path(dt.date(2022, 12, 23), ["100", "94", "93", "92", "93", "97", "99"])
    days = [d for d, _ in closes]
    scores = {(d, "v2-M"): (1, 1, 1, "早期信号") for d in days}
    (ep,) = build_episodes("SPX", closes, SPX_ONLY_5)
    rows = episode_windows(ep, days, scores, ("v2-M",), SPX_ONLY_5, closes=dict(closes))
    assert rows and max(r.date for r in rows) <= dt.date(2022, 12, 30)
    assert all(r.decline_progress is None for r in rows)


def test_outcome_rows_mask_holdout_windows():
    days = mcal.stock_trading_days(dt.date(2022, 11, 1), dt.date(2023, 2, 28))
    spx = {d: D("100") for d in days}
    qqq = {d: D("100") for d in days}
    bases = [d for d in days if d <= dt.date(2022, 12, 30)]
    rows = outcome_rows(bases, spx, qqq, CFG)
    assert rows and max(mcal.shift_trading_days(r.label.base_date, 20) for r in rows) < dt.date(2023, 1, 1)
    assert all(r.period == "验证期" for r in rows)
    unlocked = outcome_rows(bases, spx, qqq, CFG, unlock=True)
    assert len(unlocked) > len(rows)


def test_label_impact_detects_changes_and_ignores_ndx():
    from market_risk.backtest.labels import label_impact

    days = mcal.stock_trading_days(dt.date(2012, 3, 1), dt.date(2012, 5, 31))
    spx = {d: D("100") for d in days}
    qqq = {d: D("100") for d in days}
    day = days[25]
    same = label_impact("SPX", day, D("100.03"), spx, qqq, SPX_ONLY_5)
    assert not same.material and same.outcome_bases == 21
    crash = label_impact("SPX", day, D("90"), spx, qqq, SPX_ONLY_5)
    assert crash.material and crash.outcome_diffs and crash.episode_diffs
    assert not label_impact("NDX", day, D("1"), spx, qqq, SPX_ONLY_5).material


def test_only_one_series_updated_means_window_not_finished() -> None:
    """只更新了一个序列：SPX 已有窗口最后一个交易日，QQQ 只到前一天 → 窗口未结束，不生成该行（不是"数据不齐"）。"""
    days = mcal.stock_trading_days(dt.date(2012, 3, 1), dt.date(2012, 5, 31))
    base = days[0]
    end = days[20]
    spx = {d: D("100") for d in days if d <= end}
    qqq = {d: D("100") for d in days if d < end}
    assert outcome_rows([base], spx, qqq, CFG) == []
    assert outcome_rows([base], qqq, spx, CFG) == []             # 反过来（QQQ 更新、SPX 未更新）同样
    (row,) = outcome_rows([base], spx, {**qqq, end: D("100")}, CFG)   # 两者都到窗口末日 → 生成标签
    assert row.label is not None and row.data_note == ""


def test_label_impact_both_sources_incomplete_is_not_a_difference() -> None:
    """两种来源都因窗口内缺价无法生成标签：不算差异，单独列出。"""
    from market_risk.backtest.labels import label_impact

    days = mcal.stock_trading_days(dt.date(2012, 3, 1), dt.date(2012, 5, 31))
    day = days[25]
    spx = {d: D("100") for d in days}
    qqq = {d: D("100") for d in days if d != days[22]}          # QQQ 窗口内缺一天：两种 SPX 来源都算不出标签
    imp = label_impact("SPX", day, D("100.03"), spx, qqq, SPX_ONLY_5)
    assert not imp.outcome_diffs and not imp.material
    # 检验的基准日为 days[5] 至 days[25]；其中 days[5] 至 days[21] 的结果窗口包含 days[22]，days[22] 是基准日本身，
    # 都缺价；days[23] 至 days[25] 的窗口在 days[22] 之后，能生成标签（两种来源相同，不算差异）
    assert imp.incomplete_bases == tuple(days[5:23])


def test_incomplete_outcome_window_keeps_blank_row() -> None:
    """H-09：结果窗口已经结束，SPX 缺一天时保留一行，标签全部留空。"""
    days = mcal.stock_trading_days(dt.date(2012, 3, 1), dt.date(2012, 5, 31))
    base = days[0]
    missing = days[8]
    spx = {d: D("100") for d in days if d != missing}
    qqq = {d: D("100") for d in days}
    (row,) = outcome_rows([base], spx, qqq, CFG)
    serialized = outcome_row(row)
    assert row.label is None and str(missing) in row.data_note
    assert row.data_note.startswith("数据不齐：")
    assert serialized["base_date"] == base and serialized["window_end"] == days[20]
    assert all(serialized[k] is None for k in ("spx_drawdown_from_base", "qqq_drawdown_from_base",
                                                  "spx_peak_to_trough_drawdown", "qqq_peak_to_trough_drawdown",
                                                  "is_event", "is_near_event", "event_date"))
    meta = {"run_id": "test", "start": base, "end": days[-1], "days": len(days), "runtime_seconds": 0,
            "git_commit": "a" * 40, "market_manifest_sha256": "b" * 64}
    text = render_baseline(meta, CFG, [], [], [{**serialized, "base_date": str(base)}])
    assert "结果窗口数据不齐" in text and str(base) in text and str(missing) in text
