"""v1.3 描述性诊断的构造测试；期望值均由下列构造数据手算，推算过程写在各测试的注释里。"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.wavewarn.bottleneck import (
    CLASS_1,
    CLASS_2_EARLY,
    CLASS_2_JUDGED,
    CLASS_3,
    category_row,
    condition_rows,
    event_bottleneck,
)
from market_risk.wavewarn.channels import ChannelPredicate
from market_risk.wavewarn.condition_trace import (
    ABOVE_MA50,
    ALL_QUIET,
    NONPRICE_QUIET,
    NOT_RED,
    PRICE_CLEAR,
    Q_OK,
    RED_QUIET,
    REPAIRED_QQQ,
    REPAIRED_SPX,
    ConditionDay,
    condition_trace,
    required_conditions,
)
from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.diagnostics_v13 import is_median, listed_conditions
from market_risk.wavewarn.evaluation import Candidate, CandidateStates, PreparedEvaluation, evaluate_candidate
from market_risk.wavewarn.exit_costs import ExitCostEvent
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.labels_zz import UnknownLabels
from market_risk.wavewarn.lighting_reasons import (
    MAIN_ACTIVE,
    MAIN_RATE_LIMIT,
    MAIN_UNMET,
    day_reason,
    non_green_reasons,
    reason_rows,
)
from market_risk.wavewarn.period_stats import max_drawdown, period_row, year_indices, yearly_rows
from market_risk.wavewarn.state_machine import ReadyInputs
from market_risk.wavewarn.state_sequences import DiagnosticRow
from market_risk.wavewarn.timing import constant_reference

ROOT = Path(__file__).resolve().parents[1]
HALF = Decimal("0.5")
X2 = required_conditions("X2", False)                      # (1)(2)(4)(5)
LISTED = listed_conditions("X2", False)                    # (1)(2)(4)(5)(6)
DAYS = tuple(dt.date(2020, 1, 1) + dt.timedelta(days=offset) for offset in range(10))


def _trace(lights: str, **flags: str) -> tuple[ConditionDay, ...]:
    """lights 与各条件都用 10 个字符表示 10 天：灯色 G/Y/R；条件 1 成立、0 不成立。"""
    names = {"q": Q_OK, "price": PRICE_CLEAR, "spx": REPAIRED_SPX, "qqq": REPAIRED_QQQ, "ma": ABOVE_MA50}
    colour = {"G": "绿", "Y": "黄", "R": "红"}
    rows = []
    for index, day in enumerate(DAYS):
        values: dict[str, bool | None] = {names[key]: text[index] == "1" for key, text in flags.items()}
        values[NONPRICE_QUIET] = None
        values[ALL_QUIET] = False
        rows.append(ConditionDay(day, colour[lights[index]], colour[lights[index - 1]] if index else "绿", (),
                                 False, False, values))
    return tuple(rows)


def _cost(rebound_class: str, first_green: dt.date | None) -> ExitCostEvent:
    """事件 P=第1日、Tr=第3日（下标从0起）。"""
    return ExitCostEvent("SPX", DAYS[1], DAYS[3], "纳入", rebound_class, None, 0, None, None,  # type: ignore[arg-type]
                         Decimal(0) if first_green else None, None, first_green, ())


def test_bottleneck_uses_start_of_run_containing_signal_day() -> None:
    # 灯色：第1日黄，第2—4日红，第5日黄，第6日转绿（信号日），执行日为第7日。下一事件 T0=第9日，搜索到第8日。
    # (1) Q≥K 自第4日起成立；(2) 价格通道第3日成立、第4日失效、第5日起再成立；
    # (4) S5TW 第3日成立、第4—5日失效、第6日起再成立；(5) NDTW 自第4日起成立；(6) MA50 只在第8日成立。
    trace = _trace("GYRRRYGGGY", q="0000111111", price="0001011111", spx="0001001111", qqq="0000111111",
                   ma="0000000010")
    result = event_bottleneck(trace, _cost("②低点或之后转绿", DAYS[7]), DAYS[9], LISTED, X2)
    # 首次成立天数（自 Tr=第3日起）：(1) 第4日→1；(2) 第3日→0；(4) 第3日→0；(5) 第4日→1；(6) 第8日→5；
    # (7) 事件内有红灯，第5日起不在红灯→2。
    assert result.first_days == {Q_OK: 1, PRICE_CLEAR: 0, REPAIRED_SPX: 0, REPAIRED_QQQ: 1, ABOVE_MA50: 5,
                                 NOT_RED: 2}
    # 信号日第6日距 Tr 3 天，执行日第7日距 Tr 4 天。
    assert (result.category, result.signal_days, result.execution_days) == (CLASS_2_JUDGED, 3, 4)
    # 包含信号日的连续成立期起点：(1) 第4日；(2) 第5日；(4) 第6日；(5) 第4日；(7) 第5日（其前一日第4日为红，不早于 P）。
    # 起点最晚的是 (4)：它早先在第3日成立过，但失效后第6日才再次成立，按再次成立的起点判定。距 Tr 3 天。
    assert (result.bottlenecks, result.bottleneck_days) == ((REPAIRED_SPX,), 3)
    # (6) 在 X2 下不是候选：即使它成立得最晚（第8日）也不参与。
    assert ABOVE_MA50 not in X2 and result.search_end == DAYS[8]


def test_bottleneck_ties_and_red_to_yellow_condition() -> None:
    # 并列：(4)(5) 都从第6日起成立，其余更早 → 两个条件各计一次。
    tied = event_bottleneck(_trace("GYRRRYGGGY", q="0000111111", price="0001111111", spx="0000001111",
                                   qqq="0000001111", ma="0000000000"),
                            _cost("②低点或之后转绿", DAYS[7]), DAYS[9], LISTED, X2)
    assert tied.bottlenecks == (REPAIRED_SPX, REPAIRED_QQQ)
    # (7) 为瓶颈：各条件自第4日起都成立，但系统到第6日才由红降黄，第7日转绿（每天最多降一级）。
    # (7) 的非红期起点为第6日，晚于其他条件 → 瓶颈为 (7)，起点距 Tr 3 天；信号日第7日距 Tr 4 天。
    red = event_bottleneck(_trace("GYRRRRYGGY", q="0000111111", price="0000111111", spx="0000111111",
                                  qqq="0000111111", ma="0000000000"),
                           _cost("②低点或之后转绿", DAYS[8]), DAYS[9], LISTED, X2)
    assert (red.bottlenecks, red.bottleneck_days, red.signal_days) == ((NOT_RED,), 3, 4)
    # 红灯只出现在高点 P 之前（第0日），事件内没有红灯：(7) 不适用，首次成立天数留空，也不参与瓶颈。
    early = event_bottleneck(_trace("RYYYYYGGGY", q="0000111111", price="0000111111", spx="0000011111",
                                    qqq="0000111111", ma="0000000000"),
                             _cost("②低点或之后转绿", DAYS[7]), DAYS[9], LISTED, X2)
    assert early.first_days[NOT_RED] is None and early.bottlenecks == (REPAIRED_SPX,)
    # 没有下一事件时搜索到窗口末日（第9日）。
    assert event_bottleneck(_trace("GYRRRYGGGG", q="0000111111", price="0000111111", spx="0000011111",
                                   qqq="0000111111", ma="0000000001"),
                            _cost("②低点或之后转绿", DAYS[7]), None, LISTED, X2).first_days[ABOVE_MA50] == 6


def test_bottleneck_categories_and_summary_rows() -> None:
    trace = _trace("GYRRRYGGGY", q="0000111111", price="0001011111", spx="0001001111", qqq="0000111111",
                   ma="0000000010")
    judged = event_bottleneck(trace, _cost("②低点或之后转绿", DAYS[7]), DAYS[9], LISTED, X2)
    # 类别①、类别③不判瓶颈，但各条件的首次成立天数照常记录。
    first = event_bottleneck(trace, _cost("①低点前已绿", None), DAYS[9], LISTED, X2)
    third = event_bottleneck(trace, _cost("③下一事件前未转绿", None), DAYS[9], LISTED, X2)
    assert (first.category, third.category, first.bottlenecks, third.signal_days) == (CLASS_1, CLASS_3, (), None)
    assert first.first_days == judged.first_days
    # 类别②但执行日恰为 Tr（第3日）：信号日为 Tr−1，记 −1 与 0，不判瓶颈。
    early = event_bottleneck(trace, _cost("②低点或之后转绿", DAYS[3]), DAYS[9], LISTED, X2)
    assert (early.category, early.signal_days, early.execution_days, early.bottlenecks) == (
        CLASS_2_EARLY, -1, 0, ())
    events = (judged, first, third, early)
    # 件数：纳入 4；①、③、信号日 Tr−1、判瓶颈各 1；并列事件 0。
    assert category_row("SPX", events) == ("SPX", 4, 1, 1, 1, 1, 0)
    rows = {row[1]: row for row in condition_rows("SPX", events, LISTED, X2)}
    # (4)：四个事件的首次成立天数都是 0 → 成立 4、未成立 0、中位 0；成为瓶颈 1 次。
    assert rows[REPAIRED_SPX][2:] == ("是", 4, 0, Decimal(0), Decimal(0), Decimal(0), 1)
    # (6) 只列天数：四个事件都是 5；不是候选，瓶颈次数 0。
    assert rows[ABOVE_MA50][2:] == ("否（只列天数）", 4, 0, Decimal(5), Decimal(5), Decimal(5), 0)
    # 信号日：只有两个类别②事件有值（3 与 −1）→ 成立 2、空 2，中位 1。
    assert rows["(8) 首个绿灯信号日"][3:6] == (2, 2, Decimal(1))
    # 绿灯信号日当天候选条件不成立属于数据不一致，直接报错。
    with pytest.raises(ValueError, match="不成立"):
        event_bottleneck(trace, _cost("②低点或之后转绿", DAYS[5]), DAYS[9], LISTED, X2)


def _day(light: str, previous: str, active: tuple[str, ...] = (), red_quiet: bool = True,
         **unmet: bool) -> ConditionDay:
    flags: dict[str, bool | None] = {name: True for name in X2}
    flags.update({ABOVE_MA50: True, ALL_QUIET: True, NONPRICE_QUIET: None})
    names = {"q": Q_OK, "price": PRICE_CLEAR, "spx": REPAIRED_SPX, "qqq": REPAIRED_QQQ}
    for key, value in unmet.items():
        flags[names[key]] = value
    return ConditionDay(dt.date(2020, 1, 2), light, previous, active, False, red_quiet, flags)


def test_day_reason_main_classes_are_exclusive() -> None:
    # A：有通道激活，列出每个激活的通道。
    active = day_reason(_day("红", "黄", ("PR_SPX", "P_SPX")), X2)
    assert (active.main, active.labels) == (MAIN_ACTIVE, ("通道激活：PR_SPX", "通道激活：P_SPX"))
    # B：红灯、无通道激活，红通道静默不足 3 天 → 列未满足的红转黄条件。
    red = day_reason(_day("红", "红", red_quiet=False), X2)
    assert (red.main, red.labels) == (MAIN_UNMET, (f"红转黄未满足：{RED_QUIET}",))
    red_q = day_reason(_day("红", "红", red_quiet=False, q=False), X2)
    assert red_q.labels == (f"红转黄未满足：{RED_QUIET}", f"红转黄未满足：{Q_OK}")
    # C：当日刚由红降黄，且黄转绿条件已全部满足。
    limited = day_reason(_day("黄", "红"), X2)
    assert (limited.main, limited.labels) == (MAIN_RATE_LIMIT, ("降级限速：每天最多降一级",))
    # 刚由红降黄但黄转绿条件未全部满足：归 B，并列出未满足的条件。
    partial = day_reason(_day("黄", "红", spx=False), X2)
    assert (partial.main, partial.labels) == (MAIN_UNMET, (f"黄转绿未满足：{REPAIRED_SPX}",))
    # B：黄灯、无通道激活，两个条件未满足 → 两个标签。
    both = day_reason(_day("黄", "黄", spx=False, qqq=False), X2)
    assert both.labels == (f"黄转绿未满足：{REPAIRED_SPX}", f"黄转绿未满足：{REPAIRED_QQQ}")
    # 与状态机不符的情形（黄灯、无通道激活、条件全满足且前一日不是红灯）直接报错。
    with pytest.raises(ValueError, match="不符"):
        day_reason(_day("黄", "黄"), X2)
    with pytest.raises(ValueError, match="不属于"):
        day_reason(_day("绿", "黄"), X2)


def test_non_green_reasons_use_signal_day_and_count_labels() -> None:
    dates = tuple(dt.date(2020, 1, day) for day in (2, 3, 6, 7, 8))
    days = (_day("黄", "绿", ("P_SPX", "PR_SPX")), _day("红", "黄", red_quiet=False), _day("黄", "红"),
            _day("绿", "黄"), _day("绿", "绿"))
    trace = tuple(ConditionDay(date, day.light, day.previous_light, day.active, day.ready, day.red_quiet, day.flags)
                  for date, day in zip(dates, days, strict=True))
    # 区间起点为第2—5日，执行灯色 S_{j−1} 为前一日信号：黄、红、黄、绿 → 执行非绿 3 个。
    counts = non_green_reasons(trace, ("黄", "红", "黄", "绿"), dates[1:], X2)
    # 第2日区间取第1日的原因（两个通道激活，A）；第3日取第2日（红灯静默不足，B）；第4日取第3日（降级限速，C）。
    assert counts.non_green == 3
    assert (counts.main[MAIN_ACTIVE], counts.main[MAIN_UNMET], counts.main[MAIN_RATE_LIMIT]) == (1, 1, 1)
    assert counts.labels == {"通道激活：P_SPX": 1, "通道激活：PR_SPX": 1, f"红转黄未满足：{RED_QUIET}": 1,
                             "降级限速：每天最多降一级": 1}
    rows = reason_rows(counts)
    # 主分类三行占比各 1/3，合计 100%；细分原因四项各 1/3，合计超过 100%。
    assert [row[3] for row in rows[:3]] == [Decimal(1) / 3] * 3
    assert sum((row[3] for row in rows if row[0] == "细分原因"), Decimal(0)) > 1
    # 执行灯色与前一日信号不一致时报错。
    with pytest.raises(ValueError, match="不一致"):
        non_green_reasons(trace, ("红", "红", "黄", "绿"), dates[1:], X2)


def test_condition_trace_matches_state_machine_on_single_price_channel() -> None:
    days = tuple(dt.date(2020, 1, 1) + dt.timedelta(days=offset) for offset in range(10))
    # t0=第0日（初始快照）。第1日通道进入（Q=0）；第 d 日 Q=d−1；K=3，通道在第4日（Q=3）退出。
    predicates = tuple(ChannelPredicate(index == 1, index - 1 >= 3, True) for index in range(10))
    ready = tuple(ReadyInputs(max(index - 1, 0), max(index - 1, 0), True, True, True, True, True, True)
                  for index in range(10))
    channels = {"P_SPX": ("黄", predicates)}
    x2 = condition_trace(days, channels, ready, days[0], 3, "X2")  # type: ignore[arg-type]
    # 追踪自第1日起。X2：第4日 Q≥K 且价格通道当日未激活 → 当日转绿。
    assert [day.light for day in x2] == ["黄", "黄", "黄", "绿", "绿", "绿", "绿", "绿", "绿"]
    assert [day.flags[PRICE_CLEAR] for day in x2[:4]] == [False, False, False, True]
    assert [day.flags[Q_OK] for day in x2[:4]] == [False, False, False, True]
    assert x2[0].active == ("P_SPX",) and x2[3].active == () and x2[0].flags[NONPRICE_QUIET] is None
    # E2：退出当日为静默第1天，第8日满5天 → 第8日转绿；此前“全部通道连续5天”一项为否。
    e2 = condition_trace(days, channels, ready, days[0], 3, "E2")  # type: ignore[arg-type]
    assert [day.light for day in e2] == ["黄"] * 7 + ["绿", "绿"]
    assert [day.flags[ALL_QUIET] for day in e2[3:8]] == [False, False, False, False, True]


def _yearly_case() -> tuple[PreparedEvaluation, tuple[dt.date, ...], UnknownLabels]:
    days = (dt.date(2019, 12, 27), dt.date(2019, 12, 30), dt.date(2019, 12, 31), dt.date(2020, 1, 2),
            dt.date(2020, 1, 3), dt.date(2020, 1, 6))
    prices = dict(zip(days, map(Decimal, ("100", "101", "102", "103", "104", "105")), strict=True))
    prepared = PreparedEvaluation(load_wavewarn_config(ROOT / "config/wavewarn_v121.yaml"),
                                  DevelopmentInputs(days, {"SPX": prices, "QQQ": dict(prices)}), days[0],
                                  days[0], days[0], ())
    return prepared, days, UnknownLabels(days[-1], {"SPX": frozenset(), "QQQ": frozenset()},
                                         {"SPX": {}, "QQQ": {}})


def _ln(numerator: str, denominator: str) -> Decimal:
    return (Decimal(numerator) / Decimal(denominator)).ln()


def _near(actual: object, expected: Decimal) -> bool:
    return isinstance(actual, Decimal) and abs(actual - expected) < Decimal("1e-20")


def test_yearly_rows_use_each_years_own_exposure_and_benchmarks() -> None:
    prepared, days, unknown = _yearly_case()
    events = {"SPX": (), "QQQ": ()}
    states = CandidateStates(Candidate("P1", 3, Decimal("0.015"), None, 0), days[0], tuple(
        DiagnosticRow(day, "P1-E2", 3, Decimal("0.015"), light, "完整", (), "")
        for day, light in zip(days, ("绿", "黄", "黄", "绿", "绿", "绿"), strict=True)))
    model = evaluate_candidate(prepared, states, events, unknown)
    green = constant_reference(prepared, "始终绿", "绿", 0, events, unknown)
    red = constant_reference(prepared, "始终红", "红", 2, events, unknown)
    # 区间按起点归年：2019 年 3 个（12-27、12-30、12-31），2020 年 2 个（01-02、01-03）；末日无区间。
    assert year_indices(days) == {2019: [0, 1, 2], 2020: [3, 4]}
    rows = {row[1]: row for row in yearly_rows("模型", model, green, red, HALF)}
    # 执行灯色 绿、绿、黄、黄、绿。价格一路上涨，无回撤、无事件：L_G 各年为 0；L_R 为各年对数收益之和。
    # 2019：ē=(1+1+0.5)/3=5/6；主损失 = 机会项 0.5×ln(103/102) + 第3日切换 0.005；
    #       T = 主损失 − (1−5/6)×ln(103/100)；非绿占比 1/3。
    loss_2019 = HALF * _ln("103", "102") + Decimal("0.005")
    assert rows[2019][2] == 3 and rows[2019][4] == Decimal(5) / 6 and rows[2019][6] == Decimal(1) / 3
    assert _near(rows[2019][3], loss_2019)
    assert _near(rows[2019][5], loss_2019 - (1 - Decimal(5) / 6) * _ln("103", "100"))
    # 2020：ē=(0.5+1)/2=0.75；主损失 = 0.5×ln(104/103) + 第5日切换 0.005；T = 主损失 − 0.25×ln(105/103)。
    loss_2020 = HALF * _ln("104", "103") + Decimal("0.005")
    assert rows[2020][4] == Decimal("0.75") and rows[2020][6] == HALF
    assert _near(rows[2020][5], loss_2020 - Decimal("0.25") * _ln("105", "103"))
    # 全期：ē=4/5；T = 全期主损失 − 0.2×ln(105/100)。各年 T 之和不等于全期 T。
    assert rows["全期"][4] == Decimal("0.8")
    assert _near(rows["全期"][5], loss_2019 + loss_2020 - Decimal("0.2") * _ln("105", "100"))
    assert abs(rows[2019][5] + rows[2020][5] - rows["全期"][5]) > Decimal("1e-6")
    # 始终绿逐年 T 恒为 0。
    assert all(row[5] == 0 for row in yearly_rows("始终绿", green, green, red, HALF))


def test_period_row_and_max_drawdown() -> None:
    # 累计曲线：−0.1、−0.05、−0.25、0.05；最高点始终为起点 0（末步才回到 0.05）→ 最大回撤 0.25。
    assert max_drawdown([Decimal("-0.1"), Decimal("0.05"), Decimal("-0.2"), Decimal("0.3")]) == Decimal("0.25")
    assert max_drawdown([Decimal("0.1"), Decimal("0.2")]) == 0
    prepared, days, unknown = _yearly_case()
    red = constant_reference(prepared, "始终红", "红", 2, {"SPX": (), "QQQ": ()}, unknown)
    green = constant_reference(prepared, "始终绿", "绿", 0, {"SPX": (), "QQQ": ()}, unknown)
    # 期间取区间起点在 [12-30, 01-03)：12-30、12-31、01-02 三个区间，价格 101→104。
    row = period_row("构造期间", "始终绿", green, HALF, days[1], days[4])
    # 始终绿：暴露 1，按暴露累计对数收益 = ln(104/101)，一路上涨回撤 0；主损失 0。
    assert (row[2], row[3], row[4]) == (3, 1, 0)
    assert _near(row[5], _ln("102", "101") + _ln("103", "102") + _ln("104", "103")) and row[6] == 0
    # 始终红：暴露 0，按暴露累计收益 0、回撤 0；主损失 = 机会项 = ln(104/101)。
    row = period_row("构造期间", "始终红", red, HALF, days[1], days[4])
    assert (row[3], row[5], row[6]) == (0, 0, 0)
    assert _near(row[4], _ln("102", "101") + _ln("103", "102") + _ln("104", "103"))


def test_median_setting_and_dependency_direction() -> None:
    assert is_median(Candidate("N", 5, Decimal("0.02"), Decimal("0.10"), 0, "X2"))
    assert is_median(Candidate("P1", 5, Decimal("0.02"), None, 0))
    assert not is_median(Candidate("N", 5, Decimal("0.02"), Decimal("0.20"), 0))
    assert not is_median(Candidate("P1", 10, Decimal("0.02"), None, 0))
    # 依赖方向：诊断的纯计算模块不得导入读写模块、services 或 CLI（含传递依赖）。
    code = ("import importlib, json, sys\n"
            "for name in ('condition_trace', 'bottleneck', 'lighting_reasons', 'period_stats', 'diagnostics_v13',"
            " 'diagnostics_v13_report'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.evaluation_v13_run',"
            " 'market_risk.wavewarn.extended_history_run', 'market_risk.wavewarn.diagnostics_v13_run',"
            " 'market_risk.wavewarn.calibration_run', 'market_risk.wavewarn.diagnostics', 'market_risk.services',"
            " 'market_risk.cli') or m.startswith('market_risk.scoring')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
