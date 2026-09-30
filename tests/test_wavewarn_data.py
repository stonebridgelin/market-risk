"""Cboe VIX3M 开发期截断读取的构造边界。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from market_risk.wavewarn.data import parse_cboe_development


def test_cboe_vix3m_stops_before_validation_values_and_never_reads_holdout() -> None:
    requested: list[int] = []
    lines = (b"DATE,OPEN,HIGH,LOW,CLOSE\n",
             b"09/18/2009,25,27,25,26.540000\n",
             b"12/30/2016,15,17,15,16.000000\n",
             b"01/03/2017,18,19,17,BAD\n",
             b"01/03/2023,1,1,1,SECRET\n")

    def source() -> Iterator[bytes]:
        for index, line in enumerate(lines):
            requested.append(index)
            if index == 4:
                raise AssertionError("不得读取保留期行")
            yield line

    subset = parse_cboe_development(source())
    # 只保留2009-09-18和2016-12-30的官方收盘文本，不解析2017行的BAD，更不请求2023行。
    assert subset.rows == ((dt.date(2009, 9, 18), "26.540000"),
                           (dt.date(2016, 12, 30), "16.000000"))
    assert requested == [0, 1, 2, 3]
