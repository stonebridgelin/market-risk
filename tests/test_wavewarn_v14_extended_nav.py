"""补充历史真实净值与“带缓冲带的 200 日均线”的测试。

手算例只用构造的价格；读取真实数据的测试只读 SPX、QQQ 截至 2009-09-30 的收盘价（补充历史窗口末日）。
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from market_risk.wavewarn.buffered_ma import BUFFERED_MA200, buffered_ma_signals
from market_risk.wavewarn.config_v14 import load_round2_config, load_validation_config, parse_round2_config
from market_risk.wavewarn.diagnostics_round2 import MA200, PORTFOLIO, nav_rows
from market_risk.wavewarn.evaluation import evaluate_candidate, period_labels
from market_risk.wavewarn.extended_history import truncate_inputs
from market_risk.wavewarn.extended_history_v14 import prepare_price_window
from market_risk.wavewarn.extended_nav import (
    HOLD,
    SELECTED_PRICE,
    ExtendedNavResult,
    executed_lights,
    extended_nav,
    period_rows,
    reversal_share,
)
from market_risk.wavewarn.extended_nav_report import OPENING, csv_tables, report_lines
from market_risk.wavewarn.extended_nav_run import load_price_inputs
from market_risk.wavewarn.timing import ma200_signals, ma200_states

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = load_validation_config(ROOT / "config/wavewarn_v14_validation.yaml")
CONFIG = load_round2_config(ROOT / "config/wavewarn_v14_diagnostics.yaml")
WINDOW_END = dt.date(2009, 9, 30)
STORED = ROOT / "reports/research/wavewarn_v14/evaluation_development/extended_history"
D = Decimal
DAYS = tuple(dt.date(2005, 1, 3) + dt.timedelta(days=index) for index in range(8))


def _closes(values: tuple[str, ...]) -> dict[dt.date, Decimal | None]:
    return {day: D(value) for day, value in zip(DAYS, values, strict=False)}


def test_buffered_ma_signals_by_hand() -> None:
    """3 日均线、带宽 1%，自行号 2 起。

    收盘价 100、100、100、99.5、97、98.9、101：
    行号 2（起点）：均线 100，收盘 100 不低于均线 → 绿。
    行号 3：均线 (100+100+99.5)/3 = 99.8333；下沿 98.835、上沿 100.832；99.5 在带内 → 保持绿。
    行号 4：均线 (100+99.5+97)/3 = 98.8333；下沿 97.845；97 低于下沿 → 红。
    行号 5：均线 (99.5+97+98.9)/3 = 98.4667；上沿 99.451、下沿 97.482；98.9 在带内 → 保持红
            （不带缓冲的均线规则此时已转绿：98.9 ≥ 98.4667）。
    行号 6：均线 (97+98.9+101)/3 = 98.9667；上沿 99.956；101 高于上沿 → 绿。
    """
    closes = _closes(("100", "100", "100", "99.5", "97", "98.9", "101"))
    assert buffered_ma_signals(DAYS[:7], closes, DAYS[2], 3, D("0.01")) == ("绿", "绿", "红", "红", "绿")
    assert ma200_signals(DAYS[:7], closes, DAYS[2], 3) == ("绿", "红", "红", "绿", "绿")
    # 带宽为 0 时：严格高于均线转绿、严格低于转红，恰在均线上保持前状态。
    assert buffered_ma_signals(DAYS[:7], closes, DAYS[2], 3, D(0)) == ("绿", "红", "红", "绿", "绿")


def test_buffered_ma_start_rule_and_exact_band_edges_keep_state() -> None:
    """收盘价 100、100、99、101、103，自行号 2 起。

    行号 2（起点）：均线 99.6667，收盘 99 低于均线 → 红（起点用不带缓冲的规则）。
    行号 3：均线 (100+99+101)/3 = 100，上沿恰为 101；收盘 101 没有“高于”上沿 → 保持红。
    行号 4：均线 (99+101+103)/3 = 101，上沿 102.01；103 高于上沿 → 绿。
    """
    closes = _closes(("100", "100", "99", "101", "103"))
    assert buffered_ma_signals(DAYS[:5], closes, DAYS[2], 3, D("0.01")) == ("红", "红", "绿")
    # 起点当日恰在均线上：不低于均线 → 绿。
    assert buffered_ma_signals(DAYS[:3], _closes(("100", "100", "100")), DAYS[2], 3, D("0.01")) == ("绿",)
    # 缺收盘价或均线不足即报错，不填补。
    with pytest.raises(ValueError, match="缺少收盘价或均线"):
        buffered_ma_signals(DAYS[:5], {**closes, DAYS[3]: None}, DAYS[2], 3, D("0.01"))
    with pytest.raises(ValueError, match="缺少收盘价或均线"):
        buffered_ma_signals(DAYS[:5], closes, DAYS[1], 3, D("0.01"))


def test_buffer_band_is_pinned_to_registration() -> None:
    assert CONFIG.buffer_band == D("0.01")
    raw = yaml.safe_load((ROOT / "config/wavewarn_v14_diagnostics.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="不扫描其他变体"):
        parse_round2_config({**raw, "buffered_ma200": {"band": "0.02"}})


def test_period_rows_restart_wealth_inside_the_period() -> None:
    """满仓、两资产同价：收盘价 100、110、99、99、108.9 → 简单收益 +10%、−10%、0、+10%。

    期间 [行号 1, 行号 3)：取起点为行号 1、2 的两个区间，收益 −10%、0 → 期间收益 −10%；
    净值在期间起点记 1：1 → 0.9 → 0.9，最大回撤 10%（行号 1 至 2）。期间之前的 +10% 不计入。
    """
    days = DAYS[:5]
    simple = (D("0.1"), D("-0.1"), D(0), D("0.1"))
    rows = nav_rows("满仓", days, [D(1)] * 4, {"SPX": simple, "QQQ": simple},
                    {"SPX": D("0.5"), "QQQ": D("0.5")}, 0, D(0), CONFIG)
    (period,) = [row for row in period_rows(days, rows, ((days[1], days[3]),)) if row.scope == PORTFOLIO]
    assert (period.intervals, period.period_return) == (2, D("-0.1"))
    assert (period.drawdown.depth, period.drawdown.peak_date, period.drawdown.trough_date) == (
        D("0.1"), days[1], days[2])
    with pytest.raises(ValueError, match="不在评价窗口内"):
        period_rows(days, rows, ((dt.date(2004, 1, 1), dt.date(2004, 2, 1)),))
    assert reversal_share((), 5) is None


# ---------------------------------------------------------------------------
# 真实数据：只读 SPX、QQQ 截至 2009-09-30 的收盘价
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def history() -> ExtendedNavResult:
    inputs = load_price_inputs(ROOT, WINDOW_END, VALIDATION.vix3m_file)
    return extended_nav(prepare_price_window(VALIDATION.model, inputs), VALIDATION, CONFIG)


def test_window_matches_extended_history_and_reads_only_prices_before_window_end(
        history: ExtendedNavResult) -> None:
    result = history
    inputs = result.prepared.inputs
    assert set(inputs.series) == {"SPX", "QQQ"} and inputs.days[-1] == WINDOW_END
    assert all(max(series) <= WINDOW_END for series in inputs.series.values())
    # 窗口与 v1.4 补充历史相同：j₀′ = 1999-09-07，2532 个区间。
    assert (result.days[0], result.days[-1], len(result.days) - 1) == (dt.date(1999, 9, 7), WINDOW_END, 2532)
    assert all(row.metrics.intervals == 2532 for row in (*result.nav, *result.grid_nav))
    # 输入不是恰好截至窗口末日时拒绝。
    shorter = dataclasses.replace(result.prepared, inputs=truncate_inputs(inputs, dt.date(2009, 9, 29)))
    with pytest.raises(ValueError, match="截至窗口末日"):
        extended_nav(shorter, VALIDATION, CONFIG)


def test_executed_lights_match_the_loss_path_and_stored_extended_history(history: ExtendedNavResult) -> None:
    """不读标签得到的执行灯色，与损失计算路径、与已入库的补充历史输出一致。"""
    result, prepared = history, history.prepared
    # 标签只用窗口内的价格生成（截至 2009-09-30），仅用于走一遍损失路径做核对。
    events, unknown = period_labels(prepared.config, prepared.inputs, WINDOW_END)
    with (STORED / "price_only_summary.csv").open(encoding="utf-8", newline="") as file:
        stored = {(int(row["k"]), D(row["theta_p"])): row for row in csv.DictReader(file)}
    for states, item in zip(prepared.states, result.grid, strict=True):
        evaluated = evaluate_candidate(prepared, states, events, unknown)
        assert executed_lights(prepared, states) == evaluated.system_executed == item.lights
        # 诊断口径不计 j₀′ 当日的切换（初始建仓）；主损失的计费口径计入，所以恰好相差这一次。
        assert evaluated.billed_switches == len(item.switches) + int(item.switch_at_start)
        row = stored[states.candidate.k, states.candidate.theta_p]
        assert D(row["mean_exposure"]) == item.mean_exposure
        assert int(row["billed_switches"]) == evaluated.billed_switches
    average = next(item for item in result.objects if item.name == MA200)
    assert executed_lights(prepared, ma200_states(prepared, VALIDATION.model.mr_window)) == average.lights
    assert not average.switch_at_start and len(average.switches) == 96


def test_objects_and_reference_rows_follow_definitions(history: ExtendedNavResult) -> None:
    result = history
    assert [item.name for item in result.objects] == [SELECTED_PRICE, MA200, BUFFERED_MA200]
    selected = result.objects[0]
    chosen = next(item for item in result.grid if "K=5 θ_P=0.025" in item.name)
    assert (selected.lights, selected.switches) == (chosen.lights, chosen.switches) and len(result.grid) == 9
    rows = {(row.name, row.scope): row for row in result.nav}
    assert len(result.nav) == 18 and len(result.grid_nav) == 27
    closes = result.prepared.inputs.series
    for symbol in ("SPX", "QQQ"):
        # 一直持有的单资产累计收益就是首末收盘价之比减一。
        expected = closes[symbol][WINDOW_END] / closes[symbol][result.days[0]] - 1
        assert abs(rows[HOLD, symbol].metrics.cumulative - expected) < D("1e-18")
    # 缓冲带只会减少切换：带缓冲带的均线切换次数不多于 200 日均线；两者都只有绿与红。
    average, buffered = result.objects[1], result.objects[2]
    assert len(buffered.switches) <= len(average.switches) and set(buffered.lights) <= {"绿", "红"}
    # 同平均暴露基准没有切换；暴露等于对应对象的 ē。
    constant = [row for row in result.nav if row.name.startswith("恒定暴露")]
    assert len(constant) == 6 and all(row.switches == 0 for row in constant)


def test_bear_market_periods(history: ExtendedNavResult) -> None:
    result = history
    spans = sorted({(row.start, row.end, row.intervals) for row in result.periods})
    assert [(start, end) for start, end, _ in spans] == [(dt.date(2000, 3, 24), dt.date(2002, 10, 9)),
                                                         (dt.date(2007, 10, 9), dt.date(2009, 3, 9))]
    assert len(result.periods) == 2 * 18
    closes = result.prepared.inputs.series["SPX"]
    for start, end, intervals in spans:
        assert intervals == sum(start <= day < end for day in result.days)
        hold = next(row for row in result.periods if (row.name, row.scope, row.start) == (HOLD, "SPX", start))
        # 一直持有 SPX 的期间收益 = 期末收盘 ÷ 期初收盘 − 1；期间最大回撤不小于期间跌幅。
        assert abs(hold.period_return - (closes[end] / closes[start] - 1)) < D("1e-18")
        assert hold.drawdown.depth >= -hold.period_return and start <= hold.drawdown.peak_date < end


def test_report_opening_and_tables(history: ExtendedNavResult) -> None:
    lines = report_lines(history, CONFIG)
    assert lines[2] == OPENING
    assert OPENING == "本报告仅使用2009-09-30以前的纯价格数据；诊断不改变模型规则、参数、选定设定与γ。"
    text = "\n".join(lines)
    for heading in ("## 一、对象", "## 二、完整净值", "## 三、纯价格版九组汇总", "## 四、两次熊市"):
        assert heading in text
    tables = csv_tables(history, CONFIG)
    assert [name for name, _, _ in tables] == ["nav_metrics.csv", "price_only_grid_nav.csv", "bear_markets.csv"]
    assert all(len(row) == len(header) for _, header, rows in tables for row in rows)


def test_extended_nav_algorithm_modules_do_not_import_io_modules() -> None:
    """依赖方向：缓冲带均线与补充历史净值的纯计算模块不得导入读写模块、services 或 CLI（含传递依赖）。"""
    code = ("import importlib, json, sys\n"
            "for name in ('buffered_ma', 'extended_nav', 'extended_nav_report'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.extended_history_v14_run',"
            " 'market_risk.wavewarn.validation_output', 'market_risk.wavewarn.validation_run',"
            " 'market_risk.wavewarn.diagnostics_round2_run', 'market_risk.wavewarn.extended_nav_run',"
            " 'market_risk.wavewarn.lock_guard', 'market_risk.services', 'market_risk.cli')"
            " or m.startswith('market_risk.scoring')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
