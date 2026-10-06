#!/usr/bin/env python3
"""波段预警 v2.0 独立回算·乙层（N2，recompute_b.py）。

由 input_snapshot.csv.gz 的价格独立计算 U，按项目输出的计划目标（daily_targets）计算组合收益与净值，
与 daily_nav 逐区间比对；恒定仓位平均权重与净值；结构 B 下执行政策已知前缀（前 known 行）的净值算术；
市场环境分类与各环境对数收益之和、固定区间暴露替换算术（负责人裁决 R8 甲）。
规格：实施指令修订六第四节第 2、4、5、6、9 小节；算法依据见同目录 README.md。

用法：python recompute_b.py --result-dir <结果目录> --out <新报告.json>
退出码：0 全部一致；1 存在不一致；2 参数或输入错误；3 计算失败、非有限值或报告写入失败。
只读结果目录内允许读取的文件（daily_policy 只在结构 B 下读取）；只写 --out（已存在即失败）。
只用标准库，不导入 market_risk。乙层不能证明状态机与事件判定，也不推定缺价之后的政策路径。
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    InvalidOperation,
)
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 冻结登记（脚本内常量，不从项目读取）

LAYER = "乙层（recompute_b.py）"
K_VALUES = (3, 5, 10)
THETA_TEXTS = ("0.015", "0.02", "0.025")
H_VALUES = (1, 3, 5)
REFERENCE = "主参照"
HOLD = "一直持有"
AVERAGE_NAMES = ("200 日均线一级版", "带缓冲带的 200 日均线二级版（101%/99%）")
CONSTANT_PREFIX = "恒定仓位："
SELECTED_PLACEHOLDER = "选定候选"
PATH_SIGNAL = "信号模拟"
PATH_POLICY = "执行政策研究模拟"
PATH_HOLD = "一直持有"
PATH_CONSTANT = "恒定仓位"
NAV_PATHS = (PATH_SIGNAL, PATH_POLICY, PATH_HOLD, PATH_CONSTANT)
POSITIONS = ("正常", "一级", "二级")
LAMBDA = 2.0
HOLD_WEIGHTS = (0.6, 0.4)  # 正常：核心 0.6、杠杆 0.4（hold_targets）
LEVEL1_WEIGHTS = (0.6, 0.0)  # 一级（暴露替换的登记权重）
LEVEL2_WEIGHTS = (0.3, 0.0)  # 二级（暴露替换的替换权重）
BEAR_PERIODS = (("2000-03-24", "2002-10-09"), ("2007-10-09", "2009-03-09"))  # 熊市：P ≤ d < Tr
ENVIRONMENT_ASSET = "SPX"
ENVIRONMENT_UP = Decimal("0.10")
ENVIRONMENT_DOWN = Decimal("-0.10")
ENVIRONMENTS = ("熊市", "上涨年", "下跌年", "平淡年", "完整年度分类不可得")
# NYSE 各年最后交易日（回算脚本会话按 NYSE 日历人工列出，来源说明见 README“常量来源表”）
YEAR_END_NYSE = (
    (1989, "1989-12-29"),
    (1990, "1990-12-31"),
    (1991, "1991-12-31"),
    (1992, "1992-12-31"),
    (1993, "1993-12-31"),
    (1994, "1994-12-30"),
    (1995, "1995-12-29"),
    (1996, "1996-12-31"),
    (1997, "1997-12-31"),
    (1998, "1998-12-31"),
    (1999, "1999-12-31"),
    (2000, "2000-12-29"),
    (2001, "2001-12-31"),
    (2002, "2002-12-31"),
    (2003, "2003-12-31"),
    (2004, "2004-12-31"),
    (2005, "2005-12-30"),
    (2006, "2006-12-29"),
    (2007, "2007-12-31"),
    (2008, "2008-12-31"),
    (2009, "2009-12-31"),
    (2010, "2010-12-31"),
    (2011, "2011-12-30"),
    (2012, "2012-12-31"),
    (2013, "2013-12-31"),
    (2014, "2014-12-31"),
    (2015, "2015-12-31"),
    (2016, "2016-12-30"),
)
TOL_RETURN = 1e-12  # 逐区间收益（绝对）
TOL_WEALTH = 1e-12  # 逐日净值（混合绝对/相对）
TOL_LOG = 1e-10  # 对数净值汇总、环境对数收益之和、暴露替换（绝对）
TOL_WEIGHT = 1e-12  # 恒定仓位平均权重（绝对）

BASE_FILES = (
    "input_snapshot.csv.gz",
    "daily_targets.csv.gz",
    "daily_nav.csv.gz",
    "window.json",
    "run_record.json",
    "descriptive.json",
    "candidates_summary.csv",
    "reference_summary.csv",
    "environments.csv",
    "exposure_substitution.csv",
    "MANIFEST.sha256",
)
POLICY_FILE = "daily_policy.csv.gz"
HEADERS = (
    ("input_snapshot.csv.gz", ("date", "spx_close", "qqq_close")),
    ("daily_targets.csv.gz", ("date", "object", "position", "core", "leverage", "source", "cap_active")),
    ("daily_nav.csv.gz", ("date", "object", "path", "wealth", "return_to_date")),
    (
        "daily_policy.csv.gz",
        ("date", "object", "position", "core", "leverage", "source", "cap_active", "wealth"),
    ),
    (
        "candidates_summary.csv",
        (
            "order",
            "object",
            "k",
            "theta_p",
            "h",
            "signal_log_wealth",
            "signal_max_drawdown",
            "policy_log_wealth",
            "policy_max_drawdown",
            "switches",
            "exposure_magnitude",
            "r1_computable",
            "r1_signal_drawdown",
            "r1_hold_drawdown",
            "r1_satisfied",
            "spx_r2_computable",
            "spx_r2_denominator",
            "spx_r2_achieved",
            "spx_r2_new_only",
            "spx_r2_meets",
            "qqq_r2_computable",
            "qqq_r2_denominator",
            "qqq_r2_achieved",
            "qqq_r2_new_only",
            "qqq_r2_meets",
            "policy_minus_signal",
            "direct_level2",
            "via_level1_into_level2",
            "level1_intervals",
            "level2_intervals",
        ),
    ),
    (
        "reference_summary.csv",
        (
            "object",
            "signal_log_wealth",
            "signal_max_drawdown",
            "policy_log_wealth",
            "policy_max_drawdown",
            "switches",
            "exposure_magnitude",
        ),
    ),
    ("environments.csv", ("interval", "start", "category", "year_return")),
    (
        "exposure_substitution.csv",
        (
            "object",
            "computed",
            "note",
            "intervals",
            "registered_log",
            "substituted_log",
            "difference",
            "interval_days",
        ),
    ),
)
EXIT_MEANINGS = (
    (0, "全部一致"),
    (1, "存在不一致"),
    (2, "参数或输入错误（含表头不符、行数或日期不符、表外取值、结构不在已定义集合内、结构判定字段非法）"),
    (3, "计算失败、非有限值或报告写入失败"),
)

Row = dict[str, str | None]


class InputError(Exception):
    """参数或输入错误，退出码 2。"""

    def __init__(self, message: str, location: str) -> None:
        super().__init__(message)
        self.message = message
        self.location = location


class ComputeFailure(Exception):
    """计算失败、非有限值或非正净值，退出码 3。"""

    def __init__(self, message: str, location: str) -> None:
        super().__init__(message)
        self.message = message
        self.location = location


class ReportWriteError(Exception):
    """报告写入失败，退出码 3。"""


@dataclass(frozen=True)
class Item:
    """一项比对：项、对象、位置、脚本值、项目值、差值、状态。"""

    item: str
    obj: str
    position: str
    script: Any
    project: Any
    difference: float | None
    status: str


# ---------------------------------------------------------------- 登记派生与比对


def candidate_keys() -> tuple[str, ...]:
    """27 个候选键，登记顺序先 K、再 θ_P、再 h。"""
    return tuple(f"K={k},θ_P={t},h={h}" for k in K_VALUES for t in THETA_TEXTS for h in H_VALUES)


def expected_header(name: str) -> tuple[str, ...]:
    """CSV 文件的登记表头。"""
    for file_name, header in HEADERS:
        if file_name == name:
            return header
    raise InputError("未登记表头的文件", name)


def decimal_context() -> Context:
    """Decimal 默认上下文（精度 28、银行家舍入）的显式副本，不依赖线程全局上下文。"""
    return Context(prec=28, rounding=ROUND_HALF_EVEN)


def compare_float(
    item: str, obj: str, position: str, script: float | None, project: float | None, tol: float, mixed: bool
) -> Item:
    """容差比较：先校验两端有限性；mixed 为真时用 tol·max(1,|a|,|b|)。"""
    if not (math.isfinite(tol) and tol > 0.0):
        raise ComputeFailure("计算失败：容差不是有限正数", f"{item} {position}")
    if script is None and project is None:
        return Item(item, obj, position, None, None, None, "一致（两者均为空）")
    if script is None or project is None:
        return Item(item, obj, position, script, project, None, "不一致：一方缺值")
    if not math.isfinite(script):
        return Item(item, obj, position, script, project, None, "计算失败：脚本值非有限")
    if not math.isfinite(project):
        return Item(item, obj, position, script, project, None, "不一致：项目值非有限")
    difference = abs(script - project)
    limit = tol * max(1.0, abs(script), abs(project)) if mixed else tol
    status = "一致" if difference <= limit else "不一致：超出容差"
    return Item(item, obj, position, script, project, difference, status)


def compare_exact(item: str, obj: str, position: str, script: Any, project: Any) -> Item:
    """精确相等（整数、布尔、日期、分类与 Decimal 文本、哈希）。"""
    if script is None and project is None:
        return Item(item, obj, position, None, None, None, "一致（两者均为空）")
    if script is None or project is None:
        return Item(item, obj, position, script, project, None, "不一致：一方缺值")
    same = type(script) is type(project) and script == project
    return Item(item, obj, position, script, project, None, "一致" if same else "不一致：值不同")


# ---------------------------------------------------------------- 解析（纯函数）


def parse_csv_table(name: str, raw: bytes) -> list[Row]:
    """解析 CSV（.gz 先解压）；表头须与登记一致；空字段记 None；附 _line 行号。"""
    try:
        data = gzip.decompress(raw) if name.endswith(".gz") else raw
        text = data.decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise InputError(f"无法解码：{exc}", name) from exc
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        raise InputError("缺少表头", name)
    header = tuple(rows[0])
    expected = expected_header(name)
    if header != expected:
        raise InputError(f"表头不符：实际 {list(header)}，应为 {list(expected)}", name)
    parsed: list[Row] = []
    for number, values in enumerate(rows[1:], start=2):
        if len(values) != len(header):
            raise InputError("列数不符", f"{name} 第 {number} 行")
        row: Row = {key: (value if value != "" else None) for key, value in zip(header, values, strict=True)}
        row["_line"] = str(number)
        parsed.append(row)
    return parsed


def parse_json_doc(name: str, raw: bytes) -> Any:
    """解析 JSON 文件。"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"JSON 无法解析：{exc}", name) from exc


def where(table: str, row: Row) -> str:
    """行定位文字。"""
    return f"{table} 第 {row['_line']} 行"


def need(row: Row, column: str, table: str) -> str:
    """必需字段（非空）。"""
    value = row[column]
    if value is None:
        raise InputError(f"字段 {column} 为空", where(table, row))
    return value


def to_date(text: str | None, location: str) -> date:
    """ISO 日期文本。"""
    try:
        return date.fromisoformat(text if text is not None else "")
    except ValueError as exc:
        raise InputError(f"日期不合法：{text!r}", location) from exc


def to_float(text: str | None, location: str) -> float | None:
    """repr 文本读入 float；空为 None。"""
    if text is None:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise InputError(f"数值不合法：{text!r}", location) from exc


def to_int(text: str | None, location: str) -> int | None:
    """整数文本；空为 None。"""
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise InputError(f"整数不合法：{text!r}", location) from exc


def to_bool(text: str | None, location: str) -> bool | None:
    """布尔文本 true/false；空为 None。"""
    if text is None:
        return None
    if text not in ("true", "false"):
        raise InputError(f"表外取值（布尔）：{text!r}", location)
    return text == "true"


def to_decimal(text: str | None, location: str) -> Decimal | None:
    """价格文本读入 Decimal；空为 None（缺价）。"""
    if text is None:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise InputError(f"价格不合法：{text!r}", location) from exc
    if not value.is_finite():
        raise InputError(f"价格不合法：{text!r}", location)
    return value


def to_enum(text: str | None, allowed: list[str] | tuple[str, ...], location: str) -> str:
    """分类文字须在登记全集内。"""
    if text is None or text not in allowed:
        raise InputError(f"表外取值：{text!r}，允许 {list(allowed)}", location)
    return text


def jget(doc: Any, key: str, location: str) -> Any:
    """JSON 对象字段（缺失即输入错误）。"""
    if not isinstance(doc, dict) or key not in doc:
        raise InputError(f"缺少字段 {key}", location)
    return doc[key]


def jfloat(value: Any, location: str) -> float | None:
    """JSON 数值（null 为 None）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"不是数值：{value!r}", location)
    return float(value)


def jint(value: Any, location: str) -> int | None:
    """JSON 整数（null 为 None）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"不是整数：{value!r}", location)
    return value


# ---------------------------------------------------------------- 元数据与快照


@dataclass(frozen=True)
class Window:
    """window.json 中用于确定执行日范围的字段。"""

    first_day: date
    last_day: date
    n: int
    j0: int


@dataclass(frozen=True)
class Snapshot:
    """快照：日期轴与两资产收盘价（Decimal，缺价为 None）。"""

    axis: tuple[date, ...]
    spx: tuple[Decimal | None, ...]
    qqq: tuple[Decimal | None, ...]


@dataclass(frozen=True)
class StructureFields:
    """descriptive.json 的四个结构判定字段。"""

    averages: tuple[tuple[str, bool], ...]
    constants: tuple[tuple[str, bool], ...]


def parse_window(doc: Any) -> Window:
    """读 window.first_day、last_day、n、j0。"""
    block = jget(doc, "window", "window.json")
    location = "window.json window"
    n = jint(jget(block, "n", location), f"{location}.n")
    j0 = jint(jget(block, "j0", location), f"{location}.j0")
    if n is None or j0 is None or n < 1 or j0 < 0:
        raise InputError("n、j0 须为整数且 n ≥ 1、j0 ≥ 0", location)
    first = to_date(jget(block, "first_day", location), f"{location}.first_day")
    last = to_date(jget(block, "last_day", location), f"{location}.last_day")
    return Window(first, last, n, j0)


def parse_snapshot(rows: list[Row] | tuple[Row, ...]) -> Snapshot:
    """快照日期轴须升序、无重复；价格为 Decimal，空为缺价。"""
    table = "input_snapshot.csv.gz"
    axis = tuple(to_date(row["date"], where(table, row)) for row in rows)
    for index in range(1, len(axis)):
        if axis[index] <= axis[index - 1]:
            raise InputError("日期轴不是严格升序（重复或倒序）", where(table, rows[index]))
    if not axis:
        raise InputError("快照没有数据行", table)
    spx = tuple(to_decimal(row["spx_close"], where(table, row)) for row in rows)
    qqq = tuple(to_decimal(row["qqq_close"], where(table, row)) for row in rows)
    return Snapshot(axis, spx, qqq)


def struct_entry(entry: Any, name_key: str, flag_key: str, location: str) -> tuple[str, bool]:
    """一项结构判定字段：名称为字符串、标志为 JSON 布尔；否则“结构判定字段非法”。"""
    if not isinstance(entry, dict) or name_key not in entry or flag_key not in entry:
        raise InputError(f"结构判定字段非法：缺少 {name_key} 或 {flag_key}", location)
    name = entry[name_key]
    flag = entry[flag_key]
    if not isinstance(name, str):
        raise InputError(f"结构判定字段非法：{name_key} 不是字符串", location)
    if not isinstance(flag, bool):
        raise InputError(f"结构判定字段非法：{flag_key} 须为 JSON 布尔，实际 {flag!r}", location)
    return name, flag


def parse_structure_fields(doc: Any, keys: tuple[str, ...]) -> StructureFields:
    """averages[*].name/evaluable 与 constants[*].object/computed（只决定重建哪些路径）。"""
    location = "descriptive.json"
    averages = jget(doc, "averages", location)
    constants = jget(doc, "constants", location)
    if not isinstance(averages, list) or len(averages) != 2:
        raise InputError("结构判定字段非法：averages 须恰两项", location)
    if not isinstance(constants, list) or len(constants) != 2:
        raise InputError("结构判定字段非法：constants 须恰两项", location)
    avg = tuple(
        struct_entry(entry, "name", "evaluable", f"{location} averages[{index}]")
        for index, entry in enumerate(averages)
    )
    con = tuple(
        struct_entry(entry, "object", "computed", f"{location} constants[{index}]")
        for index, entry in enumerate(constants)
    )
    if sorted(name for name, _ in avg) != sorted(AVERAGE_NAMES):
        raise InputError("结构判定字段非法：averages 的 name 重复或不在已知名称集合内", location)
    allowed = {REFERENCE, SELECTED_PLACEHOLDER, *keys}
    if any(obj not in allowed for obj, _ in con) or con[0][0] == con[1][0]:
        raise InputError("结构判定字段非法：constants 的 object 重复或不在已知名称集合内", location)
    return StructureFields(avg, con)


def locate_days(snapshot: Snapshot, window: Window, cutoff: date) -> tuple[tuple[date, ...], int]:
    """执行日：first_day 至 last_day 在轴上的闭区间，长 n + 1；days[0] 须为轴上下标 j0 的日期。"""
    location = "window.json 与 input_snapshot.csv.gz"
    if window.first_day not in snapshot.axis or window.last_day not in snapshot.axis:
        raise InputError("首末执行日不在快照日期轴上", location)
    start = snapshot.axis.index(window.first_day)
    end = snapshot.axis.index(window.last_day)
    if start != window.j0:
        raise InputError(f"days[0] 在轴上的下标为 {start}，不等于 window.j0 = {window.j0}", location)
    if end - start != window.n:
        raise InputError(f"执行日数 {end - start + 1} 不等于 window.n + 1 = {window.n + 1}", location)
    if window.last_day > cutoff:
        raise InputError("最后执行日晚于截止日", location)
    return snapshot.axis[start : end + 1], start


def first_missing(snapshot: Snapshot, start: int, n: int) -> int | None:
    """known：评价窗口 days[0..n] 中首个任一资产收盘价缺失的下标；无缺价为 None。"""
    for offset in range(n + 1):
        if snapshot.spx[start + offset] is None or snapshot.qqq[start + offset] is None:
            return offset
    return None


# ---------------------------------------------------------------- 算法（纯函数）


def asset_return(previous: Decimal, current: Decimal, location: str) -> float:
    """r = float(C_i) / float(C_{i−1}) − 1.0（价格由 Decimal 转 float，不舍入）。"""
    try:
        value = float(current) / float(previous) - 1.0
    except ZeroDivisionError as exc:
        raise ComputeFailure("计算失败：非有限值（价格为 0，除零）", location) from exc
    if not math.isfinite(value):
        raise ComputeFailure("计算失败：非有限值", location)
    return value


def equal_weight_return(spx: tuple[Decimal, Decimal], qqq: tuple[Decimal, Decimal], location: str) -> float:
    """U = 0.5 * (r_SPX + r_QQQ)：先相加再乘 0.5，资产顺序先 SPX 后 QQQ。"""
    value = 0.5 * (asset_return(spx[0], spx[1], location) + asset_return(qqq[0], qqq[1], location))
    if not math.isfinite(value):
        raise ComputeFailure("计算失败：非有限值", location)
    return value


def interval_returns(snapshot: Snapshot, start: int, n: int, known: int | None) -> tuple[float | None, ...]:
    """U_1..U_n；自首个缺价下标 known 起（区间 i ≥ known）无法计算，记 None。"""
    values: list[float | None] = []
    for i in range(1, n + 1):
        if known is not None and i >= known:
            values.append(None)
            continue
        spx0, spx1 = snapshot.spx[start + i - 1], snapshot.spx[start + i]
        qqq0, qqq1 = snapshot.qqq[start + i - 1], snapshot.qqq[start + i]
        if spx0 is None or spx1 is None or qqq0 is None or qqq1 is None:
            values.append(None)
            continue
        values.append(equal_weight_return((spx0, spx1), (qqq0, qqq1), f"区间 {i}"))
    return tuple(values)


def portfolio_return(core: float, leverage: float, u: float, location: str) -> float:
    """R = core·U + leverage·λ·U（按此顺序）；leverage > 0 时 1 + λU 不大于 0 即计算失败。"""
    if leverage > 0.0 and not (1.0 + LAMBDA * u > 0.0):
        raise ComputeFailure("计算失败：杠杆因子 1 + λU 不大于 0", location)
    value = core * u + leverage * LAMBDA * u
    if not math.isfinite(value):
        raise ComputeFailure("计算失败：非有限值", location)
    return value


def wealth_chain(returns: list[float] | tuple[float, ...], location: str) -> tuple[float, ...]:
    """W_0 = 1，W_i = W_{i−1}·(1 + R_i)；非有限或非正即计算失败。"""
    wealth = [1.0]
    for index, value in enumerate(returns, start=1):
        current = wealth[-1] * (1.0 + value)
        if not math.isfinite(current) or current <= 0.0:
            raise ComputeFailure("计算失败：非有限值或非正净值", f"{location} 区间 {index}")
        wealth.append(current)
    return tuple(wealth)


def log_sum(returns: list[float] | tuple[float, ...], location: str) -> float:
    """Σ ln(1 + R_i)，用 math.fsum(math.log1p)。"""
    for value in returns:
        if not math.isfinite(value) or value <= -1.0:
            raise ComputeFailure("计算失败：非有限值或非正净值", location)
    return math.fsum(math.log1p(value) for value in returns)


def target_returns(
    weights: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    us: list[float] | tuple[float, ...],
    location: str,
) -> tuple[float, ...]:
    """区间 i 用第 i − 1 个执行日的 (core, leverage) 与 U_i。"""
    return tuple(
        portfolio_return(core, leverage, u, f"{location} 区间 {index}")
        for index, ((core, leverage), u) in enumerate(zip(weights, us, strict=True), start=1)
    )


def constant_weights(
    cores: list[float] | tuple[float, ...], leverages: list[float] | tuple[float, ...]
) -> tuple[float, float, float]:
    """恒定仓位：w̄_c = fsum(core_0..n−1)/n，w̄_l = fsum(leverage_0..n−1)/n，ē = w̄_c + λ·w̄_l。"""
    count = len(cores)
    if count == 0 or len(leverages) != count:
        raise ComputeFailure("计算失败：平均权重的样本为空或长度不符", "恒定仓位")
    core = math.fsum(cores) / count
    leverage = math.fsum(leverages) / count
    return core, leverage, core + LAMBDA * leverage


def is_bear(day: date) -> bool:
    """熊市：P ≤ d < Tr（任一熊市区间）。"""
    return any(date.fromisoformat(p) <= day < date.fromisoformat(t) for p, t in BEAR_PERIODS)


def year_end(year: int) -> date | None:
    """常量表中的 NYSE 年末交易日；表外年份为 None。"""
    for value, text in YEAR_END_NYSE:
        if value == year:
            return date.fromisoformat(text)
    return None


def validate_year_ends(axis: list[date] | tuple[date, ...]) -> None:
    """落在轴范围内的年末常量须在轴上且为该年轴上最后一日；落在轴范围外的只使相关年份不可得。"""
    present = set(axis)
    last_in_year: dict[int, date] = {}
    for day in axis:
        last_in_year[day.year] = day
    for year, text in YEAR_END_NYSE:
        end = date.fromisoformat(text)
        if axis[0] <= end <= axis[-1] and (end not in present or last_in_year.get(year) != end):
            raise InputError(
                f"年末常量 {text} 在快照轴范围内，但不在轴上或不是 {year} 年轴上最后一日",
                "input_snapshot.csv.gz 与 YEAR_END_NYSE",
            )


def year_return(year: int, closes: dict[date, Decimal | None], cutoff: date) -> Decimal | None:
    """r_y = close(y 年末) ÷ close(y−1 年末) − 1（Decimal）；年末不在表或轴上、晚于截止日或缺价为 None。"""
    ends = (year_end(year - 1), year_end(year))
    values = []
    for end in ends:
        if end is None or end > cutoff or end not in closes or closes[end] is None:
            return None
        values.append(closes[end])
    context = decimal_context()
    try:
        return context.subtract(context.divide(values[1], values[0]), Decimal(1))
    except DecimalException as exc:
        raise ComputeFailure("计算失败：年度收益无法计算（除零或非法运算）", f"{year} 年") from exc


def classify_interval(day: date, annual: Decimal | None) -> str:
    """熊市优先；其余按 r_y：≥ 0.10 上涨年，≤ −0.10 下跌年，其余平淡年；r_y 不可得为“完整年度分类不可得”。"""
    if is_bear(day):
        return "熊市"
    if annual is None:
        return "完整年度分类不可得"
    if annual >= ENVIRONMENT_UP:
        return "上涨年"
    if annual <= ENVIRONMENT_DOWN:
        return "下跌年"
    return "平淡年"


def classify_environments(
    snapshot: Snapshot, days: list[date] | tuple[date, ...], cutoff: date
) -> tuple[tuple[str, Decimal | None], ...]:
    """区间 i = 1..n 的分类与年度收益；d 为区间起点执行日 days[i−1]，资产 SPX。"""
    validate_year_ends(snapshot.axis)
    closes = dict(zip(snapshot.axis, snapshot.spx, strict=True))
    result = []
    for day in days[:-1]:
        annual = year_return(day.year, closes, cutoff)
        result.append((classify_interval(day, annual), annual))
    return tuple(result)


def substitution_arithmetic(us: list[float] | tuple[float, ...], location: str) -> tuple[float, float, float]:
    """registered = fsum(log1p(R)) 用一级 (0.6, 0.0)；substituted 用二级 (0.3, 0.0)；difference = 后 − 前。"""
    registered = log_sum([portfolio_return(*LEVEL1_WEIGHTS, u, location) for u in us], location)
    substituted = log_sum([portfolio_return(*LEVEL2_WEIGHTS, u, location) for u in us], location)
    return registered, substituted, substituted - registered


# ---------------------------------------------------------------- 输入整理


@dataclass(frozen=True)
class LayerContext:
    """乙层上下文：快照、窗口、执行日、known、项目是否有净值行。"""

    snapshot: Snapshot
    window: Window
    cutoff: date
    days: tuple[date, ...]
    start: int
    known: int | None
    has_nav: bool


def prepare(raw: dict[str, bytes]) -> LayerContext:
    """解析快照、窗口与截止日，确定执行日与 known（不从项目净值行数反推）。"""
    snapshot = parse_snapshot(parse_csv_table("input_snapshot.csv.gz", raw["input_snapshot.csv.gz"]))
    window = parse_window(parse_json_doc("window.json", raw["window.json"]))
    record = parse_json_doc("run_record.json", raw["run_record.json"])
    cutoff = to_date(jget(record, "cutoff", "run_record.json"), "run_record.json cutoff")
    days, start = locate_days(snapshot, window, cutoff)
    known = first_missing(snapshot, start, window.n)
    has_nav = bool(parse_csv_table("daily_nav.csv.gz", raw["daily_nav.csv.gz"]))
    return LayerContext(snapshot, window, cutoff, days, start, known, has_nav)


def needs_policy(raw: dict[str, bytes]) -> bool:
    """是否为结构 B（快照有缺价且项目无净值行）；只有此时才读取 daily_policy。"""
    try:
        context = prepare(raw)
    except (InputError, ComputeFailure):
        return False
    return context.known is not None and not context.has_nav


def structure_items(context: LayerContext) -> tuple[str, list[Item]]:
    """快照决定结构：无缺价 ⇔ 结构 A；有缺价 ⇔ 结构 B。"""
    if context.known is not None and context.has_nav:
        raise InputError(
            f"结构不在已定义集合内：快照在 days[{context.known}] 缺价（结构 B），但 daily_nav 有数据行",
            "daily_nav.csv.gz",
        )
    if context.known is None and not context.has_nav:
        return "A", [
            Item(
                "结构与快照",
                "结构",
                "daily_nav.csv.gz",
                "A（快照无缺价）",
                "B（无净值行）",
                None,
                "不一致：结构与快照不符",
            )
        ]
    structure = "A" if context.known is None else "B"
    return structure, [compare_exact("结构与快照", "结构", "daily_nav.csv.gz", structure, structure)]


def index_targets(
    rows: list[Row] | tuple[Row, ...],
    objects: list[str] | tuple[str, ...],
    days: list[date] | tuple[date, ...],
    table: str,
    prefix: bool,
) -> tuple[dict[str, list[Row]], list[Item]]:
    """按对象分组并核对日期（结构 A 的计划目标须恰为 days；结构 B 的 daily_policy 须为 days 的前缀）。"""
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        grouped.setdefault(need(row, "object", table), []).append(row)
    items = [
        Item("对象集合", obj, table, None, "存在逐日行", None, "不一致：对象不在有逐日行的对象集合 O_d 内")
        for obj in sorted(grouped)
        if obj not in objects
    ]
    result: dict[str, list[Row]] = {}
    for obj in objects:
        found = sorted(grouped.get(obj, []), key=lambda row: str(row["date"]))
        dates = [to_date(row["date"], where(table, row)) for row in found]
        expected = list(days[: len(dates)]) if prefix else list(days)
        if not found or dates != expected:
            raise InputError(
                f"行数或日期不符：{len(dates)} 行，应为 {len(expected)} 行；"
                f"首个不符日期 {first_bad_date(dates, expected)}",
                f"{table} 对象={obj}",
            )
        result[obj] = found
    return result, items


def first_bad_date(actual: list[date] | tuple[date, ...], expected: list[date] | tuple[date, ...]) -> str:
    """两个日期序列的首个不符日期（文字）。"""
    for left, right in zip(actual, expected, strict=False):
        if left != right:
            return min(left, right).isoformat()
    if len(actual) > len(expected):
        return actual[len(expected)].isoformat()
    if len(actual) < len(expected):
        return expected[len(actual)].isoformat()
    return "无"


def row_weights(rows: list[Row] | tuple[Row, ...], table: str) -> list[tuple[float, float]]:
    """逐行 (core, leverage)。"""
    weights = []
    for row in rows:
        core = to_float(need(row, "core", table), where(table, row))
        leverage = to_float(need(row, "leverage", table), where(table, row))
        weights.append((float(core or 0.0), float(leverage or 0.0)))
    return weights


# ---------------------------------------------------------------- 乙层核对


@dataclass(frozen=True)
class NavPath:
    """一条由乙层重建的净值路径。"""

    obj: str
    path: str
    returns: tuple[float, ...]
    wealth: tuple[float, ...]


def build_paths(
    targets: dict[str, list[Row]],
    us: list[float] | tuple[float, ...],
    constants: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    n: int,
) -> tuple[list[NavPath], dict[str, tuple[float, float, float]]]:
    """信号模拟（O_d）、一直持有与恒定仓位（K）的收益与净值。"""
    paths = []
    for obj, rows in targets.items():
        weights = row_weights(rows, "daily_targets.csv.gz")[:n]
        returns = target_returns(weights, us, f"对象={obj}")
        paths.append(NavPath(obj, PATH_SIGNAL, returns, wealth_chain(returns, f"对象={obj}")))
    hold = target_returns([HOLD_WEIGHTS] * n, us, f"对象={HOLD}")
    paths.append(NavPath(HOLD, PATH_HOLD, hold, wealth_chain(hold, f"对象={HOLD}")))
    averages: dict[str, tuple[float, float, float]] = {}
    for name, source in constants:
        weights = row_weights(targets[source], "daily_targets.csv.gz")[:n]
        average = constant_weights([w[0] for w in weights], [w[1] for w in weights])
        averages[name] = average
        returns = target_returns([(average[0], average[1])] * n, us, f"对象={name}")
        paths.append(NavPath(name, PATH_CONSTANT, returns, wealth_chain(returns, f"对象={name}")))
    return paths, averages


def compare_path(path: NavPath, rows: list[Row] | tuple[Row, ...], days: list[date] | tuple[date, ...]) -> list[Item]:
    """逐区间收益（1e-12 绝对）与逐日净值（混合容差）；首行 return_to_date 须为空。"""
    table = "daily_nav.csv.gz"
    location = f"{table} 对象={path.obj} 路径={path.path}"
    if not rows:
        return [Item("净值路径", path.obj, location, "应存在", None, None, "不一致：一方缺值")]
    dates = [to_date(row["date"], where(table, row)) for row in rows]
    if dates != list(days):
        raise InputError("行数或日期不符：净值行的日期不等于执行日序列", location)
    items = [
        compare_exact("首行 return_to_date", path.obj, f"{location} 日期={dates[0]}", None, rows[0]["return_to_date"])
    ]
    for index, row in enumerate(rows):
        at = f"{location} 日期={dates[index].isoformat()}"
        if index > 0:
            items.append(
                compare_float(
                    "逐区间收益",
                    path.obj,
                    at,
                    path.returns[index - 1],
                    to_float(row["return_to_date"], where(table, row)),
                    TOL_RETURN,
                    False,
                )
            )
        items.append(
            compare_float(
                "逐日净值",
                path.obj,
                at,
                path.wealth[index],
                to_float(row["wealth"], where(table, row)),
                TOL_WEALTH,
                True,
            )
        )
    return items


def compare_nav(
    paths: list[NavPath] | tuple[NavPath, ...],
    nav_rows: list[Row] | tuple[Row, ...],
    days: list[date] | tuple[date, ...],
) -> list[Item]:
    """daily_nav 中信号模拟、一直持有、恒定仓位三类路径逐行比对；乙层不读执行政策路径。"""
    table = "daily_nav.csv.gz"
    grouped: dict[tuple[str, str], list[Row]] = {}
    for row in nav_rows:
        path = to_enum(row["path"], NAV_PATHS, where(table, row))
        if path != PATH_POLICY:
            grouped.setdefault((need(row, "object", table), path), []).append(row)
    expected = {(path.obj, path.path) for path in paths}
    items = [
        Item(
            "合法组合", obj, f"{table} 路径={path}", None, f"{len(rows)} 行", None, "不一致：对象或路径不在合法组合表内"
        )
        for (obj, path), rows in sorted(grouped.items())
        if (obj, path) not in expected
    ]
    for path in paths:
        rows = sorted(grouped.get((path.obj, path.path), []), key=lambda row: str(row["date"]))
        items.extend(compare_path(path, rows, days))
    return items


def summary_log_items(
    paths: list[NavPath] | tuple[NavPath, ...],
    tables: dict[str, list[Row]],
    docs: dict[str, Any],
    averages: dict[str, tuple[float, float, float]],
    evaluable: list[str] | tuple[str, ...],
) -> list[Item]:
    """ln W_末 与 candidates_summary、reference_summary 的 signal_log_wealth，及 descriptive.constants 比较。"""
    logs = {path.obj: math.log(path.wealth[-1]) for path in paths}
    items = []
    keys = candidate_keys()
    for row in tables["candidates_summary.csv"]:
        obj = str(row["object"])
        if obj in keys:
            items.append(
                compare_float(
                    "signal_log_wealth",
                    obj,
                    where("candidates_summary.csv", row),
                    logs.get(obj),
                    to_float(row["signal_log_wealth"], where("candidates_summary.csv", row)),
                    TOL_LOG,
                    False,
                )
            )
    for row in tables["reference_summary.csv"]:
        obj = str(row["object"])
        if obj in (REFERENCE, HOLD, *AVERAGE_NAMES):
            script = logs.get(obj) if obj in (REFERENCE, HOLD, *evaluable) else None
            items.append(
                compare_float(
                    "signal_log_wealth",
                    obj,
                    where("reference_summary.csv", row),
                    script,
                    to_float(row["signal_log_wealth"], where("reference_summary.csv", row)),
                    TOL_LOG,
                    False,
                )
            )
    items.extend(constant_items(docs["descriptive.json"], logs, averages))
    return items


def constant_items(doc: Any, logs: dict[str, float], averages: dict[str, tuple[float, float, float]]) -> list[Item]:
    """descriptive.constants[*]：已计算者比对 core、leverage、exposure（1e-12 绝对）与 nav.log_wealth（1e-10 绝对）；
    computed 为假者 core、leverage、exposure、nav 须均为 JSON null（答复单第 14 项）。"""
    items = []
    for index, entry in enumerate(jget(doc, "constants", "descriptive.json")):
        at = f"descriptive.json constants[{index}]"
        name = CONSTANT_PREFIX + str(entry["object"])
        if entry["computed"] is False:
            items.extend(
                compare_exact(f"{key}（computed 为假须为 null）", name, at, None, jget(entry, key, at))
                for key in ("core", "leverage", "exposure", "nav")
            )
            continue
        if name not in averages:
            continue  # computed 为真但身份不符或处于结构 B：已在 object_plan / evaluate 记为不一致
        nav = jget(entry, "nav", at)
        project_log = jfloat(jget(nav, "log_wealth", at), at)
        items.append(compare_float("nav.log_wealth", name, at, logs.get(name), project_log, TOL_LOG, False))
        for label, value in zip(("core", "leverage", "exposure"), averages[name], strict=True):
            items.append(compare_float(label, name, at, value, jfloat(jget(entry, label, at), at), TOL_WEIGHT, False))
    return items


def policy_prefix_items(
    policy: dict[str, list[Row]], us: list[float | None] | tuple[float | None, ...], known: int
) -> list[Item]:
    """结构 B：各对象 wealth 非空行数须等于 known；known > 0 时重建前 known 行净值并按混合容差比对。"""
    table = POLICY_FILE
    items = []
    for obj, rows in policy.items():
        filled = sum(1 for row in rows if row["wealth"] is not None)
        items.append(compare_exact("wealth 非空行数等于 known", obj, f"{table} 对象={obj}", known, filled))
        if known == 0:
            continue
        weights = row_weights(rows, table)[: known - 1]
        returns = target_returns(weights, [float(u) for u in us[: known - 1] if u is not None], f"对象={obj}")
        wealth = wealth_chain(returns, f"{table} 对象={obj}")
        for index, row in enumerate(rows[:known]):
            items.append(
                compare_float(
                    "已知前缀净值",
                    obj,
                    f"{where(table, row)} 日期={row['date']}",
                    wealth[index] if index < len(wealth) else None,
                    to_float(row["wealth"], where(table, row)),
                    TOL_WEALTH,
                    True,
                )
            )
    return items


def environment_items(
    classes: list[tuple[str, Decimal | None]] | tuple[tuple[str, Decimal | None], ...],
    rows: list[Row] | tuple[Row, ...],
    days: list[date] | tuple[date, ...],
) -> list[Item]:
    """environments.csv：每区间一行，起点、分类、年度收益精确相等；熊市与不可得区间 year_return 为空，
    上涨年、下跌年、平淡年为 r_y 的 Decimal 精确文本（答复单第 12 项）。"""
    table = "environments.csv"
    numbers = [to_int(need(row, "interval", table), where(table, row)) for row in rows]
    if sorted(n for n in numbers if n is not None) != list(range(1, len(classes) + 1)) or len(numbers) != len(classes):
        raise InputError(f"行数或日期不符：区间编号应为 1…{len(classes)}", table)
    items = []
    for row in sorted(rows, key=lambda r: int(str(r["interval"]))):
        i = int(str(row["interval"]))
        at = where(table, row)
        category, annual = classes[i - 1]
        items.append(compare_exact("start", f"区间 {i}", at, days[i - 1], to_date(row["start"], at)))
        items.append(compare_exact("category", f"区间 {i}", at, category, to_enum(row["category"], ENVIRONMENTS, at)))
        expected = None if category in ("熊市", "完整年度分类不可得") or annual is None else str(annual)
        items.append(compare_exact("year_return", f"区间 {i}", at, expected, row["year_return"]))
    return items


def summary_entries(doc: Any, members: tuple[str, ...]) -> dict[str, Any]:
    """environment_summary：键恰为五个环境名，每项含 intervals 与 log_return_sums（键恰为集合 B 的 29 个对象）；
    否则输入错误。"""
    location = "descriptive.json environment_summary"
    summary = jget(doc, "environment_summary", "descriptive.json")
    if not isinstance(summary, dict) or set(summary) != set(ENVIRONMENTS):
        keys = list(summary) if isinstance(summary, dict) else summary
        raise InputError(f"键集合不符：应恰为五个环境名，实际 {keys}", location)
    for category, entry in summary.items():
        at = f"{location}.{category}"
        sums = jget(entry, "log_return_sums", at)
        jget(entry, "intervals", at)
        if not isinstance(sums, dict) or set(sums) != set(members):
            keys = list(sums) if isinstance(sums, dict) else sums
            raise InputError(f"log_return_sums 键集合不符：应恰为 27 个候选键 + 主参照 + 一直持有，实际 {keys}", at)
    return summary


def environment_summary_items(
    doc: Any,
    classes: list[tuple[str, Decimal | None]] | tuple[tuple[str, Decimal | None], ...],
    paths: list[NavPath] | tuple[NavPath, ...],
    structure: str,
) -> list[Item]:
    """descriptive.environment_summary（答复单第 13 项）：各环境区间数两种结构均比对；结构 A 下集合 B 各对象的
    对数收益之和（1e-10，对象净值不可用时期望 null）；结构 B 下全部为 null。"""
    members = (*candidate_keys(), REFERENCE, HOLD)
    summary = summary_entries(doc, members)
    returns = {path.obj: path.returns for path in paths if path.path in (PATH_SIGNAL, PATH_HOLD)}
    items = []
    for category in ENVIRONMENTS:
        intervals = [i for i, (value, _) in enumerate(classes) if value == category]
        entry = summary[category]
        at = f"descriptive.json environment_summary.{category}"
        items.append(compare_exact("intervals", category, at, len(intervals), jint(jget(entry, "intervals", at), at)))
        sums = entry["log_return_sums"]
        for obj in members:
            position = f"{at}.{obj}"
            if structure == "B":
                items.append(compare_exact("log_return_sums（结构 B 须为 null）", obj, position, None, sums[obj]))
                continue
            script = log_sum([returns[obj][i] for i in intervals], at) if obj in returns else None
            items.append(compare_float("log_return_sums", obj, position, script, jfloat(sums[obj], at), TOL_LOG, False))
    return items


def parse_interval_days(text: str | None, location: str) -> list[tuple[date, date]]:
    """interval_days 文字 `起/止;起/止…`。"""
    if text is None:
        return []
    pairs = []
    for part in text.split(";"):
        pieces = part.split("/")
        if len(pieces) != 2:
            raise InputError(f"interval_days 格式不符：{part!r}", location)
        pairs.append((to_date(pieces[0], location), to_date(pieces[1], location)))
    return pairs


def substitution_row_items(
    obj: str,
    row: Row,
    days: list[date] | tuple[date, ...],
    positions: dict[date, str],
    us: list[float | None] | tuple[float | None, ...],
) -> list[Item]:
    """一个候选的暴露替换：必要条件（相邻执行日、起点一级、起点熊市）与三项对数和。"""
    table = "exposure_substitution.csv"
    at = where(table, row)
    pairs = parse_interval_days(row["interval_days"], at)
    items = [compare_exact("intervals", obj, at, len(pairs), to_int(row["intervals"], at))]
    selected: list[float] = []
    for start, end in pairs:
        label = f"{at} 区间 {start.isoformat()}/{end.isoformat()}"
        index = days.index(start) + 1 if start in days else None
        if index is None or index >= len(days) or days[index] != end:
            items.append(Item("替换区间", obj, label, None, f"{start}/{end}", None, "不一致：起止不是相邻执行日"))
            continue
        if positions.get(start) != "一级":
            items.append(
                Item("替换区间", obj, label, "一级", positions.get(start), None, "不一致：起点 position 不是一级")
            )
        if not is_bear(start):
            items.append(Item("替换区间", obj, label, "熊市", start, None, "不一致：起点不满足熊市条件"))
        u = us[index - 1]
        if u is None:
            items.append(Item("替换区间", obj, label, None, f"{start}/{end}", None, "不一致：一方缺值（U 不可计算）"))
            continue
        selected.append(u)
    registered, substituted, difference = substitution_arithmetic(selected, at)
    for label, value in (("registered_log", registered), ("substituted_log", substituted), ("difference", difference)):
        items.append(compare_float(label, obj, at, value, to_float(row[label], at), TOL_LOG, False))
    return items


def substitution_items(
    rows: list[Row] | tuple[Row, ...],
    targets: dict[str, list[Row]],
    days: list[date] | tuple[date, ...],
    us: list[float | None] | tuple[float | None, ...],
) -> list[Item]:
    """exposure_substitution.csv：每候选一行；computed 为真者核对区间与算术；为假者 registered_log、substituted_log、
    difference、interval_days 须为空且 intervals 为 0（答复单第 14 项）；note 列不读取。"""
    table = "exposure_substitution.csv"
    keys = candidate_keys()
    seen: set[str] = set()
    items = []
    for row in rows:
        obj = str(row["object"])
        at = where(table, row)
        if obj not in keys or obj in seen:
            items.append(Item("替换行集合", obj, at, None, obj, None, "不一致：多余、重复或未知行"))
            continue
        seen.add(obj)
        if not to_bool(need(row, "computed", table), at):
            items.append(compare_exact("intervals（computed 为假）", obj, at, 0, to_int(row["intervals"], at)))
            items.extend(
                compare_exact(f"{key}（computed 为假须为空）", obj, at, None, row[key])
                for key in ("registered_log", "substituted_log", "difference", "interval_days")
            )
            continue
        positions = {to_date(r["date"], where("daily_targets.csv.gz", r)): str(r["position"]) for r in targets[obj]}
        items.extend(substitution_row_items(obj, row, days, positions, us))
    items.extend(
        Item("替换行集合", obj, table, "应存在", None, None, "不一致：缺少该行") for obj in keys if obj not in seen
    )
    return items


def manifest_snapshot_items(raw_manifest: bytes, raw_snapshot: bytes) -> list[Item]:
    """MANIFEST.sha256 中文件名为 input_snapshot.csv.gz 的唯一一行（`<sha256> <字节数> <文件名>`）与快照实际
    SHA-256、字节数核对；缺失、重复、格式非法、不符均为输入错误（勘误补充单 K4 第 2—3 条；只读这一行，
    不读清单所指其他文件）。"""
    location = "MANIFEST.sha256 的 input_snapshot.csv.gz 行"
    try:
        text = raw_manifest.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError("MANIFEST.sha256 不是 UTF-8 文本", location) from exc
    name = "input_snapshot.csv.gz"
    lines = [line for line in text.split("\n") if line == name or line.endswith(" " + name)]
    if len(lines) != 1:
        raise InputError(f"登记项应恰有一行，实际 {len(lines)} 行", location)
    parts = lines[0].split(" ")
    hex_digits = "0123456789abcdef"
    if len(parts) != 3 or parts[2] != name or len(parts[0]) != 64 or any(c not in hex_digits for c in parts[0]):
        raise InputError(f"格式非法：{lines[0]!r}", location)
    if not parts[1].isascii() or not parts[1].isdigit():
        raise InputError(f"字节数不是非负十进制整数：{parts[1]!r}", location)
    actual = (hashlib.sha256(raw_snapshot).hexdigest(), len(raw_snapshot))
    if (parts[0], int(parts[1])) != actual:
        raise InputError(f"与快照不符：登记 {parts[0]} {parts[1]}，实际 {actual[0]} {actual[1]}", location)
    return [compare_exact("快照 SHA-256 与字节数", name, location, actual, (parts[0], int(parts[1])))]


# ---------------------------------------------------------------- 总流程


def object_plan(fields: StructureFields) -> tuple[tuple[str, ...], list[tuple[str, str]], list[Item]]:
    """O_d = C ∪ {主参照} ∪ E；K 取 computed 为真且身份相符者（constants[0] 为主参照；constants[1] 为候选键）。"""
    evaluable = tuple(name for name in AVERAGE_NAMES if dict(fields.averages)[name])
    daily = (*candidate_keys(), REFERENCE, *evaluable)
    constants: list[tuple[str, str]] = []
    items: list[Item] = []
    for index, (obj, computed) in enumerate(fields.constants):
        identity_ok = obj == REFERENCE if index == 0 else obj in candidate_keys()
        if computed and identity_ok:
            constants.append((CONSTANT_PREFIX + obj, obj))
        elif computed:
            items.append(
                Item(
                    "恒定仓位身份",
                    obj,
                    f"descriptive.json constants[{index}]",
                    None,
                    obj,
                    None,
                    "不一致：computed 为真但对象身份不符",
                )
            )
    return daily, constants, items


def evaluate(raw: dict[str, bytes], policy_raw: bytes | None) -> dict[str, Any]:
    """乙层全部核对；返回报告主体。"""
    context = prepare(raw)
    tables = {name: parse_csv_table(name, raw[name]) for name in BASE_FILES if name.endswith((".csv", ".gz"))}
    docs = {name: parse_json_doc(name, raw[name]) for name in BASE_FILES if name.endswith(".json")}
    fields = parse_structure_fields(docs["descriptive.json"], candidate_keys())
    structure, items = structure_items(context)
    daily, constants, more = object_plan(fields)
    items.extend(more)
    if structure == "B" and constants:
        items.extend(
            Item(
                "恒定仓位条件", name, "descriptive.json constants", False, True, None, "不一致：结构 B 下 computed 为真"
            )
            for name, _ in constants
        )
        constants = []
    targets, more = index_targets(tables["daily_targets.csv.gz"], daily, context.days, "daily_targets.csv.gz", False)
    items.extend(more)
    us = interval_returns(context.snapshot, context.start, context.window.n, context.known)
    paths: list[NavPath] = []
    if context.known is None and context.has_nav:
        paths, averages = build_paths(targets, [float(u) for u in us if u is not None], constants, context.window.n)
        items.extend(compare_nav(paths, tables["daily_nav.csv.gz"], context.days))
    else:
        averages = {}
    evaluable = tuple(name for name in AVERAGE_NAMES if name in daily)
    items.extend(summary_log_items(paths, tables, docs, averages, evaluable))
    if structure == "B" and policy_raw is not None:
        policy, more = index_targets(parse_csv_table(POLICY_FILE, policy_raw), daily, context.days, POLICY_FILE, True)
        items.extend(more)
        items.extend(policy_prefix_items(policy, us, int(context.known or 0)))
    classes = classify_environments(context.snapshot, context.days, context.cutoff)
    items.extend(environment_items(classes, tables["environments.csv"], context.days))
    items.extend(environment_summary_items(docs["descriptive.json"], classes, paths, structure))
    items.extend(substitution_items(tables["exposure_substitution.csv"], targets, context.days, us))
    items.extend(manifest_snapshot_items(raw["MANIFEST.sha256"], raw["input_snapshot.csv.gz"]))
    return {
        "structure": structure,
        "known": context.known,
        "usage_restriction": jget(docs["run_record.json"], "usage_restriction", "run_record.json"),
        "cutoff": context.cutoff,
        "days": list(context.days),
        "interval_u": list(us),
        "object_sets": {"O_d": list(daily), "K": [name for name, _ in constants]},
        "environments": [
            {"interval": i + 1, "category": c, "year_return": None if a is None else str(a)}
            for i, (c, a) in enumerate(classes)
        ],
        "items": items,
    }


# ---------------------------------------------------------------- 报告


def json_ready(value: Any) -> Any:
    """转为可写 JSON 的值：日期 ISO、非有限浮点为文字、Decimal 为文本、Item 为对象。"""
    if isinstance(value, Item):
        return {
            "item": value.item,
            "object": value.obj,
            "position": value.position,
            "script": json_ready(value.script),
            "project": json_ready(value.project),
            "difference": json_ready(value.difference),
            "status": value.status,
        }
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): json_ready(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(inner) for inner in value]
    return value


def finish_report(
    body: dict[str, Any], hashes: list[dict[str, Any]], error: dict[str, str] | None, error_code: int
) -> tuple[dict[str, Any], int]:
    """汇总计数、首个不一致与退出码。"""
    items: list[Item] = body.pop("items", [])
    mismatches = [item for item in items if item.status.startswith("不一致")]
    failures = [item for item in items if item.status.startswith("计算失败")]
    first = next((item for item in items if item.status.startswith(("不一致", "计算失败"))), None)
    code = error_code if error is not None else (3 if failures else (1 if mismatches else 0))
    report = {
        "layer": LAYER,
        "inputs": hashes,
        **body,
        "error": error,
        "summary": {
            "items": len(items),
            "mismatches": len(mismatches),
            "failures": len(failures),
            "unchecked_or_recorded": sum(1 for item in items if item.status.startswith(("未核对", "照录"))),
        },
        "first_mismatch": first,
        "exit_code": code,
        "exit_code_meanings": {str(number): text for number, text in EXIT_MEANINGS},
        "items": items,
    }
    return json_ready(report), code


def build_report(
    raw: dict[str, bytes], policy_raw: bytes | None, hashes: list[dict[str, Any]]
) -> tuple[dict[str, Any], int]:
    """运行全部核对并捕获输入错误（2）与计算失败（3）。"""
    try:
        body = evaluate(raw, policy_raw)
    except InputError as exc:
        return finish_report(
            {}, hashes, {"kind": "参数或输入错误", "message": exc.message, "location": exc.location}, 2
        )
    except ComputeFailure as exc:
        return finish_report({}, hashes, {"kind": "计算失败", "message": exc.message, "location": exc.location}, 3)
    return finish_report(body, hashes, None, 0)


def read_inputs(result_dir: Path, names: list[str] | tuple[str, ...]) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """边界函数：只读指定的允许文件，返回内容与字节数、SHA-256。"""
    raw: dict[str, bytes] = {}
    hashes: list[dict[str, Any]] = []
    for name in names:
        path = result_dir / name
        if not path.is_file():
            raise InputError("输入文件不存在", name)
        data = path.read_bytes()
        raw[name] = data
        hashes.append({"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return raw, hashes


def write_report(out_path: Path, payload: bytes) -> None:
    """唯一的写入位置：同目录临时文件写完后改名为 --out；任何失败都删除临时文件并抛出 ReportWriteError。"""
    written = 0
    temp_name: str | None = None
    try:
        handle, temp_name = tempfile.mkstemp(prefix=f".{out_path.name}.", suffix=".tmp", dir=out_path.parent)
        try:
            view = memoryview(payload)
            while written < len(payload):
                written += os.write(handle, view[written : written + 65536])
            os.fsync(handle)
        finally:
            os.close(handle)
        if os.path.exists(out_path):
            raise FileExistsError(f"--out 已存在：{out_path}")
        os.replace(temp_name, out_path)
    except Exception as exc:
        removed = "无临时文件"
        if temp_name is not None and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
                removed = "临时文件已删除"
            except OSError:
                removed = "临时文件删除失败，请人工清理"
        raise ReportWriteError(
            f"报告写入失败（{type(exc).__name__}: {exc}）；已写出 {written}/{len(payload)} 字节到临时文件 "
            f"{temp_name}；{removed}；目标文件 {out_path} 未生成"
        ) from exc


def parse_arguments(argv: list[str] | tuple[str, ...]) -> argparse.Namespace:
    """命令行参数。"""
    parser = argparse.ArgumentParser(description="波段预警 v2.0 独立回算·乙层（N2）")
    parser.add_argument("--result-dir", required=True, help="结果目录（evaluation_development）")
    parser.add_argument("--out", required=True, help="报告 JSON 路径（须不存在）")
    return parser.parse_args(list(argv))


def main(argv: list[str] | tuple[str, ...]) -> int:
    """命令行入口：读输入（结构 B 时另读 daily_policy）、核对、写报告，返回退出码。"""
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    try:
        args = parse_arguments(argv)
    except SystemExit:
        return 2
    out_path = Path(args.out)
    if out_path.exists():
        print(f"参数错误：--out 已存在：{out_path}", file=sys.stderr)
        return 2
    result_dir = Path(args.result_dir)
    try:
        raw, hashes = read_inputs(result_dir, BASE_FILES)
        policy_raw = None
        if needs_policy(raw):
            extra, extra_hashes = read_inputs(result_dir, (POLICY_FILE,))
            policy_raw = extra[POLICY_FILE]
            hashes.extend(extra_hashes)
        report, code = build_report(raw, policy_raw, hashes)
    except InputError as exc:
        report, code = finish_report(
            {}, [], {"kind": "参数或输入错误", "message": exc.message, "location": exc.location}, 2
        )
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        write_report(out_path, payload)
    except ReportWriteError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(f"{LAYER}：退出码 {code}；报告 {out_path}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
