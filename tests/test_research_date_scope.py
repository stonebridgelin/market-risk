"""研究读取边界仅用构造 CSV；不读取真实验证期或保留期数据。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from market_risk.research.io import _label_rows, _rows, _values
from market_risk.wavewarn.inputs import development_series


def test_ordered_reader_stops_before_parsing_out_of_scope_values(tmp_path) -> None:
    path = tmp_path / "series.csv"
    path.write_text("date,value\n2022-12-29,1.25\n2022-12-30,1.50\n"
                    "2023-01-03,THIS_IS_NOT_DECIMAL\nmalformed,ALSO_BAD\n", encoding="utf-8")
    # 截止前两个数由字符串精确读为 Decimal；首个 2023 日期仅用于停读，
    # 其坏数值和后续坏日期都不能进入解析路径。
    assert _values(path, end=dt.date(2022, 12, 30)) == {
        dt.date(2022, 12, 29): Decimal("1.25"), dt.date(2022, 12, 30): Decimal("1.50")}


def test_ordered_reader_rejects_disorder_before_cutoff(tmp_path) -> None:
    path = tmp_path / "series.csv"
    path.write_text("date,value\n2022-12-30,1\n2022-12-29,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="日期乱序"):
        list(_rows(path, end=dt.date(2022, 12, 30)))


def test_grouped_label_checks_all_date_columns_without_global_sort(tmp_path) -> None:
    path = tmp_path / "pullback_episodes.csv"
    path.write_text("high_date,low_date,confirm_date,recovery_date\n"
                    "2022-05-01,2022-06-01,,\n2020-01-01,2020-02-01,,\n", encoding="utf-8")
    # 多资产／层级分组可使高点日回退，标签读取仍须保留两行。
    assert len(list(_label_rows(path, ("high_date", "low_date", "confirm_date", "recovery_date")))) == 2
    path.write_text("high_date,low_date,confirm_date,recovery_date\n"
                    "2022-05-01,,2023-01-03,\n", encoding="utf-8")
    # 低点被屏蔽为空也不能容许另一日期列泄漏到保留期。
    with pytest.raises(ValueError, match=r"标签文件含保留期日期.*confirm_date"):
        list(_label_rows(path, ("high_date", "low_date", "confirm_date", "recovery_date")))


def test_outcome_screened_window_and_wavewarn_input_boundaries(tmp_path) -> None:
    path = tmp_path / "outcomes.csv"
    path.write_text("base_date,window_start,window_end,event_date\n"
                    "2022-12-01,2022-12-02,2023-01-03,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="window_end"):
        list(_label_rows(path, ("base_date", "window_start", "window_end", "event_date")))
    wavewarn = tmp_path / "wavewarn.csv"
    wavewarn.write_text("date,value\n2016-12-30,100\n2017-01-03,NOT_A_PRICE\n"
                        "2023-01-03,ALSO_NOT_A_PRICE\n", encoding="utf-8")
    assert development_series(wavewarn, "value", dt.date(2016, 12, 30)) == {
        dt.date(2016, 12, 30): Decimal(100)}
