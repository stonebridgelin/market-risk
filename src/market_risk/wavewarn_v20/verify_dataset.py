"""真实文件元数据核对的入口（阶段三实施指令第七节）。须经负责人另行放行，只运行一次。

普通导入没有任何副作用：模块顶层只有本说明、标准库导入与函数定义；不安装钩子，不导入行情读取、配置读取与路径模块，不读写文件。
只有作为入口执行时，main() 的第一步才安装记录钩子；安装之后才在函数内导入业务模块并执行核对。

核对内容：对两份行情文件各以二进制只读方式打开一次，计算文件级元数据并与登记值逐项比较。不解析来源与价格。

记录的覆盖范围：记录的是自钩子安装时点起，本进程经 Python 接口的文件打开。
安装之前发生的解释器启动、runpy 执行、market_risk 与 wavewarn_v20 包初始化、本模块自身及其标准库导入，都不在范围内；
uv 进程自身与原生代码的直接读取也不在范围内。本命令不启动子进程。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def coverage_note() -> str:
    """打开记录的覆盖范围说明（随输出一并给出；文字为阶段三实施指令第七节第 3 部分的汇报原文）。"""
    return ("记录的是自钩子安装时点起，本进程经 Python 接口的文件打开。安装之前发生的解释器启动、runpy 执行、"
            "market_risk 与 wavewarn_v20 包初始化、verify_dataset 模块自身及其标准库导入，都不在范围内；"
            "uv 进程自身与原生代码的直接读取也不在范围内。本命令不启动子进程。")


def default_root() -> Path:
    """仓库根目录：本文件往上四级。"""
    return Path(__file__).resolve().parents[3]


def normalized(path: object) -> str:
    """路径统一为绝对、解析链接后、大小写规范的写法，只用于比较。"""
    return os.path.normcase(os.path.realpath(os.fspath(path)))    # type: ignore[call-overload]


def forbidden_flag_names() -> tuple[str, ...]:
    """已登记的禁止标志（补充裁决第二节第 1 条）。"""
    return ("O_CREAT", "O_TRUNC", "O_APPEND", "O_EXCL")


def skipped_flag_names() -> list[str]:
    """运行平台上没有的禁止标志：检查时跳过，并在输出中列明。"""
    return [name for name in forbidden_flag_names() if not hasattr(os, name)]


def read_only_conclusion(mode: str, flags: object) -> str:
    """按审计事件给出的模式与标志位，给出“只读”“不是只读”“无法确认只读”三种结论之一（补充裁决第二节第 1 条）。

    - flags 缺失、类型非法或为布尔值：“无法确认只读”（不表述为已发生写入；补充裁决第一部分第 8 条）；
    - mode 必须为 "r"（不含 w、a、x、+）；
    - flags & os.O_WRONLY、flags & os.O_RDWR 都为 0（不以 flags & os.O_RDONLY 判断，它可能为 0）；
    - flags 不含已登记的禁止标志；取运行平台上的符号常量，平台没有的跳过（见 skipped_flag_names）。
    审计事件里的模式来自底层的原始文件对象：以 "rb" 与 "r" 打开时都是 "r"，所以这里只能确认“只读”，
    确认不了“二进制”。二进制打开由行情读取模块源码的静态检查确认。
    """
    if not isinstance(flags, int) or isinstance(flags, bool):
        return "无法确认只读"
    if mode != "r" or flags & os.O_WRONLY or flags & os.O_RDWR:
        return "不是只读"
    if any(flags & getattr(os, name) for name in forbidden_flag_names() if hasattr(os, name)):
        return "不是只读"
    return "只读"


def read_only(mode: str, flags: object) -> bool:
    """只有结论为“只读”时为真；“无法确认只读”与“不是只读”都为假。"""
    return read_only_conclusion(mode, flags) == "只读"


def classify_opens(records: list[tuple[str, str, object]], root: Path, market_files: list[Path],
                   config_files: list[Path]) -> dict:
    """把打开记录分成三类：受保护的行情目录、配置目录、其他；并给出不符合要求之处。

    要求：行情目录下只出现两份行情文件，各一次，且都是只读打开；配置目录下只出现登记的两份配置文件。
    """
    market_root = normalized(root.joinpath("data", "market")) + os.sep
    config_root = normalized(root.joinpath("config")) + os.sep
    market = [item for item in records if normalized(item[0]).startswith(market_root)]
    config = [item for item in records if normalized(item[0]).startswith(config_root)]
    problems: list[str] = []
    expected_market = sorted(normalized(path) for path in market_files)
    if sorted(normalized(path) for path, _, _ in market) != expected_market:
        problems.append("行情目录下的打开记录不是恰好两份行情文件各一次")
    conclusions = {read_only_conclusion(mode, flags) for _, mode, flags in market}
    if "不是只读" in conclusions:
        problems.append("行情文件不是以只读方式打开")
    if "无法确认只读" in conclusions:
        problems.append("无法确认行情文件以只读方式打开（标志位缺失或类型非法）")
    if sorted({normalized(path) for path, _, _ in config}) != sorted(normalized(path) for path in config_files):
        problems.append("配置目录下的打开记录不是恰好登记的两份配置文件")
    return {"market": market, "config": config, "problems": problems}


def comparison(asset: str, path: Path, metadata: object, registered: object, differences: tuple[str, ...]) -> dict:
    """一份行情文件的各项值与逐项比较结果。"""
    fields = ("raw_sha256", "normalized_sha256", "data_rows", "first_date", "last_date")
    return {"asset": asset, "path": str(path),
            "items": [{"name": name, "file": str(getattr(metadata, name)), "registered": str(getattr(registered, name)),
                       "equal": getattr(metadata, name) == getattr(registered, name)} for name in fields],
            "total_rows": metadata.total_rows,    # type: ignore[attr-defined]
            "differences": list(differences)}


def main(argv: list[str] | None = None) -> None:
    """入口：先安装记录钩子，再导入业务模块并核对。全部相符且打开记录符合要求时退出码为 0，否则为 1。"""
    records: list[tuple[str, str, object]] = []

    def record_opens(event: str, arguments: tuple) -> None:
        if event == "open" and arguments and isinstance(arguments[0], str | bytes | os.PathLike):
            mode = str(arguments[1]) if len(arguments) > 1 else ""
            flags = arguments[2] if len(arguments) > 2 else None              # 缺失或不是整数时判为非只读
            records.append((os.fsdecode(os.fspath(arguments[0])), mode, flags))

    sys.addaudithook(record_opens)
    from market_risk.storage.paths import StoragePaths
    from market_risk.wavewarn_v20 import config_v20, data_v20

    parser = argparse.ArgumentParser(description="核对两份行情文件的文件级元数据与登记值")
    parser.add_argument("--root", type=Path, default=default_root())
    root = parser.parse_args(argv).root
    config = config_v20.load_v20_config(root)
    paths = StoragePaths(root)
    results = []
    market_files = []
    for asset in config_v20.ASSETS:
        path = paths.market_daily_file(asset)
        market_files.append(path)
        metadata = data_v20.file_metadata(data_v20.read_file_bytes(path))
        differences = data_v20.registered_differences(metadata, config.registered[asset])
        results.append(comparison(asset, path, metadata, config.registered[asset], differences))
    snapshot = list(records)
    opens = classify_opens(snapshot, root, market_files,
                           [root.joinpath(*config_v20.DATASET_FILE), root.joinpath(*config_v20.DECISIONS_FILE)])
    passed = all(not item["differences"] for item in results) and not opens["problems"]
    report = {"root": str(root), "files": results, "open_check": opens,
              "open_records": [{"path": path, "mode": mode, "flags": flags, "read_only": read_only(mode, flags),
                                "conclusion": read_only_conclusion(mode, flags)} for path, mode, flags in snapshot],
              "skipped_flags": skipped_flag_names(),
              "coverage": coverage_note(),
              "passed": passed}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
