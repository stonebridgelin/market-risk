"""阶段四开发期运行入口（development_run）的子进程测试：构造端到端演习与确定性验收（M2 第一部分指令第三节）。

所有调用 main 的测试都在子进程中运行（钩子安装不可撤销）。构造仓库根在 tmp_path 下：git init 后提交的构造源码占位、
构造 config/ 与按 NYSE 日历生成的构造行情（SPX 1990-01-02 起、QQQ 1999-03-10 起，至截止日之后若干行）。
路径一律以 root.joinpath(...) 拼接。不读真实研究数据或真实项目配置。
带拦截运行时这些测试照实标“通过但覆盖不完整”（子进程中的访问不在外层拦截范围内）。
注入类情形（选择阶段非有限值、对照计算失败、读取证据核实为“否”、写入失败、封存后访问）在子进程内预先导入业务模块后
替换函数，因此不检验钩子安装时点；钩子安装时点由不注入的情形与隔离检查保证。
每种情形都经下面的只读外部判定函数 judge 断言工程完成状态（M2 指令第二节第 7 小节第 7、8 条），不单凭尾段 outcome。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from wavewarn_v20_helpers import random_closes

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn_v20.development_output import PREFLIGHT_FILES

MODULE = "market_risk.wavewarn_v20.development_run"
SEALED = "【封存后访问】"
PREFIX_USAGE = "前缀诊断运行（严格读取未通过：缺少必需价格）"
FIRST = {"SPX": dt.date(1990, 1, 2), "QQQ": dt.date(1999, 3, 10)}
LAST = dt.date(2017, 1, 31)                                      # 截止日 2016-12-30 之后再写若干行
GIT_ENV = {"GIT_AUTHOR_NAME": "构造", "GIT_AUTHOR_EMAIL": "construct@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_NAME": "构造",
           "GIT_COMMITTER_EMAIL": "construct@example.invalid", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}
DECISIONS = "- date: 2001-01-04\n  symbol: SPY\n  decision: exclude\n  reason: 构造\n  decided_on: 2001-06-01\n"


# ---------------------------------------------------------------------------
# 构造与读取的辅助（文件写入、读取、存在性检查与子进程各集中在一处）
# ---------------------------------------------------------------------------


def put(path: Path, data: bytes) -> Path:
    """把构造的字节写进临时目录（必要时建立上级目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def get(path: Path) -> bytes:
    """读取演习输出（只读）。"""
    return path.read_bytes()


def present(path: Path) -> bool:
    return path.exists()


def run_process(arguments: list[str], cwd: Path, extra: dict | None = None) -> subprocess.CompletedProcess:
    """在新的进程中运行（git 或 sys.executable）；工作目录为构造根。"""
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", **(extra or {})}
    return subprocess.run(arguments, cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)


def git(root: Path, *arguments: str) -> None:
    done = run_process(["git", "-c", "core.autocrlf=false", *arguments], root, GIT_ENV)
    assert done.returncode == 0, done.stderr


def market_lines(asset: str, seed: int, changes: dict | None = None) -> list[tuple[dt.date, str]]:
    days = stock_trading_days(FIRST[asset], LAST)
    closes = random_closes(seed, len(days))
    rows = [(day, str(close)) for day, close in zip(days, closes, strict=True)]
    for index, (day, value) in enumerate(rows):
        if changes and day in changes:
            rows[index] = (day, changes[day](value) if callable(changes[day]) else changes[day])
    return rows


def csv_text(rows: list[tuple[dt.date, str]], source: str = "yahoo", sources: dict | None = None) -> bytes:
    lines = ["date,value,open,high,low,close,volume,source"]
    for day, value in rows:
        lines.append(f"{day},{value},IGN_OPEN,IGN_HIGH,IGN_LOW,IGN_CLOSE,IGN_VOLUME,{(sources or {}).get(day, source)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def registered_yaml(files: dict[str, bytes]) -> bytes:
    """由构造的字节直接算出登记值（不经被测代码）。"""
    lines = ["dataset:"]
    for asset, raw in files.items():
        data = [line for line in raw.decode("utf-8").split("\n")[1:] if line.strip()]
        lines += [f"  {asset}:", f"    raw_sha256: {hashlib.sha256(raw).hexdigest()}",
                  f"    normalized_sha256: {hashlib.sha256(raw).hexdigest()}", f"    data_rows: {len(data)}",
                  f"    first_date: {data[0].split(',')[0]}", f"    last_date: {data[-1].split(',')[0]}"]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_root(root: Path, spx: list | None = None, qqq: list | None = None,
               spx_sources: dict | None = None) -> Path:
    """构造仓库根：预检文件占位、两份配置、两份八列行情；git init 后提交（固定作者与时间，两次构造得同一提交号）。"""
    files = {"SPX": csv_text(spx or market_lines("SPX", 11), sources=spx_sources),
             "QQQ": csv_text(qqq or market_lines("QQQ", 12))}
    for relative in PREFLIGHT_FILES:
        if not relative.startswith("config/"):
            put(root.joinpath(*relative.split("/")), f"# 构造占位：{relative}\n".encode())
    put(root.joinpath("config", "wavewarn_v20.yaml"), registered_yaml(files))
    put(root.joinpath("config", "data_decisions.yaml"), DECISIONS.encode("utf-8"))
    for asset, raw in files.items():
        put(root.joinpath("data", "market", "daily", f"{asset}.csv"), raw)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "构造")
    return root


def run_entry(root: Path) -> subprocess.CompletedProcess:
    return run_process([sys.executable, "-B", "-m", MODULE, "--root", str(root)], root)


INJECT_PREFIX = (
    "import dataclasses, math, sys, atexit\n"
    "from pathlib import Path\n"
    "from market_risk.wavewarn_v20 import development_run, data_v20, research_run, selection\n"
    "from market_risk.wavewarn_v20 import development_output as output, development_compare as compare\n"
    "ROOT = Path(sys.argv[1])\n"
)


def run_injected(root: Path, patch: str) -> subprocess.CompletedProcess:
    script = INJECT_PREFIX + patch + "\nsys.exit(development_run.main(['--root', str(ROOT)]))\n"
    return run_process([sys.executable, "-B", "-c", script, str(root)], root)


def research_dir(root: Path) -> Path:
    return root.joinpath("reports", "research", "wavewarn_v20")


def listing(root: Path) -> dict[str, list[str]]:
    """研究目录下的目录与包外文件（名称按类别）。"""
    base = research_dir(root)
    if not present(base):
        return {}
    names = sorted(item.name for item in base.iterdir())
    return {"formal": [name for name in names if name == "evaluation_development"],
            "failed": [name for name in names if name.startswith("evaluation_development_failed_")],
            "staging": [name for name in names if name.startswith(".staging_evaluation_development_")],
            "tail": [name for name in names if name.startswith("evaluation_development_audit_tail_")],
            "note": [name for name in names if name.startswith("evaluation_development_publish_failure_")]}


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in get(path).decode("utf-8").splitlines() if line.strip()]


def files_in(directory: Path) -> set[str]:
    return {item.name for item in directory.iterdir()}


def stdout_report(done: subprocess.CompletedProcess) -> dict:
    return json.loads(done.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# 只读外部判定函数（M2 指令第二节第 7 小节第 7 条）
# ---------------------------------------------------------------------------


def judge(exit_code: int | None, stderr: str | None, root: Path) -> dict:
    """输入子进程退出码、完整标准错误与构造根目录；输出工程完成状态与逐项条件结果。只读。"""
    conditions: dict[str, bool] = {}
    conditions["1 退出码取得且为 0"] = exit_code == 0
    conditions["2 标准错误完整取得且无封存后访问行"] = stderr is not None and not any(
        line.startswith(SEALED) for line in stderr.splitlines())
    found = listing(root)
    formal = research_dir(root) / "evaluation_development"
    tails = found.get("tail", [])
    tail_ok = links = violations = False
    try:                                         # 尾段不完整（空或末行截断）即不能解析：条件 3 不成立（补充七）
        records = lines_of(research_dir(root) / tails[0]) if len(tails) == 1 else []
    except ValueError:
        records = []
    if len(tails) == 1 and present(formal) and len(records) >= 2:
        link, close, events = records[0], records[-1], records[1:-1]
        package = lines_of(formal / "audit_log.jsonl")
        coverage = package[-1]
        audit_sha = hashlib.sha256(get(formal / "audit_log.jsonl")).hexdigest()
        manifest_sha = hashlib.sha256(get(formal / "MANIFEST.sha256")).hexdigest() if present(
            formal / "MANIFEST.sha256") else None
        sequence = [item["seq"] for item in events]
        tail_ok = close.get("record") == "close" and close.get("outcome") == "成功"
        links = (link.get("record") == "link" and link.get("audit_log_sha256") == audit_sha
                 and link.get("manifest_file") == "MANIFEST.sha256" and link.get("manifest_sha256") == manifest_sha
                 and bool(sequence) and coverage["last_seq"] + 1 == sequence[0]
                 and sequence == list(range(sequence[0], sequence[0] + len(sequence))))
        violations = coverage.get("violations_in_segment") == 0 and close.get("violations_in_tail") == 0
    conditions["3 尾段收尾为成功且衔接校验通过"] = tail_ok and links
    conditions["4 包内段与尾段违规数均为 0"] = violations
    conditions["5 不存在同批发布失败说明文件"] = not found.get("note")
    return {"工程完成状态": "成功" if all(conditions.values()) else "非成功", "条件": conditions}


# ---------------------------------------------------------------------------
# 共用的构造根与正常运行（第三节第 1、2 项；第 10 项耗时与体积）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def normal(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, subprocess.CompletedProcess, float]:
    root = build_root(tmp_path_factory.mktemp("normal"))
    started = time.monotonic()
    done = run_entry(root)
    return root, done, time.monotonic() - started


def test_normal_success_path(normal: tuple[Path, subprocess.CompletedProcess, float], record_property) -> None:
    """第三节第 1 项：退出码 0；标准错误完整且无封存后访问行；尾段成功、衔接与违规检查通过；无发布失败说明。"""
    root, done, seconds = normal
    assert done.returncode == 0, done.stderr
    verdict = judge(done.returncode, done.stderr, root)
    assert verdict["工程完成状态"] == "成功", verdict
    found = listing(root)
    assert found["formal"] == ["evaluation_development"] and found["failed"] == found["staging"] == found["note"] == []
    assert len(found["tail"]) == 1
    formal = research_dir(root) / "evaluation_development"
    expected = {"run_record.json", "information_state.json", "input_snapshot.csv.gz", "window.json", "selection.json",
                "reconciliation.json", "descriptive.json", "candidates_summary.csv", "reference_summary.csv",
                "segments.csv", "environments.csv", "exposure_substitution.csv", "daily_signals.csv.gz",
                "daily_targets.csv.gz", "daily_nav.csv.gz", "daily_policy.csv.gz", "r2_events.csv.gz",
                "r2_judgements.csv.gz", "segment_ledgers.csv.gz", "diagnostics.csv.gz", "report.md",
                "run_environment.json", "audit_log.jsonl", "MANIFEST.sha256",
                # 修正批 K1（契约例外 E7）新增的四个文件
                "channel_events.csv.gz", "system_transitions.csv.gz", "switches.csv.gz", "switch_stages.csv"}
    assert files_in(formal) == expected
    record = json.loads(get(formal / "run_record.json"))
    # 修正批 E1：用途限制为数组，无限制时为空数组
    assert record["usage_restriction"] == [] and record["output_scheme"] == "v20-stage4-output-1"
    assert record["audit_conclusion"]["violation_found_until_this_file"] is False
    state = json.loads(get(formal / "information_state.json"))
    assert state["performance_info_status"] == "已产生" and state["s21_facts"]["s21_class"] == "待核对"
    assert set(state["data_read"]) == {"SPX", "QQQ"} and all(item["read"] for item in state["data_read"].values())
    assert state["stage_trace"][-1] == {"seq": state["stage_trace"][-1]["seq"], "stage": "写出", "action": "进入"}
    report = get(formal / "report.md").decode("utf-8").splitlines()
    assert report[0].startswith("# ") and report[1] == "" and PREFIX_USAGE not in report
    assert_tail(root, formal, "MANIFEST.sha256", "成功")
    # 读取审计：两份行情文件各恰一次、只读；配置只读；其余不在受保护目录。
    events = [item for item in lines_of(formal / "audit_log.jsonl")[:-1]]
    market = [item for item in events if Path(item["path"]).parent.name == "daily"]
    assert sorted(Path(item["path"]).name for item in market) == ["QQQ.csv", "SPX.csv"]
    assert all(item["mode"] == "r" and item["violation"] is None for item in market)
    sizes = {item.name: len(get(item)) for item in formal.iterdir()}
    record_property("运行耗时秒", round(seconds, 1))
    record_property("正式目录总字节", sum(sizes.values()))
    record_property("各文件字节", json.dumps(sizes, ensure_ascii=False, sort_keys=True))
    assert stdout_report(done)["manifest_sha256"] == hashlib.sha256(get(formal / "MANIFEST.sha256")).hexdigest()


def test_determinism_across_two_roots(normal: tuple[Path, subprocess.CompletedProcess, float],
                                      tmp_path: Path) -> None:
    """第三节第 2 项：同一构造输入在两个不同构造根下各运行一次，均满足外部成功条件；稳定计算文件逐字节相同；
    两份 MANIFEST.sha256 逐行比较，不同行恰限于 run_environment.json、audit_log.jsonl。"""
    first_root, first, _ = normal
    second_root = build_root(tmp_path / "second")
    second = run_entry(second_root)
    for root, done in ((first_root, first), (second_root, second)):
        assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "成功", done.stderr
    one, two = (research_dir(root) / "evaluation_development" for root in (first_root, second_root))
    assert files_in(one) == files_in(two)
    for name in sorted(files_in(one) - {"run_environment.json", "audit_log.jsonl", "MANIFEST.sha256"}):
        assert get(one / name) == get(two / name), name
    lines_one = get(one / "MANIFEST.sha256").decode("utf-8").splitlines()
    lines_two = get(two / "MANIFEST.sha256").decode("utf-8").splitlines()
    assert len(lines_one) == len(lines_two)
    different = sorted(a.split(" ", 2)[2] for a, b in zip(lines_one, lines_two, strict=True) if a != b)
    assert different == ["audit_log.jsonl", "run_environment.json"]


# ---------------------------------------------------------------------------
# 第三节第 3 项：预检拒绝（各退出码 2；未读入任何行情）
# ---------------------------------------------------------------------------


def assert_rejected(done: subprocess.CompletedProcess, root: Path) -> dict:
    assert done.returncode == 2, done.stderr
    report = stdout_report(done)
    state = report["information_state"]
    assert state["data_read"] == {} and state["performance_info_status"] == "未产生"
    assert not [item for item in report["open_events"] if Path(item["path"]).parent.name == "daily"]
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"
    return report


def test_preflight_rejects_existing_formal_directory(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    research_dir(root).joinpath("evaluation_development").mkdir(parents=True)
    assert "正式目录已存在" in assert_rejected(run_entry(root), root)["reason"]
    assert listing(root)["staging"] == [] and listing(root)["tail"] == []


def test_preflight_rejects_dirty_worktree(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    put(root / "untracked.txt", b"x\n")
    assert "工作区不干净" in assert_rejected(run_entry(root), root)["reason"]


def test_preflight_rejects_staged_changes(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    put(root / "pyproject.toml", b"# changed\n")
    git(root, "add", "pyproject.toml")
    reason = assert_rejected(run_entry(root), root)["reason"]
    assert "不干净" in reason or "暂存区不为空" in reason


def test_preflight_rejects_missing_preflight_file(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    git(root, "rm", "-q", "src/market_risk/wavewarn_v20/registered_v20.py")
    git(root, "commit", "-q", "-m", "构造：删去一个预检文件")
    assert "预检文件缺失" in assert_rejected(run_entry(root), root)["reason"]


# ---------------------------------------------------------------------------
# 第三节第 4 项：读取停止
# ---------------------------------------------------------------------------


def failed_dir(root: Path) -> Path:
    found = listing(root)
    assert found["formal"] == [] and found["staging"] == [] and len(found["failed"]) == 1, found
    return research_dir(root) / found["failed"][0]


def assert_tail(root: Path, package: Path, manifest_name: str | None, outcome: str) -> list[dict]:
    """M2 指令第二节第 7 小节第 8 条：尾段首行（衔接记录）、末行（收尾记录）、序号连续、衔接哈希、coverage_end 与
    not_covered；包内 audit_log.jsonl 末行覆盖记录的 last_seq 与尾段首个事件序号相差 1。"""
    tails = listing(root)["tail"]
    assert len(tails) == 1
    records = lines_of(research_dir(root) / tails[0])
    link, close, events = records[0], records[-1], records[1:-1]
    assert link["record"] == "link" and Path(link["package_dir"]).name == package.name
    assert close["record"] == "close" and close["outcome"] == outcome
    assert close["coverage_end"] == "尾段文件关闭前最后一次取事件" and len(close["not_covered"]) == 5
    sequence = [item["seq"] for item in events]
    assert sequence and sequence == list(range(sequence[0], sequence[0] + len(sequence)))
    assert close["last_seq"] == sequence[-1]
    if present(package / "audit_log.jsonl"):
        coverage = lines_of(package / "audit_log.jsonl")[-1]
        assert link["audit_log_sha256"] == hashlib.sha256(get(package / "audit_log.jsonl")).hexdigest()
        assert link["audit_log_last_seq"] == coverage["last_seq"] and coverage["last_seq"] + 1 == sequence[0]
    else:
        assert link["audit_log_sha256"] is None and link["audit_log_last_seq"] is None
    if manifest_name is None:
        assert link["manifest_file"] is None
    else:
        assert link["manifest_file"] == manifest_name
        assert link["manifest_sha256"] == hashlib.sha256(get(package / manifest_name)).hexdigest()
    return records


def assert_failed_before_manifest(root: Path, done: subprocess.CompletedProcess) -> tuple[dict, dict]:
    assert done.returncode == 1, done.stderr
    directory = failed_dir(root)
    names = files_in(directory)
    assert {"failure.json", "information_state_final.json", "audit_log.jsonl", "FAILURE_MANIFEST.sha256"} <= names
    assert "MANIFEST.sha256" not in names and "information_state.json" not in names
    assert_tail(root, directory, "FAILURE_MANIFEST.sha256", "失败")
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"
    return json.loads(get(directory / "failure.json")), json.loads(get(directory / "information_state_final.json"))


def test_strict_input_error_stops_without_diagnosis(tmp_path: Path) -> None:
    rows = market_lines("SPX", 11)
    bad = rows[3000][0]
    root = build_root(tmp_path / "root", spx=rows, spx_sources={bad: "tiingo"})
    failure, state = assert_failed_before_manifest(root, run_entry(root))
    assert failure["exit"] == "输入校验失败" and failure["stage"] == "严格读取"
    assert failure["exception"]["type"] == "DataInputError" and failure["usage_restriction"] == []    # 修正批 E1
    assert state["evaluation_prices_parsed"]["SPX"]["diagnosis_started"] is False
    assert state["performance_info_status"] == "未产生"


def test_k4_case11_read_stop_leaves_the_coverage_undetermined(tmp_path: Path) -> None:
    """修正批 K4 第 11、14 例：读取停止（组合层未调用）：window_coverage 为“未判定：组合层未返回”，数组为空；
    failure.json、information_state_final.json、标准输出逐字节相等。"""
    rows = market_lines("SPX", 11)
    root = build_root(tmp_path / "root", spx=rows, spx_sources={rows[3000][0]: "tiingo"})
    done = run_entry(root)
    failure, state = assert_failed_before_manifest(root, done)
    assert failure["stage"] == "严格读取" and state["s21_facts"]["组合层已调用"] is False
    assert_same_coverage(failure_documents(root, done), [], undetermined("未判定：组合层未返回"))


def test_diagnosis_input_error_after_a_missing_price(tmp_path: Path) -> None:
    rows = market_lines("SPX", 11)
    first, later = rows[3000][0], rows[3100][0]
    root = build_root(tmp_path / "root", spx=market_lines("SPX", 11, {first: "", later: "abc"}))
    failure, state = assert_failed_before_manifest(root, run_entry(root))
    assert failure["exit"] == "输入校验失败" and failure["stage"] == "诊断读取"
    evidence = failure["detail"]["evidence"]["SPX"]
    assert evidence["strict_error"]["type"] == "MissingPriceEntryError"
    assert evidence["strict_error"]["missing_value_days"] == [first.isoformat()]
    assert evidence["diagnosis_error"]["type"] == "DataInputError"
    assert state["composition_called"] if "composition_called" in state else state["s21_facts"]["组合层已调用"] is False


def test_diagnosis_evidence_check_no_is_unexpected(tmp_path: Path) -> None:
    rows = market_lines("SPX", 11)
    missing = rows[3000][0]
    root = build_root(tmp_path / "root", spx=market_lines("SPX", 11, {missing: ""}))
    patch = ("real = data_v20.diagnose_until\n"
             "def wrong(*arguments):\n"
             "    found = real(*arguments)\n"
             "    return dataclasses.replace(found, missing_value_days=())\n"
             "data_v20.diagnose_until = wrong\n")
    failure, _ = assert_failed_before_manifest(root, run_injected(root, patch))
    assert failure["exit"] == "未预期异常" and failure["stage"] == "诊断核实"
    checks = failure["detail"]["evidence"]["SPX"]["evidence_check"]
    assert checks == [{"kind": "missing_value_days", "day": missing.isoformat(), "result": "否"}]


def test_unexpected_diagnosis_error_keeps_both_pieces_of_evidence(tmp_path: Path) -> None:
    """补充六第一节第 2 小节：严格读取抛 MissingPriceEntryError 后，诊断读取抛 RuntimeError（注入）：退出码 1；
    failure.json 同时含 strict_error 与诊断异常的原始类型、消息、调用栈；出口“未预期异常”；未进入组合层；非成功。"""
    rows = market_lines("SPX", 11)
    missing = rows[3000][0]
    root = build_root(tmp_path / "root", spx=market_lines("SPX", 11, {missing: ""}))
    patch = ("def broken(*arguments):\n"
             "    raise RuntimeError('构造：诊断读取内部错误')\n"
             "data_v20.diagnose_until = broken\n")
    done = run_injected(root, patch)
    assert done.returncode == 1, done.stderr
    failure, state = assert_failed_before_manifest(root, done)
    assert failure["exit"] == "未预期异常" and failure["stage"] == "读取行情（诊断读取）"
    strict = failure["detail"]["strict_error"]
    assert strict["type"] == "MissingPriceEntryError" and strict["missing_value_days"] == [missing.isoformat()]
    for record in (failure["exception"], failure["detail"]["diagnosis_error"]):
        assert record["type"] == "RuntimeError" and record["message"] == "构造：诊断读取内部错误"
        assert "Traceback" in record["traceback"] and "broken" in record["traceback"]
    assert state["s21_facts"]["组合层已调用"] is False and state["performance_info_status"] == "未产生"


def test_missing_price_error_from_diagnosis_is_an_inconsistent_reading(tmp_path: Path) -> None:
    """补充六第一节第 3 小节：诊断读取抛 MissingPriceEntryError（注入）即读取实现不一致：出口“未预期异常”，
    两份证据同时保存，未进入组合层，非成功。"""
    rows = market_lines("SPX", 11)
    missing = rows[3000][0]
    root = build_root(tmp_path / "root", spx=market_lines("SPX", 11, {missing: ""}))
    patch = ("def broken(*arguments):\n"
             "    raise data_v20.MissingPriceEntryError('价格为空', asset='SPX')\n"
             "data_v20.diagnose_until = broken\n")
    failure, state = assert_failed_before_manifest(root, run_injected(root, patch))
    assert failure["exit"] == "未预期异常" and failure["stage"] == "读取行情（诊断读取）"
    assert failure["detail"]["detail"] == "读取实现不一致"
    evidence = failure["detail"]["evidence"]["SPX"]
    assert evidence["strict_error"]["type"] == "MissingPriceEntryError"
    assert evidence["strict_error"]["missing_value_days"] == [missing.isoformat()]
    assert evidence["diagnosis_error"]["type"] == "MissingPriceEntryError"
    assert failure["exception"]["type"] == "MissingPriceEntryError"
    assert state["s21_facts"]["组合层已调用"] is False and state["performance_info_status"] == "未产生"


# ---------------------------------------------------------------------------
# 第三节第 5 项：计算停止
# ---------------------------------------------------------------------------


def crash(value: str) -> str:
    return str((Decimal(value) * Decimal("0.35")).quantize(Decimal("0.01")))


def test_composition_stop_is_recorded(tmp_path: Path) -> None:
    """组合层停止（一直持有在构造急跌日杠杆子组合收益因子不为正）：result.stop 照录；按清单生成之前失败处置。"""
    day = dt.date(2005, 6, 1)
    spx = [(d, crash(v) if d >= day else v) for d, v in market_lines("SPX", 11)]
    qqq = [(d, crash(v) if d >= day else v) for d, v in market_lines("QQQ", 12)]
    root = build_root(tmp_path / "root", spx=spx, qqq=qqq)
    failure, state = assert_failed_before_manifest(root, run_entry(root))
    assert failure["stage"] == "组合层" and failure["detail"]["stop"]["exit"] == failure["exit"] == "计算失败"
    assert failure["detail"]["stop"]["exception_type"] == "NavError"
    assert state["s21_facts"]["组合层已调用"] is True and state["performance_info_status"] in ("已产生", "无法确认")


def test_selection_non_finite_value_is_a_calculation_failure(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    patch = ("real = research_run.select_development\n"
             "def broken(result, tolerance):\n"
             "    records = (dataclasses.replace(result.records[0], log_wealth=float('nan')), *result.records[1:])\n"
             "    return real(dataclasses.replace(result, records=records), tolerance)\n"
             "research_run.select_development = broken\n")
    failure, state = assert_failed_before_manifest(root, run_injected(root, patch))
    assert failure["exit"] == "计算失败" and failure["stage"] == "选择"
    assert failure["exception"]["type"] == "SelectionError" and state["performance_info_status"] == "已产生"


def test_comparison_failure_stops_the_run(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    patch = ("from market_risk.wavewarn_v20.nav import NavError\n"
             "def failing(*arguments):\n"
             "    raise NavError('构造：对照计算失败')\n"
             "compare.average_comparison = failing\n")
    failure, _ = assert_failed_before_manifest(root, run_injected(root, patch))
    assert failure["exit"] == "计算失败" and failure["stage"] == "描述性对照"
    assert failure["exception"]["type"] == "NavError"


# ---------------------------------------------------------------------------
# 第三节第 6 项：写入失败
# ---------------------------------------------------------------------------


def test_write_failure_before_manifest(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    patch = ("real = output.write_new\n"
             "def failing(path, data, closed=None):\n"
             "    if path.name == 'segments.csv':\n"
             "        raise OSError('构造：写出失败')\n"
             "    return real(path, data, closed)\n"
             "output.write_new = failing\n")
    failure, _ = assert_failed_before_manifest(root, run_injected(root, patch))
    assert failure["stage"] == "写出" and failure["exit"] == "未预期异常"
    names = files_in(failed_dir(root))
    assert "segments.csv" not in names and "run_record.json" in names


def test_failure_after_manifest_before_rename(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    patch = ("real = output.rename_directory\n"
             "def failing(source, target):\n"
             "    if target.name == 'evaluation_development':\n"
             "        raise OSError('构造：改名失败')\n"
             "    return real(source, target)\n"
             "output.rename_directory = failing\n")
    done = run_injected(root, patch)
    assert done.returncode == 1, done.stderr
    directory = failed_dir(root)
    names = files_in(directory)
    assert {"MANIFEST.sha256", "FAILURE_MANIFEST.sha256", "failure.json", "information_state_final.json",
            "information_state.json"} <= names
    failure = json.loads(get(directory / "failure.json"))
    manifest_sha = hashlib.sha256(get(directory / "MANIFEST.sha256")).hexdigest()
    assert failure["manifest_sha256"] == manifest_sha and "不覆盖本失败目录的最终内容" in failure["manifest_scope"]
    listed = {line.split(" ", 2)[2] for line in get(directory / "FAILURE_MANIFEST.sha256").decode().splitlines()}
    assert listed == names - {"FAILURE_MANIFEST.sha256"}
    tail = assert_tail(root, directory, "FAILURE_MANIFEST.sha256", "失败")
    assert tail[0]["frozen_manifest_sha256"] == manifest_sha
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"


def test_failure_after_publishing_leaves_formal_directory_unchanged(tmp_path: Path) -> None:
    root = build_root(tmp_path / "root")
    patch = ("real = output.read_back\n"
             "seen = []\n"
             "def failing(path, allowed):\n"
             "    if path.name == 'audit_log.jsonl' and path.parent.name == 'evaluation_development' and not seen:\n"
             "        seen.append(1)\n"
             "        raise OSError('构造：发布后复核失败')\n"
             "    return real(path, allowed)\n"
             "output.read_back = failing\n")
    done = run_injected(root, patch)
    assert done.returncode == 1, done.stderr
    found = listing(root)
    assert found["formal"] == ["evaluation_development"] and len(found["note"]) == 1
    formal = research_dir(root) / "evaluation_development"
    listed = {line.split(" ", 2)[2]: line.split(" ", 2)[0]
              for line in get(formal / "MANIFEST.sha256").decode().splitlines()}
    assert files_in(formal) == {*listed, "MANIFEST.sha256"}                      # 无追加文件
    assert all(hashlib.sha256(get(formal / name)).hexdigest() == digest for name, digest in listed.items())
    note = json.loads(get(research_dir(root) / found["note"][0]))
    assert note["statement"] == "该正式目录不得作为成功结果引用，交负责人处理"
    assert note["manifest_sha256"] == hashlib.sha256(get(formal / "MANIFEST.sha256")).hexdigest()
    tail = assert_tail(root, formal, "MANIFEST.sha256", "失败")
    assert tail[-1]["failure_note"] == found["note"][0]
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"
    # 修正批 K4 第 12、14 例：发布失败说明含两字段，与 run_record.json、information_state.json、说明内的信息状态、
    # 标准输出逐字节相等（构造为 1999-09-07 起点，满足，数组为空）。
    _, documents = success_documents(root, done)
    documents.update({"发布失败说明": note, "说明内 information_state_final": note["information_state_final"]})
    assert_same_coverage(documents, [], decided("1999-09-07", True))


def test_unwritable_failure_evidence_exits_three(tmp_path: Path) -> None:
    rows = market_lines("SPX", 11)
    root = build_root(tmp_path / "root", spx=rows, spx_sources={rows[3000][0]: "tiingo"})
    patch = ("real = output.write_new\n"
             "def failing(path, data, closed=None):\n"
             "    if path.name == 'failure.json':\n"
             "        raise OSError('构造：失败证据写不出')\n"
             "    return real(path, data, closed)\n"
             "output.write_new = failing\n")
    done = run_injected(root, patch)
    assert done.returncode == 3 and "失败证据未完整取得" in done.stderr
    found = listing(root)
    assert len(found["staging"]) == 1 and found["failed"] == []                 # 保留暂存目录原名
    staging = research_dir(root) / found["staging"][0]
    # 补充六第一节第 5 小节第 2 条：标准错误与退出信息列出目录实际路径、已存在的文件与未取得的证据。
    missing = ["failure.json", "information_state_final.json", "audit_log.jsonl", "FAILURE_MANIFEST.sha256"]
    assert staging.name in done.stderr and "已存在的文件（本次运行已写出）：无" in done.stderr
    assert "未取得的证据：" + "、".join(missing) in done.stderr
    report = stdout_report(done)
    assert Path(report["directory"]).name == staging.name and report["present_files"] == []
    assert report["missing_evidence"] == missing and report["exit_code"] == 3
    assert files_in(staging) == set()
    assert_tail(root, staging, None, "失败")
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"


# ---------------------------------------------------------------------------
# 补充七第一节第 1 小节第 5 条：部分写出的文件（完成状态与存在性分开记录）
# 注入方式同上：在子进程内替换 output.write_new；注入写出的部分字节的长度与 SHA-256 写到标准错误（已打开的流），
# 供测试核对“原样保留、未被覆盖”。
# ---------------------------------------------------------------------------

PARTIAL_WRITER = (
    "import hashlib\n"
    "real = output.write_new\n"
    "def partial(path, data, closed=None, keep=None, fail_in_data=False):\n"
    "    if fail_in_data:\n"
    "        def broken():\n"
    "            raise OSError('构造：内容生成失败')\n"
    "        return real(path, broken, closed)\n"
    "    content = data() if callable(data) else data\n"
    "    piece = content[:keep]\n"
    "    with path.open('xb') as handle:\n"
    "        handle.write(piece)\n"
    "    sys.stderr.write(f'PARTIAL {path.name} {len(piece)} {hashlib.sha256(piece).hexdigest()}\\n')\n"
    "    raise OSError('构造：写入一部分后失败')\n"
)


def partial_report(stderr: str, name: str) -> tuple[int, str]:
    line = next(line for line in stderr.splitlines() if line.startswith(f"PARTIAL {name} "))
    _, _, size, digest = line.split(" ")
    return int(size), digest


@pytest.mark.parametrize("keep", [0, 500], ids=["打开后写入前失败", "写入一部分后失败"])
def test_partial_stable_file_is_listed_in_the_failure_manifest(tmp_path: Path, keep: int) -> None:
    """稳定计算文件 segments.csv 打开后写入前失败、写入一部分后失败：退出码 1；
    失败目录实际文件集合等于失败清单所列加清单本身；incomplete_files 正确；部分文件字节与清单记录一致。"""
    root = build_root(tmp_path / "root")
    patch = PARTIAL_WRITER + (
        "def wrapped(path, data, closed=None):\n"
        f"    if path.name == 'segments.csv':\n"
        f"        return partial(path, data, closed, keep={keep})\n"
        "    return real(path, data, closed)\n"
        "output.write_new = wrapped\n")
    done = run_injected(root, patch)
    failure, _ = assert_failed_before_manifest(root, done)
    directory = failed_dir(root)
    listed = output_manifest(directory / "FAILURE_MANIFEST.sha256")
    assert files_in(directory) == {*listed, "FAILURE_MANIFEST.sha256"}                    # 测试侧列目录
    assert failure["incomplete_files"] == [{"file": "segments.csv", "status": "部分写出"}]
    size, digest = partial_report(done.stderr, "segments.csv")
    data = get(directory / "segments.csv")
    assert (len(data), hashlib.sha256(data).hexdigest()) == (size, digest) == (listed["segments.csv"][1],
                                                                                 listed["segments.csv"][0])
    assert size == keep and failure["stage"] == "写出"


def output_manifest(path: Path) -> dict[str, tuple[str, int]]:
    result = {}
    for line in get(path).decode("utf-8").splitlines():
        digest, size, name = line.split(" ", 2)
        result[name] = (digest, int(size))
    return result


@pytest.mark.parametrize("mode", ["打开后内容生成失败", "写入一部分后失败"])
def test_partial_audit_tail_is_kept_and_reported_as_incomplete(tmp_path: Path, mode: str) -> None:
    """审计尾段（发布后失败路径）打开后内容生成失败、写入一部分后失败：退出码 3；尾段原样保留、未被覆盖；发布失败说明中
    尾段标为不完整并列入未取得的证据；正式目录逐字节不变。"""
    root = build_root(tmp_path / "root")
    action = ("partial(path, data, closed, fail_in_data=True)" if mode == "打开后内容生成失败"
              else "partial(path, data, closed, keep=200)")
    patch = PARTIAL_WRITER + (
        "def wrapped(path, data, closed=None):\n"
        "    if path.name.startswith('evaluation_development_audit_tail_'):\n"
        f"        return {action}\n"
        "    return real(path, data, closed)\n"
        "output.write_new = wrapped\n")
    done = run_injected(root, patch)
    assert done.returncode == 3, done.stderr
    found = listing(root)
    assert found["formal"] == ["evaluation_development"] and len(found["note"]) == 1 and len(found["tail"]) == 1
    formal = research_dir(root) / "evaluation_development"
    listed = output_manifest(formal / "MANIFEST.sha256")
    assert files_in(formal) == {*listed, "MANIFEST.sha256"}                              # 无追加文件
    assert all(hashlib.sha256(get(formal / name)).hexdigest() == digest for name, (digest, _) in listed.items())
    tail = get(research_dir(root) / found["tail"][0])
    if mode == "打开后内容生成失败":
        assert tail == b""
    else:
        size, digest = partial_report(done.stderr, found["tail"][0])
        assert (len(tail), hashlib.sha256(tail).hexdigest()) == (size, digest) and size == 200
    note = json.loads(get(research_dir(root) / found["note"][0]))
    label = f"{found['tail'][0]}（部分写出，不完整）"
    assert note["audit_tail_written"] is False and note["audit_tail_status"] == "部分写出"
    assert note["missing_evidence"] == [label] and note["statement"] == "该正式目录不得作为成功结果引用，交负责人处理"
    assert "审计尾段部分写出（不完整），原样保留，不覆盖" in done.stderr
    assert label in stdout_report(done)["missing_evidence"]
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"


def test_partial_publish_failure_note_exits_three(tmp_path: Path) -> None:
    """发布失败说明写入一部分后失败（发布后复核失败触发发布后失败路径）：退出码 3；标准错误列明该文件不完整。"""
    root = build_root(tmp_path / "root")
    patch = PARTIAL_WRITER + (
        "real_back = output.read_back\n"
        "seen = []\n"
        "def failing(path, allowed):\n"
        "    if path.name == 'audit_log.jsonl' and path.parent.name == 'evaluation_development' and not seen:\n"
        "        seen.append(1)\n"
        "        raise OSError('构造：发布后复核失败')\n"
        "    return real_back(path, allowed)\n"
        "output.read_back = failing\n"
        "def wrapped(path, data, closed=None):\n"
        "    if path.name.startswith('evaluation_development_publish_failure_'):\n"
        "        return partial(path, data, closed, keep=100)\n"
        "    return real(path, data, closed)\n"
        "output.write_new = wrapped\n")
    done = run_injected(root, patch)
    assert done.returncode == 3, done.stderr
    found = listing(root)
    assert len(found["note"]) == 1
    note_name = found["note"][0]
    size, digest = partial_report(done.stderr, note_name)
    data = get(research_dir(root) / note_name)
    assert (len(data), hashlib.sha256(data).hexdigest()) == (size, digest)
    assert "发布失败说明写不出（部分写出）" in done.stderr and f"{note_name}（部分写出，不完整）" in done.stderr
    assert f"{note_name}（部分写出，不完整）" in stdout_report(done)["missing_evidence"]
    tail = lines_of(research_dir(root) / found["tail"][0])
    assert tail[-1]["outcome"] == "失败" and tail[-1]["failure_note"] is None      # 说明未完成，尾段不引用它
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"


def test_failure_in_the_close_callback_is_not_a_completed_write(tmp_path: Path) -> None:
    """尾段写完内容、在关闭回调之前失败（模拟关闭失败）：该文件不被当作“已完成”——按部分写出处置，退出码 3。"""
    root = build_root(tmp_path / "root")
    patch = (
        "real = output.write_new\n"
        "def wrapped(path, data, closed=None):\n"
        "    if path.name.startswith('evaluation_development_audit_tail_'):\n"
        "        def failing_close():\n"
        "            raise OSError('构造：关闭回调失败')\n"
        "        return real(path, data, failing_close)\n"
        "    return real(path, data, closed)\n"
        "output.write_new = wrapped\n")
    done = run_injected(root, patch)
    assert done.returncode == 3, done.stderr
    found = listing(root)
    assert len(found["note"]) == 1 and len(found["tail"]) == 1
    tail = lines_of(research_dir(root) / found["tail"][0])
    assert tail[-1]["record"] == "close"                                              # 内容已完整写出
    note = json.loads(get(research_dir(root) / found["note"][0]))
    assert note["audit_tail_written"] is False and note["audit_tail_status"] == "部分写出"
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"


# ---------------------------------------------------------------------------
# 第三节第 7 项：封存后访问
# ---------------------------------------------------------------------------


def test_access_after_closing_in_main_exits_four(tmp_path: Path) -> None:
    """包装尾段写出函数，在其返回后打开一个构造文件：被阻止，标准错误有“【封存后访问】”行，退出码 4；
    尾段收尾记录仍可能写“成功”，外部判定为非成功。"""
    root = build_root(tmp_path / "root")
    put(root / "probe.txt", b"probe\n")
    git(root, "add", "probe.txt")
    git(root, "commit", "-q", "-m", "构造：探针文件")
    patch = ("real = output.write_new\n"
             "def wrapped(path, data, closed=None):\n"
             "    real(path, data, closed)\n"
             "    if path.name.startswith('evaluation_development_audit_tail_'):\n"
             "        try:\n"
             "            (ROOT / 'probe.txt').read_bytes()\n"
             "        except PermissionError:\n"
             "            pass\n"
             "output.write_new = wrapped\n")
    done = run_injected(root, patch)
    assert done.returncode == 4 and any(line.startswith(SEALED) for line in done.stderr.splitlines())
    assert "收尾后出现文件访问尝试，审计尾段未覆盖" in done.stderr
    tail = lines_of(research_dir(root) / listing(root)["tail"][0])
    assert tail[-1]["outcome"] == "成功"
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "非成功"


def test_access_during_interpreter_shutdown_is_not_a_success(tmp_path: Path) -> None:
    """main 返回之后（解释器关闭阶段）的注入访问：退出码可为 0，但标准错误含“【封存后访问】”行，判定为非成功。"""
    root = build_root(tmp_path / "root")
    patch = ("def late():\n"
             "    try:\n"
             "        (ROOT / 'pyproject.toml').read_bytes()\n"
             "    except PermissionError:\n"
             "        pass\n"
             "atexit.register(late)\n")
    done = run_injected(root, patch)
    assert done.returncode == 0 and any(line.startswith(SEALED) for line in done.stderr.splitlines())
    tail = lines_of(research_dir(root) / listing(root)["tail"][0])
    assert tail[-1]["outcome"] == "成功"
    verdict = judge(done.returncode, done.stderr, root)
    assert verdict["工程完成状态"] == "非成功" and not verdict["条件"]["2 标准错误完整取得且无封存后访问行"]


# ---------------------------------------------------------------------------
# 第三节第 8、9 项：前缀诊断（三项分别记录与断言，互不推定）
# ---------------------------------------------------------------------------


def test_prefix_diagnosis_run_is_published_with_usage_restriction(tmp_path: Path) -> None:
    rows = market_lines("SPX", 11)
    missing = next(day for day, _ in rows if day >= dt.date(2010, 3, 1))
    root = build_root(tmp_path / "root", spx=market_lines("SPX", 11, {missing: ""}))
    done = run_entry(root)
    # 工程完成状态：按外部判定函数照实断言（本构造下发布完成，应满足成功引用条件）。
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "成功", done.stderr
    formal = research_dir(root) / "evaluation_development"
    record = json.loads(get(formal / "run_record.json"))
    assert record["usage_restriction"] == [PREFIX_USAGE]                         # 用途限制（修正批 E1：数组）
    report = get(formal / "report.md").decode("utf-8").splitlines()
    assert report[0].startswith("# ") and report[1] == PREFIX_USAGE
    selection = json.loads(get(formal / "selection.json"))
    assert selection["outcome"] in ("选定", "计算失败", "缺值无法评价", "无合格候选")  # 出口照录，不预设
    assert stdout_report(done)["usage_restriction"] == [PREFIX_USAGE]
    state = json.loads(get(formal / "information_state.json"))
    assert state["evaluation_prices_parsed"]["SPX"]["diagnosis_completed"] is True


def test_prefix_diagnosis_then_composition_stop(tmp_path: Path) -> None:
    """QQQ 每 50 个交易日缺价一次：63 日最高价窗口始终不完整，无法确定 t0（组合层停止）；用途限制仍须写出。"""
    rows = market_lines("QQQ", 12)
    gaps = {day: "" for index, (day, _) in enumerate(rows) if index % 50 == 25}
    root = build_root(tmp_path / "root", qqq=market_lines("QQQ", 12, gaps))
    done = run_entry(root)
    failure, state = assert_failed_before_manifest(root, done)
    assert failure["stage"] == "组合层" and failure["detail"]["stop"]["exit"] == failure["exit"]
    assert failure["usage_restriction"] == [PREFIX_USAGE]                      # 修正批 E1：数组
    assert state["evaluation_prices_parsed"]["QQQ"]["diagnosis_completed"] is True
    # 修正批 K4 第 10 例：无窗口（无法确定 t0）→“未判定：无窗口”，不写覆盖限制；三处逐字节相等。
    assert_same_coverage(failure_documents(root, done), [PREFIX_USAGE], undetermined("未判定：无窗口"))


# ---------------------------------------------------------------------------
# 修正批（M3 前修正批第一道门）：K1 四个新文件、K2 固定文字、K4 开发窗口覆盖条件（甲案）构造验收矩阵、
# K7 A11（correct:yahoo 入口端到端）、A12（非“选定”出口端到端）。
# 晚起点构造：QQQ 自上市首日 5000.00 起每个交易日下跌 0.01，至 end 当日止，其后接原随机路径按比例缩放；
# 下跌期间 P、PR 的初态运行不收敛，κ_全 由 end 决定（开发性试验：end = 2000-06-30 → j₀ = 2000-07-14；
# end = 2000-03-10 → j₀ = 2000-03-24）。期望值写在各测试的说明里。
# ---------------------------------------------------------------------------

COVERAGE_USAGE = "开发窗口未覆盖登记区间（axis[j₀] 晚于 2000-03-24）"
CONDITION = "axis[j0] <= 2000-03-24"
NOT_SELECTED = "- 开发窗口覆盖条件不满足：上列出口照录，不作正式选定、不进入锁定（登记补充第 5 条，甲案）。"
HISTORICAL = ("本报告为历史回算：以登记第零节绑定的数据集版本重放，并假定这些数值在当日美东 18:30 可以取得。"
              "运行记录中的 `acquired_at` 是本次运行取得数据的时间，不是历史上每个信号日的实际取得时间。")
EXPOSURE_NOTE = ("熊市暴露替换对照按《阶段四字段级设计稿（修订二）》第五节第 5 小节口径筛选，"
                 "与登记第 43 行字面不等价，只作描述。")
LATE_END, EQUAL_END = dt.date(2000, 6, 30), dt.date(2000, 3, 10)
NEW_FILES = {"channel_events.csv.gz", "system_transitions.csv.gz", "switches.csv.gz", "switch_stages.csv"}


def declining_qqq(end: dt.date, start_price: str = "5000.00") -> list[tuple[dt.date, str]]:
    """QQQ 自 1999-03-10 起每日下跌 0.01 至 end（含），其后为 market_lines("QQQ", 12) 按 end 次日的比例缩放。"""
    rows = market_lines("QQQ", 12)
    result: list[tuple[dt.date, str]] = []
    last = anchor = None
    for index, (day, value) in enumerate(rows):
        if day <= end:
            last = Decimal(start_price) - Decimal("0.01") * index
            result.append((day, str(last)))
        else:
            anchor = anchor or Decimal(value)
            result.append((day, str((last * Decimal(value) / anchor).quantize(Decimal("0.01")))))
    return result


def linear_lines(asset: str, start: str, step: str) -> list[tuple[dt.date, str]]:
    """严格单调的构造价格：首日 start，此后每个交易日加 step。"""
    days = stock_trading_days(FIRST[asset], LAST)
    return [(day, str(Decimal(start) + Decimal(step) * index)) for index, day in enumerate(days)]


def spx_missing_in_window() -> list[tuple[dt.date, str]]:
    """SPX 在 2010-03-01 起第一个交易日缺价（窗口内缺价 → 前缀诊断运行）。"""
    rows = market_lines("SPX", 11)
    missing = next(day for day, _ in rows if day >= dt.date(2010, 3, 1))
    return market_lines("SPX", 11, {missing: ""})


def build_root_with_decisions(root: Path, qqq: list, qqq_sources: dict, decisions: str) -> Path:
    """同 build_root，另可给 QQQ 的逐行来源与 data_decisions.yaml 全文（A11）。"""
    files = {"SPX": csv_text(market_lines("SPX", 11)), "QQQ": csv_text(qqq, sources=qqq_sources)}
    for relative in PREFLIGHT_FILES:
        if not relative.startswith("config/"):
            put(root.joinpath(*relative.split("/")), f"# 构造占位：{relative}\n".encode())
    put(root.joinpath("config", "wavewarn_v20.yaml"), registered_yaml(files))
    put(root.joinpath("config", "data_decisions.yaml"), decisions.encode("utf-8"))
    for asset, raw in files.items():
        put(root.joinpath("data", "market", "daily", f"{asset}.csv"), raw)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "构造")
    return root


def table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """读取输出的 CSV（.gz 先解压）：表头与逐行字典。"""
    import csv
    import gzip
    import io

    data = get(path)
    text = (gzip.decompress(data) if path.name.endswith(".gz") else data).decode("utf-8")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader)
    return header, [dict(zip(header, row, strict=True)) for row in reader]


def decided(first_day: str, satisfied: bool) -> dict:
    return {"condition": CONDITION, "first_day": first_day, "satisfied": satisfied, "status": "已判定"}


def undetermined(status: str) -> dict:
    return {"condition": CONDITION, "first_day": None, "satisfied": None, "status": status}


def coverage_text(document: dict) -> str:
    """K4 第 6 条：两个字段按 JSON 规范化（键排序、不转义非 ASCII）后的文本。"""
    return json.dumps({"usage_restriction": document["usage_restriction"],
                       "window_coverage": document["window_coverage"]}, ensure_ascii=False, sort_keys=True)


def assert_same_coverage(documents: dict[str, dict], usage: list[str], coverage: dict) -> None:
    """K4 第 6 条、第 14 例：所列文件中两个字段逐字节相等，且等于期望值。"""
    expected = coverage_text({"usage_restriction": usage, "window_coverage": coverage})
    assert {name: coverage_text(document) for name, document in documents.items()} == dict.fromkeys(documents,
                                                                                                    expected)


def assert_report_usage(report: list[str], usage: list[str]) -> None:
    """report.md 在标题后逐项列出同一数组，随后是空行与 K2 固定文字。"""
    assert report[1:1 + len(usage)] == usage
    assert report[1 + len(usage):6 + len(usage)] == ["", HISTORICAL, "", EXPOSURE_NOTE, ""]


def success_documents(root: Path, done: subprocess.CompletedProcess) -> tuple[Path, dict[str, dict]]:
    formal = research_dir(root) / "evaluation_development"
    return formal, {"run_record.json": json.loads(get(formal / "run_record.json")),
                    "information_state.json": json.loads(get(formal / "information_state.json")),
                    "标准输出": stdout_report(done)}


def failure_documents(root: Path, done: subprocess.CompletedProcess) -> dict[str, dict]:
    directory = failed_dir(root)
    documents = {"failure.json": json.loads(get(directory / "failure.json")),
                 "information_state_final.json": json.loads(get(directory / "information_state_final.json")),
                 "标准输出": stdout_report(done)}
    if present(directory / "run_record.json"):
        documents["run_record.json"] = json.loads(get(directory / "run_record.json"))
    return documents


@pytest.fixture(scope="module")
def late(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, subprocess.CompletedProcess]:
    root = build_root(tmp_path_factory.mktemp("late"), qqq=declining_qqq(LATE_END))
    return root, run_entry(root)


# --- K1：四个新文件（正常运行的正式目录） ----------------------------------------------------------------


def test_k1_event_files_on_the_normal_run(normal: tuple[Path, subprocess.CompletedProcess, float]) -> None:
    """表头、排序、范围；系统转换与 daily_signals 逐日一致（首个信号日以 axis[j₀−2] 判断，该日状态不在
    daily_signals 中，只核对去向与方向）；切换与 daily_targets 逐日一致、条数等于 candidates_summary /
    reference_summary 的 switches；信号模拟下不涉及全部现金，阶段数全部非空，可评价对象 stages_null_count 为 0。"""
    import itertools

    root, done, _ = normal
    assert done.returncode == 0, done.stderr
    formal = research_dir(root) / "evaluation_development"
    _, signals = table(formal / "daily_signals.csv.gz")
    _, targets = table(formal / "daily_targets.csv.gz")
    _, candidates = table(formal / "candidates_summary.csv")
    _, references = table(formal / "reference_summary.csv")
    candidate_names = [row["object"] for row in candidates]
    signal_days = sorted({row["date"] for row in signals})

    header, events = table(formal / "channel_events.csv.gz")
    assert header == ["date", "k", "theta_p", "channel", "event"]
    rank = {name: index for index, name in enumerate(("p_spx", "p_qqq", "pr_spx", "pr_qqq", "mr"))}
    keys = [(row["date"], int(row["k"]), Decimal(row["theta_p"]), rank[row["channel"]]) for row in events]
    assert events and keys == sorted(keys)
    assert {(row["k"], row["theta_p"]) for row in events} <= {(row["k"], row["theta_p"]) for row in candidates}
    assert {row["event"] for row in events} <= {"进入", "退出", "重新武装"}
    assert all(signal_days[0] <= row["date"] <= signal_days[-1] for row in events)

    header, transitions = table(formal / "system_transitions.csv.gz")
    assert header == ["date", "object", "from_risk", "to_risk", "direction"]
    order = {name: index for index, name in enumerate([*candidate_names, "主参照"])}
    assert [(row["date"], order[row["object"]]) for row in transitions] == sorted(
        (row["date"], order[row["object"]]) for row in transitions)
    level = {"正常": 0, "一级": 1, "二级": 2}
    assert all(row["direction"] == ("升级" if level[row["to_risk"]] > level[row["from_risk"]] else "降级")
               for row in transitions)
    for name in order:
        path = [(row["date"], row["risk"]) for row in signals if row["object"] == name]
        expected = [(day, before, after) for (_, before), (day, after) in itertools.pairwise(path)
                    if before != after]
        mine = [(row["date"], row["from_risk"], row["to_risk"]) for row in transitions if row["object"] == name]
        assert [item for item in mine if item[0] != path[0][0]] == expected, name
        assert all(after == path[0][1] and before != after for day, before, after in mine if day == path[0][0])

    header, switches = table(formal / "switches.csv.gz")
    assert header == ["date", "object", "path", "before", "after", "exposure_change", "exposure_magnitude", "stages"]
    header, stages = table(formal / "switch_stages.csv")
    assert header == ["object", "path", "switches", "stages_total", "stages_null_count"]
    counts = {row["object"]: row["switches"] for row in [*candidates, *references]}
    stage = {"正常": 0, "一级": 1, "二级": 2}
    objects = [row["object"] for row in stages]
    assert objects == [*candidate_names, "主参照", "200 日均线一级版", "带缓冲带的 200 日均线二级版（101%/99%）"]
    for name in objects:
        path = [(row["date"], row["position"]) for row in targets if row["object"] == name]
        expected = [(day, before, after, str(abs(stage[after] - stage[before])))
                    for (_, before), (day, after) in itertools.pairwise(path) if before != after]
        mine = [row for row in switches if row["object"] == name]
        assert [(row["date"], row["before"], row["after"], row["stages"]) for row in mine] == expected, name
        assert all(row["path"] == "信号模拟" for row in mine)
        assert counts[name] == str(len(mine))
        (total,) = [row for row in stages if row["object"] == name]
        assert total == {"object": name, "path": "信号模拟", "switches": str(len(mine)),
                         "stages_total": str(sum(int(row["stages"]) for row in mine)), "stages_null_count": "0"}
    assert all(row["stages"] != "" for row in switches)


# --- K2：固定文字（正常运行的正式目录） ------------------------------------------------------------------


def test_k2_fixed_texts_on_the_normal_run(normal: tuple[Path, subprocess.CompletedProcess, float]) -> None:
    """report.md：无用途限制时标题后为空行、第 7.1 条固定文字、空行、第 4.2 条说明；descriptive.json 只在既有顶层键之后
    新增 exposure_substitution_note（E6），既有键序与 note 取值不变。"""
    root, _, _ = normal
    formal = research_dir(root) / "evaluation_development"
    report = get(formal / "report.md").decode("utf-8").splitlines()
    assert report[:7] == ["# 波段预警 v2.0 开发期运行报告", "", HISTORICAL, "", EXPOSURE_NOTE, "", "## 窗口"]
    document = json.loads(get(formal / "descriptive.json"))
    assert list(document) == ["averages", "constants", "environment_summary", "state_statistics", "policy_events",
                              "policy_minus_signal", "note", "exposure_substitution_note"]
    assert document["note"] == "描述性对照与报告项不参与筛选、排序或确认性检验；不新增扫描参数"
    assert document["exposure_substitution_note"] == EXPOSURE_NOTE


# --- K4 构造验收矩阵 ------------------------------------------------------------------------------------


def test_k4_case1_window_starting_before_the_bear_market(
        normal: tuple[Path, subprocess.CompletedProcess, float]) -> None:
    """第 1、14 例：axis[j₀] = 1999-09-07 早于 2000-03-24，出口“选定”；满足；数组为空；三处逐字节相等；报告无限制行。"""
    root, done, _ = normal
    formal, documents = success_documents(root, done)
    assert json.loads(get(formal / "window.json"))["window"]["first_day"] == "1999-09-07"
    assert json.loads(get(formal / "selection.json"))["outcome"] == "选定"
    assert_same_coverage(documents, [], decided("1999-09-07", True))
    report = get(formal / "report.md").decode("utf-8").splitlines()
    assert_report_usage(report, [])
    assert NOT_SELECTED not in report


def test_k4_case2_window_starting_on_the_bear_market_start(tmp_path: Path) -> None:
    """第 2 例：QQQ 下跌至 2000-03-10 → axis[j₀] 恰为 2000-03-24（等号）：满足；数组为空。"""
    root = build_root(tmp_path / "root", qqq=declining_qqq(EQUAL_END))
    done = run_entry(root)
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "成功", done.stderr
    formal, documents = success_documents(root, done)
    assert json.loads(get(formal / "window.json"))["window"]["first_day"] == "2000-03-24"
    assert_same_coverage(documents, [], decided("2000-03-24", True))


def test_k4_case3_late_window_records_the_selection_but_does_not_select(
        late: tuple[Path, subprocess.CompletedProcess]) -> None:
    """第 3、14 例：axis[j₀] = 2000-07-14 晚于 2000-03-24：不满足；出口“选定”照录（选定组照录）；数组只含覆盖限制；
    报告逐项列出并在选择一节写明不作正式选定；run_record、information_state、标准输出逐字节相等。工程完成状态为成功。"""
    root, done = late
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "成功", done.stderr
    formal, documents = success_documents(root, done)
    assert json.loads(get(formal / "window.json"))["window"]["first_day"] == "2000-07-14"
    selection = json.loads(get(formal / "selection.json"))
    assert selection["outcome"] == "选定" and selection["selected"] is not None
    assert_same_coverage(documents, [COVERAGE_USAGE], decided("2000-07-14", False))
    report = get(formal / "report.md").decode("utf-8").splitlines()
    assert_report_usage(report, [COVERAGE_USAGE])
    assert report.count(NOT_SELECTED) == 1 and "- 出口：选定" in report


INFEASIBLE = ("real = research_run.select_development\n"
              "def infeasible(result, tolerance):\n"
              "    records = tuple(dataclasses.replace(item, r1=False) for item in result.records)\n"
              "    return real(dataclasses.replace(result, records=records), tolerance)\n"
              "research_run.select_development = infeasible\n")
NON_FINITE = ("real = research_run.select_development\n"
              "def broken(result, tolerance):\n"
              "    records = (dataclasses.replace(result.records[0], log_wealth=float('nan')), *result.records[1:])\n"
              "    return real(dataclasses.replace(result, records=records), tolerance)\n"
              "research_run.select_development = broken\n")


def test_k4_case4_late_window_with_no_feasible_candidate(tmp_path: Path) -> None:
    """第 4 例与 A12“无合格候选”：晚起点构造，注入使全部候选 R1 不满足 → 原始出口“无合格候选”照录，不改称；
    数组只含覆盖限制；选定候选恒定仓位未计算。"""
    root = build_root(tmp_path / "root", qqq=declining_qqq(LATE_END))
    done = run_injected(root, INFEASIBLE)
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "成功", done.stderr
    formal, documents = success_documents(root, done)
    selection = json.loads(get(formal / "selection.json"))
    assert selection["outcome"] == "无合格候选" and selection["selected"] is None
    assert_same_coverage(documents, [COVERAGE_USAGE], decided("2000-07-14", False))
    report = get(formal / "report.md").decode("utf-8").splitlines()
    assert "- 出口：无合格候选" in report and report.count(NOT_SELECTED) == 1
    constants = json.loads(get(formal / "descriptive.json"))["constants"]
    assert [(item["object"], item["computed"], item["note"]) for item in constants][-1] == (
        "选定候选", False, "无选定候选，未计算")


def test_k4_cases5_6_late_window_with_prefix_diagnosis(tmp_path: Path) -> None:
    """第 5、6、14 例：晚起点 + SPX 窗口内缺价（前缀诊断）：成功发布；数组两项按序（前缀诊断在前）；出口
    “缺值无法评价”照录；三处逐字节相等；报告逐项列出两项并写明不作正式选定。"""
    root = build_root(tmp_path / "root", spx=spx_missing_in_window(), qqq=declining_qqq(LATE_END))
    done = run_entry(root)
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "成功", done.stderr
    formal, documents = success_documents(root, done)
    assert json.loads(get(formal / "selection.json"))["outcome"] == "缺值无法评价"
    usage = [PREFIX_USAGE, COVERAGE_USAGE]
    assert_same_coverage(documents, usage, decided("2000-07-14", False))
    report = get(formal / "report.md").decode("utf-8").splitlines()
    assert_report_usage(report, usage)
    assert report.count(NOT_SELECTED) == 1


def test_k4_case7_late_window_with_a_selection_failure(tmp_path: Path) -> None:
    """第 7、14 例：晚起点 + 选择阶段计算失败（注入非有限值）：failure.json、information_state_final.json、标准输出含
    覆盖限制与 window_coverage，逐字节相等。"""
    root = build_root(tmp_path / "root", qqq=declining_qqq(LATE_END))
    done = run_injected(root, NON_FINITE)
    failure, _ = assert_failed_before_manifest(root, done)
    assert failure["exit"] == "计算失败" and failure["stage"] == "选择"
    assert_same_coverage(failure_documents(root, done), [COVERAGE_USAGE], decided("2000-07-14", False))


def test_k4_case8_late_window_prefix_diagnosis_and_selection_failure(tmp_path: Path) -> None:
    """第 8 例：晚起点 + 前缀诊断 + 选择阶段计算失败：失败记录的数组两项按序；出口与阶段照录。
    注入方式：前缀诊断下各候选均不可评价，选择程序先给“缺值无法评价”，不检查其他记录的非有限值（第 7 例的注入在此
    不触发选择阶段失败，开发检查中实际停在“序列化”）；故在选择调用内直接抛 SelectionError。"""
    root = build_root(tmp_path / "root", spx=spx_missing_in_window(), qqq=declining_qqq(LATE_END))
    patch = ("def broken(result, tolerance):\n"
             "    raise selection.SelectionError('构造：选择阶段计算失败')\n"
             "research_run.select_development = broken\n")
    done = run_injected(root, patch)
    failure, state = assert_failed_before_manifest(root, done)
    assert failure["exit"] == "计算失败" and failure["stage"] == "选择"
    assert_same_coverage(failure_documents(root, done), [PREFIX_USAGE, COVERAGE_USAGE], decided("2000-07-14", False))
    assert state["evaluation_prices_parsed"]["SPX"]["diagnosis_completed"] is True


def test_k4_case9_late_window_with_a_composition_stop(tmp_path: Path) -> None:
    """第 9、14 例：晚起点 + 2005-06-01 起两资产乘 0.35（一直持有的杠杆因子不为正，组合层停止）：有窗口，已判定、
    不满足；失败记录含 window_coverage 与覆盖限制，逐字节相等。"""
    day = dt.date(2005, 6, 1)
    spx = [(d, crash(v) if d >= day else v) for d, v in market_lines("SPX", 11)]
    qqq = [(d, crash(v) if d >= day else v) for d, v in declining_qqq(LATE_END)]
    root = build_root(tmp_path / "root", spx=spx, qqq=qqq)
    done = run_entry(root)
    failure, _ = assert_failed_before_manifest(root, done)
    assert failure["stage"] == "组合层" and failure["detail"]["stop"]["exit"] == "计算失败"
    assert_same_coverage(failure_documents(root, done), [COVERAGE_USAGE], decided("2000-07-14", False))


def test_k4_case13_and_boundaries_of_the_coverage_judgement() -> None:
    """第 13 例：截止日不是登记截止日 → 抛错，不给覆盖结论；熊市一起点与条件文字不符 → 抛错。第 2 例的等号与
    无窗口状态另以判定函数断言。"""
    from market_risk.wavewarn_v20 import development_run as entry
    from market_risk.wavewarn_v20 import registered_v20

    bear = registered_v20.BEARS[0][1]
    assert bear == dt.date(2000, 3, 24) and registered_v20.CUTOFF == dt.date(2016, 12, 30)
    assert entry.window_coverage(dt.date(2000, 3, 24), bear, registered_v20.CUTOFF) == decided("2000-03-24", True)
    assert entry.window_coverage(dt.date(2000, 3, 23), bear, registered_v20.CUTOFF) == decided("2000-03-23", True)
    assert entry.window_coverage(dt.date(2000, 3, 27), bear, registered_v20.CUTOFF) == decided("2000-03-27", False)
    assert entry.window_coverage(None, bear, registered_v20.CUTOFF) == undetermined("未判定：无窗口")
    with pytest.raises(ValueError, match="截止日"):
        entry.window_coverage(dt.date(2000, 3, 24), bear, dt.date(2016, 12, 29))
    with pytest.raises(ValueError):
        entry.window_coverage(dt.date(2000, 3, 24), dt.date(2000, 3, 23), registered_v20.CUTOFF)


# --- K7 A11：correct:yahoo 修正行的入口端到端 ------------------------------------------------------------


def corrected_root(root: Path, offset: str) -> Path:
    """QQQ 在 2008-09-08 一行的来源为 correct:yahoo，data_decisions.yaml 另有一条 QQQ correct 条目，修正值为该行价格
    加 offset（offset = "0" 时两者相等）。"""
    rows = market_lines("QQQ", 12)
    day = dt.date(2008, 9, 8)
    value = dict(rows)[day]
    entry = (f"- date: {day}\n  symbol: QQQ\n  decision: correct\n  reason: 构造\n  decided_on: 2008-10-01\n"
             f"  corrected_value: {Decimal(value) + Decimal(offset)}\n  evidence_source: 构造证据\n")
    return build_root_with_decisions(root, rows, {day: "correct:yahoo"}, DECISIONS + entry)


def test_a11_corrected_row_runs_end_to_end(tmp_path: Path) -> None:
    """修正值等于该行价格（published_price 两位小数）：入口全链成功；输入快照中该日 QQQ 收盘价即该值。"""
    root = corrected_root(tmp_path / "root", "0")
    done = run_entry(root)
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "成功", done.stderr
    formal = research_dir(root) / "evaluation_development"
    _, rows = table(formal / "input_snapshot.csv.gz")
    (row,) = [item for item in rows if item["date"] == "2008-09-08"]
    assert Decimal(row["qqq_close"]) == Decimal(dict(market_lines("QQQ", 12))[dt.date(2008, 9, 8)])


def test_a11_corrected_row_mismatch_is_an_input_error(tmp_path: Path) -> None:
    """修正值与该行价格差 0.01：严格读取“输入校验失败”（修正条目核对不符）；组合层未调用；
    覆盖“未判定：组合层未返回”。"""
    root = corrected_root(tmp_path / "root", "0.01")
    done = run_entry(root)
    failure, state = assert_failed_before_manifest(root, done)
    assert failure["exit"] == "输入校验失败" and failure["stage"] == "严格读取"
    assert "修正条目核对不符" in failure["exception"]["message"]
    assert state["s21_facts"]["组合层已调用"] is False
    assert_same_coverage(failure_documents(root, done), [], undetermined("未判定：组合层未返回"))


# --- K7 A12：非“选定”出口端到端 -------------------------------------------------------------------------


def test_a12_smooth_prices_give_missing_value_and_non_evaluable_outputs(tmp_path: Path) -> None:
    """SPX 自 1000.00 起每日加 0.05、QQQ 自 100.00 起每日加 0.01（严格上升，无回撤）：
    R2 没有任何事件 → 各候选两资产 R2 分母为 0、不可计算 → 出口“缺值无法评价”；SPX 每日只高出 200 日均线约 0.5%，
    始终在 101%/99% 缓冲带内 → 带缓冲带的均线对照在 j₀ − 1 前不收敛、不可评价（switch_stages 一行三列为空，
    switches.csv.gz 无该对象的行）；无选定候选 → 选定候选的恒定仓位未计算。"""
    root = build_root(tmp_path / "root", spx=linear_lines("SPX", "1000.00", "0.05"),
                      qqq=linear_lines("QQQ", "100.00", "0.01"))
    done = run_entry(root)
    assert judge(done.returncode, done.stderr, root)["工程完成状态"] == "成功", done.stderr
    formal, documents = success_documents(root, done)
    assert json.loads(get(formal / "selection.json"))["outcome"] == "缺值无法评价"
    _, candidates = table(formal / "candidates_summary.csv")
    assert len(candidates) == 27
    assert all(row[f"{asset}_r2_denominator"] == "0" and row[f"{asset}_r2_computable"] == "false"
               for row in candidates for asset in ("spx", "qqq"))
    descriptive = json.loads(get(formal / "descriptive.json"))
    buffered = "带缓冲带的 200 日均线二级版（101%/99%）"
    assert [(item["name"], item["evaluable"]) for item in descriptive["averages"]] == [
        ("200 日均线一级版", True), (buffered, False)]
    assert descriptive["constants"][-1]["object"] == "选定候选" and descriptive["constants"][-1]["computed"] is False
    _, stages = table(formal / "switch_stages.csv")
    assert [row for row in stages if row["object"] == buffered] == [
        {"object": buffered, "path": "信号模拟", "switches": "", "stages_total": "", "stages_null_count": ""}]
    _, switches = table(formal / "switches.csv.gz")
    assert not [row for row in switches if row["object"] == buffered]
    assert_same_coverage(documents, [], decided("1999-09-07", True))
