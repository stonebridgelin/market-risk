"""波段预警的代码与文件清单：静态分析（只解析源码的 import 语句与 git 历史），不导入项目代码，不读取任何数据。

用法（在主仓库根目录）：
    python docs/audit/inventory/module_inventory.py docs/research/代码与文件清单.md
只列出，不删除、不移动任何文件。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

GIT = "C:/Execute/Git/bin/git.exe"
PACKAGE = "src/market_risk"
WAVEWARN = "market_risk.wavewarn"
# 首次提交 → 所属版本（提交号取自 git 历史；同一版本的后续修改另列“最后修改”）
VERSION_OF_COMMIT = {
    **dict.fromkeys(("ad3da07", "dd3b540", "6043950", "08be786", "0dda226", "77c89aa", "beecbcc"), "v1.2.1"),
    **dict.fromkeys(("50d77f7", "0df0505", "8ef590c"), "v1.3"),
    **dict.fromkeys(("5bc227f",), "v1.4（开发期评价）"),
    **dict.fromkeys(("e8bbc18", "514836c", "09afca0"), "v1.4（验证期工程）"),
    **dict.fromkeys(("db3d208", "e85f63d", "bfe78a0", "a0a8ec0"), "v1.4（检验口径与诊断补充）"),
}
RECOMPUTE = ("evaluate-v14-development", "v14-extended-history", "v14-diagnostics-round2", "v14-extended-nav")


def git(*args: str) -> str:
    done = subprocess.run([GIT, *args], capture_output=True, text=True, encoding="utf-8", check=True)
    return done.stdout.strip()


def module_name(path: Path, package: Path) -> str:
    parts = path.relative_to(package).with_suffix("").parts
    return ".".join(("market_risk", *parts)).removesuffix(".__init__")


def imported(tree: ast.AST, known: set[str]) -> set[str]:
    """一段语法树里导入的项目模块（含函数体内的延迟导入）。"""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found & known


def closure(start: set[str], graph: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(start)
    while stack:
        name = stack.pop()
        if name not in seen:
            seen.add(name)
            stack.extend(graph.get(name, ()))
    return seen


def history(path: Path) -> tuple[str, str]:
    """（首次提交，最后修改提交）的短哈希。"""
    added = git("log", "--diff-filter=A", "--format=%h", "--", path.as_posix()).splitlines()
    return (added[-1] if added else "未入库"), git("log", "-1", "--format=%h", "--", path.as_posix())


def commands(root: Path, known: set[str], graph: dict[str, set[str]]) -> dict[str, tuple[str, set[str]]]:
    """CLI 命令 → （services 函数名，经它可达的全部项目模块）。命令函数与 services 函数同名。"""
    cli = ast.parse((root / PACKAGE / "cli.py").read_text(encoding="utf-8"))
    services = {node.name: node for node in ast.parse(
        (root / PACKAGE / "services.py").read_text(encoding="utf-8")).body if isinstance(node, ast.FunctionDef)}
    result: dict[str, tuple[str, set[str]]] = {}
    for node in cli.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else None
            if (isinstance(target, ast.Attribute) and target.attr == "command"
                    and isinstance(target.value, ast.Name) and target.value.id == "wavewarn_app"):
                if node.name not in services:
                    raise ValueError(f"命令函数 {node.name} 没有同名的 services 函数")
                name = decorator.args[0].value  # type: ignore[union-attr]
                result[f"wavewarn {name}"] = (node.name, closure(imported(services[node.name], known), graph))
    return result


def test_version(name: str) -> str:
    return "v1.4" if "_v14" in name else "v1.3" if "_v13" in name else "v1.2.1"


def table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
            *("| " + " | ".join(row) + " |" for row in rows), ""]


def joined(items: list[str]) -> str:
    return "、".join(f"`{item}`" for item in items) if items else "无"


def main() -> None:
    root, target = Path.cwd(), Path(sys.argv[1])
    package = root / PACKAGE
    modules = {module_name(path, package): path for path in sorted(package.rglob("*.py"))}
    known = set(modules)
    graph = {name: imported(ast.parse(path.read_text(encoding="utf-8")), known) for name, path in modules.items()}
    wavewarn = [name for name in modules if name == WAVEWARN or name.startswith(WAVEWARN + ".")]
    tests = {path.name: imported(ast.parse(path.read_text(encoding="utf-8")), known)
             for path in sorted((root / "tests").glob("test_*.py"))}
    tools = {path.relative_to(root).as_posix(): imported(ast.parse(path.read_text(encoding="utf-8")), known)
             for path in sorted((root / "docs/audit").rglob("*.py"))}
    cli = commands(root, known, graph)
    short = lambda name: "__init__" if name == WAVEWARN else name.removeprefix(WAVEWARN + ".")  # noqa: E731

    module_rows = []
    for name in wavewarn:
        path = modules[name].relative_to(root)
        first, last = history(path)
        direct_tests = [test.removeprefix("test_").removesuffix(".py") for test, used in tests.items() if name in used]
        reached = [command.removeprefix("wavewarn ") for command, (_, used) in cli.items() if name in used]
        recompute = [command for command in reached if command in RECOMPUTE]
        used_by_tools = [Path(tool).name for tool, used in tools.items() if name in used]
        importers = [short(other) for other in wavewarn if name in graph[other]]
        if name == WAVEWARN:
            status = "包的初始化文件"
        elif recompute or used_by_tools:
            status = "被复算命令或复算工具调用"
        elif reached:
            status = "只被其他命令调用"
        elif direct_tests or importers:
            status = "只被测试或其他模块引用"
        else:
            status = "没有任何引用"
        module_rows.append((f"`{short(name)}`", VERSION_OF_COMMIT.get(first, f"未归类（{first}）"), f"`{first}`",
                            f"`{last}`", joined(direct_tests), joined(reached), joined(used_by_tools), status))

    command_rows = [(f"`{command}`", f"`services.{service}`",
                     "是" if command.removeprefix("wavewarn ") in RECOMPUTE else "否",
                     str(sum(name.startswith(WAVEWARN + ".") for name in used)))
                    for command, (service, used) in cli.items()]
    wavewarn_tests = {test: used for test, used in tests.items()
                      if "wavewarn" in test or any(name.startswith(WAVEWARN) for name in used)}
    test_rows = [(f"`tests/{test}`", test_version(test), str(sum(name.startswith(WAVEWARN + ".") for name in used)))
                 for test, used in wavewarn_tests.items()]
    reachable = closure(set(wavewarn), graph)
    direct_shared = sorted({name for module in wavewarn for name in graph[module] if not name.startswith(WAVEWARN)})
    shared_rows = [(f"`{name}`", f"`{modules[name].relative_to(root).as_posix()}`",
                    "波段预警模块直接导入" if name in direct_shared else "经共享依赖间接导入")
                   for name in sorted(reachable) if not name.startswith(WAVEWARN)]
    entry_rows = [("`market_risk.services`", "`src/market_risk/services.py`",
                   "入口：`wavewarn_*` 函数（延迟导入波段预警模块）"),
                  ("`market_risk.cli`", "`src/market_risk/cli.py`",
                   "入口：`wavewarn` 命令组（只解析参数、调用 services）")]
    config_rows = [(f"`{path.relative_to(root).as_posix()}`", test_version("_" + path.stem.split("_", 1)[1]))
                   for path in sorted((root / "config").glob("wavewarn*.yaml"))]
    tool_rows = [(f"`{path.relative_to(root).as_posix()}`",
                  joined(sorted(short(name) for name in tools.get(path.relative_to(root).as_posix(), ())
                                if name.startswith(WAVEWARN + "."))))
                 for path in sorted((root / "docs/audit").rglob("*")) if path.suffix in (".py", ".ps1")]
    unreferenced = [row[0] for row in module_rows if row[-1] == "没有任何引用"]

    lines = [
        "# 代码与文件清单（波段预警）", "",
        "只列出，不删除、不移动。由 `docs/audit/inventory/module_inventory.py` 静态分析生成"
        "（解析源码的 import 语句与 git 历史，不导入项目代码、不读取任何数据）；"
        f"生成时的提交为 `{git('rev-parse', '--short', 'HEAD')}`。", "",
        "范围：`src/market_risk/wavewarn/` 的全部模块、对应测试、`config/wavewarn*.yaml`、`docs/audit/` 下的工具、"
        "`wavewarn` 命令组；波段预警实际调用的共享依赖另列并标“共享依赖”。不列正式风险评分系统的内部模块，"
        "也不列 `src/market_risk/research/`（原口径的小周期研究，不属于波段预警）。", "",
        "口径：", "",
        "- “所属版本”按模块首次提交所在的批次归类；之后被别的版本改过的，看“最后修改”一列。",
        "- “直接导入它的测试”只算测试文件里直接 import 的；“可到达它的命令”按 services 函数里的导入逐层展开。",
        f"- “复算命令”指阶段二复算用的四条：{joined(list(RECOMPUTE))}；“复算工具”指 `docs/audit/` 下的脚本。",
        "- 引用关系是静态的：能到达不等于每次运行都会执行到。", "",
        f"## 一、模块（{len(module_rows)} 个）", "",
        *table(("模块", "所属版本", "首次提交", "最后修改", "直接导入它的测试", "可到达它的命令", "导入它的复算工具",
                "状态"), module_rows),
        f"没有任何引用的模块：{'、'.join(unreferenced) if unreferenced else '无'}。", "",
        f"## 二、命令（{len(command_rows)} 条）", "",
        *table(("命令", "入口函数", "是否阶段二的复算命令", "可到达的波段预警模块数"), command_rows),
        f"## 三、测试（{len(test_rows)} 个文件）", "",
        *table(("测试文件", "对应版本", "直接导入的波段预警模块数"), test_rows),
        "## 四、配置", "", *table(("文件", "版本"), config_rows),
        "## 五、`docs/audit/` 下的工具", "", *table(("文件", "导入的波段预警模块"), tool_rows),
        "`docs/audit/独立复核/` 是正式风险评分的独立复核工具，不属于波段预警，只因在同一目录下而列出。"
        "归因版本的重建脚本在 `reports/research/wavewarn_v14/correction_rerun/recovery/rebuild.ps1`。", "",
        "## 六、共享依赖", "", *table(("模块", "文件", "关系"), [*shared_rows, *entry_rows]),
        "数据文件方面的共享依赖（市场数据集、TradingView 清洗结果、Cboe VIX3M 副本、事件标签文件）见 "
        "`reports/research/wavewarn_v14/correction_rerun/recovery/inputs_manifest.json`。", "",
    ]
    target.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"模块 {len(module_rows)}，命令 {len(command_rows)}，测试 {len(test_rows)}，没有任何引用 {len(unreferenced)}")


if __name__ == "__main__":
    main()
