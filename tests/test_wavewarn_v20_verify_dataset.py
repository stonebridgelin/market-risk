"""v2.0 真实文件元数据核对入口的验收（阶段三实施指令第七节第 5 部分）：子进程测试。

所有子进程的 --root 都指向 pytest 的临时目录，其下按仓库布局放置构造的两份行情文件与两份配置文件；
不读取真实研究数据或真实项目配置。子进程必然还会读取解释器、依赖与项目源码；子进程中的访问不在外层拦截范围内，
带拦截运行时这些测试照实标“通过但覆盖不完整”。
比较与格式化逻辑另有纯函数测试（在本进程里普通导入入口模块，普通导入不安装钩子）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from market_risk.wavewarn_v20 import verify_dataset

REPO = Path(__file__).resolve().parents[1]
MODULE = "market_risk.wavewarn_v20.verify_dataset"
SPX_ROWS = [("2001-01-02", "100.00"), ("2001-01-03", "101.00"), ("2001-01-04", "102.00"), ("2001-01-05", "103.00")]
QQQ_ROWS = [("2001-01-03", "50.00"), ("2001-01-04", "51.00"), ("2001-01-05", "52.00")]
DECISIONS = ("- date: 2001-01-04\n  symbol: SPY\n  decision: exclude\n  reason: 构造\n  decided_on: 2001-06-01\n")


def csv_bytes(rows: list[tuple[str, str]]) -> bytes:
    return ("date,value,source\n" + "".join(f"{day},{price},yahoo\n" for day, price in rows)).encode("utf-8")


def put(path: Path, data: bytes) -> Path:
    """把构造的字节写进临时目录（必要时建立上级目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def registered_yaml(files: dict[str, tuple[bytes, list[tuple[str, str]]]]) -> str:
    """由构造的字节直接算出登记值（不经过被测代码）。"""
    lines = ["dataset:"]
    for asset, (raw, rows) in files.items():
        digest = hashlib.sha256(raw).hexdigest()
        lines += [f"  {asset}:", f"    raw_sha256: {digest}", f"    normalized_sha256: {digest}",
                  f"    data_rows: {len(rows)}", f"    first_date: {rows[0][0]}", f"    last_date: {rows[-1][0]}"]
    return "\n".join(lines) + "\n"


def layout(root: Path, spx: list[tuple[str, str]] = SPX_ROWS, qqq: list[tuple[str, str]] = QQQ_ROWS) -> dict[str, Path]:
    """按仓库布局构造两份行情文件与两份配置文件。"""
    daily = root.joinpath("data", "market", "daily")
    files = {"SPX": (csv_bytes(spx), spx), "QQQ": (csv_bytes(qqq), qqq)}
    paths = {asset: put(daily / f"{asset}.csv", raw) for asset, (raw, _) in files.items()}
    paths["dataset"] = put(root / "config" / "wavewarn_v20.yaml", registered_yaml(files).encode("utf-8"))
    paths["decisions"] = put(root / "config" / "data_decisions.yaml", DECISIONS.encode("utf-8"))
    return paths


def run(arguments: list[str], root: Path) -> subprocess.CompletedProcess:
    """在新的解释器里运行；工作目录为临时目录。"""
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, *arguments], cwd=root, env=environment, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def run_entry(root: Path) -> tuple[int, dict]:
    done = run(["-m", MODULE, "--root", str(root)], root)
    return done.returncode, json.loads(done.stdout)


def same_path(first: object, second: object) -> bool:
    return verify_dataset.normalized(first) == verify_dataset.normalized(second)


# ---------------------------------------------------------------------------
# 1、2：钩子的安装时点
# ---------------------------------------------------------------------------

COUNTING = """\
import json, sys
calls = []
original = sys.addaudithook
watched = ("market_risk.wavewarn_v20.data_v20", "market_risk.wavewarn_v20.config_v20", "market_risk.storage.paths")
def counting(hook):
    calls.append([name in sys.modules for name in watched])
    return original(hook)
sys.addaudithook = counting
"""


def test_plain_import_installs_no_hook_and_loads_no_business_module(tmp_path: Path) -> None:
    script = COUNTING + f"import {MODULE}\n" + (
        "print('RESULT ' + json.dumps({'calls': calls, 'loaded': [name in sys.modules for name in watched]}))\n")
    done = run(["-c", script], tmp_path)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.split("RESULT ", 1)[1])
    assert result == {"calls": [], "loaded": [False, False, False]}       # 调用次数为 0；业务模块都没有被导入


def test_entry_installs_the_hook_once_before_importing_business_modules(tmp_path: Path) -> None:
    layout(tmp_path)
    script = COUNTING + (
        "import runpy\n"
        f"sys.argv = ['verify_dataset', '--root', {str(tmp_path)!r}]\n"
        "try:\n"
        f"    runpy.run_module({MODULE!r}, run_name='__main__')\n"
        "except SystemExit as stop:\n"
        "    code = stop.code\n"
        "loaded = [name in sys.modules for name in watched]\n"
        "print('RESULT ' + json.dumps({'calls': calls, 'code': code, 'loaded': loaded}))\n")
    done = run(["-c", script], tmp_path)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.split("RESULT ", 1)[1])
    assert result["calls"] == [[False, False, False]]                     # 恰好安装一次；安装时三个模块都还没有加载
    assert result["loaded"] == [True, True, True] and result["code"] == 0


# ---------------------------------------------------------------------------
# 3—6：核对结果、打开记录、不解析价格
# ---------------------------------------------------------------------------


def test_entry_passes_when_files_match_registered_values(tmp_path: Path) -> None:
    paths = layout(tmp_path)
    code, report = run_entry(tmp_path)
    assert code == 0 and report["passed"] is True
    assert [item["asset"] for item in report["files"]] == ["SPX", "QQQ"]
    spx = report["files"][0]
    assert same_path(spx["path"], paths["SPX"]) and spx["differences"] == [] and spx["total_rows"] == 5
    values = {item["name"]: item for item in spx["items"]}
    assert values["raw_sha256"]["file"] == hashlib.sha256(csv_bytes(SPX_ROWS)).hexdigest()
    assert (values["data_rows"]["file"], values["first_date"]["file"], values["last_date"]["file"]) == (
        "4", "2001-01-02", "2001-01-05")
    assert all(item["equal"] for item in spx["items"]) and len(spx["items"]) == 5
    assert "本命令不启动子进程" in report["coverage"]


def test_entry_fails_and_lists_differences_when_a_file_changed(tmp_path: Path) -> None:
    paths = layout(tmp_path)
    put(paths["QQQ"], csv_bytes([*QQQ_ROWS, ("2001-01-08", "53.00")]))            # 登记之后文件多了一行
    code, report = run_entry(tmp_path)
    assert code == 1 and report["passed"] is False
    spx, qqq = report["files"]
    assert spx["differences"] == []
    assert len(qqq["differences"]) == 4                                           # 两种哈希、数据行数、末日
    assert any("数据行数：文件为 4，登记为 3" in text for text in qqq["differences"])
    assert any("末日：文件为 2001-01-08，登记为 2001-01-05" in text for text in qqq["differences"])


def test_open_records_cover_exactly_the_two_data_files_and_two_config_files(tmp_path: Path) -> None:
    paths = layout(tmp_path)
    code, report = run_entry(tmp_path)
    assert code == 0 and report["open_check"]["problems"] == []
    market = report["open_check"]["market"]
    assert sorted(verify_dataset.normalized(path) for path, _, _ in market) == sorted(
        verify_dataset.normalized(paths[asset]) for asset in ("SPX", "QQQ"))      # 行情文件恰为两份，各一次
    # 只读：审计事件给出的模式对 "rb" 与 "r" 都是 "r"，这里能确认的是只读；
    # 二进制由行情读取模块里登记的那一处打开语句保证。
    assert [mode for _, mode, _ in market] == ["r", "r"]
    assert all(verify_dataset.read_only(mode, flags) for _, mode, flags in market)
    config = {verify_dataset.normalized(path) for path, _, _ in report["open_check"]["config"]}
    assert config == {verify_dataset.normalized(paths["dataset"]), verify_dataset.normalized(paths["decisions"])}
    # 全部打开记录里，没有临时目录以外的项目数据或配置路径。
    protected = [verify_dataset.normalized(REPO / name) + os.sep for name in ("data", "config")]
    records = [verify_dataset.normalized(item["path"]) for item in report["open_records"]]
    assert records and not [path for path in records if path.startswith(tuple(protected))]
    # 只有路径、模式、标志位与是否只读，没有任何文件内容。
    assert all(set(item) == {"path", "mode", "flags", "read_only", "conclusion"} for item in report["open_records"])
    assert {item["conclusion"] for item in report["open_records"]} == {"只读"}
    assert all(item["read_only"] for item in report["open_records"])               # 整个命令没有写任何文件
    assert report["skipped_flags"] == verify_dataset.skipped_flag_names()          # 跳过的禁止标志在输出中列明


def test_entry_does_not_parse_prices(tmp_path: Path) -> None:
    bad = [("2001-01-02", "不是数字"), ("2001-01-03", "abc"), ("2001-01-04", "")]
    layout(tmp_path, spx=bad)
    code, report = run_entry(tmp_path)
    assert code == 0 and report["passed"] is True
    values = {item["name"]: item["file"] for item in report["files"][0]["items"]}
    assert (values["data_rows"], values["first_date"], values["last_date"]) == ("3", "2001-01-02", "2001-01-04")


def test_entry_reports_configuration_problems_with_a_non_zero_exit(tmp_path: Path) -> None:
    paths = layout(tmp_path)
    put(paths["decisions"], b"")                                                   # 裁定表为空：配置读取报错
    done = run(["-m", MODULE, "--root", str(tmp_path)], tmp_path)
    assert done.returncode != 0 and "V20ConfigError" in done.stderr


# ---------------------------------------------------------------------------
# 比较与格式化逻辑的纯函数测试（本进程，普通导入）
# ---------------------------------------------------------------------------


def test_open_classification_rules(tmp_path: Path) -> None:
    daily = tmp_path.joinpath("data", "market", "daily")
    spx, qqq = daily / "SPX.csv", daily / "QQQ.csv"
    dataset, decisions = tmp_path / "config" / "wavewarn_v20.yaml", tmp_path / "config" / "data_decisions.yaml"
    reading = os.O_RDONLY
    good = [(str(dataset), "r", reading), (str(decisions), "r", reading), (str(spx), "r", reading),
            (str(qqq), "r", reading), (str(tmp_path / "elsewhere" / "module.py"), "r", reading)]

    def problems(records: list[tuple[str, str, int]]) -> list[str]:
        return verify_dataset.classify_opens(records, tmp_path, [spx, qqq], [dataset, decisions])["problems"]

    assert problems(good) == []
    assert problems([*good, (str(daily / "SPY.csv"), "r", reading)]) != []         # 多读了一份行情文件
    assert problems([*good, (str(spx), "r", reading)]) != []                       # 同一份行情文件打开了两次
    assert problems(good[:3] + good[4:]) != []                                     # 少读了一份
    assert problems([*good[:2], (str(spx), "w", reading), good[3]]) != []          # 不是只读（模式）
    assert problems([*good[:2], (str(spx), "r", os.O_RDWR), good[3]]) != []        # 不是只读（标志位）
    assert problems([*good, (str(tmp_path / "config" / "settings.yaml"), "r", reading)]) != []   # 多读了一份配置
    assert problems(good[1:]) != []                                                # 少读了一份配置


@pytest.mark.parametrize("mode", ["w", "a", "x", "r+", "rb", ""])
def test_read_only_rejects_any_mode_other_than_r(mode: str) -> None:
    assert not verify_dataset.read_only(mode, os.O_RDONLY)            # 审计事件只给出 "r"；"rb" 出现也视为不符


@pytest.mark.parametrize("name", ["O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_EXCL"])
def test_read_only_rejects_write_access_and_registered_forbidden_flags(name: str) -> None:
    """标志位取运行平台上的符号常量，不写死数值；平台没有的禁止标志跳过，并在输出中列明。"""
    if not hasattr(os, name):
        assert name in verify_dataset.skipped_flag_names()
        return
    assert not verify_dataset.read_only("r", os.O_RDONLY | getattr(os, name))


def test_read_only_accepts_plain_reading_and_rejects_missing_flags() -> None:
    assert verify_dataset.read_only("r", os.O_RDONLY)
    assert verify_dataset.read_only("r", os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
    assert not verify_dataset.read_only("r", None) and not verify_dataset.read_only("r", True)
    # 标志位缺失、类型非法或为布尔值：结论是“无法确认只读”，不表述为已发生写入（补充裁决第一部分第 8 条）。
    for flags in (None, True, False, "0", 0.0):
        assert verify_dataset.read_only_conclusion("r", flags) == "无法确认只读"
    assert verify_dataset.read_only_conclusion("r", os.O_RDONLY) == "只读"
    assert verify_dataset.read_only_conclusion("r", os.O_RDWR) == "不是只读"
    assert verify_dataset.read_only_conclusion("w", os.O_RDONLY) == "不是只读"


def test_classification_distinguishes_unknown_from_not_read_only(tmp_path: Path) -> None:
    daily = tmp_path.joinpath("data", "market", "daily")
    spx, qqq = daily / "SPX.csv", daily / "QQQ.csv"
    dataset, decisions = tmp_path / "config" / "wavewarn_v20.yaml", tmp_path / "config" / "data_decisions.yaml"
    configs = [(str(dataset), "r", os.O_RDONLY), (str(decisions), "r", os.O_RDONLY)]

    def problems(spx_flags: object) -> list[str]:
        records = [*configs, (str(spx), "r", spx_flags), (str(qqq), "r", os.O_RDONLY)]
        return verify_dataset.classify_opens(records, tmp_path, [spx, qqq], [dataset, decisions])["problems"]

    assert problems(None) == ["无法确认行情文件以只读方式打开（标志位缺失或类型非法）"]
    assert problems(os.O_RDWR) == ["行情文件不是以只读方式打开"]
    assert problems(os.O_RDONLY) == []
    assert verify_dataset.forbidden_flag_names() == ("O_CREAT", "O_TRUNC", "O_APPEND", "O_EXCL")
    assert verify_dataset.skipped_flag_names() == [name for name in verify_dataset.forbidden_flag_names()
                                                   if not hasattr(os, name)]


def test_plain_import_in_this_process_has_only_functions_at_top_level() -> None:
    assert verify_dataset.default_root() == REPO
    assert callable(verify_dataset.main) and "不启动子进程" in verify_dataset.coverage_note()
    with pytest.raises(AttributeError):
        verify_dataset.data_v20                                                    # type: ignore[attr-defined]  # noqa: B018
