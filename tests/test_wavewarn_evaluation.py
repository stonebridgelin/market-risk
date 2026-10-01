"""开发期编排的数值与边界测试；期望均从下列构造数据手算。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn.config import WavewarnConfig, load_wavewarn_config
from market_risk.wavewarn.diagnostics import DiagnosticRow
from market_risk.wavewarn.evaluation import (
    Candidate,
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    allocated_asset_days,
    candidate_grid,
    evaluate_candidate,
    event_scope,
    first_loss_interval,
)
from market_risk.wavewarn.execution import ExecutionDay
from market_risk.wavewarn.inputs import DevelopmentInputs
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.ledgers import build_alert_ledger
from market_risk.wavewarn.loss import AssetLossDay, MainLossDay


def test_candidate_grid_and_first_loss_boundary() -> None:
    config = WavewarnConfig({"candidates": {"k": [3, 5, 10], "theta_p": ["0.015", "0.02", "0.025"],
                                                    "q": ["0.10", "0.20"]}})
    grid = candidate_grid(config)
    # P0 3×3=9、P1 9、N 3×3×2=18、去 B/DV 后 q 无作用故9，共45。
    assert [sum(row.model == model for row in grid) for model in ("P0", "P1", "N", "N去B/DV")] == [9, 9, 18, 9]
    days = tuple(dt.date(2020, 1, day) for day in (2, 3, 6, 7, 8))
    # τ 与最晚收敛日同为 1月3日；信号日必须再经历 1月6日、1月7日两个交易日，j₀=1月7日。
    assert first_loss_interval(days, days[1], (days[0], days[1])) == days[3]
    assert first_loss_interval(days, days[4], (days[0], days[1])) == days[4]


def test_asset_rows_reconcile_even_with_one_exclusion_and_terminal_switch() -> None:
    days = (dt.date(2020, 1, 2), dt.date(2020, 1, 3))
    candidate = Candidate("P1", 3, Decimal("0.015"), None, 0)
    executions = {
        symbol: (ExecutionDay(days[0], "黄", "黄", Decimal("0.5"), True, True),
                 ExecutionDay(days[1], "绿", "绿", Decimal("1"), True, True))
        for symbol in ("SPX", "QQQ")
    }
    # SPX 原始危险项=2，QQQ 跨缺价区间价格项=0；各权重0.5→加权价格项=1。
    assets = {
        "SPX": (AssetLossDay(days[0], days[1], Decimal("0.5"), Decimal("-0.1"), True,
                             Decimal(0), Decimal(2), Decimal(0), Decimal(0), ""),),
        "QQQ": (AssetLossDay(days[0], days[1], Decimal("0.5"), None, None,
                             Decimal(0), Decimal(0), Decimal(0), Decimal(0), "跨缺价区间"),),
    }
    # 系统切换成本0.2，每资产均分0.1；首日总额=1+0.2=1.2。
    # 窗口末日虽有执行切换标记，却无后续区间，成本=0。
    daily = (MainLossDay(days[0], Decimal(1), Decimal(0), Decimal(0), Decimal("0.2"), Decimal(0)),
             MainLossDay(days[1], Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0), True))
    evaluated = CandidateEvaluation(candidate, days, ("黄", "绿"), ("完整", "完整"), ((), ()), ("", ""),
                                    executions, assets, daily)
    rows = allocated_asset_days(evaluated, {"SPX": Decimal("0.5"), "QQQ": Decimal("0.5")})
    assert (rows[0].weighted_price_loss, rows[0].switch_share, rows[0].total) == (
        Decimal(1), Decimal("0.1"), Decimal("1.1"))
    assert (rows[1].weighted_price_loss, rows[1].switch_share, rows[1].total,
            rows[1].excluded_reason) == (Decimal(0), Decimal("0.1"), Decimal("0.1"), "跨缺价区间")
    assert sum((row.total for row in rows), Decimal(0)) == evaluated.total_loss == Decimal("1.2")
    assert all(row.terminal_switch_unbilled and row.switch_share == 0 for row in rows[-2:])


def test_cross_start_and_event_ledger_have_distinct_boundaries() -> None:
    days = tuple(stock_trading_days(dt.date(2020, 1, 2), dt.date(2020, 3, 2)))
    tau, first_loss_day = days[4], days[6]
    # P=第6行早于 j₀=第7行、Tr=第9行，跨起点；第7→8与8→9行仍用于逐日危险价格项，
    # 但整个事件不能进满暴露罚项，且 P−20 不存在，不能进五类事件账。
    crossing = ZZEvent("SPX", days[5], days[6], days[8], days[10],
                       Decimal(100), Decimal(90), False)
    assert event_scope(days, crossing, tau, first_loss_day) == (True, False, False)
    # P=第24行不早于 j₀，可进罚项；但 P−20=第4行早于 τ=第5行，不能进五类事件账。
    late = ZZEvent("SPX", days[23], days[24], days[26], days[28],
                   Decimal(100), Decimal(90), False)
    assert event_scope(days, late, tau, first_loss_day) == (False, True, False)
    # P=第25行的前20行恰为 τ=第5行，五类事件账可纳入。
    eligible = ZZEvent("SPX", days[24], days[25], days[27], days[29],
                       Decimal(100), Decimal(90), False)
    assert event_scope(days, eligible, tau, first_loss_day) == (False, True, True)


def test_alert_ledger_keeps_event_started_before_loss_axis() -> None:
    days = tuple(stock_trading_days(dt.date(2020, 1, 6), dt.date(2020, 1, 8)))
    event = ZZEvent("SPX", dt.date(2020, 1, 3), days[0], days[1], days[2],
                    Decimal(100), Decimal(90), False)
    # P=1月3日早于评价轴首日1月6日，但 [P,Tr] 与非绿警报段重叠，不是“无事件警报段”。
    losses = {
        "SPX": (AssetLossDay(days[0], days[1], Decimal("0.5"), Decimal("-0.1"), True,
                             Decimal(0), Decimal(0), Decimal(0), Decimal(0), ""),
                AssetLossDay(days[1], days[2], Decimal("0.5"), Decimal("0.1"), False,
                             Decimal(0), Decimal(0), Decimal(0), Decimal(0), "")),
        "QQQ": (AssetLossDay(days[0], days[1], Decimal("0.5"), Decimal("0.1"), False,
                             Decimal(0), Decimal(0), Decimal(0), Decimal(0), ""),
                AssetLossDay(days[1], days[2], Decimal("0.5"), Decimal("0.1"), False,
                             Decimal(0), Decimal(0), Decimal(0), Decimal(0), "")),
    }
    rows = build_alert_ledger(days, ("黄", "黄", "绿"), ((), (), ()), (event,), losses,
                              {"SPX": Decimal("0.5"), "QQQ": Decimal("0.5")})
    assert len(rows) == 1
    assert rows[0].covered_events == ("SPX:2020-01-03",)
    assert rows[0].no_event_alert is False
    assert rows[0].outside_danger_days == 1  # 第二个价格区间两资产都在危险区外。


def _constructed_evaluation(prices: tuple[Decimal, ...], signals: tuple[str, ...]
                            ) -> tuple[PreparedEvaluation, CandidateStates, UnknownLabels]:
    """五日手算路径；所有未来价只在指定日期出现，过去灯色由显式序列给出。"""
    days = tuple(stock_trading_days(dt.date(2020, 1, 2), dt.date(2020, 1, 8)))[:len(prices)]
    config = load_wavewarn_config(Path(__file__).resolve().parents[1] / "config/wavewarn_v121.yaml")
    series = {symbol: dict(zip(days, prices, strict=True)) for symbol in ("SPX", "QQQ")}
    prepared = PreparedEvaluation(config, DevelopmentInputs(days, series), days[0], days[0], days[0], ())
    candidate = Candidate("P1", 3, Decimal("0.015"), None, 0)
    states = CandidateStates(candidate, days[0],
                             tuple(DiagnosticRow(day, "P1-E2", 3, Decimal("0.015"), signal,
                                                 "完整", (), "")
                                   for day, signal in zip(days, signals, strict=True)))
    unknown = UnknownLabels(days[-1], {symbol: frozenset() for symbol in series},
                            {symbol: {} for symbol in series})
    return prepared, states, unknown


def test_full_replay_and_incremental_prefix_ignore_future_price() -> None:
    # 前四个收盘为 100、98、99、101，末日设 102 或极端值 1000；前 3 个价格区间及
    # 对应的执行、切换均只依赖各区间两端与前一日信号，因此末日改价不能改变前三日明细。
    prices = tuple(Decimal(value) for value in ("100", "98", "99", "101", "102"))
    signals = ("绿", "黄", "黄", "绿", "绿")
    prepared, states, unknown = _constructed_evaluation(prices, signals)
    full = evaluate_candidate(prepared, states, {"SPX": (), "QQQ": ()}, unknown)
    prefix_prepared, prefix_states, prefix_unknown = _constructed_evaluation(prices[:4], signals[:4])
    prefix = evaluate_candidate(prefix_prepared, prefix_states, {"SPX": (), "QQQ": ()}, prefix_unknown)
    changed_prepared, changed_states, changed_unknown = _constructed_evaluation(
        (*prices[:4], Decimal("1000")), signals)
    changed = evaluate_candidate(changed_prepared, changed_states, {"SPX": (), "QQQ": ()}, changed_unknown)
    assert full.daily_losses[:3] == prefix.daily_losses[:3] == changed.daily_losses[:3]
    assert full.asset_losses["SPX"][:3] == prefix.asset_losses["SPX"][:3] == changed.asset_losses["SPX"][:3]
    assert full.executions["SPX"][:4] == prefix.executions["SPX"] == changed.executions["SPX"][:4]
    assert full.days == changed.days and prefix.days == full.days[:4]


def test_main_loss_entry_rejects_p1_and_n_without_e2() -> None:
    prices = tuple(Decimal(value) for value in ("100", "98", "99"))
    prepared, states, unknown = _constructed_evaluation(prices, ("绿", "黄", "绿"))
    raw = {**prepared.config.raw, "business": {**prepared.config.raw["business"], "e_version": "E1"}}
    wrong = PreparedEvaluation(WavewarnConfig(raw), prepared.inputs, prepared.t0, prepared.tau,
                               prepared.first_loss_day, ())
    # 主损失入口检查业务版本，不能仅凭状态行名绕过 E2 的唯一口径。
    import pytest

    with pytest.raises(ValueError, match="E2"):
        evaluate_candidate(wrong, states, {"SPX": (), "QQQ": ()}, unknown)
