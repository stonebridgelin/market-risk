"""数据集绑定与单资产读取结果的纯类型（阶段三实施指令第二节第 1 部分）。只有类型，没有任何文件操作。

行情读取（data_v20）与配置读取（config_v20）两个边界模块互不导入，二者共用的类型放在这里。
这里的 AssetSeries 是历史研究的读取结果：不含、也不得伪造首次取得时间，与留痕类型（provenance_v20）之间没有转换路径。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType


@dataclass(frozen=True)
class RegisteredFile:
    """登记第零节绑定的一份行情文件（实施口径补充第 4 条：两种哈希都核对）。"""

    asset: str
    raw_sha256: str              # 仓库文件原始字节的 SHA-256
    normalized_sha256: str       # 把 CRLF 换为 LF 之后的 SHA-256
    data_rows: int               # 不含表头的非空数据行数
    first_date: dt.date
    last_date: dt.date


@dataclass(frozen=True)
class DecisionEntry:
    """已裁定日期表中的一条：保留全部裁定类型，筛选由行情读取完成。"""

    symbol: str
    date: dt.date
    decision: str                        # exclude、keep、invalid、correct
    corrected_value: Decimal | None      # 只有 correct 有
    decided_on: dt.date                  # 裁定日期；不是取得时间，也不用来限制取得时间


@dataclass(frozen=True)
class FileMetadata:
    """文件级元数据：只由原始字节、表头与首末数据行的日期字段得到，不解析价格列与来源列。"""

    raw_sha256: str
    normalized_sha256: str
    data_rows: int               # 不含表头的非空数据行数
    total_rows: int              # 含表头的非空总行数（只报告）
    first_date: dt.date
    last_date: dt.date


@dataclass(frozen=True)
class AssetSeries:
    """单资产截至截止日的收盘价（历史研究的读取结果）。"""

    asset: str
    first_date: dt.date                       # 登记首日
    cutoff: dt.date                           # 截止日
    closes: Mapping[dt.date, Decimal]         # 按日收盘价；构造时复制并包成只读映射
    raw_sha256: str                           # 来源哈希：原始字节的 SHA-256

    def __post_init__(self) -> None:
        object.__setattr__(self, "closes", MappingProxyType(dict(self.closes)))
