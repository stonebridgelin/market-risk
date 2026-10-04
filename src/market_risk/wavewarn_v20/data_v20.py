"""行情读取的边界模块（阶段三实施指令第二节；登记第零节、第三节；实施口径补充第 1、4、5、17 条）。

文件操作只有一处：对调用方传入的行情文件路径，以二进制只读方式打开一次，读入全部字节后关闭。
哈希、文件级元数据与逐行解析都作用于内存中的同一份字节，读入之后不再访问文件。

- 物理行按 \n 识别，每行只去掉一个行尾 \r；去掉后为空或只含空格、制表符的行才是空行（补充裁决第 0 条）。
  第一行物理行必须恰为表头；文件以换行符结尾时，最后一个换行符之后的空串不算一行。
- 文件级元数据只解析表头与首末数据行的日期字段，不解析价格列与来源列。
- 元数据与登记值不符即停止：不进入来源解析、价格解析、快照构建或后续计算。
- 逐行处理时先只取日期字段；日期晚于截止日即停止，不解析该行其余字段，也不处理后续行；
  处理完截止日那条记录后立即停止。只验证截止日以内各记录之间的格式，截止日之后的格式未验证。
- 字段按原样取值，不用 strip() 清除异常字符；含异常字符即在相应的解析或比对处报错。
- 日期只接受 ASCII 的 YYYY-MM-DD 且须真实存在；价格只接受“一个或多个 ASCII 数字，可接小数点与一个或多个数字”
  （正则见 PRICE_PATTERN），再要求严格大于零
  （补充裁决第一部分第 3 条）。文件级元数据的首末日期与逐行解析使用同一套日期规则。
- 没有任何启用第二来源补齐的开关或参数；允许清单以外的来源标识进入即报错。
这里得到的是历史研究的读取结果，与留痕类型之间没有任何转换路径。

八列口径（负责人裁决 D16（2026-10-04），取代补充裁决第二节第 2 小节表头口径）：
- 表头恰为 date,value,open,high,low,close,volume,source（HEADER）。
- 截止日以内每个数据记录恰为 8 个字段：价格取下标 1（value），来源取下标 7（source），先来源后价格；
  下标 2—6（open、high、low、close、volume）不作数值解析、业务取值或一致性校验，其字节只随整行解码与分字段处理。
- value 为空即缺价，不以 close 回填；截止日之后的行不检查字段数；文件级元数据核对不检查逐行字段数。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType

from market_risk.calendar import is_stock_trading_day, stock_trading_days
from market_risk.precision import published_price
from market_risk.wavewarn_v20.dataset_v20 import AssetSeries, DecisionEntry, FileMetadata, RegisteredFile
from market_risk.wavewarn_v20.snapshot import Snapshot, make_snapshot

HEADER = b"date,value,open,high,low,close,volume,source"          # 负责人裁决 D16（2026-10-04）
ALLOWED_SOURCES = frozenset({"yahoo", "correct:yahoo"})
DATE_PATTERN = re.compile(rb"[0-9]{4}-[0-9]{2}-[0-9]{2}")          # 只匹配 ASCII 数字；以 fullmatch 使用
PRICE_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?")                 # 不接受正负号、科学计数法、空白
CORRECTED_SOURCE = "correct:yahoo"
CORRECT_DECISION = "correct"


# 原因码（补充裁决 Q5）：与 labels_r2 中的同名定义取值相同，两处一致只由 tests/test_v20_isolation.py 的测试保证，
# 修改任何一边须同时修改另一边。本模块不导入 labels_r2（不改已批准的加载集合）。
REASON_MISSING_PRICE = "缺少必需价格"
REASON_INVALID_INPUT = "输入校验失败"
REASON_UNEXPECTED = "未预期异常"


class DataEntryError(ValueError):
    """行情文件、登记值或裁定条目不符合要求的基类。实际抛出的都是下面两个子类之一，reason 为原因码。

    本模块没有“未预期异常”一类：未归入下面两类的异常（如操作系统的读文件异常）原样向上抛出，不在这里包装。
    """

    reason = ""


class MissingPriceEntryError(DataEntryError):
    """缺少必需价格：截止日以内的交易日整行缺失，或某行的价格为空。"""

    reason = REASON_MISSING_PRICE


class DataInputError(DataEntryError):
    """输入校验失败：格式、表头、日期、来源、价格词法、登记值、修正条目、截止日等不符合要求。"""

    reason = REASON_INVALID_INPUT


def read_file_bytes(path: Path) -> bytes:
    """本模块唯一的读文件函数：二进制只读打开一次，读入全部字节后关闭。不 stat、不判断存在。"""
    with path.open("rb") as file:
        return file.read()


def physical_lines(raw: bytes) -> list[bytes]:
    """按 \n 切成物理行，每行只去掉一个行尾 \r。文件以换行符结尾时，最后一个换行符之后的空串不算一行。"""
    parts = raw.split(b"\n")
    if raw.endswith(b"\n"):
        parts.pop()
    return [line[:-1] if line.endswith(b"\r") else line for line in parts]


def is_blank(line: bytes) -> bool:
    """空行：（已去掉一个行尾 \r 的）物理行为空，或只含空格、制表符。\x0b、\x0c、残留的 \r 都算非空。"""
    return line.strip(b" \t") == b""


def _check_header(lines: list[bytes]) -> None:
    """第一行物理行必须恰为表头；表头前有空行、BOM 或其他内容即报错。"""
    if not lines or lines[0] != HEADER:
        raise DataInputError(f"第一行必须恰为表头 {HEADER.decode()}")


def _date_field(line: bytes) -> dt.date:
    """一行里第一个逗号之前的字段；只解析这一个字段。

    只接受 ASCII 的 YYYY-MM-DD，并验证日期真实存在（如 2001-02-30 拒绝）。
    date.fromisoformat 另外接受 20010102、2001-W01-1 等写法，所以先按词法规则匹配。
    """
    field = line.split(b",", 1)[0]
    text = field.decode("ascii", errors="replace")
    if DATE_PATTERN.fullmatch(field) is None:
        raise DataInputError(f"日期字段不是 ISO 日期：{text!r}")
    try:
        return dt.date(int(field[:4]), int(field[5:7]), int(field[8:10]))
    except ValueError as error:
        raise DataInputError(f"日期字段不是 ISO 日期（日期不存在）：{text!r}") from error


def file_metadata(raw: bytes) -> FileMetadata:
    """文件级元数据：两种哈希、两种行数与首末日期。只解析表头与首末数据行的日期字段。

    表头为八列 HEADER（负责人裁决 D16）；不检查逐行字段数，不解析价格、来源与下标 2—6。"""
    lines = physical_lines(raw)
    _check_header(lines)
    data = [line for line in lines[1:] if not is_blank(line)]
    if not data:
        raise DataInputError("文件没有数据行")
    return FileMetadata(hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                        len(data), len(data) + 1, _date_field(data[0]), _date_field(data[-1]))


def registered_differences(metadata: FileMetadata, registered: RegisteredFile) -> tuple[str, ...]:
    """逐项比较五项登记值，返回全部不符项的说明；全部相符时为空。"""
    pairs = (("原始字节 SHA-256", metadata.raw_sha256, registered.raw_sha256),
             ("换行规范化 SHA-256", metadata.normalized_sha256, registered.normalized_sha256),
             ("数据行数", metadata.data_rows, registered.data_rows),
             ("首日", metadata.first_date, registered.first_date),
             ("末日", metadata.last_date, registered.last_date))
    return tuple(f"{name}：文件为 {actual}，登记为 {expected}"
                 for name, actual, expected in pairs if actual != expected)


def verify_registered(metadata: FileMetadata, registered: RegisteredFile) -> None:
    """任一项不符即抛异常，并列出全部不符项。"""
    differences = registered_differences(metadata, registered)
    if differences:
        raise DataInputError(f"{registered.asset} 的文件与登记值不符：" + "；".join(differences))


def parse_source(text: str) -> str:
    """来源解析：只允许 yahoo 与 correct:yahoo；其他标识报错（补齐关闭）。"""
    if text not in ALLOWED_SOURCES:
        raise DataInputError(f"来源标识不在允许清单内：{text!r}")
    return text


def parse_price(text: str) -> Decimal:
    """价格解析：非空，按两位小数 ROUND_HALF_UP 读入（实施口径补充第 1 条）。

    按原样取值：含空白或不可打印字符即报错（Decimal 会自行去掉首尾空白，所以先检查；落实已有的严格契约）。
    词法规则（补充裁决第一部分第 3 条新增）：只接受 PRICE_PATTERN，拒绝正负号与科学计数法；
    不限定小数位数。之后要求严格大于零。
    """
    if not text:
        raise MissingPriceEntryError("价格为空")
    if any(char.isspace() or not char.isprintable() for char in text):
        raise DataInputError(f"价格含空白或不可打印字符：{text!r}")
    if PRICE_PATTERN.fullmatch(text) is None:
        raise DataInputError(f"价格不符合词法规则 [0-9]+(.[0-9]+)?：{text!r}")
    try:
        price = published_price(text)
    except InvalidOperation as error:
        raise DataInputError(f"价格不是十进制数：{text!r}") from error
    if not price.is_finite() or price <= 0:
        raise DataInputError(f"价格词法合法，但按登记精度规范化后不满足正价格要求：{text!r} → {price}")
    return price


def parse_rows(raw: bytes, cutoff: dt.date) -> tuple[tuple[dt.date, Decimal, str], ...]:
    """截止日以内各行的（日期，收盘价，来源）（补充裁决第四节第 3 部分）。

    遇到空行只记下“有待定的空行”，不向后扫描；遇到非空行先只取日期字段：
    日期晚于截止日即停止（待定的空行不作判断）；不晚于截止日而此前有待定的空行，报错；
    处理完日期等于截止日的那条记录后立即停止，不再处理其后的任何行。到达文件末尾时，待定的空行不作判断。

    八列口径（负责人裁决 D16（2026-10-04），取代补充裁决第二节第 2 小节表头口径）：截止日以内每行恰 8 个字段；
    来源取下标 7、价格取下标 1，先来源后价格；下标 2—6 不解析、不校验；value 为空不以 close 回填。
    """
    lines = physical_lines(raw)
    _check_header(lines)
    rows: list[tuple[dt.date, Decimal, str]] = []
    pending_blank = False
    for line in lines[1:]:
        if is_blank(line):
            pending_blank = True
            continue
        day = _date_field(line)
        if day > cutoff:
            break
        if pending_blank:
            raise DataInputError(f"数据记录之间出现空行：{day} 这一行之前")
        if rows and day <= rows[-1][0]:
            raise DataInputError(f"日期重复或乱序：{rows[-1][0]} 之后是 {day}")
        try:
            fields = line.decode("utf-8").split(",")
        except UnicodeDecodeError as error:
            raise DataInputError(f"{day} 这一行不是 UTF-8 文本") from error
        if len(fields) != 8:
            raise DataInputError(f"{day} 这一行不是八个字段")
        source = parse_source(fields[7])
        rows.append((day, parse_price(fields[1]), source))
        if day == cutoff:
            break
    return tuple(rows)


def check_trading_axis(asset: str, first_day: dt.date, cutoff: dt.date, days: Sequence[dt.date]) -> None:
    """完整交易日轴：从登记首日到截止日的 NYSE 交易日与读入的日期逐一比对。

    整行缺失、多出非交易日都报错，并列出全部日期。
    原因分类（补充裁决 Q5）：只有整行缺失时为“缺少必需价格”；出现多出的非交易日时文件本身不合格式，
    记为“输入校验失败”（同时列出缺少的交易日）。
    """
    expected = stock_trading_days(first_day, cutoff)
    missing = sorted(set(expected) - set(days))
    extra = sorted(set(days) - set(expected))
    if missing or extra:
        parts = []
        if missing:
            parts.append("缺少交易日 " + "、".join(str(day) for day in missing))
        if extra:
            parts.append("多出非交易日 " + "、".join(str(day) for day in extra))
        message = f"{asset} 的日期与交易日轴不符：" + "；".join(parts)
        if extra:
            raise DataInputError(message)
        raise MissingPriceEntryError(message)


def check_corrections(asset: str, cutoff: dt.date, rows: Sequence[tuple[dt.date, Decimal, str]],
                      decisions: Sequence[DecisionEntry]) -> None:
    """修正条目的双向核对。

    只取标的与当前资产完全相同、裁定类型为 correct、日期在截止日以内的条目；其他资产的条目与其他裁定类型都不当作修正。
    每个这样的条目，文件中该日的行必须来源为 correct:yahoo、价格等于 published_price(修正值)；
    截止日以内的每个 correct:yahoo 行，都必须有这样一个条目。
    """
    approved = {entry.date: entry for entry in decisions
                if entry.symbol == asset and entry.decision == CORRECT_DECISION and entry.date <= cutoff}
    by_day = {day: (price, source) for day, price, source in rows}
    problems: list[str] = []
    for day, entry in sorted(approved.items()):
        if entry.corrected_value is None:
            problems.append(f"{day} 的修正条目没有修正值")
        elif day not in by_day:
            problems.append(f"{day} 有已批准的修正条目，但文件里没有这一行")
        elif by_day[day][1] != CORRECTED_SOURCE:
            problems.append(f"{day} 有已批准的修正条目，但该行来源是 {by_day[day][1]}")
        elif by_day[day][0] != published_price(entry.corrected_value):
            problems.append(f"{day} 的价格 {by_day[day][0]} 与修正值 {published_price(entry.corrected_value)} 不一致")
    for day, (_, source) in sorted(by_day.items()):
        if source == CORRECTED_SOURCE and day not in approved:
            problems.append(f"{day} 的来源是 {CORRECTED_SOURCE}，但没有对应的已批准修正条目")
    if problems:
        raise DataInputError(f"{asset} 的修正条目核对不符：" + "；".join(problems))


def read_until(path: Path, asset: str, cutoff: dt.date, registered: RegisteredFile,
               decisions: Sequence[DecisionEntry]) -> AssetSeries:
    """读取某资产截至截止日的收盘价。整个过程只打开文件一次。

    打开文件之前依次检查：登记值属于该资产；截止日是 NYSE 交易日；截止日不早于该资产的登记数据覆盖首日
    （取自 registered，不读文件）。任一不符即报错，不打开行情文件（补充裁决第二节第 6 条）。
    """
    if registered.asset != asset:
        raise DataInputError(f"登记值属于 {registered.asset}，不是 {asset}")
    if not is_stock_trading_day(cutoff):
        raise DataInputError(f"截止日 {cutoff} 不是 NYSE 交易日")
    if cutoff < registered.first_date:
        raise DataInputError(f"截止日 {cutoff} 早于 {asset} 的登记数据覆盖首日 {registered.first_date}")
    raw = read_file_bytes(path)
    verify_registered(file_metadata(raw), registered)
    rows = parse_rows(raw, cutoff)
    check_trading_axis(asset, registered.first_date, cutoff, [day for day, _, _ in rows])
    check_corrections(asset, cutoff, rows, decisions)
    return AssetSeries(asset, registered.first_date, cutoff, {day: price for day, price, _ in rows},
                       hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class AssembledSnapshot:
    """双资产快照的组装结果：阶段一的 Snapshot（类型不修改）与各资产的登记首日。"""

    snapshot: Snapshot
    first_days: Mapping[str, dt.date]

    def pre_listing_days(self, asset: str) -> tuple[dt.date, ...]:
        """共同轴上早于该资产登记首日的日期：标为“上市前”，与缺价分开。"""
        return tuple(day for day in self.snapshot.days if day < self.first_days[asset])

    def missing_days(self, asset: str) -> tuple[dt.date, ...]:
        """缺价日期：只看该资产登记首日（含）之后的日期，不含上市前的日期。"""
        closes = self.snapshot.closes[asset]
        return tuple(day for day in self.snapshot.days if day >= self.first_days[asset] and day not in closes)


def assemble_snapshot(series: Mapping[str, AssetSeries], day: dt.date, acquired_at: dt.datetime) -> AssembledSnapshot:
    """把各资产的读取结果组装成快照。共同轴从较早的登记首日开始，到快照日为止的 NYSE 交易日。"""
    if not series:
        raise DataInputError("没有任何资产的读取结果")
    for asset, item in series.items():
        if item.asset != asset:
            raise DataInputError(f"键 {asset} 下放的是 {item.asset} 的读取结果")
        if item.cutoff != day:
            raise DataInputError(f"{asset} 的截止日 {item.cutoff} 与快照日 {day} 不同")
    first_days = {asset: item.first_date for asset, item in series.items()}
    axis = stock_trading_days(min(first_days.values()), day)
    snapshot = make_snapshot(axis, {asset: item.closes for asset, item in series.items()}, day, acquired_at,
                             {asset: item.raw_sha256 for asset, item in series.items()})
    return AssembledSnapshot(snapshot, MappingProxyType(first_days))
