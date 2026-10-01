"""v1.2.1 开发期输入：严格流式截断，不载入验证期或保留期数值。"""

from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal
from pathlib import Path

from market_risk.calendar import stock_trading_days
from market_risk.precision import published_price
from market_risk.wavewarn.data import DEVELOPMENT_END
from market_risk.wavewarn.input_model import DevelopmentInputs


def development_series(path: Path, field: str, end: dt.date = DEVELOPMENT_END,
                       price: bool = False) -> dict[dt.date, Decimal]:
    """按日期递增文件读取；遇到第一条超出截止日的记录即停，未解析其数据值。"""
    if end > DEVELOPMENT_END:
        raise ValueError("研究输入截止日不得超出开发期")
    return series_until(path, field, end, price, DEVELOPMENT_END)


def series_until(path: Path, field: str, end: dt.date, price: bool, limit: dt.date) -> dict[dt.date, Decimal]:
    """含截止日之后行的有序文件：遇到第一条晚于 end 的行即停止，不解析该行及其后的数值。

    end 晚于 limit 时在入口拒绝；limit 由调用方给出（开发期 2016-12-30，验证期 2022-12-30）。
    """
    if end > limit:
        raise ValueError(f"研究输入截止日 {end} 晚于允许的上限 {limit}")
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


VALIDATION_END = dt.date(2022, 12, 30)                 # 验证期最后一个交易日；晚于此日的读取一律拒绝
VIX3M_VALIDATION_FILE = "data/research/wavewarn/v14/vix3m_cboe_validation.csv"


def input_files(root: Path, vix3m_file: str) -> dict[str, tuple[Path, str, bool]]:
    """v1.4 与各对照行所需的全部输入：文件、取值列、是否按发布价格精度处理。"""
    return {
        "SPX": (root / "data/market/daily/SPX.csv", "value", True),
        "QQQ": (root / "data/market/daily/QQQ.csv", "value", True),
        "S5TW": (root / "data/market/daily/S5TW.csv", "value", False),
        "NDTW": (root / "data/processed/tradingview/NDTW.csv", "close", False),
        "VIX": (root / "data/market/daily/VIX_CBOE.csv", "value", False),
        "VIX3M": (root / vix3m_file, "close", False),
    }


def load_inputs_until(root: Path, end: dt.date, vix3m_file: str) -> DevelopmentInputs:
    """截至 end（不晚于 2022-12-30）的全部输入；每个文件都在第一条晚于 end 的行之前停止读取。

    交易日轴自 SPX 的第一个交易日起至 end。VIX3M 用 vix3m_file 指定的 Cboe 官方副本。
    """
    if end > VALIDATION_END:
        raise ValueError(f"研究输入截止日 {end} 晚于验证期末 {VALIDATION_END}，拒绝读取")
    series = {name: series_until(path, field, end, price, VALIDATION_END)
              for name, (path, field, price) in input_files(root, vix3m_file).items()}
    return DevelopmentInputs(tuple(stock_trading_days(min(series["SPX"]), end)), series)
