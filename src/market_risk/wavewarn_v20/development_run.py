"""阶段四开发期运行的入口（阶段四字段级设计稿第二节、第三节第 4—5 小节、第四节、第七节、第八节；
M2 第一部分指令第二节第 6、7 小节）。边界侧。

入口：python -B -m market_risk.wavewarn_v20.development_run [--root <目录>]；--root 默认为仓库根目录。
除 --root 外不接受任何参数；登记值不可从命令行或环境变量覆盖。

模块顶层无副作用：只有本说明、标准库导入与定义。main 的第一条语句安装读取审计钩子，钩子装好后才在函数内导入业务模块。
本模块的文件与进程访问只有五类，均经登记入口：配置经 config_v20.load_v20_config；行情经 data_v20.precheck_read 与
data_v20.read_file_bytes；预检源码、配置与锁文件的哈希经 development_output.read_preflight_file（只在第 1 步）；
输出、清单、发布与失败证据经 development_output 的前五个基本函数；git 经 _git（只读的三条命令）。

退出码（PM）：0 进程内全部条件满足；1 运行失败且失败证据完整写出；2 预检拒绝（未读入任何行情）；
3 失败且部分失败证据未取得（标准错误列明）；4 收尾后出现文件访问尝试。退出码 0 只是成功引用的必要条件之一。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata as metadata
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

PACKAGES = ("market-risk", "numpy", "pandas", "pandas-market-calendars", "PyYAML")
SEALED_PREFIX = "【封存后访问】"
COVERAGE_END = "尾段文件关闭前最后一次取事件"
NOT_COVERED = (
    "钩子安装之前的解释器启动与入口导入（runpy、包初始化与本模块自身）",
    "原生代码直接读取",
    "子进程（预检中的 git 子进程在读取行情之前执行，其文件访问不在覆盖范围）",
    "尾段关闭之后至进程结束（含封存状态下被阻止的尝试，只见于标准错误）",
    "main 返回之后的解释器关闭阶段（被阻止的尝试只留在标准错误，不改变退出码）",
)
FORBIDDEN_FLAGS = ("O_CREAT", "O_TRUNC", "O_APPEND", "O_EXCL")
EXIT_INPUT = "输入校验失败"
EXIT_UNEXPECTED = "未预期异常"
EXIT_FAILED = "计算失败"
STAGE_DIAGNOSIS = "读取行情（诊断读取）"                       # 补充六第一节第 2 小节第 2 条


def default_root() -> Path:
    """仓库根目录：本文件往上四级（与 verify_dataset.default_root 同义）。"""
    return Path(__file__).resolve().parents[3]


def normalized(path: object) -> str:
    return os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(path))))


def within(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def read_only(mode: object, flags: object) -> bool:
    """与 verify_dataset.read_only_conclusion 的“只读”同一规则；布尔值不视为整数。"""
    if not isinstance(flags, int) or isinstance(flags, bool) or mode != "r":
        return False
    if flags & os.O_WRONLY or flags & os.O_RDWR:
        return False
    return not any(flags & getattr(os, name) for name in FORBIDDEN_FLAGS if hasattr(os, name))


class Audit:
    """读取审计钩子（设计稿第七节）：记录全部 open 事件（序号自 1 起连续）；按规则阻止违规打开；收尾后封存。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.market: dict[str, int] = {}             # 规范化路径 → 已打开次数
        self.protected: tuple[str, ...] = ()
        self.allowed_writes: tuple[str, ...] = ()
        self.sealed = False
        self.sealed_attempts = 0

    def configure(self, root: Path, market_files: list[Path]) -> None:
        self.protected = tuple(normalized(root.joinpath(name)) for name in ("data", "results", "db", "reports"))
        self.market = {normalized(path): 0 for path in market_files}

    def allow(self, places: list[Path]) -> None:
        """本次暂存、失败、正式目录与两个包外文件的写（设计稿第七节“允许（续）”）。"""
        self.allowed_writes = tuple(normalized(path) for path in places)

    def __call__(self, event: str, arguments: tuple) -> None:
        if event != "open":
            return
        raw = arguments[0] if arguments else None
        path = os.fsdecode(raw) if isinstance(raw, str | bytes | os.PathLike) else repr(raw)
        mode = arguments[1] if len(arguments) > 1 else None
        flags = arguments[2] if len(arguments) > 2 else None
        if self.sealed:
            self.sealed_attempts += 1
            sys.stderr.write(f"{SEALED_PREFIX}{json.dumps([path, str(mode), repr(flags)], ensure_ascii=False)}\n")
            raise PermissionError(f"收尾后不得访问文件：{path}")
        violation = self._violation(path, mode, flags)
        self.events.append({"record": "open", "seq": len(self.events) + 1, "path": path, "mode": str(mode),
                            "flags": flags if isinstance(flags, int) and not isinstance(flags, bool) else repr(flags),
                            "violation": violation})
        if violation is not None:
            raise PermissionError(f"读取审计阻止：{violation}：{path}")

    def _violation(self, path: str, mode: object, flags: object) -> str | None:
        if not self.protected or path.startswith("<"):
            return None
        target = normalized(path)
        if target in self.market:
            self.market[target] += 1
            if self.market[target] > 1:
                return "行情文件第二次打开"
            if not read_only(mode, flags):
                return "行情文件不是只读打开"
            return None
        if any(within(target, item) for item in self.allowed_writes):
            return None
        if any(within(target, item) for item in self.protected):
            return "受保护目录"
        return None

    def violations(self, first: int, last: int) -> int:
        return sum(1 for item in self.events[first - 1:last] if item["violation"] is not None)

    def seq_of(self, path: Path) -> int | None:
        target = normalized(path)
        found = [item["seq"] for item in self.events if normalized(item["path"]) == target]
        return found[-1] if found else None

    def seal(self) -> None:
        self.sealed = True


def _git(root: Path, *arguments: str) -> tuple[int, str]:
    """预检中的 git（只读三条：rev-parse HEAD、status --porcelain --untracked-files=all、
    diff --cached --name-only）。"""
    import subprocess

    done = subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=False)
    return done.returncode, done.stdout.decode("utf-8", errors="replace")


class Information:
    """信息状态记录（设计稿第四节第 1 小节）：在内存中初始化（补充二第一节），各步进入、离开逐条记入。"""

    def __init__(self) -> None:
        self.seq = 0
        self.data_read: dict = {}
        self.parsed: dict = {}
        self.derived: list = []
        self.performance: list = []
        self.trace: list = []
        self.exception: dict | None = None
        self.completed: list[str] = []
        self.composition_called = False

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def enter(self, stage: str) -> None:
        self.trace.append({"seq": self._next(), "stage": stage, "action": "进入"})

    def leave(self, stage: str) -> None:
        self.trace.append({"seq": self._next(), "stage": stage, "action": "离开"})

    def journal(self, event: str, item: str, field: str, stage: str) -> None:
        """组合层 journal 回调：只做列表追加。"""
        entry = {"seq": self._next(), "object": item, "field": field, "stage": stage, "event": event}
        (self.performance if event == "performance" else self.derived).append(entry)

    def outer(self, item: str, field: str, stage: str) -> None:
        """外层（选择、对照、对账、序列化）产生的表现信息。"""
        self.performance.append({"seq": self._next(), "object": item, "field": field, "stage": stage,
                                 "event": "performance"})

    def status(self) -> str:
        """设计稿第四节第 3 小节：有表现信息事件为“已产生”；组合层未被调用为“未产生”；其余为“无法确认”。"""
        if self.performance:
            return "已产生"
        if not self.composition_called:
            return "未产生"
        return "无法确认"

    def document(self) -> dict:
        status = self.status()
        return {
            "data_read": self.data_read, "evaluation_prices_parsed": self.parsed, "derived_inputs": self.derived,
            "performance_info": self.performance, "performance_info_status": status, "stage_trace": self.trace,
            "exception": self.exception, "completed_results": list(self.completed),
            "s21_facts": {"读入行情": sorted(asset for asset, item in self.data_read.items() if item.get("read")),
                          "严格解析完成": sorted(asset for asset, item in self.parsed.items()
                                           if item.get("strict_completed")),
                          "诊断解析完成": sorted(asset for asset, item in self.parsed.items()
                                           if item.get("diagnosis_completed")),
                          "组合层已调用": self.composition_called, "表现信息状态": status,
                          "s21_class": "待核对"},
        }


class RunFailure(Exception):
    """运行失败（出口、阶段与证据）；在入口内部按失败处置分支处理。"""

    def __init__(self, exit_text: str, stage: str, error: BaseException | None, detail: dict | None = None) -> None:
        super().__init__(exit_text)
        self.exit_text, self.stage, self.error, self.detail = exit_text, stage, error, detail or {}


class OutputMismatch(Exception):
    """失败目录内容（由完成与部分写出记录推得）与失败清单不一致（补充七第一节第 1 小节第 4 条）。"""


DONE, PARTIAL, ABSENT = "已完成", "部分写出", "未创建"


class WriteRecord:
    """每一次 write_new 调用的完成状态（补充七第一节第 1 小节第 1 条），只在内存中记录：正常返回为“已完成”；
    抛出异常后以 path_exists 检查该路径，存在为“部分写出”，不存在为“未创建”。任何判断都不只凭存在性推定已完整写出。"""

    def __init__(self) -> None:
        self.status: dict[str, tuple[Path, str]] = {}

    def write(self, output: Any, path: Path, data: Any, closed: Any = None) -> None:
        try:
            output.write_new(path, data, closed)
        except Exception:
            self.status[normalized(path)] = (path, PARTIAL if output.path_exists(path) else ABSENT)
            raise
        self.status[normalized(path)] = (path, DONE)

    def of(self, path: Path) -> str | None:
        found = self.status.get(normalized(path))
        return found[1] if found else None

    def done(self, path: Path) -> bool:
        return self.of(path) == DONE

    def in_directory(self, directory: Path, *states: str) -> list[str]:
        base = normalized(directory)
        return sorted(path.name for key, (path, state) in self.status.items()
                      if state in states and os.path.dirname(key) == base)

    def moved(self, source: Path, target: Path) -> None:
        """整目录改名之后，把该目录下各文件的记录改到新路径。"""
        base = normalized(source)
        for key, (path, state) in list(self.status.items()):
            if os.path.dirname(key) == base:
                del self.status[key]
                self.status[normalized(target / path.name)] = (target / path.name, state)

    def present(self, directory: Path) -> list[str]:
        """本次运行在该目录下写出（已完成或部分写出）的文件名；部分写出的标“不完整”。"""
        return [name if self.done(directory / name) else f"{name}（部分写出，不完整）"
                for name in self.in_directory(directory, DONE, PARTIAL)]


def exception_record(error: BaseException | None, trace: str | None = None) -> dict:
    if error is None:
        return {"type": "未取得", "message": "未取得", "traceback": "未取得"}
    reason = getattr(error, "reason", None)
    return {"type": type(error).__name__, "reason": reason if isinstance(reason, str) and reason else None,
            "message": str(error), "traceback": trace if trace is not None else "".join(
                traceback.format_exception(type(error), error, error.__traceback__))}


def main(argv: list[str] | None = None) -> int:
    """开发期运行入口。返回退出码（以 __main__ 方式执行时交 sys.exit）。"""
    sys.addaudithook(audit := Audit())
    started, start_clock = dt.datetime.now(dt.UTC), time.monotonic()
    parser = argparse.ArgumentParser(description="波段预警 v2.0 开发期运行（阶段四）")
    parser.add_argument("--root", type=Path, default=default_root())
    root: Path = parser.parse_args(argv).root
    from market_risk.calendar import stock_trading_days
    from market_risk.storage.paths import StoragePaths
    from market_risk.wavewarn_v20 import config_v20, data_v20, registered_v20, research_run
    from market_risk.wavewarn_v20 import development_compare as compare
    from market_risk.wavewarn_v20 import development_output as output
    from market_risk.wavewarn_v20.r1 import R1Error
    from market_risk.wavewarn_v20.selection import SelectionError

    market_files = [StoragePaths(root).market_daily_file(asset) for asset in config_v20.ASSETS]
    audit.configure(root, market_files)
    info = Information()
    stdout_report: dict = {"root": str(root)}

    # ---------------- 第 1 步：预检 ----------------
    info.enter("预检")
    try:
        commit, places, preflight = _preflight(root, started, output)
    except output.PreflightError as error:
        output.close_preflight()
        info.exception = exception_record(error)
        stdout_report.update({"exit_code": 2, "outcome": "预检拒绝", "reason": str(error),
                              "information_state": info.document(), "open_events": audit.events})
        _print(stdout_report)
        sys.stderr.write(f"预检拒绝：{error}\n")
        return 2
    output.close_preflight()
    info.leave("预检")
    audit.allow([places.staging, places.failed, places.formal, places.tail, places.publish_failure])
    stdout_report.update({"staging": str(places.staging), "formal": str(places.formal), "tail": places.tail.name})
    allowed = output.readable_roots(places)
    usage: str | None = None
    cutoff_seq: int | None = None
    files_written: dict[str, bytes] = {}
    writes = WriteRecord()                                              # 各次写入的完成状态（补充七）
    manifest_data: bytes | None = None
    # 失败上下文（补充六第一节第 2 小节第 1 条）：严格读取抛 MissingPriceEntryError 后、调用 diagnose_until 之前登记
    # strict_error；诊断读取返回后清除。只供最外层兜底保存证据，不改变任何分支的出口与流程。
    diagnosing: dict = {}

    # ---------------- 第 2—12 步 ----------------
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
        cutoff = registered_v20.CUTOFF
        series: dict = {}
        evidence: dict = {}
        for asset, path in zip(config_v20.ASSETS, market_files, strict=True):
            registered = config.registered[asset]
            data_v20.precheck_read(asset, cutoff, registered)
            raw = data_v20.read_file_bytes(path)
            info.data_read[asset] = {"read": True, "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                                     "read_event_seq": audit.seq_of(path)}
            parsed = info.parsed.setdefault(asset, {"strict_started": True, "strict_completed": False,
                                                    "diagnosis_started": False, "diagnosis_completed": False})
            try:
                item = data_v20.parse_until(raw, asset, cutoff, registered, config.decisions)
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
                    diagnosis = data_v20.diagnose_until(raw, asset, cutoff, registered, config.decisions)
                except data_v20.MissingPriceEntryError as error:
                    # 补充六第一节第 3 小节：诊断读取不应抛该异常；抛出即读取实现不一致
                    # （与 evidence_check 为“否”同属一类）。
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
                usage = output.PREFIX_USAGE
                item = diagnosis.series
            else:
                parsed["strict_completed"] = True
            series[asset] = item
            closes = list(item.closes)
            parsed.update({"rows": len(closes), "first_day": closes[0] if closes else None,
                           "last_day": closes[-1] if closes else None})
        info.leave("读取行情")

        info.enter("组装快照")
        assembled = data_v20.assemble_snapshot(series, cutoff, started)
        snapshot = assembled.snapshot
        info.leave("组装快照")

        info.enter("组合层")
        info.composition_called = True
        parameters = registered_v20.run_parameters()
        result = research_run.run_window(snapshot, registered_v20.window_spec(), parameters,
                                         registered_v20.CANDIDATES, journal=info.journal)
        info.leave("组合层")
        if result.stop is not None:
            raise RunFailure(result.stop.exit.value, "组合层", None,
                             {"stop": {"exit": result.stop.exit.value, "object": result.stop.object,
                                       "stage": result.stop.stage, "exception_type": result.stop.exception_type,
                                       "reason_code": result.stop.reason_code, "message": result.stop.message,
                                       "traceback": result.stop.traceback}})

        info.enter("选择")
        try:
            selection = research_run.select_development(result, registered_v20.TOLERANCE)
        except (SelectionError, R1Error) as error:
            raise RunFailure(EXIT_FAILED, "选择", error) from error
        except Exception as error:
            raise RunFailure(EXIT_FAILED, "选择", error) from error
        info.outer("选择程序", "选择结果", "选择")
        info.leave("选择")

        info.enter("描述性对照")
        years = range(snapshot.days[0].year - 1, snapshot.day.year + 1)
        year_ends = {year: days[-1] for year in years
                     if (days := stock_trading_days(dt.date(year, 1, 1), dt.date(year, 12, 31)))}
        try:
            descriptive = compare.descriptive(snapshot, result, selection, parameters, registered_v20.BEARS, year_ends,
                                              registered_v20.ENVIRONMENT_UP, registered_v20.ENVIRONMENT_DOWN,
                                              registered_v20.ENVIRONMENT_ASSET,
                                              lambda field: info.outer("描述性对照", field, "描述性对照"))
        except Exception as error:
            raise RunFailure(EXIT_FAILED, "描述性对照", error) from error
        info.leave("描述性对照")

        info.enter("对账")
        try:
            reconciliation = compare.reconcile_paths(result, descriptive.averages, descriptive.constants,
                                                     registered_v20.TOLERANCE)
        except Exception as error:
            raise RunFailure(EXIT_FAILED, "对账", error) from error
        info.outer("对账", "对账结果", "对账")
        info.leave("对账")

        info.enter("序列化")
        try:
            record = _run_record(commit, preflight, registered_v20.describe(), info, cutoff, audit, usage, output)
            stable = output.stable_files(snapshot, result, selection, descriptive, reconciliation, record, usage,
                                         lambda name: info.outer("序列化", name, "序列化"))
        except Exception as error:
            raise RunFailure(EXIT_FAILED, "序列化", error) from error
        info.leave("序列化")

        info.enter("写出")
        for name, data in stable.items():
            writes.write(output, places.staging / name, data)
            files_written[name] = data
            info.completed.append(name)
        state = output.json_bytes(info.document())                     # 最后一个稳定计算文件（补充二第一节）
        writes.write(output, places.staging / output.INFORMATION_STATE, state)
        files_written[output.INFORMATION_STATE] = state
        environment = output.json_bytes(_environment(started, start_clock, root, places))
        writes.write(output, places.staging / output.RUN_ENVIRONMENT, environment)
        files_written[output.RUN_ENVIRONMENT] = environment
        cutoff_seq = len(audit.events)
        audit_log = _audit_log(audit, cutoff_seq, output)
        writes.write(output, places.staging / output.AUDIT_LOG, audit_log)
        files_written[output.AUDIT_LOG] = audit_log
        output.verify_written(places.staging, files_written, allowed)
        manifest_data = output.manifest_bytes(files_written)
        writes.write(output, places.staging / output.MANIFEST, manifest_data)
    except RunFailure as failure:
        return _fail(failure, None, audit, info, places, files_written, writes, cutoff_seq, usage, allowed, output,
                     stdout_report)
    except Exception as error:                                          # 最外层兜底：只记录未预期异常
        trace = traceback.format_exc()
        if diagnosing:
            # 补充六第一节第 2 小节第 2 条：诊断读取中的未预期异常，同时写出严格读取证据与诊断异常。
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
    # 尾段之前先判定成功条件（两段违规数为 0、截止点之后有事件可衔接）；不满足即按“发布后失败”先写包外说明再写尾段。
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
        # 尾段已写出并封存（收尾记录为“失败”）：此后不得再访问文件，只在标准错误与退出信息中报告。
        sys.stderr.write(f"审计尾段收尾记录为“失败”（衔接校验或违规检查不通过）；正式目录 {places.formal}；"
                         f"{output.NOT_A_SUCCESS}\n")
        stdout_report.update({"outcome": "发布后失败", "statement": output.NOT_A_SUCCESS})
        return _finish(1, audit, stdout_report)
    stdout_report.update({"exit_code": 0, "outcome": "发布完成（进程内条件满足）", "manifest_sha256": manifest_sha,
                          "usage_restriction": usage})
    return _finish(0, audit, stdout_report)


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


def _preflight(root: Path, started: dt.datetime, output: Any) -> tuple[str, Any, dict]:
    """第 1 步：git 三条只读命令；目录与包外文件名无冲突；源码、配置与锁文件哈希；依赖版本。不读取任何行情。"""
    code, head = _git(root, "rev-parse", "HEAD")
    commit = head.strip()
    if code != 0 or len(commit) != 40:
        raise output.PreflightError(f"git rev-parse HEAD 失败：退出码 {code}")
    for arguments, name in ((("status", "--porcelain", "--untracked-files=all"), "工作区不干净"),
                            (("diff", "--cached", "--name-only"), "暂存区不为空")):
        code, text = _git(root, *arguments)
        if code != 0 or text.strip():
            raise output.PreflightError(f"{name}：退出码 {code}")
    places = output.locations(root, started.strftime("%Y%m%dT%H%M%SZ"), commit[:7])
    for path, name in ((places.formal, "正式目录已存在"), (places.staging, "暂存目录名已存在"),
                       (places.failed, "失败目录名已存在"), (places.tail, "审计尾段文件名已存在"),
                       (places.publish_failure, "发布失败说明文件名已存在")):
        if output.path_exists(path):
            raise output.PreflightError(f"{name}：{path}")
    hashes = {}
    for relative in output.PREFLIGHT_FILES:
        try:
            data = output.read_preflight_file(root, relative)
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
    return commit, places, {"source_hashes": hashes, "dependency_versions": versions}


def _run_record(commit: str, preflight: dict, registered: dict, info: Information, cutoff: dt.date, audit: Audit,
                usage: str | None, output: Any) -> dict:
    return {
        "output_scheme": output.OUTPUT_SCHEME,
        "commit": commit,
        "dependency_versions": preflight["dependency_versions"],
        "source_hashes": preflight["source_hashes"],
        "registered_v20_sha256": preflight["source_hashes"]["src/market_risk/wavewarn_v20/registered_v20.py"],
        "registered_parameters": registered,
        "data_files": {asset: {"bytes": item["bytes"], "raw_sha256": item["raw_sha256"]}
                       for asset, item in info.data_read.items()},
        "cutoff": cutoff,
        "usage_restriction": usage,
        "audit_conclusion": {"violation_found_until_this_file": audit.violations(1, len(audit.events)) > 0,
                             "note": "成功路径上任何违规都会使运行失败；完整结论以审计尾段收尾记录为准"},
        "audit_coverage": {"coverage_end": COVERAGE_END, "not_covered": list(NOT_COVERED)},
    }


def _environment(started: dt.datetime, start_clock: float, root: Path, places: Any) -> dict:
    ended = dt.datetime.now(dt.UTC)
    return {"argv": list(sys.argv), "executable": sys.executable, "acquired_at": started, "started_at": started,
            "ended_at": ended, "seconds": time.monotonic() - start_clock,
            "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"), "root": str(root),
            "staging": str(places.staging), "formal": str(places.formal)}


def _audit_log(audit: Audit, last: int, output: Any) -> bytes:
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
    """包外审计尾段（M2 指令第二节第 7 小节第 2 条的固定顺序）：先读取包内 audit_log.jsonl 与适用清单计算哈希并做衔接
    校验；再以独占方式打开尾段文件（该次打开记入尾段）；在内存中组成衔接记录、事件、收尾记录；一次写入并关闭；随即封存。
    返回（是否写出，收尾记录是否为“成功”）。"""
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


def _failure_document(failure: RunFailure, trace: str | None, usage: str | None, extra: dict,
                      missing: list[str], incomplete: list[dict]) -> dict:
    """incomplete_files（补充七第一节第 1 小节第 3 条）：部分写出的文件名与状态；普通失败为空列表。"""
    return {"exit": failure.exit_text, "stage": failure.stage, "exception": exception_record(failure.error, trace),
            "detail": failure.detail, "usage_restriction": usage, "missing_evidence": missing,
            "incomplete_files": incomplete, **extra}


def _read_incomplete(directory: Path, writes: WriteRecord, files: dict[str, bytes], allowed: tuple,
                     output: Any) -> list[dict]:
    """部分写出的文件（补充七第一节第 1 小节第 3 条）：原样保留，以 read_back 读回实际字节，列入失败清单。"""
    incomplete = [{"file": name, "status": PARTIAL} for name in writes.in_directory(directory, PARTIAL)]
    for item in incomplete:
        files[item["file"]] = output.read_back(directory / item["file"], allowed)
    return incomplete


def _content_mismatch(directory: Path, writes: WriteRecord, files: dict[str, bytes], output: Any) -> str | None:
    """改名前核对（补充七第一节第 1 小节第 4 条）：由完成与部分写出记录推得的目录文件集合，
    须等于失败清单所列文件加清单本身。"""
    derived = set(writes.in_directory(directory, DONE, PARTIAL))
    listed = {*files, output.FAILURE_MANIFEST}
    if derived == listed:
        return None
    return (f"失败目录内容与清单不一致：记录有而清单无 {sorted(derived - listed)}；"
            f"清单有而记录无 {sorted(listed - derived)}")


def _fail(failure: RunFailure, trace: str | None, audit: Audit, info: Information, places: Any,
          written: dict[str, bytes], writes: WriteRecord, cutoff_seq: int | None, usage: str | None, allowed: tuple,
          output: Any, report: dict) -> int:
    """清单生成之前失败（设计稿第八节第 5 小节）：已写文件保留（部分写出的原样保留、读回后列入失败清单，补充七）；
    新写 failure.json 与 information_state_final.json；尚未写出 audit_log.jsonl 时先写出；生成 FAILURE_MANIFEST.sha256；
    核对目录内容与清单一致后，暂存目录改名为失败目录；写尾段。"""
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
        failure_data = output.json_bytes(_failure_document(failure, trace, usage, {}, missing, incomplete))
        writes.write(output, staging / output.FAILURE, failure_data)
        files[output.FAILURE] = failure_data
        final = output.json_bytes(info.document())
        writes.write(output, staging / output.INFORMATION_STATE_FINAL, final)
        files[output.INFORMATION_STATE_FINAL] = final
        if cutoff_seq is None:
            cutoff_seq = len(audit.events)
            audit_log = _audit_log(audit, cutoff_seq, output)
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
                         writes: WriteRecord, cutoff_seq: int | None, manifest_sha: str, usage: str | None,
                         allowed: tuple, output: Any, report: dict) -> int:
    """清单生成之后、改名之前失败：冻结包原样保留；新写 failure.json（明记清单哈希与“该清单不覆盖本失败目录的最终内容”）
    与 information_state_final.json；生成 FAILURE_MANIFEST.sha256（含 MANIFEST.sha256 与部分写出的文件）；核对目录内容与
    清单一致后改名为失败目录；写尾段。"""
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
        failure_data = output.json_bytes(_failure_document(failure, trace, usage, extra, [], incomplete))
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
                    cutoff_seq: int | None, manifest_sha: str, usage: str | None, allowed: tuple, output: Any,
                    report: dict) -> int:
    """发布后失败（已改名为正式目录之后）：正式目录保持原状，不追加、不改名、不删除；包外写发布失败说明；照常写尾段
    （尾段已完成时不改写，说明中记录这一点）；两个包外文件任一未完成即退出码 3 并在标准错误列明。
    尾段与说明是否写出一律以“已完成”为准（补充七第一节第 1 小节第 2 条）；部分写出的原样保留，标“不完整”。"""
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
            "information_state_final": info.document(), "usage_restriction": usage,
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
    """失败证据写不出时的列明（补充六第一节第 5 小节第 2 条）：目录实际路径、已存在的文件、未取得的证据；
    同时记入退出信息（标准输出报告）。“已存在的文件”取本次运行在内存中记录的已写出文件，不另行列目录。"""
    report["directory"] = str(directory)
    report["present_files"] = list(present)
    report["missing_evidence"] = [*report.get("missing_evidence", []), *missing]
    return (f"目录 {directory}；已存在的文件（本次运行已写出）：{'、'.join(present) or '无'}；"
            f"未取得的证据：{'、'.join(missing) or '无'}")


def _evidence_missing(error: BaseException, audit: Audit, places: Any, staging: Path, present: list[str],
                      missing: list[str], cutoff_seq: int | None, writes: WriteRecord, allowed: tuple, output: Any,
                      report: dict) -> int:
    """失败证据写不出：不覆盖任何已有文件；标准错误与退出信息列出目录实际路径、已存在的文件（含部分写出的文件）、
    未取得的证据；保留暂存目录原名；尾段尽力写出。"""
    sys.stderr.write(f"失败证据未完整取得：{type(error).__name__}: {error}；（保留原名）"
                     f"{_evidence_text(report, staging, present, missing)}\n")
    report["evidence_incomplete"] = True
    return _tail_then_finish(3, audit, places, staging, cutoff_seq, None, None, None, writes, allowed, output, report)


def _tail_then_finish(code: int, audit: Audit, places: Any, package: Path, cutoff_seq: int | None,
                      manifest_name: str | None, frozen_sha: str | None, failure_note: str | None,
                      writes: WriteRecord, allowed: tuple, output: Any, report: dict) -> int:
    """失败分支的尾段：衔接记录指向适用的清单，收尾记录 outcome 为“失败”；写不出时在标准错误列明，退出码改为 3。
    包内 audit_log.jsonl 与清单是否可供衔接，以“已完成”为准（补充七第一节第 1 小节第 1 条）。"""
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
