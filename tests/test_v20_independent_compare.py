"""v2.0 构造比对接线（A，乙方案范围）：独立复核工具与项目实现的逐层比对。

依据：定稿二第五节第 6 部分；《接线会话指令：构造比对接线（A）定稿》、《补充三条》与《乙方案范围调整》（以后者为准）。
- 设置环境变量 V20_COMPARE_OUT（比对输出根目录）才运行，否则跳过，且不读取任何外部材料（乙补修第一节第 1 条）；
  V20_COMPARE_ONLY=<场景名,…> 只用于试跑。
- 重比较（乙补修第三节）：再设 V20_COMPARE_FROM=<第二次运行的比对输出目录>，只读取其中已保存、且与其
  导出/清单.md 哈希一致的工具输出、项目字段与入口记录，只重新执行字段比对；不运行工具子进程，不运行项目算法。
- 每个场景：核对场景与工具的 SHA-256 → 子进程运行工具 → 本进程用项目模块计算 → 逐层逐字段比对 → 写出记录。
- 子进程中的访问不在外层拦截范围内；这些测试照实标“通过但覆盖不完整”，不据此宣称子进程零真实数据访问。
- 出现“不一致”时测试失败（保存证据后停下报告），不通过修改任一实现消除差异。
"""

from __future__ import annotations

import datetime as dt
import functools
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import v20_compare_support as support

TOOL_ROOT = Path(r"C:\Users\stone\v20_tool_a161387")
TOOL_COMMIT = "a161387fb13954eaa117ba3970e425bb98d244d5"          # 被测代码 2276f1bc79dc1b37d3304da79728f7be07c29539
AUDIT = TOOL_ROOT / "docs" / "audit" / "独立复核" / "v20" / "audit_v20.py"
AUDIT_SHA256 = "40abfb78281a4af443618ed384415ae001169720f3d6b256a80add9af27a74c3"
ORIGINAL_MANIFEST = Path(r"C:\Users\stone\Downloads\v20_独立工具导出\manifest.json")
ORIGINAL_MANIFEST_SHA256 = "93ccc01cd1681d953b099e7fad8e2b47a1a882e666440a60c31564c86b520119"
ADDED_MANIFEST = Path(r"C:\Users\stone\Downloads\v20_构造验收_2276f1b\manifest_新增.json")
ADDED_MANIFEST_SHA256 = "480fc6a6866b29c007521aef966bde99cd385504c463db213027e813c00a8477"
ADDED_NAMES = ("整行缺失", "整行缺失_含重复日期", "整行缺失_含多余日期")       # 新增场景中进入互比的 3 个（只做输入层）
CONSTRUCT_ROOT = Path(r"D:\temp_claude\v20\构造输入")     # 仓库外的专用临时目录（负责人 2026-10-03 改到 D 盘）
OUT_ENV, ONLY_ENV, FROM_ENV = "V20_COMPARE_OUT", "V20_COMPARE_ONLY", "V20_COMPARE_FROM"
# 第二轮（A2）：重比较时再设 V20_COMPARE_A2=1，接入研究组合层（《构造比对第二轮（A2）》）。
A2_ENV = "V20_COMPARE_A2"
RESEARCH_RUN = Path(support.research_run.__file__)
TRIAL = ("path_000", "path_122", "path_154", "收敛反例", "止损与重入")
HERE = Path(__file__).resolve().parent
WIRING_FILES = (HERE / "test_v20_independent_compare.py", HERE / "v20_compare_support.py",
                HERE / "test_v20_isolation.py")


def cases() -> list[tuple[str, str, Path, str]]:
    """（类别，场景名，场景文件，manifest 中的 SHA-256）。原 238 个中的 212 个 full 与抽样索引；新增 3 个输入层场景。
    确认性检验本轮双方均不运行；符号轴场景本轮不互比。"""
    result = []
    original = support.load_json(ORIGINAL_MANIFEST)
    for item in original["scenarios"]:
        if item["kind"] == "full":
            result.append(("full", item["name"], ORIGINAL_MANIFEST.parent / "scenarios" / item["file"], item["sha256"]))
        elif item["kind"] == "bootstrap":
            result.append(("bootstrap", item["name"], ORIGINAL_MANIFEST.parent / "scenarios" / item["file"],
                           item["sha256"]))
    added = support.load_json(ADDED_MANIFEST)
    for item in added["scenarios"]:
        if item["name"] in ADDED_NAMES:
            result.append(("added", item["name"], ADDED_MANIFEST.parent / "新增场景" / item["file"], item["sha256"]))
    return result


def collect_cases() -> list[tuple[str, str, Path, str]]:
    """未设置 V20_COMPARE_OUT 时参数化为空，不触碰任何外部路径；设置后清单缺失照原逻辑报错（乙补修第一节第 1 条）。"""
    return cases() if os.environ.get(OUT_ENV) else []


CASES = collect_cases()


def output_root() -> Path:
    value = os.environ.get(OUT_ENV)
    if not value:
        pytest.skip(f"未设置 {OUT_ENV}：比对测试不运行")
    return Path(value)


def selected(name: str) -> bool:
    only = os.environ.get(ONLY_ENV)
    return not only or name in {item.strip() for item in only.split(",")}


def saved_source() -> Path | None:
    value = os.environ.get(FROM_ENV)
    return Path(value) if value else None


@functools.lru_cache(maxsize=1)
def saved_listing(source: Path) -> dict[str, tuple[int, str]]:
    """第二次运行的 导出/清单.md 第二节：相对路径 → （字节数，SHA-256）。"""
    lines = support.get_bytes(source / "导出" / "清单.md").decode("utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("## 二"))
    listing = {}
    for line in lines[start:]:
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) == 3 and parts[1].isdigit():
            listing[parts[0]] = (int(parts[1]), parts[2])
    return listing


def saved_bytes(source: Path, relative: str) -> bytes:
    """读取第二次运行保存的原始输出，并核对字节数与 SHA-256 与其清单一致。"""
    data = support.get_bytes(source / relative)
    size, digest = saved_listing(source)[relative]
    assert (len(data), support.sha256(data)) == (size, digest), f"{relative} 与第二次运行的清单不符"
    return data


def saved_relatives(kind: str, name: str) -> list[str]:
    files = [f"工具输出/{name}.json", f"项目字段/{name}.json"]
    return files if kind == "bootstrap" else [*files, f"构造输入/{name}/入口与适配结果.json"]


def run_parameters(kind: str, name: str, scenario: Path, tool_out: Path, source: Path | None = None) -> dict:
    """试跑结果复用的依据（补充三条第三条）：场景、工具、接线源码、配置模板与运行参数的哈希与原文。
    V20_COMPARE_ONLY 只决定运行哪些场景，不影响单个场景的计算，不列入复用依据（另记入运行记录）。
    重比较时另绑定所读取的第二次运行原始输出及其清单的哈希（乙补修第三节第 3 条），且不运行工具。"""
    if source is not None:
        listing = support.get_bytes(source / "导出" / "清单.md")
        return {"kind": kind, "scenario": str(scenario), "scenario_sha256": support.sha256(support.get_bytes(scenario)),
                "audit_sha256": support.sha256(support.get_bytes(AUDIT)), "tool_commit": TOOL_COMMIT,
                "wiring": {path.name: support.sha256(support.get_bytes(path)) for path in WIRING_FILES},
                "mode": "重比较（不运行工具子进程与项目算法）", "recompare_from": str(source),
                "source_listing_sha256": support.sha256(listing),
                "source_files": {relative: saved_listing(source)[relative][1]
                                 for relative in saved_relatives(kind, name)},
                "python": sys.version, "env": {OUT_ENV: os.environ.get(OUT_ENV), FROM_ENV: str(source),
                                               A2_ENV: os.environ.get(A2_ENV)},
                "research_run_sha256": support.sha256(support.get_bytes(RESEARCH_RUN))}
    return {"kind": kind, "scenario": str(scenario), "scenario_sha256": support.sha256(support.get_bytes(scenario)),
            "audit_sha256": support.sha256(support.get_bytes(AUDIT)), "tool_commit": TOOL_COMMIT,
            "wiring": {path.name: support.sha256(support.get_bytes(path)) for path in WIRING_FILES},
            "decisions_template_sha256": support.sha256(support.DECISIONS_TEXT.encode("utf-8")),
            "tool_command": [sys.executable, str(AUDIT), str(scenario), str(tool_out)],
            "python": sys.version, "env": {OUT_ENV: os.environ.get(OUT_ENV), "PYTHONIOENCODING": "utf-8",
                                           "PYTHONDONTWRITEBYTECODE": "1"}}


def run_tool(scenario: Path, tool_out: Path) -> tuple[int, float, str]:
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    started = time.perf_counter()
    done = subprocess.run([sys.executable, str(AUDIT), str(scenario), str(tool_out)], cwd=TOOL_ROOT,
                          env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=False)
    return done.returncode, time.perf_counter() - started, done.stderr[-4000:]


@pytest.mark.parametrize(("kind", "name", "scenario", "expected"), CASES, ids=[case[1] for case in CASES])
def test_independent_compare(kind: str, name: str, scenario: Path, expected: str) -> None:
    out = output_root()
    if not selected(name):
        pytest.skip(f"{ONLY_ENV} 未选中")
    assert support.sha256(support.get_bytes(ORIGINAL_MANIFEST)) == ORIGINAL_MANIFEST_SHA256, (
        "原 238 个场景的 manifest 哈希不符")
    assert support.sha256(support.get_bytes(ADDED_MANIFEST)) == ADDED_MANIFEST_SHA256, "新增场景的 manifest 哈希不符"
    assert support.sha256(support.get_bytes(AUDIT)) == AUDIT_SHA256, "audit_v20.py 哈希不符"
    assert support.sha256(support.get_bytes(scenario)) == expected, f"{name} 场景文件哈希与 manifest 不符"
    source = saved_source()
    if source is not None:
        recompare_case(out, source, kind, name, scenario)
        return
    tool_out = out / "工具输出" / f"{name}.json"
    record_path = out / "逐场景比对" / f"{name}.json"
    parameters = run_parameters(kind, name, scenario, tool_out)
    if record_path.is_file() and tool_out.is_file():
        previous = support.load_json(record_path)
        if previous.get("parameters") == parameters and previous.get("tool_output_sha256") == support.sha256(
                support.get_bytes(tool_out)):
            assert previous["counts"].get("不一致", 0) == 0, f"{name} 先前的比对有不一致"
            return                                                     # 试跑结果计入全量（参数与哈希逐项相同）
    tool_out.parent.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    exit_code, tool_seconds, stderr = run_tool(scenario, tool_out)
    assert exit_code in (0, 3), f"{name} 工具退出码 {exit_code}：{stderr}"
    tool = support.load_json(tool_out)
    data = support.load_json(scenario)
    project_started = time.perf_counter()
    record = support.Recorder()
    project_fields: dict = {}
    entry_info: dict = {}
    if kind == "bootstrap":
        project_fields = {"items": support.project_bootstrap(data["items"])}
        support.compare_bootstrap(record, tool, project_fields["items"])
    else:
        cutoff_text = support.scenario_cutoff(data)                  # 只对有日期轴的场景取截止日
        root = CONSTRUCT_ROOT / name
        constructed = support.construct(data, root)
        cutoff = dt.date.fromisoformat(cutoff_text) if cutoff_text.count("-") == 2 else None
        assert_decision_dates(data, cutoff)
        entry = support.csv_entry(constructed, cutoff)
        adaptation = support.adapt(data, constructed, cutoff_text)
        entry_info = {"CSV 入口实际结果": {"class": entry.stop_class, "reason": entry.stop_reason,
                                         "message": entry.message, "decision_hits": dict(entry.decision_hits)},
                      "适配路径验证": dict(adaptation.checks), "适配路径缺价日": dict(adaptation.missing),
                      "构造文件哈希": dict(constructed.hashes)}
        for relative in constructed.hashes:
            support.put_bytes(out / "构造输入" / name / relative, support.get_bytes(root / relative))
        if entry.snapshot is not None:
            assert adaptation.snapshot is not None, f"{name} CSV 入口成功而适配路径停止"
            assert support.snapshot_fields(entry.snapshot) == support.snapshot_fields(adaptation.snapshot), (
                f"{name} 无缺价场景的 S₁ 与 S₂ 不一致")
            entry_info["S₁ 与 S₂"] = "逐字段相同"
        evidence = (support.axis_evidence(*support.constructed_axis_inputs(root), cutoff)
                    if needs_axis_evidence(tool) else None)
        whole_row = support.compare_input_layer(record, tool, support.input_facts(entry, adaptation), cutoff_text,
                                                evidence)
        if whole_row is not None:
            entry_info["整行缺失三项核对"] = whole_row
        if kind == "full" and adaptation.snapshot is not None:
            project_fields = support.project_full(adaptation.snapshot)
            support.compare_full(record, tool, project_fields)
        else:
            note = "未比较（输入层停止）" if kind == "full" else "只做输入层比对"
            record.add("算法层", "全部", "未比较", name, note=note)
    project_seconds = time.perf_counter() - project_started
    support.put_bytes(out / "项目字段" / f"{name}.json", support.dump_json(project_fields))
    support.put_bytes(out / "构造输入" / name / "入口与适配结果.json", support.dump_json(entry_info))
    counts = {status: record.total(status) for status in support.STATUSES}
    counts["窗口末日边界"] = record.window_end()                    # 记为“未比较”，另行计数
    result = {"name": name, "kind": kind, "parameters": parameters, "started": started,
              "tool_exit_code": exit_code, "tool_seconds": round(tool_seconds, 3),
              "project_seconds": round(project_seconds, 3),
              "tool_output_sha256": support.sha256(support.get_bytes(tool_out)), "counts": counts,
              "by_layer": {layer: items for layer, items in record.summary().items()}, "details": record.details,
              "alignment": project_fields.get("alignment") if isinstance(project_fields, dict) else None}
    support.put_bytes(record_path, support.dump_json(result))
    assert counts["不一致"] == 0, f"{name} 有 {counts['不一致']} 处不一致，见 {record_path}"


def needs_axis_evidence(tool: dict) -> bool:
    """工具给出整行缺失（input_checks.added_dates 非空）时，需要项目入口层的交易日轴证据（乙补修二第一节）。"""
    return bool((tool.get("input_checks") or {}).get("added_dates"))


def assert_decision_dates(data: dict, cutoff: dt.date | None) -> None:
    assert cutoff is not None and not set(support.DECISION_DATES) & {
        dt.date.fromisoformat(day) for day in data["axis"] if len(day) == 10}, "固定裁定条目的日期落在日期轴上"


def recompare_case(out: Path, source: Path, kind: str, name: str, scenario: Path) -> None:
    """重比较一个场景（乙补修第三节）：只读取第二次运行保存且哈希与其清单一致的工具输出、项目字段与入口记录，
    只重新执行字段比对。不运行工具子进程，不运行项目算法，不复制原始输出（只在记录中绑定其哈希）。"""
    record_path = out / "逐场景比对" / f"{name}.json"
    parameters = run_parameters(kind, name, scenario, out / "工具输出" / f"{name}.json", source)
    if record_path.is_file():
        previous = support.load_json(record_path)
        if previous.get("parameters") == parameters:
            assert previous["counts"].get("不一致", 0) == 0, f"{name} 先前的比对有不一致"
            return                                                     # 参数与哈希逐项相同
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tool_bytes = saved_bytes(source, f"工具输出/{name}.json")
    tool = support.parse_json(tool_bytes)
    project_fields = support.parse_json(saved_bytes(source, f"项目字段/{name}.json"))
    data = support.load_json(scenario)
    record = support.Recorder()
    extra: dict = {}
    if kind == "bootstrap":
        support.compare_bootstrap(record, tool, project_fields["items"])
    else:
        cutoff_text = support.scenario_cutoff(data)
        assert_decision_dates(data, dt.date.fromisoformat(cutoff_text) if cutoff_text.count("-") == 2 else None)
        entry_info = support.parse_json(saved_bytes(source, f"构造输入/{name}/入口与适配结果.json"))
        facts = support.saved_input_facts(data, entry_info, cutoff_text)
        if facts.stop_class is None:
            assert entry_info.get("S₁ 与 S₂") == "逐字段相同", f"{name} 第二次运行未记录 S₁ 与 S₂ 相同"
        evidence = None
        if needs_axis_evidence(tool):
            # 乙补修二第一节：先按 _2 清单核对构造 CSV 与配置的哈希，再用项目入口层函数对其日期列表核对交易日轴。
            for relative in entry_info["构造文件哈希"]:
                saved_bytes(source, f"构造输入/{name}/{relative}")
                extra[f"构造输入/{name}/{relative}"] = saved_listing(source)[f"构造输入/{name}/{relative}"][1]
            evidence = support.axis_evidence(*support.constructed_axis_inputs(source / "构造输入" / name),
                                             dt.date.fromisoformat(cutoff_text))
        whole_row = support.compare_input_layer(record, tool, facts, cutoff_text, evidence)
        if whole_row is not None:
            extra["整行缺失三项核对"] = whole_row
        extra["入口与适配结果"] = {"CSV 入口实际结果": entry_info["CSV 入口实际结果"],
                                "适配路径验证": entry_info["适配路径验证"]}
        if kind == "full" and facts.snapshot_built:
            support.compare_full(record, tool, project_fields)
            if os.environ.get(A2_ENV):
                extra["第二轮"] = research_round(record, out, name, data, cutoff_text, entry_info, tool,
                                                 project_fields)
        else:
            note = "未比较（输入层停止）" if kind == "full" else "只做输入层比对"
            record.add("算法层", "全部", "未比较", name, note=note)
    counts = {status: record.total(status) for status in support.STATUSES}
    counts["窗口末日边界"] = record.window_end()
    research = extra.get("第二轮") or {}
    result = {"name": name, "kind": kind, "parameters": parameters, "started": started,
              "tool_exit_code": tool.get("exit_code"), "tool_seconds": None,
              "project_seconds": research.get("组合层耗时（秒）"),
              "tool_output_sha256": support.sha256(tool_bytes), "counts": counts,
              "by_layer": {layer: items for layer, items in record.summary().items()}, "details": record.details,
              "alignment": project_fields.get("alignment") if isinstance(project_fields, dict) else None,
              "evidence": extra}
    support.put_bytes(record_path, support.dump_json(result))
    support.assert_self_consistent(research.get("自洽检查") or {}, name)
    stop = research.get("stop") or {}
    # 《A2 修订二》第六节：run_window 记为未预期异常（或计算失败）即停，记录异常类型、消息与场景，不自行断定原因。
    assert stop.get("exit") not in ("未预期异常", "计算失败"), f"{name} run_window 停止：{stop}"
    assert counts["不一致"] == 0, f"{name} 有 {counts['不一致']} 处不一致，见 {record_path}"


def research_round(record: support.Recorder, out: Path, name: str, data: dict, cutoff_text: str, entry_info: dict,
                   tool: dict, project_fields: dict) -> dict:
    """第二轮：沿用构造入口得到快照（写入本轮输出目录，构造文件哈希须与第二次运行相同），开发期 27 组、诊断开启
    运行一次组合层；先做自洽检查，自洽无“不同”才比对新增层（《A2 修订二》第二节；《A2 补充二》第四节）。
    构造历史起点按裁决 2 收紧版；修正起点的场景另以原起点运行一次，只用于披露两套起点的差异。"""
    root = out / "构造输入" / name
    constructed = support.construct(data, root)
    assert dict(constructed.hashes) == dict(entry_info["构造文件哈希"]), f"{name} 构造文件与第二次运行不同"
    adaptation = support.adapt(data, constructed, cutoff_text)
    assert adaptation.snapshot is not None, f"{name} 快照适配路径未构造出快照"
    configured = support.research_histories(root)
    histories, start_table = support.construct_histories(data, configured)
    started = time.perf_counter()
    result, chosen = support.run_research(adaptation.snapshot, histories)
    seconds = round(time.perf_counter() - started, 3)
    axis = adaptation.snapshot.days
    stop = None if result.stop is None else {"exit": result.stop.exit.value,
                                             "exception_type": result.stop.exception_type,
                                             "reason_code": result.stop.reason_code,
                                             "message": result.stop.message, "detail": dict(result.stop.detail)}
    checks = support.self_consistency(result, project_fields, axis)
    compared = not any(support.self_state(check) == support.SELF_DIFFERENT for check in checks.values())
    if compared:
        support.compare_research(record, tool, result, chosen, axis, len(axis) - 1)
    disclosure = None
    if histories != configured:
        original, original_choice = support.run_research(adaptation.snapshot, configured)
        disclosure = support.start_disclosure(result, chosen, original, original_choice)
    return {"组合层耗时（秒）": seconds,
            "构造登记首日": {asset: str(day) for asset, day in histories.items()},
            "配置 first_date": {asset: str(day) for asset, day in configured.items()},
            "构造登记首日与配置": {asset: "相同" if histories[asset] == configured[asset] else "不同"
                              for asset in histories},
            "起点依据": start_table, "两套起点差异": disclosure, "stop": stop,
            "selection": None if chosen is None else {
                "outcome": chosen.selection.outcome.value,
                "selected": (None if chosen.selection.selected is None
                             else support.group_key(chosen.selection.selected))},
            "自洽检查": checks, "已与工具比对": compared}


def test_summary() -> None:
    """汇总：按层的一致、不一致、未比较、接口差异计数，以及窗口末日边界条数（只汇总已有的逐场景记录）。"""
    out = output_root()
    folder = out / "逐场景比对"
    files = sorted(folder.iterdir()) if folder.is_dir() else []
    totals: dict = {}
    by_layer: dict = {}
    rows = []
    for path in files:
        item = support.load_json(path)
        rows.append(item)
        for status, count in item["counts"].items():
            totals[status] = totals.get(status, 0) + count
        for layer, fields in item["by_layer"].items():
            for status_counts in fields.values():
                for status, count in status_counts.items():
                    by_layer.setdefault(layer, {}).setdefault(status, 0)
                    by_layer[layer][status] += count
    lines = ["# 构造比对汇总（列明范围内的构造比对结果，不是完整工程验收或算法资格通过）", "",
             f"- 场景记录数：{len(rows)}", f"- 合计：{totals}", "", "| 层 | " + " | ".join(support.STATUSES) + " |",
             "| --- |" + " --- |" * len(support.STATUSES)]
    for layer in sorted(by_layer):
        lines.append(f"| {layer} | " + " | ".join(str(by_layer[layer].get(status, 0)) for status in support.STATUSES)
                     + " |")
    lines += ["",
              "| 场景 | 类别 | 工具退出码 | 工具耗时（秒） | 项目耗时（秒） | 不一致 | 未比较 | 接口差异 "
              "| 窗口末日边界 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in rows:
        counts = item["counts"]
        lines.append(f"| {item['name']} | {item['kind']} | {item['tool_exit_code']} | {item['tool_seconds']} | "
                     f"{item['project_seconds']} | {counts.get('不一致', 0)} | {counts.get('未比较', 0)} | "
                     f"{counts.get('接口差异', 0)} | {counts.get('窗口末日边界', 0)} |")
    support.put_bytes(out / "汇总.md", ("\n".join(lines) + "\n").encode("utf-8"))
    assert totals.get("不一致", 0) == 0


# ---------------------------------------------------------------------------
# 接线自检（乙补修第二节；乙补修二第四节）：未设置环境变量时照常运行。其中
# test_wiring_cases_skip_without_output_env 使用临时目录中不存在的路径验证读取门控与文件缺失异常；
# 其余用例使用内存构造记录，不读文件。
# ---------------------------------------------------------------------------

DAYS = ("2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07")    # 构造的执行日轴：m = d = 第 3 日，d⁺ = 第 4 日


def built_exec_sim() -> tuple[dict, dict, list]:
    """构造一组自洽的执行政策研究模拟记录：第 3 日缺价（m），持仓中止损无法确定（d），第 4 日起目标无法确定。"""
    def plan(day: str) -> dict:
        return {"exec_idx": day, "exposure": "1", "core_w": "1", "lev_w": "0", "cap_in_force": False,
                "cap_binding": False, "source": "系统"}

    common = {"S": "正常", "all_valid": True, "signal_target": "1"}
    tool = {"days": [
        {"idx": DAYS[0], **common, "determined": True, "W": 1.0, "held_exposure": "1", "next_target": plan(DAYS[1])},
        {"idx": DAYS[1], **common, "determined": True, "W": 1.01, "U": 0.01, "R": 0.01, "held_exposure": "1",
         "next_target": plan(DAYS[2])},
        {"idx": DAYS[2], **common, "determined": False, "W": None, "U": None, "R": None, "held_exposure": "1",
         "stop_check": "无法确定"},
        {"idx": DAYS[3], **common, "determined": False}],
        "failed": {"type": "缺价", "missing": [["SPX", DAYS[2]]]}, "undetermined_from": DAYS[2], "switches": 0,
        "switches_determined_through": DAYS[2], "switch_list": [], "summary": None, "events": []}
    target = {"exposure": 1.0, "core_w": 1.0, "lev_w": 0.0, "source": "系统", "cap_active": False,
              "reentry_cap": False}
    mine = {"complete": False, "targets": [{"exec_idx": day, **target} for day in DAYS[:3]],
            "days": [{"idx": DAYS[0], "W": 1.0}, {"idx": DAYS[1], "W": 1.01, "U": 0.01, "R": 0.01}],
            "undetermined": {"day": DAYS[3], "reason": "持仓期间当日净值不可得，止损是否触发无法判断"},
            "missing": [["SPX", DAYS[2]]], "switches": 0, "switch_list": [], "failed": None}
    signals = [{"idx": day, "S": "正常", "all_valid": True, "signal_target": 1.0} for day in DAYS]
    return tool, mine, signals


def exec_counts(tool: dict, mine: dict, signals: list) -> support.Recorder:
    record = support.Recorder()
    support.compare_exec_sim(record, "构造组", tool, mine, signals)
    return record


def item_counts(record: support.Recorder, item: str) -> dict:
    return record.summary()["候选执行政策研究模拟"].get(item, {})


def test_wiring_cases_skip_without_output_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """无环境变量且外部清单不存在：参数化为空、不读取清单；有环境变量而清单缺失时照原逻辑报错。"""
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "ORIGINAL_MANIFEST", tmp_path / "不存在" / "manifest.json")
    monkeypatch.setattr(module, "ADDED_MANIFEST", tmp_path / "不存在" / "manifest_新增.json")
    monkeypatch.delenv(OUT_ENV, raising=False)
    assert collect_cases() == []
    monkeypatch.setenv(OUT_ENV, str(tmp_path))
    with pytest.raises(FileNotFoundError):
        collect_cases()


def test_wiring_exec_sim_baseline_is_consistent() -> None:
    """对照：自洽的构造记录没有不一致，原因映射、d⁺ 起无法确定与 m 起 W 不可计算各自计数。"""
    record = exec_counts(*built_exec_sim())
    assert record.total("不一致") == 0, record.inconsistent()
    assert item_counts(record, support.UNDETERMINED_ITEM) == {"一致": 1}
    assert item_counts(record, "d⁺ 起无法确定") == {"一致": 1}
    assert item_counts(record, support.GAP_ITEM) == {"一致": 2}


def test_wiring_wealth_recovered_after_gap_is_inconsistent() -> None:
    """目标未知后工具 W 被错误恢复：判为不一致。"""
    tool, mine, signals = built_exec_sim()
    tool["days"][3]["W"] = 1.02
    record = exec_counts(tool, mine, signals)
    assert item_counts(record, support.GAP_ITEM) == {"一致": 1, "不一致": 1}
    assert item_counts(record, "d⁺ 起无法确定") == {"一致": 1}


def test_wiring_unmapped_reason_is_not_consistent() -> None:
    """停止原因不在映射表中：不判一致（记未比较）；项目原因与映射值不同：判不一致。"""
    tool, mine, signals = built_exec_sim()
    tool["days"][2]["stop_check"] = "未列入映射表的原因"
    assert item_counts(exec_counts(tool, mine, signals), support.UNDETERMINED_ITEM) == {"未比较": 1}
    tool, mine, signals = built_exec_sim()
    mine["undetermined"]["reason"] = "无法确定"
    assert item_counts(exec_counts(tool, mine, signals), support.UNDETERMINED_ITEM) == {"不一致": 1}


def built_axis_evidence(missing: dict[str, tuple[str, ...]]) -> support.AxisEvidence:
    """内存构造的项目侧证据：派生轴为三个构造日期；有缺失的资产记 check_trading_axis 抛“缺少必需价格”。"""
    entry = {asset: (("MissingPriceEntryError", "缺少必需价格") if days else (None, None))
             for asset, days in missing.items()}
    return support.AxisEvidence(missing, dict.fromkeys(missing, DAYS[:3]), entry)


def test_wiring_whole_row_missing_three_items() -> None:
    """乙补修二第四节：工具少列 QQQ → 不一致；派生轴差一日 → 不一致；三项相等 → 一致。"""
    evidence = built_axis_evidence({"SPX": (DAYS[1],), "QQQ": (DAYS[1],)})
    both = [{"date": DAYS[1], "assets": ["SPX", "QQQ"], "reason": "预期交易日整行缺失"}]
    record = support.Recorder()
    support.compare_whole_row_missing(record, both, list(DAYS[:3]), evidence)
    assert record.summary()["输入层"][support.WHOLE_ROW_ITEM] == {"一致": 1}
    spx_only = [{"date": DAYS[1], "assets": ["SPX"], "reason": "预期交易日整行缺失"}]
    record = support.Recorder()
    found = support.compare_whole_row_missing(record, spx_only, list(DAYS[:3]), evidence)
    assert record.summary()["输入层"][support.WHOLE_ROW_ITEM] == {"不一致": 1}
    assert not found["各项"]["1 实际缺失日期集合（按资产）"]["相等"] and not found["各项"]["2 涉及资产集合"]["相等"]
    record = support.Recorder()
    found = support.compare_whole_row_missing(record, both, list(DAYS[:2]), evidence)
    assert record.summary()["输入层"][support.WHOLE_ROW_ITEM] == {"不一致": 1}
    assert found["各项"]["3 派生日期轴"]["相等"] is False


# ---------------------------------------------------------------------------
# 第二轮接线自检（《构造比对第二轮（A2）》第四节第 1 条）：内存构造记录，不读文件。
# ---------------------------------------------------------------------------


def test_wiring_a2_self_consistency_difference_stops() -> None:
    """自洽检查任一“不同”即停；元组与列表经同一序列化后视为相同表示。"""
    support.assert_self_consistent({"t0": support.SELF_SAME}, "构造")
    with pytest.raises(AssertionError, match="自洽检查不同"):
        support.assert_self_consistent({"t0": support.SELF_SAME, "hold": support.SELF_DIFFERENT}, "构造")
    assert support.canonical({"a": (1, 2)}) == support.canonical({"a": [1, 2]})


def test_wiring_a2_r2_category_conversion() -> None:
    """R2 判定：项目 EventClass 的取值文字与工具 category 对应；类别不同判不一致。"""
    from decimal import Decimal

    from market_risk.wavewarn_v20.labels_r2 import R2Event
    from market_risk.wavewarn_v20.r2 import EventClass, EventJudgement, R2Result

    day = dt.date.fromisoformat
    event = R2Event("SPX", day(DAYS[0]), Decimal("100"), day(DAYS[1]), day(DAYS[2]), day(DAYS[3]), Decimal("94"),
                    None, True)
    judgement = EventJudgement(event, EventClass.LATE, None, None, None, False)
    counts = {category: int(category is EventClass.LATE) for category in EventClass}
    mine = R2Result((judgement,), counts, True, 1, 0, 0, False, (0, 1))
    tool_event = {"P": DAYS[0], "category": "迟到", "first_new": None, "first_new_confirmable": False,
                  "exec_idx": None, "offset_vs_T3": None, "peak_new_uncertain": False}
    tool = {"r2": {"SPX": {"events": [tool_event], "counts": {c.value: counts[c] for c in EventClass},
                           "denominator": 1, "passed": 0, "new_only": 0, "computable": True, "ratio_ok": False,
                           "excluding_insufficient": [0, 1]}}}
    record = support.Recorder()
    support.compare_r2_judgements(record, "构造组", tool, {"SPX": mine, "QQQ": None})
    assert record.total("不一致") == 0                                           # QQQ：双方均无判定，记一致
    assert record.summary()["R2 判定"]["events.category"] == {"一致": 1}
    tool_event["category"] = "漏报"
    record = support.Recorder()
    support.compare_r2_judgements(record, "构造组", tool, {"SPX": mine, "QQQ": None})
    assert record.summary()["R2 判定"]["events.category"] == {"不一致": 1}


def test_wiring_a2_selection_outcome_conversion() -> None:
    """选择结果：项目 Outcome 的取值文字与工具 exit 对应；出口不同判不一致。"""
    from decimal import Decimal
    from types import SimpleNamespace

    from market_risk.wavewarn_v20.convergence import Candidate
    from market_risk.wavewarn_v20.selection import CandidateRecord, select
    first, second = Candidate(3, Decimal("0.015"), 1), Candidate(3, Decimal("0.015"), 3)
    records = (CandidateRecord(first, 0, False, True, {"SPX": True, "QQQ": True}, 0.1, 4),
               CandidateRecord(second, 1, False, True, {"SPX": True, "QQQ": True}, 0.2, 5))
    chosen = SimpleNamespace(selection=select(records, False, 1e-10))
    result = SimpleNamespace(records=records)
    keys = [support.group_key(first), support.group_key(second)]
    tool = {"records": [{"key": key, "order": index, "failure": None, "nav_missing": False, "r2_computable": True,
                         "lnW_end": item.log_wealth, "r1_ok": True, "r2_ok": {"SPX": True, "QQQ": True},
                         "switches": item.switches}
                        for index, (key, item) in enumerate(zip(keys, records, strict=True))],
            "selection": {"exit": "选定", "feasible": keys, "M": 0.2, "tie_group": [keys[1]], "selected": keys[1]}}
    record = support.Recorder()
    support.compare_records_and_selection(record, tool, result, chosen)
    assert record.total("不一致") == 0 and record.summary()["候选记录与选择"]["exit ↔ outcome"] == {"一致": 1}
    tool["selection"]["exit"] = "无合格候选"
    record = support.Recorder()
    support.compare_records_and_selection(record, tool, result, chosen)
    assert record.summary()["候选记录与选择"]["exit ↔ outcome"] == {"不一致": 1}


def test_wiring_a2_enumeration_alignment() -> None:
    """诊断枚举路径：按初始状态对齐（两侧顺序可不同）；工具运行 = [初始] + 项目逐日状态前 len − 1 项。"""
    from market_risk.wavewarn_v20.channels import ChannelState

    axis = [dt.date.fromisoformat(day) for day in DAYS]
    active, armed = ChannelState.ACTIVE, ChannelState.ARMED
    runs = (support.research_run.EnumeratedRun(armed, (active, active, active)),
            support.research_run.EnumeratedRun(active, (armed, active, active)))
    mine = support.research_run.EnumeratedConvergence("MR", 1, runs, 1, 2)
    tool_runs = {"激活": ["激活", "已武装未激活", "激活"], "已武装未激活": ["已武装未激活", "激活", "激活"]}
    record = support.Recorder()
    support.compare_enumeration(record, "构造 MR", list(tool_runs.values()), list(tool_runs), DAYS[0], DAYS[2], mine,
                                axis)
    assert record.total("不一致") == 0 and record.total("一致") == 7
    record = support.Recorder()
    support.compare_enumeration(record, "构造 MR", list(tool_runs.values()), list(tool_runs), DAYS[0], DAYS[3], mine,
                                axis)
    assert record.summary()["诊断收敛枚举"]["conv_day ↔ first_common_global"] == {"不一致": 1}


# ---------------------------------------------------------------------------
# 《A2 补充二》第三、四节新增的接线自检（内存构造记录，不读文件）
# ---------------------------------------------------------------------------


def test_wiring_a2_self_consistency_four_states() -> None:
    """四种状态：第一轮缺该组为“无第一轮基准”；缺键不与合法 None 混同；浮点按 1e-12；第一轮多出的叶计“不同”。"""
    from decimal import Decimal

    assert support.compare_group({"a": None}, support.MISSING)["状态"] == support.SELF_NO_BASELINE
    assert support.compare_group({"a": None}, {"a": None})["状态"] == support.SELF_SAME
    assert support.compare_group({"a": None}, {})["状态"] == support.SELF_NO_BASELINE          # 缺键 ≠ None
    assert support.compare_group({"a": None}, {"a": 0})["状态"] == support.SELF_DIFFERENT
    assert support.compare_group({"w": Decimal("1.0000000000001")}, {"w": Decimal("1")})["状态"] == support.SELF_SAME
    assert support.compare_group({"w": Decimal("1.00000000001")}, {"w": Decimal("1")})["状态"] == support.SELF_DIFFERENT
    assert support.compare_group({"a": 1}, {"a": 1, "b": 2})["状态"] == support.SELF_DIFFERENT
    assert support.compare_group({"flag": True}, {"flag": 1})["状态"] == support.SELF_DIFFERENT
    support.assert_self_consistent({"x": {"状态": support.SELF_NO_BASELINE}, "y": {"状态": support.SELF_NO_FIELD}},
                                   "构造")


def test_wiring_a2_stop_reason_three_elements() -> None:
    """停止原因三要素分别映射：出口、原因码按表换算，下标记未比较；表外出口不自动对应为工具的正常停止。"""
    from types import MappingProxyType, SimpleNamespace

    rr = support.research_run
    stop = rr.StopRecord(rr.Exit.EMPTY_WINDOW, "公共", "起点", "EmptyWindowError", "评价窗口为空", "构造",
                         MappingProxyType({"detail": "j₀ 超出日期轴"}), None)
    tool = {"stop_reason": {"reason": "评价窗口为空", "sub_reason": "起点晚于最后一个收盘日", "j0_index": 9,
                            "E_index": 8}}
    record = support.Recorder()
    support.compare_research(record, tool, SimpleNamespace(stop=stop), None, [], 0)
    items = record.summary()["停止原因（组合层）"]
    assert items["exit ↔ stop_reason.reason"] == {"一致": 1}
    assert items["reason_code ↔ stop_reason.reason"] == {"一致": 1}
    assert items["detail ↔ sub_reason"] == {"一致": 1} and items["j0_index、E_index"] == {"未比较": 1}
    failed = rr.StopRecord(rr.Exit.FAILED, "公共", "R1", "NavError", None, "构造", MappingProxyType({}), None)
    record = support.Recorder()
    support.compare_research(record, tool, SimpleNamespace(stop=failed), None, [], 0)
    assert record.summary()["停止原因（组合层）"]["exit ↔ stop_reason.reason"] == {"不一致": 1}


def test_wiring_a2_missing_tool_field_is_not_consistent() -> None:
    """工具缺字段时记未比较而非一致：工具组内没有 ledger、项目有段账 → 未比较。"""
    from market_risk.wavewarn_v20.r2 import SegmentClass, SegmentLedger

    ledger = SegmentLedger((), {category: 0 for category in SegmentClass}, None)
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", {"r2": {}}, {"SPX": ledger, "QQQ": None})
    assert record.summary()["提示段账"]["段账"] == {"未比较": 1} and record.total("一致") == 1   # QQQ 双方均无


def test_wiring_a2_unavailable_combinations() -> None:
    """Unavailable 只映射 (labels_r2.r2_events, 缺少必需价格)；其他组合记“未比较（映射表外）”。"""
    rr = support.research_run
    day = dt.date.fromisoformat(DAYS[0])
    tool = {"r2_events": {}, "r2_events_error": {"SPX": {"reason": "缺少必需价格", "missing_days": [DAYS[0]]},
                                                 "QQQ": {"reason": "缺少必需价格", "missing_days": [DAYS[0]]}}}
    events = {"SPX": rr.Unavailable((("SPX", day),), "缺少必需价格", "MissingPriceError", "labels_r2.r2_events"),
              "QQQ": rr.Unavailable((("QQQ", day),), "缺少必需价格", None, "basket_prefix")}
    record = support.Recorder()
    support.compare_r2_events(record, tool, events)
    items = record.summary()["R2 事件"]
    assert items["不可得（reason_code ↔ r2_events_error.reason）"] == {"一致": 1}
    assert items["不可得（映射表外）"] == {"未比较": 1}


def test_wiring_a2_declared_late_start() -> None:
    """裁决 2 收紧版：只有场景定义声明晚开始、且声明日之前无价格的资产才修正起点；其余维持配置 first_date。"""
    axis = list(DAYS)
    configured = {"SPX": dt.date.fromisoformat(DAYS[0]), "QQQ": dt.date.fromisoformat(DAYS[0])}
    late = {"axis": axis, "prices": {"SPX": ["1", "1", "1", "1"], "QQQ": [None, None, "1", "1"]},
            "meta": {"类型": "QQQ晚开始", "缺价": [{"资产": "QQQ", "起": 0, "长度": 2}]}}
    histories, table = support.construct_histories(late, configured)
    assert histories["QQQ"] == dt.date.fromisoformat(DAYS[2]) and histories["SPX"] == configured["SPX"]
    assert table[0]["声明日之前有价格"] == "无" and table[0]["新起点"] == DAYS[2]
    priced = {**late, "prices": {"SPX": ["1"] * 4, "QQQ": [None, "1", "1", "1"]}}
    histories, table = support.construct_histories(priced, configured)
    assert histories == configured and table[0]["处理"].startswith("维持")
    undeclared = {**late, "meta": {"类型": "单日缺价", "缺价": [{"资产": "QQQ", "起": 0, "长度": 2}]}}
    assert support.construct_histories(undeclared, configured) == (configured, [])


# ---------------------------------------------------------------------------
# 补充三修订二第一节五项接线更正的自检（内存构造记录，不读文件）
# ---------------------------------------------------------------------------


def test_wiring_a3_empty_containers_and_missing_fields() -> None:
    """第 1 项与负责人口径：空容器保留路径与类型；组合层多出的键记“无第一轮基准”；第一轮已有的键在组合层缺失、
    或由非空变为空（字典、列表同样）判“不同”。"""
    same, different, baseline = support.SELF_SAME, support.SELF_DIFFERENT, support.SELF_NO_BASELINE
    assert support.compare_group({"a": []}, {"a": []})["状态"] == same
    assert support.compare_group({"a": {}}, {"a": {}})["状态"] == same
    assert support.compare_group({"a": [1]}, {"a": []})["状态"] == baseline         # 空列表 → 非空：多出的键
    assert support.compare_group({"a": {"x": 1}}, {"a": {}})["状态"] == baseline    # 空字典 → 非空：多出的键
    assert support.compare_group({"a": []}, {"a": [1]})["状态"] == different        # 非空 → 空
    assert support.compare_group({"a": {}}, {"a": {"x": 1}})["状态"] == different   # 非空 → 空
    assert support.compare_group({"a": {}}, {"a": []})["状态"] == different         # 类型不同
    assert support.compare_group({}, {"a": 1})["状态"] == different                 # 第一轮字段在组合层缺失
    assert support.compare_group({"a": None}, {})["状态"] == baseline               # 组合层多出的键


def stop_record(exit_: object, exception: str | None, reason: str | None, detail: dict, stage: str = "起点",
                object_: str = "公共", message: str = "构造") -> object:
    from types import MappingProxyType

    return support.research_run.StopRecord(exit_, object_, stage, exception, reason, message,
                                           MappingProxyType(detail), None)


def test_wiring_a3_stop_mapping_to_first_round() -> None:
    """第 2 项：只按已登记的 STOP_REASONS 三项比较；其余出口记“无第一轮基准”并保留实际值，不猜测对应；
    第一轮明确未停止而组合层以表内出口停止判“不同”。"""
    rr = support.research_run
    empty = stop_record(rr.Exit.EMPTY_WINDOW, "EmptyWindowError", "评价窗口为空", {"detail": "j₀ 超出日期轴"})
    saved = {"class": "EmptyWindowError", "reason": "评价窗口为空", "detail": "j₀ 超出日期轴", "message": "构造"}
    assert {c["状态"] for c in support.compare_stop(empty, saved).values()} == {support.SELF_SAME}
    undetermined = stop_record(rr.Exit.UNDETERMINABLE, "R2Undeterminable", None, {})
    checks = support.compare_stop(undetermined, None)
    assert {c["状态"] for c in checks.values()} == {support.SELF_NO_BASELINE}
    assert checks["stop.exit ↔ 第一轮 stop.reason"]["组合层实际值"] == "分类无法确定"
    assert checks["stop.exception_type ↔ 第一轮 stop.class"]["组合层实际值"] == "R2Undeterminable"
    no_start = stop_record(rr.Exit.NO_START, "NoStartError", "无法确定 t0", {})
    assert support.compare_stop(no_start, None)["stop.exit ↔ 第一轮 stop.reason"]["状态"] == support.SELF_DIFFERENT


def test_wiring_a3_basket_unavailable_combination() -> None:
    """第 3 项：信号净值与一直持有的 Unavailable 只接受 (basket_prefix, 缺少必需价格)；表外组合保留原值。"""
    rr = support.research_run
    day = dt.date.fromisoformat(DAYS[0])
    allowed = rr.Unavailable((("SPX", day),), "缺少必需价格", None, "basket_prefix")
    assert support.nav_or_missing(allowed, ())["failed"] == {"type": "缺价", "missing": [["SPX", DAYS[0]]]}
    outside = rr.Unavailable((("SPX", day),), "缺少必需价格", "MissingPriceError", "labels_r2.r2_events")
    assert support.nav_or_missing(outside, ()) == {"映射表外": {"source": "labels_r2.r2_events",
                                                              "reason_code": "缺少必需价格"}}


def test_wiring_a3_stop_checks_each_produced_group() -> None:
    """第 4 项：逐字段组判断停止前是否已产生；已产生且第一轮有对应的照常比较，未产生的记“停止前无字段”。"""
    from types import SimpleNamespace

    rr = support.research_run
    axis = [dt.date(2004, 1, 1) + dt.timedelta(days=index) for index in range(10)]
    text = [day.isoformat() for day in axis]
    stop = stop_record(rr.Exit.UNDETERMINABLE, "R2Undeterminable", None, {}, "R2", "K=3")
    window = SimpleNamespace(t0=2, kappa_all=3, j0=5, n=4, e_index=9, reference_index=3, convergences={})
    missing = (("SPX", axis[6]),)
    common = SimpleNamespace(prefix=SimpleNamespace(values=(), first_missing=1, missing=missing),
                             hold=rr.Unavailable(missing, "缺少必需价格", None, "basket_prefix"))
    saved = {"stop": None, "t0": text[2], "kappa_all": text[3], "j0": text[5], "j0_index": 5, "n": 4, "E": text[9],
             "E_index": 9, "window_first_signal_day": text[4], "reference": {"conv_day": text[3]},
             "hold": {"failed": {"type": "缺价", "missing": [["SPX", text[6]]]}, "nav": None, "summary": None},
             "groups": {}}
    result = SimpleNamespace(stop=stop, window=window, common=common, reference=None, candidates={})
    checks = support.self_consistency(result, saved, axis)
    assert checks["t0"]["状态"] == support.SELF_SAME and checks["hold"]["状态"] == support.SELF_SAME   # 已产生
    assert checks["reference.days"]["状态"] == support.SELF_NO_FIELD                                   # 未产生
    assert checks["groups 键（已完成候选）"]["状态"] == support.SELF_NO_FIELD
    saved["j0"] = text[6]
    assert support.self_consistency(result, saved, axis)["j0"]["状态"] == support.SELF_DIFFERENT
    no_window = SimpleNamespace(stop=stop, window=None, common=None, reference=None, candidates={})
    assert support.self_consistency(no_window, saved, axis)["t0"]["状态"] == support.SELF_NO_FIELD


def d13_case() -> tuple[dict, object]:
    from types import SimpleNamespace

    rr = support.research_run
    day = dt.date.fromisoformat(DAYS[0])
    stop = stop_record(rr.Exit.UNDETERMINABLE, "R2Undeterminable", None, {}, "R2", "K=3,θ_P=0.025,h=1",
                       "提示段在 2004-02-18 开始，但前一日的状态无法确定，起始日无法判断")
    events = {"SPX": (), "QQQ": rr.Unavailable((("QQQ", day),), "缺少必需价格", "MissingPriceError",
                                               "labels_r2.r2_events")}
    tool = {"groups": {"K=3,θ=0.025,h=1": {"r2": {}}}, "selection": {"exit": "缺值无法评价"},
            "r2_events_error": {"QQQ": {"reason": "缺少必需价格"}}}
    return tool, SimpleNamespace(stop=stop, common=SimpleNamespace(events=events))


def test_wiring_a3_d13_five_conditions() -> None:
    """第 5 项：五项条件逐项核实全部成立才记“裁决差异（D13-甲）”（独立计数，只豁免出口差异）；任一不成立仍按
    停止原因映射比较（不一致）。"""
    from types import SimpleNamespace

    tool, result = d13_case()
    record = support.Recorder()
    support.compare_research(record, tool, result, None, [], 0)
    assert record.summary()["停止原因（组合层）"]["exit ↔ stop_reason.reason"] == {support.D13_STATUS: 1}
    assert record.total("不一致") == 0 and record.total(support.D13_STATUS) == 1
    assert all(support.d13_evidence(tool, result)["逐项"].values())
    rr = support.research_run
    labels_other = rr.Unavailable(result.common.events["QQQ"].missing, "缺少必需价格", None, "basket_prefix")
    broken = [
        ({**tool, "r2_events_error": {}}, result),                                                  # 条件 1
        (tool, SimpleNamespace(stop=result.stop, common=SimpleNamespace(events={"SPX": (), "QQQ": labels_other}))),
        (tool, SimpleNamespace(stop=stop_record(rr.Exit.UNDETERMINABLE, "R2Undeterminable", None, {}, "R2",
                                                "K=3,θ_P=0.025,h=1", "构造：迟到与漏报"),
                               common=result.common)),                                              # 条件 2
        ({**tool, "groups": {"K=3,θ=0.025,h=1": {"ledger": {}}}}, result),                         # 条件 3
        ({**tool, "selection": {"exit": "选定"}}, result),                                          # 条件 4
        ({**tool, "stop_reason": {"reason": "提示段起始状态无法确定"}}, result),                       # 条件 5
    ]
    for changed_tool, changed_result in broken:
        record = support.Recorder()
        support.compare_research(record, changed_tool, changed_result, None, [], 0)
        assert record.total(support.D13_STATUS) == 0 and record.total("不一致") >= 1
