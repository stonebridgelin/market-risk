"""v1.4 验证期“只能运行一次”的保护：用临时 git 仓库构造每一种拒绝情形。

临时仓库里没有任何行情数据；这些测试不读取、不运行验证期。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.wavewarn.config_v14 import ValidationConfig, load_validation_config
from market_risk.wavewarn.lock_guard import (
    LockError,
    find_git,
    parse_lock_record,
    static_problems,
    verify_lock,
)
from market_risk.wavewarn.validation_run import CONFIG_FILE, check_v14_lock, run_v14_validation

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_validation_config(ROOT / CONFIG_FILE)
PARAMS = CONFIG.model.base.paired_parameters()
RECORD = "docs/research/v14_锁定记录_2026-10-02.md"
CONFIG_FILES = ("config/wavewarn_v14.yaml", "config/wavewarn_v121.yaml", "config/wavewarn_v14_validation.yaml")
SPEC_FIELDS = {"v1.4 修订登记 SHA-256": "docs/research/波段预警研究规格_v1.4_修订登记.md",
               "v1.2.1 规格副本 SHA-256": "docs/research/波段预警研究规格_v1.2.1.md",
               "v1.3 修订登记 SHA-256": "docs/research/波段预警研究规格_v1.3_修订登记.md"}


def _git_executable() -> str:
    try:
        return find_git(CONFIG.git_executable)
    except LockError:
        pytest.skip("本机找不到 git，跳过需要临时仓库的测试")


def _git(repo: Path, *arguments: str) -> str:
    done = subprocess.run([_git_executable(), "-c", "user.name=test", "-c", "user.email=test@example.com",
                           "-c", "core.autocrlf=false", *arguments], cwd=repo, capture_output=True, check=True)
    return done.stdout.decode().strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _record_text(repo: Path, commit: str, heading: str = "# v1.4 锁定记录", setting: str = "K=5，θ_P=2.5%",
                 seeds: str = "20260929、20260910、20260940") -> str:
    """按草稿的字段名写一份锁定记录；哈希取临时仓库里各文件的实际值。"""
    configs = "；".join(f"`{name}`：`{_sha(repo / name)}`" for name in CONFIG_FILES)
    rows = [f"| 代码提交号 | `{commit}` |", f"| 配置 SHA-256 | {configs} |",
            *(f"| {field} | `{_sha(repo / path)}`（`{path}`） |" for field, path in SPEC_FIELDS.items()),
            f"| 选定设定 | **v1.4，{setting}** |", f"| 随机种子 | {seeds} |", "| 重抽样次数 | 10,000 |"]
    return "\n".join([heading, "", "| 字段 | 取值 |", "| --- | --- |", *rows, ""])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """临时仓库：真实的三份配置与三份内容任意的规格文件，提交为“代码提交”。"""
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "docs/research").mkdir(parents=True)
    for name in CONFIG_FILES:
        shutil.copyfile(ROOT / name, root / name)
    for index, path in enumerate(SPEC_FIELDS.values()):
        (root / path).write_text(f"规格文件 {index}\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "code")
    return root


def _lock(repo: Path, **changes: str) -> Path:
    """写入锁定记录并单独提交；返回记录路径。"""
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    (repo / RECORD).write_text(_record_text(repo, commit, **changes), encoding="utf-8", newline="\n")
    _git(repo, "add", RECORD)
    _git(repo, "commit", "-q", "-m", "lock")
    return repo / RECORD


def _refused(repo: Path, record: Path, text: str) -> None:
    with pytest.raises(LockError, match=text):
        verify_lock(repo, record, CONFIG, PARAMS)


def test_valid_lock_record_passes_and_reports_its_hash(repo: Path) -> None:
    record = _lock(repo)
    check = check_v14_lock(repo, record)
    # 核对通过：返回记录在仓库内的路径、HEAD 与记录本身的 SHA-256（HEAD 中已提交内容）。
    assert (check.path, check.head) == (RECORD, _git(repo, "rev-parse", "HEAD"))
    assert check.sha256 == _sha(record) and (check.record.k, check.record.theta_p) == (5, Decimal("0.025"))


def test_refuses_when_head_differs_from_code_commit_by_more_than_the_record(repo: Path) -> None:
    record = _lock(repo)
    # 锁定之后又提交了别的文件：HEAD 相对于代码提交的差异不再恰好只有锁定记录。
    (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "extra")
    _refused(repo, record, "不是恰好只有锁定记录这一个文件")


def test_refuses_when_record_commit_is_not_the_parent_state(repo: Path) -> None:
    # 记录里写的是更早的提交：其后还有一次改动配置之外文件的提交，差异多于一个文件。
    early = _git(repo, "rev-parse", "--short", "HEAD")
    (repo / "notes.md").write_text("后来的改动\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later")
    (repo / RECORD).write_text(_record_text(repo, early), encoding="utf-8", newline="\n")
    _git(repo, "add", RECORD)
    _git(repo, "commit", "-q", "-m", "lock")
    _refused(repo, repo / RECORD, "不是恰好只有锁定记录这一个文件")
    # 记录里的提交号在仓库中不存在。
    (repo / RECORD).write_text(_record_text(repo, "deadbee"), encoding="utf-8", newline="\n")
    _git(repo, "commit", "-q", "-am", "lock2")
    _refused(repo, repo / RECORD, "在仓库中不存在")


def test_refuses_uncommitted_changes_and_untracked_files(repo: Path) -> None:
    record = _lock(repo)
    # 已跟踪文件有未提交的改动。
    (repo / "config/wavewarn_v14.yaml").write_text("改动\n", encoding="utf-8")
    _refused(repo, record, "未提交的改动")
    _git(repo, "checkout", "--", "config/wavewarn_v14.yaml")
    verify_lock(repo, record, CONFIG, PARAMS)
    # 未跟踪的文件同样拒绝；锁定记录本身尚未提交时也属于这种情形。
    (repo / "scratch.txt").write_text("x\n", encoding="utf-8")
    _refused(repo, record, "未提交的改动")


def test_refuses_when_spec_or_config_hash_differs(repo: Path) -> None:
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    text = _record_text(repo, commit)
    # 把记录里 v1.3 登记的哈希换成别的值：与仓库中已提交的内容不一致。
    path = SPEC_FIELDS["v1.3 修订登记 SHA-256"]
    (repo / RECORD).write_text(text.replace(_sha(repo / path), "0" * 64), encoding="utf-8", newline="\n")
    _git(repo, "add", RECORD)
    _git(repo, "commit", "-q", "-m", "lock")
    _refused(repo, repo / RECORD, "v1.3_修订登记.md 的 SHA-256 与锁定记录不一致")
    # 记录里缺一份配置的哈希：无法解析，同样拒绝。
    broken = text.replace(f"`config/wavewarn_v121.yaml`：`{_sha(repo / 'config/wavewarn_v121.yaml')}`", "")
    (repo / RECORD).write_text(broken, encoding="utf-8", newline="\n")
    _git(repo, "commit", "-q", "-am", "lock2")
    _refused(repo, repo / RECORD, "找不到 config/wavewarn_v121.yaml 的 SHA-256")


def test_refuses_draft_wrong_setting_and_wrong_seeds(repo: Path) -> None:
    _refused(repo, _lock(repo, heading="# v1.4 锁定记录（草稿）"), "是草稿")
    repo_record = repo / RECORD
    commit = _git(repo, "rev-parse", "--short", "HEAD~1")
    for changes, message in (({"setting": "K=10，θ_P=2.5%"}, "不是审核过的"),
                             ({"seeds": "20260929、20260910、20260941"}, "随机种子与配置不一致")):
        repo_record.write_text(_record_text(repo, commit, **changes), encoding="utf-8", newline="\n")
        _git(repo, "commit", "-q", "-am", "lock")
        _refused(repo, repo_record, message)


def test_static_checks_on_parsed_record() -> None:
    text = "\n".join(["# v1.4 锁定记录（草稿）", "| 字段 | 取值 |", "| 代码提交号 | `5bc227f`（说明） |",
                      *(f"| {field} | `{'A' * 64}` |" for field in SPEC_FIELDS),
                      "| 配置 SHA-256 | " + "；".join(f"`{name}`：`{'B' * 64}`" for name in CONFIG_FILES) + " |",
                      "| 选定设定 | **v1.4，K=5，θ_P=2.5%**（登记顺序第 6 组） |",
                      "| 随机种子 | 主设定（区块 20）20260929；区块 10 为 20260910；区块 40 为 20260940。 |",
                      "| 重抽样次数 | 10,000 |"])
    record = parse_lock_record(text, CONFIG.locked_files)
    # 草稿的字段按表格解析：提交号、三份规格与三份配置的哈希、选定设定、三个种子与重抽样次数。
    assert (record.draft, record.commit, record.k, record.theta_p, record.resamples) == (
        True, "5bc227f", 5, Decimal("0.025"), 10_000)
    assert record.seeds == {20260929, 20260910, 20260940} and len(record.hashes) == 6
    assert record.hashes["config/wavewarn_v14_validation.yaml"] == "B" * 64
    # 演练允许草稿；正式运行不允许。
    assert static_problems(record, CONFIG, PARAMS, False) == []
    assert static_problems(record, CONFIG, PARAMS, True) == ["锁定记录是草稿，不是正式锁定记录"]
    with pytest.raises(LockError, match="缺少字段"):
        parse_lock_record("| 代码提交号 | `5bc227f` |", CONFIG.locked_files)


def test_refuses_when_git_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    # 配置里没有可用的路径、PATH 中也找不到：拒绝并给出处理办法。
    with pytest.raises(LockError, match="加入 PATH"):
        find_git("")
    with pytest.raises(LockError, match="git_executable"):
        find_git("Z:/不存在/git.exe")


def test_validation_command_refuses_existing_output_and_writes_lock_hash_first(repo: Path) -> None:
    record = _lock(repo)
    output = repo / CONFIG.validation_output
    # (2) 输出目录已存在验证期结果：在核对锁定记录之前就拒绝。
    output.mkdir(parents=True)
    with pytest.raises(LockError, match="拒绝再次运行"):
        run_v14_validation(repo, record)
    output.rmdir()
    # (1) 锁定记录核对不过：拒绝，且不创建输出目录。
    (repo / "scratch.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(LockError, match="拒绝运行验证期"):
        run_v14_validation(repo, record)
    assert not output.exists()
    (repo / "scratch.txt").unlink()
    # (3) 核对通过后，运行一开始就把锁定记录的 SHA-256 写入输出。临时仓库里没有任何行情数据，
    # 所以随后在读取输入时失败；此时输出目录里已经有锁定记录信息，再次运行会被拒绝。
    with pytest.raises(FileNotFoundError):
        run_v14_validation(repo, record)
    written = json.loads((output / "lock_record.json").read_text(encoding="utf-8"))
    assert written == {"lock_record": RECORD, "lock_record_sha256": _sha(record),
                       "code_commit": _git(repo, "rev-parse", "--short", "HEAD~1"),
                       "head": _git(repo, "rev-parse", "HEAD"), "formal": True}
    with pytest.raises(LockError, match="拒绝再次运行"):
        run_v14_validation(repo, record)


def test_validation_config_is_pinned_to_registration() -> None:
    assert isinstance(CONFIG, ValidationConfig)
    assert (CONFIG.locked_k, CONFIG.locked_theta, CONFIG.big_drop_threshold, CONFIG.significance) == (
        5, Decimal("0.025"), Decimal("0.15"), Decimal("0.05"))
    assert set(CONFIG.locked_files["配置 SHA-256"]) == set(CONFIG_FILES)
    # 改动锁定设定为网格外的值会被拒绝。
    import yaml

    from market_risk.wavewarn.config_v14 import parse_validation_config

    raw = yaml.safe_load((ROOT / CONFIG_FILE).read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="候选网格"):
        parse_validation_config({**raw, "locked_setting": {"k": 7, "theta_p": "0.025"}}, CONFIG.model)
    assert dataclasses.is_dataclass(CONFIG)
