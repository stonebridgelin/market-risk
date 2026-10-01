"""v1.4 验证期工程的测试：输入读取、防未来信息与开发期一致性。

真实数据只读到 2016-12-30；验证期（2017 年起）的真实数据不在任何测试中读取或运行。
"""

from __future__ import annotations

import datetime as dt
import gzip
from pathlib import Path

import pytest

from market_risk.wavewarn.config_v14 import load_v14_config
from market_risk.wavewarn.data_v14 import development_prefix_matches, parse_cboe_until, subset_text
from market_risk.wavewarn.evaluation import period_labels
from market_risk.wavewarn.evaluation_v14_run import write_v14_evaluation
from market_risk.wavewarn.inputs import (
    VALIDATION_END,
    VIX3M_VALIDATION_FILE,
    load_development_inputs,
    load_inputs_until,
    series_until,
)
from market_risk.wavewarn.v14_model import prepare_v14

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
