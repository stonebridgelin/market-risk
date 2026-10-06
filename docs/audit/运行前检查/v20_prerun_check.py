"""v2.0 开发期正式运行前检查能力（阶段四 M2 第二部分指令修订六第十节；N8，R6 甲案）。

用法：python v20_prerun_check.py --checklist <清单.json> --out <报告.json>

- 读取清单 JSON 与仓库、工具工作树、备份清单的指定文件，逐条判定；写一份报告（已存在即失败）。
- 全部文件读取只经 read_checked(path, root, whitelist)：先按词法拒绝落在 data 之下的路径（不对其做任何文件操作），
  再 resolve(strict=True)，以 relative_to 判断目录包含关系（不用字符串前缀），最后核对已登记相对路径白名单。
- 子进程只有 git rev-parse HEAD 与 git status --porcelain，在仓库与工具工作树各执行一次，均带 timeout。
- 不读 data/market；本次不实现任何数据文件哈希选项（第十节第 4 项在 M2 内不做）。
- 退出码：0 全部通过；1 有指出项或人工确认项未确认；2 参数或输入错误（含路径越界、清单结构不符）；3 报告写入失败。
- “检查能力已实现”不等于“真实数据检查已通过”；人工确认项只核对字段非空与格式，不证明其真实性。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHECKLIST_ROOTS = (Path(__file__).resolve().parents[3], Path(r"D:\temp_claude\v20"))   # 清单 JSON 允许的两个根
TOOL_FILES = ("docs/audit/独立复核/v20/audit_v20.py", "docs/audit/独立复核/v20/README.md")
RESEARCH_DIR = ("reports", "research", "wavewarn_v20")
FORMAL_NAMES = ("evaluation_development",)                                    # 正式目录（名字恰为）
FORMAL_PREFIXES = ("evaluation_development_failed_", "evaluation_development_audit_tail_",
                   "evaluation_development_publish_failure_")                 # 失败、尾段、发布失败说明（前缀）
REQUIRED_KEYS = ("repo_root", "tool_root", "backup_root", "expected_commit", "expected_tool_commit", "file_hashes",
                 "tool_file_hashes", "backup_manifests", "manual_confirmations")
REQUIRED_CONFIRMATIONS = ("13_开发期结果处置预案已决定", "14_授权C已授予")         # 第十节第 13、14 项（键名待确认）
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX40 = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT = 60
PASS, FLAG = "通过", "指出"


class InputError(Exception):
    """参数或输入错误（退出码 2）。"""


class ReportError(Exception):
    """报告写入失败（退出码 3）。"""


@dataclass
class Findings:
    """逐条核对结果：（项，状态，说明）。"""

    rows: list[dict] = field(default_factory=list)

    def add(self, item: str, ok: bool, detail: str) -> None:
        self.rows.append({"项": item, "状态": PASS if ok else FLAG, "说明": detail})

    def flagged(self) -> int:
        return sum(1 for row in self.rows if row["状态"] != PASS)


# ---------------------------------------------------------------------------
# 路径边界（第十节第 2 小节“目录包含关系”规则）
# ---------------------------------------------------------------------------


def lexical_parts(path: Path, root: Path) -> tuple[str, ...]:
    """不访问文件系统：规范化后的路径相对根的组成部分；不在根下（词法）即越界。"""
    target = Path(os.path.normpath(path if path.is_absolute() else root / path))
    try:
        return target.relative_to(Path(os.path.normpath(root))).parts
    except ValueError as error:
        raise InputError(f"路径越界（词法）：{path} 不在 {root} 之下") from error


def checked_path(path: Path, root: Path, whitelist: frozenset[str]) -> Path:
    """（1）词法上任一组成部分为 data 即拒绝（不 resolve、不打开）；（2）root、target 各 resolve(strict=True)；
    （3）relative_to 判断包含关系；（4）解析后的相对路径再查 data 与白名单。"""
    if "data" in lexical_parts(path, root):
        raise InputError(f"路径落在 data 之下：{path}")
    try:
        resolved_root = Path(root).resolve(strict=True)
        target = (path if path.is_absolute() else root / path).resolve(strict=True)
    except OSError as error:
        raise InputError(f"路径不存在或不可解析：{path}：{error}") from error
    try:
        relative = target.relative_to(resolved_root)
    except ValueError as error:
        raise InputError(f"路径越界：{target} 不在 {resolved_root} 之下") from error
    if "data" in relative.parts:
        raise InputError(f"解析后的路径落在 data 之下：{target}")
    if relative.as_posix() not in whitelist:
        raise InputError(f"路径不在已登记白名单内：{relative.as_posix()}")
    return target


def read_checked(path: Path, root: Path, whitelist: frozenset[str]) -> bytes:
    """全部文件读取的唯一入口。"""
    return checked_path(path, root, whitelist).read_bytes()


def repo_key_allowed(key: str) -> bool:
    """仓库文件哈希的键：pyproject.toml、uv.lock、config/*.yaml、src/market_risk/wavewarn_v20/*.py、
    docs/audit/独立回算/v20/*、docs/audit/运行前检查/*（各一层，不含子目录）。"""
    parts = key.split("/")
    parent, name = "/".join(parts[:-1]), parts[-1]
    if "\\" in key or ".." in parts or not name:
        return False
    return (key in ("pyproject.toml", "uv.lock") or (parent == "config" and name.endswith(".yaml"))
            or (parent == "src/market_risk/wavewarn_v20" and name.endswith(".py"))
            or parent in ("docs/audit/独立回算/v20", "docs/audit/运行前检查"))


# ---------------------------------------------------------------------------
# 清单结构
# ---------------------------------------------------------------------------


def directory(text: object, name: str) -> Path:
    if not isinstance(text, str) or not text:
        raise InputError(f"{name} 须为非空字符串")
    if "data" in Path(os.path.normpath(text)).parts:
        raise InputError(f"{name} 落在 data 之下：{text}")
    path = Path(text)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InputError(f"{name} 不存在：{text}") from error
    if not resolved.is_dir():
        raise InputError(f"{name} 不是目录：{text}")
    return resolved


def hash_table(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise InputError(f"{name} 须为非空对象")
    for key, digest in value.items():
        if not isinstance(key, str) or not isinstance(digest, str) or HEX64.fullmatch(digest) is None:
            raise InputError(f"{name} 的条目须为 相对路径 → 64 位小写十六进制：{key!r}")
    return dict(value)


def validated(checklist: dict) -> dict[str, Any]:
    """清单 JSON 的结构核对：九个键齐全；三个根为已存在目录；哈希与提交号格式；备份清单与人工确认项结构。"""
    missing = [key for key in REQUIRED_KEYS if key not in checklist]
    if missing:
        raise InputError(f"清单缺少键：{missing}")
    roots = {key: directory(checklist[key], key) for key in ("repo_root", "tool_root", "backup_root")}
    for key in ("expected_commit", "expected_tool_commit"):
        if not isinstance(checklist[key], str) or HEX40.fullmatch(checklist[key]) is None:
            raise InputError(f"{key} 须为 40 位小写十六进制")
    files = hash_table(checklist["file_hashes"], "file_hashes")
    outside = [key for key in files if not repo_key_allowed(key)]
    if outside:
        raise InputError(f"file_hashes 含未登记类别的路径：{outside}")
    tool_files = hash_table(checklist["tool_file_hashes"], "tool_file_hashes")
    if sorted(tool_files) != sorted(TOOL_FILES):
        raise InputError(f"tool_file_hashes 须恰为 {list(TOOL_FILES)}")
    manifests = checklist["backup_manifests"]
    if not isinstance(manifests, list) or not manifests or not all(
            isinstance(item, dict) and set(item) == {"path", "sha256"} and isinstance(item["path"], str)
            and isinstance(item["sha256"], str) and HEX64.fullmatch(item["sha256"]) for item in manifests):
        raise InputError("backup_manifests 须为非空列表，每项恰有 path 与 64 位 sha256")
    if not isinstance(checklist["manual_confirmations"], dict):
        raise InputError("manual_confirmations 须为对象")
    return {**roots, "expected_commit": checklist["expected_commit"],
            "expected_tool_commit": checklist["expected_tool_commit"], "file_hashes": files,
            "tool_file_hashes": tool_files, "backup_manifests": manifests,
            "manual_confirmations": checklist["manual_confirmations"]}


# ---------------------------------------------------------------------------
# 逐条核对（第十节第 2 小节输入表）
# ---------------------------------------------------------------------------


def git_state(root: Path) -> tuple[str, str]:
    """只调用 git rev-parse HEAD 与 git status --porcelain（cwd=root，均带 timeout）。"""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False,
                          timeout=GIT_TIMEOUT)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False,
                            timeout=GIT_TIMEOUT)
    if head.returncode != 0 or status.returncode != 0:
        raise InputError(f"git 失败（{root}）：{head.stderr.strip()} {status.stderr.strip()}")
    return head.stdout.strip(), status.stdout


def check_versions(found: Findings, label: str, root: Path, expected: str) -> None:
    head, porcelain = git_state(root)
    found.add(f"{label} HEAD", head == expected, f"实际 {head}，清单 {expected}")
    found.add(f"{label} 工作区与暂存区为空", porcelain == "", "porcelain 无输出" if porcelain == "" else porcelain)


def check_hashes(found: Findings, label: str, root: Path, table: dict[str, str]) -> None:
    whitelist = frozenset(table)
    for relative, expected in sorted(table.items()):
        actual = hashlib.sha256(read_checked(Path(relative), root, whitelist)).hexdigest()
        found.add(f"{label} {relative}", actual == expected, f"实际 {actual}，清单 {expected}")


def check_backups(found: Findings, root: Path, manifests: list[dict]) -> None:
    whitelist = frozenset(Path(os.path.normpath(item["path"])).as_posix() if not Path(item["path"]).is_absolute()
                          else lexical_relative(item["path"], root) for item in manifests)
    for item in manifests:
        actual = hashlib.sha256(read_checked(Path(item["path"]), root, whitelist)).hexdigest()
        found.add(f"备份清单 {item['path']}", actual == item["sha256"], f"实际 {actual}，清单 {item['sha256']}"
                  "（只核对清单文件自身，不逐文件核对备份内容）")


def lexical_relative(text: str, root: Path) -> str:
    return Path(*lexical_parts(Path(text), root)).as_posix()


def check_formal_names(found: Findings, repo_root: Path) -> None:
    """repo_root/reports/research/wavewarn_v20 下正式、失败、尾段、发布失败说明四类名字均不存在。"""
    research = repo_root.joinpath(*RESEARCH_DIR)
    names = sorted(item.name for item in research.iterdir()) if research.is_dir() else []
    conflicts = [name for name in names if name in FORMAL_NAMES or name.startswith(FORMAL_PREFIXES)]
    found.add("正式目录冲突", not conflicts, f"已存在：{conflicts}" if conflicts else "四类名字均不存在")


def check_confirmations(found: Findings, confirmations: dict) -> None:
    """人工确认项：每项非空，sha256 为 64 位十六进制，date 为 ISO 日期；只核对格式，不证明真实性。"""
    for key in REQUIRED_CONFIRMATIONS:
        item = confirmations.get(key)
        ok = isinstance(item, dict) and bool(str(item.get("原文", "")).strip()) and isinstance(item.get("sha256"), str)
        ok = ok and HEX64.fullmatch(item["sha256"]) is not None and valid_date(item.get("date"))
        found.add(f"人工确认 {key}", ok, "字段齐全（不证明真实性）" if ok else "未确认")


def valid_date(text: object) -> bool:
    if not isinstance(text, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) is None:
        return False
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def run_checks(checklist_path: Path) -> tuple[dict[str, Any], Findings, str]:
    """清单 JSON（须在两个允许根之一的下面）→ 结构核对 → 逐条核对。返回（结构化清单，结果，清单 SHA-256）。"""
    root = next((item for item in CHECKLIST_ROOTS if is_under(checklist_path, item)), None)
    if root is None:
        raise InputError(f"清单 JSON 须在 {[str(item) for item in CHECKLIST_ROOTS]} 之下：{checklist_path}")
    raw = read_checked(checklist_path, root, frozenset({lexical_relative(str(checklist_path), root)}))
    try:
        checklist = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError(f"清单 JSON 不可解析：{error}") from error
    if not isinstance(checklist, dict):
        raise InputError("清单 JSON 须为对象")
    spec = validated(checklist)
    found = Findings()
    check_versions(found, "仓库", spec["repo_root"], spec["expected_commit"])
    check_hashes(found, "仓库文件", spec["repo_root"], spec["file_hashes"])
    check_versions(found, "工具工作树", spec["tool_root"], spec["expected_tool_commit"])
    check_hashes(found, "工具文件", spec["tool_root"], spec["tool_file_hashes"])
    check_backups(found, spec["backup_root"], spec["backup_manifests"])
    check_formal_names(found, spec["repo_root"])
    check_confirmations(found, spec["manual_confirmations"])
    return spec, found, hashlib.sha256(raw).hexdigest()


def is_under(path: Path, root: Path) -> bool:
    try:
        lexical_parts(path, root)
    except InputError:
        return False
    return True


# ---------------------------------------------------------------------------
# 报告（唯一的写入处：先写同目录临时名，再改名；目标已存在即失败）
# ---------------------------------------------------------------------------


def write_report(out: Path, payload: bytes) -> None:
    if out.exists():
        raise InputError(f"报告已存在：{out}")
    partial = out.with_name(out.name + ".partial")
    try:
        with partial.open("xb") as handle:
            handle.write(payload)
        os.replace(partial, out)
    except OSError as error:
        if partial.is_file():
            partial.unlink()
        raise ReportError(f"报告写入失败：{error}；已写出部分：无（临时文件已删除）") from error


def report_bytes(checklist_path: Path, checklist_sha: str | None, found: Findings | None, code: int,
                 message: str | None) -> bytes:
    payload = {"工具": "v20_prerun_check", "清单": str(checklist_path), "清单 SHA-256": checklist_sha,
               "退出码": code, "退出码含义": {"0": "全部通过", "1": "有指出项或人工确认项未确认",
                                          "2": "参数或输入错误", "3": "报告写入失败"},
               "错误": message, "核对": [] if found is None else found.rows,
               "指出项数": None if found is None else found.flagged(),
               "说明": "检查能力已实现不等于真实数据检查已通过；人工确认项只核对字段非空与格式，不证明真实性"}
    return (json.dumps(payload, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="v2.0 开发期正式运行前检查（N8）")
    parser.add_argument("--checklist", required=True)
    parser.add_argument("--out", required=True)
    arguments = parser.parse_args(argv)
    checklist_path, out = Path(arguments.checklist), Path(arguments.out)
    found, digest, message = None, None, None
    try:
        _, found, digest = run_checks(checklist_path)
        code = 1 if found.flagged() else 0
    except InputError as error:
        code, message = 2, str(error)
    try:
        write_report(out, report_bytes(checklist_path, digest, found, code, message))
    except InputError as error:
        print(f"停止：{error}", file=sys.stderr)
        return 2
    except ReportError as error:
        print(f"停止：{error}", file=sys.stderr)
        return 3
    if message is not None:
        print(f"停止：{message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
