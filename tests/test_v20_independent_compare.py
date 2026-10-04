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

TOOL_ROOT = Path(support.OLD_TOOL.root)
TOOL_COMMIT = support.OLD_TOOL.commit                             # 被测代码 2276f1bc79dc1b37d3304da79728f7be07c29539
AUDIT_RELATIVE = Path("docs") / "audit" / "独立复核" / "v20" / "audit_v20.py"
AUDIT = TOOL_ROOT / AUDIT_RELATIVE
AUDIT_SHA256 = support.OLD_TOOL.audit_sha256
ORIGINAL_MANIFEST = Path(r"C:\Users\stone\Downloads\v20_独立工具导出\manifest.json")
ORIGINAL_MANIFEST_SHA256 = "93ccc01cd1681d953b099e7fad8e2b47a1a882e666440a60c31564c86b520119"
ADDED_MANIFEST = Path(r"C:\Users\stone\Downloads\v20_构造验收_2276f1b\manifest_新增.json")
ADDED_MANIFEST_SHA256 = "480fc6a6866b29c007521aef966bde99cd385504c463db213027e813c00a8477"
ADDED_NAMES = ("整行缺失", "整行缺失_含重复日期", "整行缺失_含多余日期")       # 新增场景中进入互比的 3 个（只做输入层）
CONSTRUCT_ROOT = Path(r"D:\temp_claude\v20\构造输入")     # 仓库外的专用临时目录（负责人 2026-10-03 改到 D 盘）
OUT_ENV, ONLY_ENV, FROM_ENV = "V20_COMPARE_OUT", "V20_COMPARE_ONLY", "V20_COMPARE_FROM"
# 第二轮（A2）：重比较时再设 V20_COMPARE_A2=1，接入研究组合层（《构造比对第二轮（A2）》）。
A2_ENV = "V20_COMPARE_A2"
# 第三轮（A3）：再设 V20_COMPARE_TOOL_FROM=<第三轮目录>，按场景选择工具输出来源（《A2 第三轮补充一》第二节）；
# 未设置时行为与第二轮完全相同。全程只读已保存输出，不启动工具子进程。
TOOL_FROM_ENV = "V20_COMPARE_TOOL_FROM"
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


def third_source() -> Path | None:
    value = os.environ.get(TOOL_FROM_ENV)
    return Path(value) if value else None


@functools.lru_cache(maxsize=1)
def third_inputs(third: Path) -> tuple[tuple[str, ...], dict[str, str], frozenset[str]]:
    """第三轮目录的受影响场景清单、工具重跑记录（场景 → 哈希）与 工具输出\\ 下的场景名；任一清单缺失即失败。"""
    listing = third / "导出" / "受影响场景清单.md"
    rerun = third / "检查记录" / "工具重跑记录.json"
    affected = support.affected_names(support.get_bytes(listing).decode("utf-8") if listing.is_file() else None)
    hashes = support.rerun_hashes(support.load_json(rerun) if rerun.is_file() else None)
    folder = third / "工具输出"
    files = frozenset(path.stem for path in folder.glob("*.json")) if folder.is_dir() else frozenset()
    return affected, hashes, files


def tool_source(source: Path, third: Path, name: str) -> support.ToolSource:
    """第三轮的工具输出来源：受影响场景 → _1（绑定新工具），其余 → _2（绑定旧工具）。"""
    affected, hashes, files = third_inputs(third)
    return support.choose_tool_source(name, affected, hashes, set(files), saved_listing(source))


def saved_relatives(kind: str, name: str) -> list[str]:
    files = [f"工具输出/{name}.json", f"项目字段/{name}.json"]
    return files if kind == "bootstrap" else [*files, f"构造输入/{name}/入口与适配结果.json"]


def run_parameters(kind: str, name: str, scenario: Path, tool_out: Path, source: Path | None = None,
                   chosen: support.ToolSource | None = None) -> dict:
    """试跑结果复用的依据（补充三条第三条）：场景、工具、接线源码、配置模板与运行参数的哈希与原文。
    V20_COMPARE_ONLY 只决定运行哪些场景，不影响单个场景的计算，不列入复用依据（另记入运行记录）。
    重比较时另绑定所读取的第二次运行原始输出及其清单的哈希（乙补修第三节第 3 条），且不运行工具。
    第三轮（chosen 不为 None）：工具绑定写该场景工具输出实际的生成工具；来自 _1 的工具输出不列入 _2 的
    source_files，另记来源、哈希与第三轮两份清单的哈希（《A2 第三轮补充一》第二节第 1、4 条）。"""
    if source is not None:
        listing = support.get_bytes(source / "导出" / "清单.md")
        relatives = saved_relatives(kind, name)
        if chosen is not None and chosen.label == support.SOURCE_THIRD:
            relatives = [relative for relative in relatives if relative != chosen.relative]
        parameters = {"kind": kind, "scenario": str(scenario),
                      "scenario_sha256": support.sha256(support.get_bytes(scenario)),
                      "audit_sha256": support.sha256(support.get_bytes(AUDIT)), "tool_commit": TOOL_COMMIT,
                      "wiring": {path.name: support.sha256(support.get_bytes(path)) for path in WIRING_FILES},
                      "mode": "重比较（不运行工具子进程与项目算法）", "recompare_from": str(source),
                      "source_listing_sha256": support.sha256(listing),
                      "source_files": {relative: saved_listing(source)[relative][1] for relative in relatives},
                      "python": sys.version, "env": {OUT_ENV: os.environ.get(OUT_ENV), FROM_ENV: str(source),
                                                     A2_ENV: os.environ.get(A2_ENV)},
                      "research_run_sha256": support.sha256(support.get_bytes(RESEARCH_RUN))}
        if chosen is not None:
            third = third_source()
            audit = Path(chosen.binding.root) / AUDIT_RELATIVE
            parameters.update({"audit_sha256": support.sha256(support.get_bytes(audit)),
                               "tool_commit": chosen.binding.commit, "tool_root": chosen.binding.root,
                               "tool_from": str(third),
                               "tool_from_files": {relative: support.sha256(support.get_bytes(third / relative))
                                                   for relative in ("导出/受影响场景清单.md",
                                                                    "检查记录/工具重跑记录.json")},
                               **support.tool_source_fields(chosen)})
            parameters["env"][TOOL_FROM_ENV] = str(third)
        return parameters
    return {"kind": kind, "scenario": str(scenario), "scenario_sha256": support.sha256(support.get_bytes(scenario)),
            "audit_sha256": support.sha256(support.get_bytes(AUDIT)), "tool_commit": TOOL_COMMIT,
            "wiring": {path.name: support.sha256(support.get_bytes(path)) for path in WIRING_FILES},
            "decisions_template_sha256": support.sha256(support.DECISIONS_TEXT.encode("utf-8")),
            "tool_command": [sys.executable, str(AUDIT), str(scenario), str(tool_out)],
            "python": sys.version, "env": {OUT_ENV: os.environ.get(OUT_ENV), "PYTHONIOENCODING": "utf-8",
                                           "PYTHONDONTWRITEBYTECODE": "1"}}


def run_tool(scenario: Path, tool_out: Path, AUDIT: Path = AUDIT, TOOL_ROOT: Path = TOOL_ROOT
             ) -> tuple[int, float, str]:
    """A 默认用旧工具；第二轮 B 显式传入 NEW_TOOL 的 audit_v20.py 与工作树。
    子进程语句原文不变，沿用隔离检查已登记的例外（按函数名与语句原文匹配）。"""
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
    third = third_source()
    chosen = tool_source(source, third, name) if third is not None else None
    if chosen is not None:
        audit = Path(chosen.binding.root) / AUDIT_RELATIVE
        assert support.sha256(support.get_bytes(audit)) == chosen.binding.audit_sha256, (
            f"{name} 生成工具 {chosen.binding.commit[:7]} 的 audit_v20.py 哈希不符")
    parameters = run_parameters(kind, name, scenario, out / "工具输出" / f"{name}.json", source, chosen)
    if record_path.is_file():
        previous = support.load_json(record_path)
        if previous.get("parameters") == parameters:
            assert previous["counts"].get("不一致", 0) == 0, f"{name} 先前的比对有不一致"
            return                                                     # 参数与哈希逐项相同
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if chosen is None:
        tool_bytes = saved_bytes(source, f"工具输出/{name}.json")
    elif chosen.label == support.SOURCE_THIRD:
        tool_bytes = support.checked_tool_bytes(chosen, support.get_bytes(third / chosen.relative))
    else:
        tool_bytes = support.checked_tool_bytes(chosen, saved_bytes(source, chosen.relative))
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
                                                 project_fields, None if chosen is None else chosen.label)
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
    if chosen is not None:
        result.update(support.tool_source_fields(chosen))           # 第三轮：工具输出来源与生成工具
    support.put_bytes(record_path, support.dump_json(result))
    support.assert_self_consistent(research.get("自洽检查") or {}, name)
    stop = research.get("stop") or {}
    # 《A2 修订二》第六节：run_window 记为未预期异常（或计算失败）即停，记录异常类型、消息与场景，不自行断定原因。
    assert stop.get("exit") not in ("未预期异常", "计算失败"), f"{name} run_window 停止：{stop}"
    assert counts["不一致"] == 0, f"{name} 有 {counts['不一致']} 处不一致，见 {record_path}"


def research_round(record: support.Recorder, out: Path, name: str, data: dict, cutoff_text: str, entry_info: dict,
                   tool: dict, project_fields: dict, source_label: str | None = None) -> dict:
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
        support.compare_research(record, tool, result, chosen, axis, len(axis) - 1, source_label)
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
# 第二轮 B：确认性检验的构造场景比对（《第二轮 B 设计（修订二）》；《接线 B 试跑》）。
# V20_COMPARE_B == "1" 且 V20_COMPARE_OUT 已设置时才参数化 69 个场景，否则参数化为空、不读取任何外部材料。
# B 运行中 V20_COMPARE_FROM、V20_COMPARE_A2、V20_COMPARE_TOOL_FROM 必须未设置。
# ---------------------------------------------------------------------------

B_ENV = "V20_COMPARE_B"
B_ROOT = Path(r"C:\Users\stone\Downloads\v20_独立工具导出_B")
B_MANIFEST = B_ROOT / "manifest.json"
B_MANIFEST_SHA256 = "6c40a794d811a8840672d58d05cb2c76ef6f2cf1466acb925e6113225d9654bb"
B_LISTING_SHA256 = "ca6667103fe2a64baf3c6b446bd05ab1f732bd6495e448d7741501a8d166546f"
NEW_AUDIT = Path(support.NEW_TOOL.root) / AUDIT_RELATIVE


def b_cases() -> list[str]:
    """manifest 的 69 个名称（按 manifest 顺序）；未设置 V20_COMPARE_B=1 或 V20_COMPARE_OUT 时为空。"""
    if os.environ.get(B_ENV) != "1" or not os.environ.get(OUT_ENV):
        return []
    return [item["name"] for item in support.load_json(B_MANIFEST)["scenarios"]]


B_CASES = b_cases()


def b_parameters(name: str, scenario: Path, histories: dict, split: str) -> dict:
    """逐场景记录的运行依据：场景、工具、接线源码、组合层与检验参数；histories、initial、continuity 写入记录。"""
    return {"kind": "confirm", "scenario": str(scenario),
            "scenario_sha256": support.sha256(support.get_bytes(scenario)),
            "audit_sha256": support.sha256(support.get_bytes(NEW_AUDIT)), "tool_commit": support.NEW_TOOL.commit,
            "tool_root": support.NEW_TOOL.root, "manifest_sha256": support.sha256(support.get_bytes(B_MANIFEST)),
            "listing_sha256": support.sha256(support.get_bytes(B_ROOT / "清单.md")),
            "wiring": {path.name: support.sha256(support.get_bytes(path)) for path in WIRING_FILES},
            "research_run_sha256": support.sha256(support.get_bytes(RESEARCH_RUN)),
            "histories": {asset: str(day) for asset, day in histories.items()},
            "initial": "登记初始快照（REGISTERED_CHANNELS、REGISTERED_SYSTEM、REGISTERED_REFERENCE）",
            "continuity": "COMPLETE_TRADING_AXIS", "purpose": "CONSTRUCTED", "diagnostics": False,
            "confirm_parameters": {"main": [20, 20261020], "sensitivities": [[10, 20261010], [40, 20261040],
                                                                          [60, 20261060], [120, 202610120]],
                                   "resamples": 10_000, "alpha": 0.05, "warning_p": 0.10, "annual_days": 252,
                                   "minimum_growth": 0.01, "tolerance": 1e-10, "padding": 20, "split": split},
            "python": sys.version, "env": {OUT_ENV: os.environ.get(OUT_ENV), B_ENV: os.environ.get(B_ENV),
                                           ONLY_ENV: os.environ.get(ONLY_ENV)}, "name": name}


@pytest.mark.parametrize("name", B_CASES, ids=B_CASES)
def test_b_confirm_case(name: str) -> None:
    """一个 confirm 场景：核对场景、manifest 与工具哈希 → 子进程运行新工具 → 构造输入与快照 →
    项目 run_window（构造验收、固定起点、单候选、诊断关闭）→ 未停止时 confirmatory_input → confirmatory_test →
    出口、对齐断言、各层比较 → 写出记录。"""
    if os.environ.get(B_ENV) != "1":
        pytest.skip(f"未设置 {B_ENV}=1：B 比对不运行")
    out = output_root()
    if not selected(name):
        pytest.skip(f"{ONLY_ENV} 未选中")
    for variable in (FROM_ENV, A2_ENV, TOOL_FROM_ENV):
        assert variable not in os.environ, f"B 运行中 {variable} 必须未设置"
    assert support.sha256(support.get_bytes(B_MANIFEST)) == B_MANIFEST_SHA256, "B 场景 manifest 哈希不符"
    assert support.sha256(support.get_bytes(B_ROOT / "清单.md")) == B_LISTING_SHA256, "B 场景清单哈希不符"
    assert support.sha256(support.get_bytes(NEW_AUDIT)) == support.NEW_TOOL.audit_sha256, "audit_v20.py 哈希不符"
    entry = next(item for item in support.load_json(B_MANIFEST)["scenarios"] if item["name"] == name)
    scenario = B_ROOT / "scenarios" / f"{name}.json"
    assert support.sha256(support.get_bytes(scenario)) == entry["sha256"], f"{name} 场景文件哈希与 manifest 不符"
    data = support.load_json(scenario)
    assert data["kind"] == "confirm", f"{name} 不是 confirm 场景"
    tool_out = out / "工具输出" / f"{name}.json"
    tool_out.parent.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    exit_code, tool_seconds, stderr = run_tool(scenario, tool_out, NEW_AUDIT, Path(support.NEW_TOOL.root))
    assert exit_code in (0, 3), f"{name} 工具退出码 {exit_code}：{stderr}"
    tool = support.load_json(tool_out)
    project_started = time.perf_counter()
    record = support.Recorder()
    # 构造输入（与 A 的 full 同一转换）→ CSV 入口与快照适配路径 → 输入层（沿用 A 映射）
    cutoff_text = support.scenario_cutoff(data)
    root = out / "构造输入" / name
    constructed = support.construct(data, root)
    cutoff = dt.date.fromisoformat(cutoff_text)
    assert_decision_dates(data, cutoff)
    csv = support.csv_entry(constructed, cutoff)
    adaptation = support.adapt(data, constructed, cutoff_text)
    evidence = (support.axis_evidence(*support.constructed_axis_inputs(root), cutoff)
                if needs_axis_evidence(tool) else None)
    # 补充二：在 B 调用 A 的输入层比较之前统一判断键是否存在（缺键项记未比较）；A 的函数不改。
    support.compare_b_input_layer(record, tool, support.input_facts(csv, adaptation), cutoff_text, evidence)
    snapshot = adaptation.snapshot
    assert snapshot is not None, f"{name} 快照适配路径未构造出快照"
    axis = snapshot.days
    candidate = support.confirm_candidate(data)
    histories = {asset: axis[0] for asset in support.ASSETS}
    split = dt.date.fromisoformat(data["half_split"])
    spec = support.research_run.WindowSpec(
        support.research_run.Purpose.CONSTRUCTED, dt.date.fromisoformat(data["window_start"]), snapshot.day, histories,
        support.research_run.InitialStates(support.REGISTERED_CHANNELS, support.REGISTERED_SYSTEM,
                                           support.REGISTERED_REFERENCE),
        support.research_run.Continuity.COMPLETE_TRADING_AXIS)
    error, result = None, None
    try:
        result = support.research_run.run_window(snapshot, spec, support.confirm_run_parameters(), (candidate,))
    except support.research_run.ResearchRunError as caught:
        # 设计第六节第 6 条：只有 confirm_空窗口_later 允许捕获，且消息须恰为登记原文；其他一律停止。
        assert support.b_accept_error(name, str(caught)), f"{name} 未登记的异常：{type(caught).__name__}：{caught}"
        error = str(caught)
    stop = None if result is None else result.stop
    j0 = (result.window.j0 if result is not None and result.window is not None
          else (stop.detail.get("j0_index") if stop is not None else None))
    facts = support.b_start_facts(snapshot, candidate) if (stop is not None or tool.get("exit_code") == 3) else None
    support.compare_b_common_top(record, tool, data)                       # 《接线 B 全量》第一节第 1 条：分支之前
    stop_optional = support.compare_b_stop_keys(record, tool) if tool.get("exit_code") == 3 else None
    case = support.compare_b_exit(record, tool, stop, error, axis, j0, facts)
    extra: dict = {"出口情形": case, "起点依据": facts, "工具 stop_reason": tool.get("stop_reason"),
                   "停止类可有键原值": stop_optional,
                   "项目 stop": None if stop is None else {"exit": stop.exit.value,
                                                           "exception_type": stop.exception_type,
                                                           "message": stop.message, "detail": dict(stop.detail)},
                   "项目异常": None if error is None else {"type": "ResearchRunError", "message": error},
                   "自洽": "无第一轮基准（B 场景无第一轮同名组合层记录；确认性检验在第一轮双方均未运行）"}
    if case == "双方均完成":
        assert result is not None and result.window is not None
        confirm_data = support.research_run.confirmatory_input(result, candidate, support.B_PERIOD)
        outcome = support.confirmatory.confirmatory_test(confirm_data, support.confirm_parameters(split))
        alignment = support.b_alignment(data, axis, j0, confirm_data, split, tool)
        extra["对齐断言"] = alignment
        assert all(value is not False for value in alignment.values()), f"{name} 对齐断言不成立：{alignment}"
        premise = support.b_premise(tool, result.common.events)
        extra["P_B"] = premise
        support.compare_b_confirm(record, tool, data, candidate, result, confirm_data, outcome, premise)
        if outcome.valid:                                                    # 第一节第 4 条：p 原值，便于复算
            rows = (outcome.main, *outcome.sensitivities)
            extra["p 原值"] = {"项目": {str(row.block): row.p_value for row in rows},
                              "工具": {str(item.get("b")): str(item.get("p")) for item in tool.get("bootstrap") or []}}
        extra["项目检验"] = {"valid": outcome.valid, "reason": outcome.reason, "n": outcome.n,
                          "category": outcome.category, "conclusion": outcome.conclusion}
    project_seconds = time.perf_counter() - project_started
    counts = {status: record.total(status) for status in support.B_STATUSES}
    counts["窗口末日边界"] = record.window_end()
    payload = {"name": name, "kind": "confirm", "parameters": b_parameters(name, scenario, histories,
                                                                          data["half_split"]),
               "started": started, "tool_exit_code": exit_code, "tool_seconds": round(tool_seconds, 3),
               "project_seconds": round(project_seconds, 3),
               "tool_output_sha256": support.sha256(support.get_bytes(tool_out)), "counts": counts,
               "by_layer": {layer: items for layer, items in record.summary().items()}, "details": record.details,
               "evidence": extra}
    record_path = out / "逐场景比对" / f"{name}.json"
    support.put_bytes(record_path, support.dump_json(payload))
    assert counts["不一致"] == 0, f"{name} 有 {counts['不一致']} 处不一致，见 {record_path}"


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
    # 补充二（修订一）第一节：“双方均无判定”须前提 P 成立；本例 QQQ 为双方事件均不可得（P 成立）。只补输入，断言不变。
    premise = {"SPX": False, "QQQ": True}
    record = support.Recorder()
    support.compare_r2_judgements(record, "构造组", tool, {"SPX": mine, "QQQ": None}, premise)
    assert record.total("不一致") == 0                                           # QQQ：双方均无判定，记一致
    assert record.summary()["R2 判定"]["events.category"] == {"一致": 1}
    tool_event["category"] = "漏报"
    record = support.Recorder()
    support.compare_r2_judgements(record, "构造组", tool, {"SPX": mine, "QQQ": None}, premise)
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


# ---------------------------------------------------------------------------
# 第三轮（A3）混合来源的自检（《A2 第三轮补充一》第二节第 5 条；内存构造，不读文件）
# ---------------------------------------------------------------------------


def test_wiring_a3r_mixed_tool_source() -> None:
    """受影响场景选 _1（绑定新工具）、其余选 _2（绑定旧工具）；哈希不符、清单缺失、两处都有或都无即失败。"""
    new_bytes, old_bytes = b'{"name": "path_170"}', b'{"name": "path_000"}'
    listing = ("# 受影响场景清单\n\n| 场景 | r2_events_error 非空的资产 | _2 工具输出 SHA-256 |\n| --- | --- | --- |\n"
               "| path_170 | QQQ | `" + "0" * 64 + "` |\n")
    rerun = {"tool_commit": support.NEW_TOOL.commit, "audit_sha256": support.NEW_TOOL.audit_sha256,
             "runs": [{"name": "path_170", "exit_code": 0, "output_sha256": support.sha256(new_bytes)}]}
    second = {"工具输出/path_000.json": (len(old_bytes), support.sha256(old_bytes)),
              "工具输出/path_170.json": (1, "1" * 64)}
    affected, hashes = support.affected_names(listing), support.rerun_hashes(rerun)
    assert affected == ("path_170",)
    third = support.choose_tool_source("path_170", affected, hashes, {"path_170"}, second)
    assert (third.label, third.binding) == (support.SOURCE_THIRD, support.NEW_TOOL)
    assert support.checked_tool_bytes(third, new_bytes) == new_bytes
    old = support.choose_tool_source("path_000", affected, hashes, {"path_170"}, second)
    assert (old.label, old.binding) == (support.SOURCE_SECOND, support.OLD_TOOL)
    assert support.tool_source_fields(old) == {"工具输出来源": "_2", "工具输出 SHA-256": support.sha256(old_bytes),
                                               "生成工具提交": support.OLD_TOOL.commit,
                                               "生成工具 audit_v20.py SHA-256": support.OLD_TOOL.audit_sha256}
    failures = [
        lambda: support.checked_tool_bytes(third, old_bytes),                                       # 哈希不符
        lambda: support.affected_names(None),                                                       # 清单缺失
        lambda: support.rerun_hashes(None),                                                         # 重跑记录缺失
        lambda: support.rerun_hashes({**rerun, "audit_sha256": support.OLD_TOOL.audit_sha256}),     # 绑定不符
        lambda: support.choose_tool_source("path_000", affected, hashes, {"path_170", "path_000"}, second),  # 两处都有
        lambda: support.choose_tool_source("path_170", affected, hashes, set(), second),             # 受影响但 _1 无
        lambda: support.choose_tool_source("path_999", affected, hashes, {"path_170"}, second),      # 两处都无
        lambda: support.choose_tool_source("path_170", affected, {}, {"path_170"}, second),          # 两份清单不一致
    ]
    for failure in failures:
        with pytest.raises(support.ToolSourceError):
            failure()


# ---------------------------------------------------------------------------
# 第三轮（A3）不可得资产的表示换算自检（《A2 第三轮补充二（修订一）》第一节第 5 条；内存构造，不读文件）
# ---------------------------------------------------------------------------

UNAVAILABLE = support.UNAVAILABLE_TEXT


def unavailable_qqq() -> object:
    rr = support.research_run
    return rr.Unavailable((("QQQ", dt.date.fromisoformat(DAYS[1])),), "缺少必需价格", "MissingPriceError",
                          "labels_r2.r2_events")


def test_wiring_a3r_premise_requires_matching_reason() -> None:
    """前提 P(a)：项目 labels_r2 缺价不可得、工具 r2_events_error 有该资产，且原因与缺价日一致；
    不凭字串或 None 认定。"""
    events = {"SPX": (), "QQQ": unavailable_qqq()}
    tool = {"r2_events_error": {"QQQ": {"reason": "缺少必需价格", "missing_days": [DAYS[1]]}}}
    assert support.availability_premise(tool, events) == {"SPX": False, "QQQ": True}
    assert support.availability_premise({"r2_events_error": {"QQQ": {"reason": "缺少必需价格",
                                                                       "missing_days": [DAYS[2]]}}},
                                        events)["QQQ"] is False                  # 缺价日不一致
    assert support.availability_premise({}, events)["QQQ"] is False              # 工具无 r2_events_error


def test_wiring_a3r_unavailable_text_with_premise() -> None:
    """(a) P 成立 + 字串 ↔ None：R2 记“R2 无法计算（双方均无判定）”，段账记“段账（双方均无）”；两资产都不可得时
    段账结构三项各记一致。"""
    premise = {"SPX": True, "QQQ": True}
    group = {"r2": {"SPX": UNAVAILABLE, "QQQ": UNAVAILABLE},
             "ledger": {"segments": None, "pre_window_count": None,
                        "by_asset": {"SPX": UNAVAILABLE, "QQQ": UNAVAILABLE}}}
    record = support.Recorder()
    support.compare_r2_judgements(record, "构造组", group, {"SPX": None, "QQQ": None}, premise)
    support.compare_ledgers(record, "构造组", group, {"SPX": None, "QQQ": None}, premise, support.SOURCE_THIRD)
    summary = record.summary()
    assert record.total("不一致") == 0
    assert summary["R2 判定"]["R2 无法计算（双方均无判定）"] == {"一致": 2}
    assert summary["提示段账"]["段账（双方均无）"] == {"一致": 2}
    assert summary["提示段账"][support.LEDGER_STRUCTURE_ITEM] == {"一致": 3}


def test_wiring_a3r_premise_with_object_is_inconsistent() -> None:
    """(b) P 成立而任一侧出现判定或段账对象：不一致。"""
    premise = {"SPX": False, "QQQ": True}
    record = support.Recorder()
    support.compare_r2_judgements(record, "构造组", {"r2": {"QQQ": {"events": []}}}, {"SPX": object(), "QQQ": None},
                                  premise)
    assert record.summary()["R2 判定"]["P 成立而出现判定对象"] == {"不一致": 1}
    record = support.Recorder()
    support.compare_r2_judgements(record, "构造组", {"r2": {"QQQ": UNAVAILABLE}}, {"SPX": object(), "QQQ": object()},
                                  premise)
    assert record.summary()["R2 判定"]["P 成立而出现判定对象"] == {"不一致": 1}


def test_wiring_a3r_unavailable_text_without_premise_is_inconsistent() -> None:
    """(c) P 不成立而工具出现字串或项目为 None：不一致；_1 来源缺 ledger 键也不一致（⑥）。"""
    premise = {"SPX": False, "QQQ": False}
    record = support.Recorder()
    support.compare_r2_judgements(record, "构造组", {"r2": {"SPX": UNAVAILABLE}}, {"SPX": object(), "QQQ": None},
                                  premise)
    assert record.summary()["R2 判定"]["P 不成立而出现不可得表示"] == {"不一致": 2}
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", {"r2": {}}, {"SPX": None, "QQQ": None}, premise, support.SOURCE_THIRD)
    assert record.summary()["提示段账"]["ledger 键缺失（_1 来源）"] == {"不一致": 2}


def available_ledger(pre_window: bool, category: object) -> object:
    from market_risk.wavewarn_v20.r2 import Segment, SegmentClass, SegmentLedger

    segment = Segment(dt.date.fromisoformat(DAYS[0]), dt.date.fromisoformat(DAYS[1]), pre_window)
    counts = {item: int(item is category) for item in SegmentClass}
    return SegmentLedger(((segment, category),), counts, None)


def test_wiring_a3r_null_class_requires_pre_window() -> None:
    """(d) 可得资产 SPX：class 为 null 而 pre_window 为假 → 不一致；pre_window 为真且项目为“窗口前已启动” → 一致；
    class 中出现不可得资产的键 → 不一致。"""
    from market_risk.wavewarn_v20.r2 import SegmentClass

    premise = {"SPX": False, "QQQ": True}

    def ledger_group(pre_window: bool, classes: object, category: object) -> dict:
        counts = {item.value: int(item is category) for item in SegmentClass if item is not SegmentClass.PRE_WINDOW}
        return {"ledger": {"segments": [{"start": DAYS[0], "end": DAYS[1], "pre_window": pre_window, "class": classes}],
                           "pre_window_count": int(category is SegmentClass.PRE_WINDOW),
                           "by_asset": {"SPX": {"counts": counts, "false_alarm_ratio": "无定义"}, "QQQ": UNAVAILABLE}}}

    early = SegmentClass.EARLY
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", ledger_group(False, None, early),
                            {"SPX": available_ledger(False, early), "QQQ": None}, premise, support.SOURCE_THIRD)
    assert record.summary()["提示段账"]["segments.class（null ↔ 窗口前已启动）"] == {"不一致": 1}
    pre = SegmentClass.PRE_WINDOW
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", ledger_group(True, None, pre),
                            {"SPX": available_ledger(True, pre), "QQQ": None}, premise, support.SOURCE_THIRD)
    assert record.total("不一致") == 0
    assert record.summary()["提示段账"]["segments.class（null ↔ 窗口前已启动）"] == {"一致": 1}
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", ledger_group(False, {"SPX": early.value, "QQQ": early.value}, early),
                            {"SPX": available_ledger(False, early), "QQQ": None}, premise, support.SOURCE_THIRD)
    assert record.summary()["提示段账"]["segments.class 不含不可得资产的键"] == {"不一致": 1}


def test_wiring_a3r_both_unavailable_requires_null_segments() -> None:
    """(e) 两资产都不可得而 segments 非 null：段账结构不一致。"""
    premise = {"SPX": True, "QQQ": True}
    group = {"ledger": {"segments": [], "pre_window_count": None, "by_asset": {"SPX": UNAVAILABLE, "QQQ": UNAVAILABLE}}}
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", group, {"SPX": None, "QQQ": None}, premise, support.SOURCE_THIRD)
    assert record.summary()["提示段账"][support.LEDGER_STRUCTURE_ITEM] == {"一致": 2, "不一致": 1}


# ---------------------------------------------------------------------------
# 补充三（段账结构核验修正）：段账①③的直接自检与结构核验的缺失键（《A2 第三轮补充三》第一节第 2 条；内存构造）。
#
# 第三轮表示换算分支的自检覆盖表（R2 判定：compare_r2_judgements；段账：compare_ledgers）：
# | 分支 | 覆盖用例 |
# | R2 ① P 成立而出现判定对象 | test_wiring_a3r_premise_with_object_is_inconsistent |
# | R2 ② P 成立、键缺失或字串 ↔ None | test_wiring_a3r_unavailable_text_with_premise（字串）；
# |                               | test_wiring_a2_r2_category_conversion（键缺失） |
# | R2 ③ P 不成立而出现字串或 None | test_wiring_a3r_unavailable_text_without_premise_is_inconsistent |
# | R2 ④ 双方均为对象 | test_wiring_a2_r2_category_conversion |
# | R2 ⑤ 映射表外字串 | 未覆盖 |
# | 段账 ① P 成立而出现段账对象 | test_wiring_a3r_ledger_premise_with_object_is_inconsistent |
# | 段账 ② P 成立、字串 ↔ None | test_wiring_a3r_unavailable_text_with_premise |
# | 段账 ③ P 不成立而出现字串或 None | test_wiring_a3r_ledger_without_premise_is_inconsistent |
# | 段账 ④ 双方均为对象（class null、不可得资产键） | test_wiring_a3r_null_class_requires_pre_window |
# | 段账 ⑤ 两资产都不可得的结构核验 | test_wiring_a3r_unavailable_text_with_premise（一致）；
# |                               | test_wiring_a3r_both_unavailable_requires_null_segments（segments 非 null）；
# |                               | test_wiring_a3r_ledger_structure_requires_keys（缺键） |
# | 段账 ⑥ ledger 键缺失 | _1 来源：test_wiring_a3r_unavailable_text_without_premise_is_inconsistent；
# |                     | _2 来源或未设第三轮来源：未覆盖 |
# | 段账 ⑦ 映射表外字串 | 未覆盖 |
# ---------------------------------------------------------------------------


def ledger_with_qqq(qqq: object) -> dict:
    """SPX 可得（P 不成立、项目有段账对象）的最小段账：只用于 QQQ 一侧的分支判定，SPX 一侧的比较不在断言范围内。"""
    from market_risk.wavewarn_v20.r2 import SegmentClass

    counts = {item.value: 0 for item in SegmentClass if item is not SegmentClass.PRE_WINDOW}
    return {"ledger": {"segments": [], "pre_window_count": 0,
                       "by_asset": {"SPX": {"counts": counts, "false_alarm_ratio": "无定义"}, "QQQ": qqq}}}


def test_wiring_a3r_ledger_premise_with_object_is_inconsistent() -> None:
    """段账①：P(QQQ) 成立而工具 by_asset["QQQ"] 为对象，或项目 ledgers["QQQ"] 不为 None → 各记不一致 1。"""
    from market_risk.wavewarn_v20.r2 import SegmentClass

    premise = {"SPX": False, "QQQ": True}
    item = "P 成立而出现段账对象"
    spx = available_ledger(False, SegmentClass.EARLY)
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", ledger_with_qqq({"counts": {}, "false_alarm_ratio": "无定义"}),
                            {"SPX": spx, "QQQ": None}, premise, support.SOURCE_THIRD)
    assert record.summary()["提示段账"][item] == {"不一致": 1}
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", ledger_with_qqq(UNAVAILABLE), {"SPX": spx, "QQQ": spx}, premise,
                            support.SOURCE_THIRD)
    assert record.summary()["提示段账"][item] == {"不一致": 1}


def test_wiring_a3r_ledger_without_premise_is_inconsistent() -> None:
    """段账③：P 不成立而工具 by_asset["QQQ"] 为字串，或项目 ledgers["QQQ"] 为 None（工具为对象）→ 各记不一致 1。"""
    from market_risk.wavewarn_v20.r2 import SegmentClass

    premise = {"SPX": False, "QQQ": False}
    item = "P 不成立而出现不可得表示"
    spx = available_ledger(False, SegmentClass.EARLY)
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", ledger_with_qqq(UNAVAILABLE), {"SPX": spx, "QQQ": spx}, premise,
                            support.SOURCE_THIRD)
    assert record.summary()["提示段账"][item] == {"不一致": 1}
    record = support.Recorder()
    support.compare_ledgers(record, "构造组", ledger_with_qqq({"counts": {}, "false_alarm_ratio": "无定义"}),
                            {"SPX": spx, "QQQ": None}, premise, support.SOURCE_THIRD)
    assert record.summary()["提示段账"][item] == {"不一致": 1}


def test_wiring_a3r_ledger_structure_requires_keys() -> None:
    """段账⑤：两资产都不可得时，segments、pre_window_count 须键存在且为 null，by_asset 两键须存在且为登记字串；
    缺任一键 → 段账结构不一致 1；三键齐全 → 一致 3（回归）。"""
    premise = {"SPX": True, "QQQ": True}
    full = {"segments": None, "pre_window_count": None, "by_asset": {"SPX": UNAVAILABLE, "QQQ": UNAVAILABLE}}
    cases = [({key: value for key, value in full.items() if key != "segments"}, {"一致": 2, "不一致": 1}),
             ({key: value for key, value in full.items() if key != "pre_window_count"}, {"一致": 2, "不一致": 1}),
             ({**full, "by_asset": {"SPX": UNAVAILABLE}}, {"一致": 2, "不一致": 1}),
             (full, {"一致": 3})]
    for ledger, expected in cases:
        record = support.Recorder()
        support.compare_ledgers(record, "构造组", {"ledger": ledger}, {"SPX": None, "QQQ": None}, premise,
                                support.SOURCE_THIRD)
        assert record.summary()["提示段账"][support.LEDGER_STRUCTURE_ITEM] == expected


# ---------------------------------------------------------------------------
# 第二轮 B 接线自检（《接线 B 试跑》第一节第 8 条；纯内存构造，不读场景文件、不跑子进程）。
#
# 已覆盖 / 未覆盖分支表：
# | 分支 | 覆盖用例 |
# | 1 原因匹配：类别 + 对象 + 缺价日期全同才一致；多报记未比较；找不到不一致；净值 / 标签史按 object 区分 |
# |   | test_wiring_b_reason_matching |
# | 1 原因：主设定 n ÷ b < 2、d_j 非有限（工具侧登记文字、无 object） | test_wiring_b_reason_exact_without_object |
# | 1 原因：R2 非左截断事件为 0 个、对账不符 | 未覆盖（无自检；若试跑触发，以实际记录检验） |
# | 补充一 1 Decimal 换算：容差规则与 p 精确 | test_wiring_b_decimal_numbers |
# | 补充一 2 / 补充二 1 工具 confirm 无 inputs → 缺价日期集合与入口停止（缺价）未比较 |
#   | test_wiring_b_absent_inputs |
# | 补充二 2 清理表各类不存在的键（stop_reason、input_checks、r1、r2 子键、事件子键） |
#   | test_wiring_b_absent_key_classes |
# | 补充一 5 own_loss_text：一致 / 不一致 / 未比较（文字格式未登记） | test_wiring_b_own_loss_text |
# | 2 两阶段：提前返回缺键 → 未比较；项目短路 → 未比较；无“项目 None + 工具缺键 → 一致”；应存在而缺键 → 不一致 |
# |   | test_wiring_b_two_phase_pairs |
# | 3 P_B(a) 三条件；工具 r2 缺键单独不构成不可得 | test_wiring_b_premise |
# | 4 无效结论两种文字 ↔ 计算无效；其他文字不放宽 | test_wiring_b_invalid_conclusion |
# | 5 警示三条映射；集合不等即不一致 | test_wiring_b_warnings |
# | 6 浮点容差内而类别或判断不同 → 不一致 | test_wiring_b_tolerance_does_not_mask_judgement |
# | 7 p 精确、n 精确 | test_wiring_b_p_and_n_exact |
# | 8 出口四条接口差异、未登记组合、D15 条件缺一不成立 | test_wiring_b_exits |
# | 9 ResearchRunError 只在指定场景、指定原文时接受 | test_wiring_b_accept_error |
# | 10 registered_settings = false → 不一致 | test_wiring_b_registered_settings |
# | 全量 1 停止类顶层键前置与必需键 | test_wiring_b_stop_class_top_keys |
# | 全量 3 b 集合按数值排序的区分性 | test_wiring_b_bootstrap_numeric_order |
# | 事件置零集合与自助法逐行比较（compare_b_zeroing、compare_b_bootstrap） | 未覆盖（由试跑实际记录检验） |
# | compare_b_confirm 整体流程、b_alignment | 未覆盖（需要窗口结果，由试跑实际记录检验） |
# ---------------------------------------------------------------------------


def test_wiring_b_reason_matching() -> None:
    """净值缺价：类别、对象、缺价日期全同才一致，工具多报的 R2 原因记未比较；日期不同或只有 R2 对象 → 不一致。"""
    day = dt.date.fromisoformat(DAYS[2])
    nav = [{"reason": "缺少必需价格", "object": "净值", "missing": [["SPX", DAYS[2]]]}]
    labels = [{"reason": "缺少必需价格", "object": "R2 SPX", "missing_days": [DAYS[2]]}]
    reason = "净值所需价格缺失，收益无法计算"
    record = support.Recorder()
    support.compare_b_reasons(record, nav + labels, reason, [("SPX", day)], {}, {})
    assert record.total("一致") == 1 and record.total("不一致") == 0
    assert record.summary()["检验层"]["invalid_reasons（工具多报原因）"] == {"未比较": 1}
    record = support.Recorder()
    support.compare_b_reasons(record, [{**nav[0], "missing": [["SPX", DAYS[3]]]}], reason, [("SPX", day)], {}, {})
    assert record.total("不一致") == 1                                         # 缺价日期不同
    record = support.Recorder()
    support.compare_b_reasons(record, labels, reason, [("SPX", day)], {}, {})
    assert record.total("不一致") == 1                                         # 标签史缺价不能顶替净值缺价
    record = support.Recorder()
    support.compare_b_reasons(record, nav, "R1 无法计算", [], {}, {})
    assert record.summary()["检验层"]["invalid_reasons（项目原因不在映射表内）"] == {"不一致": 1}


def test_wiring_b_two_phase_pairs() -> None:
    """两阶段：工具提前返回缺可不存在键 → 未比较；工具已算而项目短路 → 未比较；不存在“项目 None + 工具缺键 → 一致”；
    工具有效而缺应存在键 → 不一致。"""
    record = support.Recorder()
    assert support.b_pair(record, "检验层", "delta", {}, "delta", None, False, False) == (False, None)
    assert support.b_pair(record, "检验层", "delta", {"delta": 0.1}, "delta", None, False, False) == (False, 0.1)
    assert record.summary()["检验层"]["delta"] == {"未比较": 2}
    assert [item["note"] for item in record.details] == [support.B_TOOL_EARLY, support.B_PROJECT_SHORT]
    assert record.total("一致") == 0
    record = support.Recorder()
    support.b_pair(record, "检验层", "delta", {}, "delta", None, True, True)
    assert record.summary()["检验层"]["delta（缺键）"] == {"不一致": 1}


def test_wiring_b_premise() -> None:
    """P_B(a)：项目 labels_r2 缺价不可得 ∧ 工具 invalid_reasons 有 {R2 QQQ, 缺少必需价格, 同缺价日}
    ∧ 工具 r2 无 QQQ 键。"""
    events = {"SPX": (), "QQQ": unavailable_qqq()}
    reasons = [{"reason": "缺少必需价格", "object": "R2 QQQ", "missing_days": [DAYS[1]]}]
    assert support.b_premise({"invalid_reasons": reasons, "r2": {"SPX": {}}}, events) == {"SPX": False, "QQQ": True}
    assert support.b_premise({"invalid_reasons": reasons, "r2": {"QQQ": {}}}, events)["QQQ"] is False
    assert support.b_premise({"invalid_reasons": [], "r2": {}}, events)["QQQ"] is False
    assert support.b_premise({"invalid_reasons": reasons, "r2": {}}, {"SPX": (), "QQQ": ()})["QQQ"] is False


def test_wiring_b_invalid_conclusion() -> None:
    """无效结论：工具两种文字 ↔ 项目“计算无效”；其他文字不放宽。"""
    assert support.b_invalid_conclusion_ok("计算无效", "计算无效")
    assert support.b_invalid_conclusion_ok("计算无效，不写任何优劣结论", "计算无效")
    assert not support.b_invalid_conclusion_ok("计算无效。", "计算无效")
    assert not support.b_invalid_conclusion_ok("计算无效", "未证明长期收益优于参照规则")


def test_wiring_b_warnings() -> None:
    """警示文字三条映射；映射后集合不等即不一致。"""
    tool = ["敏感性区块下 p ≥ 0.10", "前后两半的 Δ 方向不一致", "事件窗口置零后不再为正"]
    mine = [support.confirmatory.WARNING_SENSITIVITY, support.confirmatory.WARNING_HALVES,
            support.confirmatory.WARNING_ZEROING]
    assert support.b_mapped_warnings(tool) == sorted(mine)
    record = support.Recorder()
    record.exact("检验层", "warnings", "场景", support.b_mapped_warnings(tool[:2]), sorted(mine))
    assert record.total("不一致") == 1


def test_wiring_b_tolerance_does_not_mask_judgement() -> None:
    """浮点“容差内”而类别不同：容差内单列、类别记不一致（触发停止）；超出容差即不一致。"""
    record = support.Recorder()
    assert support.b_number(record, "检验层", "delta", "场景", 0.1, 0.1 + 1e-15) == support.TOLERANCE_STATUS
    record.exact("检验层", "category", "category", "A", "B")
    assert record.total(support.TOLERANCE_STATUS) == 1 and record.total("不一致") == 1 and record.total("一致") == 0
    assert support.b_number(support.Recorder(), "检验层", "delta", "场景", 0.1, 0.1 + 1e-9) == "不一致"
    assert support.b_number(support.Recorder(), "检验层", "delta", "场景", 0.1, 0.1) == "一致"


def test_wiring_b_p_and_n_exact() -> None:
    """p 精确（不用容差）；n 精确。"""
    record = support.Recorder()
    support.compare_b_bootstrap(record, [{"b": 20, "seed": 20261020, "valid": True, "p": 0.04, "q025": -0.1,
                                          "q975": 0.1}],
                                [support.confirmatory.BootstrapRow(20, 20261020, True, 0.04 + 1e-15, -0.1, 0.1, "")])
    assert record.summary()["检验层"]["bootstrap.p（精确）"] == {"不一致": 1}
    record = support.Recorder()
    record.exact("检验层", "n", "n", 341, 340)
    assert record.total("不一致") == 1


def test_wiring_b_exits() -> None:
    """出口：四条接口差异各一例；未登记组合 → 不一致；D15 条件缺一不成立。"""
    rr = support.research_run
    axis = [dt.date(2006, 1, 2) + dt.timedelta(days=index) for index in range(300)]
    e_index = len(axis) - 1
    unmet = stop_record(rr.Exit.FIXED_START_UNMET, None, None, {"j0_index": e_index})
    empty = {"exit_code": 3, "stop_reason": {"reason": "评价窗口为空", "sub_reason": "起点等于最后一个收盘日"}}
    later = {"exit_code": 3, "stop_reason": {"reason": "评价窗口为空", "sub_reason": "起点晚于最后一个收盘日"}}
    unconverged = {"exit_code": 3, "stop_reason": {"reason": "未收敛"}}
    cases = [(empty, unmet, None, e_index, None), (later, None, support.B_LATER_MESSAGE, None, None),
             (unconverged, stop_record(rr.Exit.NOT_CONVERGED, "NotConvergedError", "始终不收敛", {}), None, None, None),
             (unconverged, unmet, None, 205, {"t0": 199, "kappa": 210})]
    for tool, stop, error, j0, facts in cases:
        record = support.Recorder()
        support.compare_b_exit(record, tool, stop, error, axis, j0, facts)
        assert record.total("接口差异") == 1 and record.total("不一致") == 0, (tool, j0)
    record = support.Recorder()
    support.compare_b_exit(record, {"exit_code": 3, "stop_reason": {"reason": "计算失败"}}, None, None, axis, 220, None)
    assert record.summary()["出口"][support.B_UNREGISTERED_EXIT] == {"不一致": 1}
    record = support.Recorder()
    support.compare_b_exit(record, {"exit_code": 0}, unmet, None, axis, 220, {"t0": 199, "kappa": 210})
    assert record.total(support.D15_STATUS) == 1 and record.total("不一致") == 0
    for j0, facts in ((220, {"t0": 199, "kappa": None}), (262, {"t0": 199, "kappa": 210}),
                      (205, {"t0": 199, "kappa": 210}), (220, None)):
        record = support.Recorder()
        support.compare_b_exit(record, {"exit_code": 0}, unmet, None, axis, j0, facts)
        assert record.total(support.D15_STATUS) == 0 and record.total("不一致") == 1, (j0, facts)


def test_wiring_b_accept_error() -> None:
    """ResearchRunError 只在 confirm_空窗口_later、且消息恰为登记原文时接受。"""
    assert support.b_accept_error("confirm_空窗口_later", "构造验收给出的固定起点须在轴上")
    assert not support.b_accept_error("confirm_空窗口_equal", "构造验收给出的固定起点须在轴上")
    assert not support.b_accept_error("confirm_空窗口_later", "构造验收给出的固定起点须在轴上。")


def test_wiring_b_registered_settings() -> None:
    """registered_settings 为 false → 不一致；params 精确（完成类专有）；tool、kind、name 由 compare_b_common_top
    在分支之前比较（《接线 B 全量》第一节第 1 条），tool 与登记值 NEW_TOOL_VERSION 精确。"""
    from decimal import Decimal

    from market_risk.wavewarn_v20.convergence import Candidate

    candidate = Candidate(3, Decimal("0.015"), 1)
    scenario = {"kind": "confirm", "name": "confirm_000"}
    tool = {"tool": "v20-indep-3", "kind": "confirm", "name": "confirm_000", "params": support.group_key(candidate),
            "registered_settings": False}
    record = support.Recorder()
    support.compare_b_common_top(record, tool, scenario)
    support.compare_b_top(record, tool, scenario, candidate)
    summary = record.summary()["顶层键"]
    assert summary["registered_settings"] == {"不一致": 1}
    assert summary["tool"] == summary["kind"] == summary["name"] == summary["params"] == {"一致": 1}
    record = support.Recorder()
    support.compare_b_common_top(record, {**tool, "tool": "v20-indep-2"}, scenario)
    assert record.summary()["顶层键"]["tool"] == {"不一致": 1}


def stop_class_tool() -> dict:
    return {"tool": "v20-indep-3", "kind": "confirm", "name": "confirm_空窗口_equal", "exit_code": 3,
            "stop_reason": {"reason": "评价窗口为空", "detail": "确认性检验窗口内没有可计入收益的区间",
                            "sub_reason": "起点等于最后一个收盘日", "n": 0}}


def test_wiring_b_stop_class_top_keys() -> None:
    """《接线 B 全量》第一节第 3 条：停止类三项（tool、kind、name）正确值 → 一致；错误值各一例 → 不一致；
    必需键齐全时必需键检查全一致，可有键返回原值；缺 stop_reason 或缺 name → 不一致。"""
    scenario = {"kind": "confirm", "name": "confirm_空窗口_equal"}
    tool = stop_class_tool()
    record = support.Recorder()
    support.compare_b_common_top(record, tool, scenario)
    optional = support.compare_b_stop_keys(record, tool)
    assert record.total("不一致") == 0 and record.total("一致") == 3 + len(support.B_STOP_REQUIRED)
    assert optional == {"sub_reason": "起点等于最后一个收盘日", "n": 0}
    for key, wrong in (("tool", "v20-indep-2"), ("kind", "full"), ("name", "confirm_空窗口_later")):
        record = support.Recorder()
        support.compare_b_common_top(record, {**tool, key: wrong}, scenario)
        assert record.summary()["顶层键"][key] == {"不一致": 1}, key
    for missing in ("stop_reason", "name"):
        record = support.Recorder()
        changed = {k: v for k, v in tool.items() if k != missing}
        support.compare_b_common_top(record, changed, scenario)
        support.compare_b_stop_keys(record, changed)
        assert record.total("不一致") >= 1, missing
    record = support.Recorder()
    support.compare_b_stop_keys(record, {**tool, "stop_reason": {"reason": "评价窗口为空"}})
    assert record.summary()["顶层键"]["停止类必需键 stop_reason.detail"] == {"不一致": 1}


def test_wiring_b_bootstrap_numeric_order() -> None:
    """复核第 1 项：多区块（b = 10、20、120 乱序输入）按数值排序后集合比较一致；按字符串排序会得到 [10, 120, 20]，
    与数值排序不同，本例能区分两种排序。"""
    blocks = [120, 10, 20]
    assert sorted(blocks, key=str) != sorted(blocks)                           # 前提：两种排序结果不同
    rows = [support.confirmatory.BootstrapRow(block, 20261000 + block, True, 0.5, -0.1, 0.1, "")
            for block in (10, 20, 120)]
    tool_rows = [{"b": block, "seed": 20261000 + block, "valid": True, "p": 0.5, "q025": -0.1, "q975": 0.1}
                 for block in blocks]
    record = support.Recorder()
    support.compare_b_bootstrap(record, tool_rows, rows)
    assert record.summary()["检验层"]["bootstrap 行（b 集合）"] == {"一致": 1}
    assert record.total("不一致") == 0


def test_wiring_b_decimal_numbers() -> None:
    """补充一第一节第 1 条：工具值为 Decimal 时先 float()：精确相等一致、差在 1e-12 内容差内、超出不一致；
    p 换算后精确比较：相等一致、差 1e-17 不一致。"""
    from decimal import Decimal

    record = support.Recorder()
    assert support.b_number(record, "检验层", "delta", "场景", Decimal("0.13360481864155985"),
                            0.13360481864155985) == "一致"
    assert support.b_number(record, "检验层", "delta", "场景", Decimal("0.01220100092709897"),
                            0.01220100092709896) == support.TOLERANCE_STATUS
    assert support.b_number(record, "检验层", "delta", "场景", Decimal("0.1"), 0.1 + 1e-9) == "不一致"
    later = Decimal("0.17518248175182484")
    assert float(later) != 0.17518248175182483                                  # 前提：差 1e-17 在 float 上可分
    for tool_p, expected in ((Decimal("0.17518248175182483"), {"一致": 1}), (later, {"不一致": 1})):
        record = support.Recorder()
        support.compare_b_bootstrap(record, [{"b": 20, "seed": 20261020, "valid": True, "p": tool_p,
                                              "q025": Decimal("-0.1"), "q975": Decimal("0.1")}],
                                    [support.confirmatory.BootstrapRow(20, 20261020, True, 0.17518248175182483, -0.1,
                                                                       0.1, "")])
        assert record.summary()["检验层"]["bootstrap.p（精确）"] == expected
        assert record.summary()["检验层"]["bootstrap 行（b 集合）"] == {"一致": 1}


def test_wiring_b_reason_exact_without_object() -> None:
    """《接线 B 全量 补充一》第一节第 3 条：项目“主设定无效：n ÷ b < 2（n = …）”↔ 工具 reason 精确等于登记的工具侧文字
    “主设定 n ÷ b < 2”且无 object → 一致；带 object → 不一致；工具文字为项目文字 → 不一致；工具列表无此条 → 不一致。
    d_j 非有限：工具 reason 精确等于“d_j 出现非有限值”且无 object → 一致；带 object → 不一致。"""
    reason = "主设定无效：n ÷ b < 2（n = 30）"
    for entries, expected in (([{"reason": "主设定 n ÷ b < 2"}], (1, 0)),
                              ([{"reason": "主设定 n ÷ b < 2", "object": "检验"}], (0, 1)),
                              ([{"reason": reason}], (0, 1)),
                              ([{"reason": "缺少必需价格", "object": "净值", "missing": []}], (0, 1))):
        record = support.Recorder()
        support.compare_b_reasons(record, entries, reason, [], {}, {})
        assert (record.total("一致"), record.total("不一致")) == expected, entries
    for entries, expected in (([{"reason": "d_j 出现非有限值"}], (1, 0)),
                              ([{"reason": "d_j 出现非有限值", "object": "检验"}], (0, 1))):
        record = support.Recorder()
        support.compare_b_reasons(record, entries, "d_j 出现非有限值", [], {}, {})
        assert (record.total("一致"), record.total("不一致")) == expected, entries


def b_input_facts(stop_class: str | None = None, missing: dict | None = None) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(checks={}, snapshot_built=stop_class is None, missing=missing or {}, axis=[],
                           stop_class=stop_class, stop_reason=None if stop_class is None else "缺少必需价格")


B_INPUT_CHECKS = {"raw_axis": [], "post_cutoff": {"status": "未验证"}, "derived_axis": [], "added_dates": []}


def test_wiring_b_absent_inputs() -> None:
    """补充一第一节第 2 条、补充二第一节第 1 条：工具 confirm 输出缺 inputs 时，“入口停止（缺少必需价格）”与两项缺价日期
    集合均为未比较，不出现一致或不一致；A 的 compare_input_layer 未改（直接调用时照旧按 A 记录）。"""
    tool = {"exit_code": 0, "input_checks": B_INPUT_CHECKS}
    record = support.Recorder()
    support.compare_b_input_layer(record, tool, b_input_facts("MissingPriceEntryError", {"SPX": [DAYS[1]]}), DAYS[-1],
                                  None)
    summary = record.summary()["输入层"]
    for item in ("入口停止（缺少必需价格）", "SPX 缺价日期集合", "QQQ 缺价日期集合"):
        assert summary[item] == {"未比较": 1}, item
    assert all("inputs" in entry["note"] for entry in record.details if entry["item"] in summary)
    record = support.Recorder()
    support.compare_input_layer(record, tool, b_input_facts("MissingPriceEntryError", {"SPX": [DAYS[1]]}), DAYS[-1])
    assert record.summary()["输入层"]["入口停止（缺少必需价格）"] == {"不一致": 1}          # A 原样：缺键读成“未见缺价”


def test_wiring_b_absent_key_classes() -> None:
    """补充二第一节第 4 条：清理表中每一类“不存在”至少一例——stop_reason（退出码 0）、input_checks（停止类）、r1 子键、
    r2[资产] 子键、r2 事件子键，均记未比较，不出现一致或不一致；键齐全时照 A 比较。"""
    record = support.Recorder()
    support.compare_b_input_layer(record, {"exit_code": 0, "input_checks": B_INPUT_CHECKS}, b_input_facts(),
                                  DAYS[-1], None)
    summary = record.summary()["输入层"]
    assert summary["入口停止"] == {"未比较": 1}                                         # stop_reason 不存在
    assert summary["截止日以内原始轴"] == summary["截止日之后"] == {"一致": 1}             # 键存在：照 A
    record = support.Recorder()
    support.compare_b_input_layer(record, {"exit_code": 3, "stop_reason": {"reason": "评价窗口为空"}}, b_input_facts(),
                                  DAYS[-1], None)
    summary = record.summary()["输入层"]
    assert summary["截止日以内原始轴"] == summary["截止日之后"] == {"未比较": 1}           # 停止类无 input_checks
    assert summary["入口停止"] == {"一致": 1}                                            # stop_reason 存在：照 A
    record = support.Recorder()
    support.compare_b_r1(record, "构造组", {"r1": {"computable": True, "ok": False, "mdd_signal": 0.1}}, object())
    assert record.summary()["R1"] == {"r1.mdd_hold": {"未比较": 1}}
    record = support.Recorder()
    support.compare_b_r2(record, "构造组", {"r2": {"SPX": {"events": [{"P": DAYS[0]}]}}}, {"SPX": None, "QQQ": None},
                         {"SPX": False, "QQQ": False})
    summary = record.summary()["R2 判定"]
    assert "r2.SPX.counts" in summary and "r2.SPX.events[0].category" in summary
    assert record.total("一致") == 0 and record.total("不一致") == 0


def test_wiring_b_own_loss_text() -> None:
    """补充一第一节第 5 条：工具文字中的 4 个百分数与 n 和工具本侧数值相符 → 一致；不符 → 不一致；
    取不出 4 个数字或无 n → 未比较（工具文字格式未登记）。项目侧文字须与 own_result_text 重算逐字相等。"""
    import math as m

    tool = {"lnW": {"candidate": -0.05, "reference": -0.08, "hold": -0.02}, "annual_relative_growth": 0.031, "n": 120}
    shown = [f"{m.expm1(v) * 100:+.2f}%" for v in (-0.05, -0.08, -0.02)] + [f"{0.031 * 100:+.2f}%"]
    good = f"候选自身收益 {shown[0]}，参照 {shown[1]}，一直持有 {shown[2]}；年化相对净值增长率 {shown[3]}（n = 120）"
    for text, status in ((good, "一致"), (good.replace("n = 120", "n = 121"), "不一致"),
                         (good.replace(shown[3], "+9.99%"), "不一致"), ("候选自身亏损，参照亏损更多", "未比较"),
                         (good.replace("（n = 120）", ""), "未比较")):
        record = support.Recorder()
        support.compare_b_own_loss(record, text, tool, "项目文字", "项目文字")
        assert record.summary()["检验层"]["own_loss_text 文字与工具本侧数值（工具侧）"] == {status: 1}, text
        assert record.summary()["检验层"]["own_result ↔ own_result_text 重算（项目侧）"] == {"一致": 1}
    record = support.Recorder()
    support.compare_b_own_loss(record, None, tool, "项目文字", "项目文字")
    assert record.summary()["检验层"]["own_loss_text 是否为 null"] == {"不一致": 1}
