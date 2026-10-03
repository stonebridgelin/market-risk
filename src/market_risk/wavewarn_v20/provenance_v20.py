"""留痕字段契约（阶段三实施指令第四节）：类型、字段校验与按时判断。纯计算，没有任何文件操作。

本阶段只实现类型、字段校验与按时判断；不实现任何转换函数。类型测试只验收字段契约。
- PriceObservation：一条价格观测的留痕，可以保存历史补录，包括无法证明当时首次取得时间的观测。
- SignalInputSnapshot：有取得证据支持的信号输入，只接受对该信号日能确认按时的观测。
- 历史研究的读取结果（AssetSeries、Snapshot）不含、也不得伪造首次取得时间；本包里没有从它们到留痕类型的转换路径。
- 本阶段不授权人工修正值进入前瞻信号；批准生效时点与版本证据的接入以后另行设计、另行验收。

舍入规则不复制：收盘价按 market_risk.precision.published_price 读入（算法模块依赖规则的唯一具名例外）。
出处：登记第一节第 6 小节、第八节第 3 小节；《数据留痕设计说明》第 3、4 节。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from zoneinfo import ZoneInfo

from market_risk.precision import published_price

# 模块常量均为不可变结构；权威依据见补充裁决第二节第 9 条（与配置、行情读取模块的一致性另有测试，
# 相等只证明一致，不证明正确）。
NEW_YORK = ZoneInfo("America/New_York")
# 截止时刻：信号日美东 18:30:00，恰为这一时刻算按时（登记第一节第 6 小节、第八节第 3 小节；
# 《数据留痕设计说明》第 4 节第 5 条；阶段三定稿第四节第 3 部分）。
CUTOFF = dt.time(18, 30)
ASSETS = ("SPX", "QQQ")
# 登记数据覆盖首日（登记第零节，数据集版本绑定）。
REGISTERED_FIRST_DAY: Mapping[str, dt.date] = MappingProxyType({"SPX": dt.date(1990, 1, 2),
                                                                "QQQ": dt.date(1999, 3, 10)})
# 允许的来源（实施口径补充第 5 条）。
SOURCES = frozenset({"yahoo", "correct:yahoo"})
# 以 fullmatch 使用，只用 ASCII 字符类（补充裁决正则排查）：match 配合 $ 会接受末尾的换行符，
# Unicode 数字类会接受非 ASCII 数字。
_RAW_VALUE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ProvenanceError(ValueError):
    """留痕记录或快照不符合字段契约。"""


class Capture(Enum):
    LIVE = "实时留痕"
    BACKFILL = "历史补录"


class Timeliness(Enum):
    ON_TIME = "按时"
    LATE = "晚于截止时间"
    UNPROVEN = "无法证明按时"       # 首次取得时间为空；不得写成已确认迟到


def _aware(name: str, value: object) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProvenanceError(f"{name} 必须是带时区的时间")
    return value


@dataclass(frozen=True)
class PriceObservation:
    """一条价格观测的留痕。

    first_obtained_at_et 是“当时首次取得时间”：这条记录的确切数值及其版本（raw_value、source、source_sha256）
    最早被取得的时刻，构造时换算到 America/New_York（含夏令时）。无法证明时留空，
    不得用本次取得时间、录入时间、交易日或裁定日期代替。
    修订值或人工修正值是新的版本，须有自己的首次取得时间；不得无证据沿用旧值的取得时间，无法证明时留空。
    取得一个正确数值与批准采用它是两件不同的事：正确数值可以在裁定之前已经取得。

    局限：单条记录的字段校验不能证明调用方填写的时间确实属于该版本。版本证据的关联与修订链尚未实现，
    本类型不声称已机械保证这一点。
    """

    asset: str
    trade_day: dt.date
    raw_value: str                               # 十进制数字原文，不带符号与单位
    close: Decimal                               # 等于 published_price(raw_value)
    source: str
    retrieved_at: dt.datetime                    # 本次取得时间，带时区
    first_obtained_at_et: dt.datetime | None     # 当时首次取得时间（美东）；可为空
    entered_at_utc: dt.datetime                  # 录入时间，偏移 +00:00
    capture: Capture
    source_sha256: str                           # 本次取得的来源原始字节的 SHA-256（64 位小写十六进制）

    def __post_init__(self) -> None:
        if self.asset not in ASSETS:
            raise ProvenanceError(f"资产只能是 {ASSETS}：{self.asset!r}")
        if not isinstance(self.trade_day, dt.date) or isinstance(self.trade_day, dt.datetime):
            raise ProvenanceError("trade_day 必须是日期")
        if self.trade_day < REGISTERED_FIRST_DAY[self.asset]:
            first_day = REGISTERED_FIRST_DAY[self.asset]
            raise ProvenanceError(f"{self.asset} 的交易日 {self.trade_day} 早于登记首日 {first_day}")
        if not isinstance(self.raw_value, str) or _RAW_VALUE.fullmatch(self.raw_value) is None:
            raise ProvenanceError(f"raw_value 须是不带符号与单位的十进制数字原文：{self.raw_value!r}")
        if not isinstance(self.close, Decimal) or not self.close.is_finite() or self.close <= 0:
            raise ProvenanceError("close 必须是正的有限 Decimal")
        if self.close != published_price(self.raw_value):
            raise ProvenanceError(f"close {self.close} 不等于 published_price({self.raw_value})")
        if self.source not in SOURCES:
            raise ProvenanceError(f"来源只能是 {sorted(SOURCES)}：{self.source!r}")
        if not isinstance(self.capture, Capture):
            raise ProvenanceError("capture 只能是“实时留痕”或“历史补录”")
        if not isinstance(self.source_sha256, str) or _SHA256.fullmatch(self.source_sha256) is None:
            raise ProvenanceError("source_sha256 须是 64 位小写十六进制")
        retrieved = _aware("retrieved_at", self.retrieved_at)
        entered = _aware("entered_at_utc", self.entered_at_utc)
        if entered.utcoffset() != dt.timedelta(0):
            raise ProvenanceError("entered_at_utc 的偏移必须是 +00:00")
        first = self.first_obtained_at_et
        if first is not None:
            first = _aware("first_obtained_at_et", first).astimezone(NEW_YORK)
            object.__setattr__(self, "first_obtained_at_et", first)
        if self.capture is Capture.LIVE and (first is None or first != retrieved):
            raise ProvenanceError("实时留痕：first_obtained_at_et 必填，且等于 retrieved_at 换算到美东的时间")
        if first is not None and first > retrieved:
            raise ProvenanceError("时间顺序：first_obtained_at_et 不得晚于 retrieved_at")
        if retrieved > entered:
            raise ProvenanceError("时间顺序：retrieved_at 不得晚于 entered_at_utc")
        if first is not None and first < dt.datetime.combine(self.trade_day, dt.time(0, 0), NEW_YORK):
            raise ProvenanceError("first_obtained_at_et 不得早于交易日的美东 00:00")


def validate_trading_days(record: PriceObservation, trading_days: frozenset[dt.date]) -> None:
    """观测的交易日须是 NYSE 交易日；交易日集合由调用方传入。"""
    if record.trade_day not in trading_days:
        raise ProvenanceError(f"{record.asset} 的 {record.trade_day} 不是交易日")


def timeliness(observation: PriceObservation, signal_day: dt.date) -> Timeliness:
    """按时判断：截止时刻按信号日计算（不按观测自己的交易日），为信号日美东 18:30:00，含夏令时。"""
    first = observation.first_obtained_at_et
    if first is None:
        return Timeliness.UNPROVEN
    deadline = dt.datetime.combine(signal_day, CUTOFF, NEW_YORK)
    return Timeliness.ON_TIME if first <= deadline else Timeliness.LATE


@dataclass(frozen=True)
class SignalInputSnapshot:
    """有取得证据支持的信号输入：只接受对该信号日能确认按时的观测。

    observations 为资产 → 按 trade_day 升序的观测元组；构造时复制传入的映射与序列，内部为只读映射，值为元组。
    signal_day 是否为交易日，由调用方用交易日集合校验。不要求快照在 18:30 之前生成。
    """

    signal_day: dt.date
    observations: Mapping[str, Sequence[PriceObservation]]
    built_at_utc: dt.datetime

    def __post_init__(self) -> None:
        built = _aware("built_at_utc", self.built_at_utc)
        if built.utcoffset() != dt.timedelta(0):
            raise ProvenanceError("built_at_utc 的偏移必须是 +00:00")
        if not isinstance(self.observations, Mapping) or set(self.observations) != set(ASSETS):
            raise ProvenanceError(f"observations 的键集合必须恰为 {set(ASSETS)}")
        copied = {asset: tuple(self.observations[asset]) for asset in ASSETS}
        problems: list[str] = []
        for asset, items in copied.items():
            seen: set[dt.date] = set()
            for item in items:
                if not isinstance(item, PriceObservation):
                    raise ProvenanceError("observations 的值必须是 PriceObservation")
                label = f"{asset} 键下 {item.asset} {item.trade_day}"
                if item.asset != asset:
                    problems.append(f"{label}：观测的资产与所在的键不一致")
                if item.trade_day > self.signal_day:
                    problems.append(f"{label}：交易日晚于信号日")
                result = timeliness(item, self.signal_day)
                if result is not Timeliness.ON_TIME:
                    problems.append(f"{label}：{result.value}")
                if item.entered_at_utc > built:
                    problems.append(f"{label}：录入时间晚于快照生成时间")
                if item.trade_day in seen:
                    problems.append(f"{label}：同一资产同一交易日有重复的观测")
                seen.add(item.trade_day)
        if problems:
            raise ProvenanceError("；".join(problems))
        ordered = {asset: tuple(sorted(items, key=lambda item: item.trade_day)) for asset, items in copied.items()}
        object.__setattr__(self, "observations", MappingProxyType(ordered))

    @property
    def same_day_prices_on_time(self) -> bool:
        """“当日两资产价格按时可得”：信号日当日的 SPX、QQQ 观测都存在，且都按时。

        它不表示历史窗口完整、通道有效或整体信号可计算。
        """
        return all(any(item.trade_day == self.signal_day for item in self.observations[asset]) for asset in ASSETS)
