"""波段预警 v2.0 的隔离检查：包边界、依赖方向与源码静态检查。

用途限定：本文件只读取项目源码（src/market_risk/wavewarn_v20/、包初始化文件与 v2.0 的测试文件），以及做模块导入检查；
不读取受保护的市场数据目录，也不读取任何数据文件。
其中一个测试在新的解释器里导入模块（启动子进程，子进程只执行模块导入）；带拦截运行时它照实标“通过但覆盖不完整”。

前三个测试原在 tests/test_wavewarn_v20_boundaries.py 与 tests/test_wavewarn_v20_execution.py（阶段一已验收），
按负责人 2026-10-02 的裁决移到这里：函数体与期望值不改，只调整了必要的导入与它们用到的常量、辅助函数。
其后是阶段二新增的源码静态检查。
阶段三按实施指令定稿二第十节修改了其中四个测试（导入集合、检查范围、禁用写法、清单登记），并新增依赖方向、
包初始化文件与核对入口顶层结构的检查；模块职责表、导入白名单、文件操作例外与测试职责表均按定稿第一节、第六节登记。
补充裁决（docs/research/v20_阶段三补充裁决.md）另加：按 AST 的三类检查与例外的定位计数、
行情读取模块打开调用的检查、拦截插件的检查，以及一个读取 config/wavewarn_v20.yaml 的一致性测试
（定稿第〇节“不读取真实项目配置”的具名例外，只此一处）。
"""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "market_risk" / "wavewarn_v20"
TESTS = ROOT / "tests"
# wavewarn_v20 下任何模块都不得导入的模块（及其子模块）。
FORBIDDEN = ("market_risk.wavewarn", "market_risk.scoring", "market_risk.outcomes", "market_risk.data.market",
             "market_risk.data.snapshot", "market_risk.research")
# 阶段一的纯计算模块：除本包与标准库外不导入任何东西。
PURE = ("snapshot", "inputs", "channels", "state_machine", "convergence", "reference", "execution", "nav")
# 阶段二新增的纯计算模块（另由下面新增的测试检查）。
STAGE_TWO = ("labels_r2", "r2", "r1", "selection", "confirmatory")

# ---------------------------------------------------------------------------
# 模块职责表（定稿第一节第 1 部分）
# ---------------------------------------------------------------------------

ALGORITHM = (*PURE, *STAGE_TWO, "dataset_v20", "provenance_v20")
BOUNDARY = ("data_v20", "config_v20", "verify_dataset")
INITIALIZERS = (ROOT / "src" / "market_risk" / "__init__.py", PACKAGE / "__init__.py")

# 导入白名单（定稿第一节第 2 部分，精确集合）：本包以外实际加载的 market_risk 模块。
OUTSIDE: dict[str, frozenset[str]] = {
    **{name: frozenset({"market_risk"}) for name in (*PURE, *STAGE_TWO, "dataset_v20")},
    "provenance_v20": frozenset({"market_risk", "market_risk.precision"}),
    "data_v20": frozenset({"market_risk", "market_risk.calendar", "market_risk.config", "market_risk.models",
                           "market_risk.precision"}),
    "config_v20": frozenset({"market_risk", "market_risk.config"}),
    "verify_dataset": frozenset({"market_risk"}),
}
# 实际加载的本包模块（不含包本身；每个集合都包含被检查模块自己）。
INSIDE: dict[str, frozenset[str]] = {name: frozenset(items.split()) for name, items in {
    "snapshot": "snapshot",
    "inputs": "inputs snapshot",
    "channels": "channels inputs snapshot",
    "state_machine": "channels inputs snapshot state_machine",
    "reference": "channels inputs reference snapshot state_machine",
    "convergence": "channels convergence inputs reference snapshot state_machine",
    "execution": "channels execution inputs reference snapshot state_machine",
    "nav": "channels execution inputs nav reference snapshot state_machine",
    "labels_r2": "labels_r2",
    "r2": "labels_r2 r2",
    "r1": "r1",
    "selection": "channels convergence inputs reference selection snapshot state_machine",
    "confirmatory": "confirmatory labels_r2",
    "dataset_v20": "dataset_v20",
    "provenance_v20": "provenance_v20",
    "data_v20": "data_v20 dataset_v20 snapshot",
    "config_v20": "config_v20 dataset_v20",
    "verify_dataset": "verify_dataset",
}.items()}

# ---------------------------------------------------------------------------
# 测试职责表（定稿第六节第 1 部分）；tests/ 下文件名含 v20 的文件每个恰好登记一次。
# 独立工具的两份测试（test_v20_independent_tool.py、test_v20_independent_compare.py）在其入库时登记。
# ---------------------------------------------------------------------------

PURE_TESTS = tuple(f"test_wavewarn_v20_{name}.py" for name in (
    "snapshot", "inputs", "channels", "state_machine", "convergence", "boundaries", "reference", "execution", "nav",
    "labels_r2", "r2", "r1_selection", "confirmatory", "provenance"))
TEST_DUTIES: dict[str, tuple[str, ...]] = {
    "纯算法测试": PURE_TESTS,
    "辅助文件": ("wavewarn_v20_helpers.py",),
    "构造文件读写测试": ("test_wavewarn_v20_data_entry.py", "test_wavewarn_v20_config_entry.py"),
    "子进程测试": ("test_wavewarn_v20_verify_dataset.py",),
    "隔离检查": ("test_v20_isolation.py",),
    "插件自测": ("test_v20_data_guard.py",),
    # 定稿的测试职责表没有列出拦截插件本身；这里登记为“照旧”，不适用静态检查（待负责人确认）。
    "拦截插件": ("v20_data_guard.py",),
}


def forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN)


def modules() -> list[str]:
    return sorted(f"market_risk.wavewarn_v20.{path.stem}" for path in PACKAGE.glob("*.py") if path.stem != "__init__")


def imported_names(path: Path) -> set[str]:
    """源码中出现的全部导入（含函数内的导入）；from 导入同时记下模块与“模块.名字”。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


# ---------------------------------------------------------------------------
# 自阶段一移来的三个测试（函数体与期望值不改；第二个按定稿第十节第 1 部分第 1 项修改）
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
    """在全新的解释器里只导入这一个模块，检查实际被加载的全部模块（含间接导入）与白名单的精确集合相等。"""
    code = ("import importlib, json, sys\n"
            f"importlib.import_module({module!r})\n"
            "print(json.dumps(sorted(name for name in sys.modules if name.startswith('market_risk'))))\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    loaded = json.loads(done.stdout)
    assert [name for name in loaded if forbidden(name)] == []
    prefix = "market_risk.wavewarn_v20"
    name = module.rsplit(".", 1)[1]
    outside = {item for item in loaded if item != prefix and not item.startswith(prefix + ".")}
    inside = {item.removeprefix(prefix + ".") for item in loaded if item.startswith(prefix + ".")}
    assert outside == OUTSIDE[name], name
    assert inside == INSIDE[name], name
    if name in ALGORITHM:
        assert not inside & set(BOUNDARY), name


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
# 源码静态检查（阶段二新增；阶段三按定稿第一节第 4 部分与补充裁决第三节改为按职责适用）
# ---------------------------------------------------------------------------

# 算法侧（算法模块、包初始化文件、纯算法测试、辅助文件）第一层：已验收的 13 项文本匹配，例外为空，不改为 AST。
BANNED = ("data/market", r"data\market", "subprocess", "os.system", "ctypes", "multiprocessing", "open(",
          "read_text", "read_bytes", "read_csv", "loadtxt", "os.stat", ".exists(")
EXCEPTIONS: frozenset[tuple[str, str]] = frozenset()

# 边界侧：文本匹配 19 项，另加两种登记调用；命中只允许出现在登记的位置（文件, 所在函数, 行内容），每处恰好一次。
BOUNDARY_BANNED = (*BANNED, "write_text", "write_bytes", "urllib", "requests", "socket", "http",
                   "_read_yaml(", "load_data_decisions(")
BOUNDARY_EXCEPTIONS: frozenset[tuple[str, str, str]] = frozenset({
    ("data_v20.py", "read_file_bytes", 'with path.open("rb") as file:'),
    ("config_v20.py", "load_v20_config", "raw = project_config._read_yaml(dataset_path)"),
    ("config_v20.py", "load_v20_config", "if not decisions_path.exists():"),
    ("config_v20.py", "load_v20_config", "loaded = project_config.load_data_decisions(decisions_path)"),
})

# AST 检查的三类写法（补充裁决第三节第 1 部分“检查方式”与补充第 3 条）。
NETWORK = ("urllib", "requests", "socket", "http")
# 调用类：禁用写法 → AST 中的函数名或属性名。
CALLS = {"os.system": "system", "open(": "open", "read_text": "read_text", "read_bytes": "read_bytes",
         "read_csv": "read_csv", "loadtxt": "loadtxt", "os.stat": "stat", ".exists(": "exists",
         "write_text": "write_text", "write_bytes": "write_bytes", "addaudithook": "addaudithook"}
SUBPROCESS_CALLS = ("run", "Popen", "call", "check_call", "check_output")
PATH_FRAGMENTS = ("data/market", "data\\market")


@dataclasses.dataclass(frozen=True)
class Rules:
    """一组 AST 检查规则：导入类的模块名、调用类的名称、是否识别 subprocess 调用、路径字符串片段。"""

    imports: tuple[str, ...]
    calls: tuple[str, ...]
    subprocess_calls: bool
    fragments: tuple[str, ...]


# 算法侧第二层（A2，属收紧）：第 14 至 19 项与 addaudithook，按 AST；例外为空。
ALGORITHM_RULES = Rules(NETWORK, ("write_text", "write_bytes", "addaudithook"), False, ())
# 测试侧（A1 与补充第 3 条）：导入、调用（含 subprocess 的五个函数）、路径字符串。
TEST_RULES = Rules(("subprocess", "ctypes", "multiprocessing", *NETWORK), tuple(CALLS.values()), True,
                   PATH_FRAGMENTS)
# 测试侧没有例外可言的写法：网络导入与受保护目录的字面路径。
TEST_NEVER = (*NETWORK, *PATH_FRAGMENTS)


@dataclasses.dataclass(frozen=True)
class Hit:
    """一处实际违规命中。"""

    file: str
    function: str          # 所在函数的完整限定名；模块顶层为 "<module>"
    text: str              # 命中所在的完整源码片段（statement_source 提取）
    item: str              # 命中的禁用写法（导入的模块名、调用名或路径片段）
    line: int


@dataclasses.dataclass(frozen=True)
class Allowed:
    """一条登记的实际违规例外（补充裁决第三节第 3 部分的字段）。"""

    file: str
    function: str
    text: str              # 登记的完整源码片段，与 statement_source 的提取方式相同
    item: str
    reason: str
    scope: str
    in_function: int       # 该原文在登记函数中的预期命中次数
    in_file: int           # 该原文在整个文件中的预期命中次数


DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
COMPOUND = (*DEFINITIONS, ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar,
            ast.Match)


def inner_name(scope: str, name: str) -> str:
    return name if scope == "<module>" else f"{scope}.{name}"


def owners(tree: ast.Module) -> dict[int, str]:
    """每个节点所属作用域的完整限定名，按“定义发生的外层作用域”口径（补充裁决第一部分第 7 条）：
    函数与类的定义节点本身、装饰器、参数（含默认值与注解）、返回注解、基类与关键字都归外层；
    只有定义体（body）中的节点归该函数或类。模块级记为 "<module>"，类体中记为该类，嵌套函数中记为外层函数。"""
    result: dict[int, str] = {}

    def mark(node: ast.AST, scope: str) -> None:
        result[id(node)] = scope
        if isinstance(node, DEFINITIONS):
            body = {id(statement) for statement in node.body}
            inner = inner_name(scope, node.name)
            for child in ast.iter_child_nodes(node):
                mark(child, inner if id(child) in body else scope)
        else:
            for child in ast.iter_child_nodes(node):
                mark(child, scope)

    for child in ast.iter_child_nodes(tree):
        mark(child, "<module>")
    return result


def statement_source(source: str, tree: ast.Module, node: ast.AST) -> str:
    """命中所在的完整源码片段（补充裁决第一部分第 1 条），用 ast.get_source_segment 提取，多行参数全部保留：
    - 命中在简单语句中：取整条语句；
    - 命中在复合语句（函数、类、if、for、while、with、try 等）的头部，如装饰器、参数默认值、with 的上下文表达式：
      取包含命中的最外层表达式，不取整个复合语句，以免放宽到整个函数或语句块。"""
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    chain = [node]                                      # 从命中节点向上直到最近的语句
    while not isinstance(chain[-1], ast.stmt):
        chain.append(parents[id(chain[-1])])
    statement = chain[-1]
    if isinstance(statement, COMPOUND):
        # 命中在复合语句的头部：取紧挨该语句之下、包含命中的最外层表达式。
        target = next(item for item in reversed(chain[:-1]) if isinstance(item, ast.expr))
    else:
        target = statement
    segment = ast.get_source_segment(source, target)
    assert segment is not None, ast.dump(target)
    return segment


def matches_module(name: str, modules: tuple[str, ...]) -> str | None:
    return next((module for module in modules if name == module or name.startswith(module + ".")), None)


def ast_hits(name: str, source: str, rules: Rules) -> list[Hit]:
    """按三类方式找出一份源码中的全部实际违规命中（只识别同一文件中的直接导入映射，不追踪赋值等）。"""
    tree = ast.parse(source)
    scope = owners(tree)
    # subprocess 的模块别名与直接导入的名称（含别名）→ 原名。
    module_aliases = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                      for alias in node.names if alias.name == "subprocess"}
    direct_names = {alias.asname or alias.name: alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module == "subprocess" and node.level == 0
                    for alias in node.names if alias.name in SUBPROCESS_CALLS}
    hits: list[Hit] = []

    def add(node: ast.AST, item: str) -> None:
        number = node.lineno                                                         # type: ignore[attr-defined]
        hits.append(Hit(name, scope.get(id(node), "<module>"), statement_source(source, tree, node), item, number))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (module := matches_module(alias.name, rules.imports)) is not None:
                    add(node, module)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if (module := matches_module(node.module, rules.imports)) is not None:
                add(node, module)
        elif isinstance(node, ast.Call):
            function = node.func
            called = function.id if isinstance(function, ast.Name) else (
                function.attr if isinstance(function, ast.Attribute) else None)
            if called in rules.calls:
                add(node, called)
            if rules.subprocess_calls:
                if (isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
                        and function.value.id in module_aliases and function.attr in SUBPROCESS_CALLS):
                    add(node, f"subprocess.{function.attr}")
                elif isinstance(function, ast.Name) and function.id in direct_names:
                    add(node, f"subprocess.{direct_names[function.id]}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for fragment in rules.fragments:
                if fragment in node.value:
                    add(node, fragment)
    return sorted(hits, key=lambda hit: (hit.line, hit.item))


def exception_problems(hits: list[Hit], allowed: frozenset[Allowed]) -> list[str]:
    """例外的定位与计数（补充裁决第三节第 3 部分第 1 至 5 条）：
    每处命中恰好对应一条登记例外（文件、函数、原文、写法一致）；函数内与全文件的命中次数等于登记次数；
    同一文件其他函数中出现相同原文即失败；登记了却没有命中的例外同样失败。"""
    problems: list[str] = []
    keys = {(item.file, item.function, item.text, item.item): item for item in allowed}
    for hit in hits:
        if (hit.file, hit.function, hit.text, hit.item) not in keys:
            problems.append(f"未登记的命中：{hit.file}:{hit.line} {hit.function} {hit.item!r} {hit.text}")
    for item in allowed:
        in_function = sum(1 for hit in hits if (hit.file, hit.function, hit.text, hit.item) == (
            item.file, item.function, item.text, item.item))
        in_file = sum(1 for hit in hits if (hit.file, hit.text, hit.item) == (item.file, item.text, item.item))
        if in_function == 0:
            problems.append(f"登记了却没有命中：{item.file} {item.function} {item.text}")
        elif in_function != item.in_function or in_file != item.in_file:
            problems.append(f"命中次数不符：{item.file} {item.function} {item.text}"
                            f"（函数内 {in_function}/{item.in_function}，全文件 {in_file}/{item.in_file}）")
    return problems


WRITE_REASON = "构造文件读写测试必须先把构造的字节或文本写进临时目录，才能用被测函数读取"
TEST_EXCEPTIONS: frozenset[Allowed] = frozenset({
    Allowed("test_wavewarn_v20_data_entry.py", "put", "path.write_bytes(data)", "write_bytes",
            WRITE_REASON, "只写 tmp_path 下的构造行情文件（path = tmp_path / name）", 1, 1),
    Allowed("test_wavewarn_v20_config_entry.py", "write", 'path.write_text(text, encoding="utf-8")', "write_text",
            WRITE_REASON, "只写 tmp_path 下 config/ 之中的构造配置文件（root 均为 tmp_path）", 1, 1),
    Allowed("test_wavewarn_v20_verify_dataset.py", "put", "path.write_bytes(data)", "write_bytes",
            WRITE_REASON, "只写 tmp_path 下按仓库布局放置的构造行情文件与构造配置文件", 1, 1),
    Allowed("test_wavewarn_v20_verify_dataset.py", "<module>", "import subprocess", "subprocess",
            "入口的验收必须在新的解释器里执行（定稿第七节第 5 部分）", "只供本文件的 run 函数使用", 1, 1),
    # 整条 return 语句（两行，含全部参数；第二行保留原文件中的续行缩进）。
    Allowed("test_wavewarn_v20_verify_dataset.py", "run",
            "return subprocess.run([sys.executable, *arguments], cwd=root, env=environment, capture_output=True, "
            "text=True,\n                          encoding=\"utf-8\", errors=\"replace\", check=False)",
            "subprocess.run",
            "入口的验收必须在新的解释器里执行（定稿第七节第 5 部分）",
            "只以 sys.executable 启动子进程（-m 入口或 -c 登记脚本），工作目录与 --root 都是 tmp_path", 1, 1),
})


def module_file(name: str) -> Path:
    return PACKAGE / f"{name}.py"


def duty_files(duty: str) -> list[Path]:
    return [TESTS / name for name in TEST_DUTIES[duty]]


def algorithm_files() -> list[Path]:
    return [*(module_file(name) for name in ALGORITHM), *INITIALIZERS, *duty_files("纯算法测试"),
            *duty_files("辅助文件")]


def boundary_files() -> list[Path]:
    return [module_file(name) for name in BOUNDARY]


def constructed_test_files() -> list[Path]:
    return [*duty_files("构造文件读写测试"), *duty_files("子进程测试")]


def source_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


UNDECIDABLE = "归属无法唯一判断"


def line_scopes(source: str) -> dict[int, str]:
    """按行号给出作用域，与 owners 同一口径（“定义发生的外层作用域”）：
    一行落在某个函数或类的定义体范围内（定义体第一条语句所在行至定义结束行），就归该函数或类，取最内层；
    装饰器行、参数与默认值所在的签名行都在定义体之前，归外层；其余行为 "<module>"。
    定义头与定义体写在同一物理行（如 def f(x=open("a")): return 1）时，按行无法区分命中属于头部还是函数体，
    该行记为 UNDECIDABLE（“归属无法唯一判断”）；它不等于任何登记的作用域，所以不能据此命中例外而放行。"""
    tree = ast.parse(source)
    scope = owners(tree)
    spans: list[tuple[int, int, str]] = []
    shared: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, DEFINITIONS):
            spans.append((node.body[0].lineno, node.end_lineno or node.lineno,
                          inner_name(scope[id(node)], node.name)))
            # 定义头的全部节点（装饰器、参数及其默认值与注解、返回注解、基类与关键字）中最靠后的结束行；
            # ast.arguments 本身没有位置信息，所以逐层遍历取其内部节点。
            header = [item for child in ast.iter_child_nodes(node)
                      if not any(child is statement for statement in node.body)
                      for item in ast.walk(child) if getattr(item, "end_lineno", None) is not None]
            header_end = max([node.lineno, *(item.end_lineno for item in header)])
            if node.body[0].lineno <= header_end:
                shared.add(node.body[0].lineno)
    result: dict[int, str] = {}
    for number in range(1, len(source.splitlines()) + 1):
        covering = [(end - start, name) for start, end, name in spans if start <= number <= end]
        result[number] = min(covering)[1] if covering else "<module>"
        if number in shared:
            result[number] = UNDECIDABLE
    return result


def text_hits(name: str, source: str, banned: tuple[str, ...]) -> list[tuple[str, str, str, int, str]]:
    """文本匹配：逐行查找禁用写法，返回（文件, 所在作用域, 行内容, 行号, 写法）。"""
    by_line = line_scopes(source)
    return [(name, by_line[number], line.strip(), number, banned_item)
            for number, line in enumerate(source.splitlines(), start=1)
            for banned_item in banned if banned_item in line]


def text_scan(paths: list[Path], banned: tuple[str, ...]) -> list[tuple[str, str, str, int, str]]:
    return [item for path in paths for item in text_hits(path.name, source_of(path), banned)]


def test_static_check_covers_the_expected_files() -> None:
    """检查范围由模块职责表与测试职责表确定，两表覆盖包内全部模块与 tests/ 下全部含 v20 的文件。"""
    stems = {path.stem for path in PACKAGE.glob("*.py")}
    assert stems == {*ALGORITHM, *BOUNDARY, "__init__"}
    assert not set(ALGORITHM) & set(BOUNDARY) and set(OUTSIDE) == set(INSIDE) == {*ALGORITHM, *BOUNDARY}
    registered = [name for names in TEST_DUTIES.values() for name in names]
    assert len(registered) == len(set(registered))                                # 每个文件恰好登记一次
    assert set(registered) == {path.name for path in TESTS.glob("*.py") if "v20" in path.name}
    assert "wavewarn_v20_helpers.py" in registered and len(PURE_TESTS) == 14
    for path in (*algorithm_files(), *boundary_files(), *constructed_test_files()):
        assert path.is_file(), path.name


def test_v20_code_and_tests_contain_no_uncovered_access_paths() -> None:
    # 算法侧第一层：13 项文本匹配，例外为空。
    assert [item for item in text_scan(algorithm_files(), BANNED)
            if (item[0], item[2]) not in EXCEPTIONS] == []
    # 算法侧第二层：第 14 至 19 项与 addaudithook，按 AST，例外为空。
    assert [hit for path in algorithm_files() for hit in ast_hits(path.name, source_of(path), ALGORITHM_RULES)] == []
    # 边界侧：文本匹配，只允许登记的位置，每处恰好一次。
    found = text_scan(boundary_files(), BOUNDARY_BANNED)
    assert [item for item in found if item[:3] not in BOUNDARY_EXCEPTIONS] == []
    assert sorted(item[:3] for item in found) == sorted(BOUNDARY_EXCEPTIONS)
    # 测试侧：按 AST 的三类方式找出实际违规命中；网络导入与受保护路径没有例外；其余逐处对应登记例外。
    hits = [hit for path in constructed_test_files() for hit in ast_hits(path.name, source_of(path), TEST_RULES)]
    assert [hit for hit in hits if hit.item in TEST_NEVER] == []
    assert exception_problems(hits, TEST_EXCEPTIONS) == []


def test_banned_list_is_the_registered_one() -> None:
    assert len(BANNED) == 13 and BANNED[1] == "data" + chr(92) + "market"
    assert EXCEPTIONS == frozenset()
    assert BOUNDARY_BANNED == (*BANNED, "write_text", "write_bytes", "urllib", "requests", "socket", "http",
                               "_read_yaml(", "load_data_decisions(")
    assert len(BOUNDARY_EXCEPTIONS) == 4
    assert {name for name, _, _ in BOUNDARY_EXCEPTIONS} == {"data_v20.py", "config_v20.py"}
    assert ALGORITHM_RULES == Rules(("urllib", "requests", "socket", "http"), ("write_text", "write_bytes",
                                                                                "addaudithook"), False, ())
    assert TEST_RULES.imports == ("subprocess", "ctypes", "multiprocessing", "urllib", "requests", "socket", "http")
    assert set(TEST_RULES.calls) == {"system", "open", "read_text", "read_bytes", "read_csv", "loadtxt", "stat",
                                     "exists", "write_text", "write_bytes", "addaudithook"}
    assert TEST_RULES.subprocess_calls and SUBPROCESS_CALLS == ("run", "Popen", "call", "check_call", "check_output")
    assert TEST_RULES.fragments == ("data/market", "data" + chr(92) + "market")
    # 测试侧例外逐项等于实现时登记的清单（随汇报提交，由负责人核对；尚未批准）。
    registered = sorted((item.file, item.function, item.item, item.in_function, item.in_file)
                        for item in TEST_EXCEPTIONS)
    assert registered == [
        ("test_wavewarn_v20_config_entry.py", "write", "write_text", 1, 1),
        ("test_wavewarn_v20_data_entry.py", "put", "write_bytes", 1, 1),
        ("test_wavewarn_v20_verify_dataset.py", "<module>", "subprocess", 1, 1),
        ("test_wavewarn_v20_verify_dataset.py", "put", "write_bytes", 1, 1),
        ("test_wavewarn_v20_verify_dataset.py", "run", "subprocess.run", 1, 1),
    ]
    assert not {item.item for item in TEST_EXCEPTIONS} & set(TEST_NEVER)


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


# ---------------------------------------------------------------------------
# 阶段三新增（定稿第十节第 2 部分）
# ---------------------------------------------------------------------------


def boundary_reference(names: set[str], targets: tuple[str, ...]) -> list[str]:
    """导入中指向本包某些模块的项（含 from market_risk.wavewarn_v20 import 模块名 的写法）。"""
    full = tuple(f"market_risk.wavewarn_v20.{target}" for target in targets)
    return sorted(name for name in names if any(name == item or name.startswith(item + ".") for item in full))


def test_algorithm_modules_do_not_import_boundary_modules() -> None:
    for name in ALGORITHM:
        assert boundary_reference(imported_names(module_file(name)), BOUNDARY) == [], name


def test_data_and_config_modules_do_not_import_each_other() -> None:
    assert boundary_reference(imported_names(module_file("data_v20")), ("config_v20", "verify_dataset")) == []
    assert boundary_reference(imported_names(module_file("config_v20")), ("data_v20", "verify_dataset")) == []


def test_data_module_does_not_import_config_models_or_provenance_directly() -> None:
    names = imported_names(module_file("data_v20"))
    direct = ("market_risk.config", "market_risk.models")
    # 含 from market_risk import config / models 的写法（记为“market_risk.config”等）。
    assert not [name for name in names if any(name == item or name.startswith(item + ".") for item in direct)]
    assert boundary_reference(names, ("provenance_v20",)) == []


def test_package_initializers_contain_no_executable_code() -> None:
    for path in INITIALIZERS:
        body = ast.parse(path.read_text(encoding="utf-8")).body
        docstring = len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str)
        assert body == [] or docstring, path


def test_verify_dataset_top_level_structure() -> None:
    tree = ast.parse(module_file("verify_dataset").read_text(encoding="utf-8"))
    body = tree.body
    assert isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)   # 文档字符串
    for node in body[1:]:
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None and node.level == 0
            assert node.module == "__future__" or node.module.split(".")[0] in sys.stdlib_module_names
        elif isinstance(node, ast.If):
            assert node is body[-1] and ast.unparse(node.test) == "__name__ == '__main__'"
            assert ast.unparse(node).splitlines()[1:] == ["    main()"] and node.orelse == []
        else:
            assert isinstance(node, ast.FunctionDef), ast.unparse(node)
    # sys.addaudithook 只出现在入口函数中。
    hooks = [function.name for function in body if isinstance(function, ast.FunctionDef)
             for node in ast.walk(function) if isinstance(node, ast.Attribute) and node.attr == "addaudithook"]
    assert hooks == ["main"]
    every = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "addaudithook"]
    assert len(every) == 1


# ---------------------------------------------------------------------------
# 补充裁决新增：行情读取模块的打开调用、审计钩子、拦截插件、检查器自身、一致性
# ---------------------------------------------------------------------------

READ_FUNCTION = ("read_file_bytes", "path")        # 登记的读文件函数与它的路径参数名


def test_data_module_has_exactly_one_binary_open_on_the_registered_path() -> None:
    """唯一允许的打开调用：在登记的读文件函数内，对登记的路径参数调用 path.open("rb") 或 path.open(mode="rb")，
    只有这一个参数。其他一切 open(...)、属性名为 open 或 fdopen 的调用都拒绝（补充裁决第二节第 1 条）。"""
    tree = ast.parse(source_of(module_file("data_v20")))
    scope = owners(tree)
    function_name, parameter = READ_FUNCTION
    definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    assert [argument.arg for argument in definition.args.args] == [parameter]
    allowed, other = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id == "open":
            other.append(ast.unparse(node))
        elif isinstance(function, ast.Attribute) and function.attr in ("open", "fdopen"):
            positional = (len(node.args) == 1 and not node.keywords and isinstance(node.args[0], ast.Constant)
                          and node.args[0].value == "rb")
            keyword = (not node.args and len(node.keywords) == 1 and node.keywords[0].arg == "mode"
                       and isinstance(node.keywords[0].value, ast.Constant) and node.keywords[0].value.value == "rb")
            registered = (scope.get(id(node)) == function_name and function.attr == "open"
                          and isinstance(function.value, ast.Name) and function.value.id == parameter)
            (allowed if registered and (positional or keyword) else other).append(ast.unparse(node))
    assert allowed == ["path.open('rb')"] and other == []
    # 不出现读文件的调用，不导入 io、os、mmap、codecs、builtins。
    called = {node.func.id if isinstance(node.func, ast.Name) else node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)}
    assert not called & {"read_text", "read_bytes", "read_csv", "loadtxt"}
    names = imported_names(module_file("data_v20"))
    assert not [name for name in names if matches_module(name, ("io", "os", "mmap", "codecs", "builtins"))]


def test_addaudithook_is_called_only_in_the_entry_function() -> None:
    """产品代码中只有 verify_dataset.main 调用 sys.addaudithook，且只有一处。"""
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(source_of(path))
        scope = owners(tree)
        found += [(path.stem, scope.get(id(node))) for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "addaudithook"]
    assert found == [("verify_dataset", "main")]


GUARD = TESTS / "v20_data_guard.py"


def test_guard_plugin_starts_no_process_and_imports_no_network_module() -> None:
    """拦截插件（A3）：不导入 subprocess、multiprocessing 与网络模块；不调用 os.system、os.exec*、os.spawn*、
    os.posix_spawn。以字符串出现的审计事件名及对这些事件的处理不算启动子进程。"""
    tree = ast.parse(source_of(GUARD))
    names = imported_names(GUARD)
    assert not [name for name in names if matches_module(name, ("subprocess", "multiprocessing", *NETWORK))]
    launching = [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "os" and (node.func.attr == "system" or node.func.attr == "posix_spawn"
                                                     or node.func.attr.startswith(("exec", "spawn")))]
    assert launching == []
    from_os = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "os"
               for alias in node.names]
    assert not [name for name in from_os if name in ("system", "posix_spawn") or name.startswith(("exec", "spawn"))]


def test_guard_plugin_is_loaded_only_explicitly() -> None:
    """拦截插件不被 src/ 下任何模块导入；仓库根与 tests/ 下的 conftest、pyproject.toml 的配置文本中不含插件名。
    这只说明配置文本中不含插件名，不表示任何运行方式都不会自动加载插件。
    pyproject.toml 的文本读取是第二处具名读取例外（补充裁决第一部分第 6 条），只此一个路径。"""
    importing = [path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "src").rglob("*.py"))
                 if any("v20_data_guard" in name for name in imported_names(path))]
    assert importing == []
    # 只查仓库根与 tests/ 下的 conftest（不遍历仓库根目录，以免进入受保护的数据目录）。
    candidates = [ROOT / "conftest.py", *sorted(TESTS.rglob("conftest.py")), ROOT / "pyproject.toml"]
    for path in [path for path in candidates if path.is_file()]:
        assert "v20_data_guard" not in source_of(path), path


def test_checker_recognises_the_three_ways_to_call_a_subprocess_function() -> None:
    """检查器的构造测试（补充第 3 条）：普通属性调用、模块别名调用、直接导入的名称及其别名，各命中一次；
    未登记的调用使检查失败。只识别同一文件中的直接导入映射，赋值、getattr、importlib 不追踪。"""
    source = (
        "import subprocess\n"
        "import subprocess as sp\n"
        "from subprocess import run\n"
        "from subprocess import check_output as launch\n"
        "def plain():\n"
        "    subprocess.run(['x'])\n"
        "def aliased():\n"
        "    sp.Popen(['x'])\n"
        "def direct():\n"
        "    run(['x'])\n"
        "    launch(['x'])\n"
        "def untracked():\n"
        "    alias = subprocess\n"
        "    alias.call(['x'])\n"
        "    getattr(subprocess, 'check_call')(['x'])\n"
        "    script = 'subprocess.run([1])'\n"
    )
    hits = ast_hits("constructed.py", source, TEST_RULES)
    calls = [(hit.function, hit.item) for hit in hits if hit.item.startswith("subprocess.")]
    assert calls == [("plain", "subprocess.run"), ("aliased", "subprocess.Popen"), ("direct", "subprocess.run"),
                     ("direct", "subprocess.check_output")]
    imports = [(hit.function, hit.text) for hit in hits if hit.item == "subprocess"]
    assert [text for _, text in imports] == ["import subprocess", "import subprocess as sp",
                                             "from subprocess import run",
                                             "from subprocess import check_output as launch"]
    # 全部登记：检查通过；少登记一处：失败；多登记一处：失败；同一原文出现在其他函数：失败。
    registered = frozenset(Allowed(hit.file, hit.function, hit.text, hit.item, "构造", "构造", 1, 1) for hit in hits)
    assert exception_problems(hits, registered) == []
    missing = frozenset(item for item in registered if item.function != "aliased")
    assert any("未登记的命中" in problem for problem in exception_problems(hits, missing))
    extra = registered | {Allowed("constructed.py", "plain", "subprocess.call(['x'])", "subprocess.call",
                                  "构造", "构造", 1, 1)}
    assert any("登记了却没有命中" in problem for problem in exception_problems(hits, extra))
    twice = source + "def again():\n    subprocess.run(['x'])\n"
    twice_hits = ast_hits("constructed.py", twice, TEST_RULES)
    problems = exception_problems(twice_hits, registered)
    assert any("未登记的命中" in problem for problem in problems)
    assert any("命中次数不符" in problem for problem in problems)


SCOPE_SOURCE = (
    "open('m')\n"                                   # 1 模块级
    "@deco(open('d1'))\n"                           # 2 装饰器：归外层（模块）
    "def f(a=open('a1')):\n"                        # 3 参数默认值：归外层（模块）
    "    open('f')\n"                               # 4 函数体：f
    "    @deco(open('d2'))\n"                       # 5 嵌套函数的装饰器：归外层函数 f
    "    def g(b=open('a2')):\n"                    # 6 嵌套函数的默认值：归外层函数 f
    "        open('g')\n"                           # 7 嵌套函数体：f.g
    "    return g\n"
    "class C(Base(open('base'))):\n"                # 9 基类：归外层（模块）
    "    x = open('c')\n"                           # 10 类体：C
    "    @deco(open('d3'))\n"                       # 11 方法的装饰器：归类 C
    "    def m(self, c=open('a3')):\n"              # 12 方法的默认值：归类 C
    "        open('m')\n"                           # 13 方法体：C.m
    "def h(\n"
    "    k=open('a4'),\n"                           # 15 多行签名中的默认值：归外层（模块）
    "):\n"
    "    return open('h')\n"                        # 17 函数体：h
)
SCOPE_EXPECTED = [(1, "<module>"), (2, "<module>"), (3, "<module>"), (4, "f"), (5, "f"), (6, "f"), (7, "f.g"),
                  (9, "<module>"), (10, "C"), (11, "C"), (12, "C"), (13, "C.m"), (15, "<module>"), (17, "h")]


def test_reason_codes_of_data_and_label_modules_are_identical() -> None:
    """补修 Q5：data_v20 与 labels_r2 各自定义取值相同的原因码（不新建共用模块，不改已批准的加载集合）。
    两处定义的一致只由本测试保证；以后修改任何一边，须同时修改另一边。本测试导入两边，不改变产品模块的加载集合。"""
    from market_risk.wavewarn_v20 import data_v20, labels_r2

    names = ("REASON_MISSING_PRICE", "REASON_INVALID_INPUT", "REASON_UNEXPECTED")
    assert [getattr(data_v20, name) for name in names] == [getattr(labels_r2, name) for name in names] == [
        "缺少必需价格", "输入校验失败", "未预期异常"]
    assert data_v20.MissingPriceEntryError.reason == labels_r2.MissingPriceError.reason == "缺少必需价格"
    assert data_v20.DataInputError.reason == labels_r2.LabelInputError.reason == "输入校验失败"
    assert labels_r2.UnexpectedLabelError.reason == "未预期异常"
    # 产品模块之间没有因此新增导入。
    assert "market_risk.wavewarn_v20.labels_r2" not in imported_names(module_file("data_v20"))
    assert not [name for name in imported_names(module_file("labels_r2")) if name.startswith("market_risk")]


def test_definition_header_and_body_on_the_same_physical_line() -> None:
    """补修（同一行定义的作用域归属）：
    - 同一行的定义头命中（默认值）：AST 归外层；
    - 同一行的函数体命中：AST 归该函数；
    - 文本检查无法唯一定位，两者都报告“归属无法唯一判断”，且不能据此命中例外而放行。"""
    source = ("def f(a=open('x')): return 1\n"           # 1 定义头命中
              "def g(): return open('y')\n"              # 2 函数体命中
              "def h(\n"
              "    k=open('z')): return 2\n"             # 4 多行签名的最后一行与函数体同行：定义头命中
              "def ok(\n"
              "    k=1,\n"
              "):\n"
              "    return open('w')\n")                  # 8 函数体单独成行：可唯一定位
    rules = Rules((), ("open",), False, ())
    assert [(hit.line, hit.function) for hit in ast_hits("s.py", source, rules)] == [
        (1, "<module>"), (2, "g"), (4, "<module>"), (8, "ok")]
    text = [(line, scope) for _, scope, _, line, _ in text_hits("s.py", source, ("open(",))]
    assert text == [(1, UNDECIDABLE), (2, UNDECIDABLE), (4, UNDECIDABLE), (8, "ok")]
    # 无法唯一判断的命中不等于任何登记的作用域：即使按函数名登记了例外，也不放行。
    # 能唯一定位的第 8 行按作用域登记后可以匹配，作为对照。
    registered = {("s.py", "g", "def g(): return open('y')"), ("s.py", "<module>", "def f(a=open('x')): return 1"),
                  ("s.py", "ok", "return open('w')")}
    unmatched = [item for item in text_hits("s.py", source, ("open(",)) if item[:3] not in registered]
    assert [item[3] for item in unmatched] == [1, 2, 4]


def test_ast_and_text_scopes_follow_the_same_rule() -> None:
    """作用域归属（补充裁决第一部分第 7 条）：owners（AST）与 line_scopes（文本）都按“定义发生的外层作用域”口径，
    对模块级、装饰器、参数默认值、类体、嵌套函数给出相同结果。"""
    ast_result = [(hit.line, hit.function) for hit in ast_hits("s.py", SCOPE_SOURCE, Rules((), ("open",), False, ()))]
    text_result = [(line, scope) for _, scope, _, line, _ in text_hits("s.py", SCOPE_SOURCE, ("open(",))]
    assert ast_result == text_result == SCOPE_EXPECTED


def test_statement_source_takes_whole_simple_statements_but_only_header_expressions() -> None:
    """完整源码片段（补充裁决第一部分第 1 条）：简单语句取整条（多行参数全部保留）；
    复合语句头部的命中只取包含它的最外层表达式，不放宽到整个函数或语句块。"""
    source = ("def r():\n"
              "    return run_it(open('x'),\n"
              "                  flag=True)\n"
              "def w(p):\n"
              "    with p.open('rb') as f:\n"
              "        return f.read()\n"
              "def k():\n"
              "    call(path='data/market/x')\n")
    rules = Rules((), ("open",), False, ("data/market",))
    texts = [(hit.function, hit.item, hit.text) for hit in ast_hits("s.py", source, rules)]
    assert texts == [("r", "open", "return run_it(open('x'),\n                  flag=True)"),
                     ("w", "open", "p.open('rb')"),
                     ("k", "data/market", "call(path='data/market/x')")]
    decorated = [hit.text for hit in ast_hits("s.py", SCOPE_SOURCE, Rules((), ("open",), False, ()))]
    assert decorated[1:3] == ["deco(open('d1'))", "open('a1')"]                        # 装饰器与默认值
    assert decorated[7] == "Base(open('base'))" and decorated[8] == "x = open('c')"


def test_exception_must_match_the_whole_multi_line_statement() -> None:
    """只登记多行语句的首行、或改动任一续行参数，都不再匹配：检查失败（多行参数全部参与比较）。"""
    source = "def r():\n    return run_it(open('x'),\n                  flag=True)\n"
    rules = Rules((), ("open",), False, ())
    hits = ast_hits("s.py", source, rules)
    whole = Allowed("s.py", "r", "return run_it(open('x'),\n                  flag=True)", "open", "构造", "构造", 1, 1)
    assert exception_problems(hits, frozenset({whole})) == []
    first_line = dataclasses.replace(whole, text="return run_it(open('x'),")
    assert any("未登记的命中" in problem for problem in exception_problems(hits, frozenset({first_line})))
    changed = ast_hits("s.py", source.replace("flag=True", "flag=False"), rules)
    assert any("未登记的命中" in problem for problem in exception_problems(changed, frozenset({whole})))
    # 同一函数中另有一条相同的语句：函数内次数不符。
    doubled = ast_hits("s.py", source + "    return run_it(open('x'),\n                  flag=True)\n", rules)
    assert any("命中次数不符" in problem for problem in exception_problems(doubled, frozenset({whole})))


def test_checker_ignores_comments_strings_and_event_names_but_catches_paths() -> None:
    source = (
        "# subprocess.run(['x'])  open('a')\n"
        "EVENT = 'subprocess.Popen'\n"
        "SCRIPT = 'import subprocess; subprocess.run([1]); open(\"f\").read()'\n"
        "PATH = 'data/market/daily/SPX.csv'\n"
        "def f(path):\n"
        "    return path.exists() and path.write_text('x') and open(path)\n"
        "import http.client\n"
    )
    hits = ast_hits("constructed.py", source, TEST_RULES)
    # 同一行内的命中按写法名排序。
    assert [(hit.function, hit.item) for hit in hits] == [("<module>", "data/market"), ("f", "exists"),
                                                         ("f", "open"), ("f", "write_text"), ("<module>", "http")]


def test_registered_first_days_match_the_dataset_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """一致性测试（补充第 1 条；定稿第〇节“新增 v2.0 测试不读取真实项目配置”的具名例外）：
    只用一处 _read_yaml 读取项目根下的 config/wavewarn_v20.yaml，只比较登记首日；不读取 data_decisions.yaml，
    不调用 load_v20_config。相等只证明一致，不证明正确；正确性以补充裁决第二节第 9 条所列的权威依据为准。"""
    from market_risk import config as project_config
    from market_risk.wavewarn_v20 import provenance_v20

    target = ROOT / "config" / "wavewarn_v20.yaml"
    opened: list[Path] = []
    real_open = Path.open

    def recording_open(self: Path, *args, **kwargs):
        opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    raw = project_config._read_yaml(target)
    monkeypatch.undo()
    assert opened == [target]                                                      # 只打开这一个路径一次
    first_days = {asset: raw["dataset"][asset]["first_date"] for asset in ("SPX", "QQQ")}
    assert first_days == dict(provenance_v20.REGISTERED_FIRST_DAY)


def test_source_lists_agree_without_reading_files() -> None:
    """来源清单的一致性比较：不读文件，按集合比较。相等只证明一致，不证明正确。"""
    from market_risk.wavewarn_v20 import data_v20, provenance_v20

    assert provenance_v20.SOURCES == data_v20.ALLOWED_SOURCES == frozenset({"yahoo", "correct:yahoo"})


def test_the_config_exception_is_the_only_read_yaml_call_in_this_file() -> None:
    """本文件中的 _read_yaml 调用恰为一致性测试中的一处（配置例外的登记范围）。"""
    tree = ast.parse(source_of(Path(__file__)))
    scope = owners(tree)
    calls = [scope.get(id(node)) for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr in ("_read_yaml", "load_data_decisions",
                                                                              "load_v20_config")]
    assert calls == ["test_registered_first_days_match_the_dataset_config"]
    # 第二处具名读取例外：pyproject.toml 只在插件加载检查中出现一次。
    mentions = [scope.get(id(node)) for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and node.value == "pyproject" + ".toml"]   # 拼接，避免计入本句
    assert mentions == ["test_guard_plugin_is_loaded_only_explicitly"]
