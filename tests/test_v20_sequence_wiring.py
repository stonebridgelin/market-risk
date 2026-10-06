"""真实序列接线模式（N5，tests/v20_sequence_wiring.py）的纯转换、边界与短序列工具运行测试
（阶段四 M2 第二部分指令修订六第五节第 2、5 小节，第六节第 2 小节）。

- 构造快照一律在内存或 tmp_path 中生成，不读任何演习产物、不读真实行情。
- 短序列：60 个 NYSE 交易日（2015-01-02 至 2015-03-30，扣除 2015-01-19、2015-02-16 两个休市日），只验证场景格式层
  与工具确实被调用；不作完整评价成功的验收。
- 子进程只启动工具 1709880（哈希先核对）、本文件写入 tmp_path 的替身脚本与 tasklist；每个 subprocess.run 都带 timeout。
  带拦截运行时这些测试照实标“通过但覆盖不完整”（子进程中的访问不在外层拦截范围内）。
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import v20_sequence_wiring as sw

TOOL_ROOT = Path(r"C:\Users\stone\v20_tool_1709880")
AUDIT = TOOL_ROOT / "docs" / "audit" / "独立复核" / "v20" / "audit_v20.py"
AUDIT_SHA256 = "8ff8081828c94134bc0d3e000ea77f3442564241438c8d444c601003bb19ca3d"
TOOL_TIMEOUT = 600                                   # 第六节第 2 小节：短序列工具运行时限（秒）
HOLIDAYS_2015 = (dt.date(2015, 1, 19), dt.date(2015, 2, 16))      # NYSE 2015 年 1—3 月的休市日（周一）
DIGEST = "0" * 64


# ---------------------------------------------------------------------------
# 构造与读写辅助（文件写入、读取、子进程各集中在一处）
# ---------------------------------------------------------------------------


def put(path: Path, data: bytes) -> Path:
    """把构造的字节写进 tmp_path（必要时建立上级目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def get(path: Path) -> bytes:
    """读回 tmp_path 中由被测函数、工具或替身脚本写出的文件。"""
    return path.read_bytes()


def run_command(arguments: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    """以子进程运行（工具、替身脚本或 tasklist）；与比对接线 run_tool_timed 同一写法，另带 timeout。"""
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(arguments, cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=timeout)


def short_axis() -> list[dt.date]:
    """2015-01-02 起的 60 个 NYSE 交易日（周一至周五，扣除两个休市日）。"""
    days, day = [], dt.date(2015, 1, 2)
    while len(days) < 60:
        if day.weekday() < 5 and day not in HOLIDAYS_2015:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def price(index: int, base: int) -> str:
    return f"{base + index % 7}.{(index * 13) % 100:02d}"


def rows_for(days: list[dt.date], qqq_from: int = 0) -> list[list[str]]:
    """原始行（含表头）：SPX 全程有价；QQQ 自下标 qqq_from 起有价，此前为空。"""
    rows = [list(sw.HEADER)]
    for index, day in enumerate(days):
        rows.append([day.isoformat(), price(index, 100), price(index, 50) if index >= qqq_from else ""])
    return rows


def gz_bytes(rows: list[list[str]]) -> bytes:
    return gzip.compress(("\n".join(",".join(row) for row in rows) + "\n").encode("utf-8"), mtime=0)


def registration_for(days: list[dt.date], note: str, segments: tuple | None = None,
                     qqq_from: int = 0) -> sw.Registration:
    """测试登记：两资产历史起点、一段分段与截止日都取自构造轴。"""
    chosen = segments if segments is not None else (("构造段", days[9], days[49]),)
    return sw.Registration(chosen, (("SPX", days[0]), ("QQQ", days[qqq_from])), days[-1],
                           f"{sw.TEST_SOURCE_PREFIX}{note}")


def convert(rows: list[list[str]], registration: sw.Registration, cutoff: dt.date | None = None,
            parser: sw.PriceParser = sw.parse_price) -> dict:
    return sw.scenario_from_rows(rows, cutoff or registration.cutoff, DIGEST, "sequence_test", registration, parser)


def short_scenario_file(tmp_path: Path) -> tuple[Path, dict]:
    """60 日快照 → N5 完整校验（测试登记）→ write_scenario 写出场景文件。"""
    days = short_axis()
    registration = registration_for(days, "短序列")
    data = gz_bytes(rows_for(days))
    digest = sw.check_snapshot_hash(data, hashlib.sha256(data).hexdigest())
    scenario = sw.scenario_from_rows(sw.rows_from_bytes(data), days[-1], digest, "sequence_short", registration,
                                     sw.parse_price)
    path = sw.write_scenario(tmp_path / "scenario.json", sw.scenario_bytes(scenario), None)
    return path, scenario


def tool_report(scenario: Path, out: Path) -> tuple[subprocess.CompletedProcess, dict]:
    """核对 audit_v20.py 哈希后以子进程运行工具；返回进程结果与工具输出 JSON（照录退出码与标准错误）。"""
    assert hashlib.sha256(get(AUDIT)).hexdigest() == AUDIT_SHA256, "audit_v20.py 哈希不符"
    done = run_command([sys.executable, str(AUDIT), str(scenario), str(out)], TOOL_ROOT, TOOL_TIMEOUT)
    assert out.is_file(), f"工具未写出输出：退出码 {done.returncode}，标准错误 {done.stderr[-2000:]}"
    return done, json.loads(get(out).decode("utf-8"))


# ---------------------------------------------------------------------------
# 第五节第 2 小节：字段映射表“构造测试”列
# ---------------------------------------------------------------------------


def test_fixed_fields() -> None:
    days = short_axis()
    scenario = convert(rows_for(days), registration_for(days, "固定字段"))
    assert (scenario["kind"], scenario["params"], scenario["calendar"], scenario["name"]) == (
        "full", "registered", "NYSE", "sequence_test")
    assert scenario["axis"] == [day.isoformat() for day in days] and scenario["cutoff"] == "2015-03-30"
    assert scenario["prices"]["SPX"][0] == "100.00" and scenario["prices"]["QQQ"][1] == "51.13"
    assert scenario["r1_segments"] == [[days[9].isoformat(), days[49].isoformat()]]
    assert set(scenario) == {"kind", "name", "axis", "calendar", "cutoff", "prices", "params", "r1_segments", "meta"}


def test_meta_records_registration_source() -> None:
    days = short_axis()
    scenario = convert(rows_for(days), registration_for(days, "来源"))
    assert scenario["meta"]["登记来源"] == "测试登记:来源" and scenario["meta"]["快照SHA256"] == DIGEST
    assert scenario["meta"]["类型"] == "真实序列" and scenario["meta"]["缺价"] == []
    assert sw.REGISTERED.source == "registered_v20"


def test_registered_values_come_from_the_module() -> None:
    assert sw.REGISTERED.cutoff == dt.date(2016, 12, 30)
    assert dict(sw.REGISTERED.histories) == {"SPX": dt.date(1990, 1, 2), "QQQ": dt.date(1999, 3, 10)}
    assert [item[1:] for item in sw.REGISTERED.segments] == [
        (dt.date(1998, 12, 31), dt.date(2009, 12, 31)), (dt.date(2009, 12, 31), dt.date(2016, 12, 30)),
        (dt.date(2000, 3, 24), dt.date(2002, 10, 9)), (dt.date(2007, 10, 9), dt.date(2009, 3, 9))]


def test_missing_price_maps_to_null() -> None:
    days = short_axis()
    rows = rows_for(days)
    rows[31][2] = ""
    scenario = convert(rows, registration_for(days, "缺价"))
    assert scenario["prices"]["QQQ"][30] is None and scenario["prices"]["SPX"][30] is not None


def test_duplicate_date_stops() -> None:
    days = short_axis()
    rows = rows_for(days)
    rows[6][0] = rows[5][0]
    with pytest.raises(sw.SequenceError, match="日期重复"):
        convert(rows, registration_for(days, "重复"))


def test_out_of_order_stops() -> None:
    days = short_axis()
    rows = rows_for(days)
    rows[5], rows[6] = rows[6], rows[5]
    with pytest.raises(sw.SequenceError, match="日期乱序"):
        convert(rows, registration_for(days, "乱序"))


def test_bad_date_lexeme_stops() -> None:
    days = short_axis()
    for bad in ("2015-1-05", "2015/01/05", "2015-02-30", " 2015-01-05"):
        rows = rows_for(days)
        rows[2][0] = bad
        with pytest.raises(sw.SequenceError, match="日期"):
            convert(rows, registration_for(days, "日期词法"))


def test_price_lexeme_rejects_nan_inf_negative() -> None:
    days = short_axis()
    for bad in ("nan", "inf", "-1.00", "1.005", "1.0", "0.00", "1e2", "+1.00", " 1.00"):
        rows = rows_for(days)
        rows[3][1] = bad
        with pytest.raises(sw.SequenceError, match="价格"):
            convert(rows, registration_for(days, "价格词法"))


def test_swapped_columns_stop() -> None:
    days = short_axis()
    rows = rows_for(days)
    rows[0] = ["date", "qqq_close", "spx_close"]
    with pytest.raises(sw.SequenceError, match="表头"):
        convert(rows, registration_for(days, "错位"))


def test_extra_column_stops() -> None:
    days = short_axis()
    rows = rows_for(days)
    rows[0] = [*sw.HEADER, "volume"]
    with pytest.raises(sw.SequenceError, match="表头"):
        convert(rows, registration_for(days, "多列"))
    rows = rows_for(days)
    rows[4].append("9.99")
    with pytest.raises(sw.SequenceError, match="列数"):
        convert(rows, registration_for(days, "多列"))


def test_rows_after_cutoff_are_not_parsed() -> None:
    """第五节第 5 小节：截止日之后追加一行 spx_close 为 BAD；先按 date 丢弃再解析，解析函数未被调用于该行。"""
    days = short_axis()
    rows = [*rows_for(days), ["2015-03-31", "BAD", "BAD"]]
    calls: list[str] = []

    def counting(text: str) -> str | None:
        calls.append(text)
        return sw.parse_price(text)

    scenario = convert(rows, registration_for(days, "截止日之后"), parser=counting)
    assert scenario["axis"][-1] == scenario["cutoff"] == "2015-03-30" and "2015-03-31" not in scenario["axis"]
    assert len(calls) == 2 * len(days) and "BAD" not in calls


def test_cutoff_must_equal_last_axis_day() -> None:
    """截止日当日无行（原始行末日早于 cutoff）→ 轴末日不等于截止日。"""
    days = short_axis()
    registration = registration_for(days, "截止日当日无行", segments=(("构造段", days[9], days[40]),))
    with pytest.raises(sw.SequenceError, match="轴末日不等于截止日"):
        convert(rows_for(days[:-1]), registration)


def test_cutoff_after_development_end_stops() -> None:
    days = short_axis()
    with pytest.raises(sw.SequenceError, match="晚于登记截止日"):
        convert(rows_for(days), sw.REGISTERED, cutoff=dt.date(2017, 1, 3))


def test_snapshot_hash_mismatch_stops(tmp_path: Path) -> None:
    """文件级哈希：篡改副本的一个字节 → 停；与截止日检查各自独立断言。"""
    data = gz_bytes(rows_for(short_axis()))
    registered = hashlib.sha256(data).hexdigest()
    path = put(tmp_path / sw.SNAPSHOT_FILE, data)
    assert sw.check_snapshot_hash(sw.read_snapshot_csv(path), registered) == registered
    tampered = bytearray(data)
    tampered[-5] ^= 0x01
    copy = put(tmp_path / "tampered" / sw.SNAPSHOT_FILE, bytes(tampered))
    with pytest.raises(sw.SequenceError, match="快照 SHA-256 不符"):
        sw.check_snapshot_hash(sw.read_snapshot_csv(copy), registered)


def test_segments_outside_axis_stop() -> None:
    days = short_axis()
    registration = registration_for(days, "分段", segments=(("构造段", days[9], dt.date(2015, 1, 3)),))
    with pytest.raises(sw.SequenceError, match="不在轴上"):
        convert(rows_for(days), registration)


def test_late_start_meta_from_registration() -> None:
    """缺价长度 = 登记起点在轴上的下标（不是首个非空值的下标）。"""
    days = short_axis()
    scenario = convert(rows_for(days, qqq_from=12), registration_for(days, "晚开始", qqq_from=12))
    assert scenario["meta"]["类型"] == "真实序列_QQQ晚开始"
    assert scenario["meta"]["缺价"] == [{"资产": "QQQ", "起": 0, "长度": 12}]
    assert scenario["prices"]["QQQ"][:12] == [None] * 12 and scenario["prices"]["QQQ"][12] is not None


def test_price_before_registered_start_stops() -> None:
    days = short_axis()
    with pytest.raises(sw.SequenceError, match="之前有价格"):
        convert(rows_for(days, qqq_from=10), registration_for(days, "起点前有价", qqq_from=12))


def test_registered_start_missing_price_stops() -> None:
    days = short_axis()
    with pytest.raises(sw.SequenceError, match="缺价"):
        convert(rows_for(days, qqq_from=13), registration_for(days, "起点缺价", qqq_from=12))


def test_run_record_reads_only_cutoff(tmp_path: Path) -> None:
    """补充单 K4 第 4 条：read_run_record 只读 cutoff（commit 等其他键不读、不核对）。"""
    good = {"cutoff": "2016-12-30", "commit": "不是提交号", "other": 1}
    path = put(tmp_path / sw.RUN_RECORD_FILE, json.dumps(good).encode("utf-8"))
    assert sw.read_run_record(path) == dt.date(2016, 12, 30)
    for bad in ({"commit": "a" * 40}, {"cutoff": 20161230}, {"cutoff": "2016-12-32"}):
        with pytest.raises(sw.SequenceError):
            sw.run_record_cutoff(bad)


# ---------------------------------------------------------------------------
# 第六节第 2 小节：短序列、停止原因反例、写入中断、超时与终止
# ---------------------------------------------------------------------------


def test_sequence_short_scenario_passes_n5_and_tool_stops_with_t0_absent(tmp_path: Path) -> None:
    path, scenario = short_scenario_file(tmp_path)
    assert len(scenario["axis"]) == 60 and scenario["meta"]["登记来源"] == "测试登记:短序列"
    done, report = tool_report(path, tmp_path / "tool.json")
    stop = report.get("stop_reason") or {}
    assert (done.returncode, stop.get("reason")) == (3, "t0 不存在"), (
        f"退出码 {done.returncode}；stop_reason {stop}；标准错误 {done.stderr[-2000:]}")


def test_sequence_short_scenario_rejected_under_registered(tmp_path: Path) -> None:
    """同一 60 日快照、默认 REGISTERED：分段端点不在轴上 → SequenceError；工具未被调用（调用计数 0）。"""
    days = short_axis()
    calls: list[Path] = []
    data = gz_bytes(rows_for(days))
    with pytest.raises(sw.SequenceError, match=r"分段 .* 不在轴上"):
        scenario = sw.scenario_from_rows(sw.rows_from_bytes(data), days[-1], DIGEST, "sequence_short",
                                         sw.REGISTERED, sw.parse_price)
        calls.append(sw.write_scenario(tmp_path / "scenario.json", sw.scenario_bytes(scenario), None))
    assert calls == [] and not (tmp_path / "scenario.json").is_file()


def test_sequence_tool_distinguishes_input_error(tmp_path: Path) -> None:
    """不经 N5：把短序列场景的 axis 第 2 日改为与第 1 日相同，直接喂给工具 → 退出码 3、输入校验失败。"""
    path, _ = short_scenario_file(tmp_path)
    broken = json.loads(get(path).decode("utf-8"))
    broken["axis"][1] = broken["axis"][0]
    copy = put(tmp_path / "broken.json", json.dumps(broken, ensure_ascii=False).encode("utf-8"))
    done, report = tool_report(copy, tmp_path / "tool_broken.json")
    stop = report.get("stop_reason") or {}
    assert (done.returncode, stop.get("reason")) == (3, "输入校验失败"), (
        f"退出码 {done.returncode}；stop_reason {stop}；标准错误 {done.stderr[-2000:]}")


def test_sequence_write_failure_leaves_no_partial_scenario(tmp_path: Path) -> None:
    days = short_axis()
    scenario = convert(rows_for(days), registration_for(days, "写入中断"))
    target = tmp_path / "scenario.json"
    with pytest.raises(sw.SequenceError, match="场景写入失败"):
        sw.write_scenario(target, sw.scenario_bytes(scenario), 10)
    assert sorted(item.name for item in tmp_path.iterdir()) == []
    sw.write_scenario(target, sw.scenario_bytes(scenario), None)
    with pytest.raises(sw.SequenceError, match="已存在"):
        sw.write_scenario(target, b"{}", None)
    assert json.loads(get(target).decode("utf-8")) == scenario


STAND_IN = "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(30)\n"


def alive(pid: int) -> bool:
    done = run_command(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], Path.cwd(), 30)
    return any(line.startswith('"') and f'"{pid}"' in line for line in done.stdout.splitlines())


def test_sequence_tool_timeout_is_recorded_and_process_terminated(tmp_path: Path) -> None:
    """替身脚本把自身 PID 写入文件后 sleep 30；timeout 1 秒 → TimeoutExpired，记录“未完成（超时）”，不重试；
    随后以 tasklist 断言该 PID 已不存在（最多轮询 5 秒）。"""
    script = put(tmp_path / "stand_in.py", STAND_IN.encode("utf-8"))
    pid_file = tmp_path / "pid.txt"
    record: dict = {}
    attempts = 0
    try:
        attempts += 1
        run_command([sys.executable, str(script), str(pid_file)], tmp_path, 1)
        record["状态"] = "完成"
    except subprocess.TimeoutExpired as expired:
        record = {"状态": "未完成（超时）", "时限秒": expired.timeout}
    assert record["状态"] == "未完成（超时）" and attempts == 1
    pid = int(get(pid_file).decode("utf-8"))
    deadline = time.monotonic() + 5
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not alive(pid), f"超时后替身进程 {pid} 仍存在"
