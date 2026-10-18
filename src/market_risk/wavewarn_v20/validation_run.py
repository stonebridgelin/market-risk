"""阶段五验证期与保留期运行的入口（V1 设计稿定稿第三节、第四节、第八节、第九节；V2 指令第四节第 2—5、8、9 条）。
边界侧。

入口：python -B -m market_risk.wavewarn_v20.validation_run [--root <目录>] [--mode validation|holdout]
      [--validation-manifest <路径> --validation-manifest-sha256 <64 位十六进制> --authorization-sha256 <64 位十六进制>]
--root 默认为仓库根目录；--mode 默认 validation。保留期模式默认禁用，须另给后三个参数（第九节）。
登记值不可从命令行或环境变量覆盖。

结构沿用开发期入口（development_run）已验收的做法：审计钩子、预检、信息状态、暂存目录、清单最后生成、改名发布、
包外审计尾段、三类失败处置、S-21 分类。从 development_run 只导入公共类 Audit、Information、WriteRecord、
RunFailure 与公共函数 exception_record、default_root（及公共常量）；它的私有函数不导入，在本模块重写，行为与开发期
入口等价（T-EQV）。模块顶层无副作用：只有本说明、标准库导入、development_run 的上述导入与定义；main 的第一条语句
安装读取审计钩子，钩子装好后才在函数内导入业务模块。

本模块的文件与进程访问只有五类，均经登记入口：配置经 config_v20.load_v20_config；行情经 data_v20.precheck_read 与
data_v20.read_file_bytes；预检源码、配置与锁文件的哈希经 validation_output.PreflightReader（只在第 1 步）；基准文件经
validation_output.read_reference；输出、清单、发布与失败证据经 development_output 的基本函数；git 经 _git（只读三条）。

退出码与开发期相同：0 进程内全部条件满足；1 运行失败且失败证据完整写出；2 预检拒绝（未读入任何行情）；
3 失败且部分失败证据未取得（标准错误列明）；4 收尾后出现文件访问尝试。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import sys
import time
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from market_risk.wavewarn_v20.development_run import (
    COVERAGE_END,
    DONE,
    EXIT_FAILED,
    EXIT_INPUT,
    EXIT_UNEXPECTED,
    NOT_COVERED,
    PACKAGES,
    PARTIAL,
    STAGE_DIAGNOSIS,
    Audit,
    Information,
    RunFailure,
    WriteRecord,
    default_root,
    exception_record,
)

HEX64 = re.compile(r"[0-9a-f]{64}")
STAGE_OVERLAP = "重叠比对"
EXIT_OVERLAP = "重叠状态比对不符"
# 开发窗口覆盖条件只对开发期截止日定义（development_run.COVERAGE_CUTOFF），验证期与保留期不适用。
COVERAGE_NOT_APPLICABLE = {"condition": None, "first_day": None, "satisfied": None,
                           "status": "不适用：开发窗口覆盖条件只对开发期截止日定义"}
# 重叠比对不符的失败记录措辞（V2 指令第四节第 3 条，N162；裁决第（5）项）。
OVERLAP_STATEMENT = {
    "validation": "计为验证期一次性运行已使用；已读入并解析验证期数据，不得称尚未接触验证期",
    "holdout": "计为保留期一次性运行已使用；已读入并解析保留期数据，不得称尚未接触保留期",
}
AUTHORIZATION_NOTE = ("入口只校验格式并原样写入运行记录，作为人工核对授权后的登记凭据；入口不验证授权真伪，"
                      "报告与记录不得称程序已验证授权")
VALID_CATEGORIES = ("A", "B")


class OutputMismatch(Exception):
    """失败目录内容（由完成与部分写出记录推得）与失败清单不一致（与开发期入口同一规则，在此重写）。"""


# ---------------------------------------------------------------------------
# 组合层之后的四步（纯计算；入口按顺序调用，构造测试可直接调用）
# ---------------------------------------------------------------------------


def date_assertions(period: Any, snapshot: Any, result: Any, candidate: Any, offset: int) -> dict:
    """组合层返回后、确认性检验前的日期断言（锁定补充记录（一）第六节第 2 条；V2 指令第四节第 4 条）与 D15 断言：
    j₀、E、f（候选与主参照的首条净值模拟信号）、R2 状态起日（完整记录首日）、首个与最后计入区间逐项与登记相同；
    j₀ ≥ t0 + offset 且 j₀ ≥ κ_全 + 1（t0、κ 取组合层按同一交易日轴、对冻结候选与主参照计算的值）。"""
    window, reference = result.window, result.reference
    outcome = result.candidates[candidate]
    targets = outcome.outcome.targets
    days = tuple(item.day for item in targets)
    checks = [
        ("j₀（first_return_day）", period.first_return_day, window.first_day),
        ("E（last_day）", period.cutoff, window.last_day),
        ("f（候选首条净值模拟信号）", period.first_signal_day, outcome.outcome.signals[0].day),
        ("f（主参照首条净值模拟信号）", period.first_signal_day, reference.outcome.signals[0].day),
        ("R2 状态起日（候选完整记录首日）", period.status_first_day, outcome.system[0].day),
        ("R2 状态起日（主参照完整记录首日）", period.status_first_day, reference.days[0].day),
        ("R2 提示状态首日", period.status_first_day, min(outcome.status)),
        ("首个计入收益区间", tuple(period.first_interval), days[:2]),
        ("最后计入收益区间", tuple(period.last_interval), days[-2:]),
    ]
    rows = [{"item": item, "expected": list(expected) if isinstance(expected, tuple) else expected,
             "actual": list(actual) if isinstance(actual, tuple) else actual, "passed": expected == actual}
            for item, expected, actual in checks]
    d15 = {"j0": window.j0, "t0": window.t0, "offset": offset, "kappa_all": window.kappa_all,
           "j0_min": window.j0_min, "warmup_ok": window.j0 >= window.t0 + offset,
           "convergence_ok": window.j0 >= window.kappa_all + 1}
    passed = all(row["passed"] for row in rows) and d15["warmup_ok"] and d15["convergence_ok"]
    return {"period": period.period, "passed": passed, "checks": rows, "d15": d15, "n": window.n}


def recompute_signals(snapshot: Any, candidate: Any, initial: Any, windows: Any) -> dict:
    """同一进程、同一快照上，用组合层所用的同一组公共状态函数（只导入、不修改）自 t0 计算冻结候选与主参照的
    逐日信号记录（V2 指令第四节第 3 条所写调用方式）。返回 {对象键名: 自 axis[t0 + 1] 起的 SignalRecord 序列}。"""
    from market_risk.wavewarn_v20 import convergence, execution, inputs, reference, research_run, state_machine

    spx = inputs.snapshot_asset_days(snapshot, "SPX", windows)
    qqq = inputs.snapshot_asset_days(snapshot, "QQQ", windows)
    trend = inputs.snapshot_trend_days(snapshot, "SPX", windows)
    t0 = convergence.first_valid_index(spx, qqq, trend)
    k = t0 + 1
    paths = state_machine.run_channels(initial.channels, spx[k:], qqq[k:], trend[k:], candidate.k, candidate.theta,
                                       windows.average)
    evidence = state_machine.evidence_series(spx[k:], qqq[k:], trend[k:], paths)
    system = state_machine.run_system(initial.system, evidence, candidate.k, candidate.h)
    candidate_records = execution.signal_records(system, evidence)
    reference_days = reference.run_reference(initial.reference, trend[k:], windows.average)
    valid = tuple(trend[index].close is not None and trend[index].complete for index in range(len(snapshot.days)))
    reference_records = execution.reference_signal_records(reference_days, valid[k:])
    return {research_run.candidate_key(candidate): candidate_records, research_run.REFERENCE: reference_records}


def internal_consistency(recomputed: Mapping[str, Sequence[Any]], result: Any, candidate: Any) -> list[str]:
    """重算记录中日期为 j₀ − 1 至 E − 1 的部分，须与组合层冻结结果的净值模拟信号逐条相等（全部字段）。"""
    from market_risk.wavewarn_v20 import research_run

    expected = {research_run.candidate_key(candidate): result.candidates[candidate].outcome.signals,
                research_run.REFERENCE: result.reference.outcome.signals}
    problems = []
    for key, signals in expected.items():
        first, last = signals[0].day, signals[-1].day
        part = tuple(item for item in recomputed[key] if first <= item.day <= last)
        if part != tuple(signals):
            problems.append(f"{key}：重算记录与组合层冻结结果在 {first} 至 {last} 之间不逐条相等")
    return problems


def compare_published(rows: Sequence[tuple[str, ...]], recomputed: Mapping[str, Sequence[Any]],
                      axis: Sequence[dt.date], baseline: str, limit: int = 20) -> dict:
    """已发布字段比对（V2 指令第四节第 3 条）：基准中两对象键各自的行即比对范围；日期无重复，且等于交易日轴在其最小与
    最大日期之间的连续切片；重算记录须覆盖这些日期；按（对象, 日期）配对，五列逐字比较。"""
    from market_risk.wavewarn_v20 import validation_output as output

    problems: list[str] = []
    mismatches: list[dict] = []
    keys: dict[str, dict] = {}
    compared = 0
    positions = {day: index for index, day in enumerate(axis)}
    for key, records in recomputed.items():
        own = [row for row in rows if row[1] == key]
        if not own:
            problems.append(f"基准中没有对象键 {key}")
            continue
        days: list[dt.date] = []
        for row in own:
            try:
                day = dt.date.fromisoformat(row[0])
            except ValueError:
                problems.append(f"{key}：基准日期不是 ISO 日期：{row[0]!r}")
                continue
            if day.isoformat() != row[0]:
                problems.append(f"{key}：基准日期写法不是 YYYY-MM-DD：{row[0]!r}")
            days.append(day)
        if len(set(days)) != len(days):
            problems.append(f"{key}：基准日期有重复")
        ordered = sorted(set(days))
        if ordered and (ordered[0] not in positions or ordered[-1] not in positions
                        or tuple(axis[positions[ordered[0]]:positions[ordered[-1]] + 1]) != tuple(ordered)):
            problems.append(f"{key}：基准日期集合不是交易日轴上的连续切片")
        by_day = {item.day: item for item in records}
        absent = [day for day in ordered if day not in by_day]
        if absent:
            problems.append(f"{key}：重算记录没有覆盖基准日期 {absent[0]} 等 {len(absent)} 日")
        keys[key] = {"rows": len(own), "first": ordered[0] if ordered else None,
                     "last": ordered[-1] if ordered else None}
        for row in own:
            try:
                day = dt.date.fromisoformat(row[0])
            except ValueError:
                continue
            if day not in by_day:
                continue
            expected = output.signal_cells(by_day[day])
            for column, actual, wanted in zip(output.SIGNALS_HEADER[2:], row[2:], expected, strict=True):
                compared += 1
                if actual != wanted:
                    if len(mismatches) < limit:
                        mismatches.append({"object": key, "date": row[0], "column": column, "baseline": actual,
                                           "recomputed": wanted})
                    problems.append(f"{key} {row[0]} {column}")
    same = not problems
    return {"baseline": baseline, "keys": keys, "compared_cells": compared, "outcome": "相同" if same else "不符",
            "passed": same, "problem_count": len(problems), "problems": problems[:limit], "mismatches": mismatches,
            "columns": list(output.SIGNALS_HEADER[2:]), "scope": output.OVERLAP_SCOPE}


def confirm(result: Any, candidate: Any, period: Any) -> Any:
    """确认性检验接线（V1 第四节）：confirmatory_input(result, 冻结候选, 期间名) →
    confirmatory_test(输入, 登记参数)。"""
    from market_risk.wavewarn_v20 import confirmatory, research_run

    data = research_run.confirmatory_input(result, candidate, period.period)
    return confirmatory.confirmatory_test(data, period.confirmatory)


def diagnose(result: Any, candidate: Any, rule: Any) -> dict:
    """必报诊断（标准第五节）：全部取自同一次运行的组合层结果；窗口为信号日 [f, E]。"""
    from market_risk.wavewarn_v20 import validation_diagnostics as diagnostics

    outcome = result.candidates[candidate]
    first, last = outcome.outcome.signals[0].day, result.window.last_day
    window = diagnostics.DiagnosticWindow(first, last, tuple(sorted(day for day in outcome.status if day >= first)))
    results = {asset: value for asset, value in outcome.r2.items()}
    if any(value is None for value in results.values()):
        raise diagnostics.DiagnosticsError("R2 无法计算，诊断不计算")
    segments = {asset: tuple(segment for segment, _ in outcome.ledgers[asset].classes) for asset in results}
    states = [(item.day, item.risk) for item in outcome.system]
    return diagnostics.diagnostics(results, segments, states, window, rule)


def holdout_gate(manifest_path: Path, manifest_sha: str, authorization_sha: str, manifest: bytes,
                 files: Mapping[str, bytes]) -> dict:
    """保留期启用条件（V1 第九节；V2 指令第四节第 9 条）：清单哈希等于参数；清单所列 confirmatory.json 与
    daily_signals.csv.gz 的哈希与字节数与清单相同；类别为 A 或 B 且主检验有效；授权哈希只校验格式。
    不满足即抛 ValueError（由预检转为拒绝）。"""
    from market_risk.wavewarn_v20 import validation_output as output

    if not HEX64.fullmatch(authorization_sha):
        raise ValueError("--authorization-sha256 须为 64 位小写十六进制")
    if output.sha256(manifest) != manifest_sha:
        raise ValueError("验证期清单的 SHA-256 与 --validation-manifest-sha256 不符")
    listed = output.parse_manifest(manifest)
    for name in (output.CONFIRMATORY, output.SIGNALS_FILE):
        if name not in listed:
            raise ValueError(f"验证期清单中没有 {name}")
        digest, size = listed[name]
        if (output.sha256(files[name]), len(files[name])) != (digest, size):
            raise ValueError(f"{name} 的哈希或字节数与验证期清单不符")
    document = output.read_json(files[output.CONFIRMATORY])
    if not isinstance(document, dict) or document.get("valid") is not True:
        raise ValueError("验证期确认性检验为计算无效，保留期不得启用")
    category = document.get("category")
    if category not in VALID_CATEGORIES:
        raise ValueError(f"验证期类别为 {category}，不是 A 或 B，保留期不得启用")
    main = document.get("main")
    if not isinstance(main, dict) or main.get("valid") is not True:
        raise ValueError("验证期主检验无效，保留期不得启用")
    return {"validation_manifest": str(manifest_path), "validation_manifest_sha256": manifest_sha,
            "validation_category": category, "validation_main_valid": True,
            "authorization_sha256": authorization_sha, "authorization_note": AUTHORIZATION_NOTE}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="波段预警 v2.0 验证期与保留期运行（阶段五）")
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--mode", choices=("validation", "holdout"), default="validation")
    parser.add_argument("--validation-manifest", type=Path, default=None)
    parser.add_argument("--validation-manifest-sha256", default=None)
    parser.add_argument("--authorization-sha256", default=None)
    return parser


def _baseline_paths(root: Path, arguments: argparse.Namespace, output: Any) -> list[Path]:
    """基准文件的路径（审计钩子据此放行读取）：验证期为 M3 正式目录中的清单与逐日信号；保留期为参数给出的验证期清单
    及同目录中的 confirmatory.json 与 daily_signals.csv.gz（参数缺失时为空）。"""
    if arguments.mode == "validation":
        return list(output.development_baseline(root))
    if arguments.validation_manifest is None:
        return []
    manifest = Path(os.path.abspath(arguments.validation_manifest))
    return [manifest, manifest.parent / output.CONFIRMATORY, manifest.parent / output.SIGNALS_FILE]


def main(argv: list[str] | None = None) -> int:
    """验证期与保留期运行入口。返回退出码（以 __main__ 方式执行时交 sys.exit）。"""
    sys.addaudithook(audit := Audit())
    started, start_clock = dt.datetime.now(dt.UTC), time.monotonic()
    arguments = _parser().parse_args(argv)
    root: Path = arguments.root
    from market_risk.storage.paths import StoragePaths
    from market_risk.wavewarn_v20 import config_v20, data_v20, registered_v20, research_run
    from market_risk.wavewarn_v20 import development_compare as compare
    from market_risk.wavewarn_v20 import registered_v20_validation as registered
    from market_risk.wavewarn_v20 import validation_output as output

    period = registered.PERIODS[arguments.mode]
    candidate = registered.FROZEN_CANDIDATE
    market_files = [StoragePaths(root).market_daily_file(asset) for asset in config_v20.ASSETS]
    audit.configure(root, market_files)
    baseline_paths = _baseline_paths(root, arguments, output)
    audit.allow(baseline_paths)                                         # 预检中读取基准文件（受保护目录内的只读）
    info = Information()
    info.window_coverage = dict(COVERAGE_NOT_APPLICABLE)
    stdout_report: dict = {"root": str(root), "mode": period.mode}

    # ---------------- 第 1 步：预检 ----------------
    info.enter("预检")
    reader = output.PreflightReader()
    try:
        commit, places, preflight, baseline = _preflight(root, started, period, arguments, reader, output, registered)
    except output.PreflightError as error:
        reader.close()
        info.exception = exception_record(error)
        stdout_report.update({"exit_code": 2, "outcome": "预检拒绝", "reason": str(error),
                              "information_state": info.document(), "open_events": audit.events})
        _print(stdout_report)
        sys.stderr.write(f"预检拒绝：{error}\n")
        return 2
    reader.close()
    info.leave("预检")
    audit.allow([places.staging, places.failed, places.formal, places.tail, places.publish_failure, *baseline_paths])
    stdout_report.update({"staging": str(places.staging), "formal": str(places.formal), "tail": places.tail.name})
    allowed = output.readable_roots(places)
    usage = info.usage_restriction
    stdout_report.update({"usage_restriction": usage, "window_coverage": info.window_coverage})
    cutoff_seq: int | None = None
    files_written: dict[str, bytes] = {}
    writes = WriteRecord()
    manifest_data: bytes | None = None
    diagnosing: dict = {}

    # ---------------- 第 2 步起 ----------------
    try:
        info.enter("建暂存目录")
        output.make_directory(places.staging)
        info.leave("建暂存目录")

        info.enter("加载配置")
        config = config_v20.load_v20_config(root)
        for asset in config_v20.ASSETS:
            if config.registered[asset].first_date != registered_v20.HISTORIES[asset]:
                raise RunFailure(EXIT_INPUT, "加载配置", None, {
                    "detail": f"{asset} 的登记历史起点 {registered_v20.HISTORIES[asset]} 与配置 first_date "
                              f"{config.registered[asset].first_date} 不同"})
        info.leave("加载配置")

        info.enter("读取行情")
        cutoff = period.cutoff
        series: dict = {}
        evidence: dict = {}
        for asset, path in zip(config_v20.ASSETS, market_files, strict=True):
            entry = config.registered[asset]
            data_v20.precheck_read(asset, cutoff, entry)
            raw = data_v20.read_file_bytes(path)
            info.data_read[asset] = {"read": True, "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                                     "read_event_seq": audit.seq_of(path),
                                     "layers": {"byte_read": "整份文件", "price_parse": f"≤ {cutoff.isoformat()}",
                                                "compute": f"≤ {cutoff.isoformat()}"}}
            parsed = info.parsed.setdefault(asset, {"strict_started": True, "strict_completed": False,
                                                    "diagnosis_started": False, "diagnosis_completed": False})
            try:
                item = data_v20.parse_until(raw, asset, cutoff, entry, config.decisions)
            except data_v20.DataInputError as error:
                raise RunFailure(EXIT_INPUT, "严格读取", error, {"asset": asset}) from error
            except data_v20.MissingPriceEntryError as strict_error:
                strict = {"type": type(strict_error).__name__, "reason": strict_error.reason,
                          "message": str(strict_error), "asset": strict_error.asset,
                          "missing_value_days": list(strict_error.missing_value_days),
                          "missing_row_days": list(strict_error.missing_row_days),
                          "extra_days": list(strict_error.extra_days)}
                parsed["diagnosis_started"] = True
                evidence[asset] = {"strict_error": strict}
                diagnosing.update({"asset": asset, "strict_error": strict})
                try:
                    diagnosis = data_v20.diagnose_until(raw, asset, cutoff, entry, config.decisions)
                except data_v20.MissingPriceEntryError as error:
                    evidence[asset]["diagnosis_error"] = exception_record(error)
                    raise RunFailure(EXIT_UNEXPECTED, STAGE_DIAGNOSIS, error, {
                        "asset": asset, "evidence": evidence, "detail": "读取实现不一致"}) from error
                except data_v20.DataEntryError as error:
                    evidence[asset]["diagnosis_error"] = exception_record(error)
                    raise RunFailure(EXIT_INPUT, "诊断读取", error, {"asset": asset, "evidence": evidence}) from error
                diagnosing.clear()
                checks = [{"kind": "missing_value_days", "day": day, "result": "是" if day in
                           diagnosis.missing_value_days else "否"} for day in strict_error.missing_value_days]
                checks += [{"kind": "missing_row_days", "day": day, "result": "是" if day in
                            diagnosis.missing_row_days else "否"} for day in strict_error.missing_row_days]
                evidence[asset].update({"diagnosis": {"missing_value_days": list(diagnosis.missing_value_days),
                                                      "missing_row_days": list(diagnosis.missing_row_days)},
                                        "evidence_check": checks})
                parsed["diagnosis_completed"] = True
                if any(check["result"] == "否" for check in checks):
                    raise RunFailure(EXIT_UNEXPECTED, "诊断核实", None, {
                        "asset": asset, "evidence": evidence, "detail": "读取实现不一致"}) from strict_error
                if output.PREFIX_USAGE not in usage:
                    usage.append(output.PREFIX_USAGE)
                item = diagnosis.series
            else:
                parsed["strict_completed"] = True
                info.data_read[asset]["normalized_sha256_verified"] = entry.normalized_sha256
            series[asset] = item
            closes = list(item.closes)
            parsed.update({"rows": len(closes), "first_day": closes[0] if closes else None,
                           "last_day": closes[-1] if closes else None})
        info.leave("读取行情")

        info.enter("组装快照")
        snapshot = data_v20.assemble_snapshot(series, cutoff, started).snapshot
        info.leave("组装快照")

        info.enter("组合层")
        info.composition_called = True
        parameters = registered_v20.run_parameters()
        result = research_run.run_window(snapshot, registered.window_spec(period), parameters, (candidate,),
                                         journal=info.journal)
        info.leave("组合层")
        if result.stop is not None:
            raise RunFailure(result.stop.exit.value, "组合层", None,
                             {"stop": {"exit": result.stop.exit.value, "object": result.stop.object,
                                       "stage": result.stop.stage, "exception_type": result.stop.exception_type,
                                       "reason_code": result.stop.reason_code, "message": result.stop.message,
                                       "traceback": result.stop.traceback}})

        info.enter("日期断言")
        try:
            dates = date_assertions(period, snapshot, result, candidate, parameters.offset)
        except Exception as error:
            raise RunFailure(EXIT_FAILED, "日期断言", error) from error
        info.outer("日期断言", "日期与 D15 断言", "日期断言")
        if not dates["passed"]:
            raise RunFailure(EXIT_FAILED, "日期断言", None, {"date_assertions": dates})
        info.leave("日期断言")

        info.enter(STAGE_OVERLAP)
        try:
            recomputed = recompute_signals(snapshot, candidate, registered.INITIAL, parameters.windows)
            consistency = internal_consistency(recomputed, result, candidate)
        except Exception as error:
            raise RunFailure(EXIT_FAILED, STAGE_OVERLAP, error) from error
        info.outer(STAGE_OVERLAP, "重算记录", STAGE_OVERLAP)
        if consistency:
            raise RunFailure(EXIT_FAILED, STAGE_OVERLAP, None, {"internal_consistency": consistency})
        try:
            rows = output.parse_signals(baseline["signals"])
        except Exception as error:
            raise RunFailure(EXIT_OVERLAP, STAGE_OVERLAP, error, {
                "statement": OVERLAP_STATEMENT[period.mode], "repair": "修复须新登记"}) from error
        overlap = compare_published(rows, recomputed, snapshot.days, baseline["name"])
        overlap["baseline_sha256"] = baseline["signals_sha256"]
        info.outer(STAGE_OVERLAP, "已发布字段比对", STAGE_OVERLAP)
        if not overlap["passed"]:
            raise RunFailure(EXIT_OVERLAP, STAGE_OVERLAP, None, {
                "overlap": overlap, "statement": OVERLAP_STATEMENT[period.mode], "repair": "修复须新登记"})
        info.leave(STAGE_OVERLAP)

        info.enter("确认性检验")
        try:
            tested = confirm(result, candidate, period)
            confirmatory = output.confirmatory_document(tested, period)
        except Exception as error:
            raise RunFailure(EXIT_FAILED, "确认性检验", error) from error
        info.outer("确认性检验", "检验结果", "确认性检验")
        info.leave("确认性检验")

        info.enter("必报诊断")
        if tested.valid:
            try:
                diagnostics = output.diagnostics_document(diagnose(result, candidate, registered_v20.R2_RULE), None)
            except Exception as error:
                raise RunFailure(EXIT_FAILED, "必报诊断", error) from error
            info.outer("必报诊断", "D-1—D-3", "必报诊断")
        else:
            diagnostics = output.diagnostics_document(None, f"计算无效（{tested.reason}）时不计算本节")
        info.leave("必报诊断")

        info.enter("描述性对照")
        try:
            averages = (compare.average_comparison(snapshot, result, parameters, False),
                        compare.average_comparison(snapshot, result, parameters, True))
            constants = (compare.constant_exposure(research_run.REFERENCE, result.reference.outcome.targets, result,
                                                   parameters),
                         compare.constant_exposure(research_run.candidate_key(candidate),
                                                   result.candidates[candidate].outcome.targets, result, parameters))
            descriptive = output.descriptive_document(result, averages, constants)
        except Exception as error:
            raise RunFailure(EXIT_FAILED, "描述性对照", error) from error
        info.outer("描述性对照", "描述性对照", "描述性对照")
        info.leave("描述性对照")

        info.enter("序列化")
        try:
            record = _run_record(commit, preflight, period, registered, registered_v20, info, audit, usage, output,
                                 baseline)
            stable = output.stable_files(snapshot, result, period, record, dates, overlap, confirmatory, diagnostics,
                                         descriptive, usage, baseline.get("gate"),
                                         lambda name: info.outer("序列化", name, "序列化"))
        except Exception as error:
            raise RunFailure(EXIT_FAILED, "序列化", error) from error
        info.leave("序列化")

        info.enter("写出")
        for name, data in stable.items():
            writes.write(output, places.staging / name, data)
            files_written[name] = data
            info.completed.append(name)
        state = output.json_bytes(info.document())
        writes.write(output, places.staging / output.INFORMATION_STATE, state)
        files_written[output.INFORMATION_STATE] = state
        environment = output.json_bytes(_environment(started, start_clock, root, places, period))
        writes.write(output, places.staging / output.RUN_ENVIRONMENT, environment)
        files_written[output.RUN_ENVIRONMENT] = environment
        cutoff_seq = len(audit.events)
        audit_log = _audit_log(audit, cutoff_seq)
        writes.write(output, places.staging / output.AUDIT_LOG, audit_log)
        files_written[output.AUDIT_LOG] = audit_log
        output.verify_written(places.staging, files_written, allowed)
        manifest_data = output.manifest_bytes(files_written)
        writes.write(output, places.staging / output.MANIFEST, manifest_data)
    except RunFailure as failure:
        return _fail(failure, None, audit, info, places, files_written, writes, cutoff_seq, usage, allowed, output,
                     stdout_report)
    except Exception as error:
        trace = traceback.format_exc()
        if diagnosing:
            return _fail(RunFailure(EXIT_UNEXPECTED, STAGE_DIAGNOSIS, error, {
                "asset": diagnosing["asset"], "strict_error": diagnosing["strict_error"],
                "diagnosis_error": exception_record(error, trace)}), trace, audit, info, places, files_written,
                writes, cutoff_seq, usage, allowed, output, stdout_report)
        return _fail(RunFailure(EXIT_UNEXPECTED, _current_stage(info), error), trace, audit, info,
                     places, files_written, writes, cutoff_seq, usage, allowed, output, stdout_report)

    # ---------------- 清单生成之后：读回复算、违规检查、改名、尾段 ----------------
    manifest_sha = output.sha256(manifest_data)
    try:
        output.verify_manifest(places.staging, output.MANIFEST, allowed)
        if audit.violations(cutoff_seq + 1, len(audit.events)):
            raise RunFailure(EXIT_UNEXPECTED, "清单生成之后", None, {"detail": "截止点之后发现违规打开"})
        output.rename_directory(places.staging, places.formal)
        writes.moved(places.staging, places.formal)
    except RunFailure as failure:
        return _fail_after_manifest(failure, None, audit, info, places, writes, cutoff_seq, manifest_sha, usage,
                                    allowed, output, stdout_report)
    except Exception as error:
        if output.path_exists(places.formal) and not output.path_exists(places.staging):
            writes.moved(places.staging, places.formal)
            return _fail_published(RunFailure(EXIT_UNEXPECTED, "改名", error), audit, info, places, writes,
                                   cutoff_seq, manifest_sha, usage, allowed, output, stdout_report)
        return _fail_after_manifest(RunFailure(EXIT_UNEXPECTED, "清单生成之后", error), traceback.format_exc(), audit,
                                    info, places, writes, cutoff_seq, manifest_sha, usage, allowed, output,
                                    stdout_report)
    if audit.violations(1, len(audit.events)) or len(audit.events) <= cutoff_seq:
        return _fail_published(RunFailure(EXIT_UNEXPECTED, "审计尾段之前", None, {"detail": "违规或衔接条件不满足"}),
                               audit, info, places, writes, cutoff_seq, manifest_sha, usage, allowed, output,
                               stdout_report)
    try:
        _, success = _write_tail(audit, places, places.formal, cutoff_seq, output.MANIFEST, manifest_sha, None, True,
                                 None, writes, allowed, output)
    except Exception as error:
        return _fail_published(RunFailure(EXIT_UNEXPECTED, "审计尾段", error), audit, info, places, writes,
                               cutoff_seq, manifest_sha, usage, allowed, output, stdout_report)
    if not success:
        sys.stderr.write(f"审计尾段收尾记录为“失败”（衔接校验或违规检查不通过）；正式目录 {places.formal}；"
                         f"{output.NOT_A_SUCCESS}\n")
        stdout_report.update({"outcome": "发布后失败", "statement": output.NOT_A_SUCCESS})
        return _finish(1, audit, stdout_report)
    stdout_report.update({"exit_code": 0, "outcome": "发布完成（进程内条件满足）", "manifest_sha256": manifest_sha,
                          "usage_restriction": usage})
    return _finish(0, audit, stdout_report)


# ---------------------------------------------------------------------------
# 重写的私有部分（与 development_run 的同名私有函数行为等价；T-EQV）
# ---------------------------------------------------------------------------


def _current_stage(info: Information) -> str:
    entered = [item["stage"] for item in info.trace if item["action"] == "进入"]
    left = [item["stage"] for item in info.trace if item["action"] == "离开"]
    open_stages = [stage for stage in entered if entered.count(stage) > left.count(stage)]
    return open_stages[-1] if open_stages else "未知"


def _print(report: dict) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(report, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _finish(code: int, audit: Audit, report: dict) -> int:
    """收尾后只向已打开的标准输出、标准错误写入结论；封存状态下出现过访问尝试即退出码 4。"""
    if audit.sealed_attempts:
        sys.stderr.write("收尾后出现文件访问尝试，审计尾段未覆盖\n")
        code = 4
    report["exit_code"] = code
    _print(report)
    return code


def _git(root: Path, *arguments: str) -> tuple[int, str]:
    """预检中的 git（只读三条：rev-parse HEAD、status --porcelain --untracked-files=all、
    diff --cached --name-only）。"""
    import subprocess

    done = subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=False)
    return done.returncode, done.stdout.decode("utf-8", errors="replace")


def _check_holdout_arguments(arguments: argparse.Namespace, output: Any) -> None:
    """保留期模式的三个参数：缺任一即拒绝；两个哈希须为 64 位小写十六进制（只校验格式）。"""
    missing = [name for name, value in (("--validation-manifest", arguments.validation_manifest),
                                        ("--validation-manifest-sha256", arguments.validation_manifest_sha256),
                                        ("--authorization-sha256", arguments.authorization_sha256))
               if value is None]
    if missing:
        raise output.PreflightError(f"保留期模式默认禁用：缺少参数 {'、'.join(missing)}")
    for name, value in (("--validation-manifest-sha256", arguments.validation_manifest_sha256),
                        ("--authorization-sha256", arguments.authorization_sha256)):
        if not HEX64.fullmatch(value):
            raise output.PreflightError(f"{name} 须为 64 位小写十六进制：{value!r}")


def _baseline(root: Path, arguments: argparse.Namespace, output: Any, registered: Any) -> dict:
    """基准文件的核对（预检中，任何行情读取之前）。验证期：M3 清单的 SHA-256 等于登记值；清单所列逐日信号的哈希与
    字节数等于登记值；逐日信号文件本身的 SHA-256 等于登记值。保留期：启用条件（holdout_gate）。"""
    if arguments.mode == "validation":
        manifest_path, signals_path = output.development_baseline(root)
        try:
            manifest = output.read_reference(manifest_path)
        except OSError as error:
            raise output.PreflightError(f"M3 清单不可读：{error}") from error
        if output.sha256(manifest) != registered.M3_MANIFEST_SHA256:
            raise output.PreflightError("M3 清单的 SHA-256 与登记值不符")
        try:
            listed = output.parse_manifest(manifest)
        except (ValueError, UnicodeDecodeError) as error:
            raise output.PreflightError(f"M3 清单不可解析：{error}") from error
        if listed.get(registered.SIGNALS_NAME) != (registered.M3_SIGNALS_SHA256, registered.M3_SIGNALS_BYTES):
            raise output.PreflightError("M3 清单所列逐日信号的哈希或字节数与登记值不符")
        try:
            signals = output.read_reference(signals_path)
        except OSError as error:
            raise output.PreflightError(f"M3 逐日信号不可读：{error}") from error
        if output.sha256(signals) != registered.M3_SIGNALS_SHA256 or len(signals) != registered.M3_SIGNALS_BYTES:
            raise output.PreflightError("M3 逐日信号的 SHA-256 或字节数与登记值、清单不符")
        return {"name": f"{registered.M3_FORMAL_NAME}/{registered.SIGNALS_NAME}", "signals": signals,
                "signals_sha256": output.sha256(signals), "manifest_sha256": output.sha256(manifest), "gate": None}
    paths = _baseline_paths(root, arguments, output)
    try:
        manifest = output.read_reference(paths[0])
        files = {output.CONFIRMATORY: output.read_reference(paths[1]),
                 output.SIGNALS_FILE: output.read_reference(paths[2])}
    except OSError as error:
        raise output.PreflightError(f"验证期正式输出不可读：{error}") from error
    try:
        gate = holdout_gate(paths[0], arguments.validation_manifest_sha256, arguments.authorization_sha256, manifest,
                            files)
    except (ValueError, UnicodeDecodeError) as error:
        raise output.PreflightError(f"保留期启用条件不满足：{error}") from error
    signals = files[output.SIGNALS_FILE]
    return {"name": f"验证期正式输出/{output.SIGNALS_FILE}", "signals": signals,
            "signals_sha256": output.sha256(signals),
            "manifest_sha256": output.sha256(manifest), "gate": gate}


def _preflight(root: Path, started: dt.datetime, period: Any, arguments: argparse.Namespace, reader: Any,
               output: Any, registered: Any) -> tuple[str, Any, dict, dict]:
    """第 1 步：保留期参数（格式）；git 三条只读命令；目录与包外文件名无冲突；源码、配置与锁文件哈希；依赖版本；
    基准文件核对。不读取任何行情。"""
    if arguments.mode == "holdout":
        _check_holdout_arguments(arguments, output)
    code, head = _git(root, "rev-parse", "HEAD")
    commit = head.strip()
    if code != 0 or len(commit) != 40:
        raise output.PreflightError(f"git rev-parse HEAD 失败：退出码 {code}")
    for command, name in ((("status", "--porcelain", "--untracked-files=all"), "工作区不干净"),
                          (("diff", "--cached", "--name-only"), "暂存区不为空")):
        code, text = _git(root, *command)
        if code != 0 or text.strip():
            raise output.PreflightError(f"{name}：退出码 {code}")
    places = output.locations(root, period.formal_name, started.strftime("%Y%m%dT%H%M%SZ"), commit[:7])
    for path, name in ((places.formal, "正式目录已存在"), (places.staging, "暂存目录名已存在"),
                       (places.failed, "失败目录名已存在"), (places.tail, "审计尾段文件名已存在"),
                       (places.publish_failure, "发布失败说明文件名已存在")):
        if output.path_exists(path):
            raise output.PreflightError(f"{name}：{path}")
    hashes = {}
    for relative in output.PREFLIGHT_FILES:
        try:
            data = reader.read(root, relative)
        except OSError as error:
            raise output.PreflightError(f"预检文件缺失或不可读：{relative}：{error}") from error
        hashes[relative] = hashlib.sha256(data).hexdigest()
    versions = {}
    for package in PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    versions["python"] = sys.version.split()[0]
    baseline = _baseline(root, arguments, output, registered)
    return commit, places, {"source_hashes": hashes, "dependency_versions": versions}, baseline


def _shared_parameters(registered_v20: Any) -> dict:
    """共用登记值（取自 registered_v20.describe 中与评价期无关的部分）。"""
    described = registered_v20.describe()
    return {key: described[key] for key in ("initial_states", "input_windows", "positions", "policy",
                                            "r2_thresholds", "r2_rule", "offset", "tolerance", "r1_ratio",
                                            "histories", "continuity")}


def _run_record(commit: str, preflight: dict, period: Any, registered: Any, registered_v20: Any, info: Information,
                audit: Audit, usage: list[str], output: Any, baseline: dict) -> dict:
    hashes = preflight["source_hashes"]
    return {
        "output_scheme": output.OUTPUT_SCHEME,
        "commit": commit,
        "dependency_versions": preflight["dependency_versions"],
        "source_hashes": hashes,
        "registered_v20_sha256": hashes["src/market_risk/wavewarn_v20/registered_v20.py"],
        "registered_v20_validation_sha256": hashes["src/market_risk/wavewarn_v20/registered_v20_validation.py"],
        "registered_parameters": registered.describe(period),
        "shared_registered_parameters": _shared_parameters(registered_v20),
        "data_files": {asset: {"bytes": item["bytes"], "raw_sha256": item["raw_sha256"],
                               "normalized_sha256_verified": item.get("normalized_sha256_verified")}
                       for asset, item in info.data_read.items()},
        "data_scope": output.data_scope_text(period),
        "cutoff": period.cutoff,
        "baseline": {"name": baseline["name"], "signals_sha256": baseline["signals_sha256"],
                     "manifest_sha256": baseline["manifest_sha256"]},
        "holdout_gate": baseline["gate"],
        "usage_restriction": list(usage),
        "window_coverage": dict(info.window_coverage),
        "audit_conclusion": {"violation_found_until_this_file": audit.violations(1, len(audit.events)) > 0,
                             "note": "成功路径上任何违规都会使运行失败；完整结论以审计尾段收尾记录为准"},
        "audit_coverage": {"coverage_end": COVERAGE_END, "not_covered": list(NOT_COVERED)},
    }


def _environment(started: dt.datetime, start_clock: float, root: Path, places: Any, period: Any) -> dict:
    ended = dt.datetime.now(dt.UTC)
    return {"argv": list(sys.argv), "executable": sys.executable, "acquired_at": started, "started_at": started,
            "ended_at": ended, "seconds": time.monotonic() - start_clock,
            "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"), "root": str(root),
            "mode": period.mode, "staging": str(places.staging), "formal": str(places.formal)}


def _audit_log(audit: Audit, last: int) -> bytes:
    """包内段：从钩子安装（序号 1）至截止点（开始写出本文件之前）的事件原文；末行为覆盖记录。只写一次。"""
    lines = [json.dumps(item, ensure_ascii=False) for item in audit.events[:last]]
    lines.append(json.dumps({"record": "coverage", "start": "hook_installed",
                             "cutoff_stage": "before_writing_audit_log", "first_seq": 1, "last_seq": last,
                             "violations_in_segment": audit.violations(1, last)},
                            ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_tail(audit: Audit, places: Any, package: Path, last: int | None, manifest_name: str | None,
                manifest_sha: str | None, frozen_sha: str | None, success_requested: bool, failure_note: str | None,
                writes: WriteRecord, allowed: tuple, output: Any) -> tuple[bool, bool]:
    """包外审计尾段：先读取包内 audit_log.jsonl 与适用清单计算哈希并做衔接校验；再以独占方式新建尾段文件（该次打开
    记入尾段）；在内存中组成衔接记录、事件、收尾记录；一次写入并关闭；随即封存。返回（是否写出，收尾记录是否为“成功”）。"""
    audit_sha = None
    if last is not None:
        audit_sha = output.sha256(output.read_back(package / output.AUDIT_LOG, allowed))
    if manifest_name is not None:
        manifest_sha = output.sha256(output.read_back(package / manifest_name, allowed))
    outcome: dict = {}

    def compose() -> bytes:
        first = (last or 0) + 1
        events = audit.events[first - 1:]
        contiguous = [item["seq"] for item in events] == list(range(first, first + len(events)))
        violations = audit.violations(first, len(audit.events))
        linked = last is not None and contiguous and bool(events) and events[0]["seq"] == last + 1
        success = success_requested and linked and violations == 0 and (
            audit.violations(1, last or 0) == 0)
        outcome["success"] = success
        link = {"record": "link", "package_dir": str(package), "audit_log_sha256": audit_sha,
                "audit_log_last_seq": last, "manifest_file": manifest_name, "manifest_sha256": manifest_sha}
        if frozen_sha is not None:
            link["frozen_manifest_sha256"] = frozen_sha
        close = {"record": "close", "last_seq": len(audit.events), "violations_in_tail": violations,
                 "outcome": "成功" if success else "失败", "failure_note": failure_note,
                 "coverage_end": COVERAGE_END, "not_covered": list(NOT_COVERED)}
        lines = [json.dumps(link, ensure_ascii=False), *(json.dumps(item, ensure_ascii=False) for item in events),
                 json.dumps(close, ensure_ascii=False)]
        return ("\n".join(lines) + "\n").encode("utf-8")

    writes.write(output, places.tail, compose, audit.seal)
    return True, bool(outcome.get("success"))


def _failure_document(failure: RunFailure, trace: str | None, usage: list[str], coverage: dict, extra: dict,
                      missing: list[str], incomplete: list[dict]) -> dict:
    return {"exit": failure.exit_text, "stage": failure.stage, "exception": exception_record(failure.error, trace),
            "detail": failure.detail, "usage_restriction": list(usage), "window_coverage": dict(coverage),
            "missing_evidence": missing,
            "incomplete_files": incomplete, **extra}


def _read_incomplete(directory: Path, writes: WriteRecord, files: dict[str, bytes], allowed: tuple,
                     output: Any) -> list[dict]:
    incomplete = [{"file": name, "status": PARTIAL} for name in writes.in_directory(directory, PARTIAL)]
    for item in incomplete:
        files[item["file"]] = output.read_back(directory / item["file"], allowed)
    return incomplete


def _content_mismatch(directory: Path, writes: WriteRecord, files: dict[str, bytes], output: Any) -> str | None:
    derived = set(writes.in_directory(directory, DONE, PARTIAL))
    listed = {*files, output.FAILURE_MANIFEST}
    if derived == listed:
        return None
    return (f"失败目录内容与清单不一致：记录有而清单无 {sorted(derived - listed)}；"
            f"清单有而记录无 {sorted(listed - derived)}")


def _fail(failure: RunFailure, trace: str | None, audit: Audit, info: Information, places: Any,
          written: dict[str, bytes], writes: WriteRecord, cutoff_seq: int | None, usage: list[str], allowed: tuple,
          output: Any, report: dict) -> int:
    """清单生成之前失败：已写文件保留（部分写出的原样保留、读回后列入失败清单）；新写 failure.json 与
    information_state_final.json；尚未写出 audit_log.jsonl 时先写出；生成 FAILURE_MANIFEST.sha256；核对目录内容与
    清单一致后，暂存目录改名为失败目录；写尾段。"""
    info.exception = exception_record(failure.error, trace)
    report.update({"outcome": "运行失败", "exit": failure.exit_text, "stage": failure.stage})
    staging = places.staging
    planned = [output.FAILURE, output.INFORMATION_STATE_FINAL,
               *([output.AUDIT_LOG] if cutoff_seq is None else []), output.FAILURE_MANIFEST]
    if not output.path_exists(staging):
        sys.stderr.write(f"运行失败（{failure.exit_text}，{failure.stage}），暂存目录未建立；"
                         f"{_evidence_text(report, staging, [], planned)}\n")
        return _tail_then_finish(3, audit, places, staging, None, None, None, None, writes, allowed, output, report)
    missing: list[str] = []
    files: dict[str, bytes] = dict(written)
    try:
        incomplete = _read_incomplete(staging, writes, files, allowed, output)
    except Exception as error:
        unread = [f"{name}（部分写出，读回失败）" for name in writes.in_directory(staging, PARTIAL)
                  if name not in files]
        return _evidence_missing(error, audit, places, staging, writes.present(staging), [*unread, *planned],
                                 cutoff_seq, writes, allowed, output, report)
    report["incomplete_files"] = incomplete
    try:
        failure_data = output.json_bytes(_failure_document(failure, trace, usage, info.window_coverage, {}, missing,
                                                           incomplete))
        writes.write(output, staging / output.FAILURE, failure_data)
        files[output.FAILURE] = failure_data
        final = output.json_bytes(info.document())
        writes.write(output, staging / output.INFORMATION_STATE_FINAL, final)
        files[output.INFORMATION_STATE_FINAL] = final
        if cutoff_seq is None:
            cutoff_seq = len(audit.events)
            audit_log = _audit_log(audit, cutoff_seq)
            writes.write(output, staging / output.AUDIT_LOG, audit_log)
            files[output.AUDIT_LOG] = audit_log
        listing = output.manifest_bytes(files)
        writes.write(output, staging / output.FAILURE_MANIFEST, listing)
    except Exception as error:
        return _evidence_missing(error, audit, places, staging, writes.present(staging),
                                 [name for name in planned if not writes.done(staging / name)], cutoff_seq, writes,
                                 allowed, output, report)
    if (mismatch := _content_mismatch(staging, writes, files, output)) is not None:
        return _evidence_missing(OutputMismatch(mismatch), audit, places, staging, writes.present(staging), [],
                                 cutoff_seq, writes, allowed, output, report)
    try:
        output.rename_directory(staging, places.failed)
    except Exception as error:
        sys.stderr.write(f"失败目录改名未完成，保留暂存目录原名：{type(error).__name__}: {error}；"
                         f"{_evidence_text(report, staging, writes.present(staging), [])}\n")
        return _tail_then_finish(3, audit, places, staging, cutoff_seq, output.FAILURE_MANIFEST, None, None, writes,
                                 allowed, output, report)
    writes.moved(staging, places.failed)
    report["failed_dir"] = str(places.failed)
    return _tail_then_finish(1, audit, places, places.failed, cutoff_seq, output.FAILURE_MANIFEST, None,
                             None, writes, allowed, output, report)


def _fail_after_manifest(failure: RunFailure, trace: str | None, audit: Audit, info: Information, places: Any,
                         writes: WriteRecord, cutoff_seq: int | None, manifest_sha: str, usage: list[str],
                         allowed: tuple, output: Any, report: dict) -> int:
    """清单生成之后、改名之前失败：冻结包原样保留；新写 failure.json（明记清单哈希与“该清单不覆盖本失败目录的最终内容”）
    与 information_state_final.json；生成 FAILURE_MANIFEST.sha256；核对目录内容与清单一致后改名为失败目录；写尾段。"""
    info.exception = exception_record(failure.error, trace)
    report.update({"outcome": "运行失败", "exit": failure.exit_text, "stage": failure.stage})
    staging = places.staging
    planned = [output.FAILURE, output.INFORMATION_STATE_FINAL, output.FAILURE_MANIFEST]
    files: dict[str, bytes] = {}
    try:
        listed = output.parse_manifest(output.read_back(staging / output.MANIFEST, allowed))
        files = {name: output.read_back(staging / name, allowed) for name in listed}
        files[output.MANIFEST] = output.read_back(staging / output.MANIFEST, allowed)
        incomplete = _read_incomplete(staging, writes, files, allowed, output)
        report["incomplete_files"] = incomplete
        extra = {"manifest_sha256": manifest_sha, "manifest_stage": "清单生成之后、改名之前",
                 "manifest_scope": "该清单不覆盖本失败目录的最终内容；适用版本为“发布前候选内容”，状态为“未发布”"}
        failure_data = output.json_bytes(_failure_document(failure, trace, usage, info.window_coverage, extra, [],
                                                           incomplete))
        writes.write(output, staging / output.FAILURE, failure_data)
        files[output.FAILURE] = failure_data
        final = output.json_bytes(info.document())
        writes.write(output, staging / output.INFORMATION_STATE_FINAL, final)
        files[output.INFORMATION_STATE_FINAL] = final
        writes.write(output, staging / output.FAILURE_MANIFEST, output.manifest_bytes(files))
    except Exception as error:
        return _evidence_missing(error, audit, places, staging, writes.present(staging),
                                 [name for name in planned if not writes.done(staging / name)], cutoff_seq, writes,
                                 allowed, output, report)
    if (mismatch := _content_mismatch(staging, writes, files, output)) is not None:
        return _evidence_missing(OutputMismatch(mismatch), audit, places, staging, writes.present(staging), [],
                                 cutoff_seq, writes, allowed, output, report)
    try:
        output.rename_directory(staging, places.failed)
    except Exception as error:
        sys.stderr.write(f"失败目录改名未完成，保留暂存目录原名：{type(error).__name__}: {error}；"
                         f"{_evidence_text(report, staging, writes.present(staging), [])}\n")
        return _tail_then_finish(3, audit, places, staging, cutoff_seq, output.FAILURE_MANIFEST, manifest_sha,
                                 None, writes, allowed, output, report)
    writes.moved(staging, places.failed)
    report["failed_dir"] = str(places.failed)
    return _tail_then_finish(1, audit, places, places.failed, cutoff_seq, output.FAILURE_MANIFEST, manifest_sha,
                             None, writes, allowed, output, report)


def _fail_published(failure: RunFailure, audit: Audit, info: Information, places: Any, writes: WriteRecord,
                    cutoff_seq: int | None, manifest_sha: str, usage: list[str], allowed: tuple, output: Any,
                    report: dict) -> int:
    """发布后失败（已改名为正式目录之后）：正式目录保持原状，不追加、不改名、不删除；包外写发布失败说明；照常写尾段
    （尾段已完成时不改写，说明中记录这一点）；两个包外文件任一未完成即退出码 3 并在标准错误列明。"""
    info.exception = exception_record(failure.error)
    report.update({"outcome": "发布后失败", "exit": failure.exit_text, "stage": failure.stage,
                   "formal": str(places.formal), "statement": "该正式目录不得作为成功结果引用"})
    tail_status = writes.of(places.tail)
    tail_written, tail_partial = tail_status == DONE, tail_status == PARTIAL
    tail_label = f"{places.tail.name}（部分写出，不完整）"
    code = 3 if tail_partial else 1
    if tail_partial:
        sys.stderr.write(f"审计尾段部分写出（不完整），原样保留，不覆盖：{places.tail}；{output.NOT_A_SUCCESS}\n")
        report["missing_evidence"] = [*report.get("missing_evidence", []), tail_label]
    note = {"exception": exception_record(failure.error), "stage": failure.stage, "exit": failure.exit_text,
            "formal_dir": str(places.formal), "manifest_sha256": manifest_sha,
            "information_state_final": info.document(), "usage_restriction": list(usage),
            "window_coverage": dict(info.window_coverage),
            "audit_tail": places.tail.name, "audit_tail_written": tail_written,
            "audit_tail_status": tail_status or "未写",
            "missing_evidence": [] if tail_written else [tail_label if tail_partial else "审计尾段（随后尽力写出）"],
            "statement": output.NOT_A_SUCCESS}
    formal_files = f"正式目录中 {output.MANIFEST} 所列文件与 {output.MANIFEST}"
    try:
        writes.write(output, places.publish_failure, output.json_bytes(note))
        report["publish_failure_note"] = places.publish_failure.name
    except Exception as error:
        note_status = writes.of(places.publish_failure)
        note_label = places.publish_failure.name + ("（部分写出，不完整）" if note_status == PARTIAL else "")
        present = [formal_files, *([places.tail.name] if tail_written else []),
                   *([tail_label] if tail_partial else []), *([note_label] if note_status == PARTIAL else [])]
        sys.stderr.write(f"发布失败说明写不出（{note_status}）：{type(error).__name__}: {error}；"
                         f"{_evidence_text(report, places.formal, present, [note_label])}；"
                         f"{output.NOT_A_SUCCESS}\n")
        code = 3
    note_done = writes.done(places.publish_failure)
    if tail_written or tail_partial or audit.sealed:
        return _finish(code, audit, report)
    try:
        _write_tail(audit, places, places.formal, cutoff_seq, output.MANIFEST, manifest_sha, None, False,
                    places.publish_failure.name if note_done else None, writes, allowed, output)
    except Exception as error:
        partial = writes.of(places.tail) == PARTIAL
        present = [formal_files, *([places.publish_failure.name] if note_done else []),
                   *([tail_label] if partial else [])]
        missing = [tail_label if partial else places.tail.name]
        sys.stderr.write(f"审计尾段写不出（{writes.of(places.tail)}）：{type(error).__name__}: {error}；"
                         f"{_evidence_text(report, places.formal, present, missing)}；"
                         f"{output.NOT_A_SUCCESS}\n")
        code = 3
    return _finish(code, audit, report)


def _evidence_text(report: dict, directory: Path, present: list[str], missing: list[str]) -> str:
    report["directory"] = str(directory)
    report["present_files"] = list(present)
    report["missing_evidence"] = [*report.get("missing_evidence", []), *missing]
    return (f"目录 {directory}；已存在的文件（本次运行已写出）：{'、'.join(present) or '无'}；"
            f"未取得的证据：{'、'.join(missing) or '无'}")


def _evidence_missing(error: BaseException, audit: Audit, places: Any, staging: Path, present: list[str],
                      missing: list[str], cutoff_seq: int | None, writes: WriteRecord, allowed: tuple, output: Any,
                      report: dict) -> int:
    sys.stderr.write(f"失败证据未完整取得：{type(error).__name__}: {error}；（保留原名）"
                     f"{_evidence_text(report, staging, present, missing)}\n")
    report["evidence_incomplete"] = True
    return _tail_then_finish(3, audit, places, staging, cutoff_seq, None, None, None, writes, allowed, output, report)


def _tail_then_finish(code: int, audit: Audit, places: Any, package: Path, cutoff_seq: int | None,
                      manifest_name: str | None, frozen_sha: str | None, failure_note: str | None,
                      writes: WriteRecord, allowed: tuple, output: Any, report: dict) -> int:
    try:
        has_log = cutoff_seq is not None and writes.done(package / output.AUDIT_LOG)
        _write_tail(audit, places, package, cutoff_seq if has_log else None,
                    manifest_name if manifest_name and writes.done(package / manifest_name) else None,
                    None, frozen_sha, False, failure_note, writes, allowed, output)
    except Exception as error:
        partial = writes.of(places.tail) == PARTIAL
        label = places.tail.name + ("（部分写出，不完整，原样保留）" if partial else "")
        sys.stderr.write(f"审计尾段写不出（{writes.of(places.tail)}）：{type(error).__name__}: {error}；"
                         f"目录 {package}；未取得的证据：{label}\n")
        report["missing_evidence"] = [*report.get("missing_evidence", []), label]
        code = 3
    return _finish(code, audit, report)


if __name__ == "__main__":
    sys.exit(main())
