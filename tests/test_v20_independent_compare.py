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
                "python": sys.version, "env": {OUT_ENV: os.environ.get(OUT_ENV), FROM_ENV: str(source)}}
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
        else:
            note = "未比较（输入层停止）" if kind == "full" else "只做输入层比对"
            record.add("算法层", "全部", "未比较", name, note=note)
    counts = {status: record.total(status) for status in support.STATUSES}
    counts["窗口末日边界"] = record.window_end()
    result = {"name": name, "kind": kind, "parameters": parameters, "started": started,
              "tool_exit_code": tool.get("exit_code"), "tool_seconds": None, "project_seconds": None,
              "tool_output_sha256": support.sha256(tool_bytes), "counts": counts,
              "by_layer": {layer: items for layer, items in record.summary().items()}, "details": record.details,
              "alignment": project_fields.get("alignment") if isinstance(project_fields, dict) else None,
              "evidence": extra}
    support.put_bytes(record_path, support.dump_json(result))
    assert counts["不一致"] == 0, f"{name} 有 {counts['不一致']} 处不一致，见 {record_path}"


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
