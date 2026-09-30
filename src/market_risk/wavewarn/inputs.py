"""v1.2.1 开发期输入：严格流式截断，不载入验证期或保留期数值。"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.calendar import stock_trading_days
from market_risk.precision import published_price
from market_risk.wavewarn.data import DEVELOPMENT_END


@dataclass(frozen=True)
class DevelopmentInputs:
    days: tuple[dt.date, ...]
    series: Mapping[str, Mapping[dt.date, Decimal]]


def development_series(path: Path, field: str, end: dt.date = DEVELOPMENT_END,
                       price: bool = False) -> dict[dt.date, Decimal]:
    """按日期递增文件读取；遇到第一条超出截止日的记录即停，未解析其数据值。"""
    if end > DEVELOPMENT_END:
        raise ValueError("研究输入截止日不得超出开发期")
    result: dict[dt.date, Decimal] = {}
    previous: dt.date | None = None
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or "date" not in reader.fieldnames or field not in reader.fieldnames:
            raise ValueError(f"研究输入表头缺少 date 或 {field}：{path}")
        for row in reader:
            day = dt.date.fromisoformat(row["date"])
            if previous is not None and day <= previous:
                raise ValueError(f"研究输入日期未严格升序：{path}")
            if day > end:
                break
            value = row[field]
            if value:
                result[day] = published_price(value) if price else Decimal(value)
            previous = day
    return result


def load_development_inputs(root: Path, end: dt.date) -> DevelopmentInputs:
    """读取 SPX、QQQ、S5TW、NDTW 与 Cboe 官方 VIX/VIX3M 的开发期副本。"""
    paths = {
        "SPX": (root / "data/market/daily/SPX.csv", "value", True),
        "QQQ": (root / "data/market/daily/QQQ.csv", "value", True),
        "S5TW": (root / "data/market/daily/S5TW.csv", "value", False),
        "NDTW": (root / "data/processed/tradingview/NDTW.csv", "close", False),
        "VIX": (root / "data/market/daily/VIX_CBOE.csv", "value", False),
        "VIX3M": (root / "data/research/wavewarn/v121/vix3m_cboe_development.csv", "close", False),
    }
    if end != DEVELOPMENT_END:
        raise ValueError("开发期截止日与 v1.2.1 规格不一致")
    series = {name: development_series(path, field, end=end, price=price)
              for name, (path, field, price) in paths.items()}
    first = min(series["SPX"])
    return DevelopmentInputs(tuple(stock_trading_days(first, end)), series)
