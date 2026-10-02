"""数据访问拦截插件（波段预警 v2.0 阶段二起使用；只在显式加载时生效）。

加载：`uv run python -m pytest -p tests.v20_data_guard`。不修改 conftest.py、pyproject.toml 与任何既有测试。

- 默认拦截模式：当前 pytest 进程内，经 Python 的文件打开、目录列举、复制、移动、删除与 sqlite 连接接口访问受保护目录
  （默认是仓库的 data/market/）的行为，在打开或列举之前抛出 PermissionError，并按测试记录。
- `V20_GUARD_MODE=record` 为只记录模式：任何事件都不抛异常，只记录事件名、路径或命令、所属测试。
- 其他取值在插件加载时报错，pytest 不开始运行。
- `V20_GUARD_ROOT` 用一个目录代替受保护目录（只供插件自测）；`V20_GUARD_LOG` 指定记录文件。

不覆盖的途径（不得称为已证明安全）：子进程中的访问；绕过 Python 文件接口的原生代码；os.stat 一类只取元数据的调用；
以文件描述符或 dir_fd 相对路径打开的文件；硬链接与 Windows 8.3 短文件名。子进程与 ctypes 只记录、不拦截。

结构：纯逻辑（解析模式、路径判定、事件决定、按测试汇总、生成记录）写成不改变进程状态的函数；
`sys.addaudithook` 只在本模块被 pytest 作为插件加载时调用一次。只导入本模块不会安装钩子。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

MODE_ENV, LOG_ENV, ROOT_ENV = "V20_GUARD_MODE", "V20_GUARD_LOG", "V20_GUARD_ROOT"
BLOCK_MODE, RECORD_MODE = "block", "record"
DEFAULT_LOG = Path(".pytest_tmp") / "v20_guard_log.jsonl"

BLOCK, RECORD, IGNORE = "拦截", "记录", "忽略"
KIND_PATH, KIND_PROCESS, KIND_NATIVE = "路径", "子进程", "ctypes"

# 带路径的审计事件 → 路径参数的位置。命中受保护目录时，拦截模式下拦截，记录模式下记录。
PATH_EVENTS: Mapping[str, tuple[int, ...]] = {
    "open": (0,),
    "os.listdir": (0,),
    "os.scandir": (0,),
    "os.walk": (0,),
    "glob.glob": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.copymode": (0, 1),
    "shutil.copystat": (0, 1),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "os.truncate": (0,),
    "os.chmod": (0,),
    "os.utime": (0,),
    "sqlite3.connect": (0,),
}
# 子进程：不拦截，只记录。
PROCESS_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                            "os.startfile", "os.startfile/2"})
# ctypes：不拦截，只记录。
NATIVE_EVENTS = frozenset({"ctypes.dlopen", "ctypes.call_function"})
WATCHED = frozenset(PATH_EVENTS) | PROCESS_EVENTS | NATIVE_EVENTS

# 汇总口径
PASSED, INCOMPLETE, BLOCKED, FAILED = "通过", "通过但覆盖不完整", "未完成（被拦截）", "失败"
SKIPPED, XFAILED, XPASSED = "跳过", "预期失败", "意外通过"
CATEGORIES = (PASSED, INCOMPLETE, BLOCKED, FAILED, SKIPPED, XFAILED, XPASSED)
# pytest 原结果
OUT_PASSED, OUT_FAILED, OUT_SKIPPED, OUT_XFAILED, OUT_XPASSED = "passed", "failed", "skipped", "xfailed", "xpassed"
SESSION = "会话级记录（不属于任何测试）"
DIRECT, CACHED = "直接拦截", "夹具缓存"


class GuardConfigError(ValueError):
    """插件的环境变量取值不合法。"""


class V20DataGuardBlocked(PermissionError):
    """拦截器抛出的专用异常。归类时只认这个类（沿异常链查找），不以报错文字判断。"""

    def __init__(self, event: str, path: str, origin_test: str, fixture: str | None) -> None:
        super().__init__(f"v20_data_guard：数据边界拦截，不得访问 {path}（事件 {event}）")
        self.event, self.path, self.origin_test, self.fixture = event, path, origin_test, fixture


@dataclass(frozen=True)
class Decision:
    action: str          # 拦截、记录、忽略
    kind: str            # 路径、子进程、ctypes
    detail: str          # 命中的路径，或子进程命令、ctypes 目标的文字描述


@dataclass(frozen=True)
class Record:
    event: str
    kind: str
    action: str
    detail: str
    test: str
    caller: str = ""     # 子进程与 ctypes 记录的调用来源（最近几层调用的“模块.函数”），用于写明来源


@dataclass(frozen=True)
class CachedBlock:
    """夹具缓存的拦截：某测试失败或出错，异常链里有拦截器的专用异常，但它自己没有拦截记录。"""

    test: str
    fixture: str | None       # 最初被拦截时正在建立的夹具
    origin_test: str          # 夹具最初被拦截时所属的测试
    path: str


# ---------------------------------------------------------------------------
# 纯逻辑：不安装钩子，不改变进程状态
# ---------------------------------------------------------------------------


def parse_mode(value: str | None) -> str:
    """未设置（或空串）为拦截；record 为只记录；其他取值报错。"""
    if value is None or value == "":
        return BLOCK_MODE
    if value == RECORD_MODE:
        return RECORD_MODE
    raise GuardConfigError(f"{MODE_ENV} 只能不设置（拦截）或设为 {RECORD_MODE}（只记录），不能是 {value!r}")


def normalize(path: Any, cwd: str) -> str | None:
    """转为绝对路径，解析符号链接与目录联接，统一大小写；无法映射为路径的参数（如文件描述符）返回 None。"""
    if isinstance(path, bool) or isinstance(path, int) or path is None:
        return None
    try:
        text = os.fsdecode(os.fspath(path))
    except TypeError:
        return None
    if not text:
        return None
    absolute = text if os.path.isabs(text) else os.path.join(cwd, text)
    return os.path.normcase(os.path.realpath(os.path.abspath(absolute)))


def is_protected(normalized: str | None, root: str) -> bool:
    """落在受保护目录之内（含该目录本身）即为命中；root 须已经过 normalize。"""
    if normalized is None:
        return False
    return normalized == root or normalized.startswith(root.rstrip("\\/") + os.sep)


def describe(value: Any) -> str:
    """子进程命令或 ctypes 目标的文字描述；只记录命令，不记录任何文件内容。"""
    if isinstance(value, bytes):
        return os.fsdecode(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return " ".join(describe(item) for item in value)
    return repr(value)


def decide(event: str, args: Sequence[Any], mode: str, root: str, cwd: str) -> Decision:
    """按事件名与参数决定“拦截、记录、忽略”。"""
    if event in PATH_EVENTS:
        for position in PATH_EVENTS[event]:
            if position < len(args):
                normalized = normalize(args[position], cwd)
                if is_protected(normalized, root):
                    return Decision(BLOCK if mode == BLOCK_MODE else RECORD, KIND_PATH, str(normalized))
        return Decision(IGNORE, KIND_PATH, "")
    if event in PROCESS_EVENTS:
        command = args[1] if event == "subprocess.Popen" and len(args) > 1 and args[1] else (args[0] if args else "")
        return Decision(RECORD, KIND_PROCESS, describe(command))
    if event in NATIVE_EVENTS:
        return Decision(RECORD, KIND_NATIVE, describe(args[0]) if args else "")
    return Decision(IGNORE, "", "")


def guard_exception(error: BaseException | None) -> V20DataGuardBlocked | None:
    """沿异常链（__cause__ 与 __context__）查找拦截器的专用异常；没有则为 None。"""
    seen: set[int] = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, V20DataGuardBlocked):
            return current
        pending += [current.__cause__, current.__context__]
    return None


def classify(outcome: str, records: Iterable[Record], cached: bool = False) -> str:
    """一个测试的汇总口径。

    有拦截记录一律为“未完成（被拦截）”，与测试结果无关（包括捕获异常后通过、跳过、预期失败）。
    没有拦截记录、但失败或出错且异常链里有拦截器的专用异常（cached）：也是“未完成（被拦截）”，来源为夹具缓存。
    其余：失败照实为失败；跳过、预期失败、意外通过按 pytest 原结果单列；
    通过且有子进程或 ctypes 记录为“通过但覆盖不完整”；否则为“通过”。
    """
    records = tuple(records)
    if any(item.action == BLOCK for item in records):
        return BLOCKED
    if outcome == OUT_FAILED and cached:
        return BLOCKED
    if outcome == OUT_FAILED:
        return FAILED
    if outcome == OUT_SKIPPED:
        return SKIPPED
    if outcome == OUT_XFAILED:
        return XFAILED
    if outcome == OUT_XPASSED:
        return XPASSED
    if outcome != OUT_PASSED:
        raise GuardConfigError(f"未知的测试结果：{outcome!r}")
    if any(item.kind in (KIND_PROCESS, KIND_NATIVE) for item in records):
        return INCOMPLETE
    return PASSED


@dataclass(frozen=True)
class Summary:
    counts: Mapping[str, int]                          # 七类计数
    categories: Mapping[str, str]                      # 测试 → 口径
    sources: Mapping[str, int]                         # “未完成（被拦截）”的两种来源：直接拦截、夹具缓存
    blocked: Mapping[str, tuple[str, ...]]             # 直接拦截：有拦截记录的测试 → 路径
    cached: Mapping[str, tuple[str, ...]]              # 夹具缓存：测试 → “夹具名；最初被拦截的测试；路径”
    uncovered: Mapping[str, tuple[str, ...]]           # 有子进程或 ctypes 记录的测试（不论结果）→ 命令或目标
    protected_recorded: Mapping[str, tuple[str, ...]]  # 记录模式下访问了受保护目录的测试 → “事件 路径”


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def summarize(outcomes: Mapping[str, str], records: Sequence[Record],
              cached_blocks: Sequence[CachedBlock] = ()) -> Summary:
    """按测试汇总。只在记录里出现、没有 pytest 结果的归属（会话、收集阶段）不计入七类计数，但照样列入清单。"""
    by_test: dict[str, list[Record]] = {}
    for item in records:
        by_test.setdefault(item.test, []).append(item)
    blocked = {test: _unique(item.detail for item in items if item.action == BLOCK)
               for test, items in by_test.items() if any(item.action == BLOCK for item in items)}
    cached: dict[str, tuple[str, ...]] = {}
    for item in cached_blocks:
        if item.test in blocked or outcomes.get(item.test) != OUT_FAILED:
            continue          # 自己有拦截记录的算直接拦截；没有失败或出错的不归这一类
        text = f"夹具 {item.fixture or '（未知）'}；最初被拦截于 {item.origin_test}；路径 {item.path}"
        cached[item.test] = _unique((*cached.get(item.test, ()), text))
    categories = {test: classify(outcome, by_test.get(test, ()), test in cached)
                  for test, outcome in outcomes.items()}
    counts = {name: sum(1 for value in categories.values() if value == name) for name in CATEGORIES}
    sources = {DIRECT: sum(1 for test, value in categories.items() if value == BLOCKED and test not in cached),
               CACHED: sum(1 for test, value in categories.items() if value == BLOCKED and test in cached)}
    uncovered = {test: _unique(f"{item.event} {item.detail}" for item in items
                               if item.kind in (KIND_PROCESS, KIND_NATIVE))
                 for test, items in by_test.items() if any(item.kind in (KIND_PROCESS, KIND_NATIVE) for item in items)}
    protected = {test: _unique(f"{item.event} {item.detail}" for item in items
                               if item.kind == KIND_PATH and item.action == RECORD)
                 for test, items in by_test.items()
                 if any(item.kind == KIND_PATH and item.action == RECORD for item in items)}
    return Summary(counts, categories, sources, blocked, cached, uncovered, protected)


def log_lines(mode: str, root: str, summary: Summary, records: Sequence[Record]) -> list[str]:
    """记录文件的内容：第一行是汇总，其后每条访问记录一行（JSON）。只含事件名、路径或命令、所属测试。"""
    head = {"type": "summary", "mode": mode, "protected_root": root, "counts": dict(summary.counts),
            "blocked_sources": dict(summary.sources),
            "blocked": {test: list(paths) for test, paths in summary.blocked.items()},
            "blocked_by_cached_fixture": {test: list(items) for test, items in summary.cached.items()},
            "uncovered": {test: list(items) for test, items in summary.uncovered.items()},
            "protected_recorded": {test: list(items) for test, items in summary.protected_recorded.items()},
            "not_plain_pass": {test: value for test, value in summary.categories.items() if value != PASSED}}
    lines = [json.dumps(head, ensure_ascii=False, sort_keys=True)]
    lines += [json.dumps({"type": "record", "event": item.event, "kind": item.kind, "action": item.action,
                          "detail": item.detail, "test": item.test, "caller": item.caller},
                         ensure_ascii=False, sort_keys=True)
              for item in records]
    return lines


def summary_lines(mode: str, root: str, summary: Summary) -> list[str]:
    """终端摘要追加的段落。"""
    lines = [f"模式：{'拦截' if mode == BLOCK_MODE else '只记录'}；受保护目录：{root}",
             "；".join(f"{name} {summary.counts[name]}" for name in CATEGORIES),
             f"未完成（被拦截）的来源：{DIRECT} {summary.sources[DIRECT]}；{CACHED} {summary.sources[CACHED]}"]
    for title, mapping in (("未完成（被拦截）·直接拦截", summary.blocked),
                           ("未完成（被拦截）·夹具缓存", summary.cached),
                           ("有子进程或 ctypes 记录（覆盖不完整）", summary.uncovered),
                           ("记录到对受保护目录的访问", summary.protected_recorded)):
        lines.append(f"{title}：{len(mapping)} 项")
        lines += [f"  {test} ← {'；'.join(items)}" for test, items in mapping.items()]
    return lines


# ---------------------------------------------------------------------------
# 钩子安装与 pytest 接口：只在作为插件加载时生效
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    mode: str
    root: str
    records: list[Record] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)
    cached: list[CachedBlock] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)      # 正在建立的夹具（栈）
    current: str = SESSION
    local: threading.local = field(default_factory=threading.local)


_STATE: _Session | None = None


def _audit(event: str, args: tuple[Any, ...]) -> None:
    state = _STATE
    if state is None or event not in WATCHED or getattr(state.local, "busy", False):
        return
    state.local.busy = True
    try:
        decision = decide(event, args, state.mode, state.root, os.getcwd())
    except Exception:    # 判定本身出错时不得影响被测代码
        return
    finally:
        state.local.busy = False
    if decision.action == IGNORE:
        return
    caller = _caller() if decision.kind in (KIND_PROCESS, KIND_NATIVE) else ""
    state.records.append(Record(event, decision.kind, decision.action, decision.detail, state.current, caller))
    if decision.action == BLOCK:
        raise V20DataGuardBlocked(event, decision.detail, state.current,
                                  state.fixtures[-1] if state.fixtures else None)


def _caller() -> str:
    """子进程与 ctypes 事件的调用来源：最近几层不属于 ctypes、subprocess 与本插件的“模块.函数”。"""
    names: list[str] = []
    frame = sys._getframe(2)
    while frame is not None and len(names) < 4:
        module = str(frame.f_globals.get("__name__", "?"))
        if module.split(".")[0] not in ("ctypes", "subprocess") and not module.endswith("v20_data_guard"):
            names.append(f"{module}.{frame.f_code.co_name}")
        frame = frame.f_back
    return " ← ".join(names)


def _install() -> None:
    """安装审计钩子（整个进程只一次）。取值不合法时在此报错，pytest 不开始运行。"""
    global _STATE
    if _STATE is not None:
        return
    try:
        mode = parse_mode(os.environ.get(MODE_ENV))
    except GuardConfigError as error:
        raise pytest.UsageError(str(error)) from error
    configured = os.environ.get(ROOT_ENV)
    protected = configured if configured else str(Path(__file__).resolve().parents[1] / "data" / "market")
    root = normalize(protected, os.getcwd())
    if root is None:
        raise pytest.UsageError(f"{ROOT_ENV} 不是可用的路径：{protected!r}")
    _STATE = _Session(mode, root)
    sys.addaudithook(_audit)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: Any, parser: Any, args: Any) -> None:
    """以 -p 加载时最早被调用的钩子：早于 conftest 与测试模块的导入。"""
    _install()


def pytest_configure(config: Any) -> None:
    _install()


def pytest_collectstart(collector: Any) -> None:
    if _STATE is not None:
        _STATE.current = f"收集：{collector.nodeid or '（根）'}"


def pytest_collectreport(report: Any) -> None:
    if _STATE is not None and report.failed:
        _STATE.outcomes[f"收集：{report.nodeid or '（根）'}"] = OUT_FAILED


def pytest_collection_finish(session: Any) -> None:
    if _STATE is not None:
        _STATE.current = SESSION


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    if _STATE is not None:
        _STATE.current = nodeid


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    if _STATE is not None:
        _STATE.current = SESSION


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: Any) -> Any:
    """记下正在建立的夹具名，供拦截时写入专用异常；不改变夹具的结果。"""
    if _STATE is None:
        yield
        return
    _STATE.fixtures.append(str(fixturedef.argname))
    try:
        yield
    finally:
        _STATE.fixtures.pop()


def pytest_runtest_makereport(item: Any, call: Any) -> None:
    """只读取异常：异常链里有拦截器的专用异常时记下来，供归类使用；不修改报告。"""
    if _STATE is None or call.excinfo is None:
        return
    found = guard_exception(call.excinfo.value)
    if found is not None:
        _STATE.cached.append(CachedBlock(item.nodeid, found.fixture, found.origin_test, found.path))


def pytest_runtest_logreport(report: Any) -> None:
    """按 pytest 的原结果记账；不修改任何结果。"""
    if _STATE is None:
        return
    previous = _STATE.outcomes.get(report.nodeid)
    xfail = hasattr(report, "wasxfail")
    if report.failed:
        outcome = OUT_FAILED
    elif report.skipped:
        outcome = OUT_XFAILED if xfail else OUT_SKIPPED
    elif report.when == "call":
        outcome = OUT_XPASSED if xfail else OUT_PASSED
    else:
        return
    if previous != OUT_FAILED:
        _STATE.outcomes[report.nodeid] = outcome


def _current_summary() -> Summary | None:
    if _STATE is None:
        return None
    return summarize(dict(_STATE.outcomes), tuple(_STATE.records), tuple(_STATE.cached))


def pytest_terminal_summary(terminalreporter: Any) -> None:
    summary = _current_summary()
    if _STATE is None or summary is None:
        return
    terminalreporter.section("v20_data_guard")
    for line in summary_lines(_STATE.mode, _STATE.root, summary):
        terminalreporter.write_line(line)


def pytest_sessionfinish(session: Any, exitstatus: Any) -> None:
    """会话结束时写出记录文件；不修改退出码。"""
    summary = _current_summary()
    if _STATE is None or summary is None:
        return
    target = Path(os.environ.get(LOG_ENV) or DEFAULT_LOG)
    records = tuple(_STATE.records)
    _STATE.local.busy = True          # 写记录文件本身不再触发判定
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(log_lines(_STATE.mode, _STATE.root, summary, records)) + "\n", encoding="utf-8")
    finally:
        _STATE.local.busy = False
