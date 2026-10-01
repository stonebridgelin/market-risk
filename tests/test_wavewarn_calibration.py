"""固定延迟参照只核对构造事件，期望值均由注释中的价格人工推算。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from market_risk.wavewarn.calibration import (
    INFINITY,
    delay_boundary,
    distribution,
    fixed_delay_reference,
    threshold_comparisons,
)
from market_risk.wavewarn.labels_zz import ZZEvent


def _event() -> ZZEvent:
    days = [dt.date(2016, 12, day) for day in (23, 27, 28, 29, 30)]
    return ZZEvent("SPX", days[0], days[1], days[1], days[3], Decimal("100"), Decimal("80"), False)


def test_fixed_delay_classification_uses_next_t0_before_window_end() -> None:
    days = tuple(dt.date(2016, 12, day) for day in (23, 27, 28, 29, 30))
    closes = dict(zip(days, (Decimal("100"), Decimal("80"), Decimal("82"),
                             Decimal("85"), Decimal("88")), strict=True))
    event = _event()
    # Tr=27日；m=2 落在29日，下一 T0 也是29日，故先归③，即使该价存在。
    assert fixed_delay_reference(days, closes, event, days[3], 2).classification == "③"
    # m=10 已超期末，但下一 T0 在期末内，仍先归③；无下一事件才叫窗口不足。
    assert fixed_delay_reference(days, closes, event, days[3], 10).classification == "③"
    assert fixed_delay_reference(days, closes, event, None, 10).classification == "后续窗口不足"
    # m=3 恰为期末，R=(88−80)/(100−80)=0.4；期末当天可以计算。
    at_end = fixed_delay_reference(days, closes, event, None, 3)
    assert (at_end.classification, at_end.recovery) == ("②", Decimal("0.4"))


def test_conservative_percentiles_use_infinity_only_for_ranking() -> None:
    # 两个有限值 0.2、0.4 加一个③：有序 [0.2,0.4,∞]，中位=0.4，P75 插值 0.4/∞→超限。
    median_ok, both_ok, conservative_ok, main, conservative = threshold_comparisons(
        (Decimal("0.2"), Decimal("0.4")), 1)
    assert (median_ok, both_ok, conservative_ok) == (True, True, False)
    assert (main.n, conservative.n, conservative.median, conservative.p75) == (2, 3, Decimal("0.4"), INFINITY)
    # [0.2,∞] 的中位在有限值与∞之间，必须超限，不能产生 NaN。
    assert distribution((Decimal("0.2"), INFINITY)).median == INFINITY
    # 四个样本 [0.1,0.2,0.3,∞]：中位=(0.2+0.3)/2=0.25，P75 涉及∞。
    four = distribution((Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), INFINITY))
    assert (four.minimum, four.p25, four.median, four.p75, four.maximum) == (
        Decimal("0.1"), Decimal("0.175"), Decimal("0.25"), INFINITY, INFINITY)


def test_calibration_tables_counts_and_actual_delay_split() -> None:
    """纯计算入口：一件 SPX 事件，无下一事件；QQQ 无纳入事件。期望值按下列价格手算。"""
    from market_risk.wavewarn.calibration import calibration_tables

    days = tuple(dt.date(2016, 12, day) for day in (23, 27, 28, 29, 30))
    closes = dict(zip(days, (Decimal("100"), Decimal("80"), Decimal("82"),
                             Decimal("85"), Decimal("88")), strict=True))
    event = _event()            # P=23日 100，Tr=27日 80，End=29日
    exit_rows = ({"inclusion": "纳入", "symbol": "SPX", "peak_date": "2016-12-23", "trough_date": "2016-12-27",
                  "scenario": "P1-E2", "k": "3", "theta_p": "0.015",
                  "rebound_class": "②低点或之后转绿", "first_green_from_trough": "2016-12-30"},)
    tables = calibration_tables(days, {"SPX": closes, "QQQ": {}},
                                {"SPX": ((event, None),), "QQQ": ()}, exit_rows)
    # 事件跌幅=(100−80)/100=0.2；R_end=(C(29日)85−80)/(100−80)=0.25。
    assert tables.event_rows[0][8:] == (Decimal("0.2"), Decimal("0.25"))
    # 8 个延迟：m=0、1、2、3 的执行日为 27、28、29、30 日，归②；m=5、8、10、15 超出期末且无下一事件，归窗口不足。
    by_m = {row[1]: row[3:6] for row in tables.fixed_summary if row[0] == "SPX"}
    assert [by_m[m] for m in (0, 1, 2, 3, 5, 8, 10, 15)] == [(1, 0, 0)] * 4 + [(0, 0, 1)] * 4
    assert all(sum(by_m[m]) == 1 for m in by_m)                     # 三项之和等于纳入数
    # m=3：R=(88−80)/20=0.4。
    assert [row[6] for row in tables.fixed_rows if row[3] == 3] == [Decimal("0.4")]
    # 实际 g=30日，相对 Tr=27日 延迟 3 个交易日；End=29日，故 g>End。
    assert tables.actual_rows == (("P1-E2", "3", "0.015", "SPX", days[0], days[1], days[4], 3, "g>End"),)
    assert tables.actual_summary[0][4:7] == (0, 1, 0)


def test_calibration_algorithm_module_does_not_import_io_modules() -> None:
    """依赖方向：纯计算模块不得导入读写模块、services 或 CLI（含传递依赖）。"""
    import json
    import subprocess
    import sys

    code = ("import importlib, json, sys\n"
            "importlib.import_module('market_risk.wavewarn.calibration')\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.calibration_run', 'market_risk.wavewarn.exit_costs', 'market_risk.services',"
            " 'market_risk.cli')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []


def test_discrete_delay_boundary_monotonic_and_nonmonotonic() -> None:
    # 单调序列：0/1/2日满足，3日不满足；最大满足2、最小不满足3。
    assert delay_boundary(((0, True), (1, True), (2, True), (3, False))) == (2, 3, (0, 1, 2))
    # 非单调序列：0、3满足而1、2不满足；只列离散事实，不把1至3日解释为连续阈值。
    assert delay_boundary(((0, True), (1, False), (2, False), (3, True))) == (3, 1, (0, 3))
    assert delay_boundary(((0, None), (1, False))) == (None, 1, ())
