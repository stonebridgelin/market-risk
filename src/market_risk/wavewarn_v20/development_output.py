"""阶段四开发期运行的输出、清单、发布与失败证据（阶段四字段级设计稿第八节；M2 第一部分指令第二节第 5 小节）。边界侧。

本批新增的文件操作全部集中在本模块的六个基本函数（M2 指令第二节第 5 小节第 3 条，PM）：
make_directory（新建目录，已存在即失败）、write_new（以独占方式新建并写入，已存在即失败）、
read_back（读取文件字节：只用于读回、清单复算与尾段哈希，对象限于本次暂存、失败、正式目录与两个包外文件）、
rename_directory（整目录改名，目标已存在即失败）、path_exists（存在性检查）、
read_preflight_file（预检只读入口：只接受 PREFLIGHT_FILES 中的相对路径，只在预检步骤中调用）。
本模块其他函数只组织内容，不直接操作文件。行情读取与配置读取不经本模块。

格式（设计稿第八节第 3 小节）：UTF-8 无 BOM；LF；JSON 不转义非 ASCII、缩进 2、键按定义顺序、末尾一个换行；CSV 首行
表头、RFC 4180 引号、空字段只表示 None（写出前断言无空字符串值）；浮点用 repr；Decimal 原文；日期 ISO；
枚举写登记中文值；gzip 用 GzipFile(filename="", mtime=0, compresslevel=9)；文件名 ASCII 小写下划线、不含时间戳。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path

from market_risk.storage.paths import StoragePaths
from market_risk.wavewarn_v20 import development_compare as compare
from market_risk.wavewarn_v20.nav import NavResult, PartialPolicyResult, PolicyResult
from market_risk.wavewarn_v20.research_run import (
    HOLD,
    REFERENCE,
    DevelopmentSelection,
    Unavailable,
    WindowResult,
    candidate_key,
)
from market_risk.wavewarn_v20.snapshot import Snapshot

OUTPUT_SCHEME = "v20-stage4-output-1"                     # 输出方案版本（PM）
FORMAL_NAME = "evaluation_development"
MANIFEST = "MANIFEST.sha256"
FAILURE_MANIFEST = "FAILURE_MANIFEST.sha256"
AUDIT_LOG = "audit_log.jsonl"
RUN_ENVIRONMENT = "run_environment.json"
INFORMATION_STATE = "information_state.json"
INFORMATION_STATE_FINAL = "information_state_final.json"
FAILURE = "failure.json"
PREFIX_USAGE = "前缀诊断运行（严格读取未通过：缺少必需价格）"       # 补充二第四节
NOT_A_SUCCESS = "该正式目录不得作为成功结果引用，交负责人处理"
# 允许变化的文件集合（设计稿第八节第 2 小节）：清单中只有这两行允许不同。
UNSTABLE_FILES = (RUN_ENVIRONMENT, AUDIT_LOG)

# 预检只读入口的固定集合（M2 指令第二节第 5 小节第 6 条；共 35 个，POSIX 写法）。
PREFLIGHT_FILES: tuple[str, ...] = (
    "pyproject.toml", "uv.lock", "config/wavewarn_v20.yaml", "config/data_decisions.yaml",
    *(f"src/market_risk/{name}" for name in ("__init__.py", "calendar.py", "config.py", "models.py", "precision.py",
                                              "storage/__init__.py", "storage/paths.py")),
    *(f"src/market_risk/wavewarn_v20/{name}.py" for name in (
        "__init__", "channels", "config_v20", "confirmatory", "convergence", "data_v20", "dataset_v20", "execution",
        "inputs", "labels_r2", "nav", "provenance_v20", "r1", "r2", "reference", "research_run", "selection",
        "snapshot", "state_machine", "verify_dataset", "registered_v20", "development_compare", "development_output",
        "development_run")),
)


class OutputError(ValueError):
    """输出、读回、清单或发布不符合要求。"""


class PreflightError(ValueError):
    """预检拒绝：不读任何行情（退出码 2）。"""


# ---------------------------------------------------------------------------
# 目录与包外文件（设计稿第八节第 1 小节；路径由 storage/paths.py 生成）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Locations:
    """一次运行内的全部输出位置；<UTC>_<短提交> 在一次运行内取同一值。"""

    research: Path
    formal: Path
    staging: Path
    failed: Path
    tail: Path
    publish_failure: Path
    stamp: str


def locations(root: Path, utc: str, short_commit: str) -> Locations:
    research = StoragePaths(root).v20_research_dir
    stamp = f"{utc}_{short_commit}"
    return Locations(research, research / FORMAL_NAME, research / f".staging_{FORMAL_NAME}_{stamp}",
                     research / f"{FORMAL_NAME}_failed_{stamp}", research / f"{FORMAL_NAME}_audit_tail_{stamp}.jsonl",
                     research / f"{FORMAL_NAME}_publish_failure_{stamp}.json", stamp)


def readable_roots(places: Locations) -> tuple[Path, ...]:
    """read_back 允许的对象：本次暂存、失败、正式目录与两个包外文件。"""
    return places.staging, places.failed, places.formal, places.tail, places.publish_failure


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _within(path: Path, root: Path) -> bool:
    target, base = _normalized(path), _normalized(root)
    return target == base or target.startswith(base.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------------------
# 六个基本函数（本批新增的文件操作只在这里）
# ---------------------------------------------------------------------------


def make_directory(path: Path) -> None:
    """新建目录（含尚不存在的上级目录）；目录本身已存在即失败。"""
    path.mkdir(parents=True, exist_ok=False)


def write_new(path: Path, data: bytes | Callable[[], bytes], closed: Callable[[], None] | None = None) -> None:
    """以独占方式新建并写入文件（"xb"）；已存在即失败，不覆盖。

    data 可为在打开之后才生成内容的函数（审计尾段须包含打开尾段文件本身的事件，M2 指令第二节第 7 小节第 2 条）；
    closed 在文件关闭后立即调用（审计钩子由此转为封存状态，第 4 条）。"""
    with path.open("xb") as handle:
        handle.write(data() if callable(data) else data)
    if closed is not None:
        closed()


def read_back(path: Path, allowed: Sequence[Path]) -> bytes:
    """读取文件字节：只用于读回、清单复算与尾段哈希；对象须位于 allowed 列出的目录或文件之内。"""
    if not any(_within(path, item) for item in allowed):
        raise OutputError(f"读回对象不在本次输出位置之内：{path}")
    return path.read_bytes()


def rename_directory(source: Path, target: Path) -> None:
    """整目录改名；目标已存在即失败（先检查，再改名；改名本身在目标存在时也会失败）。"""
    if path_exists(target):
        raise OutputError(f"改名目标已存在：{target}")
    source.rename(target)


def path_exists(path: Path) -> bool:
    """存在性检查。"""
    return path.exists()


_preflight_closed = False


def close_preflight() -> None:
    """第 1 步（预检）结束：此后 read_preflight_file 一律失败。"""
    global _preflight_closed
    _preflight_closed = True


def read_preflight_file(root: Path, relative: str) -> bytes:
    """预检只读入口：只接受 PREFLIGHT_FILES 中的相对路径；规范化后须位于 root 之下且不位于 root/data 之下；
    只读、返回字节；只允许在第 1 步（预检）中调用，之后调用即失败。"""
    if _preflight_closed:
        raise PreflightError("预检已结束，不得再调用预检只读入口")
    if relative not in PREFLIGHT_FILES:
        raise PreflightError(f"不在预检文件集合之内：{relative!r}")
    path = root.joinpath(*relative.split("/"))
    if not _within(path, root) or _within(path, root.joinpath("data")):
        raise PreflightError(f"预检路径越界：{relative!r}")
    content = path.read_bytes()
    return content


# ---------------------------------------------------------------------------
# 格式
# ---------------------------------------------------------------------------


def plain(value: object) -> object:
    """JSON 可表示的值：Decimal 原文、日期 ISO、枚举登记中文值；浮点须有限。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OutputError(f"出现非有限浮点：{value!r}")
        return value
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    if value is None or isinstance(value, bool | int | str):
        return value
    raise OutputError(f"不支持写出的值类型：{type(value).__name__}")


def json_bytes(value: object) -> bytes:
    return (json.dumps(plain(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def cell(value: object) -> str:
    """CSV 单元格：None 为空字段；空字符串不允许（空字段只表示 None）；浮点 repr；布尔写 true/false。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OutputError(f"出现非有限浮点：{value!r}")
        return repr(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value)
    if text == "":
        raise OutputError("CSV 中出现空字符串值（空字段只表示 None）")
    return text


def csv_bytes(header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in rows:
        if len(row) != len(header):
            raise OutputError("CSV 行的字段数与表头不同")
        writer.writerow([cell(item) for item in row])
    return buffer.getvalue().encode("utf-8")


def gzip_bytes(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as stream:
        stream.write(data)
    return buffer.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 清单（设计稿第八节第 4 小节）：除自身外的全部文件，相对路径排序；每行“SHA-256 字节数 相对路径”
# ---------------------------------------------------------------------------


def manifest_bytes(files: Mapping[str, bytes]) -> bytes:
    lines = [f"{sha256(files[name])} {len(files[name])} {name}" for name in sorted(files)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_manifest(data: bytes) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for line in data.decode("utf-8").splitlines():
        digest, size, name = line.split(" ", 2)
        if len(digest) != 64 or name in result:
            raise OutputError(f"清单行不合法：{line!r}")
        result[name] = (digest, int(size))
    return result


# ---------------------------------------------------------------------------
# 写出、读回、清单与发布（组织内容；文件操作只经上面的基本函数）
# ---------------------------------------------------------------------------


def write_files(directory: Path, files: Mapping[str, bytes]) -> list[str]:
    """按给定顺序独占写出；返回已写出的文件名（写出失败时由调用方据此判断已完成的部分）。"""
    written: list[str] = []
    for name, data in files.items():
        write_new(directory / name, data)
        written.append(name)
    return written


def verify_written(directory: Path, files: Mapping[str, bytes], allowed: Sequence[Path]) -> None:
    """读回每个文件：字节与内存内容相同；JSON 可解析且与内存对象一致；CSV（含 gzip）行数与内存一致。"""
    for name, data in files.items():
        back = read_back(directory / name, allowed)
        if back != data:
            raise OutputError(f"读回字节与写出内容不同：{name}")
        if name.endswith(".json"):
            if json.loads(back.decode("utf-8")) != json.loads(data.decode("utf-8")):
                raise OutputError(f"读回 JSON 与内存对象不一致：{name}")
        elif name.endswith((".csv", ".csv.gz")):
            text = (gzip.decompress(back) if name.endswith(".gz") else back).decode("utf-8")
            expected = (gzip.decompress(data) if name.endswith(".gz") else data).decode("utf-8")
            if list(csv.reader(io.StringIO(text))) != list(csv.reader(io.StringIO(expected))):
                raise OutputError(f"读回 CSV 的字段或行数与内存不一致：{name}")


def verify_manifest(directory: Path, manifest_name: str, allowed: Sequence[Path]) -> dict[str, tuple[str, int]]:
    """读回清单并逐文件复算字节数与 SHA-256。"""
    listed = parse_manifest(read_back(directory / manifest_name, allowed))
    for name, (digest, size) in listed.items():
        data = read_back(directory / name, allowed)
        if (sha256(data), len(data)) != (digest, size):
            raise OutputError(f"清单复算不符：{name}")
    return listed


# ---------------------------------------------------------------------------
# 序列化（第 11 步）：稳定计算文件。逐日表按日期升序，同日按候选 0—26、主参照、一直持有、描述性对照。
# ---------------------------------------------------------------------------


def _risk(value: object) -> str | None:
    names = {0: "正常", 1: "一级", 2: "二级"}
    return None if value is None else names[int(value)]          # type: ignore[call-overload]


@dataclass(frozen=True)
class Objects:
    """逐日表的对象顺序：候选 0—26、主参照、一直持有、描述性对照。"""

    names: tuple[str, ...]

    def rank(self, name: str) -> int:
        return self.names.index(name)


def objects_of(result: WindowResult, descriptive: compare.Descriptive | None) -> Objects:
    names = [candidate_key(candidate) for candidate in result.candidates]
    names += [REFERENCE, HOLD]
    if descriptive is not None:
        names += [item.name for item in descriptive.averages]
        names += [f"恒定仓位：{item.object}" for item in descriptive.constants]
    return Objects(tuple(names))


def serialize_snapshot(snapshot: Snapshot) -> bytes:
    rows = [(day, snapshot.closes["SPX"].get(day), snapshot.closes["QQQ"].get(day)) for day in snapshot.days]
    return gzip_bytes(csv_bytes(("date", "spx_close", "qqq_close"), rows))


def _sorted(rows: list[tuple], order: Objects) -> list[tuple]:
    return [row[2:] for row in sorted(rows, key=lambda row: (row[0], row[1]))]


def serialize_daily(result: WindowResult, descriptive: compare.Descriptive | None) -> dict[str, bytes]:
    """daily_signals、daily_targets（信号模拟计划目标）、daily_nav、daily_policy（执行政策研究模拟）。"""
    order = objects_of(result, descriptive)
    signals: list[tuple] = []
    targets: list[tuple] = []
    navs: list[tuple] = []
    policies: list[tuple] = []

    def add_object(name: str, signal_rows: Sequence, target_rows: Sequence, signal_nav: object,
                   policy: object) -> None:
        rank = order.rank(name)
        for item in signal_rows:
            signals.append((item.day, rank, item.day, name, _risk(item.risk), item.level, item.c1, item.c2,
                            item.all_valid))
        for item in target_rows:
            targets.append((item.day, rank, item.day, name, item.position, item.core, item.leverage, item.source,
                            item.cap_active))
        add_nav(name, rank, "信号模拟", signal_nav)
        if isinstance(policy, PolicyResult | PartialPolicyResult):
            executions = policy.nav.executions if isinstance(policy, PolicyResult) else policy.executions
            wealth = {item.day: item.wealth for item in executions}
            for item in policy.targets:
                policies.append((item.day, rank, item.day, name, item.position, item.core, item.leverage,
                                 item.source, item.cap_active, wealth.get(item.day)))
            if isinstance(policy, PolicyResult):
                add_nav(name, rank, "执行政策研究模拟", policy.nav)

    def add_nav(name: str, rank: int, path: str, nav: object) -> None:
        if isinstance(nav, NavResult):
            for index, item in enumerate(nav.executions):
                navs.append((item.day, rank, path, item.day, name, path, item.wealth,
                             None if index == 0 else nav.returns[index - 1]))
        elif isinstance(nav, compare.PathNav):
            for index, day in enumerate(nav.days):
                navs.append((day, rank, path, day, name, path, nav.wealth[index],
                             None if index == 0 else nav.returns[index - 1]))

    for candidate, outcome in result.candidates.items():
        add_object(candidate_key(candidate), outcome.outcome.signals, outcome.outcome.targets,
                   outcome.outcome.signal_nav, outcome.outcome.policy)
    if result.reference is not None:
        add_object(REFERENCE, result.reference.outcome.signals, result.reference.outcome.targets,
                   result.reference.outcome.signal_nav, result.reference.outcome.policy)
    if result.common is not None:
        add_nav(HOLD, order.rank(HOLD), "一直持有", result.common.hold)
    if descriptive is not None:
        for item in descriptive.averages:
            add_object(item.name, item.signals, item.targets, item.signal_nav, item.policy)
        for item in descriptive.constants:
            add_nav(f"恒定仓位：{item.object}", order.rank(f"恒定仓位：{item.object}"), "恒定仓位", item.nav)
    return {
        "daily_signals.csv.gz": gzip_bytes(csv_bytes(
            ("date", "object", "risk", "level", "c1", "c2", "all_valid"), _sorted(signals, order))),
        "daily_targets.csv.gz": gzip_bytes(csv_bytes(
            ("date", "object", "position", "core", "leverage", "source", "cap_active"), _sorted(targets, order))),
        "daily_nav.csv.gz": gzip_bytes(csv_bytes(
            ("date", "object", "path", "wealth", "return_to_date"),
            [row[3:] for row in sorted(navs, key=lambda row: (row[0], row[1], row[2]))])),
        "daily_policy.csv.gz": gzip_bytes(csv_bytes(
            ("date", "object", "position", "core", "leverage", "source", "cap_active", "wealth"),
            _sorted(policies, order))),
    }


def serialize_r2(result: WindowResult) -> dict[str, bytes]:
    events: list[tuple] = []
    judgements: list[tuple] = []
    ledgers: list[tuple] = []
    order = objects_of(result, None)
    if result.common is not None:
        for asset, value in result.common.events.items():
            if isinstance(value, Unavailable):
                continue
            for event in value:
                events.append((event.peak, asset, event.asset, event.peak, event.peak_close, event.t3, event.t5,
                               event.trough, event.trough_close, event.end, event.unfinished))
    for candidate, outcome in result.candidates.items():
        name, rank = candidate_key(candidate), order.rank(candidate_key(candidate))
        for asset, r2 in outcome.r2.items():
            for item in (r2.judgements if r2 is not None else ()):
                judgements.append((item.event.peak, rank, asset, name, asset, item.event.peak, item.category,
                                   item.first_new_day, item.executable_day, item.executable_offset,
                                   item.peak_new_uncertain))
        for asset, ledger in outcome.ledgers.items():
            for segment, category in (ledger.classes if ledger is not None else ()):
                ledgers.append((segment.start, rank, asset, name, asset, segment.start, segment.end,
                                segment.pre_window, category))
    return {
        "r2_events.csv.gz": gzip_bytes(csv_bytes(
            ("asset", "peak", "peak_close", "t3", "t5", "trough", "trough_close", "end", "unfinished"),
            [row[2:] for row in sorted(events, key=lambda row: (row[0], row[1]))])),
        "r2_judgements.csv.gz": gzip_bytes(csv_bytes(
            ("object", "asset", "peak", "category", "first_new_day", "executable_day", "executable_offset",
             "peak_new_uncertain"), [row[3:] for row in sorted(judgements, key=lambda row: row[:3])])),
        "segment_ledgers.csv.gz": gzip_bytes(csv_bytes(
            ("object", "asset", "start", "end", "pre_window", "category"),
            [row[3:] for row in sorted(ledgers, key=lambda row: row[:3])])),
    }


def serialize_diagnostics(result: WindowResult) -> bytes:
    """诊断（组合层设计第十四节）：回撤（D 或 D̂）、杠杆因子 1 + λU、收敛枚举的首个共同位置（轴行号）。"""
    dated: list[tuple] = []
    undated: list[tuple] = []
    order = objects_of(result, None)
    diagnostics = result.diagnostics
    if diagnostics is not None:
        for asset, items in diagnostics.drawdowns.items():
            for item in items:
                dated.append((item.day, -1, "回撤", asset, "D̂" if item.is_estimate else "D", item.day,
                              item.drawdown, item.is_estimate))
        names = {repr(candidate): candidate_key(candidate) for candidate in result.candidates}
        names["reference"] = REFERENCE
        for key, items in diagnostics.leverage.items():
            name = names[key]
            for item in items:
                dated.append((item.end_day, order.rank(name), "杠杆因子", name, "检查" if item.checked else "不检查",
                              item.end_day, item.factor, item.ok))
        for candidate, diagnosis in diagnostics.convergence.items():
            name = candidate_key(candidate)
            for channel, enumerated in (*diagnosis.channels.items(), ("系统", diagnosis.system)):
                undated.append((order.rank(name), channel, "收敛枚举", name, channel, None,
                                enumerated.first_common_global, None))
        undated.append((order.rank(REFERENCE), "主参照", "收敛枚举", REFERENCE, "主参照", None,
                        diagnostics.reference.first_common_global, None))
    rows = [row[2:] for row in sorted(dated, key=lambda row: (row[0], row[1], row[2], row[4]))]
    rows += [row[2:] for row in undated]
    return gzip_bytes(csv_bytes(("kind", "object", "item", "date", "value", "flag"), rows))


def window_document(snapshot: Snapshot, result: WindowResult) -> dict:
    window = result.window
    document: dict = {"purpose": result.spec.purpose, "last_day": result.spec.last_day,
                      "histories": dict(result.spec.histories),
                      "candidates_requested": len(result.candidates_requested),
                      "stop": None if result.stop is None else stop_document(result.stop)}
    if window is not None:
        days = snapshot.days
        document["window"] = {
            "t0": window.t0, "t0_day": days[window.t0], "j0": window.j0, "j0_min": window.j0_min,
            "kappa_all": window.kappa_all, "kappa_all_day": days[window.kappa_all],
            "reference_index": window.reference_index, "reference_day": days[window.reference_index],
            "e_index": window.e_index, "n": window.n, "first_day": window.first_day, "last_day": window.last_day,
            "start_basis": window.start_basis, "history_first": window.history_first,
            "history_last": window.history_last,
            "convergences": [{"object": candidate_key(candidate), "channels": dict(item.channel_indices),
                              "kappa_channel": item.kappa_channel, "system_index": item.system_index,
                              "system_day": days[item.system_index]}
                             for candidate, item in window.convergences.items()]}
    return document


def stop_document(stop: object) -> dict:
    return {"exit": stop.exit, "object": stop.object, "stage": stop.stage,          # type: ignore[attr-defined]
            "exception_type": stop.exception_type, "reason_code": stop.reason_code,  # type: ignore[attr-defined]
            "message": stop.message, "detail": {key: str(value) for key, value in    # type: ignore[attr-defined]
                                                stop.detail.items()},               # type: ignore[attr-defined]
            "traceback": stop.traceback}                                           # type: ignore[attr-defined]


def selection_document(selection: DevelopmentSelection) -> dict:
    chosen = selection.selection
    return {"outcome": chosen.outcome, "tolerance": selection.tolerance,
            "selected": None if chosen.selected is None else candidate_key(chosen.selected),
            "feasible": [candidate_key(item) for item in chosen.feasible], "maximum": chosen.maximum,
            "tied": [candidate_key(item) for item in chosen.tied],
            "records": [{"order": item.order, "object": candidate_key(item.candidate), "failed": item.failed,
                         "r1": item.r1, "r2": dict(item.r2), "log_wealth": item.log_wealth,
                         "switches": item.switches} for item in chosen.records]}


def reconciliation_document(rows: Sequence[compare.ReconciliationRow]) -> dict:
    return {"tolerance_scope": "只用于两类对数净值对账；日期、状态、分类、计数、出口、哈希一律精确相等",
            "rows": [{"kind": row.kind, "object": row.object, "path": row.path, "difference": row.difference,
                      "tolerance": row.tolerance, "passed": row.passed} for row in rows]}


def _policy_summary(policy: object) -> dict:
    if isinstance(policy, PolicyResult):
        return {"complete": True, **compare.nav_summary(policy.nav)}
    if isinstance(policy, PartialPolicyResult):
        return {"complete": False, "undetermined": None if policy.undetermined is None else
                {"day": policy.undetermined.day, "reason": policy.undetermined.reason},
                "missing": [[asset, day] for asset, day in policy.missing]}
    return {"complete": None}


def descriptive_document(descriptive: compare.Descriptive) -> dict:
    return {
        "averages": [{"name": item.name, "domain": [_risk(state) for state in item.domain],
                      "convergence_day": item.convergence_day, "evaluable": item.evaluable, "note": item.note,
                      "signal": compare.nav_summary(item.signal_nav) if item.evaluable else None,
                      "policy": _policy_summary(item.policy) if item.evaluable else None,
                      "switches": compare.switch_summary(item.switches) if item.evaluable else None}
                     for item in descriptive.averages],
        "constants": [{"object": item.object, "computed": item.computed, "note": item.note, "core": item.core,
                       "leverage": item.leverage, "exposure": item.exposure,
                       "nav": compare.nav_summary(item.nav) if item.computed else None}
                      for item in descriptive.constants],
        "environment_summary": descriptive.environment_summary,
        "state_statistics": [{"object": item.object, "direct_level2": len(item.direct_level2_days),
                              "direct_level2_days": list(item.direct_level2_days),
                              "via_level1": len(item.via_level1_days), "via_level1_days": list(item.via_level1_days),
                              "level1_intervals": len(item.level1_runs), "level2_intervals": len(item.level2_runs),
                              "truncated": [[run.position, run.start, run.end, run.left_truncated,
                                             run.right_truncated] for run in (*item.level1_runs, *item.level2_runs)
                                            if run.left_truncated or run.right_truncated],
                              "start_note": item.start_note} for item in descriptive.statistics],
        "policy_events": {name: [[event.kind, event.day, event.detail] for event in events]
                          for name, events in descriptive.policy_events.items()},
        "policy_minus_signal": dict(descriptive.policy_differences),
        "note": "描述性对照与报告项不参与筛选、排序或确认性检验；不新增扫描参数",
    }


def serialize_tables(result: WindowResult, descriptive: compare.Descriptive) -> dict[str, bytes]:
    candidate_rows = []
    statistics = {item.object: item for item in descriptive.statistics}
    for candidate, outcome in result.candidates.items():
        key = candidate_key(candidate)
        signal = compare.nav_summary(outcome.outcome.signal_nav)
        policy = _policy_summary(outcome.outcome.policy)
        switch = compare.switch_summary(outcome.outcome.switches)
        r2 = {asset: outcome.r2.get(asset) for asset in ("SPX", "QQQ")}
        stats = statistics[key]
        candidate_rows.append((
            outcome.record.order, key, candidate.k, candidate.theta, candidate.h, signal["log_wealth"],
            signal["max_drawdown"], policy.get("log_wealth"), policy.get("max_drawdown"), switch["count"],
            switch["magnitude_total"], outcome.r1.computable, outcome.r1.signal_drawdown, outcome.r1.hold_drawdown,
            outcome.r1.satisfied,
            *(value for asset in ("SPX", "QQQ") for value in (
                None if r2[asset] is None else r2[asset].computable,
                None if r2[asset] is None else r2[asset].denominator,
                None if r2[asset] is None else r2[asset].achieved,
                None if r2[asset] is None else r2[asset].new_only,
                None if r2[asset] is None else r2[asset].meets)),
            descriptive.policy_differences[key]["difference"], len(stats.direct_level2_days),
            len(stats.via_level1_days), len(stats.level1_runs), len(stats.level2_runs)))
    reference_rows = []
    if result.reference is not None and result.common is not None:
        for name, signal_nav, policy, changes in (
                (REFERENCE, result.reference.outcome.signal_nav, result.reference.outcome.policy,
                 result.reference.outcome.switches),
                (HOLD, result.common.hold, None, ()),
                *((item.name, item.signal_nav, item.policy, item.switches) for item in descriptive.averages)):
            signal = compare.nav_summary(signal_nav)
            policy_summary = _policy_summary(policy)
            reference_rows.append((name, signal["log_wealth"], signal["max_drawdown"],
                                   policy_summary.get("log_wealth"), policy_summary.get("max_drawdown"),
                                   len(changes), compare.switch_summary(changes)["magnitude_total"]))
    segment_rows = [(candidate_key(candidate), item.spec.name, item.spec.start, item.spec.end, item.coverage,
                     item.first_nav_day, item.last_nav_day, item.returns_count, item.drawdown_ratio, item.undefined)
                    for candidate, outcome in result.candidates.items() for item in outcome.segments]
    environment_rows = [(row.interval, row.start, row.category, row.year_return) for row in descriptive.environments]
    substitution_rows = [(item.candidate, item.computed, item.note, len(item.intervals), item.registered_log,
                          item.substituted_log, item.difference,
                          ";".join(f"{start.isoformat()}/{end.isoformat()}" for start, end in item.intervals) or None)
                         for item in descriptive.substitutions]
    r2_columns = tuple(f"{asset}_{name}" for asset in ("spx", "qqq")
                       for name in ("r2_computable", "r2_denominator", "r2_achieved", "r2_new_only", "r2_meets"))
    return {
        "candidates_summary.csv": csv_bytes(
            ("order", "object", "k", "theta_p", "h", "signal_log_wealth", "signal_max_drawdown", "policy_log_wealth",
             "policy_max_drawdown", "switches", "exposure_magnitude", "r1_computable", "r1_signal_drawdown",
             "r1_hold_drawdown", "r1_satisfied", *r2_columns, "policy_minus_signal", "direct_level2",
             "via_level1_into_level2", "level1_intervals", "level2_intervals"), candidate_rows),
        "reference_summary.csv": csv_bytes(
            ("object", "signal_log_wealth", "signal_max_drawdown", "policy_log_wealth", "policy_max_drawdown",
             "switches", "exposure_magnitude"), reference_rows),
        "segments.csv": csv_bytes(
            ("object", "segment", "start", "end", "coverage", "first_nav_day", "last_nav_day", "returns_count",
             "drawdown_ratio", "undefined"), segment_rows),
        "environments.csv": csv_bytes(("interval", "start", "category", "year_return"), environment_rows),
        "exposure_substitution.csv": csv_bytes(
            ("object", "computed", "note", "intervals", "registered_log", "substituted_log", "difference",
             "interval_days"), substitution_rows),
    }


def render_report(result: WindowResult, selection: DevelopmentSelection, usage_restriction: str | None) -> bytes:
    """报告：普通运行不写用途限制；经过诊断读取的运行在第一个标题之后的第一行写固定文字（补充二第四节）。"""
    lines = ["# 波段预警 v2.0 开发期运行报告"]
    if usage_restriction is not None:
        lines.append(usage_restriction)
    window = result.window
    lines += ["", "## 窗口", ""]
    if window is not None:
        lines += [f"- 首个计入收盘日 j₀：{window.first_day.isoformat()}；E：{window.last_day.isoformat()}",
                  f"- 可计入收益区间数 n = {window.n}", f"- 起点依据：{window.start_basis}"]
    chosen = selection.selection
    lines += ["", "## 选择（组合层与选择程序出口）", "", f"- 出口：{chosen.outcome.value}",
              f"- 选定组：{'无' if chosen.selected is None else candidate_key(chosen.selected)}",
              f"- 可行组数：{len(chosen.feasible)}；并列组数：{len(chosen.tied)}"]
    lines += ["", "## 说明", "", "- 描述性对照与报告项不参与筛选、排序或确认性检验。",
              "- 执行政策与信号模拟之差包含整套止损、冷却、重入路径的差异，不归因为任何单项规则。", ""]
    return "\n".join(lines).encode("utf-8")


def stable_files(snapshot: Snapshot, result: WindowResult, selection: DevelopmentSelection,
                 descriptive: compare.Descriptive, reconciliation: Sequence[compare.ReconciliationRow],
                 run_record: Mapping, usage_restriction: str | None,
                 note: Callable[[str], None] | None = None) -> dict[str, bytes]:
    """全部稳定计算文件（information_state.json 除外，它由调用方最后写出），按写出顺序排列。"""

    def mark(name: str) -> None:
        if note is not None:
            note(name)

    files: dict[str, bytes] = {"run_record.json": json_bytes(run_record)}
    files["input_snapshot.csv.gz"] = serialize_snapshot(snapshot)
    files["window.json"] = json_bytes(window_document(snapshot, result))
    files["selection.json"] = json_bytes(selection_document(selection))
    files["reconciliation.json"] = json_bytes(reconciliation_document(reconciliation))
    files["descriptive.json"] = json_bytes(descriptive_document(descriptive))
    mark("汇总文件")
    files.update(serialize_tables(result, descriptive))
    mark("汇总表")
    files.update(serialize_daily(result, descriptive))
    files.update(serialize_r2(result))
    files["diagnostics.csv.gz"] = serialize_diagnostics(result)
    mark("逐日明细")
    files["report.md"] = render_report(result, selection, usage_restriction)
    return files
