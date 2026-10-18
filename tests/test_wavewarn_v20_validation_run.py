"""阶段五验证期与保留期运行入口（validation_run）的子进程测试（V2 指令第五节：T-OUT、T-CUT、T-OVL 入口停止、T-NC1 入口、
T-INV3 与 T-D-INV 入口、T-HOLD、T-EQV）。

所有调用 main 的测试都在子进程中运行（钩子安装不可撤销）。构造仓库根在 tmp_path 下：git init 后提交的构造源码占位、
构造 config/、按 NYSE 日历生成的构造行情（SPX 1990-01-02 起、QQQ 1999-03-10 起，至 2026-10-30），以及构造的 M3 基准
（reports/research/wavewarn_v20/evaluation_development/ 下的清单与逐日信号）。路径一律以 root.joinpath(...) 拼接。
不读真实研究数据或真实项目配置。

构造基准：在测试进程内以开发期输出的 serialize_daily 写出较早截止日（2016-12-30）的冻结候选与主参照逐日信号（与 M3 基准
同一格式），不经被测入口。真实的 M3 基准哈希是登记常量，构造基准不可能与之相同，所以需要走完整运行的情形以注入脚本
（-c）在子进程内预先替换这三个常量，并把重抽样次数降为 RESAMPLES（只为缩短耗时；登记值由纯算法测试核对）。
注入类情形在钩子安装之前导入业务模块，因此不检验钩子安装时点；钩子安装时点由不注入的情形检查（预检拒绝、T-EQV）。
带拦截运行时这些测试照实标“通过但覆盖不完整”（子进程中的访问不在外层拦截范围内）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from wavewarn_v20_helpers import random_closes

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn_v20 import registered_v20_validation as registered
from market_risk.wavewarn_v20.development_output import csv_bytes, gzip_bytes, manifest_bytes, serialize_daily
from market_risk.wavewarn_v20.research_run import Continuity, Purpose, WindowSpec, run_window
from market_risk.wavewarn_v20.snapshot import make_snapshot
from market_risk.wavewarn_v20.validation_output import PREFLIGHT_FILES, parse_signals

MODULE = "market_risk.wavewarn_v20.validation_run"
DEVELOPMENT = "market_risk.wavewarn_v20.development_run"
SEALED = "【封存后访问】"
FIRST = {"SPX": dt.date(1990, 1, 2), "QQQ": dt.date(1999, 3, 10)}
LAST = dt.date(2026, 10, 30)
SEEDS = {"SPX": 21, "QQQ": 22}
RESAMPLES = 40
GIT_ENV = {"GIT_AUTHOR_NAME": "构造", "GIT_AUTHOR_EMAIL": "construct@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_NAME": "构造",
           "GIT_COMMITTER_EMAIL": "construct@example.invalid", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}
DECISIONS = "- date: 2001-01-04\n  symbol: SPY\n  decision: exclude\n  reason: 构造\n  decided_on: 2001-06-01\n"
FAILURE_FILES = {"failure.json", "information_state_final.json", "audit_log.jsonl", "FAILURE_MANIFEST.sha256"}


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


def market_rows(asset: str) -> list[tuple[dt.date, str]]:
    days = stock_trading_days(FIRST[asset], LAST)
    return [(day, str(close)) for day, close in zip(days, random_closes(SEEDS[asset], len(days)), strict=True)]


def csv_text(rows: list[tuple[dt.date, str]], changes: dict | None = None) -> bytes:
    lines = ["date,value,open,high,low,close,volume,source"]
    for day, value in rows:
        line = f"{day},{value},IGN_OPEN,IGN_HIGH,IGN_LOW,IGN_CLOSE,IGN_VOLUME,yahoo"
        lines.append((changes or {}).get(day, line))
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


def research_dir(root: Path) -> Path:
    return root.joinpath("reports", "research", "wavewarn_v20")


def market_file(root: Path, name: str) -> Path:
    return root.joinpath("data", "market", "daily", f"{name}.csv")


@pytest.fixture(scope="module")
def baseline() -> bytes:
    """构造 M3 基准：较早截止日 2016-12-30、构造验收用途、只含冻结候选；经开发期输出的 serialize_daily 写出。"""
    axis = tuple(stock_trading_days(FIRST["SPX"], dt.date(2016, 12, 30)))
    prices = {asset: {day: _decimal(value) for day, value in market_rows(asset) if day <= axis[-1]}
              for asset in ("SPX", "QQQ")}
    snapshot = make_snapshot(axis, prices, axis[-1], dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                             {"SPX": "0" * 64, "QQQ": "0" * 64})
    spec = WindowSpec(Purpose.CONSTRUCTED, None, axis[-1], FIRST, registered.INITIAL,
                      Continuity.COMPLETE_TRADING_AXIS)
    result = run_window(snapshot, spec, registered.run_parameters(), (registered.FROZEN_CANDIDATE,))
    assert result.stop is None
    return serialize_daily(result, None)["daily_signals.csv.gz"]


def _decimal(text: str):
    from decimal import Decimal

    return Decimal(text)


def baseline_files(signals: bytes) -> dict[str, bytes]:
    return {"daily_signals.csv.gz": signals, "MANIFEST.sha256": manifest_bytes({"daily_signals.csv.gz": signals})}


def build_root(root: Path, signals: bytes | None, spx_changes: dict | None = None,
               extra: dict[tuple[str, ...], bytes] | None = None) -> Path:
    """构造仓库根：预检文件占位、两份配置、两份八列行情、构造 M3 基准（给出时）与额外文件（键为路径分段）；
    git init 后提交。"""
    files = {"SPX": csv_text(market_rows("SPX"), spx_changes), "QQQ": csv_text(market_rows("QQQ"))}
    for relative in PREFLIGHT_FILES:
        if not relative.startswith("config/"):
            put(root.joinpath(*relative.split("/")), f"# 构造占位：{relative}\n".encode())
    put(root.joinpath("config", "wavewarn_v20.yaml"), registered_yaml(files))
    put(root.joinpath("config", "data_decisions.yaml"), DECISIONS.encode("utf-8"))
    for asset, raw in files.items():
        put(market_file(root, asset), raw)
    if signals is not None:
        for name, data in baseline_files(signals).items():
            put(research_dir(root).joinpath("evaluation_development", name), data)
    for parts, data in (extra or {}).items():
        put(root.joinpath(*parts), data)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "构造")
    return root


def run_entry(root: Path, *arguments: str, module: str = MODULE) -> subprocess.CompletedProcess:
    return run_process([sys.executable, "-B", "-m", module, "--root", str(root), *arguments], root)


INJECT = (
    "import dataclasses, sys\n"
    "from pathlib import Path\n"
    "from market_risk.wavewarn_v20 import validation_run, data_v20, research_run, confirmatory\n"
    "from market_risk.wavewarn_v20 import registered_v20_validation as registered\n"
    "ROOT = Path(sys.argv[1])\n"
    "registered.M3_MANIFEST_SHA256, registered.M3_SIGNALS_SHA256 = sys.argv[2], sys.argv[3]\n"
    "registered.M3_SIGNALS_BYTES = int(sys.argv[4])\n"
    "for mode, period in list(registered.PERIODS.items()):\n"
    "    registered.PERIODS[mode] = dataclasses.replace(period, confirmatory=dataclasses.replace(\n"
    f"        period.confirmatory, resamples={RESAMPLES}))\n"
)


def run_injected(root: Path, signals: bytes, patch: str = "", arguments: tuple[str, ...] = ()) -> (
        subprocess.CompletedProcess):
    files = baseline_files(signals)
    script = (INJECT + patch + f"\nsys.exit(validation_run.main(['--root', str(ROOT), *{list(arguments)!r}]))\n")
    return run_process([sys.executable, "-B", "-c", script, str(root),
                        hashlib.sha256(files["MANIFEST.sha256"]).hexdigest(),
                        hashlib.sha256(signals).hexdigest(), str(len(signals))], root)


def listing(root: Path, name: str = "evaluation_validation") -> dict[str, list[str]]:
    base = research_dir(root)
    if not present(base):
        return {}
    names = sorted(item.name for item in base.iterdir())
    return {"formal": [item for item in names if item == name],
            "failed": [item for item in names if item.startswith(f"{name}_failed_")],
            "staging": [item for item in names if item.startswith(f".staging_{name}_")],
            "tail": [item for item in names if item.startswith(f"{name}_audit_tail_")],
            "note": [item for item in names if item.startswith(f"{name}_publish_failure_")]}


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in get(path).decode("utf-8").splitlines() if line.strip()]


def files_in(directory: Path) -> set[str]:
    return {item.name for item in directory.iterdir()}


def stdout_report(done: subprocess.CompletedProcess) -> dict:
    return json.loads(done.stdout.strip().splitlines()[-1])


def judge(exit_code: int, stderr: str, root: Path, name: str = "evaluation_validation") -> dict:
    """只读外部判定：退出码 0；标准错误无封存后访问行；尾段收尾为成功且衔接校验通过；违规数为 0；无发布失败说明。"""
    conditions: dict[str, bool] = {"退出码为 0": exit_code == 0,
                                   "无封存后访问行": not any(line.startswith(SEALED) for line in stderr.splitlines())}
    found = listing(root, name)
    formal = research_dir(root) / name
    tails = found.get("tail", [])
    tail_ok = links = violations = False
    records = lines_of(research_dir(root) / tails[0]) if len(tails) == 1 else []
    if len(tails) == 1 and present(formal) and len(records) >= 2:
        link, close, events = records[0], records[-1], records[1:-1]
        coverage = lines_of(formal / "audit_log.jsonl")[-1]
        sequence = [item["seq"] for item in events]
        tail_ok = close.get("record") == "close" and close.get("outcome") == "成功"
        links = (link.get("audit_log_sha256") == hashlib.sha256(get(formal / "audit_log.jsonl")).hexdigest()
                 and link.get("manifest_sha256") == hashlib.sha256(get(formal / "MANIFEST.sha256")).hexdigest()
                 and bool(sequence) and coverage["last_seq"] + 1 == sequence[0])
        violations = coverage.get("violations_in_segment") == 0 and close.get("violations_in_tail") == 0
    conditions["尾段成功且衔接"] = tail_ok and links
    conditions["违规数为 0"] = violations
    conditions["无发布失败说明"] = not found.get("note")
    return {"状态": "成功" if all(conditions.values()) else "非成功", "条件": conditions}


def failure_of(root: Path, name: str = "evaluation_validation") -> tuple[Path, dict]:
    found = listing(root, name)
    assert len(found["failed"]) == 1 and found["formal"] == [] and found["staging"] == [], found
    directory = research_dir(root) / found["failed"][0]
    return directory, json.loads(get(directory / "failure.json"))


# ---------------------------------------------------------------------------
# T-OUT 与 T-CUT（a）：正常运行（截止日之后有一行不合格式，不被解析）
# ---------------------------------------------------------------------------

BAD_AFTER_CUTOFF = {dt.date(2023, 3, 1): "2023-03-01,not-a-price,x,y"}
EXPECTED_FILES = {
    "run_record.json", "input_snapshot.csv.gz", "window.json", "date_assertions.json", "overlap_check.json",
    "confirmatory.json", "diagnostics_validation.json", "descriptive.json", "daily_signals.csv.gz",
    "daily_targets.csv.gz", "daily_nav.csv.gz", "daily_policy.csv.gz", "r2_events.csv.gz", "r2_judgements.csv.gz",
    "segment_ledgers.csv.gz", "report.md", "information_state.json", "run_environment.json", "audit_log.jsonl",
    "MANIFEST.sha256"}


@pytest.fixture(scope="module")
def normal(tmp_path_factory: pytest.TempPathFactory, baseline: bytes) -> tuple[Path, subprocess.CompletedProcess]:
    root = build_root(tmp_path_factory.mktemp("normal"), baseline, BAD_AFTER_CUTOFF)
    return root, run_injected(root, baseline)


def test_normal_run_outputs(normal: tuple[Path, subprocess.CompletedProcess]) -> None:
    root, done = normal
    assert done.returncode == 0, done.stderr
    assert judge(done.returncode, done.stderr, root)["状态"] == "成功"
    found = listing(root)
    assert found["formal"] == ["evaluation_validation"] and len(found["tail"]) == 1
    assert found["failed"] == found["staging"] == found["note"] == []
    formal = research_dir(root) / "evaluation_validation"
    assert files_in(formal) == EXPECTED_FILES
    record = json.loads(get(formal / "run_record.json"))
    assert record["output_scheme"] == "v20-stage5-output-1" and record["usage_restriction"] == []
    assert record["holdout_gate"] is None and record["cutoff"] == "2022-12-30"
    assert record["registered_parameters"]["first_return_day"] == "2016-12-30"
    assert record["registered_parameters"]["basis_sha256"]["lock_supplement_1"] == registered.LOCK_SUPPLEMENT_SHA256
    assert record["registered_parameters"]["confirmatory"]["resamples"] == RESAMPLES       # 注入值（只为缩短耗时）
    assert set(record["source_hashes"]) == set(PREFLIGHT_FILES)
    dates = json.loads(get(formal / "date_assertions.json"))
    assert dates["passed"] is True and dates["d15"]["warmup_ok"] and dates["d15"]["convergence_ok"]
    overlap = json.loads(get(formal / "overlap_check.json"))
    assert overlap["passed"] is True and set(overlap["keys"]) == {"K=3,θ_P=0.025,h=3", "主参照"}
    confirmed = json.loads(get(formal / "confirmatory.json"))
    assert confirmed["period"] == "验证期" and confirmed["main"]["block"] == 20
    diagnostics = json.loads(get(formal / "diagnostics_validation.json"))
    if confirmed["valid"]:
        assert confirmed["category"] in ("A", "B", "C", "D")
        assert diagnostics["computed"] is True and diagnostics["window"]["first"] == "2016-12-29"
        assert diagnostics["window"]["last"] == "2022-12-30"
    report = get(formal / "report.md").decode("utf-8")
    assert report.startswith("# 波段预警 v2.0 验证期运行报告\n") and "实盘有效" not in report
    assert "验证期报告须照写本段。" in report and registered.STANDARD_R3_SHA256 in report
    state = json.loads(get(formal / "information_state.json"))
    assert state["s21_facts"]["s21_class"] == "待核对" and state["performance_info_status"] == "已产生"
    assert state["window_coverage"]["status"].startswith("不适用")
    for asset in ("SPX", "QQQ"):
        assert state["data_read"][asset]["layers"] == {"byte_read": "整份文件", "price_parse": "≤ 2022-12-30",
                                                       "compute": "≤ 2022-12-30"}
        assert state["evaluation_prices_parsed"][asset]["last_day"] == "2022-12-30"
    # T-CUT（a）：2023-03-01 的不合格式行在截止日之后，不被解析；快照末日为截止日。
    snapshot_rows = gzip_rows(get(formal / "input_snapshot.csv.gz"))
    assert snapshot_rows[-1][0] == "2022-12-30"
    signals = parse_signals(get(formal / "daily_signals.csv.gz"))
    assert {row[1] for row in signals} == {"K=3,θ_P=0.025,h=3", "主参照"}
    assert signals[0][0] == "2016-12-29" and signals[-1][0] == "2022-12-29"
    # 审计：两份行情文件各恰一次、只读；基准文件只读；无违规。
    events = lines_of(formal / "audit_log.jsonl")[:-1]
    market = [item for item in events if Path(item["path"]).parent.name == "daily"]
    assert sorted(Path(item["path"]).name for item in market) == ["QQQ.csv", "SPX.csv"]
    assert all(item["mode"] == "r" and item["violation"] is None for item in market)
    reference = [item for item in events if Path(item["path"]).parent.name == "evaluation_development"]
    assert sorted(Path(item["path"]).name for item in reference) == ["MANIFEST.sha256", "daily_signals.csv.gz"]
    assert all(item["mode"] == "r" for item in reference)
    assert all(item["violation"] is None for item in events)


def gzip_rows(data: bytes) -> list[list[str]]:
    import csv
    import gzip
    import io

    return list(csv.reader(io.StringIO(gzip.decompress(data).decode("utf-8"))))[1:]


# ---------------------------------------------------------------------------
# T-OVL（入口）：重叠范围内一日已发布字段被改动 → 停止，不进入确认性检验
# ---------------------------------------------------------------------------


def altered(signals: bytes) -> bytes:
    """把基准中冻结候选第 10 行的风险列改为另一个值（其余字节不变的含义）。"""
    rows = parse_signals(signals)
    index = [number for number, row in enumerate(rows) if row[1] == "K=3,θ_P=0.025,h=3"][10]
    row = list(rows[index])
    row[2] = "一级" if row[2] == "正常" else "正常"
    rows[index] = tuple(row)
    header = ("date", "object", "risk", "level", "c1", "c2", "all_valid")
    return gzip_bytes(csv_bytes(header, [tuple(None if value == "" else value for value in item) for item in rows]))


def test_overlap_mismatch_stops_before_confirmatory(tmp_path: Path, baseline: bytes) -> None:
    changed = altered(baseline)
    root = build_root(tmp_path / "root", changed)
    done = run_injected(root, changed)
    assert done.returncode == 1, done.stderr
    directory, failure = failure_of(root)
    assert files_in(directory) == FAILURE_FILES
    assert (failure["exit"], failure["stage"]) == ("重叠状态比对不符", "重叠比对")
    assert failure["detail"]["statement"] == ("计为验证期一次性运行已使用；已读入并解析验证期数据，"
                                              "不得称尚未接触验证期")
    assert failure["detail"]["overlap"]["mismatches"][0]["column"] == "risk"
    state = json.loads(get(directory / "information_state_final.json"))
    stages = [item["stage"] for item in state["stage_trace"]]
    assert "重叠比对" in stages and "确认性检验" not in stages and "必报诊断" not in stages


# ---------------------------------------------------------------------------
# T-NC1（入口）与 T-INV3、T-D-INV（入口）
# ---------------------------------------------------------------------------


def t0_day() -> dt.date:
    """构造行情的 t0（以 inputs、convergence 的公共函数独立计算，不经被测入口）。"""
    from decimal import Decimal

    from market_risk.wavewarn_v20.convergence import first_valid_index
    from market_risk.wavewarn_v20.inputs import snapshot_asset_days, snapshot_trend_days

    axis = tuple(stock_trading_days(FIRST["SPX"], dt.date(2002, 12, 31)))
    prices = {asset: {day: Decimal(value) for day, value in market_rows(asset) if day <= axis[-1]}
              for asset in ("SPX", "QQQ")}
    snapshot = make_snapshot(axis, prices, axis[-1], dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                             {"SPX": "0" * 64, "QQQ": "0" * 64})
    windows = registered.WINDOWS
    t0 = first_valid_index(snapshot_asset_days(snapshot, "SPX", windows), snapshot_asset_days(snapshot, "QQQ", windows),
                           snapshot_trend_days(snapshot, "SPX", windows))
    return axis[t0]


def test_never_converged_stops_like_the_development_entry(tmp_path: Path, baseline: bytes) -> None:
    """SPX 自 t0 的下一交易日起 value 全为空：严格读取缺价 → 前缀诊断读取（与开发期入口相同的处置，用途限制）→
    P_SPX 的全部初始状态运行始终不相遇，组合层“始终不收敛” → 清单生成前失败；不进入确认性检验、不计算诊断、不写优劣。"""
    start = t0_day()
    changes = {day: f"{day},,x,x,x,x,x,yahoo" for day, _ in market_rows("SPX") if day > start}
    root = build_root(tmp_path / "root", baseline, changes)
    done = run_injected(root, baseline)
    assert done.returncode == 1, done.stderr
    directory, failure = failure_of(root)
    assert files_in(directory) == FAILURE_FILES
    assert (failure["exit"], failure["stage"]) == ("始终不收敛", "组合层")
    assert failure["usage_restriction"] == ["前缀诊断运行（严格读取未通过：缺少必需价格）"]
    stages = [item["stage"] for item in json.loads(get(directory / "information_state_final.json"))["stage_trace"]]
    assert "组合层" in stages and "确认性检验" not in stages and "日期断言" not in stages


def test_missing_price_in_window_is_an_invalid_computation(tmp_path: Path, baseline: bytes) -> None:
    """SPX 2019-06-03 的 value 为空：信号模拟净值所需价格缺失 → 计算无效（登记第七节第 5 小节第三项）；照常发布，
    confirmatory.json 只写原因、不写类别；必报诊断不计算（T-D-INV）；报告不写优劣。"""
    day = dt.date(2019, 6, 3)
    root = build_root(tmp_path / "root", baseline, {day: f"{day},,x,x,x,x,x,yahoo"})
    done = run_injected(root, baseline)
    assert done.returncode == 0, done.stderr
    formal = research_dir(root) / "evaluation_validation"
    confirmed = json.loads(get(formal / "confirmatory.json"))
    assert confirmed["valid"] is False and confirmed["category"] is None
    assert confirmed["reason"] == "净值所需价格缺失，收益无法计算"
    diagnostics = json.loads(get(formal / "diagnostics_validation.json"))
    assert diagnostics["computed"] is False and diagnostics["reason"].startswith("计算无效")
    report = get(formal / "report.md").decode("utf-8")
    assert "- 类别：" not in report and "不写任何优劣结论" in report
    record = json.loads(get(formal / "run_record.json"))
    assert record["usage_restriction"] == ["前缀诊断运行（严格读取未通过：缺少必需价格）"]


# ---------------------------------------------------------------------------
# T-CUT（b）：审计钩子拒绝 SPX、QQQ 以外的行情文件
# ---------------------------------------------------------------------------


def test_audit_refuses_other_market_files(tmp_path: Path, baseline: bytes) -> None:
    """注入：读完 SPX 后再尝试读同一行情目录中的 SPY.csv（构造根中另放一份）→ 钩子以“受保护目录”阻止 → 失败；
    包内审计段记下该次违规。"""
    spy = csv_text(market_rows("SPX"))
    root = build_root(tmp_path / "root", baseline, extra={("data", "market", "daily", "SPY.csv"): spy})
    patch = ("real = data_v20.read_file_bytes\n"
             "def reading(path):\n"
             "    data = real(path)\n"
             "    if path.name == 'SPX.csv':\n"
             "        real(path.with_name('SPY.csv'))\n"
             "    return data\n"
             "data_v20.read_file_bytes = reading\n")
    done = run_injected(root, baseline, patch)
    assert done.returncode == 1, done.stderr
    directory, failure = failure_of(root)
    assert failure["exception"]["type"] == "PermissionError" and "受保护目录" in failure["exception"]["message"]
    events = lines_of(directory / "audit_log.jsonl")[:-1]
    assert [Path(item["path"]).name for item in events if item["violation"] == "受保护目录"] == ["SPY.csv"]


# ---------------------------------------------------------------------------
# 预检拒绝（未读入任何行情；不注入，因而同时检查钩子在任何读取之前安装）
# ---------------------------------------------------------------------------


def assert_rejected(done: subprocess.CompletedProcess) -> dict:
    assert done.returncode == 2, done.stderr
    report = stdout_report(done)
    state = report["information_state"]
    assert state["data_read"] == {} and state["performance_info_status"] == "未产生"
    assert not [item for item in report["open_events"] if Path(item["path"]).parent.name == "daily"]
    # 钩子安装时点：业务模块的导入（读入源码）出现在审计事件中，说明钩子先于它们安装。
    opened = {Path(item["path"]).name for item in report["open_events"]}
    assert {"data_v20.py", "registered_v20_validation.py", "validation_output.py"} <= opened or any(
        name.startswith(("data_v20.", "registered_v20_validation.")) for name in opened), sorted(opened)[:20]
    return report


@pytest.fixture(scope="module")
def plain_root(tmp_path_factory: pytest.TempPathFactory, baseline: bytes) -> Path:
    """不注入的拒绝情形共用的构造根（构造基准的哈希不等于登记值）。"""
    return build_root(tmp_path_factory.mktemp("plain"), baseline)


def test_preflight_rejects_unregistered_baseline(plain_root: Path) -> None:
    """不注入：构造 M3 清单的 SHA-256 不等于登记值 → 预检拒绝，未读入任何行情。"""
    assert "M3 清单的 SHA-256 与登记值不符" in assert_rejected(run_entry(plain_root))["reason"]


def test_preflight_rejects_missing_stage_five_module(tmp_path: Path, baseline: bytes) -> None:
    root = build_root(tmp_path / "root", baseline)
    git(root, "rm", "-q", "src/market_risk/wavewarn_v20/validation_run.py")
    git(root, "commit", "-q", "-m", "构造：删去一个预检文件")
    assert "预检文件缺失" in assert_rejected(run_entry(root))["reason"]


# ---------------------------------------------------------------------------
# T-HOLD：保留期模式默认禁用；启用条件缺任一项即拒绝（拒绝发生在任何行情读取之前）；全部满足时写入独立目录
# ---------------------------------------------------------------------------

AUTHORIZATION = "c" * 64


def gate_directory(directory: Path, signals: bytes, confirmed: dict | None = None,
                   tamper: str | None = None) -> tuple[Path, str]:
    """构造验证期正式输出（仓库外）：confirmatory.json、daily_signals.csv.gz 与只列这两个文件的清单。
    tamper 给出时，在写入清单之后改动该文件的内容（使其哈希与清单不符）。"""
    document = confirmed if confirmed is not None else {"valid": True, "category": "A", "main": {"valid": True}}
    files = {"confirmatory.json": json.dumps(document, ensure_ascii=False).encode("utf-8"),
             "daily_signals.csv.gz": signals}
    manifest = manifest_bytes(files)
    put(directory / "MANIFEST.sha256", manifest)
    for name, data in files.items():
        put(directory / name, data + (b" " if name == tamper else b""))
    return directory / "MANIFEST.sha256", hashlib.sha256(manifest).hexdigest()


def holdout_arguments(manifest: Path, digest: str, authorization: str = AUTHORIZATION) -> list[str]:
    return ["--mode", "holdout", "--validation-manifest", str(manifest), "--validation-manifest-sha256", digest,
            "--authorization-sha256", authorization]


def test_holdout_is_disabled_by_default(plain_root: Path, tmp_path: Path, baseline: bytes) -> None:
    manifest, digest = gate_directory(tmp_path / "v", baseline)
    full = holdout_arguments(manifest, digest)
    reason = assert_rejected(run_entry(plain_root, "--mode", "holdout"))["reason"]
    assert reason == ("保留期模式默认禁用：缺少参数 --validation-manifest、--validation-manifest-sha256、"
                      "--authorization-sha256")
    for flag in ("--validation-manifest", "--validation-manifest-sha256", "--authorization-sha256"):
        index = full.index(flag)
        partial = full[:index] + full[index + 2:]
        assert f"缺少参数 {flag}" in assert_rejected(run_entry(plain_root, *partial))["reason"]
    assert "--authorization-sha256 须为 64 位小写十六进制" in assert_rejected(
        run_entry(plain_root, *holdout_arguments(manifest, digest, "C" * 64)))["reason"]
    assert "--validation-manifest-sha256 须为 64 位小写十六进制" in assert_rejected(
        run_entry(plain_root, *holdout_arguments(manifest, "x")))["reason"]


@pytest.mark.parametrize(("case", "expected"), [
    ("manifest_hash", "验证期清单的 SHA-256 与 --validation-manifest-sha256 不符"),
    ("confirmatory_hash", "confirmatory.json 的哈希或字节数与验证期清单不符"),
    ("signals_hash", "daily_signals.csv.gz 的哈希或字节数与验证期清单不符"),
    ("category_C", "验证期类别为 C，不是 A 或 B"),
    ("category_D", "验证期类别为 D，不是 A 或 B"),
    ("invalid", "验证期确认性检验为计算无效"),
    ("main_invalid", "验证期主检验无效"),
])
def test_holdout_gate_rejections(plain_root: Path, tmp_path: Path, baseline: bytes, case: str, expected: str) -> None:
    documents = {"category_C": {"valid": True, "category": "C", "main": {"valid": True}},
                 "category_D": {"valid": True, "category": "D", "main": {"valid": True}},
                 "invalid": {"valid": False, "category": None, "main": None},
                 "main_invalid": {"valid": True, "category": "A", "main": {"valid": False}}}
    tamper = {"confirmatory_hash": "confirmatory.json", "signals_hash": "daily_signals.csv.gz"}.get(case)
    manifest, digest = gate_directory(tmp_path / "v", baseline, documents.get(case), tamper)
    if case == "manifest_hash":
        digest = "0" * 64
    reason = assert_rejected(run_entry(plain_root, *holdout_arguments(manifest, digest)))["reason"]
    assert reason.startswith("保留期启用条件不满足：") and expected in reason


def test_holdout_runs_into_its_own_directory(tmp_path: Path, normal: tuple[Path, subprocess.CompletedProcess],
                                             baseline: bytes) -> None:
    """全部条件满足（注入替换重抽样次数）：以正常验证期运行的 daily_signals.csv.gz 为基准、类别 A 的构造检验文件
    为启用凭据，保留期运行写入独立目录 evaluation_holdout/；验证期目录不出现；授权哈希照录、注明程序未验证授权。"""
    validation_root, _ = normal
    signals = get(research_dir(validation_root) / "evaluation_validation" / "daily_signals.csv.gz")
    manifest, digest = gate_directory(tmp_path / "v", signals)
    root = build_root(tmp_path / "root", baseline)
    done = run_injected(root, baseline, arguments=tuple(holdout_arguments(manifest, digest)))
    assert done.returncode == 0, done.stderr
    assert judge(done.returncode, done.stderr, root, "evaluation_holdout")["状态"] == "成功"
    assert listing(root)["formal"] == []
    formal = research_dir(root) / "evaluation_holdout"
    names = files_in(formal)
    assert "diagnostics_holdout.json" in names and "diagnostics_validation.json" not in names
    record = json.loads(get(formal / "run_record.json"))
    assert record["cutoff"] == "2026-09-25" and record["registered_parameters"]["first_return_day"] == "2022-12-30"
    gate = record["holdout_gate"]
    assert gate["authorization_sha256"] == AUTHORIZATION and gate["validation_category"] == "A"
    assert gate["validation_manifest_sha256"] == digest and "入口不验证授权真伪" in gate["authorization_note"]
    assert json.loads(get(formal / "date_assertions.json"))["passed"] is True
    overlap = json.loads(get(formal / "overlap_check.json"))
    assert overlap["passed"] is True and overlap["keys"]["主参照"]["first"] == "2016-12-29"
    assert json.loads(get(formal / "confirmatory.json"))["period"] == "保留期"
    report = get(formal / "report.md").decode("utf-8")
    assert report.startswith("# 波段预警 v2.0 保留期运行报告\n") and "程序未验证授权" in report


# ---------------------------------------------------------------------------
# T-EQV：重写的私有部分与开发期入口行为等价（同一构造根、同一场景，两个入口各运行一次，不注入）
# ---------------------------------------------------------------------------


def test_equivalence_preflight_rejection(tmp_path: Path) -> None:
    """正式目录已存在：两个入口都以退出码 2 拒绝；标准输出报告的键只多 mode 一项；信息状态的键与阶段轨迹相同；
    两者的审计事件中都没有行情文件，且都记下了钩子安装之后的业务模块导入。"""
    root = build_root(tmp_path / "root", None)
    for name in ("evaluation_development", "evaluation_validation"):
        research_dir(root).joinpath(name).mkdir(parents=True)
    mine = assert_rejected(run_entry(root))
    theirs = stdout_report(run_entry(root, module=DEVELOPMENT))
    assert theirs["exit_code"] == 2 and "正式目录已存在" in theirs["reason"] and "正式目录已存在" in mine["reason"]
    assert set(mine) == set(theirs) | {"mode"}
    assert set(mine["information_state"]) == set(theirs["information_state"])
    assert mine["information_state"]["stage_trace"] == theirs["information_state"]["stage_trace"]


def test_equivalence_failure_before_manifest(tmp_path: Path, baseline: bytes) -> None:
    """严格读取失败（SPX 2000-01-03 的来源标识不在允许清单内）：两个入口（验证入口以保留期模式、启用凭据齐备）都以
    退出码 1 结束；失败目录的文件集合、failure.json 与最终信息状态的键、出口与阶段、阶段轨迹的进入与离开顺序都相同；
    各有一份尾段，收尾记录为“失败”，衔接记录指向失败清单。"""
    day = dt.date(2000, 1, 3)
    changes = {day: f"{day},100.00,x,x,x,x,x,other"}
    # 两个入口各用一个构造根（同一构造输入、同一提交号）：一个入口写出的失败证据会使另一个入口的预检判“工作区不干净”。
    roots = {"evaluation_holdout": build_root(tmp_path / "mine", None, changes),
             "evaluation_development": build_root(tmp_path / "theirs", None, changes)}
    manifest, digest = gate_directory(tmp_path / "v", baseline)
    mine = run_entry(roots["evaluation_holdout"], *holdout_arguments(manifest, digest))
    theirs = run_entry(roots["evaluation_development"], module=DEVELOPMENT)
    assert mine.returncode == theirs.returncode == 1, (mine.stderr, theirs.stderr)
    results = {}
    for name, done in (("evaluation_holdout", mine), ("evaluation_development", theirs)):
        root = roots[name]
        directory, failure = failure_of(root, name)
        state = json.loads(get(directory / "information_state_final.json"))
        tail = lines_of(research_dir(root) / listing(root, name)["tail"][0])
        results[name] = (files_in(directory), failure, state, tail, stdout_report(done))
    (files_a, failure_a, state_a, tail_a, report_a), (files_b, failure_b, state_b, tail_b, report_b) = results.values()
    assert files_a == files_b == FAILURE_FILES
    assert set(failure_a) == set(failure_b) and (failure_a["exit"], failure_a["stage"]) == (
        failure_b["exit"], failure_b["stage"]) == ("输入校验失败", "严格读取")
    assert set(state_a) == set(state_b)
    assert [(item["stage"], item["action"]) for item in state_a["stage_trace"]] == [
        (item["stage"], item["action"]) for item in state_b["stage_trace"]]
    for tail in (tail_a, tail_b):
        assert tail[0]["record"] == "link" and tail[0]["manifest_file"] == "FAILURE_MANIFEST.sha256"
        assert tail[-1]["record"] == "close" and tail[-1]["outcome"] == "失败"
    assert set(tail_a[0]) == set(tail_b[0]) and set(tail_a[-1]) == set(tail_b[-1])
    assert set(report_a) == set(report_b) | {"mode"}
