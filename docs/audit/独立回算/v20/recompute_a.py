#!/usr/bin/env python3
"""波段预警 v2.0 独立回算·甲层（N1，recompute_a.py）。

由结果目录的逐日输出（daily_targets、daily_nav、daily_signals、daily_policy、r2_judgements、
segment_ledgers）独立重算汇总，与 selection.json、candidates_summary.csv、reference_summary.csv、
segments.csv、reconciliation.json、descriptive.json 的指定字段逐项比对，写出 JSON 报告。
规格：实施指令修订六第四节第 2、3、5、6、8 小节；算法依据与“登记来源 / 实现来源”标注见同目录 README.md。

用法：python recompute_a.py --result-dir <结果目录> --out <新报告.json>
退出码：0 全部一致；1 存在不一致；2 参数或输入错误；3 计算失败、非有限值或报告写入失败。
只读结果目录内允许读取的文件；只写 --out（已存在即失败；先写临时文件再改名）。
只用标准库，不导入 market_risk。甲层不能证明收益、状态、事件本身由价格正确生成，也不宣称完成乙层复核。
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
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 冻结登记（脚本内常量，不从项目读取）

LAYER = "甲层（recompute_a.py）"
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
ASSETS = ("SPX", "QQQ")
R1_RATIO = 0.5
R2_NUMERATOR = 3
R2_DENOMINATOR = 5
TIE_TOLERANCE = 1e-10
LAMBDA = 2.0
R2_CATEGORIES = ("左截断", "输入不足", "持续覆盖达标", "新提示达标", "提示中断", "迟到", "漏报")
LEDGER_CATEGORIES = ("窗口前已启动", "事件内提示", "低点后提示", "提前提示", "误报", "观察不完整")
COVERAGES = ("完整覆盖", "部分覆盖", "无收益区间", "不可计算")
OUTCOMES = ("选定", "计算失败", "缺值无法评价", "无合格候选")
RECON_KINDS = ("单一路径", "相对主参照")
SEGMENTS = (
    ("1999—2009", "1998-12-31", "2009-12-31"),
    ("2010—2016", "2009-12-31", "2016-12-30"),
    ("熊市一", "2000-03-24", "2002-10-09"),
    ("熊市二", "2007-10-09", "2009-03-09"),
)
TOL_LOG = 1e-10  # 对数净值汇总与对账（绝对）
TOL_WEALTH = 1e-12  # 逐日净值（混合绝对/相对）
TOL_FLOAT = 1e-12  # 其余 float64 字段（绝对；推导见 README）

INPUT_FILES = (
    "daily_targets.csv.gz",
    "daily_nav.csv.gz",
    "daily_signals.csv.gz",
    "daily_policy.csv.gz",
    "r2_judgements.csv.gz",
    "segment_ledgers.csv.gz",
    "window.json",
    "run_record.json",
    "descriptive.json",
    "selection.json",
    "candidates_summary.csv",
    "reference_summary.csv",
    "segments.csv",
    "reconciliation.json",
)
HEADERS = (
    ("daily_targets.csv.gz", ("date", "object", "position", "core", "leverage", "source", "cap_active")),
    ("daily_nav.csv.gz", ("date", "object", "path", "wealth", "return_to_date")),
    ("daily_signals.csv.gz", ("date", "object", "risk", "level", "c1", "c2", "all_valid")),
    (
        "daily_policy.csv.gz",
        ("date", "object", "position", "core", "leverage", "source", "cap_active", "wealth"),
    ),
    (
        "r2_judgements.csv.gz",
        (
            "object",
            "asset",
            "peak",
            "category",
            "first_new_day",
            "executable_day",
            "executable_offset",
            "peak_new_uncertain",
        ),
    ),
    ("segment_ledgers.csv.gz", ("object", "asset", "start", "end", "pre_window", "category")),
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
    (
        "segments.csv",
        (
            "object",
            "segment",
            "start",
            "end",
            "coverage",
            "first_nav_day",
            "last_nav_day",
            "returns_count",
            "drawdown_ratio",
            "undefined",
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


# ---------------------------------------------------------------- 登记派生


def candidate_keys() -> tuple[str, ...]:
    """27 个候选键，登记顺序先 K、再 θ_P、再 h。"""
    return tuple(f"K={k},θ_P={t},h={h}" for k in K_VALUES for t in THETA_TEXTS for h in H_VALUES)


def candidate_parameters(order: int) -> tuple[int, str, int]:
    """登记顺序号对应的 (K, θ_P 文本, h)。"""
    return K_VALUES[order // 9], THETA_TEXTS[(order // 3) % 3], H_VALUES[order % 3]


def expected_header(name: str) -> tuple[str, ...]:
    """CSV 文件的登记表头。"""
    for file_name, header in HEADERS:
        if file_name == name:
            return header
    raise InputError("未登记表头的文件", name)


# ---------------------------------------------------------------- 比对


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
    """精确相等（整数、布尔、日期、分类文字）；布尔与整数不混同。"""
    if script is None and project is None:
        return Item(item, obj, position, None, None, None, "一致（两者均为空）")
    if script is None or project is None:
        return Item(item, obj, position, script, project, None, "不一致：一方缺值")
    same = type(script) is type(project) and script == project
    return Item(item, obj, position, script, project, None, "一致" if same else "不一致：值不同")


def note_item(item: str, obj: str, position: str, project: Any, status: str) -> Item:
    """只照录、不判定的项（状态以“未核对”或“照录”开头，不计入不一致）。"""
    return Item(item, obj, position, None, project, None, status)


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
    """布尔文本 true/false；空为 None；其余为表外取值。"""
    if text is None:
        return None
    if text not in ("true", "false"):
        raise InputError(f"表外取值（布尔）：{text!r}", location)
    return text == "true"


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
    """JSON 数值（null 为 None；布尔不是数值）。"""
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


def jbool(value: Any, location: str) -> bool | None:
    """JSON 布尔（null 为 None）。"""
    if value is None:
        return None
    if not isinstance(value, bool):
        raise InputError(f"不是布尔：{value!r}", location)
    return value


# ---------------------------------------------------------------- 元数据


@dataclass(frozen=True)
class Window:
    """window.json 中用于确定执行日范围的字段。"""

    first_day: date
    last_day: date
    n: int
    j0: int
    e_index: int


@dataclass(frozen=True)
class StructureFields:
    """descriptive.json 的四个结构判定字段。"""

    averages: tuple[tuple[str, bool], ...]
    constants: tuple[tuple[str, bool], ...]


def parse_window(doc: Any) -> Window:
    """读 window.first_day、last_day、n、j0、e_index。"""
    block = jget(doc, "window", "window.json")
    location = "window.json window"
    n, j0, e_index = (jint(jget(block, key, location), f"{location}.{key}") for key in ("n", "j0", "e_index"))
    if n is None or j0 is None or e_index is None or n < 1:
        raise InputError("n、j0、e_index 须为整数且 n ≥ 1", location)
    first = to_date(jget(block, "first_day", location), f"{location}.first_day")
    last = to_date(jget(block, "last_day", location), f"{location}.last_day")
    return Window(first, last, n, j0, e_index)


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
    """averages[*].name/evaluable 与 constants[*].object/computed；类型、重复、名称集合逐项核对。"""
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


def determine_structure(
    flags: list[bool | None] | tuple[bool | None, ...], nav_rows: list[Row] | tuple[Row, ...]
) -> str:
    """结构 A：r1_computable 全真且有“一直持有”路径；结构 B：全假且 daily_nav 无行；其余停止。"""
    paths = sorted({str(row["path"]) for row in nav_rows})
    if flags and all(flag is True for flag in flags) and PATH_HOLD in paths:
        return "A"
    if flags and all(flag is False for flag in flags) and not nav_rows:
        return "B"
    raise InputError(
        f"结构不在已定义集合内：各候选 r1_computable = {list(flags)}；daily_nav 路径集合 = {paths}",
        "candidates_summary.csv 的 r1_computable；daily_nav.csv.gz 的 path",
    )


# ---------------------------------------------------------------- 算法（纯函数）


def wealth_chain(returns: list[float] | tuple[float, ...], location: str) -> tuple[float, ...]:
    """W_0 = 1，W_i = W_{i−1}·(1 + r_i)；非有限或非正即计算失败。"""
    wealth = [1.0]
    for index, value in enumerate(returns, start=1):
        current = wealth[-1] * (1.0 + value)
        if not math.isfinite(current) or current <= 0.0:
            raise ComputeFailure("计算失败：非有限值或非正净值", f"{location} 区间 {index}")
        wealth.append(current)
    return tuple(wealth)


def log_return_sum(returns: list[float] | tuple[float, ...], location: str) -> float:
    """Σ ln(1 + r_i)，用 math.fsum(math.log1p)。"""
    for index, value in enumerate(returns, start=1):
        if not math.isfinite(value) or value <= -1.0:
            raise ComputeFailure("计算失败：非有限值或非正净值", f"{location} 区间 {index}")
    return math.fsum(math.log1p(value) for value in returns)


def max_drawdown(wealth: list[float] | tuple[float, ...], location: str) -> float:
    """MDD = max_t (1 − W_t ÷ max_{s≤t} W_s)，含起点；序列须非空且为正的有限数。"""
    if not wealth:
        raise ComputeFailure("计算失败：净值序列为空", location)
    peak = wealth[0]
    worst = 0.0
    for value in wealth:
        if not math.isfinite(value) or value <= 0.0:
            raise ComputeFailure("计算失败：非有限值或非正净值", location)
        peak = max(peak, value)
        worst = max(worst, 1.0 - value / peak)
    return worst


@dataclass(frozen=True)
class R1Result:
    """R1：可计算、信号回撤、一直持有回撤、是否满足。"""

    computable: bool
    signal_drawdown: float | None
    hold_drawdown: float | None
    satisfied: bool | None


def r1_judgement(signal_drawdown: float | None, hold_drawdown: float | None) -> R1Result:
    """satisfied = mdd_signal ≤ 0.5 × mdd_hold；任一不可计算则不可计算。"""
    if signal_drawdown is None or hold_drawdown is None:
        return R1Result(False, None, None, None)
    return R1Result(True, signal_drawdown, hold_drawdown, signal_drawdown <= R1_RATIO * hold_drawdown)


@dataclass(frozen=True)
class R2Result:
    """R2：可计算、分母、达标、只计新提示、是否满足。"""

    computable: bool
    denominator: int
    achieved: int
    new_only: int
    meets: bool | None


def r2_counts(categories: list[str] | tuple[str, ...]) -> R2Result:
    """分母 = 事件数 − 左截断（含输入不足）；达标 = 持续覆盖达标 + 新提示达标；5·达标 ≥ 3·分母。"""
    denominator = len(categories) - sum(1 for value in categories if value == "左截断")
    if denominator == 0:
        return R2Result(False, 0, 0, 0, None)
    achieved = sum(1 for value in categories if value in ("持续覆盖达标", "新提示达标"))
    new_only = sum(1 for value in categories if value == "新提示达标")
    meets = R2_DENOMINATOR * achieved >= R2_NUMERATOR * denominator
    return R2Result(True, denominator, achieved, new_only, meets)


def exposure(core: float, leverage: float) -> float:
    """暴露 e = core + λ·leverage。"""
    return core + LAMBDA * leverage


@dataclass(frozen=True)
class SwitchSummary:
    """切换次数、调仓幅度（Σ|Δe|）、方向和（ΣΔe）。"""

    count: int
    magnitude: float
    change: float


def switch_summary(positions: list[str] | tuple[str, ...], exposures: list[float] | tuple[float, ...]) -> SwitchSummary:
    """按执行日升序，position 与前一执行日不同计一次切换；首行不计。"""
    deltas = [
        exposures[index] - exposures[index - 1]
        for index in range(1, len(positions))
        if positions[index] != positions[index - 1]
    ]
    return SwitchSummary(len(deltas), math.fsum(abs(value) for value in deltas), math.fsum(deltas))


@dataclass(frozen=True)
class StateStatistics:
    """四项状态统计及首信号日依赖标注。"""

    direct: int
    direct_days: tuple[date, ...]
    via: int
    via_days: tuple[date, ...]
    level1_intervals: int
    level2_intervals: int
    truncated: tuple[tuple[str, date, date, bool, bool], ...]
    first_day_dependency: str | None


def level_runs(
    positions: list[str] | tuple[str, ...], days: list[date] | tuple[date, ...], n: int
) -> tuple[int, int, tuple[tuple[str, date, date, bool, bool], ...]]:
    """对 targets[0..n−1] 的连续相同 position 分段 [s, e)；返回一级段数、二级段数与截断段。

    截断段元素为 (position, days[s], days[e], 左截断 s = 0, 右截断 e = n)，只收左或右截断的一级、二级段，
    先一级段后二级段、各按段序（答复单第 5 项；起止日期按第四节第 3 小节“段起点下标、段终点下标”对应执行日）。
    """
    level1 = level2 = 0
    found: list[tuple[str, date, date, bool, bool]] = []
    start = 0
    for index in range(1, n + 1):
        if index < n and positions[index] == positions[start]:
            continue
        value = positions[start]
        if value in ("一级", "二级"):
            level1 += int(value == "一级")
            level2 += int(value == "二级")
            if start == 0 or index == n:
                found.append((value, days[start], days[index], start == 0, index == n))
        start = index
    ordered = [entry for entry in found if entry[0] == "一级"] + [entry for entry in found if entry[0] == "二级"]
    return level1, level2, tuple(ordered)


def state_statistics(
    signal_days: list[date] | tuple[date, ...],
    risks: list[str] | tuple[str, ...],
    positions: list[str] | tuple[str, ...],
    execution_days: list[date] | tuple[date, ...],
    project_direct_days: list[date] | tuple[date, ...],
    project_via_days: list[date] | tuple[date, ...],
) -> StateStatistics:
    """自第 2 个信号日起精确重算转换；首信号日为二级时按项目日期列表判定其所取前一日状态并标注。"""
    n = len(positions) - 1
    direct = [signal_days[k] for k in range(1, n + 1) if risks[k] == "二级" and risks[k - 1] == "正常"]
    via = [signal_days[k] for k in range(1, n + 1) if risks[k] == "二级" and risks[k - 1] == "一级"]
    dependency = None
    if risks[0] == "二级":
        first = signal_days[0]
        dependency = f"依赖窗口外状态，未独立核对：首个信号日 {first.isoformat()} 为二级，其前一日状态不在逐日输出中"
        if first in project_direct_days:
            direct.insert(0, first)
        elif first in project_via_days:
            via.insert(0, first)
    level1, level2, truncated = level_runs(positions, execution_days, n)
    return StateStatistics(len(direct), tuple(direct), len(via), tuple(via), level1, level2, truncated, dependency)


@dataclass(frozen=True)
class SegmentMap:
    """分段映射：覆盖、净值端点下标、端点日期、区间数。"""

    coverage: str
    a: int | None
    b: int | None
    first_nav_day: date | None
    last_nav_day: date | None
    returns_count: int


def segment_mapping(days: list[date] | tuple[date, ...], start: date, end: date) -> SegmentMap:
    """i1 = max(bisect_right(days, start), 1)，i2 = bisect_right(days, end) − 1；a = i1 − 1，b = i2。"""
    i1 = max(bisect_right(days, start), 1)
    i2 = bisect_right(days, end) - 1
    if i2 < i1:
        return SegmentMap("无收益区间", None, None, None, None, 0)
    a, b = i1 - 1, i2
    coverage = "完整覆盖" if start >= days[0] and end <= days[-1] else "部分覆盖"
    return SegmentMap(coverage, a, b, days[a], days[b], b - a)


def segment_ratio(
    signal_wealth: list[float] | tuple[float, ...],
    hold_wealth: list[float] | tuple[float, ...],
    a: int,
    b: int,
    location: str,
) -> tuple[float | None, bool]:
    """MDD(signal[a..b]) ÷ MDD(hold[a..b])（切片以段首净值为起点）；分母为 0 时比值空、无定义为真。"""
    hold = max_drawdown(hold_wealth[a : b + 1], location)
    if hold == 0.0:
        return None, True
    return max_drawdown(signal_wealth[a : b + 1], location) / hold, False


def policy_minus_signal(policy_end: float | None, signal_end: float | None, location: str) -> float | None:
    """ln(执行政策末净值) − ln(信号模拟末净值)；任一不可得为空。"""
    if policy_end is None or signal_end is None:
        return None
    if not (math.isfinite(policy_end) and math.isfinite(signal_end)) or policy_end <= 0 or signal_end <= 0:
        raise ComputeFailure("计算失败：非有限值或非正净值", location)
    return math.log(policy_end) - math.log(signal_end)


@dataclass(frozen=True)
class CandidateRecord:
    """选择程序的一条候选记录。"""

    order: int
    key: str
    failed: bool
    r1: bool | None
    r2_spx: bool | None
    r2_qqq: bool | None
    log_wealth: float | None
    switches: int


@dataclass(frozen=True)
class SelectionResult:
    """选择结果：出口、可行集、最大值、并列组、选定。"""

    outcome: str
    feasible: tuple[str, ...]
    maximum: float | None
    tied: tuple[str, ...]
    selected: str | None


def select_candidate(records: list[CandidateRecord] | tuple[CandidateRecord, ...]) -> SelectionResult:
    """出口优先级：计算失败 > 缺值无法评价 > 无合格候选 > 选定；并列 ln W ≥ M − 1e-10，按 (切换, 顺序) 取最小。"""
    if any(record.failed for record in records):
        return SelectionResult("计算失败", (), None, (), None)
    if any(None in (r.r1, r.r2_spx, r.r2_qqq, r.log_wealth) for r in records):
        return SelectionResult("缺值无法评价", (), None, (), None)
    feasible = [r for r in records if r.r1 and r.r2_spx and r.r2_qqq]
    if not feasible:
        return SelectionResult("无合格候选", (), None, (), None)
    maximum = max(float(r.log_wealth or 0.0) for r in feasible)
    tied = [r for r in feasible if float(r.log_wealth or 0.0) >= maximum - TIE_TOLERANCE]
    chosen = min(tied, key=lambda r: (r.switches, r.order))
    return SelectionResult("选定", tuple(r.key for r in feasible), maximum, tuple(r.key for r in tied), chosen.key)


# ---------------------------------------------------------------- 输入整理


@dataclass(frozen=True)
class Inputs:
    """解析后的全部输入。"""

    tables: dict[str, list[Row]]
    docs: dict[str, Any]
    window: Window
    fields: StructureFields
    structure: str


@dataclass(frozen=True)
class Chain:
    """一条（对象，路径）的净值链。"""

    obj: str
    path: str
    dates: tuple[date, ...]
    returns: tuple[float, ...]
    script_wealth: tuple[float, ...]
    project_wealth: tuple[float | None, ...]
    drawdown: float


def parse_inputs(raw: dict[str, bytes]) -> Inputs:
    """解析全部输入文件并判定结构。"""
    tables = {name: parse_csv_table(name, raw[name]) for name in INPUT_FILES if name.endswith((".csv", ".gz"))}
    docs = {name: parse_json_doc(name, raw[name]) for name in INPUT_FILES if name.endswith(".json")}
    window = parse_window(docs["window.json"])
    fields = parse_structure_fields(docs["descriptive.json"], candidate_keys())
    flags = [
        to_bool(row["r1_computable"], where("candidates_summary.csv", row)) for row in tables["candidates_summary.csv"]
    ]
    structure = determine_structure(flags, tables["daily_nav.csv.gz"])
    return Inputs(tables, docs, window, fields, structure)


def first_bad_date(actual: list[date] | tuple[date, ...], expected: list[date] | tuple[date, ...]) -> date | None:
    """两个升序日期序列的首个不符日期。"""
    for left, right in zip(actual, expected, strict=False):
        if left != right:
            return min(left, right)
    if len(actual) > len(expected):
        return actual[len(expected)]
    if len(actual) < len(expected):
        return expected[len(actual)]
    return None


def require_dates(location: str, dates: list[date] | tuple[date, ...], expected: list[date] | tuple[date, ...]) -> None:
    """日期集合须恰为 expected：多一行、少一行、日期不在集合内、同日重复均为输入错误。"""
    ordered = sorted(dates)
    for index in range(1, len(ordered)):
        if ordered[index] == ordered[index - 1]:
            raise InputError(f"行数或日期不符：同日重复 {ordered[index].isoformat()}", location)
    bad = first_bad_date(ordered, expected)
    if bad is not None:
        raise InputError(
            f"行数或日期不符：首个不符日期 {bad.isoformat()}（实际 {len(ordered)} 行，应为 {len(expected)} 行）",
            location,
        )


def derive_days(targets: list[Row] | tuple[Row, ...], window: Window) -> list[date]:
    """执行日序列：主参照的 daily_targets 日期，须从 first_day 到 last_day、共 n + 1 个、无重复。"""
    table = "daily_targets.csv.gz"
    dates = sorted(to_date(row["date"], where(table, row)) for row in targets if row["object"] == REFERENCE)
    location = f"{table} 对象={REFERENCE}"
    if not dates:
        raise InputError("行数或日期不符：缺少主参照的全部行", location)
    if len(set(dates)) != len(dates) or len(dates) != window.n + 1:
        raise InputError(f"行数或日期不符：主参照有 {len(dates)} 行，应为 n + 1 = {window.n + 1} 行", location)
    if dates[0] != window.first_day or dates[-1] != window.last_day:
        raise InputError("行数或日期不符：首末执行日与 window.first_day/last_day 不符", location)
    return dates


def signal_axis(signals: list[Row] | tuple[Row, ...], days: list[date] | tuple[date, ...]) -> list[date]:
    """信号日：首个为执行日 days[0] 的前一交易日（取自主参照，须早于 days[0]），其后为 days[0..n−1]。"""
    table = "daily_signals.csv.gz"
    dates = [to_date(row["date"], where(table, row)) for row in signals if row["object"] == REFERENCE]
    if not dates:
        raise InputError("行数或日期不符：缺少主参照的全部行", f"{table} 对象={REFERENCE}")
    first = min(dates)
    if first >= days[0]:
        raise InputError("行数或日期不符：首个信号日不早于首个执行日", f"{table} 对象={REFERENCE}")
    return [first, *days[:-1]]


def index_by_object(
    table: str,
    rows: list[Row] | tuple[Row, ...],
    objects: list[str] | tuple[str, ...],
    expected: list[date] | tuple[date, ...],
    prefix: bool,
) -> tuple[dict[str, list[Row]], list[Item]]:
    """按对象分组并核对日期；O_d 以外的对象记不一致，O_d 成员缺行为输入错误。"""
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
        found = grouped.get(obj, [])
        location = f"{table} 对象={obj}"
        if not found:
            raise InputError("行数或日期不符：缺少该对象的全部行", location)
        dates = [to_date(row["date"], where(table, row)) for row in found]
        require_dates(location, dates, expected[: len(dates)] if prefix else expected)
        result[obj] = sorted(found, key=lambda row: str(row["date"]))
    return result, items


def index_nav(
    rows: list[Row] | tuple[Row, ...],
    legal: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    days: list[date] | tuple[date, ...],
) -> tuple[dict[tuple[str, str], list[Row]], list[Item]]:
    """daily_nav 按（对象，路径）分组；表外组合记不一致；合法组合缺行或日期不符为输入错误。"""
    table = "daily_nav.csv.gz"
    grouped: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        path = to_enum(row["path"], NAV_PATHS, where(table, row))
        grouped.setdefault((need(row, "object", table), path), []).append(row)
    items = [
        Item(
            "合法组合",
            obj,
            f"{table} 路径={path}",
            None,
            f"{len(found)} 行",
            None,
            "不一致：对象或路径不在合法组合表内",
        )
        for (obj, path), found in sorted(grouped.items())
        if (obj, path) not in legal
    ]
    result: dict[tuple[str, str], list[Row]] = {}
    for obj, path in legal:
        found = grouped.get((obj, path), [])
        location = f"{table} 对象={obj} 路径={path}"
        if not found:
            raise InputError("行数或日期不符：缺少必需的（对象，路径）全部行", location)
        require_dates(location, [to_date(row["date"], where(table, row)) for row in found], days)
        result[(obj, path)] = sorted(found, key=lambda row: str(row["date"]))
    return result, items


# ---------------------------------------------------------------- 对象集合


@dataclass(frozen=True)
class ObjectSets:
    """C、E、O_d、K 及项目出口（结构核对用）。"""

    candidates: tuple[str, ...]
    evaluable: tuple[str, ...]
    daily: tuple[str, ...]
    constants: tuple[str, ...]
    constant_sources: tuple[tuple[str, str], ...]


def constant_members(
    fields: StructureFields, structure: str, outcome: str, selected: str | None
) -> tuple[list[tuple[str, str]], list[Item]]:
    """K：computed 为真且身份与条件核对通过者；身份或条件不符记不一致且不计入 K。"""
    location = "descriptive.json constants"
    (obj0, computed0), (obj1, computed1) = fields.constants
    expected1 = selected if outcome == "选定" else SELECTED_PLACEHOLDER
    items = [
        compare_exact("恒定仓位身份", obj0, f"{location}[0].object", REFERENCE, obj0),
        compare_exact("恒定仓位身份", obj1, f"{location}[1].object（出口 {outcome}）", expected1, obj1),
    ]
    if outcome != "选定":
        items.append(compare_exact("恒定仓位条件", obj1, f"{location}[1].computed（出口非选定）", False, computed1))
    members: list[tuple[str, str]] = []
    checks = ((obj0, computed0, obj0 == REFERENCE), (obj1, computed1, obj1 == expected1 and outcome == "选定"))
    for index, (obj, computed, identity_ok) in enumerate(checks):
        if computed and structure == "B":
            items.append(
                Item(
                    "恒定仓位条件",
                    obj,
                    f"{location}[{index}].computed",
                    False,
                    True,
                    None,
                    "不一致：结构 B 下 computed 为真",
                )
            )
        elif computed and identity_ok:
            members.append((CONSTANT_PREFIX + obj, obj))
    return members, items


def object_sets(inputs: Inputs) -> tuple[ObjectSets, list[Item]]:
    """由冻结登记与结构判定字段确定 C、E、O_d、K。"""
    selection = inputs.docs["selection.json"]
    outcome = to_enum(jget(selection, "outcome", "selection.json"), OUTCOMES, "selection.json outcome")
    selected = jget(selection, "selected", "selection.json")
    if selected is not None and not isinstance(selected, str):
        raise InputError("selected 不是字符串或 null", "selection.json selected")
    keys = candidate_keys()
    evaluable = tuple(name for name in AVERAGE_NAMES if dict(inputs.fields.averages)[name])
    members, items = constant_members(inputs.fields, inputs.structure, outcome, selected)
    sets = ObjectSets(
        keys,
        evaluable,
        (*keys, REFERENCE, *evaluable),
        tuple(name for name, _ in members),
        tuple(members),
    )
    return sets, items


def legal_nav_pairs(sets: ObjectSets, structure: str) -> list[tuple[str, str]]:
    """合法组合表：结构 A 下信号、政策路径属 O_d，一直持有属 H，恒定仓位属 K；结构 B 下无。"""
    if structure == "B":
        return []
    pairs = [(obj, path) for obj in sets.daily for path in (PATH_SIGNAL, PATH_POLICY)]
    pairs.append((HOLD, PATH_HOLD))
    pairs.extend((name, PATH_CONSTANT) for name in sets.constants)
    return pairs


# ---------------------------------------------------------------- 甲层核对：净值链与对账


def build_chain(obj: str, path: str, rows: list[Row] | tuple[Row, ...]) -> tuple[Chain, list[Item]]:
    """独立净值链：W_0 = 1，逐行比较 wealth；首行 wealth 须为 1.0、return_to_date 须为空。"""
    table = "daily_nav.csv.gz"
    location = f"{table} 对象={obj} 路径={path}"
    first = rows[0]
    first_wealth = to_float(first["wealth"], where(table, first))
    items = [
        Item(
            "首行净值",
            obj,
            f"{location} 日期={first['date']}",
            1.0,
            first_wealth,
            None,
            "一致" if first_wealth == 1.0 else "不一致：首行净值不为 1",
        ),
        Item(
            "首行 return_to_date",
            obj,
            f"{location} 日期={first['date']}",
            None,
            first["return_to_date"],
            None,
            "一致" if first["return_to_date"] is None else "不一致：首行 return_to_date 非空",
        ),
    ]
    returns = []
    for row in rows[1:]:
        value = to_float(row["return_to_date"], where(table, row))
        if value is None:
            raise InputError("非首行 return_to_date 为空", where(table, row))
        returns.append(value)
    script = wealth_chain(returns, location)
    project = tuple(to_float(row["wealth"], where(table, row)) for row in rows)
    dates = tuple(to_date(row["date"], where(table, row)) for row in rows)
    for day, mine, theirs in zip(dates, script, project, strict=True):
        items.append(
            compare_float("逐日净值", obj, f"{location} 日期={day.isoformat()}", mine, theirs, TOL_WEALTH, True)
        )
    chain = Chain(obj, path, dates, tuple(returns), script, project, max_drawdown(script, location))
    return chain, items


def project_log_end(chain: Chain) -> float | None:
    """项目末行 wealth 的对数；缺值、非有限或非正时为空。"""
    end = chain.project_wealth[-1]
    if end is None or not math.isfinite(end) or end <= 0.0:
        return None
    return math.log(end)


@dataclass(frozen=True)
class ReconRow:
    """reconciliation.json 的一行。"""

    kind: str
    obj: str
    path: Any
    difference: float | None
    tolerance: float | None
    passed: bool | None


def parse_reconciliation(doc: Any) -> list[ReconRow]:
    """解析 reconciliation.rows。"""
    rows = jget(doc, "rows", "reconciliation.json")
    if not isinstance(rows, list):
        raise InputError("rows 不是数组", "reconciliation.json")
    parsed = []
    for index, row in enumerate(rows):
        location = f"reconciliation.json rows[{index}]"
        kind = to_enum(jget(row, "kind", location), RECON_KINDS, location)
        obj = jget(row, "object", location)
        if not isinstance(obj, str):
            raise InputError("object 不是字符串", location)
        parsed.append(
            ReconRow(
                kind,
                obj,
                jget(row, "path", location),
                jfloat(jget(row, "difference", location), location),
                jfloat(jget(row, "tolerance", location), location),
                jbool(jget(row, "passed", location), location),
            )
        )
    return parsed


def recon_row_items(label: str, obj: str, script_difference: float | None, row: ReconRow) -> list[Item]:
    """对账行：difference（1e-12 绝对）、tolerance（须为 1e-10）、passed。"""
    passed = None if script_difference is None else script_difference <= TOL_LOG
    return [
        compare_float(
            f"{label} difference", obj, f"reconciliation {label}", script_difference, row.difference, TOL_FLOAT, False
        ),
        compare_exact(f"{label} tolerance", obj, f"reconciliation {label}", TOL_LOG, row.tolerance),
        compare_exact(f"{label} passed", obj, f"reconciliation {label}", passed, row.passed),
    ]


def recon_key(obj: str, path: str, constant_sources: dict[str, str]) -> tuple[str, str]:
    """（对象，路径）在 reconciliation.json 中的键。恒定仓位路径：daily_nav 的对象名为“恒定仓位：<对象>”（全角冒号），
    对账行的对象名为原对象名（第四次交付指令第二节，development_compare.py 第 569—571 行）；
    constant_sources 只含前缀精确匹配、
    去前缀后属于 constants[*].object 且 computed 为真并通过身份核对的 K 成员。其他路径对象名不变。"""
    if path == PATH_CONSTANT:
        return constant_sources[obj], path
    return obj, path


def check_single_reconciliation(
    chains: dict[tuple[str, str], Chain],
    rows: list[ReconRow] | tuple[ReconRow, ...],
    constant_sources: dict[str, str],
) -> list[Item]:
    """单一路径：Σlog1p、独立 ln W_n、项目 ln wealth_末两两 ≤ 1e-10；重算 difference 与项目比较。
    对账行以（object，path）定位，恒定仓位路径用原对象名（见 recon_key）。"""
    keyed: dict[tuple[str, Any], ReconRow] = {}
    items: list[Item] = []
    for row in rows:
        if row.kind != "单一路径":
            continue
        key = (row.obj, row.path)
        if key in keyed:
            items.append(Item("对账行", row.obj, f"单一路径 路径={row.path}", None, "重复", None, "不一致：重复对账行"))
        keyed[key] = row
    for key, chain in chains.items():
        obj, path = key
        location = f"对象={obj} 路径={path}"
        sum_log = log_return_sum(chain.returns, location)
        own = math.log(chain.script_wealth[-1])
        theirs = project_log_end(chain)
        items.append(compare_float("对账 Σlog1p 与独立 ln W", obj, location, sum_log, own, TOL_LOG, False))
        items.append(compare_float("对账 Σlog1p 与项目 ln W", obj, location, sum_log, theirs, TOL_LOG, False))
        items.append(compare_float("对账 独立 ln W 与项目 ln W", obj, location, own, theirs, TOL_LOG, False))
        expected_key = recon_key(obj, path, constant_sources)
        row = keyed.pop(expected_key, None)
        if row is None:
            position = f"单一路径 对账行对象={expected_key[0]} 路径={path}"
            items.append(Item("对账行", obj, position, "应存在", None, None, "不一致：缺少对账行"))
            continue
        difference = None if theirs is None else abs(sum_log - theirs)
        items.extend(recon_row_items(f"单一路径 {path}", obj, difference, row))
    items.extend(
        Item("对账行", obj, f"单一路径 路径={path}", None, "存在", None, "不一致：多余对账行") for (obj, path) in keyed
    )
    return items


def relative_difference(candidate: Chain, reference: Chain) -> float | None:
    """abs(fsum(log1p(r_c) − log1p(r_ref)) − (ln W_c,末 − ln W_ref,末))，W 取项目末行。"""
    own_end, ref_end = project_log_end(candidate), project_log_end(reference)
    if own_end is None or ref_end is None:
        return None
    check_log_domain(candidate.returns + reference.returns)
    pairs = zip(candidate.returns, reference.returns, strict=True)
    total = math.fsum(math.log1p(mine) - math.log1p(theirs) for mine, theirs in pairs)
    return abs(total - (own_end - ref_end))


def check_log_domain(values: list[float] | tuple[float, ...]) -> None:
    """对数前的定义域检查（1 + r 须为正的有限数）。"""
    for value in values:
        if not math.isfinite(value) or value <= -1.0:
            raise ComputeFailure("计算失败：非有限值或非正净值", "相对主参照对账")


def check_relative_reconciliation(
    chains: dict[tuple[str, str], Chain], rows: list[ReconRow] | tuple[ReconRow, ...], keys: list[str] | tuple[str, ...]
) -> list[Item]:
    """相对主参照：先核对执行日序列相同；difference ≤ 1e-10，且与项目值之差 ≤ 1e-12。"""
    keyed: dict[str, ReconRow] = {}
    items: list[Item] = []
    for row in rows:
        if row.kind == "相对主参照":
            if row.obj in keyed:
                items.append(Item("对账行", row.obj, "相对主参照", None, "重复", None, "不一致：重复对账行"))
            keyed[row.obj] = row
    reference = chains.get((REFERENCE, PATH_SIGNAL))
    for key in keys:
        candidate = chains.get((key, PATH_SIGNAL))
        if candidate is None or reference is None:
            continue
        if candidate.dates != reference.dates:
            items.append(
                Item("相对主参照执行日", key, "daily_nav 信号模拟", None, None, None, "不一致：执行日序列与主参照不同")
            )
            continue
        difference = relative_difference(candidate, reference)
        if difference is None:
            status = "不一致：一方缺值（项目末行 wealth 不可用）"
        else:
            status = "一致" if difference <= TOL_LOG else "不一致：相对主参照对账超出 1e-10"
        items.append(Item("相对主参照 difference ≤ 1e-10", key, "独立重算", difference, TOL_LOG, None, status))
        row = keyed.pop(key, None)
        if row is None:
            items.append(Item("对账行", key, "相对主参照", "应存在", None, None, "不一致：缺少对账行"))
            continue
        items.extend(recon_row_items("相对主参照", key, difference, row))
    items.extend(Item("对账行", obj, "相对主参照", None, "存在", None, "不一致：多余对账行") for obj in keyed)
    return items


# ---------------------------------------------------------------- 甲层核对：逐日派生量


@dataclass(frozen=True)
class Derived:
    """由逐日输出独立重算的全部派生量。"""

    chains: dict[tuple[str, str], Chain]
    r1: dict[str, R1Result]
    r2: dict[tuple[str, str], R2Result]
    switches: dict[str, SwitchSummary]
    states: dict[str, StateStatistics]
    policy_diff: dict[str, float | None]
    policy_last_items: list[Item]


def chain_drawdown(chains: dict[tuple[str, str], Chain], obj: str, path: str) -> float | None:
    """某（对象，路径）的独立最大回撤；不可计算为空。"""
    chain = chains.get((obj, path))
    return None if chain is None else chain.drawdown


def r2_by_candidate(
    rows: list[Row] | tuple[Row, ...], keys: list[str] | tuple[str, ...]
) -> tuple[dict[tuple[str, str], R2Result], list[Item]]:
    """从 r2_judgements.category 计数；类别与资产须在登记全集内；对象须在候选集合内。"""
    table = "r2_judgements.csv.gz"
    grouped: dict[tuple[str, str], list[str]] = {}
    items: list[Item] = []
    for row in rows:
        obj = need(row, "object", table)
        asset = to_enum(row["asset"], ASSETS, where(table, row))
        category = to_enum(row["category"], R2_CATEGORIES, where(table, row))
        if obj not in keys:
            items.append(Item("对象集合", obj, where(table, row), None, obj, None, "不一致：对象不在候选集合 C 内"))
            continue
        grouped.setdefault((obj, asset), []).append(category)
    result = {(key, asset): r2_counts(grouped.get((key, asset), [])) for key in keys for asset in ASSETS}
    return result, items


def switches_by_object(targets: dict[str, list[Row]]) -> dict[str, SwitchSummary]:
    """每个 O_d 对象的切换次数与调仓幅度（暴露由行内 core、leverage 计算）。"""
    table = "daily_targets.csv.gz"
    result = {}
    for obj, rows in targets.items():
        positions = [to_enum(row["position"], POSITIONS, where(table, row)) for row in rows]
        exposures = []
        for row in rows:
            core = to_float(need(row, "core", table), where(table, row))
            leverage = to_float(need(row, "leverage", table), where(table, row))
            exposures.append(exposure(float(core or 0.0), float(leverage or 0.0)))
        result[obj] = switch_summary(positions, exposures)
    return result


STATE_KEYS = (
    "direct_level2",
    "direct_level2_days",
    "via_level1",
    "via_level1_days",
    "level1_intervals",
    "level2_intervals",
    "truncated",
    "start_note",
)


def project_state_entries(doc: Any, keys: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """descriptive.state_statistics：对象恰为 27 个候选键 + 主参照（登记顺序，主参照最后），每项必需字段齐全；
    对象多、少、顺序不符或必需项缺失为输入错误（答复单第 6 项）。"""
    location = "descriptive.json state_statistics"
    entries = jget(doc, "state_statistics", "descriptive.json")
    if not isinstance(entries, list):
        raise InputError("state_statistics 不是数组", location)
    objects = [entry.get("object") if isinstance(entry, dict) else None for entry in entries]
    expected = [*keys, REFERENCE]
    if objects != expected:
        raise InputError(f"对象集合不符：应恰为 27 个候选键 + 主参照（按登记顺序），实际 {objects}", location)
    for obj, entry in zip(objects, entries, strict=True):
        missing = [key for key in STATE_KEYS if key not in entry]
        if missing:
            raise InputError(f"必需项缺失：{missing}", f"{location} 对象={obj}")
    return dict(zip(expected, entries, strict=True))


def date_list(value: Any, location: str) -> list[date]:
    """JSON 日期数组。"""
    if not isinstance(value, list):
        raise InputError("不是数组", location)
    return [to_date(text if isinstance(text, str) else None, location) for text in value]


def states_by_object(
    signals: dict[str, list[Row]], targets: dict[str, list[Row]], project: dict[str, dict[str, Any]]
) -> dict[str, StateStatistics]:
    """每个 O_d 对象的四项状态统计。"""
    result = {}
    for obj, rows in targets.items():
        signal_rows = signals[obj]
        days = [to_date(row["date"], where("daily_signals.csv.gz", row)) for row in signal_rows]
        risks = [to_enum(row["risk"], POSITIONS, where("daily_signals.csv.gz", row)) for row in signal_rows]
        for row in signal_rows:
            to_bool(row["all_valid"], where("daily_signals.csv.gz", row))
        positions = [to_enum(row["position"], POSITIONS, where("daily_targets.csv.gz", row)) for row in rows]
        execution_days = [to_date(row["date"], where("daily_targets.csv.gz", row)) for row in rows]
        entry = project.get(obj, {})
        location = f"descriptive.json state_statistics 对象={obj}"
        direct_days = date_list(entry.get("direct_level2_days", []), location)
        via_days = date_list(entry.get("via_level1_days", []), location)
        result[obj] = state_statistics(days, risks, positions, execution_days, direct_days, via_days)
    return result


def policy_differences(
    chains: dict[tuple[str, str], Chain],
    policy: dict[str, list[Row]],
    objects: list[str] | tuple[str, ...],
    structure: str,
) -> tuple[dict[str, float | None], list[Item]]:
    """执行政策与信号模拟之差；政策末净值取 daily_nav 政策路径末行，并核对与 daily_policy 末行相等。"""
    result: dict[str, float | None] = {}
    items: list[Item] = []
    for obj in objects:
        signal = chains.get((obj, PATH_SIGNAL))
        policy_chain = chains.get((obj, PATH_POLICY))
        if structure == "B" or signal is None or policy_chain is None:
            result[obj] = None
            continue
        nav_end = policy_chain.project_wealth[-1]
        last = policy[obj][-1]
        table_end = to_float(last["wealth"], where("daily_policy.csv.gz", last))
        items.append(
            compare_exact(
                "政策末净值（daily_nav 与 daily_policy）", obj, f"daily_policy 日期={last['date']}", nav_end, table_end
            )
        )
        result[obj] = policy_minus_signal(nav_end, signal.project_wealth[-1], f"对象={obj}")
    return result, items


def derive_all(inputs: Inputs, sets: ObjectSets, days: list[date]) -> tuple[Derived, list[Item]]:
    """逐日表索引、净值链、R1、R2、切换、状态统计与政策差额。"""
    tables = inputs.tables
    items: list[Item] = []
    axis = signal_axis(tables["daily_signals.csv.gz"], days)
    signals, extra = index_by_object("daily_signals.csv.gz", tables["daily_signals.csv.gz"], sets.daily, axis, False)
    items.extend(extra)
    targets, extra = index_by_object("daily_targets.csv.gz", tables["daily_targets.csv.gz"], sets.daily, days, False)
    items.extend(extra)
    policy, extra = index_by_object(
        "daily_policy.csv.gz", tables["daily_policy.csv.gz"], sets.daily, days, inputs.structure == "B"
    )
    items.extend(extra)
    items.extend(check_policy_wealth(policy, inputs.structure))
    nav, extra = index_nav(tables["daily_nav.csv.gz"], legal_nav_pairs(sets, inputs.structure), days)
    items.extend(extra)
    chains: dict[tuple[str, str], Chain] = {}
    for (obj, path), rows in nav.items():
        chain, chain_items = build_chain(obj, path, rows)
        chains[(obj, path)] = chain
        items.extend(chain_items)
    hold = chain_drawdown(chains, HOLD, PATH_HOLD)
    r1 = {key: r1_judgement(chain_drawdown(chains, key, PATH_SIGNAL), hold) for key in sets.candidates}
    r2, extra = r2_by_candidate(tables["r2_judgements.csv.gz"], sets.candidates)
    items.extend(extra)
    states = states_by_object(signals, targets, project_state_entries(inputs.docs["descriptive.json"], sets.candidates))
    differences, extra = policy_differences(chains, policy, sets.daily, inputs.structure)
    derived = Derived(chains, r1, r2, switches_by_object(targets), states, differences, extra)
    return derived, items


def check_policy_wealth(policy: dict[str, list[Row]], structure: str) -> list[Item]:
    """daily_policy.wealth：结构 A 全部非空；结构 B 非空行构成前缀且各对象非空行数相同。"""
    table = "daily_policy.csv.gz"
    counts: dict[str, int] = {}
    for obj, rows in policy.items():
        filled = [row["wealth"] is not None for row in rows]
        known = sum(filled)
        if structure == "A" and known != len(rows):
            raise InputError("行数或日期不符：结构 A 下 wealth 有空值", f"{table} 对象={obj}")
        if not all(filled[:known]):
            bad = rows[filled.index(False)]
            raise InputError(
                f"行数或日期不符：wealth 非空行不构成前缀，首个不符日期 {bad['date']}", f"{table} 对象={obj}"
            )
        counts[obj] = known
    if structure == "A" or len(set(counts.values())) <= 1:
        return []
    return [
        Item("结构 B wealth 非空行数", obj, table, None, count, None, "不一致：各对象 wealth 非空行数不相同")
        for obj, count in sorted(counts.items())
    ]


# ---------------------------------------------------------------- 甲层核对：汇总比对


def candidate_rows(
    table: str, rows: list[Row] | tuple[Row, ...], keys: list[str] | tuple[str, ...]
) -> tuple[dict[str, Row], list[Item]]:
    """候选汇总表按对象索引；顺序、名称、个数与登记逐项核对。"""
    items = [
        compare_exact(
            "候选记录集合",
            keys[index],
            f"{table} 第 {index + 1} 条",
            (index, keys[index]),
            (to_int(row["order"], where(table, row)), row["object"]),
        )
        if index < len(keys)
        else Item(
            "候选记录集合",
            str(row["object"]),
            f"{table} 第 {index + 1} 条",
            None,
            row["object"],
            None,
            "不一致：多余记录",
        )
        for index, row in enumerate(rows)
    ]
    items.extend(
        Item(
            "候选记录集合",
            keys[index],
            f"{table} 第 {index + 1} 条",
            (index, keys[index]),
            None,
            None,
            "不一致：一方缺值",
        )
        for index in range(len(rows), len(keys))
    )
    indexed: dict[str, Row] = {}
    for row in rows:
        if row["object"] in keys and row["object"] not in indexed:
            indexed[str(row["object"])] = row
    return indexed, items


def compare_candidate_core(key: str, row: Row, order: int, derived: Derived) -> list[Item]:
    """candidates_summary 的参数、净值、回撤、切换与 R1 列。"""
    table = "candidates_summary.csv"
    at = where(table, row)
    k, theta, h = candidate_parameters(order)
    signal = derived.chains.get((key, PATH_SIGNAL))
    policy = derived.chains.get((key, PATH_POLICY))
    r1 = derived.r1[key]
    switch = derived.switches[key]
    theta_ok = decimal_equal(row["theta_p"], theta)
    items = [
        compare_exact("k", key, at, k, to_int(row["k"], at)),
        Item("theta_p", key, at, theta, row["theta_p"], None, "一致" if theta_ok else "不一致：值不同"),
        compare_exact("h", key, at, h, to_int(row["h"], at)),
        compare_float(
            "signal_log_wealth", key, at, log_end(signal), to_float(row["signal_log_wealth"], at), TOL_LOG, False
        ),
        compare_float(
            "signal_max_drawdown", key, at, drawdown(signal), to_float(row["signal_max_drawdown"], at), TOL_FLOAT, False
        ),
        compare_float(
            "policy_log_wealth", key, at, log_end(policy), to_float(row["policy_log_wealth"], at), TOL_LOG, False
        ),
        compare_float(
            "policy_max_drawdown", key, at, drawdown(policy), to_float(row["policy_max_drawdown"], at), TOL_FLOAT, False
        ),
        compare_exact("switches", key, at, switch.count, to_int(row["switches"], at)),
        compare_float(
            "exposure_magnitude", key, at, switch.magnitude, to_float(row["exposure_magnitude"], at), TOL_FLOAT, False
        ),
        compare_exact("r1_computable", key, at, r1.computable, to_bool(row["r1_computable"], at)),
        compare_float(
            "r1_signal_drawdown", key, at, r1.signal_drawdown, to_float(row["r1_signal_drawdown"], at), TOL_FLOAT, False
        ),
        compare_exact(
            "r1_signal_drawdown 等于 signal_max_drawdown",
            key,
            at,
            to_float(row["signal_max_drawdown"], at),
            to_float(row["r1_signal_drawdown"], at),
        ),
        compare_float(
            "r1_hold_drawdown", key, at, r1.hold_drawdown, to_float(row["r1_hold_drawdown"], at), TOL_FLOAT, False
        ),
        compare_exact("r1_satisfied", key, at, r1.satisfied, to_bool(row["r1_satisfied"], at)),
    ]
    return items


def decimal_equal(text: str | None, registered: str) -> bool:
    """θ_P 文本按十进制数值相等核对。"""
    try:
        return text is not None and Decimal(text) == Decimal(registered)
    except InvalidOperation:
        return False


def log_end(chain: Chain | None) -> float | None:
    """独立净值链的 ln W_末。"""
    return None if chain is None else math.log(chain.script_wealth[-1])


def drawdown(chain: Chain | None) -> float | None:
    """独立净值链的最大回撤。"""
    return None if chain is None else chain.drawdown


def compare_candidate_rest(key: str, row: Row, derived: Derived) -> list[Item]:
    """candidates_summary 的 R2 十列、政策差额与四项状态统计列。"""
    at = where("candidates_summary.csv", row)
    items = []
    for asset, prefix in (("SPX", "spx"), ("QQQ", "qqq")):
        r2 = derived.r2[(key, asset)]
        items.extend(
            [
                compare_exact(
                    f"{prefix}_r2_computable", key, at, r2.computable, to_bool(row[f"{prefix}_r2_computable"], at)
                ),
                compare_exact(
                    f"{prefix}_r2_denominator", key, at, r2.denominator, to_int(row[f"{prefix}_r2_denominator"], at)
                ),
                compare_exact(f"{prefix}_r2_achieved", key, at, r2.achieved, to_int(row[f"{prefix}_r2_achieved"], at)),
                compare_exact(f"{prefix}_r2_new_only", key, at, r2.new_only, to_int(row[f"{prefix}_r2_new_only"], at)),
                compare_exact(f"{prefix}_r2_meets", key, at, r2.meets, to_bool(row[f"{prefix}_r2_meets"], at)),
            ]
        )
    state = derived.states[key]
    items.extend(
        [
            compare_float(
                "policy_minus_signal",
                key,
                at,
                derived.policy_diff[key],
                to_float(row["policy_minus_signal"], at),
                TOL_LOG,
                False,
            ),
            compare_exact("direct_level2", key, at, state.direct, to_int(row["direct_level2"], at)),
            compare_exact("via_level1_into_level2", key, at, state.via, to_int(row["via_level1_into_level2"], at)),
            compare_exact("level1_intervals", key, at, state.level1_intervals, to_int(row["level1_intervals"], at)),
            compare_exact("level2_intervals", key, at, state.level2_intervals, to_int(row["level2_intervals"], at)),
        ]
    )
    return items


def check_candidates_summary(rows: list[Row] | tuple[Row, ...], sets: ObjectSets, derived: Derived) -> list[Item]:
    """candidates_summary.csv：27 行逐项比对。"""
    indexed, items = candidate_rows("candidates_summary.csv", rows, sets.candidates)
    for order, key in enumerate(sets.candidates):
        row = indexed.get(key)
        if row is None:
            continue
        items.extend(compare_candidate_core(key, row, order, derived))
        items.extend(compare_candidate_rest(key, row, derived))
    return items


def reference_expectation(obj: str, sets: ObjectSets, derived: Derived) -> dict[str, Any]:
    """reference_summary 某行的脚本值（一直持有：switches 0、exposure_magnitude 0.0，答复单第 3 项）。"""
    if obj == HOLD:
        hold = derived.chains.get((HOLD, PATH_HOLD))
        return {
            "signal_log_wealth": log_end(hold),
            "signal_max_drawdown": drawdown(hold),
            "policy_log_wealth": None,
            "policy_max_drawdown": None,
            "switches": 0,
            "exposure_magnitude": 0.0,
        }
    if obj in AVERAGE_NAMES and obj not in sets.evaluable:
        return {
            "signal_log_wealth": None,
            "signal_max_drawdown": None,
            "policy_log_wealth": None,
            "policy_max_drawdown": None,
            "switches": 0,
            "exposure_magnitude": 0.0,
        }
    signal = derived.chains.get((obj, PATH_SIGNAL))
    policy = derived.chains.get((obj, PATH_POLICY))
    switch = derived.switches[obj]
    return {
        "signal_log_wealth": log_end(signal),
        "signal_max_drawdown": drawdown(signal),
        "policy_log_wealth": log_end(policy),
        "policy_max_drawdown": drawdown(policy),
        "switches": switch.count,
        "exposure_magnitude": switch.magnitude,
    }


def check_reference_summary(rows: list[Row] | tuple[Row, ...], sets: ObjectSets, derived: Derived) -> list[Item]:
    """reference_summary.csv 须恰含主参照、一直持有与两条均线对照四行。"""
    table = "reference_summary.csv"
    names = (REFERENCE, HOLD, *AVERAGE_NAMES)
    indexed: dict[str, Row] = {}
    items: list[Item] = []
    for row in rows:
        obj = str(row["object"])
        if obj not in names or obj in indexed:
            items.append(Item("参照行集合", obj, where(table, row), None, obj, None, "不一致：多余、重复或未知行"))
            continue
        indexed[obj] = row
    for obj in names:
        row = indexed.get(obj)
        if row is None:
            items.append(Item("参照行集合", obj, table, "应存在", None, None, "不一致：缺少该对象行"))
            continue
        at = where(table, row)
        for column, value in reference_expectation(obj, sets, derived).items():
            if column == "switches":
                items.append(compare_exact(column, obj, at, value, to_int(row[column], at)))
            else:
                tol = TOL_LOG if column.endswith("log_wealth") else TOL_FLOAT
                items.append(compare_float(column, obj, at, value, to_float(row[column], at), tol, False))
    return items


def check_segments(
    rows: list[Row] | tuple[Row, ...], sets: ObjectSets, derived: Derived, days: list[date] | tuple[date, ...]
) -> list[Item]:
    """segments.csv：每候选恰 4 行冻结分段；覆盖、端点、区间数、回撤比、无定义。"""
    table = "segments.csv"
    indexed: dict[tuple[str, str], Row] = {}
    items: list[Item] = []
    names = [name for name, _, _ in SEGMENTS]
    for row in rows:
        to_enum(row["coverage"], COVERAGES, where(table, row))
        key = (str(row["object"]), str(row["segment"]))
        if row["object"] not in sets.candidates or row["segment"] not in names or key in indexed:
            items.append(
                Item("分段行集合", key[0], where(table, row), None, key[1], None, "不一致：多余、重复或未知行")
            )
            continue
        indexed[key] = row
    hold = derived.chains.get((HOLD, PATH_HOLD))
    for obj in sets.candidates:
        signal = derived.chains.get((obj, PATH_SIGNAL))
        for name, start_text, end_text in SEGMENTS:
            row = indexed.get((obj, name))
            if row is None:
                items.append(Item("分段行集合", obj, f"{table} 分段={name}", "应存在", None, None, "不一致：缺少该行"))
                continue
            items.extend(segment_items(obj, name, start_text, end_text, row, days, signal, hold))
    return items


def segment_items(
    obj: str,
    name: str,
    start_text: str,
    end_text: str,
    row: Row,
    days: list[date] | tuple[date, ...],
    signal: Chain | None,
    hold: Chain | None,
) -> list[Item]:
    """一个（候选，分段）的映射、覆盖与回撤比比对。"""
    at = where("segments.csv", row)
    start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    mapping = segment_mapping(days, start, end)
    coverage, ratio, undefined = mapping.coverage, None, False
    if mapping.a is not None and mapping.b is not None:
        if signal is None or hold is None:
            coverage = "不可计算"
        else:
            ratio, undefined = segment_ratio(signal.script_wealth, hold.script_wealth, mapping.a, mapping.b, at)
    items = [
        compare_exact("start", obj, at, start, to_date(row["start"], at)),
        compare_exact("end", obj, at, end, to_date(row["end"], at)),
        compare_exact("coverage", obj, at, coverage, row["coverage"]),
        compare_exact(
            "first_nav_day",
            obj,
            at,
            mapping.first_nav_day,
            None if row["first_nav_day"] is None else to_date(row["first_nav_day"], at),
        ),
        compare_exact(
            "last_nav_day",
            obj,
            at,
            mapping.last_nav_day,
            None if row["last_nav_day"] is None else to_date(row["last_nav_day"], at),
        ),
        compare_exact("returns_count", obj, at, mapping.returns_count, to_int(row["returns_count"], at)),
        compare_float("drawdown_ratio", obj, at, ratio, to_float(row["drawdown_ratio"], at), TOL_FLOAT, False),
    ]
    # 无收益区间、不可计算时 undefined 为 False（答复单第 4 项）
    items.append(compare_exact("undefined", obj, at, undefined, to_bool(row["undefined"], at)))
    return items


# ---------------------------------------------------------------- 选择与描述性文件


def project_key_list(value: Any, keys: list[str] | tuple[str, ...], location: str) -> list[str]:
    """selection.json 的 feasible/tied：元素须为候选键文本（答复单第 7 项），保留项目顺序以便比对顺序。"""
    if not isinstance(value, list):
        raise InputError("不是数组", location)
    for element in value:
        if not isinstance(element, str) or element not in keys:
            raise InputError(f"表外取值：{element!r}（须为候选键文本）", location)
    return list(value)


def script_records(
    project_records: list[Any] | tuple[Any, ...], sets: ObjectSets, derived: Derived
) -> list[CandidateRecord]:
    """脚本候选记录：failed 取项目记录（甲层不能独立判定计算失败），其余由逐日输出重算。"""
    failed = {}
    for index, record in enumerate(project_records):
        location = f"selection.json records[{index}]"
        failed[jget(record, "object", location)] = jbool(jget(record, "failed", location), location)
    records = []
    for order, key in enumerate(sets.candidates):
        r1 = derived.r1[key]
        spx, qqq = derived.r2[(key, "SPX")].meets, derived.r2[(key, "QQQ")].meets
        log_wealth = log_end(derived.chains.get((key, PATH_SIGNAL)))
        switches = derived.switches[key].count
        records.append(CandidateRecord(order, key, bool(failed.get(key)), r1.satisfied, spx, qqq, log_wealth, switches))
    return records


def record_items(record: Any, mine: CandidateRecord, location: str) -> list[Item]:
    """一条 selection.records 与脚本记录的比对。"""
    r2 = jget(record, "r2", location)
    project_log = jfloat(jget(record, "log_wealth", location), location)
    return [
        compare_exact("r1", mine.key, location, mine.r1, jbool(jget(record, "r1", location), location)),
        compare_exact("r2.SPX", mine.key, location, mine.r2_spx, jbool(jget(r2, "SPX", location), location)),
        compare_exact("r2.QQQ", mine.key, location, mine.r2_qqq, jbool(jget(r2, "QQQ", location), location)),
        compare_float("log_wealth", mine.key, location, mine.log_wealth, project_log, TOL_LOG, False),
        compare_exact(
            "switches", mine.key, location, mine.switches, jint(jget(record, "switches", location), location)
        ),
    ]


def check_records(
    project_records: list[Any] | tuple[Any, ...],
    records: list[CandidateRecord] | tuple[CandidateRecord, ...],
    keys: list[str] | tuple[str, ...],
) -> list[Item]:
    """selection.records：只含 27 条，按 order 0..26 排列、不重复，名称按登记；逐条比对 r1、r2、log_wealth、
    switches。"""
    items = []
    for index, record in enumerate(project_records):
        location = f"selection.json records[{index}]"
        identity = (jint(jget(record, "order", location), location), jget(record, "object", location))
        if index >= len(keys):
            items.append(Item("候选记录集合", str(identity[1]), location, None, identity, None, "不一致：多余记录"))
            continue
        expected = (index, keys[index])
        items.append(compare_exact("候选记录集合", keys[index], location, expected, identity))
        if identity == expected:
            items.extend(record_items(record, records[index], location))
    items.extend(
        Item("候选记录集合", keys[i], f"selection.json records[{i}]", (i, keys[i]), None, None, "不一致：一方缺值")
        for i in range(len(project_records), len(keys))
    )
    return items


def outcome_from_records(project_records: list[Any] | tuple[Any, ...]) -> str | None:
    """按项目 records 推出非“选定”出口：failed 真 → 计算失败；r1、log_wealth 或 r2 任一值为空 → 缺值无法评价；
    无可行记录 → 无合格候选；否则（存在可行记录）为 None。"""
    rows = [record if isinstance(record, dict) else {} for record in project_records]
    if any(row.get("failed") is True for row in rows):
        return "计算失败"
    for row in rows:
        r2 = row.get("r2") if isinstance(row.get("r2"), dict) else {}
        if row.get("r1") is None or row.get("log_wealth") is None or r2.get("SPX") is None or r2.get("QQQ") is None:
            return "缺值无法评价"
    if not any(row.get("r1") is True and row["r2"].get("SPX") is True and row["r2"].get("QQQ") is True for row in rows):
        return "无合格候选"
    return None


RESERVED_REFERENCE = "保留：主参照失败原因未独立核验"


def reference_evidence(rows: list[Row] | tuple[Row, ...]) -> Item:
    """主参照失败分支的旁证：reference_summary.csv 主参照行 signal_log_wealth、
    signal_max_drawdown 是否为空（只照录）。"""
    table = "reference_summary.csv"
    row = next((r for r in rows if r["object"] == REFERENCE), None)
    evidence = None
    if row is not None:
        evidence = {
            "signal_log_wealth 为空": row["signal_log_wealth"] is None,
            "signal_max_drawdown 为空": row["signal_max_drawdown"] is None,
        }
    status = "照录：主参照失败分支旁证（空值只表示净值不可计算，不能区分失败与其他原因）"
    return Item("主参照失败旁证", REFERENCE, f"{table} 主参照行", None, evidence, None, status)


def outcome_items(
    outcome: Any, records: list[CandidateRecord], project_records: list[Any], reference_rows: list[Row]
) -> tuple[SelectionResult, list[Item]]:
    """出口核验。“计算失败” ⇔ 主参照失败或任一候选 failed 为真：项目出口为“计算失败”而 records 无 failed 为真时
    （主参照分支），outcome 一项记“保留：主参照失败原因未独立核验”（不判不一致、不计入一致）并写旁证；结果四字段仍按
    空值核对（负责人补充执行限定）。其余出口照常重算并按 records 核对出口成立。"""
    location = "selection.json"
    if outcome == "计算失败" and not any(record.failed for record in records):
        expected = SelectionResult("计算失败", (), None, (), None)
        reserved = Item("outcome", "选择", location, "计算失败（主参照分支）", outcome, None, RESERVED_REFERENCE)
        return expected, [reserved, reference_evidence(reference_rows)]
    result = select_candidate(records)
    items = [compare_exact("outcome", "选择", location, result.outcome, outcome)]
    if outcome != "选定":
        by_records = outcome_from_records(project_records)
        items.append(compare_exact("出口按 records 成立", "选择", f"{location} records", by_records, outcome))
    return result, items


def check_selection(doc: Any, sets: ObjectSets, derived: Derived, reference_rows: list[Row]) -> list[Item]:
    """selection.json：出口、容差、可行集、最大值、并列组、选定（按出口逐项核验，列表顺序亦比对）与 27 条记录。"""
    location = "selection.json"
    project_records = jget(doc, "records", location)
    if not isinstance(project_records, list):
        raise InputError("records 不是数组", location)
    records = script_records(project_records, sets, derived)
    outcome = jget(doc, "outcome", location)
    result, items = outcome_items(outcome, records, project_records, reference_rows)
    keys = sets.candidates
    feasible = project_key_list(jget(doc, "feasible", location), keys, f"{location} feasible")
    tied = project_key_list(jget(doc, "tied", location), keys, f"{location} tied")
    maximum = jfloat(jget(doc, "maximum", location), location)
    items.extend(
        [
            compare_exact(
                "tolerance", "选择", location, TIE_TOLERANCE, jfloat(jget(doc, "tolerance", location), location)
            ),
            compare_exact("selected", "选择", location, result.selected, jget(doc, "selected", location)),
            compare_exact("feasible", "选择", location, list(result.feasible), feasible),
            compare_float("maximum", "选择", location, result.maximum, maximum, TOL_LOG, False),
            compare_exact("tied", "选择", location, list(result.tied), tied),
        ]
    )
    items.extend(check_records(project_records, records, keys))
    return items


def truncated_element(value: Any, location: str) -> tuple[str, date, date, bool, bool]:
    """truncated 元素 [position, start, end, left_truncated, right_truncated]。"""
    if not isinstance(value, list) or len(value) != 5:
        raise InputError(f"truncated 元素格式不符：{value!r}", location)
    position, start, end, left, right = value
    if position not in ("一级", "二级") or not isinstance(left, bool) or not isinstance(right, bool):
        raise InputError(f"truncated 元素取值不符：{value!r}", location)
    return position, to_date(start, location), to_date(end, location), left, right


def state_entry_items(obj: str, entry: Any, state: StateStatistics, location: str) -> list[Item]:
    """一个对象的 descriptive.state_statistics 比对：计数、日期列表与 truncated 逐条精确；start_note 照录。"""
    counts = (
        ("direct_level2", state.direct),
        ("via_level1", state.via),
        ("level1_intervals", state.level1_intervals),
        ("level2_intervals", state.level2_intervals),
    )
    items = [compare_exact(key, obj, location, mine, jint(entry[key], location)) for key, mine in counts]
    for key, days in (("direct_level2_days", state.direct_days), ("via_level1_days", state.via_days)):
        items.append(compare_exact(key, obj, location, list(days), date_list(entry[key], location)))
    truncated = entry["truncated"]
    if not isinstance(truncated, list):
        raise InputError("truncated 不是数组", location)
    project = [truncated_element(element, location) for element in truncated]
    items.append(compare_exact("truncated", obj, location, list(state.truncated), project))
    items.append(note_item("start_note", obj, location, entry["start_note"], "照录：start_note 文字不核对"))
    return items


def check_state_entries(doc: Any, sets: ObjectSets, derived: Derived) -> list[Item]:
    """descriptive.state_statistics（27 个候选 + 主参照）逐对象比对；首信号日依赖逐项标注。"""
    items = []
    for obj, entry in project_state_entries(doc, sets.candidates).items():
        location = f"descriptive.json state_statistics 对象={obj}"
        items.extend(state_entry_items(obj, entry, derived.states[obj], location))
    for obj in (*sets.candidates, REFERENCE):
        dependency = derived.states[obj].first_day_dependency
        if dependency is not None:
            items.append(note_item("首信号日转换", obj, "daily_signals 首个信号日", None, f"未核对：{dependency}"))
    return items


def average_entry_items(name: str, entry: Any, sets: ObjectSets, derived: Derived, at: str) -> list[Item]:
    """一条 descriptive.averages：不可评价者 switches、signal、policy 须为 JSON null；
    可评价者比对 switches 与 signal.log_wealth。"""
    if name not in sets.evaluable:
        return [
            compare_exact(f"{key}（不可评价须为 null）", name, at, None, jget(entry, key, at))
            for key in ("switches", "signal", "policy")
        ]
    switches = jget(entry, "switches", at)
    mine = derived.switches[name]
    signal_log = log_end(derived.chains.get((name, PATH_SIGNAL)))
    project_log = jfloat(jget(jget(entry, "signal", at), "log_wealth", at), at)
    items = [compare_exact("switches.count", name, at, mine.count, jint(jget(switches, "count", at), at))]
    for key, value in (("magnitude_total", mine.magnitude), ("change_total", mine.change)):
        project = jfloat(jget(switches, key, at), at)
        items.append(compare_float(f"switches.{key}", name, at, value, project, TOL_FLOAT, False))
    items.append(compare_float("signal.log_wealth", name, at, signal_log, project_log, TOL_LOG, False))
    return items


def check_average_entries(doc: Any, sets: ObjectSets, derived: Derived) -> list[Item]:
    """descriptive.averages[*]（答复单第 2、8 项）。"""
    items = []
    for index, entry in enumerate(jget(doc, "averages", "descriptive.json")):
        items.extend(average_entry_items(entry["name"], entry, sets, derived, f"descriptive.json averages[{index}]"))
    return items


def check_constant_entries(doc: Any, sets: ObjectSets, derived: Derived) -> list[Item]:
    """descriptive.constants[*].nav：已计算者比对 log_wealth、max_drawdown；
    computed 为假者 nav 须为 null（答复单第 14 项）。"""
    items = []
    sources = dict(sets.constant_sources)
    for index, entry in enumerate(jget(doc, "constants", "descriptive.json")):
        at = f"descriptive.json constants[{index}]"
        name = CONSTANT_PREFIX + str(entry["object"])
        nav = jget(entry, "nav", at)
        if entry["computed"] is False:
            items.append(compare_exact("nav（computed 为假须为 null）", name, at, None, nav))
            continue
        if name not in sources:
            continue  # computed 为真但身份或条件不符：已在 constant_members 记为不一致，不计入 K
        chain = derived.chains.get((name, PATH_CONSTANT))
        project_log = jfloat(jget(nav, "log_wealth", at), at)
        project_mdd = jfloat(jget(nav, "max_drawdown", at), at)
        items.append(compare_float("nav.log_wealth", name, at, log_end(chain), project_log, TOL_LOG, False))
        items.append(compare_float("nav.max_drawdown", name, at, drawdown(chain), project_mdd, TOL_FLOAT, False))
    return items


def check_descriptive(doc: Any, sets: ObjectSets, derived: Derived) -> list[Item]:
    """descriptive.json：averages、constants、政策差额、状态统计。"""
    return [
        *check_average_entries(doc, sets, derived),
        *check_constant_entries(doc, sets, derived),
        *check_policy_entries(doc, sets, derived),
        *check_state_entries(doc, sets, derived),
    ]


def check_policy_entries(doc: Any, sets: ObjectSets, derived: Derived) -> list[Item]:
    """descriptive.policy_minus_signal：键恰为 27 个候选 + 主参照 + 两条均线对照（共 30），每项含 difference 与 note；
    键多、少或必需项缺失为输入错误；difference 1e-10 绝对；note 不读取（答复单第 6 项）。"""
    location = "descriptive.json policy_minus_signal"
    entries = jget(doc, "policy_minus_signal", "descriptive.json")
    expected = (*sets.candidates, REFERENCE, *AVERAGE_NAMES)
    if not isinstance(entries, dict) or set(entries) != set(expected):
        keys = list(entries) if isinstance(entries, dict) else entries
        raise InputError(f"键集合不符：应恰为 27 个候选键 + 主参照 + 两条均线对照，实际 {keys}", location)
    items = []
    for obj in expected:
        entry = entries[obj]
        if not isinstance(entry, dict) or "difference" not in entry or "note" not in entry:
            raise InputError("必需项 difference 或 note 缺失", f"{location} 对象={obj}")
        project = jfloat(entry["difference"], f"{location} 对象={obj}")
        mine = derived.policy_diff.get(obj)
        items.append(compare_float("policy_minus_signal.difference", obj, location, mine, project, TOL_LOG, False))
    return items


def ledger_counts(rows: list[Row] | tuple[Row, ...], keys: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    """segment_ledgers 每对象每资产各类别件数与 pre_window 为真的件数（只写入报告，不比对）。"""
    table = "segment_ledgers.csv.gz"
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        asset = to_enum(row["asset"], ASSETS, where(table, row))
        category = to_enum(row["category"], LEDGER_CATEGORIES, where(table, row))
        pre_window = to_bool(row["pre_window"], where(table, row))
        bucket = counts.setdefault((need(row, "object", table), asset), {})
        bucket[category] = bucket.get(category, 0) + 1
        bucket["pre_window 为真"] = bucket.get("pre_window 为真", 0) + int(bool(pre_window))
    return [
        {"object": obj, "asset": asset, "in_candidate_set": obj in keys, "counts": counts[(obj, asset)]}
        for obj, asset in sorted(counts)
    ]


# ---------------------------------------------------------------- 结构 B 专项与总流程


def check_structure_b(inputs: Inputs, sets: ObjectSets) -> list[Item]:
    """结构 B：reconciliation.rows 为空、constants 均未计算（已在 K 中核对）、用途限制照录。"""
    if inputs.structure != "B":
        return []
    rows = jget(inputs.docs["reconciliation.json"], "rows", "reconciliation.json")
    return [
        compare_exact(
            "结构 B 对账行数", "对账", "reconciliation.json rows", 0, len(rows) if isinstance(rows, list) else None
        )
    ]


def evaluate(raw: dict[str, bytes]) -> dict[str, Any]:
    """甲层全部核对；返回报告主体（不含输入哈希）。"""
    inputs = parse_inputs(raw)
    sets, items = object_sets(inputs)
    days = derive_days(inputs.tables["daily_targets.csv.gz"], inputs.window)
    derived, more = derive_all(inputs, sets, days)
    items.extend(more)
    items.extend(derived.policy_last_items)
    recon = parse_reconciliation(inputs.docs["reconciliation.json"])
    items.extend(check_single_reconciliation(derived.chains, recon, dict(sets.constant_sources)))
    items.extend(check_relative_reconciliation(derived.chains, recon, sets.candidates))
    items.extend(check_candidates_summary(inputs.tables["candidates_summary.csv"], sets, derived))
    items.extend(check_reference_summary(inputs.tables["reference_summary.csv"], sets, derived))
    items.extend(check_segments(inputs.tables["segments.csv"], sets, derived, days))
    items.extend(check_selection(inputs.docs["selection.json"], sets, derived, inputs.tables["reference_summary.csv"]))
    items.extend(check_descriptive(inputs.docs["descriptive.json"], sets, derived))
    items.extend(check_structure_b(inputs, sets))
    record = inputs.docs["run_record.json"]
    return {
        "structure": inputs.structure,
        "usage_restriction": jget(record, "usage_restriction", "run_record.json"),
        "cutoff": jget(record, "cutoff", "run_record.json"),
        "window": {
            "first_day": inputs.window.first_day,
            "last_day": inputs.window.last_day,
            "n": inputs.window.n,
            "j0": inputs.window.j0,
            "e_index": inputs.window.e_index,
        },
        "object_sets": {
            "C": list(sets.candidates),
            "E": list(sets.evaluable),
            "O_d": list(sets.daily),
            "K": list(sets.constants),
            "count_O_d": len(sets.daily),
            "count_K": len(sets.constants),
        },
        "ledger_counts": ledger_counts(inputs.tables["segment_ledgers.csv.gz"], sets.candidates),
        "items": items,
    }


# ---------------------------------------------------------------- 报告


def json_ready(value: Any) -> Any:
    """转为可写 JSON 的值：日期 ISO、非有限浮点为文字、Item 为对象。"""
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
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): json_ready(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(inner) for inner in value]
    return value


def is_problem(item: Item) -> bool:
    """不一致或计算失败。"""
    return item.status.startswith(("不一致", "计算失败"))


def finish_report(
    body: dict[str, Any], hashes: list[dict[str, Any]], error: dict[str, str] | None, error_code: int
) -> tuple[dict[str, Any], int]:
    """汇总计数、首个不一致与退出码。"""
    items: list[Item] = body.pop("items", [])
    mismatches = [item for item in items if item.status.startswith("不一致")]
    failures = [item for item in items if item.status.startswith("计算失败")]
    first = next((item for item in items if is_problem(item)), None)
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
            "reserved": sum(1 for item in items if item.status.startswith("保留")),
        },
        "first_mismatch": first,
        "exit_code": code,
        "exit_code_meanings": {str(number): text for number, text in EXIT_MEANINGS},
        "items": items,
    }
    return json_ready(report), code


def build_report(raw: dict[str, bytes], hashes: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    """运行全部核对并捕获输入错误（2）与计算失败（3）。"""
    try:
        body = evaluate(raw)
    except InputError as exc:
        return finish_report(
            {}, hashes, {"kind": "参数或输入错误", "message": exc.message, "location": exc.location}, 2
        )
    except ComputeFailure as exc:
        return finish_report({}, hashes, {"kind": "计算失败", "message": exc.message, "location": exc.location}, 3)
    return finish_report(body, hashes, None, 0)


def read_inputs(result_dir: Path) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """边界函数：只读允许的输入文件，返回内容与字节数、SHA-256。"""
    raw: dict[str, bytes] = {}
    hashes: list[dict[str, Any]] = []
    for name in INPUT_FILES:
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
    parser = argparse.ArgumentParser(description="波段预警 v2.0 独立回算·甲层（N1）")
    parser.add_argument("--result-dir", required=True, help="结果目录（evaluation_development）")
    parser.add_argument("--out", required=True, help="报告 JSON 路径（须不存在）")
    return parser.parse_args(list(argv))


def main(argv: list[str] | tuple[str, ...]) -> int:
    """命令行入口：读输入、核对、写报告，返回退出码。"""
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
    try:
        raw, hashes = read_inputs(Path(args.result_dir))
        report, code = build_report(raw, hashes)
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
