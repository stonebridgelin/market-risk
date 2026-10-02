"""波段预警 v2.0 的隔离检查：包边界、依赖方向与源码静态检查。

用途限定：本文件只读取项目源码（src/market_risk/wavewarn_v20/ 与 v2.0 的测试文件），以及做模块导入检查；
不读取受保护的市场数据目录，也不读取任何数据文件。
其中一个测试在新的解释器里导入模块（启动子进程，子进程只执行模块导入）；带拦截运行时它照实标“通过但覆盖不完整”。

前三个测试原在 tests/test_wavewarn_v20_boundaries.py 与 tests/test_wavewarn_v20_execution.py（阶段一已验收），
按负责人 2026-10-02 的裁决移到这里：函数体与期望值不改，只调整了必要的导入与它们用到的常量、辅助函数。
其后是阶段二新增的源码静态检查。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "market_risk" / "wavewarn_v20"
# wavewarn_v20 下任何模块都不得导入的模块（及其子模块）。
FORBIDDEN = ("market_risk.wavewarn", "market_risk.scoring", "market_risk.outcomes", "market_risk.data.market",
             "market_risk.data.snapshot", "market_risk.research")
# 阶段一的纯计算模块：除本包与标准库外不导入任何东西。
PURE = ("snapshot", "inputs", "channels", "state_machine", "convergence", "reference", "execution", "nav")
# 阶段二新增的纯计算模块（另由下面新增的测试检查）。
STAGE_TWO = ("labels_r2", "r2", "r1", "selection", "confirmatory")


def forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN)


def modules() -> list[str]:
    return sorted(f"market_risk.wavewarn_v20.{path.stem}" for path in PACKAGE.glob("*.py") if path.stem != "__init__")


# ---------------------------------------------------------------------------
# 自阶段一移来的三个测试（函数体与期望值不改）
# ---------------------------------------------------------------------------


def test_no_module_imports_forbidden_modules_statically() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        assert sorted(name for name in names if forbidden(name)) == [], path.name
        project = {name for name in names if name.startswith("market_risk")}
        if path.stem in PURE:
            assert all(name.startswith("market_risk.wavewarn_v20") for name in project), path.name


@pytest.mark.parametrize("module", modules())
def test_importing_a_module_loads_no_forbidden_module(module: str) -> None:
    """在全新的解释器里只导入这一个模块，检查实际被加载的全部模块（含间接导入）。"""
    code = ("import importlib, json, sys\n"
            f"importlib.import_module({module!r})\n"
            "print(json.dumps(sorted(name for name in sys.modules if name.startswith('market_risk'))))\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    loaded = json.loads(done.stdout)
    assert [name for name in loaded if forbidden(name)] == []
    assert all(name == "market_risk" or name.startswith("market_risk.wavewarn_v20") for name in loaded)


def test_no_research_function_returns_or_builds_an_actual_execution() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
                assert "ActualExecution" not in ast.unparse(node.returns), f"{path.name}:{node.name}"
            if isinstance(node, ast.Call):
                assert "ActualExecution" not in ast.unparse(node.func), f"{path.name} 构造了 ActualExecution"
        mentioned = any(isinstance(node, ast.Name) and node.id == "ActualExecution" for node in ast.walk(tree))
        assert not mentioned or path.name == "execution.py"


# ---------------------------------------------------------------------------
# 阶段二新增：源码静态检查（把拦截插件不覆盖的访问途径收窄到可检查的程度）
# ---------------------------------------------------------------------------

# 不得出现的内容：受保护目录的写法、子进程与 ctypes、文件读取与只取元数据的调用。
BANNED = ("data/market", r"data\market", "subprocess", "os.system", "ctypes", "multiprocessing", "open(",
          "read_text", "read_bytes", "read_csv", "loadtxt", "os.stat", ".exists(")
# 例外清单：v2.0 测试写入 pytest 临时目录的必要文件操作，逐处列出（文件名, 行内容）。目前没有任何例外。
EXCEPTIONS: frozenset[tuple[str, str]] = frozenset()


def checked_files() -> list[Path]:
    """适用范围：wavewarn_v20 的全部产品代码与 tests/test_wavewarn_v20_*.py。

    不含本文件、拦截插件 tests/v20_data_guard.py 与插件自测 tests/test_v20_data_guard.py（三者按各自的规定处理）。
    """
    return [*sorted(PACKAGE.glob("*.py")), *sorted((ROOT / "tests").glob("test_wavewarn_v20_*.py"))]


def test_static_check_covers_the_expected_files() -> None:
    names = {path.name for path in checked_files()}
    assert {f"{name}.py" for name in (*PURE, *STAGE_TWO)} <= names
    assert sum(name.startswith("test_wavewarn_v20_") for name in names) >= 10
    assert not {"v20_data_guard.py", "test_v20_data_guard.py", "test_v20_isolation.py"} & names


def test_v20_code_and_tests_contain_no_uncovered_access_paths() -> None:
    found = []
    for path in checked_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for banned in BANNED:
                if banned in line and (path.name, line.strip()) not in EXCEPTIONS:
                    found.append(f"{path.name}:{number}: {banned!r}")
    assert found == []


def test_banned_list_is_the_registered_one() -> None:
    assert len(BANNED) == 13 and BANNED[1] == "data" + chr(92) + "market"
    assert EXCEPTIONS == frozenset()


def test_stage_two_modules_import_only_this_package() -> None:
    """阶段二的五个模块也是纯计算：项目内只导入本包；产品代码不导入旧的波段预警包。"""
    for name in STAGE_TWO:
        tree = ast.parse((PACKAGE / f"{name}.py").read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        project = {item for item in names if item.startswith("market_risk")}
        assert all(item.startswith("market_risk.wavewarn_v20") for item in project), name
        assert not any(forbidden(item) for item in names), name
