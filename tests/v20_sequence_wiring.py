"""v2.0 真实序列接线模式（阶段四 M2 第二部分指令修订六第五节；设计稿第十节）：快照 → 工具 full 场景的纯转换。

- 输入：正式目录的 input_snapshot.csv.gz（表头 date,spx_close,qqq_close）与 run_record.json
  （只取 cutoff；补充单 K4 第 4 条）。
- 输出：工具 1709880 的 full 场景 JSON（kind、name、axis、calendar、cutoff、prices、params、r1_segments、meta）。
- 登记参数（分段、历史起点、截止日）封装为冻结数据类 Registration；REGISTERED 为唯一默认值，所有转换函数以
  registration 为显式参数，场景 meta.登记来源 写明“registered_v20”或“测试登记:<说明>”。
- 处理顺序固定（第五节第 2 小节）：读原始文件全部行（只取 date 文本）→ 以 date > cutoff 丢弃其后各行 →
  只对保留行解析价格 → 生成轴 → 校验 axis[-1] == cutoff。“原始文件行”与“场景轴”分别定义，axis 永远指截断后的轴。
- 读写只在三个边界函数：read_snapshot_csv、read_run_record、write_scenario（先写临时名再改名）。
  其余函数只依赖参数、经返回值输出；不导入 v20_compare_support 或任何其他项目模块（依赖方向为 support → 本模块）。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from market_risk.wavewarn_v20 import registered_v20

HEADER = ("date", "spx_close", "qqq_close")          # 快照表头（D29 serialize_snapshot；顺序固定）
ASSETS = ("SPX", "QQQ")                               # 资产顺序（D30 ASSETS）
COLUMN_OF = {"SPX": 1, "QQQ": 2}                      # 资产 → 快照列下标
DATE_LEXEME = re.compile(r"\d{4}-\d{2}-\d{2}")        # ISO 日期严格词法（fullmatch）
PRICE_LEXEME = re.compile(r"\d+\.\d{2}")              # 两位小数文本；空值为缺价
SOURCE_REGISTERED = "registered_v20"
TEST_SOURCE_PREFIX = "测试登记:"
SNAPSHOT_FILE = "input_snapshot.csv.gz"
RUN_RECORD_FILE = "run_record.json"

# 字段映射表（第五节第 2 小节；接线说明第十九节照录）：（来源，目标，规则）。
FIELD_MAPPING: tuple[tuple[str, str, str], ...] = (
    ("input_snapshot.csv.gz·date", "axis[i]", "YYYY-MM-DD 严格词法；保留行严格升序、无重复"),
    ("input_snapshot.csv.gz·spx_close", "prices.SPX[i]", "空 → null；非空须为两位小数文本且 > 0，原文写入"),
    ("input_snapshot.csv.gz·qqq_close", "prices.QQQ[i]", "同上"),
    ("input_snapshot.csv.gz·表头", "—", "恰为 date,spx_close,qqq_close；列名错位或多列即停"),
    ("run_record.json·cutoff（只读该键）", "cutoff",
     "≤ registration.cutoff；date > cutoff 的原始行先丢弃再解析；轴末日 = cutoff"),
    ("快照文件 SHA-256", "meta.快照SHA256", "整份字节 SHA-256，须等于登记值（调用方给出）"),
    ("registration.segments", "r1_segments", "[[起日, 止日], …]，顺序同登记；端点须在轴上"),
    ("registration.histories", "meta.类型、meta.缺价", "登记起点在轴上；其前全空、当日非空；缺价长度 = 起点下标"),
    ("固定值", "kind、params、calendar、name、meta.登记来源", "full、registered、NYSE、调用方给出的名称、登记来源"),
)


class PriceParser(Protocol):
    """价格文本解析函数的类型：空文本 → None，否则返回校验后的原文。"""

    def __call__(self, text: str) -> str | None: ...


class SequenceError(ValueError):
    """快照 → 场景的转换或校验失败：不生成场景。"""


@dataclass(frozen=True)
class Registration:
    """接线所需的登记参数：分段（名称，起日，止日）、两资产历史起点、截止日与登记来源文字。"""

    segments: tuple[tuple[str, dt.date, dt.date], ...]
    histories: tuple[tuple[str, dt.date], ...]
    cutoff: dt.date
    source: str

    @classmethod
    def from_module(cls, module: Any) -> Registration:
        """由 registered_v20 模块取分段、历史起点与截止日（不复制数值）。"""
        segments = tuple((item.name, item.start, item.end) for item in module.SEGMENTS)
        histories = tuple((asset, module.HISTORIES[asset]) for asset in ASSETS)
        return cls(segments, histories, module.CUTOFF, SOURCE_REGISTERED)


REGISTERED = Registration.from_module(registered_v20)


# ---------------------------------------------------------------------------
# 纯函数：词法、截断、映射与校验
# ---------------------------------------------------------------------------


def parse_date(text: str) -> dt.date:
    """ISO 日期：先 fullmatch 严格词法，再按日历合法性解析；任一不符即 SequenceError。"""
    if DATE_LEXEME.fullmatch(text) is None:
        raise SequenceError(f"日期词法不符：{text!r}")
    try:
        return dt.date.fromisoformat(text)
    except ValueError as error:
        raise SequenceError(f"日期不合法：{text!r}") from error


def parse_price(text: str) -> str | None:
    """收盘价文本：空 → None（缺价）；非空须为两位小数文本且大于 0，返回原文（不转数值、不舍入）。"""
    if text == "":
        return None
    if PRICE_LEXEME.fullmatch(text) is None:
        raise SequenceError(f"价格文本不符（须为两位小数）：{text!r}")
    try:
        positive = Decimal(text) > 0
    except InvalidOperation as error:
        raise SequenceError(f"价格文本不可解析：{text!r}") from error
    if not positive:
        raise SequenceError(f"价格须大于 0：{text!r}")
    return text


def snapshot_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_snapshot_hash(data: bytes, expected: str) -> str:
    """快照文件整份字节的 SHA-256 须等于登记值；返回实际值。"""
    actual = snapshot_sha256(data)
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise SequenceError(f"快照 SHA-256 登记值格式不符：{expected!r}")
    if actual != expected:
        raise SequenceError(f"快照 SHA-256 不符：实际 {actual}，登记 {expected}")
    return actual


def check_header(header: list[str]) -> None:
    if tuple(header) != HEADER:
        raise SequenceError(f"快照表头须恰为 {','.join(HEADER)}：{list(header)}")


def retained_rows(rows: list[list[str]], cutoff: dt.date) -> list[list[str]]:
    """原始数据行（不含表头）→ 截止日以内的保留行：只读每行的 date 文本，自首个 date > cutoff 的行起全部丢弃。
    被丢弃的行不解析价格、不核对列数。"""
    kept: list[list[str]] = []
    for row in rows:
        if not row:
            raise SequenceError("快照含空行")
        if parse_date(row[0]) > cutoff:
            break
        kept.append(row)
    return kept


def axis_of(rows: list[list[str]]) -> list[str]:
    """保留行 → 轴（日期文本）；列数须为 3；严格升序、无重复。"""
    axis: list[str] = []
    for row in rows:
        if len(row) != len(HEADER):
            raise SequenceError(f"{row[0]} 行的列数为 {len(row)}，须为 {len(HEADER)}")
        if axis and row[0] <= axis[-1]:
            kind = "重复" if row[0] == axis[-1] else "乱序"
            raise SequenceError(f"日期{kind}：{row[0]}（前一行 {axis[-1]}）")
        axis.append(row[0])
    return axis


def prices_of(rows: list[list[str]], price_parser: PriceParser) -> dict[str, list]:
    """只对保留行解析价格（price_parser 由调用方给出，测试以计数证明截止日之后的行未被解析）。"""
    return {asset: [price_parser(row[COLUMN_OF[asset]]) for row in rows] for asset in ASSETS}


def segments_on_axis(axis: list[str], registration: Registration) -> list[list[str]]:
    """登记分段 → r1_segments；每个端点须在轴上。"""
    present = set(axis)
    result = []
    for name, start, end in registration.segments:
        for day in (start, end):
            if day.isoformat() not in present:
                raise SequenceError(f"分段 {name} 的端点 {day} 不在轴上")
        result.append([start.isoformat(), end.isoformat()])
    return result


def late_starts(axis: list[str], prices: dict[str, list], registration: Registration) -> list[dict]:
    """登记历史起点：须在轴上；该日之前该资产全空、该日非空。起点下标 k > 0 时记一项缺价 {资产, 起 0, 长度 k}。"""
    gaps = []
    for asset, first in registration.histories:
        text = first.isoformat()
        if text not in axis:
            raise SequenceError(f"{asset} 的登记历史起点 {text} 不在轴上")
        index = axis.index(text)
        if any(value is not None for value in prices[asset][:index]):
            raise SequenceError(f"{asset} 在登记历史起点 {text} 之前有价格")
        if prices[asset][index] is None:
            raise SequenceError(f"{asset} 在登记历史起点 {text} 缺价")
        if index > 0:
            gaps.append({"资产": asset, "起": 0, "长度": index})
    return gaps


def scenario_meta(gaps: list[dict], registration: Registration, digest: str) -> dict:
    late = "".join(gap["资产"] for gap in gaps)
    kind = f"真实序列_{late}晚开始" if late else "真实序列"
    return {"类型": kind, "缺价": [dict(gap) for gap in gaps], "登记来源": registration.source,
            "快照SHA256": digest, "来源": SNAPSHOT_FILE}


def scenario_from_rows(rows: list[list[str]], cutoff: dt.date, digest: str, name: str,
                       registration: Registration, price_parser: PriceParser) -> dict:
    """原始行（含表头）→ full 场景。顺序固定：截止日核对 → 表头 → 按 date 截断 → 轴 → 解析保留行价格 →
    轴末日 = 截止日 → 分段 → 历史起点。任一不符即 SequenceError，不生成场景。"""
    if cutoff > registration.cutoff:
        raise SequenceError(f"截止日 {cutoff} 晚于登记截止日 {registration.cutoff}")
    if not rows:
        raise SequenceError("快照为空")
    check_header(rows[0])
    kept = retained_rows(rows[1:], cutoff)
    axis = axis_of(kept)
    prices = prices_of(kept, price_parser)
    if not axis or axis[-1] != cutoff.isoformat():
        raise SequenceError(f"轴末日不等于截止日：{axis[-1] if axis else '（空轴）'} ≠ {cutoff}")
    segments = segments_on_axis(axis, registration)
    gaps = late_starts(axis, prices, registration)
    return {"kind": "full", "name": name, "axis": axis, "calendar": "NYSE", "cutoff": cutoff.isoformat(),
            "prices": prices, "params": "registered", "r1_segments": segments,
            "meta": scenario_meta(gaps, registration, digest)}


def rows_from_bytes(data: bytes) -> list[list[str]]:
    """gzip 压缩的 UTF-8 CSV 字节 → 原始行（含表头）。"""
    try:
        text = gzip.decompress(data).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as error:
        raise SequenceError(f"快照不是可解压的 UTF-8 CSV：{error}") from error
    return [list(row) for row in csv.reader(text.splitlines())]


def run_record_cutoff(record: dict) -> dt.date:
    """run_record.json → 截止日（只取 cutoff；补充单 K4 第 4 条）。"""
    cutoff = record.get("cutoff")
    if not isinstance(cutoff, str):
        raise SequenceError(f"run_record 的 cutoff 不符：{cutoff!r}")
    return parse_date(cutoff)


def scenario_bytes(scenario: dict) -> bytes:
    return json.dumps(scenario, ensure_ascii=False, indent=1).encode("utf-8")


# ---------------------------------------------------------------------------
# 边界函数（逐处登记于 tests/test_v20_isolation.py）
# ---------------------------------------------------------------------------


def read_snapshot_csv(path: Path) -> bytes:
    """读快照文件的全部字节（一次）；解析交给 rows_from_bytes。"""
    return path.read_bytes()


def read_run_record(path: Path) -> dt.date:
    """读 run_record.json，只取 cutoff。"""
    with path.open("rb") as handle:
        record = json.loads(handle.read().decode("utf-8"))
    return run_record_cutoff(record)


def write_scenario(path: Path, data: bytes, fail_after: int | None) -> Path:
    """先写同目录临时名，再改名为目标；目标已存在即失败。fail_after 为测试注入（写入该字节数后抛 OSError），
    正式调用传 None。任一步失败：删除临时文件，不留下与场景同名的文件，抛 SequenceError。"""
    if path.exists():
        raise SequenceError(f"场景文件已存在：{path}")
    partial = path.with_name(path.name + ".partial")
    try:
        with partial.open("xb") as handle:
            handle.write(data if fail_after is None else data[:fail_after])
            if fail_after is not None:
                raise OSError(f"注入的写入中断（已写 {fail_after} 字节）")
        os.replace(partial, path)
    except OSError as error:
        if partial.exists():
            partial.unlink()
        raise SequenceError(f"场景写入失败：{error}") from error
    return path
