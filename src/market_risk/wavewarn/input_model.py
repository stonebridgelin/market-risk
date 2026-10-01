"""开发期输入的不可变传递结构，不依赖文件读取。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class DevelopmentInputs:
    days: tuple[dt.date, ...]
    series: Mapping[str, Mapping[dt.date, Decimal]]
