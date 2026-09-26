"""测试公共夹具。"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from market_risk.calendar import BondCalendar, bond_calendar_from_dates

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOP_PATH = PROJECT_ROOT / "docs" / "SOP.md"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def parse_sop_appendix_a(sop_path: Path = SOP_PATH) -> dict[dt.date, float]:
    """从 docs/SOP.md 附录A 解析财政部10年期收益率（2025-08-01 至 2025-12-31）。

    附录A 中出现的日期即为该期间的债市营业日（缺失日期为债市休市日）。
    """
    text = sop_path.read_text(encoding="utf-8")
    start = text.index("## 附录A")
    end = text.index("## 附录B")
    section = text[start:end]
    result: dict[dt.date, float] = {}
    for line in section.splitlines():
        if not re.match(r"^\d{2}/\d{2} ", line):
            continue
        for item in line.split("｜"):
            mm_dd, value = item.split()
            month, day = (int(x) for x in mm_dd.split("/"))
            result[dt.date(2025, month, day)] = float(value)
    return result


@pytest.fixture(scope="session")
def appendix_a_yields() -> dict[dt.date, float]:
    return parse_sop_appendix_a()


@pytest.fixture(scope="session")
def bond_cal_2025h2(appendix_a_yields: dict[dt.date, float]) -> BondCalendar:
    """2025-08-01 至 2025-12-31 的债市日历（来自 SOP 附录A，不依赖网络）。"""
    return bond_calendar_from_dates(
        appendix_a_yields.keys(), dt.date(2025, 8, 1), dt.date(2025, 12, 31)
    )
