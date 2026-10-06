"""运行前检查能力（N8，docs/audit/运行前检查/v20_prerun_check.py）的构造验收（阶段四 M2 第二部分指令修订六第六节
第 3 小节、第七节第 1 条、第十节第 2 小节）。

- 构造仓库、工具工作树与备份根都在 tmp_path 下（git init 后提交，固定作者与时间）；构造根下只放占位文件。
- N8 只在子进程中运行（sys.executable，cwd 为 tmp_path），每个 subprocess.run 都带 timeout；
  另以 git 建立构造仓库，以 cmd /c mklink /J 建立目录联接（Windows 下无需特权的“链接指向根外”）。
- N8 要求清单 JSON 位于仓库根或 D:\\temp_claude\\v20 之下：本文件的 tmp_path 须落在两者之一（检查脚本与开发性
  检查的 --basetemp 均在 D:\\temp_claude\\v20 下；未指定 --basetemp 时为仓库根下的 .pytest_tmp）。
- 带拦截运行时这些测试照实标“通过但覆盖不完整”（子进程中的访问不在外层拦截范围内）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "docs" / "audit" / "运行前检查" / "v20_prerun_check.py"
TIMEOUT = 120
GIT_ENV = {"GIT_AUTHOR_NAME": "构造", "GIT_AUTHOR_EMAIL": "construct@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_NAME": "构造",
           "GIT_COMMITTER_EMAIL": "construct@example.invalid", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}
REPO_FILES = ("pyproject.toml", "uv.lock", "config/wavewarn_v20.yaml", "src/market_risk/wavewarn_v20/registered_v20.py",
              "docs/audit/独立回算/v20/README.md", "docs/audit/运行前检查/v20_prerun_check.py")
TOOL_FILES = ("docs/audit/独立复核/v20/audit_v20.py", "docs/audit/独立复核/v20/README.md")
MARKET = "/".join(("data", "market", "daily", "SPX.csv"))        # 构造根下的行情占位（不写字面路径片段）
CONFIRMED = {"原文": "构造确认", "sha256": "a" * 64, "date": "2026-10-05"}


# ---------------------------------------------------------------------------
# 构造与读写辅助（写入、读取、子进程各集中在一处）
# ---------------------------------------------------------------------------


def put(path: Path, data: bytes) -> Path:
    """把构造的字节写进 tmp_path（必要时建立上级目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def get(path: Path) -> bytes:
    """读回 tmp_path 中由 N8 或审计钩子写出的文件。"""
    return path.read_bytes()


def run(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """子进程（git、cmd 或 sys.executable）；工作目录在 tmp_path 下。"""
    environment = {**os.environ, **GIT_ENV, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(arguments, cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=TIMEOUT)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_tree(root: Path, files: tuple[str, ...]) -> str:
    """建立构造 git 工作树并提交；返回 HEAD。"""
    for relative in files:
        put(root.joinpath(*relative.split("/")), f"# 构造占位：{relative}\n".encode())
    for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "构造"]):
        done = run(["git", "-c", "core.autocrlf=false", *command], root)
        assert done.returncode == 0, done.stderr
    return run(["git", "rev-parse", "HEAD"], root).stdout.strip()


def build(tmp_path: Path) -> dict:
    """构造仓库（含行情占位，不列入任何哈希）、工具工作树、备份根与一份全部通过的清单。"""
    repo, tool, backup = tmp_path / "repo", tmp_path / "tool", tmp_path / "backup"
    commit = git_tree(repo, (*REPO_FILES, MARKET))
    tool_commit = git_tree(tool, TOOL_FILES)
    manifest = put(backup / "v20" / "清单.md", "# 构造备份清单\n".encode())
    checklist = {"repo_root": str(repo), "tool_root": str(tool), "backup_root": str(backup),
                 "expected_commit": commit, "expected_tool_commit": tool_commit,
                 "file_hashes": {name: sha(get(repo.joinpath(*name.split("/")))) for name in REPO_FILES},
                 "tool_file_hashes": {name: sha(get(tool.joinpath(*name.split("/")))) for name in TOOL_FILES},
                 "backup_manifests": [{"path": "v20/清单.md", "sha256": sha(get(manifest))}],
                 "manual_confirmations": {"13_开发期结果处置预案已决定": dict(CONFIRMED),
                                          "14_授权C已授予": dict(CONFIRMED)}}
    return {"tmp": tmp_path, "repo": repo, "tool": tool, "backup": backup, "checklist": checklist}


def check(case: dict, name: str = "清单.json") -> tuple[int, dict | None, str]:
    """写清单 → 子进程运行 N8 → （退出码，报告，标准错误）。"""
    path = put(case["tmp"] / name, json.dumps(case["checklist"], ensure_ascii=False).encode("utf-8"))
    out = case["tmp"] / f"{name}.报告.json"
    done = run([sys.executable, "-B", str(CHECKER), "--checklist", str(path), "--out", str(out)], case["tmp"])
    report = json.loads(get(out).decode("utf-8")) if out.is_file() else None
    return done.returncode, report, done.stderr


def flagged(report: dict) -> list[str]:
    return [row["项"] for row in report["核对"] if row["状态"] != "通过"]


# ---------------------------------------------------------------------------
# 第六节第 3 小节与第十节第 2 小节：每行一例通过、一例指出项
# ---------------------------------------------------------------------------


def test_prerun_all_pass(tmp_path: Path) -> None:
    code, report, stderr = check(build(tmp_path))
    assert (code, report["指出项数"]) == (0, 0), stderr
    assert {row["项"] for row in report["核对"]} >= {"仓库 HEAD", "工具工作树 HEAD", "正式目录冲突"}


def test_prerun_commit_match(tmp_path: Path) -> None:
    case = build(tmp_path)
    case["checklist"]["expected_commit"] = "0" * 40
    case["checklist"]["expected_tool_commit"] = "1" * 40
    code, report, _ = check(case)
    assert code == 1 and flagged(report) == ["仓库 HEAD", "工具工作树 HEAD"]


def test_prerun_worktree_must_be_clean(tmp_path: Path) -> None:
    case = build(tmp_path)
    put(case["repo"] / "未提交.txt", b"x")
    put(case["tool"] / "未提交.txt", b"x")
    code, report, _ = check(case)
    assert code == 1 and flagged(report) == ["仓库 工作区与暂存区为空", "工具工作树 工作区与暂存区为空"]


def test_prerun_file_hash_mismatch(tmp_path: Path) -> None:
    case = build(tmp_path)
    case["checklist"]["file_hashes"]["uv.lock"] = "b" * 64
    case["checklist"]["tool_file_hashes"][TOOL_FILES[0]] = "c" * 64
    code, report, _ = check(case)
    assert code == 1 and flagged(report) == ["仓库文件 uv.lock", f"工具文件 {TOOL_FILES[0]}"]


def test_prerun_backup_manifest_hash_mismatch(tmp_path: Path) -> None:
    case = build(tmp_path)
    case["checklist"]["backup_manifests"][0]["sha256"] = "d" * 64
    code, report, _ = check(case)
    assert code == 1 and flagged(report) == ["备份清单 v20/清单.md"]


def test_prerun_formal_dir_conflict(tmp_path: Path) -> None:
    for name in ("evaluation_development", "evaluation_development_failed_20260101T000000Z_abcdef0"):
        case = build(tmp_path / name)
        (case["repo"] / "reports" / "research" / "wavewarn_v20" / name).mkdir(parents=True)   # 空目录：git 不显示
        code, report, _ = check(case)
        assert code == 1 and flagged(report) == ["正式目录冲突"], name


def test_prerun_manual_items_required(tmp_path: Path) -> None:
    case = build(tmp_path)
    case["checklist"]["manual_confirmations"] = {}
    code, report, _ = check(case)
    assert code == 1 and flagged(report) == ["人工确认 13_开发期结果处置预案已决定", "人工确认 14_授权C已授予"]
    case["checklist"]["manual_confirmations"] = {"13_开发期结果处置预案已决定": {**CONFIRMED, "sha256": "x"},
                                                 "14_授权C已授予": {**CONFIRMED, "date": "2026-13-01"}}
    code, report, _ = check(case, "清单二.json")
    assert code == 1 and len(flagged(report)) == 2


def test_prerun_report_write_failure(tmp_path: Path) -> None:
    """--out 的上级是文件（Windows 下目录无法设为只读拒写，以此构造写入失败）→ 退出码 3，不留下同名文件；
    --out 已存在 → 退出码 2，原文件不变。"""
    case = build(tmp_path)
    path = put(tmp_path / "清单.json", json.dumps(case["checklist"], ensure_ascii=False).encode("utf-8"))
    blocker = put(tmp_path / "不是目录", b"x")
    done = run([sys.executable, "-B", str(CHECKER), "--checklist", str(path), "--out", str(blocker / "报告.json")],
               tmp_path)
    assert done.returncode == 3 and "报告写入失败" in done.stderr
    existing = put(tmp_path / "已有报告.json", b"old")
    done = run([sys.executable, "-B", str(CHECKER), "--checklist", str(path), "--out", str(existing)], tmp_path)
    assert done.returncode == 2 and get(existing) == b"old"


# ---------------------------------------------------------------------------
# 路径边界：四例拒绝（.. 越界、同前缀兄弟目录、链接指向根外、data/market 出现）与清单位置
# ---------------------------------------------------------------------------


def rejected(case: dict, text: str) -> None:
    code, report, stderr = check(case)
    assert code == 2 and report is not None and report["核对"] == [] and text in report["错误"], stderr


def test_prerun_rejects_dotdot_escape(tmp_path: Path) -> None:
    case = build(tmp_path)
    put(tmp_path / "outside.md", b"x")
    case["checklist"]["backup_manifests"][0]["path"] = "../outside.md"
    rejected(case, "越界")


def test_prerun_rejects_same_prefix_sibling(tmp_path: Path) -> None:
    case = build(tmp_path)
    sibling = put(tmp_path / "backup_x" / "清单.md", b"x")
    case["checklist"]["backup_manifests"][0]["path"] = str(sibling)
    rejected(case, "越界")


def test_prerun_rejects_link_outside_root(tmp_path: Path) -> None:
    case = build(tmp_path)
    put(tmp_path / "outside" / "清单.md", b"x")
    done = run(["cmd", "/c", "mklink", "/J", str(case["backup"] / "link"), str(tmp_path / "outside")], tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    case["checklist"]["backup_manifests"][0]["path"] = "link/清单.md"
    rejected(case, "越界")


def test_prerun_rejects_market_data_path(tmp_path: Path) -> None:
    case = build(tmp_path)
    case["checklist"]["file_hashes"][MARKET] = "e" * 64
    rejected(case, "未登记类别")
    case = build(tmp_path / "备份")
    case["checklist"]["backup_manifests"][0]["path"] = MARKET
    rejected(case, "data")


def test_prerun_checklist_must_be_under_allowed_roots(tmp_path: Path) -> None:
    outside = Path(tmp_path.anchor) / "v20_prerun_not_allowed" / "清单.json"
    done = run([sys.executable, "-B", str(CHECKER), "--checklist", str(outside), "--out", str(tmp_path / "r.json")],
               tmp_path)
    assert done.returncode == 2 and "清单 JSON 须在" in done.stderr
    assert json.loads(get(tmp_path / "r.json").decode("utf-8"))["退出码"] == 2


HOOKED = (
    "import runpy, sys\n"
    "log = open(sys.argv[1], 'w', encoding='utf-8')\n"
    "def hook(event, args):\n"
    "    if event in ('open', 'os.scandir', 'os.listdir') and args:\n"
    "        log.write(f'{event}\\t{args[0]}\\n'); log.flush()\n"
    "sys.addaudithook(hook)\n"
    "sys.argv = sys.argv[2:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def test_prerun_never_opens_market_data(tmp_path: Path) -> None:
    """构造根含行情占位；N8 在带审计钩子的子进程中运行（通过与拒绝两种情形），记录中没有任何 data 下路径的打开或
    列目录事件。"""
    for label, patch in (("通过", None), ("拒绝", MARKET)):
        case = build(tmp_path / label)
        if patch is not None:
            case["checklist"]["file_hashes"][patch] = "e" * 64
        path = put(case["tmp"] / "清单.json", json.dumps(case["checklist"], ensure_ascii=False).encode("utf-8"))
        log = case["tmp"] / "审计.txt"
        done = run([sys.executable, "-B", "-c", HOOKED, str(log), str(CHECKER), "--checklist", str(path), "--out",
                    str(case["tmp"] / "报告.json")], case["tmp"])
        assert done.returncode == (0 if patch is None else 2), done.stderr
        events = get(log).decode("utf-8").splitlines()
        market = str(case["repo"].joinpath("data")).lower()
        assert events and not [line for line in events if market in line.lower()], label


# ---------------------------------------------------------------------------
# 第七节第 1 条：N8 的 AST 静态检查
# ---------------------------------------------------------------------------

ALLOWED_IMPORTS = {"__future__", "argparse", "datetime", "hashlib", "json", "os", "re", "subprocess", "sys",
                   "dataclasses", "pathlib", "typing"}
WRITE_CALLS = {"open": "write_report", "write_bytes": None, "write_text": None, "replace": "write_report",
               "unlink": "write_report", "mkdir": None, "rename": None, "read_bytes": "read_checked"}


def test_prerun_checker_imports_and_write_sites() -> None:
    """import 集合只含标准库白名单（不含 market_risk、numpy、pandas、yaml）；文件写入与改名只在 write_report，
    文件读取只在 read_checked；subprocess.run 只在 git_state。"""
    tree = ast.parse(get(CHECKER).decode("utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 and node.module}
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS
    sites: dict[str, set[str]] = {}
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
            if name in WRITE_CALLS or name == "run":
                sites.setdefault(name, set()).add(function.name)
    assert sites.get("run") == {"git_state"}
    for name, owner in WRITE_CALLS.items():
        assert sites.get(name, set()) <= ({owner} if owner else set()), (name, sites.get(name))
