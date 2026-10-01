"""v1.4 验证期工程的测试：输入读取、防未来信息与开发期一致性。

真实数据只读到 2016-12-30；验证期（2017 年起）的真实数据不在任何测试中读取或运行。
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import gzip
import random
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn.config_v14 import load_v14_config, load_validation_config
from market_risk.wavewarn.data_v14 import development_prefix_matches, parse_cboe_until, subset_text
from market_risk.wavewarn.evaluation import PreparedEvaluation, evaluate_candidate, period_labels
from market_risk.wavewarn.evaluation_v14_run import write_v14_evaluation
from market_risk.wavewarn.extended_history import truncate_inputs
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.inputs import (
    VALIDATION_END,
    VIX3M_VALIDATION_FILE,
    load_development_inputs,
    load_inputs_until,
    series_until,
)
from market_risk.wavewarn.main_test import NOT_SUPPORTED, SUPPORTED, conclusion
from market_risk.wavewarn.v14_model import prepare_v14
from market_risk.wavewarn.validation_flow import (
    WindowEvaluation,
    evaluate_window,
    locked_states,
    validation_window,
    window_prepared,
)
from market_risk.wavewarn.validation_output import REPORT_NAME, write_window_outputs
from market_risk.wavewarn.validation_report import LockInfo

ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_END = dt.date(2016, 12, 30)
STORED = ROOT / "reports/research/wavewarn_v14/evaluation_development"


def test_series_reader_stops_before_first_row_after_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "series.csv"
    # 截止日 2022-12-30 之后的第一行，其数值一旦被解析就会抛错（不是数字）；再往后的行连日期都不合法。
    path.write_text("date,value\n2022-12-29,10.5\n2022-12-30,11\n2023-01-03,一旦解析就抛错\n不是日期,x\n",
                    encoding="utf-8")
    values = series_until(path, "value", dt.date(2022, 12, 30), False, VALIDATION_END)
    assert list(values) == [dt.date(2022, 12, 29), dt.date(2022, 12, 30)]
    # 截止日取在中途：2022-12-30 一行同样不被解析。
    assert list(series_until(path, "value", dt.date(2022, 12, 29), False, VALIDATION_END)) == [
        dt.date(2022, 12, 29)]
    # 截止日晚于允许的上限时在入口拒绝，文件不会被打开。
    with pytest.raises(ValueError, match="晚于"):
        series_until(tmp_path / "不存在.csv", "value", dt.date(2023, 1, 3), False, VALIDATION_END)
    with pytest.raises(ValueError, match="晚于验证期末"):
        load_inputs_until(tmp_path, dt.date(2023, 1, 3), VIX3M_VALIDATION_FILE)
    # 截止日之前的行若不可解析，照常报错，不被跳过。
    bad = tmp_path / "bad.csv"
    bad.write_text("date,value\n2022-12-29,坏值\n", encoding="utf-8")
    with pytest.raises(ArithmeticError):
        series_until(bad, "value", dt.date(2022, 12, 30), False, VALIDATION_END)


def test_cboe_parser_stops_at_cutoff_and_checks_development_prefix() -> None:
    lines = [b"DATE,OPEN,HIGH,LOW,CLOSE\n", b"09/18/2009,1,1,1,25.59\n", b"12/30/2016,1,1,1,16.78\n",
             b"01/03/2017,1,1,1,16.11\n", b"12/30/2022,1,1,1,23.57\n",
             b"01/03/2023,1,1,1,not-a-number\n", b"garbage\n"]
    subset = parse_cboe_until(iter(lines), VALIDATION_END)
    # 截止日之后的首行只读到日期即停止：其收盘值不是数字也不会报错，之后的行不再读取。
    assert [day for day, _ in subset.rows] == [dt.date(2009, 9, 18), dt.date(2016, 12, 30),
                                               dt.date(2017, 1, 3), dt.date(2022, 12, 30)]
    assert subset_text(subset).splitlines()[-1] == "2022-12-30,23.57"
    # 2016-12-30 及以前的部分须与开发期副本逐行相同。
    assert development_prefix_matches(subset, "date,close\n2009-09-18,25.59\n2016-12-30,16.78\n")
    assert not development_prefix_matches(subset, "date,close\n2009-09-18,25.59\n2016-12-30,16.79\n")
    with pytest.raises(ValueError, match="表头"):
        parse_cboe_until(iter([b"DATE,CLOSE\n"]), VALIDATION_END)


def test_validation_copy_truncated_to_development_end_equals_development_inputs() -> None:
    old = load_development_inputs(ROOT, DEVELOPMENT_END)
    new = load_inputs_until(ROOT, DEVELOPMENT_END, VIX3M_VALIDATION_FILE)
    # 新入口以 2016-12-30 为截止日读取（VIX3M 来自验证期副本，在其后的首行之前停止），与开发期入口完全相同。
    assert new.days == old.days and new.days[-1] == DEVELOPMENT_END
    assert {name: dict(values) for name, values in new.series.items()} == {
        name: dict(values) for name, values in old.series.items()}
    assert all(max(values) <= DEVELOPMENT_END for values in new.series.values())
    # 标签截止日须等于输入的最后一个交易日，不能用更晚的价格生成标签。
    config = load_v14_config(ROOT / "config/wavewarn_v14.yaml")
    with pytest.raises(ValueError, match="标签截止日"):
        period_labels(config.base, new, VALIDATION_END)


def _normalised(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def test_development_outputs_are_reproduced_through_validation_entry(tmp_path: Path) -> None:
    """防未来信息 (b)：经新入口把输入截断到 2016-12-30 重跑，状态、灯色、损失与入库的开发期输出逐字节相同。"""
    config = load_v14_config(ROOT / "config/wavewarn_v14.yaml")
    inputs = load_inputs_until(ROOT, DEVELOPMENT_END, VIX3M_VALIDATION_FILE)
    destination = tmp_path / "result"
    write_v14_evaluation(prepare_v14(config, inputs), config, destination)
    # 逐日明细（选定设定与四条参照行的灯色、执行、各项损失）：与入库的 gzip 解压后逐字节相同。
    stored_daily = gzip.decompress((STORED / "daily_selected.csv.gz").read_bytes())
    assert (destination / "daily_selected.csv").read_bytes() == stored_daily
    # 全部 21 组设定的汇总、选择追踪、参照行、收敛日、缺值审计、事件账与警报账、报告：逐字节相同（换行统一后比较）。
    names = ("settings_summary.csv", "selection_trace.csv", "reference_rows.csv", "convergence.csv",
             "missing_audit.csv", "event_scope_counts.csv", "event_ledger_selected.csv",
             "alert_ledger_selected.csv", "yearly_alert_selected.csv", "开发期评价报告.md")
    different = [name for name in names
                 if _normalised((destination / name).read_bytes()) != _normalised((STORED / name).read_bytes())]
    assert different == []

# ---------------------------------------------------------------------------
# 评价流程：只用构造数据（随机游走），不读取任何真实的验证期数据
# ---------------------------------------------------------------------------

def _synthetic_inputs() -> DevelopmentInputs:
    """2015-01-02 至 2022-12-30 的构造序列：价格为随机游走，广度与波动率指数为有界随机序列。"""
    days = tuple(stock_trading_days(dt.date(2015, 1, 2), VALIDATION_END))
    rng = random.Random(20261001)
    series: dict[str, dict[dt.date, Decimal]] = {name: {} for name in ("SPX", "QQQ", "S5TW", "NDTW", "VIX", "VIX3M")}
    levels = {"SPX": 2000.0, "QQQ": 100.0}
    breadth = {"S5TW": 60.0, "NDTW": 55.0}
    for day in days:
        shock = rng.gauss(0.0002, 0.009)
        for name, beta in (("SPX", 1.0), ("QQQ", 1.2)):
            levels[name] *= 1 + beta * shock + rng.gauss(0, 0.003)
            series[name][day] = Decimal(f"{levels[name]:.2f}")
        for name in breadth:
            breadth[name] = min(95.0, max(5.0, breadth[name] + 400 * shock + rng.gauss(0, 3)))
            series[name][day] = Decimal(f"{breadth[name]:.2f}")
        vix = max(10.0, 18 - 600 * shock + rng.gauss(0, 1.5))
        series["VIX"][day] = Decimal(f"{vix:.2f}")
        series["VIX3M"][day] = Decimal(f"{vix * rng.uniform(0.95, 1.2):.2f}")
    return DevelopmentInputs(days, series)


VALIDATION_CONFIG = load_validation_config(ROOT / "config/wavewarn_v14_validation.yaml")
QUICK = dataclasses.replace(VALIDATION_CONFIG.model.base.paired_parameters(), resamples=40)   # 构造测试用少量重抽样
LOCK = LockInfo("构造测试", "0" * 64, "0000000", False)


@pytest.fixture(scope="module")
def synthetic_run() -> tuple[DevelopmentInputs, PreparedEvaluation, WindowEvaluation]:
    inputs = _synthetic_inputs()
    base = prepare_v14(VALIDATION_CONFIG.model, inputs)
    window = validation_window(base, VALIDATION_CONFIG, QUICK)
    return inputs, base, evaluate_window(base, window, VALIDATION_CONFIG, QUICK)


def test_validation_window_mechanics_on_synthetic_data(
        synthetic_run: tuple[DevelopmentInputs, PreparedEvaluation, WindowEvaluation]) -> None:
    inputs, base, result = synthetic_run
    start = dt.date(2017, 1, 3)
    axis = tuple(day for day in inputs.days if day >= start)
    # 模型自 t0 连续运行（t0、τ、j₀ 都早于 2017 年）；计入区间起点自 2017-01-03 起，最后一个区间止于 2022-12-30。
    assert base.t0 < base.tau <= base.first_loss_day < start == result.prepared.first_loss_day
    assert result.locked.days == axis and [row.date for row in result.differences] == list(axis[:-1])
    # 锁定设定就是 K=5、θ_P=2.5% 的 v1.4；状态序列自 t0 起，没有在窗口起点重新开始。
    candidate = result.locked_states.candidate
    assert (candidate.model, candidate.k, candidate.theta_p) == ("V4", 5, Decimal("0.025"))
    assert result.locked_states.rows[0].date == base.t0
    # 回撤参考高点在窗口第一天重置：第一个区间的回撤纪录增量只取决于该区间自身（不会带入此前的回撤）。
    first = result.locked.asset_losses["SPX"][0]
    closes = inputs.series["SPX"]
    expected = max((Decimal("0.98") * closes[axis[0]] / closes[axis[1]]).ln(), Decimal(0))
    assert first.start == axis[0] and abs(first.drawdown_increment - expected) < Decimal("1e-20")
    # ē 按窗口内的计入区间计算：等于窗口内执行暴露（绿 1、黄 0.5、红 0）的平均。
    exposure = {"绿": Decimal(1), "黄": Decimal("0.5"), "红": Decimal(0)}
    lights = result.locked.system_executed[:-1]
    assert result.main_test.mean_model == sum((exposure[light] for light in lights), Decimal(0)) / len(lights)
    # 事件按 P ≥ 2017-01-03 归属：退出代价与转绿延迟的纳入事件、大跌事件的高点都不早于窗口起点。
    locked_row = next(row for row in result.rows if row.candidate == candidate)
    for symbol in ("SPX", "QQQ"):
        confirmed = [event for event in result.events[symbol] if event.peak_date >= start and not event.right_censored]
        assert locked_row.delays[symbol].included == len(confirmed)
    assert all(drop.event.peak_date >= start and drop.decline >= Decimal("0.15") for drop in result.big_drops)


def test_main_test_identities_on_synthetic_data(
        synthetic_run: tuple[DevelopmentInputs, PreparedEvaluation, WindowEvaluation]) -> None:
    _, _, result = synthetic_run
    test = result.main_test
    close = lambda a, b: abs(a - b) < Decimal("1e-18")  # noqa: E731
    locked_row = next(row for row in result.rows if row.candidate == result.locked_states.candidate)
    # Σd_j = T_V4 − T_MA；按损失分项的分解之和等于 Σd_j。
    assert close(test.main.total_difference, locked_row.timing.score - result.references[3].timing.score)
    assert close(sum(test.components.values(), Decimal(0)), test.main.total_difference)
    assert all(close(sum(row.components.values(), Decimal(0)), row.total) for row in result.differences[:50])
    # 同一条 d_j 序列：三种区块的统计量相同（只有重抽样不同）；前后两半的统计量之和等于全期，区间数之和等于全期。
    assert test.short_block.total_difference == test.long_block.total_difference == test.main.total_difference
    assert test.first_half is not None and test.second_half is not None and test.split == dt.date(2020, 1, 1)
    assert close(test.first_half.total_difference + test.second_half.total_difference, test.main.total_difference)
    assert test.first_half.sample_count + test.second_half.sample_count == test.main.sample_count
    assert (test.main.block_length, test.short_block.block_length, test.long_block.block_length) == (20, 10, 40)
    assert (test.main.seed, test.short_block.seed, test.long_block.seed) == (20260929, 20260910, 20260940)
    # 逐事件剔除：只含与窗口相交的合并事件；置零后的统计量 = 原统计量 − 被置零区间内的 d_j 之和。
    days = [row.date for row in result.differences]
    assert test.leave_one and all(item.trough_date >= days[0] for item in test.leave_one)
    item = test.leave_one[0]
    low = max(0, bisect.bisect_left(days, item.peak_date) - 20)
    high = min(len(days), bisect.bisect_right(days, item.trough_date) + 20)
    removed = sum((row.total for row in result.differences[low:high]), Decimal(0))
    assert close(item.total, test.main.total_difference - removed)
    # 置零日期按“当日有资产区间被排除”统计；超过 1% 才做删除敏感性。
    assert test.zero_dates == sum(row.zeroed for row in result.differences)
    assert (test.without_zero is not None) == (test.zero_dates * 100 > len(result.differences))
    # 措辞规则只取决于主设定的 p。
    assert conclusion(test, Decimal("0.05")) == (SUPPORTED if test.main.p_value < Decimal("0.05") else NOT_SUPPORTED)


def test_truncation_keeps_states_and_losses_up_to_cutoff(
        synthetic_run: tuple[DevelopmentInputs, PreparedEvaluation, WindowEvaluation]) -> None:
    """防未来信息 (c)，构造数据：把输入截断到验证期中途某日，该日及之前的状态不变；标签相同时损失也不变。"""
    inputs, base, result = synthetic_run
    cutoff = dt.date(2019, 6, 28)
    truncated = truncate_inputs(inputs, cutoff)
    again = prepare_v14(VALIDATION_CONFIG.model, truncated)
    # t0、τ、j₀ 与各设定的收敛日不变；每个设定截至截断日的灯色、数据状态、激活通道与原因逐日相同。
    assert (again.t0, again.tau, again.first_loss_day) == (base.t0, base.tau, base.first_loss_day)
    for short, full in zip(again.states, base.states, strict=True):
        assert short.candidate == full.candidate and short.convergence_date == full.convergence_date
        assert short.rows == full.rows[:len(short.rows)] and short.rows[-1].date == cutoff
    # 损失：ZZ 标签按设计是事后标签，截断后尾段的标签会变；这里固定使用全期标签，只检验损失计算本身不看未来。
    window = dataclasses.replace(result.window, rule=result.window.rule)
    prepared = window_prepared(again, window)
    chosen = locked_states(prepared, VALIDATION_CONFIG)
    short_run = evaluate_candidate(prepared, chosen, result.events, result.unknown)
    count = len(short_run.days) - 1
    assert short_run.days == result.locked.days[:count + 1] and short_run.days[-1] == cutoff
    assert short_run.daily_losses[:count] == result.locked.daily_losses[:count]
    assert all(short_run.asset_losses[symbol] == result.locked.asset_losses[symbol][:count]
               for symbol in ("SPX", "QQQ"))
    assert short_run.system_executed == result.locked.system_executed[:count + 1]


def test_window_outputs_and_report_are_written(
        tmp_path: Path, synthetic_run: tuple[DevelopmentInputs, PreparedEvaluation, WindowEvaluation]) -> None:
    _, _, result = synthetic_run
    write_window_outputs(tmp_path, result, LOCK, VALIDATION_CONFIG, QUICK)
    names = {path.name for path in tmp_path.iterdir()}
    assert {"zz_events.csv", "zz_merged.csv", "zz_unknown.csv", "settings_summary.csv", "reference_rows.csv",
            "main_test.csv", "paired_differences.csv", "leave_one_event.csv", "big_drop_events.csv",
            "daily_selected.csv", "event_ledger_selected.csv", "alert_ledger_selected.csv", REPORT_NAME} <= names
    report = (tmp_path / REPORT_NAME).read_text(encoding="utf-8")
    # 四部分与开头的锁定记录、披露、措辞规则都在；结论句只能是登记的两句之一。
    for text in ("## 一、主检验", "## 二、损失与择时", "## 三、事件账与警报账", "## 四、大跌事件", "措辞规则",
                 "披露", LOCK.sha256):
        assert text in report
    assert (SUPPORTED in report.split("## 一、主检验")[1]) != (
        f"按措辞规则：{NOT_SUPPORTED}" in report)
    # 逐日差序列逐行写出，行数等于计入区间数；汇总含全部 21 组运行对象。
    assert len((tmp_path / "paired_differences.csv").read_text(encoding="utf-8").splitlines()) == len(
        result.differences) + 1
    assert len((tmp_path / "settings_summary.csv").read_text(encoding="utf-8").splitlines()) == 22
