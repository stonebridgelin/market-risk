"""开发期编排的数值与边界测试；期望均从下列构造数据手算，推算过程写在各测试的注释里。"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn.config import WavewarnConfig, load_wavewarn_config
from market_risk.wavewarn.evaluation import (
    REFERENCE_LIGHTS,
    Candidate,
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    allocated_asset_days,
    axis_loss,
    candidate_grid,
    development_scope_rule,
    evaluate_candidate,
    event_scope,
    first_loss_interval,
    reference_evaluation,
    validation_scope_rule,
)
from market_risk.wavewarn.evaluation_tables import (
    DAILY_HEADER,
    EVENT_HEADER,
    ModelSummary,
    asset_ledger_entries,
    daily_rows,
    decided_by,
    ledger_class_rows,
    ledger_row,
    merged_ledger_entries,
    model_summary,
    ranked,
    ranking_rows,
)
from market_risk.wavewarn.execution import ExecutionDay, execute_asset
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent, merge_zz_events
from market_risk.wavewarn.ledgers import build_alert_ledger
from market_risk.wavewarn.loss import AssetLossDay, LossSettings, MainLossDay, configured_loss_settings
from market_risk.wavewarn.state_sequences import DiagnosticRow

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/wavewarn_v121.yaml"
HALF = Decimal("0.5")
NO_EVENTS: dict[str, tuple[ZZEvent, ...]] = {"SPX": (), "QQQ": ()}


def _near(actual: Decimal, expected: Decimal) -> bool:
    """对数用 Decimal 28 位计算；只容许末位舍入差。"""
    return abs(actual - expected) < Decimal("1e-20")


def _ln(numerator: str, denominator: str) -> Decimal:
    return (Decimal(numerator) / Decimal(denominator)).ln()


def _days(count: int) -> tuple[dt.date, ...]:
    return tuple(stock_trading_days(dt.date(2020, 1, 2), dt.date(2020, 6, 30)))[:count]


def _prepared(days: tuple[dt.date, ...], spx: tuple[str, ...], qqq: tuple[str, ...],
              first_loss_index: int = 0) -> PreparedEvaluation:
    """t0=τ=首日；j₀ 由参数指定。价格只来自参数，没有读取任何数据文件。"""
    series = {"SPX": dict(zip(days, map(Decimal, spx), strict=True)),
              "QQQ": dict(zip(days, map(Decimal, qqq), strict=True))}
    return PreparedEvaluation(load_wavewarn_config(CONFIG_PATH), DevelopmentInputs(days, series),
                              days[0], days[0], days[first_loss_index], ())


def _states(days: tuple[dt.date, ...], signals: tuple[str, ...], order: int = 0) -> CandidateStates:
    candidate = Candidate("P1", 3, Decimal("0.015"), None, order)
    return CandidateStates(candidate, days[0], tuple(
        DiagnosticRow(day, "P1-E2", 3, Decimal("0.015"), signal, "完整", (), "")
        for day, signal in zip(days, signals, strict=True)))


def _no_unknown(days: tuple[dt.date, ...]) -> UnknownLabels:
    return UnknownLabels(days[-1], {"SPX": frozenset(), "QQQ": frozenset()}, {"SPX": {}, "QQQ": {}})


def test_candidate_grid_and_first_loss_boundary() -> None:
    config = WavewarnConfig({"candidates": {"k": [3, 5, 10], "theta_p": ["0.015", "0.02", "0.025"],
                                                    "q": ["0.10", "0.20"]}})
    grid = candidate_grid(config)
    # P0 3×3=9、P1 9、N 3×3×2=18、去 B/DV 后 q 无作用故9，共45。
    assert [sum(row.model == model for row in grid) for model in ("P0", "P1", "N", "N去B/DV")] == [9, 9, 18, 9]
    days = tuple(dt.date(2020, 1, day) for day in (2, 3, 6, 7, 8))
    # τ 与最晚收敛日同为 1月3日；信号日必须再经历 1月6日、1月7日两个交易日，j₀=τ+2=1月7日。
    assert first_loss_interval(days, days[1], (days[0], days[1])) == days[3]
    # τ 比最晚收敛日晚 3 个交易日时，j₀=τ。
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
                                    executions, assets, daily, ("绿", "黄"))
    weights = {"SPX": HALF, "QQQ": HALF}
    rows = allocated_asset_days(evaluated, weights)
    assert (rows[0].weighted_price_loss, rows[0].switch_share, rows[0].total) == (
        Decimal(1), Decimal("0.1"), Decimal("1.1"))
    assert (rows[1].weighted_price_loss, rows[1].switch_share, rows[1].total,
            rows[1].excluded_reason) == (Decimal(0), Decimal("0.1"), Decimal("0.1"), "跨缺价区间")
    assert sum((row.total for row in rows), Decimal(0)) == evaluated.total_loss == Decimal("1.2")
    # 窗口末日两行都显示切换标记（system_switches=1），但分摊额为 0。
    assert all(row.terminal_switch_unbilled and row.system_switches == 1 and row.switch_share == 0
               for row in rows[-2:])
    # 写出的每一行与表头等长；被排除的 QQQ 行仍带切换分摊额 0.1。
    prices = {"SPX": {days[0]: Decimal(100), days[1]: Decimal(90)}, "QQQ": {days[1]: Decimal(200)}}
    written = daily_rows(evaluated, weights, prices)
    assert all(len(row) == len(DAILY_HEADER) for row in written)
    assert written[1][DAILY_HEADER.index("switch_cost_share")] == Decimal("0.1")
    assert written[1][DAILY_HEADER.index("close")] == ""           # 缺价不填补


def test_daily_items_sum_to_total_and_counts_use_executed_light() -> None:
    days = _days(5)
    prices = ("100", "98", "99", "101", "102")
    prepared = _prepared(days, prices, prices)
    evaluated = evaluate_candidate(prepared, _states(days, ("绿", "黄", "黄", "绿", "绿")),
                                   NO_EVENTS, _no_unknown(days))
    # 执行灯色 S_{j−1}：第1日沿用初始绿，其后为前一日信号 → 绿、绿、黄、黄、绿；暴露 1、1、0.5、0.5、1。
    assert evaluated.system_executed == ("绿", "绿", "黄", "黄", "绿")
    # 无事件，四个区间都在危险区间外。参考高点 H=100：98、99 均不低于 0.98×100，101 创新高后 102 更高，
    # 所以回撤项全为 0。机会项 = β×(1−e)×r：前两个区间 e=1 为 0；第3个 0.5×ln(101/99)；第4个 0.5×ln(102/101)。
    # 两资产价格相同、权重各 0.5，加权后等于单资产值：0.5×ln(101/99)+0.5×ln(102/101)≈0.0149265。
    price_part = HALF * _ln("101", "99") + HALF * _ln("102", "101")
    # 切换：第3日（绿→黄）计费一次 γ=κ_D×c_s=2×0.0025=0.005；第5日（黄→绿）是窗口末日，不计费。
    assert _near(evaluated.total_loss, price_part + Decimal("0.005"))
    assert [row.switch_cost for row in evaluated.daily_losses] == [0, 0, Decimal("0.005"), 0, 0]
    assert evaluated.daily_losses[-1].terminal_switch_unbilled
    # 执行非绿天数只数计入区间（前4日）的执行灯色：第3、4日，共2；计费切换1次。
    assert (evaluated.executed_non_green_days, evaluated.billed_switches) == (2, 1)
    # 日度项之和等于总损失；两条资产行逐日合计等于系统日度主损失；全部行合计等于主损失。
    summary = model_summary(_states(days, ("绿",) * 5), evaluated)
    assert (summary.danger_loss + summary.drawdown_loss + summary.opportunity_loss + summary.switch_cost
            + summary.miss_penalty) == summary.total_loss == evaluated.total_loss
    rows = allocated_asset_days(evaluated, {"SPX": HALF, "QQQ": HALF})
    for index, system in enumerate(evaluated.daily_losses):
        assert rows[2 * index].total + rows[2 * index + 1].total == system.total
    assert sum((row.total for row in rows), Decimal(0)) == evaluated.total_loss
    # 信号 绿、绿、黄、红、绿：执行灯色为 绿、绿、绿、黄、红，计入区间里只有第4日非绿（信号非绿却有2日）；
    # 第4日切换计费，第5日切换在窗口末日不计。
    other = evaluate_candidate(prepared, _states(days, ("绿", "绿", "黄", "红", "绿")),
                               NO_EVENTS, _no_unknown(days))
    assert (other.executed_non_green_days, other.billed_switches) == (1, 1)
    assert sum(light != "绿" for light in other.signals) == 2


def test_reference_rows_match_hand_calculation() -> None:
    days = _days(5)
    # j₀=第2日；从 j₀ 起 SPX 收盘 100、110、99、108，QQQ 恒为 200；无事件。首日价格不进入计算。
    prepared = _prepared(days, ("50", "100", "110", "99", "108"), ("200",) * 5, first_loss_index=1)
    results = {name: reference_evaluation(prepared, name, light, NO_EVENTS, _no_unknown(days))
               for name, light in REFERENCE_LIGHTS}
    # SPX 回撤：H 在 j₀ 重置为 100；110 创新高，H=110；110→99 时 X=ln(0.98×110/99)=ln(107.8/99)≈0.085158，
    # ΔX 即此值；99→108 时 108/110≥0.98，X=0。QQQ 不动，各项为 0。
    drawdown = _ln("107.8", "99")
    # SPX 三个区间的对数收益之和 = ln(110/100)+ln(99/110)+ln(108/99)≈0.076961。
    returns = _ln("110", "100") + _ln("99", "110") + _ln("108", "99")
    green, yellow, red = results["始终绿"], results["始终黄"], results["始终红"]
    # 始终绿 e=1：只有回撤项 = w×κ_0×1×ΔX = 0.5×0.5×ΔX；机会项 0。
    assert _near(green.drawdown_loss, HALF * HALF * drawdown) and green.opportunity_loss == 0
    assert _near(green.total_loss, HALF * HALF * drawdown)
    # 始终黄 e=0.5：回撤项 0.5×0.5×0.5×ΔX；机会项 = w×β×(1−0.5)×Σr = 0.5×0.5×Σr。
    assert _near(yellow.drawdown_loss, HALF * HALF * HALF * drawdown)
    assert _near(yellow.opportunity_loss, HALF * HALF * returns)
    # 始终红 e=0：危险项、回撤项都乘 e=0，只有机会项 = 0.5×1×Σr≈0.038481。
    assert (red.danger_loss, red.drawdown_loss) == (0, 0)
    assert _near(red.total_loss, HALF * returns) and _near(red.opportunity_loss, HALF * returns)
    # 三行切换项为 0；执行非绿天数：绿 0，黄、红各为计入区间数 3。
    assert [row.switch_cost for row in (green, yellow, red)] == [0, 0, 0]
    assert [row.executed_non_green_days for row in (green, yellow, red)] == [0, 3, 3]
    assert [row.exposure for row in (green, yellow, red)] == [1, HALF, 0]


def test_cross_start_event_only_counts_danger_after_first_loss_day() -> None:
    days = _days(6)
    # SPX 收盘 100、95、96、90、95、96；事件 P=第1日、T0=第2日、Tr=第4日、结束日=第5日。j₀=第2日，τ=第1日。
    prepared = _prepared(days, ("100", "95", "96", "90", "95", "96"), ("200",) * 6, first_loss_index=1)
    event = ZZEvent("SPX", days[0], days[1], days[3], days[4], Decimal(100), Decimal(90), False)
    events = {"SPX": (event,), "QQQ": ()}
    evaluated = evaluate_candidate(prepared, _states(days, ("绿",) * 6), events, _no_unknown(days))
    # 全程绿灯 e=1。危险区间 [P,Tr) 落在 j₀ 之后的只有第2→3日、第3→4日：
    # 第2→3日上涨，危险项 0；第3→4日 = κ_D×1×ln(96/90) = 2×0.0645385。加权 0.5 → ln(96/90)≈0.0645385。
    # 第1→2日（100→95，危险项 2×ln(100/95)≈0.1026）早于 j₀，不计。
    # Tr 之后：第4→5日 95/96≥0.98，回撤为 0；第5日是结束日，H 重置；e=1 机会项为 0。无切换。
    assert _near(evaluated.total_loss, _ln("96", "90"))
    assert [row.dangerous for row in evaluated.asset_losses["SPX"]] == [True, True, False, False]
    # 跨起点事件不进“危险区间全程满暴露”罚项（即使 μ>0），也不进事件账。
    rule = development_scope_rule(prepared.tau, prepared.first_loss_day)
    scope = event_scope(days, event, rule)
    assert (scope.crosses_start, scope.complete_for_penalty, scope.in_ledger, scope.category) == (
        True, False, False, "跨起点")
    base = configured_loss_settings(prepared.config)
    with_mu = LossSettings(base.parameters, Decimal("0.1"), base.weight_spx, base.weight_qqq)
    penalised = axis_loss(prepared, evaluated.executions, events, _no_unknown(days), with_mu)
    assert penalised.miss_penalty == {"SPX": {}, "QQQ": {}}
    assert sum((row.total for row in penalised.daily_losses), Decimal(0)) == evaluated.total_loss
    entries = asset_ledger_entries(prepared, _states(days, ("绿",) * 6), events, rule)
    assert [(entry.in_ledger, entry.classified) for entry in entries] == [(False, None)]


def test_miss_penalty_sits_on_own_asset_row_before_trough() -> None:
    days = _days(6)
    prepared = _prepared(days, ("100", "100", "110", "104", "100", "106"),
                         ("200", "210", "200", "190", "205", "205"), first_loss_index=1)
    # SPX 事件 P=第3日、Tr=第5日；QQQ 事件 P=第2日、Tr=第4日。两者的 P 都不早于 j₀=第2日。
    events = {"SPX": (ZZEvent("SPX", days[2], days[3], days[4], days[5], Decimal(110), Decimal(100), False),),
              "QQQ": (ZZEvent("QQQ", days[1], days[2], days[3], days[4], Decimal(210), Decimal(190), False),)}
    # 信号 黄、绿、绿、绿、绿、绿 → 执行灯色（第2日起）黄、绿、绿、绿、绿，暴露 0.5、1、1、1、1。
    signals = ("黄", "绿", "绿", "绿", "绿", "绿")
    eta = configured_loss_settings(prepared.config).parameters.eta
    executions = {symbol: execute_asset(days, signals, tuple(prepared.inputs.series[symbol][day] for day in days),
                                        eta)[1:] for symbol in ("SPX", "QQQ")}
    base = configured_loss_settings(prepared.config)
    mu = Decimal("0.1")
    losses = axis_loss(prepared, executions, events, _no_unknown(days),
                       LossSettings(base.parameters, mu, base.weight_spx, base.weight_qqq))
    # SPX 危险区间为第3→4、4→5日，暴露都是 1 → 触发，罚项记在区间 Tr−1（第4日）的 SPX 行。
    # QQQ 危险区间为第2→3、3→4日，第2日暴露 0.5 → 不触发。
    assert losses.miss_penalty == {"SPX": {days[3]: mu}, "QQQ": {}}
    evaluated = CandidateEvaluation(Candidate("P1", 3, Decimal("0.015"), None, 0), days[1:], signals[1:],
                                    ("完整",) * 5, ((),) * 5, ("",) * 5, executions, losses.asset_losses,
                                    losses.daily_losses, signals[:-1], losses.miss_penalty)
    rows = allocated_asset_days(evaluated, {"SPX": HALF, "QQQ": HALF})
    # 整笔 0.1 只出现在 SPX 的第4日行，不乘权重、不摊给 QQQ；其余行为 0。
    assert [(row.symbol, row.date) for row in rows if row.miss_penalty] == [("SPX", days[3])]
    assert sum((row.miss_penalty for row in rows), Decimal(0)) == mu
    # 全部行合计等于主损失；与 μ=0 相比恰好多出一笔 0.1。
    total = sum((row.total for row in losses.daily_losses), Decimal(0))
    assert sum((row.total for row in rows), Decimal(0)) == total
    without = axis_loss(prepared, executions, events, _no_unknown(days), base)
    assert total - sum((row.total for row in without.daily_losses), Decimal(0)) == mu


def test_development_scope_boundaries_are_independent() -> None:
    days = tuple(stock_trading_days(dt.date(2020, 1, 2), dt.date(2020, 3, 2)))
    rule = development_scope_rule(days[4], days[6])                       # τ=第5行，j₀=第7行
    # P=第6行早于 j₀、Tr=第9行：跨起点，不进罚项；P 前不足20行，不进事件账。
    crossing = event_scope(days, ZZEvent("SPX", days[5], days[6], days[8], days[10],
                                         Decimal(100), Decimal(90), False), rule)
    assert (crossing.crosses_start, crossing.complete_for_penalty, crossing.in_ledger) == (True, False, False)
    # P=第24行不早于 j₀，进罚项；但 P−20=第4行早于 τ=第5行，仅因高点前20天不足而不进事件账。
    late = event_scope(days, ZZEvent("SPX", days[23], days[24], days[26], days[28],
                                     Decimal(100), Decimal(90), False), rule)
    assert (late.complete_for_penalty, late.in_ledger, late.category) == (True, False, "完整纳入但高点前20天不足")
    # P=第25行，P−20 恰为 τ：进事件账。
    eligible = event_scope(days, ZZEvent("SPX", days[24], days[25], days[27], days[29],
                                         Decimal(100), Decimal(90), False), rule)
    assert (eligible.complete_for_penalty, eligible.in_ledger, eligible.category) == (
        True, True, "完整纳入并进事件账")
    # 右截尾事件三项都不纳入；j₀ 之前已结束的事件单列。
    censored = event_scope(days, ZZEvent("SPX", days[30], days[31], days[33], None,
                                         Decimal(100), Decimal(90), True), rule)
    assert (censored.complete_for_penalty, censored.in_ledger, censored.category) == (False, False, "右截尾")
    early = event_scope(days, ZZEvent("SPX", days[1], days[2], days[3], days[4],
                                      Decimal(100), Decimal(90), False), rule)
    assert (early.crosses_start, early.category) == (False, "起点之前结束")


def test_validation_scope_rule_on_constructed_calendar() -> None:
    """验证期纳入规则只用构造日历检验，不读取任何验证期数据，也不运行验证期评价。"""
    days = tuple(stock_trading_days(dt.date(2016, 9, 1), dt.date(2017, 3, 31)))
    start = days.index(dt.date(2017, 1, 3))
    first = ZZEvent("SPX", days[start], days[start + 1], days[start + 4], days[start + 6],
                    Decimal(100), Decimal(90), False)
    # τ 恰为 P 前第20个交易日（落在2016年）：事件按 P ≥ 2017-01-03 归属验证期，并可进事件账。
    rule = validation_scope_rule(days[start - 20])
    scope = event_scope(days, first, rule)
    assert days[start - 20].year == 2016
    assert (scope.complete_for_penalty, scope.in_ledger, scope.category) == (True, True, "完整纳入并进事件账")
    # τ 再晚一个交易日：P 前20天的灯色不全，只是不进事件账，罚项与逐日项照常。
    short = event_scope(days, first, validation_scope_rule(days[start - 19]))
    assert (short.complete_for_penalty, short.in_ledger, short.category) == (
        True, False, "完整纳入但高点前20天不足")
    # P 在 2016 年、Tr 在 2017 年：跨验证期起点，只计起点之后的逐日项，不进罚项与事件账。
    crossing = event_scope(days, ZZEvent("SPX", days[start - 3], days[start - 2], days[start + 5],
                                         days[start + 8], Decimal(100), Decimal(90), False), rule)
    assert (crossing.crosses_start, crossing.complete_for_penalty, crossing.in_ledger) == (True, False, False)
    # 模型须在验证期起点前已可用：τ 晚于 2017-01-03 时拒绝。
    with pytest.raises(ValueError, match="验证期"):
        validation_scope_rule(days[start + 1])


def test_event_ledger_covers_asset_and_merged_events() -> None:
    days = _days(40)
    flat = ("100",) * 40
    prepared = _prepared(days, flat, flat, first_loss_index=2)
    # SPX 事件 P=第26行、T0=第28行、Tr=第31行；QQQ 事件 P=第27行、T0=第29行、Tr=第33行（下标从0起为25…32）。
    spx = ZZEvent("SPX", days[25], days[27], days[30], days[34], Decimal(100), Decimal(90), False)
    qqq = ZZEvent("QQQ", days[26], days[28], days[32], days[36], Decimal(100), Decimal(90), False)
    events = {"SPX": (spx,), "QQQ": (qqq,)}
    merged = merge_zz_events((spx, qqq))
    # 两个闭区间 [P,Tr] 相交，合并为一件：P、T0 取最早（下标25、27），Tr 取最晚（下标32）。
    assert [(item.peak_date, item.t0_date, item.trough_date) for item in merged] == [
        (days[25], days[27], days[32])]
    # 灯色只有下标26为黄，其余为绿。
    signals = tuple("黄" if index == 26 else "绿" for index in range(40))
    states = _states(days, signals)
    rule = development_scope_rule(prepared.tau, prepared.first_loss_day)
    entries = (*asset_ledger_entries(prepared, states, events, rule),
               *merged_ledger_entries(prepared, states, merged, rule))
    # SPX：S_{T0−1}=下标26 非绿，警报段起点 26 在 [T0−20,T0−1] 内 → 新警报；S_{T0−2}=下标25 为绿 → 未执行。
    # QQQ：S_{T0−1}=下标27 为绿，[T0,Tr−1]=28…31 全绿，但 [P−20,T0−2]=6…26 含下标26 非绿 → 中断。
    # 合并事件 T0 同 SPX → 新警报。
    assert [(entry.scope, entry.classified.classification if entry.classified else None)
            for entry in entries] == [("SPX", "新警报"), ("QQQ", "中断"), ("合并", "新警报")]
    assert entries[0].classified is not None and entries[0].classified.signal_before_t0
    assert not entries[0].classified.reduction_executed_before_t0
    assert all(len(ledger_row(("P1", 3, Decimal("0.015"), ""), entry)) == len(EVENT_HEADER) for entry in entries)
    counts = {row[4]: row[7:13] for row in ledger_class_rows(("P1", 3, Decimal("0.015"), ""), entries)}
    # 各列：事件账纳入数、新警报、持续覆盖、迟到、中断、漏报。
    assert counts == {"SPX": (1, 1, 0, 0, 0, 0), "QQQ": (1, 0, 0, 0, 1, 0), "合并": (1, 1, 0, 0, 0, 0)}


def _summary(model: str, loss: str, non_green: int, switches: int, order: int) -> ModelSummary:
    zero = Decimal(0)
    return ModelSummary(Candidate(model, 3, Decimal("0.015"), None, order), dt.date(2010, 1, 4), 10,  # type: ignore[arg-type]
                        Decimal(loss), zero, zero, zero, zero, zero, zero, non_green, switches, False, 0, 0)


def test_ranking_ties_are_broken_in_three_levels() -> None:
    a = _summary("P1", "1.0", 10, 4, 3)
    b = _summary("P1", "1.0", 10, 4, 2)
    c = _summary("P1", "1.0", 10, 3, 5)
    d = _summary("P1", "1.0", 9, 9, 8)
    e = _summary("P1", "0.9", 99, 99, 9)
    other = _summary("P0", "0.1", 0, 0, 0)
    ordered = ranked((a, b, c, d, e, other), "P1")
    # 主损失最小的 e 居首；其余四个主损失并列：d 非绿天数 9 最少；c 与 a、b 非绿同为 10，c 切换 3 次较少；
    # a、b 两级都并列，登记顺序 2 的 b 在前。P0 的 other 不混入 P1 的排名。
    assert ordered == (e, d, c, b, a)
    assert [decided_by(first, second) for first, second in itertools.pairwise(ordered)] == [
        "主损失", "执行非绿天数", "计费切换次数", "登记顺序"]
    rows = ranking_rows((a, b, c, d, e, other))
    assert all(row[0] == "开发期工程运行，未锁定" for row in rows)
    assert [(row[1], row[2], row[6]) for row in rows] == [
        ("P0", 1, 0), ("P1", 1, 9), ("P1", 2, 8), ("P1", 3, 5), ("P1", 4, 2), ("P1", 5, 3)]
    # 解释性分解不参与选参。
    with pytest.raises(ValueError, match="不参与选参"):
        ranked((a,), "N去B/DV")


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
                              {"SPX": HALF, "QQQ": HALF})
    assert len(rows) == 1
    assert rows[0].covered_events == ("SPX:2020-01-03",)
    assert rows[0].no_event_alert is False
    assert rows[0].outside_danger_days == 1  # 第二个价格区间两资产都在危险区外。


def test_full_replay_and_incremental_prefix_ignore_future_price() -> None:
    # 前四个收盘为 100、98、99、101，末日设 102 或极端值 1000；前 3 个价格区间及
    # 对应的执行、切换均只依赖各区间两端与前一日信号，因此末日改价不能改变前三日明细。
    prices = ("100", "98", "99", "101", "102")
    signals = ("绿", "黄", "黄", "绿", "绿")
    days = _days(5)

    def run(count: int, last: str | None = None) -> CandidateEvaluation:
        used = (*prices[:count - 1], last or prices[count - 1])
        return evaluate_candidate(_prepared(days[:count], used, used), _states(days[:count], signals[:count]),
                                  NO_EVENTS, _no_unknown(days[:count]))

    full, prefix, changed = run(5), run(4), run(5, "1000")
    # 截断到第4日（增量运行的前缀）与全量重放、与改动未来价格后的结果，在截断日及之前逐项相同。
    assert full.daily_losses[:3] == prefix.daily_losses[:3] == changed.daily_losses[:3]
    assert full.asset_losses["SPX"][:3] == prefix.asset_losses["SPX"][:3] == changed.asset_losses["SPX"][:3]
    assert full.executions["SPX"][:4] == prefix.executions["SPX"] == changed.executions["SPX"][:4]
    assert full.system_executed[:4] == prefix.system_executed == changed.system_executed[:4]
    assert full.signals[:4] == prefix.signals
    assert full.days == changed.days and prefix.days == full.days[:4]


def test_main_loss_entry_rejects_p1_and_n_without_e2() -> None:
    days = _days(3)
    prepared = _prepared(days, ("100", "98", "99"), ("100", "98", "99"))
    raw = {**prepared.config.raw, "business": {**prepared.config.raw["business"], "e_version": "E1"}}
    wrong = PreparedEvaluation(WavewarnConfig(raw), prepared.inputs, prepared.t0, prepared.tau,
                               prepared.first_loss_day, ())
    # 主损失入口检查业务版本，不能仅凭状态行名绕过 E2 的唯一口径。
    with pytest.raises(ValueError, match="E2"):
        evaluate_candidate(wrong, _states(days, ("绿", "黄", "绿")), NO_EVENTS, _no_unknown(days))


def test_evaluation_algorithm_modules_do_not_import_io_modules() -> None:
    """依赖方向：评价的纯计算模块不得导入读写模块、services 或 CLI（含传递依赖）。"""
    code = ("import importlib, json, sys\n"
            "for name in ('evaluation', 'evaluation_tables', 'evaluation_report', 'state_sequences'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.calibration_run',"
            " 'market_risk.wavewarn.diagnostics', 'market_risk.wavewarn.development', 'market_risk.services',"
            " 'market_risk.cli', 'market_risk.research.io', 'market_risk.backtest.engine')"
            " or m.startswith('market_risk.scoring')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
