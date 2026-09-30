"""开发期流式读取不得载入后续时期的数值。"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.wavewarn.inputs import development_series, load_development_inputs


def test_development_series_stops_before_validation_and_holdout_values(tmp_path: Path) -> None:
    path = tmp_path / "series.csv"
    path.write_text("date,value\n2016-12-29,100.005\n2016-12-30,101.004\n"
                    "2017-01-03,INVALID_VALIDATION_VALUE\n2023-01-03,INVALID_HOLDOUT_VALUE\n",
                    encoding="utf-8")
    # 只读至2016-12-30；下一行只判日期，后续字段不作 Decimal 转换，也不读保留期行。
    assert development_series(path, "value", price=True) == {
        dt.date(2016, 12, 29): Decimal("100.01"), dt.date(2016, 12, 30): Decimal("101.00")}


def test_development_loader_rejects_validation_end_before_opening_any_series(tmp_path: Path) -> None:
    # 根目录没有任何 CSV；若截止日被改成验证期，必须先拒绝，不能先打开或载入数据。
    with pytest.raises(ValueError, match="开发期截止日"):
        load_development_inputs(tmp_path, dt.date(2022, 12, 30))
    with pytest.raises(ValueError, match="不得超出开发期"):
        development_series(tmp_path / "不存在.csv", "value", end=dt.date(2017, 1, 3))
