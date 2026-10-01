"""v1.4 验证期“只能运行一次”的保护：解析锁定记录并与仓库核对。

正式锁定记录单独作为一次提交（只包含这一个文件）。验证期命令核对，任一不符即拒绝：
(a) HEAD 相对于记录中“代码提交号”的差异恰好只有这一个锁定记录文件；
(b) 工作区没有任何未提交改动；
(c) 记录中各规格与配置文件的 SHA-256 与仓库（HEAD 中已提交的内容）一致；
(d) 记录中的选定设定等于审核过的锁定设定，随机种子与重抽样次数等于配置；
(e) 记录的“此前失败的运行”字段恰好列出失败记录目录中的全部失败记录（补充登记 C）。
字段按锁定记录草稿的表格解析：取对应行里反引号内的值。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.config import PairedParameters
from market_risk.wavewarn.config_v14 import ValidationConfig

COMMIT_FIELD, SETTING_FIELD, SEED_FIELD, RESAMPLE_FIELD = "代码提交号", "选定设定", "随机种子", "重抽样次数"
FAILURE_FIELD, NO_FAILURES = "此前失败的运行", "无"
GIT_HINT = ("找不到 git：请把 C:\\Execute\\Git\\bin 加入 PATH，"
            "或在 config/wavewarn_v14_validation.yaml 的 git_executable 中指定路径")
GZIP_HINT = ("找不到 gzip：请把 C:\\Execute\\Git\\usr\\bin 加入 PATH，"
             "或在 config/wavewarn_v14_validation.yaml 的 gzip_executable 中指定路径")


class LockError(ValueError):
    """锁定记录与仓库不符，拒绝运行验证期。"""


@dataclass(frozen=True)
class LockRecord:
    draft: bool
    commit: str
    hashes: Mapping[str, str]            # 仓库内路径 → 记录中的 SHA-256（大写）
    k: int
    theta_p: Decimal
    seeds: frozenset[int]
    resamples: int
    failures: frozenset[str]             # “此前失败的运行”列出的失败记录文件名；“无”为空集


def table_fields(text: str) -> dict[str, str]:
    """把 Markdown 表格的每一行读成“首列 → 其余列”；同名字段取第一次出现。"""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and len(cells) >= 2 and cells[0] not in fields:
            fields[cells[0]] = " | ".join(cells[1:])
    return fields


def _required(fields: Mapping[str, str], name: str) -> str:
    if name not in fields:
        raise LockError(f"锁定记录缺少字段：{name}")
    return fields[name]


def _file_hashes(fields: Mapping[str, str], locked_files: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """单文件字段取该行第一个 64 位十六进制值；多文件字段取“`路径`”之后最近的那个哈希。"""
    hashes: dict[str, str] = {}
    for name, paths in locked_files.items():
        cell = _required(fields, name)
        for path in paths:
            pattern = (r"`([0-9A-Fa-f]{64})`" if len(paths) == 1
                       else re.escape(f"`{path}`") + r"[^`]*`([0-9A-Fa-f]{64})`")
            found = re.search(pattern, cell)
            if found is None:
                raise LockError(f"锁定记录的“{name}”中找不到 {path} 的 SHA-256")
            hashes[path] = found.group(1).upper()
    return hashes


def listed_failures(cell: str) -> frozenset[str]:
    """“此前失败的运行”：写“无”，或把每份失败记录的文件名写在反引号内（可带目录，只取文件名）。"""
    names = frozenset(name.replace("\\", "/").rsplit("/", 1)[-1] for name in re.findall(r"`([^`]+)`", cell))
    if not names and not cell.strip().startswith(NO_FAILURES):
        raise LockError(f"锁定记录的“{FAILURE_FIELD}”既不是“{NO_FAILURES}”，也没有列出失败记录的文件名")
    return names


def parse_lock_record(text: str, locked_files: Mapping[str, Sequence[str]]) -> LockRecord:
    """按草稿的字段名解析；开头十行内出现“草稿”即视为草稿。"""
    fields = table_fields(text)
    commit = re.search(r"`([0-9a-f]{7,40})`", _required(fields, COMMIT_FIELD))
    setting = re.search(r"K\s*=\s*(\d+)\s*[，,]\s*θ_P\s*=\s*([\d.]+)\s*%", _required(fields, SETTING_FIELD))
    resamples = re.search(r"[\d,]+", _required(fields, RESAMPLE_FIELD))
    if commit is None or setting is None or resamples is None:
        raise LockError("锁定记录的代码提交号、选定设定或重抽样次数无法解析")
    seeds = frozenset(int(value) for value in re.findall(r"(?<!\d)(\d{8})(?!\d)", _required(fields, SEED_FIELD)))
    return LockRecord(any("草稿" in line for line in text.splitlines()[:10]), commit.group(1),
                      _file_hashes(fields, locked_files), int(setting.group(1)), Decimal(setting.group(2)) / 100,
                      seeds, int(resamples.group(0).replace(",", "")),
                      listed_failures(_required(fields, FAILURE_FIELD)))


def static_problems(record: LockRecord, config: ValidationConfig, params: PairedParameters,
                    formal: bool) -> list[str]:
    """不需要 git 的核对：是否草稿、选定设定、随机种子与重抽样次数。"""
    problems = []
    if formal and record.draft:
        problems.append("锁定记录是草稿，不是正式锁定记录")
    if (record.k, record.theta_p) != (config.locked_k, config.locked_theta):
        problems.append(f"记录中的选定设定 K={record.k}、θ_P={record.theta_p} 不是审核过的 "
                        f"K={config.locked_k}、θ_P={config.locked_theta}")
    if record.seeds != frozenset((params.seed_main, params.seed_block_10, params.seed_block_40)):
        problems.append("记录中的随机种子与配置不一致")
    if record.resamples != params.resamples:
        problems.append("记录中的重抽样次数与配置不一致")
    return problems


def find_executable(configured: str, name: str, hint: str) -> str:
    """先用配置中指定的路径（存在时），否则在 PATH 中查找；都找不到则拒绝。"""
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which(name)
    if found is None:
        raise LockError(hint)
    return found


def find_git(configured: str) -> str:
    return find_executable(configured, "git", GIT_HINT)


def find_gzip(configured: str) -> str:
    return find_executable(configured, "gzip", GZIP_HINT)


def failure_records(directory: Path) -> tuple[str, ...]:
    """失败记录目录中的全部文件名；目录不存在即没有失败记录。"""
    return tuple(sorted(path.name for path in directory.iterdir() if path.is_file())) if directory.is_dir() else ()


def failure_problems(record: LockRecord, existing: Sequence[str]) -> list[str]:
    """锁定记录须恰好列出全部失败记录：漏列或列出不存在的记录都拒绝。"""
    problems = []
    missing = sorted(set(existing) - record.failures)
    unknown = sorted(record.failures - set(existing))
    if missing:
        problems.append(f"锁定记录的“{FAILURE_FIELD}”没有列出全部失败记录：" + "、".join(missing))
    if unknown:
        problems.append(f"锁定记录的“{FAILURE_FIELD}”列出了不存在的失败记录：" + "、".join(unknown))
    return problems


def run_git(executable: str, root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([executable, "-c", "core.quotepath=false", *arguments], cwd=root, capture_output=True,
                          check=False)


def sha256_text(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


@dataclass(frozen=True)
class LockCheck:
    record: LockRecord
    path: str                # 锁定记录在仓库内的路径
    sha256: str              # 锁定记录（HEAD 中已提交内容）的 SHA-256
    head: str


def git_problems(executable: str, root: Path, record: LockRecord, relative: str) -> tuple[list[str], str]:
    """与仓库历史、工作区、已提交内容有关的核对；返回问题清单与 HEAD。"""
    problems: list[str] = []
    head = run_git(executable, root, ("rev-parse", "HEAD")).stdout.decode().strip()
    resolved = run_git(executable, root, ("rev-parse", "--verify", "--quiet", f"{record.commit}^{{commit}}"))
    if run_git(executable, root, ("status", "--porcelain")).stdout.strip():
        problems.append("工作区有未提交的改动或未跟踪的文件")
    if resolved.returncode != 0:
        return [*problems, f"记录中的代码提交号 {record.commit} 在仓库中不存在"], head
    code = resolved.stdout.decode().strip()
    if run_git(executable, root, ("merge-base", "--is-ancestor", code, "HEAD")).returncode != 0:
        problems.append("记录中的代码提交号不是 HEAD 的祖先")
    changed = run_git(executable, root, ("diff", "--name-only", code, "HEAD")).stdout.decode("utf-8").split("\n")
    changed = [name for name in changed if name]
    if changed != [relative]:
        problems.append("HEAD 相对于记录中代码提交号的差异不是恰好只有锁定记录这一个文件："
                        + ("、".join(changed) or "没有差异"))
    for path, expected in record.hashes.items():
        shown = run_git(executable, root, ("show", f"HEAD:{path}"))
        if shown.returncode != 0 or sha256_text(shown.stdout) != expected:
            problems.append(f"{path} 的 SHA-256 与锁定记录不一致")
    return problems, head


def verify_lock(root: Path, record_path: Path, config: ValidationConfig, params: PairedParameters) -> LockCheck:
    """正式锁定记录的全部核对；任一不符即拒绝，并一次列出全部问题。"""
    executable = find_git(config.git_executable)
    try:
        relative = record_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise LockError(f"锁定记录不在仓库内：{record_path}") from exc
    if not record_path.is_file():
        raise LockError(f"锁定记录不存在：{record_path}")
    record = parse_lock_record(record_path.read_text(encoding="utf-8"), config.locked_files)
    find_gzip(config.gzip_executable)
    history, head = git_problems(executable, root, record, relative)
    problems = [*static_problems(record, config, params, True), *history,
                *failure_problems(record, failure_records(root / config.failures_output))]
    if problems:
        raise LockError("拒绝运行验证期：" + "；".join(problems))
    committed = run_git(executable, root, ("show", f"HEAD:{relative}")).stdout
    return LockCheck(record, relative, sha256_text(committed), head)
