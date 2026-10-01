"""v1.4 验证期与开发期演练的读写边界。

验证期命令只能在正式锁定之后运行一次：先核对锁定记录（lock_guard，含“此前失败的运行”），输出目录已存在时拒绝。
失败处理按《v1.4 检验口径与诊断补充登记》C 部分：全部计算在内存与临时目录中完成，成功后才一次写入正式输出目录；
中途失败只写一份不含任何数值的失败记录，正式输出目录不创建。运行过程不输出任何统计量。
开发期演练用同一套流程与写出方式，但不读取 2016-12-30 之后的数据。
"""

from __future__ import annotations

import datetime as dt
import json
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.config import PairedParameters
from market_risk.wavewarn.config_v14 import ValidationConfig, load_validation_config
from market_risk.wavewarn.inputs import load_inputs_until
from market_risk.wavewarn.lock_guard import (
    FAILURE_FIELD,
    LockCheck,
    LockError,
    find_gzip,
    parse_lock_record,
    sha256_text,
    static_problems,
    verify_lock,
)
from market_risk.wavewarn.v14_model import prepare_v14
from market_risk.wavewarn.validation_flow import (
    WindowEvaluation,
    evaluate_window,
    rehearsal_window,
    validation_window,
)
from market_risk.wavewarn.validation_output import publish_window
from market_risk.wavewarn.validation_report import LockInfo

CONFIG_FILE = "config/wavewarn_v14_validation.yaml"
STAGE_READ, STAGE_COMPUTE, STAGE_WRITE = "读取输入", "计算", "写出（临时目录、压缩逐日明细、移入正式输出目录）"


@dataclass(frozen=True)
class WindowRun:
    """服务层返回值；不含任何检验结果，结果只在输出目录的报告里。"""

    output: Path
    title: str
    intervals: int
    lock_record_sha256: str
    daily_sha256: str            # 逐日明细压缩前
    daily_gz_sha256: str         # 逐日明细压缩后（gzip -n）


class ValidationRunFailed(ValueError):
    """验证期运行中途失败：正式输出目录未创建，已写失败记录。消息不含任何数值。"""


class StageFailure(Exception):
    """带阶段名的内部异常；原始异常在 __cause__ 中。"""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def staged[Value](stage: str, action: Callable[[], Value]) -> Value:
    """执行一个阶段；任何异常（含中断）都标上阶段名后向上抛出。"""
    try:
        return action()
    except BaseException as exc:
        raise StageFailure(stage) from exc


def failed_stage(error: BaseException) -> tuple[str, BaseException]:
    """失败阶段与原始异常；没有标上阶段名的异常只可能来自读取输入或计算。"""
    if isinstance(error, StageFailure) and error.__cause__ is not None:
        return error.stage, error.__cause__
    return f"{STAGE_READ}或{STAGE_COMPUTE}", error


def check_v14_lock(root: Path, lock_record: Path) -> LockCheck:
    """只核对锁定记录（含“此前失败的运行”与 gzip 是否可用），不读取任何数据；供正式运行前预检。"""
    config = load_validation_config(root / CONFIG_FILE)
    return verify_lock(root, lock_record, config, config.model.base.paired_parameters())


def code_locations(error: BaseException, root: Path) -> list[dict[str, object]]:
    """堆栈中的代码位置：仓库内的文件写相对路径，其余只写文件名；不含源代码行与任何变量值。"""
    base = root.resolve()
    result: list[dict[str, object]] = []
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename)
        try:
            name = path.resolve().relative_to(base).as_posix()
        except (ValueError, OSError):
            name = path.name
        result.append({"file": name, "line": frame.lineno, "function": frame.name})
    return result


def failure_payload(now: dt.datetime, stage: str, error: BaseException, lock: LockInfo, head: str,
                    root: Path) -> dict[str, object]:
    """失败记录的内容：时间、失败阶段、错误类型与堆栈中的代码位置；不含错误消息文本与任何数值。"""
    return {"time_utc": now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "stage": stage,
            "error_type": type(error).__qualname__, "code_locations": code_locations(error, root),
            "lock_record": lock.path, "lock_record_sha256": lock.sha256, "head": head,
            "note": "本记录不含错误消息文本、统计量或任何数值；正式输出目录未创建。"
                    f"修复须新建提交并写新的正式锁定记录，在“{FAILURE_FIELD}”字段列出全部失败记录。"}


def write_failure_record(directory: Path, payload: dict[str, object], now: dt.datetime) -> Path:
    """写入失败记录目录；同一秒内已有同名记录时加序号，不覆盖。"""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path, serial = directory / f"failure_{stamp}.json", 1
    while path.exists():
        serial += 1
        path = directory / f"failure_{stamp}_{serial}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def evaluate_validation(root: Path, config: ValidationConfig, params: PairedParameters) -> WindowEvaluation:
    """读取截至验证期末的输入并运行；只在 run_v14_validation 通过全部核对后调用。"""
    inputs = staged(STAGE_READ, lambda: load_inputs_until(root, config.end, config.vix3m_file))

    def compute() -> WindowEvaluation:
        base = prepare_v14(config.model, inputs)
        return evaluate_window(base, validation_window(base, config, params), config, params)

    return staged(STAGE_COMPUTE, compute)


def run_v14_validation(root: Path, lock_record: Path, now: dt.datetime,
                       evaluate: Callable[[Path, ValidationConfig, PairedParameters], WindowEvaluation]
                       ) -> WindowRun:
    """正式验证期运行。evaluate 由服务层传入 evaluate_validation（测试中可换成不读数据的替身）。

    输出目录已存在即拒绝；锁定记录核对不过即拒绝（这两种情形没有读取任何数据，不算失败的运行）。
    核对通过后的任何失败都只写失败记录，正式输出目录不创建。
    """
    config = load_validation_config(root / CONFIG_FILE)
    output = root / config.validation_output
    if output.exists():
        raise LockError(f"输出目录已存在验证期结果，拒绝再次运行：{output}")
    params = config.model.base.paired_parameters()
    check = verify_lock(root, lock_record, config, params)
    gzip_executable = find_gzip(config.gzip_executable)
    lock = LockInfo(check.path, check.sha256, check.record.commit, True)
    try:
        result = evaluate(root, config, params)
        daily = staged(STAGE_WRITE, lambda: publish_window(output, result, lock, check.head, config, params,
                                                          gzip_executable))
    except BaseException as failure:
        stage, cause = failed_stage(failure)
        record = write_failure_record(root / config.failures_output,
                                      failure_payload(now, stage, cause, lock, check.head, root), now)
        raise ValidationRunFailed(
            f"验证期运行在“{stage}”阶段失败（{type(cause).__qualname__}），正式输出目录未创建。"
            f"失败记录：{record}。修复须新建提交并写新的正式锁定记录，"
            f"在“{FAILURE_FIELD}”字段列出全部失败记录。") from None
    return WindowRun(output, result.window.title, len(result.differences), check.sha256, daily.raw,
                     daily.compressed)


def run_v14_rehearsal(root: Path, lock_draft: Path) -> WindowRun:
    """开发期演练：评价窗口为开发期，输入只读到开发期末；设定与种子按锁定记录草稿核对。"""
    config = load_validation_config(root / CONFIG_FILE)
    end = config.model.base.development_end()
    output = root / config.rehearsal_output
    if output.exists():
        raise FileExistsError(f"演练目录已存在，拒绝覆盖：{output}")
    params = config.model.base.paired_parameters()
    gzip_executable = find_gzip(config.gzip_executable)
    data = lock_draft.read_bytes()
    record = parse_lock_record(data.decode("utf-8"), config.locked_files)
    problems = static_problems(record, config, params, False)
    if problems:
        raise LockError("锁定记录草稿与配置不一致：" + "；".join(problems))
    base = prepare_v14(config.model, load_inputs_until(root, end, config.vix3m_file))
    result = evaluate_window(base, rehearsal_window(base, config), config, params)
    lock = LockInfo(lock_draft.resolve().relative_to(root.resolve()).as_posix(),
                    sha256_text(data.replace(b"\r\n", b"\n")), record.commit, False)
    daily = publish_window(output, result, lock, "", config, params, gzip_executable)
    return WindowRun(output, result.window.title, len(result.differences), lock.sha256, daily.raw,
                     daily.compressed)
