"""拦截插件 tests/v20_data_guard.py 的自测，分两层。

（一）纯逻辑：在进程内用构造输入测试。自测代码自身不调用 sys.addaudithook，也不在本进程加载插件
     （本文件以普通模块的方式导入插件文件，只使用其中的纯函数）。
（二）钩子的实际行为：在相互独立的子进程里各跑一次同一份构造测试，由本进程解析结果比较。
     子进程只在本次自测的临时目录运行，受保护目录指向临时目录中的构造目录，不加载仓库的 conftest 与 pyproject 设置；
     构造测试的源码在写出前经静态检查，不含受保护数据目录的写法。
     这些是隔离措施，不是“子进程未读取真实数据目录”的证明：带拦截运行时，外层拦截器只能把这一层标为“通过但覆盖不完整”。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import v20_data_guard as guard

TESTS_DIR = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# （一）纯逻辑
# ---------------------------------------------------------------------------


def test_mode_parsing() -> None:
    assert guard.parse_mode(None) == guard.BLOCK_MODE          # 未设置：拦截
    assert guard.parse_mode("") == guard.BLOCK_MODE
    assert guard.parse_mode("record") == guard.RECORD_MODE     # 只记录
    for bad in ("block", "RECORD", "off", "0", "record "):
        with pytest.raises(guard.GuardConfigError):
            guard.parse_mode(bad)


def protected_root(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo" / "data" / "guarded"
    return root, guard.normalize(str(root), str(tmp_path))     # type: ignore[return-value]


def test_path_matching(tmp_path: Path) -> None:
    directory, root = protected_root(tmp_path)
    cwd = str(tmp_path / "repo")

    def hit(path: object, base: str = cwd) -> bool:
        return guard.is_protected(guard.normalize(path, base), root)

    assert hit(str(directory))                                              # 受保护目录本身
    assert hit(str(directory / "daily" / "SPX.csv"))                        # 子路径（绝对路径）
    assert hit(directory / "manifest.json")                                 # PathLike
    assert hit(os.fsencode(str(directory / "manifest.json")))               # bytes
    assert hit(os.path.join("data", "guarded", "daily", "QQQ.csv"))         # 相对路径（相对当前目录）
    assert hit(os.path.join("src", "..", "data", "guarded", "x.csv"))       # 含 ..
    assert hit(os.path.join("..", "data", "guarded"), str(tmp_path / "repo" / "tests"))
    assert not hit(str(tmp_path / "repo" / "data" / "guarded2" / "x.csv"))  # 相邻但不在其内
    assert not hit(str(tmp_path / "repo" / "data" / "guarded_backup"))
    assert not hit(str(tmp_path / "repo" / "data"))                         # 上级目录不算命中
    assert not hit(os.path.join("data", "manual", "x.csv"))
    assert not hit(3) and not hit(None) and not hit(True) and not hit("")   # 文件描述符等无法映射为路径
    if WINDOWS:
        assert hit(str(directory).upper()) and hit(str(directory / "DAILY" / "spx.CSV").swapcase())


PATH_CASES = [("open", 0), ("os.listdir", 0), ("os.scandir", 0), ("shutil.copyfile", 0), ("shutil.copyfile", 1),
              ("shutil.copytree", 0), ("shutil.copytree", 1), ("os.rename", 0), ("os.rename", 1), ("os.remove", 0),
              ("sqlite3.connect", 0)]


@pytest.mark.parametrize(("event", "position"), PATH_CASES)
def test_path_event_decisions(tmp_path: Path, event: str, position: int) -> None:
    directory, root = protected_root(tmp_path)
    cwd = str(tmp_path)
    inside, outside = str(directory / "daily" / "SPX.csv"), str(tmp_path / "elsewhere" / "x.csv")
    args = [outside, outside, 0]
    args[position] = inside
    blocked = guard.decide(event, args, guard.BLOCK_MODE, root, cwd)
    assert (blocked.action, blocked.kind) == (guard.BLOCK, guard.KIND_PATH)        # 拦截模式：拦截
    assert blocked.detail == guard.normalize(inside, cwd)
    recorded = guard.decide(event, args, guard.RECORD_MODE, root, cwd)
    assert (recorded.action, recorded.kind) == (guard.RECORD, guard.KIND_PATH)     # 记录模式：记录
    for mode in (guard.BLOCK_MODE, guard.RECORD_MODE):                             # 未命中：忽略
        assert guard.decide(event, [outside, outside, 0], mode, root, cwd).action == guard.IGNORE


@pytest.mark.parametrize("mode", [guard.BLOCK_MODE, guard.RECORD_MODE])
def test_process_and_native_events_are_only_recorded(tmp_path: Path, mode: str) -> None:
    _, root = protected_root(tmp_path)
    arguments = ["git", ["git", "status", "--short"], None, None]
    popen = guard.decide("subprocess.Popen", arguments, mode, root, str(tmp_path))
    assert (popen.action, popen.kind, popen.detail) == (guard.RECORD, guard.KIND_PROCESS, "git status --short")
    system = guard.decide("os.system", ["dir"], mode, root, str(tmp_path))
    assert (system.action, system.kind, system.detail) == (guard.RECORD, guard.KIND_PROCESS, "dir")
    for event in ("ctypes.dlopen", "ctypes.call_function"):
        native = guard.decide(event, ["kernel32", ()], mode, root, str(tmp_path))
        assert (native.action, native.kind) == (guard.RECORD, guard.KIND_NATIVE)
    # 不在清单里的事件、以文件描述符打开的文件：忽略。
    assert guard.decide("import", ["os"], mode, root, str(tmp_path)).action == guard.IGNORE
    assert guard.decide("open", [7, "r", 0], mode, root, str(tmp_path)).action == guard.IGNORE


def record(test: str, kind: str, action: str, event: str = "open", detail: str = "x") -> guard.Record:
    return guard.Record(event, kind, action, detail, test)


def test_four_way_classification_covers_every_row() -> None:
    blocked = [record("t", guard.KIND_PATH, guard.BLOCK)]
    process = [record("t", guard.KIND_PROCESS, guard.RECORD, "subprocess.Popen", "git status")]
    native = [record("t", guard.KIND_NATIVE, guard.RECORD, "ctypes.dlopen", "kernel32")]
    assert guard.classify(guard.OUT_PASSED, []) == guard.PASSED                       # 通过
    assert guard.classify(guard.OUT_PASSED, process) == guard.INCOMPLETE              # 通过但覆盖不完整（子进程）
    assert guard.classify(guard.OUT_PASSED, native) == guard.INCOMPLETE               # 通过但覆盖不完整（ctypes）
    assert guard.classify(guard.OUT_FAILED, []) == guard.FAILED                       # 失败
    assert guard.classify(guard.OUT_FAILED, process) == guard.FAILED
    # 有拦截记录：无论测试结果如何都是“未完成（被拦截）”，包括捕获了拦截异常后通过的测试。
    for outcome in (guard.OUT_PASSED, guard.OUT_FAILED, guard.OUT_SKIPPED, guard.OUT_XFAILED, guard.OUT_XPASSED):
        assert guard.classify(outcome, blocked) == guard.BLOCKED
        assert guard.classify(outcome, [*process, *blocked]) == guard.BLOCKED
    # 跳过、预期失败、意外通过：按 pytest 原结果单列。
    assert guard.classify(guard.OUT_SKIPPED, []) == guard.SKIPPED
    assert guard.classify(guard.OUT_XFAILED, process) == guard.XFAILED
    assert guard.classify(guard.OUT_XPASSED, []) == guard.XPASSED
    # 记录模式下对受保护目录的访问只是记录，不算被拦截。
    assert guard.classify(guard.OUT_PASSED, [record("t", guard.KIND_PATH, guard.RECORD)]) == guard.PASSED
    with pytest.raises(guard.GuardConfigError):
        guard.classify("unknown", [])


def test_summary_keeps_audit_results_for_skipped_and_expected_failures() -> None:
    outcomes = {"a": guard.OUT_PASSED, "b": guard.OUT_PASSED, "c": guard.OUT_PASSED, "d": guard.OUT_FAILED,
                "e": guard.OUT_SKIPPED, "f": guard.OUT_XFAILED, "g": guard.OUT_XPASSED, "h": guard.OUT_SKIPPED,
                "i": guard.OUT_PASSED}
    records = [record("b", guard.KIND_PROCESS, guard.RECORD, "subprocess.Popen", "git status"),
               record("c", guard.KIND_PATH, guard.BLOCK, "open", "P1"),
               record("c", guard.KIND_PATH, guard.BLOCK, "os.listdir", "P2"),
               record("c", guard.KIND_PATH, guard.BLOCK, "open", "P1"),            # 同一路径只列一次
               record("f", guard.KIND_NATIVE, guard.RECORD, "ctypes.dlopen", "lib"),   # 预期失败不掩盖 ctypes 记录
               record("h", guard.KIND_PATH, guard.BLOCK, "open", "P3"),             # 跳过不掩盖拦截
               record("i", guard.KIND_PATH, guard.RECORD, "open", "P4"),            # 记录模式下的访问
               record(guard.SESSION, guard.KIND_NATIVE, guard.RECORD, "ctypes.dlopen", "startup")]
    summary = guard.summarize(outcomes, records)
    assert summary.counts == {guard.PASSED: 2, guard.INCOMPLETE: 1, guard.BLOCKED: 2, guard.FAILED: 1,
                              guard.SKIPPED: 1, guard.XFAILED: 1, guard.XPASSED: 1}
    assert sum(summary.counts.values()) == len(outcomes)
    assert summary.categories["c"] == summary.categories["h"] == guard.BLOCKED
    assert summary.blocked == {"c": ("P1", "P2"), "h": ("P3",)}
    assert summary.sources == {guard.DIRECT: 2, guard.CACHED: 0} and summary.cached == {}
    assert set(summary.uncovered) == {"b", "f", guard.SESSION}      # 会话级的记录没有测试结果，只列入清单
    assert summary.uncovered["b"] == ("subprocess.Popen git status",)
    assert summary.protected_recorded == {"i": ("open P4",)}


def blocked_error(origin: str = "t1", fixture: str | None = "shared") -> guard.V20DataGuardBlocked:
    return guard.V20DataGuardBlocked("open", "P9", origin, fixture)


def test_guard_exception_is_found_by_class_along_the_exception_chain() -> None:
    error = blocked_error()
    assert isinstance(error, PermissionError) and guard.guard_exception(error) is error
    assert (error.event, error.path, error.origin_test, error.fixture) == ("open", "P9", "t1", "shared")
    try:
        try:
            raise error
        except PermissionError as inner:
            raise RuntimeError("wrapped") from inner            # __cause__
    except RuntimeError as outer:
        assert guard.guard_exception(outer) is error
    try:
        try:
            raise error
        except PermissionError:
            raise ValueError("during handling")                 # __context__  # noqa: B904
    except ValueError as outer:
        assert guard.guard_exception(outer) is error
    # 只认类，不认文字：报错文字与拦截器完全相同的普通 PermissionError 不算。
    assert guard.guard_exception(PermissionError(str(error))) is None
    assert guard.guard_exception(None) is None and guard.guard_exception(RuntimeError("x")) is None
    first, second = RuntimeError("a"), RuntimeError("b")        # 成环的异常链也能结束
    first.__context__, second.__context__ = second, first
    assert guard.guard_exception(first) is None


def test_failure_caused_by_a_cached_fixture_block_is_unfinished_not_failed() -> None:
    assert guard.classify(guard.OUT_FAILED, [], cached=True) == guard.BLOCKED
    assert guard.classify(guard.OUT_FAILED, [], cached=False) == guard.FAILED
    # 只对失败或出错的测试起作用：通过、跳过的测试不因此改类。
    assert guard.classify(guard.OUT_PASSED, [], cached=True) == guard.PASSED
    assert guard.classify(guard.OUT_SKIPPED, [], cached=True) == guard.SKIPPED
    outcomes = {"t1": guard.OUT_FAILED, "t2": guard.OUT_FAILED, "t3": guard.OUT_FAILED, "t4": guard.OUT_FAILED,
                "t5": guard.OUT_PASSED}
    records = [record("t1", guard.KIND_PATH, guard.BLOCK, "open", "P9")]
    cached = [guard.CachedBlock("t1", "shared", "t1", "P9"),      # 自己有拦截记录：算直接拦截
              guard.CachedBlock("t2", "shared", "t1", "P9"),      # 夹具缓存
              guard.CachedBlock("t3", None, "t1", "P9"),
              guard.CachedBlock("t5", "shared", "t1", "P9")]      # 没有失败：不归这一类
    summary = guard.summarize(outcomes, records, cached)
    assert summary.counts[guard.BLOCKED] == 3 and summary.counts[guard.FAILED] == 1    # t4 是普通失败
    assert summary.sources == {guard.DIRECT: 1, guard.CACHED: 2}
    assert summary.blocked == {"t1": ("P9",)}
    assert summary.cached == {"t2": ("夹具 shared；最初被拦截于 t1；路径 P9",),
                              "t3": ("夹具 （未知）；最初被拦截于 t1；路径 P9",)}
    assert summary.categories["t4"] == guard.FAILED and summary.categories["t5"] == guard.PASSED
    lines = guard.summary_lines(guard.BLOCK_MODE, "ROOT", summary)
    assert "未完成（被拦截）的来源：直接拦截 1；夹具缓存 2" in lines
    assert "未完成（被拦截）·夹具缓存：2 项" in lines


def test_log_file_content_and_format() -> None:
    records = [record("b", guard.KIND_PROCESS, guard.RECORD, "subprocess.Popen", "git status"),
               record("c", guard.KIND_PATH, guard.BLOCK, "open", "P1")]
    summary = guard.summarize({"a": guard.OUT_PASSED, "b": guard.OUT_PASSED, "c": guard.OUT_FAILED}, records)
    lines = guard.log_lines(guard.BLOCK_MODE, "ROOT", summary, records)
    parsed = [json.loads(line) for line in lines]
    assert len(parsed) == 3 and parsed[0]["type"] == "summary" and parsed[0]["mode"] == guard.BLOCK_MODE
    assert parsed[0]["protected_root"] == "ROOT"
    assert parsed[0]["counts"][guard.BLOCKED] == 1 and parsed[0]["blocked"] == {"c": ["P1"]}
    assert parsed[0]["not_plain_pass"] == {"b": guard.INCOMPLETE, "c": guard.BLOCKED}
    assert parsed[1] == {"type": "record", "event": "subprocess.Popen", "kind": guard.KIND_PROCESS,
                         "action": guard.RECORD, "detail": "git status", "test": "b", "caller": ""}
    assert parsed[0]["blocked_sources"] == {guard.DIRECT: 1, guard.CACHED: 0}
    assert parsed[0]["blocked_by_cached_fixture"] == {}
    # 只有事件名、类别、动作、路径或命令、所属测试，没有任何文件内容字段。
    assert set(parsed[2]) == {"type", "event", "kind", "action", "detail", "test", "caller"}
    text = "\n".join(guard.summary_lines(guard.RECORD_MODE, "ROOT", summary))
    assert "只记录" in text and "未完成（被拦截）·直接拦截：1 项" in text and "c ← P1" in text
    assert "未完成（被拦截）的来源：直接拦截 1；夹具缓存 0" in text


def test_self_test_file_installs_no_hook_in_this_process() -> None:
    """本文件以普通模块导入插件：没有经过 pytest 的插件加载，所以这一份模块对象没有安装钩子、没有会话状态。"""
    assert guard.__name__ == "v20_data_guard" and guard._STATE is None
    source = Path(__file__).read_text(encoding="utf-8")
    assert "addaudit" + "hook(" not in source and "runpytest" + "_inprocess" not in source


# ---------------------------------------------------------------------------
# （二）钩子的实际行为：独立子进程
# ---------------------------------------------------------------------------

CONTENT = bytes(range(256)) * 3 + "构造内容，不是行情".encode()
CONSTRUCTED = '''\
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROTECTED = Path(os.environ["CASE_PROTECTED"])
OUT = Path(os.environ["CASE_OUT"])


def test_pass():
    assert True


def test_fail():
    assert False


def test_skip():
    pytest.skip("constructed skip")


@pytest.mark.xfail(strict=False)
def test_xfail():
    assert False


def test_access():
    data = (PROTECTED / "sample.bin").read_bytes()
    names = sorted(os.listdir(PROTECTED))
    shutil.copyfile(PROTECTED / "sample.bin", OUT / "copy.bin")
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    (OUT / "read.bin").write_bytes(data)
    (OUT / "names.txt").write_text("\\n".join(names), encoding="utf-8")
'''


SHARED_FIXTURE = '''\
import os
from pathlib import Path

import pytest

PROTECTED = Path(os.environ["CASE_PROTECTED"])


@pytest.fixture(scope="module")
def shared():
    return (PROTECTED / "sample.bin").read_bytes()


def test_first(shared):
    assert shared


def test_second(shared):
    assert shared


def test_third(shared):
    assert shared


def test_plain_permission_error():
    raise PermissionError("an ordinary permission error, not raised by the guard")


def test_unrelated():
    assert True
'''


class Child:
    """一次独立子进程运行的结果。"""

    def __init__(self, returncode: int, results: dict[str, str], out: Path, log: Path, output: str) -> None:
        self.returncode, self.results, self.out, self.log, self.output = returncode, results, out, log, output

    def log_entries(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def junit_results(path: Path) -> dict[str, str]:
    results: dict[str, str] = {}
    if not path.is_file():
        return results
    for case in ET.parse(path).getroot().iter("testcase"):
        children = [(child.tag, child.get("type", "")) for child in case]
        failed = "/".join(f"{tag}:{kind}" for tag, kind in children)
        results[case.get("name", "")] = failed if children else "passed"
    return results


def prepare(tmp_path: Path) -> Path:
    """临时目录里的构造受保护目录、最小 pytest.ini 与构造测试文件；写出前静态检查构造测试的源码。"""
    for source in (CONSTRUCTED, SHARED_FIXTURE):
        assert ("data" + "/market") not in source and ("data" + "\\market") not in source
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "sample.bin").write_bytes(CONTENT)
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_constructed.py").write_text(CONSTRUCTED, encoding="utf-8")
    (tmp_path / "test_shared_fixture.py").write_text(SHARED_FIXTURE, encoding="utf-8")
    return protected


def run_child(tmp_path: Path, name: str, plugin: bool, mode: str | None,
              target: str = "test_constructed.py") -> Child:
    protected, out, log = tmp_path / "protected", tmp_path / f"out-{name}", tmp_path / f"log-{name}.jsonl"
    out.mkdir()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("V20_GUARD_")}
    environment.update(CASE_PROTECTED=str(protected), CASE_OUT=str(out), PYTHONPATH=str(TESTS_DIR),
                       V20_GUARD_ROOT=str(protected), V20_GUARD_LOG=str(log), PYTHONIOENCODING="utf-8")
    if mode is not None:
        environment["V20_GUARD_MODE"] = mode
    xml = tmp_path / f"junit-{name}.xml"
    command = [sys.executable, "-m", "pytest", "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path),
               "--basetemp", str(tmp_path / f"basetemp-{name}"), "-p", "no:cacheprovider", "--junitxml", str(xml),
               *(("-p", "v20_data_guard") if plugin else ()), str(tmp_path / target)]
    done = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)
    return Child(done.returncode, junit_results(xml), out, log, done.stdout + done.stderr)


EXPECTED_PLAIN = {"test_pass": "passed", "test_fail": "failure:", "test_skip": "skipped:pytest.skip",
                  "test_xfail": "skipped:pytest.xfail", "test_access": "passed"}


def test_record_mode_changes_no_result_compared_with_no_plugin(tmp_path: Path) -> None:
    """对照一：同一份构造测试，不加载插件与记录模式各在一个独立子进程里运行一次。"""
    prepare(tmp_path)
    plain = run_child(tmp_path, "plain", plugin=False, mode=None)
    recorded = run_child(tmp_path, "record", plugin=True, mode="record")
    assert plain.results == EXPECTED_PLAIN, plain.output
    assert plain.results == recorded.results, recorded.output              # 逐项结果完全相同
    assert plain.returncode == recorded.returncode == 1                    # 退出码相同（有一项构造的失败）
    assert plain.results["test_access"] == "passed" and plain.results["test_fail"].startswith("failure")
    for child in (plain, recorded):                                        # 读到的内容与构造内容逐字节相同
        assert (child.out / "read.bin").read_bytes() == CONTENT
        assert (child.out / "copy.bin").read_bytes() == CONTENT
        assert (child.out / "names.txt").read_text(encoding="utf-8") == "sample.bin"
    assert not plain.log.is_file()                                         # 不加载插件：不产生记录文件
    entries = recorded.log_entries()
    assert entries[0]["type"] == "summary" and entries[0]["mode"] == guard.RECORD_MODE
    access = [item for item in entries[1:] if item["test"].endswith("::test_access")]
    assert all(item["action"] == guard.RECORD for item in entries[1:])     # 记录模式不拦截
    events = {item["event"] for item in access}
    assert {"open", "os.listdir", "shutil.copyfile", "subprocess.Popen"} <= events   # 打开、列举、复制、子进程
    assert any(item["event"] == "open" and item["detail"].endswith("sample.bin") for item in access)
    assert entries[0]["counts"] == {guard.PASSED: 1, guard.INCOMPLETE: 1, guard.BLOCKED: 0, guard.FAILED: 1,
                                    guard.SKIPPED: 1, guard.XFAILED: 1, guard.XPASSED: 0}
    assert "v20_data_guard" in recorded.output and "v20_data_guard" not in plain.output


def test_block_mode_stops_access_before_anything_is_read(tmp_path: Path) -> None:
    """拦截模式：访问受保护目录的那一项归为“未完成（被拦截）”，其余各项与不加载插件时相同。"""
    prepare(tmp_path)
    plain = run_child(tmp_path, "plain", plugin=False, mode=None)
    blocked = run_child(tmp_path, "block", plugin=True, mode=None)
    others = [name for name in plain.results if name != "test_access"]
    assert {name: plain.results[name] for name in others} == {name: blocked.results[name] for name in others}
    assert blocked.results["test_access"].startswith("failure") and "V20DataGuardBlocked" in blocked.output
    assert not (blocked.out / "read.bin").exists() and not (blocked.out / "copy.bin").exists()   # 什么都没有读到
    entries = blocked.log_entries()
    assert entries[0]["mode"] == guard.BLOCK_MODE
    assert entries[0]["counts"] == {guard.PASSED: 1, guard.INCOMPLETE: 0, guard.BLOCKED: 1, guard.FAILED: 1,
                                    guard.SKIPPED: 1, guard.XFAILED: 1, guard.XPASSED: 0}
    (test, paths), = entries[0]["blocked"].items()
    assert test.endswith("::test_access") and len(paths) == 1 and paths[0].endswith("sample.bin")
    assert "未完成（被拦截）·直接拦截：1 项" in blocked.output
    assert entries[0]["blocked_sources"] == {guard.DIRECT: 1, guard.CACHED: 0}


def test_illegal_mode_value_stops_pytest_before_any_test_runs(tmp_path: Path) -> None:
    prepare(tmp_path)
    child = run_child(tmp_path, "bogus", plugin=True, mode="bogus")
    assert child.returncode != 0 and child.results == {}
    assert "V20_GUARD_MODE" in child.output and not child.log.is_file()
    assert not (child.out / "read.bin").exists()


def test_fixture_blocked_once_makes_later_tests_unfinished_by_cached_fixture(tmp_path: Path) -> None:
    """共享夹具被拦截后，pytest 缓存它的异常；后续测试不再触发新的访问，仍归“未完成（被拦截）”，来源为夹具缓存。

    反例在同一份构造测试里：抛出普通 PermissionError 的测试仍归“失败”。
    """
    prepare(tmp_path)
    plain = run_child(tmp_path, "plain", plugin=False, mode=None, target="test_shared_fixture.py")
    assert plain.results == {"test_first": "passed", "test_second": "passed", "test_third": "passed",
                             "test_plain_permission_error": "failure:", "test_unrelated": "passed"}, plain.output
    blocked = run_child(tmp_path, "block", plugin=True, mode=None, target="test_shared_fixture.py")
    assert {name: value.split(":")[0] for name, value in blocked.results.items()} == {
        "test_first": "error", "test_second": "error", "test_third": "error",
        "test_plain_permission_error": "failure", "test_unrelated": "passed"}, blocked.output
    head, *records = blocked.log_entries()
    assert head["counts"] == {guard.PASSED: 1, guard.INCOMPLETE: 0, guard.BLOCKED: 3, guard.FAILED: 1,
                              guard.SKIPPED: 0, guard.XFAILED: 0, guard.XPASSED: 0}
    assert head["blocked_sources"] == {guard.DIRECT: 1, guard.CACHED: 2}
    # 只有第一个测试真的触发了访问：拦截记录只有一条。
    assert [(item["event"], item["action"]) for item in records] == [("open", guard.BLOCK)]
    (direct, paths), = head["blocked"].items()
    assert direct.endswith("::test_first") and paths[0].endswith("sample.bin")
    cached = head["blocked_by_cached_fixture"]
    assert sorted(name.split("::")[1] for name in cached) == ["test_second", "test_third"]
    for items in cached.values():                 # 清单写明夹具名与它最初被拦截的测试
        assert len(items) == 1 and items[0].startswith("夹具 shared；最初被拦截于 ")
        assert "::test_first；路径 " in items[0] and items[0].endswith("sample.bin")
    failed = [name for name, value in head["not_plain_pass"].items() if value == guard.FAILED]
    # 普通 PermissionError 仍是失败。
    assert [name.split("::")[1] for name in failed] == ["test_plain_permission_error"]
    assert "未完成（被拦截）的来源：直接拦截 1；夹具缓存 2" in blocked.output
