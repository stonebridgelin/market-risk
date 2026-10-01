"""v1.4 验证期与开发期演练的读写边界。

验证期命令只能在正式锁定之后运行一次：先核对锁定记录（lock_guard），输出目录已有结果时拒绝，
运行开始时把锁定记录的 SHA-256 写入输出。开发期演练用同一套流程，但不读取 2016-12-30 之后的数据。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.config_v14 import ValidationConfig, load_validation_config
from market_risk.wavewarn.inputs import load_inputs_until
from market_risk.wavewarn.lock_guard import (
    LockCheck,
    LockError,
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
from market_risk.wavewarn.validation_output import write_lock_info, write_window_outputs
from market_risk.wavewarn.validation_report import LockInfo

CONFIG_FILE = "config/wavewarn_v14_validation.yaml"


@dataclass(frozen=True)
class WindowRun:
    """服务层返回值；不含任何检验结果，结果只在输出目录的报告里。"""

    output: Path
    title: str
    intervals: int
    lock_record_sha256: str
    daily_sha256: str


def check_v14_lock(root: Path, lock_record: Path) -> LockCheck:
    """只核对锁定记录，不读取任何数据；供正式运行前预检。"""
    config = load_validation_config(root / CONFIG_FILE)
    return verify_lock(root, lock_record, config, config.model.base.paired_parameters())


def _evaluate_validation(root: Path, config: ValidationConfig) -> WindowEvaluation:
    """读取截至验证期末的输入并运行；只在 run_v14_validation 通过全部核对后调用。"""
    params = config.model.base.paired_parameters()
    base = prepare_v14(config.model, load_inputs_until(root, config.end, config.vix3m_file))
    return evaluate_window(base, validation_window(base, config, params), config, params)


def run_v14_validation(root: Path, lock_record: Path) -> WindowRun:
    """正式验证期运行：输出目录已存在即拒绝；锁定记录核对不过即拒绝；运行开始时先写入锁定记录的 SHA-256。"""
    config = load_validation_config(root / CONFIG_FILE)
    output = root / config.validation_output
    if output.exists():
        raise LockError(f"输出目录已存在验证期结果，拒绝再次运行：{output}")
    params = config.model.base.paired_parameters()
    check = verify_lock(root, lock_record, config, params)
    lock = LockInfo(check.path, check.sha256, check.record.commit, True)
    output.mkdir(parents=True)
    write_lock_info(output, lock, check.head)
    result = _evaluate_validation(root, config)
    daily = write_window_outputs(output, result, lock, config, params)
    return WindowRun(output, result.window.title, len(result.differences), check.sha256, daily)


def run_v14_rehearsal(root: Path, lock_draft: Path) -> WindowRun:
    """开发期演练：评价窗口为开发期，输入只读到开发期末；设定与种子按锁定记录草稿核对。"""
    config = load_validation_config(root / CONFIG_FILE)
    end = config.model.base.development_end()
    output = root / config.rehearsal_output
    if output.exists():
        raise FileExistsError(f"演练目录已存在，拒绝覆盖：{output}")
    params = config.model.base.paired_parameters()
    data = lock_draft.read_bytes()
    record = parse_lock_record(data.decode("utf-8"), config.locked_files)
    problems = static_problems(record, config, params, False)
    if problems:
        raise LockError("锁定记录草稿与配置不一致：" + "；".join(problems))
    base = prepare_v14(config.model, load_inputs_until(root, end, config.vix3m_file))
    result = evaluate_window(base, rehearsal_window(base), config, params)
    lock = LockInfo(lock_draft.resolve().relative_to(root.resolve()).as_posix(),
                    sha256_text(data.replace(b"\r\n", b"\n")), record.commit, False)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".validation_rehearsal_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        write_lock_info(staging, lock, "")
        daily = write_window_outputs(staging, result, lock, config, params)
        staging.rename(output)
    return WindowRun(output, result.window.title, len(result.differences), lock.sha256, daily)
