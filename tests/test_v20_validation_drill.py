"""阶段五 V4：构造演习与接线自检（V4 执行指令定稿第四节第 8 条）。

设置 V20_V4_OUT（演习目录 O）时，演习用例参数化为 v4_det、v4_2、v4_3、v4_4、v4_5a—v4_5d、v4_6 与
v4_prepare_frame；未设置时参数化为空，不触碰任何外部路径（照 V3 test_v3_confirm_case 的做法）。
演习只用构造数据：构造根中的行情为 V3 场景生成的构造价格；构造根、记录与框架演习文件都写在 O 下，原样保留。
接线自检不设变量也运行，只用内存，不运行入口。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import v20_compare_support as support

V4_ENV = "V20_V4_OUT"
V4_CASES = list(support.V4_CASES) if os.environ.get(V4_ENV) else []
# 演习用例运行时须未设置的比对变量（第四节第 8 条）。
V4_UNSET = ("V20_COMPARE_OUT", "V20_COMPARE_B", "V20_COMPARE_ONLY", "V20_COMPARE_FROM", "V20_COMPARE_A2",
            "V20_COMPARE_TOOL_FROM", "V20_V3_OUT")


@pytest.mark.parametrize("name", V4_CASES, ids=V4_CASES)
def test_v4_drill_case(name: str) -> None:
    """一个演习项：建构造根 → 以演习包装脚本运行入口（子进程，限时 3600 秒）→ 只读核对 → 验收记录写入 O\\records。"""
    value = os.environ.get(V4_ENV)
    if not value:
        pytest.skip(f"未设置 {V4_ENV}：V4 演习不运行")
    for variable in V4_UNSET:
        assert variable not in os.environ, f"V4 演习中 {variable} 必须未设置"
    verdict = support.v4_drill(Path(value), name)
    failed = [key for key, passed in verdict["conditions"].items() if not passed]
    assert verdict["passed"] and not failed, f"{name} 验收不成立：{failed}"


# ---------------------------------------------------------------------------
# 接线自检（只用内存；不运行入口）
# ---------------------------------------------------------------------------


def test_wiring_v4_drill_script_is_the_registered_text() -> None:
    """演习包装脚本：1425 字节、SHA-256 为执行指令所列值、全文 ASCII；不含 stable_files、resamples、PERIODS、
    dataclasses（不替换稳定文件写出、不改任何登记值）。"""
    data = support.V4_DRILL_SCRIPT.encode("utf-8")
    assert len(data) == 1425 and data.isascii()
    assert hashlib.sha256(data).hexdigest() == support.V4_DRILL_SHA256 == (
        "cd787d8b020f42ab10d90cd5645d02131974ee74462fcd473f7c5b4fdafd597e")
    for word in support.V4_FORBIDDEN_WORDS:
        assert word not in support.V4_DRILL_SCRIPT, word
    assert support.V4_FORBIDDEN_WORDS == ("stable_files", "resamples", "PERIODS", "dataclasses")
    assert 'validation_run.main(["--root", str(ROOT)])' in support.V4_DRILL_SCRIPT


def test_wiring_v4_file_partition() -> None:
    """文件划分：稳定文件 17 个、环境与审计文件 2 个、清单 1 个，合计等于 V2 的 EXPECTED_FILES。"""
    stable, environment = set(support.V4_STABLE_FILES), set(support.V4_ENV_FILES)
    assert len(support.V4_STABLE_FILES) == len(stable) == 17 and environment == {"run_environment.json",
                                                                                   "audit_log.jsonl"}
    assert not stable & environment and support.V4_MANIFEST not in stable | environment
    assert stable | environment | {support.V4_MANIFEST} == support.v2_run.EXPECTED_FILES


BASE = "D:\\drill\\v20_V4_drill_1"
STAMPS = ("20261018T010203Z_6fdd37b", "20261018T020304Z_6fdd37b")


def contexts() -> tuple[support.V4Context, support.V4Context]:
    return tuple(support.V4Context(f"{BASE}\\roots\\{name}", f"{BASE}\\pycache\\{name}", f"{BASE}\\tmp\\{name}",
                                   stamp) for name, stamp in zip(("v4_1a", "v4_1b"), STAMPS, strict=True))


def research(context: support.V4Context) -> str:
    return f"{context.root}\\reports\\research\\wavewarn_v20"


def environment(context: support.V4Context, started: str, seconds: float) -> dict:
    return {"argv": ["-c", context.root, "const", "m" * 64, "s" * 64, "1234"], "executable": "C:\\py\\python.exe",
            "acquired_at": started, "started_at": started, "ended_at": started, "seconds": seconds, "host": "H",
            "root": context.root, "mode": "validation",
            "staging": f"{research(context)}\\.staging_evaluation_validation_{context.stamp}",
            "formal": f"{research(context)}\\evaluation_validation"}


def audit(context: support.V4Context) -> list[dict]:
    rows = [context.root.replace("\\", "/") + "/config/wavewarn_v20.yaml",
            f"{context.pycache}\\market_risk\\x.cpython-312.pyc", f"{context.tmp}\\scratch",
            f"{research(context)}\\.staging_evaluation_validation_{context.stamp}\\run_record.json",
            "config\\data_decisions.yaml", "C:\\py\\Lib\\json\\__init__.py"]
    lines = [{"record": "open", "seq": number, "path": path, "mode": "r", "flags": 0, "violation": None}
             for number, path in enumerate(rows, start=1)]
    return [*lines, {"record": "coverage", "start": "hook_installed", "first_seq": 1, "last_seq": len(rows),
                     "violations_in_segment": 0}]


def to_bytes(rows: list[dict]) -> bytes:
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode("utf-8")


def test_wiring_v4_det_comparison_only_allows_the_registered_positions() -> None:
    """第 7 条：只有允许位置不同时判相同；任一其他字段不同时判不同；相似前缀的目录不替换、判不同；
    非 path 字段中出现根路径时不替换、判不同（N205、N209）。"""
    a, b = contexts()
    pair = (a, b)
    left_env, right_env = environment(a, "2026-10-18T01:02:03", 61.5), environment(b, "2026-10-18T02:03:04", 59.0)
    assert support.v4_compare_environment(json.dumps(left_env).encode(), json.dumps(right_env).encode(), pair)["same"]
    assert support.v4_compare_audit(to_bytes(audit(a)), to_bytes(audit(b)), pair)["same"]
    # 其他键不同 → 不同。
    changed = {**right_env, "host": "OTHER"}
    result = support.v4_compare_environment(json.dumps(left_env).encode(), json.dumps(changed).encode(), pair)
    assert not result["same"] and result["unexplained"] == ["host"]
    rows = audit(b)
    rows[0] = {**rows[0], "mode": "rb"}
    assert support.v4_compare_audit(to_bytes(audit(a)), to_bytes(rows), pair)["unexplained"] == [1]
    # 相似前缀（根为 …\v4_1a，值为 …\v4_1a_x\f）不替换 → 不同。
    left_rows, right_rows = audit(a), audit(b)
    left_rows[5] = {**left_rows[5], "path": f"{a.root}_x\\f"}
    right_rows[5] = {**right_rows[5], "path": f"{b.root}_x\\f"}
    assert support.v4_replace_path(f"{a.root}_x\\f", a) == f"{a.root}_x\\f"
    assert not support.v4_compare_audit(to_bytes(left_rows), to_bytes(right_rows), pair)["same"]
    # 非 path 字段中出现根路径时不替换 → 不同。
    left_rows, right_rows = audit(a), audit(b)
    left_rows[4] = {**left_rows[4], "violation": a.root}
    right_rows[4] = {**right_rows[4], "violation": b.root}
    assert not support.v4_compare_audit(to_bytes(left_rows), to_bytes(right_rows), pair)["same"]
    left_env, right_env = {**left_env, "executable": f"{a.root}\\python.exe"}, {**right_env,
                                                                                "executable": f"{b.root}\\python.exe"}
    result = support.v4_compare_environment(json.dumps(left_env).encode(), json.dumps(right_env).encode(), pair)
    assert not result["same"] and result["unexplained"] == ["executable"]
    # 时间戳只在研究目录下的四种路径段中替换，且须等于本次时间戳。
    assert support.v4_replace_path(f"{research(a)}\\evaluation_validation_failed_{a.stamp}\\x", a) == (
        "<ROOT>\\reports\\research\\wavewarn_v20\\evaluation_validation_failed_<STAMP>\\x")
    assert support.v4_replace_path(f"{research(a)}\\evaluation_validation_failed_{b.stamp}", a).endswith(b.stamp)
    assert support.v4_replace_path(f"{a.root}\\other_{a.stamp}", a) == f"<ROOT>\\other_{a.stamp}"
    assert support.v4_stamp({"staging": f"{research(a)}\\.staging_evaluation_validation_{a.stamp}"}) == a.stamp
    assert support.v4_stamp({"staging": None}) is None


def test_wiring_v4_manifest_verdict_checks_the_listed_set() -> None:
    """B88：所列集合与逐项相符分开判定；漏列一项而其余逐项正确时集合判假；多列不存在的文件时两项都判假；
    字节数不符时逐项判假。"""
    files: dict[str, bytes | None] = {"a.json": b"A", "b.json": b"BB", "c.json": b"CCC"}
    entry = {name: (hashlib.sha256(data).hexdigest(), len(data)) for name, data in files.items() if data is not None}
    full = support.v4_manifest_verdict(entry, files, set(files))
    assert full["listed_exact"] and full["match"]
    short = support.v4_manifest_verdict({k: v for k, v in entry.items() if k != "c.json"}, files, set(files))
    assert not short["listed_exact"] and short["match"]
    extra = support.v4_manifest_verdict({**entry, "d.json": entry["a.json"]}, {**files, "d.json": None}, set(files))
    assert not extra["listed_exact"] and not extra["match"]
    wrong = support.v4_manifest_verdict({**entry, "a.json": (entry["a.json"][0], 2)}, files, set(files))
    assert wrong["listed_exact"] and not wrong["match"]
    empty = support.v4_manifest_verdict({}, files, set(files))
    assert not empty["listed_exact"]


def test_wiring_v4_open_events_require_valid_structure() -> None:
    """B89：缺字段、类型错误、空列表、项结构错误时三项中“无行情目录访问”判假；行情目录事件判假；
    结构有效且无行情目录访问时三项都判真。行情目录路径按段拼接（照 D2 v4_data_market_event）。"""
    root = Path(BASE) / "roots" / "v4_5a"
    key = "open_events 无构造根行情目录下的事件"
    good = {"open_events": [{"record": "open", "seq": 1, "path": str(root / "config" / "wavewarn_v20.yaml")}]}
    assert all(support.v4_open_events_checks(root, good).values())
    market_path = "\\".join((str(root), *("data", "market"), "SPX.csv"))
    market = support.v4_open_events_checks(root, {"open_events": [{"path": market_path}]})
    assert market["open_events 每项为对象且 path 为非空字符串"] and not market[key]
    for bad in ({}, {"open_events": None}, {"open_events": "x"}, {"open_events": []}, {"open_events": ["p"]},
                {"open_events": [{"seq": 1}]}, {"open_events": [{"path": ""}]}, {"open_events": [{"path": 3}]}):
        assert not support.v4_open_events_checks(root, bad)[key], bad
