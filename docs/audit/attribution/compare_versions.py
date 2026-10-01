"""单项与合并归因、f_label 不变性验收：只比较四个版本已经写出的文件，不导入项目代码，不重新计算任何模型结果。

用法（在主仓库根目录）：
    python docs/audit/attribution/compare_versions.py <工作目录的上级目录> reports/research/wavewarn_v14/attribution
四个版本的工作目录名固定为 B、B_fBW、B_flabel、B_fBW_flabel（由 build_version.ps1 构造并运行）。
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

VERSIONS = ("B", "B_fBW", "B_flabel", "B_fBW_flabel")
OUTPUT_ROOT = "reports/research/wavewarn_v14"
OUTPUT_DIRS = ("evaluation_development", "diagnostics_round2", "extended_nav")
FIXED = "fixed_setting.json"
GIT = "C:/Execute/Git/bin/git.exe"
BASE_TAG = "v1.4-asrun"
NUMERIC = (("主损失 L", ("total_loss",)), ("危险项", ("components", "danger_loss")),
           ("回撤项", ("components", "drawdown_loss")), ("机会项", ("components", "opportunity_loss")),
           ("切换项", ("components", "switch_cost")), ("漏报罚项", ("components", "full_exposure_cost")),
           ("T", ("timing_score",)), ("T价格", ("price_score",)), ("ē", ("mean_exposure",)),
           ("非绿占比", ("non_green_share",)), ("计费切换次数", ("billed_switches",)),
           ("目标暴露变化量", ("exposure_change",)),
           ("SPX 转绿延迟中位数（类别②）", ("green_delay", "SPX", "median_class_2")),
           ("QQQ 转绿延迟中位数（类别②）", ("green_delay", "QQQ", "median_class_2")),
           ("SPX 类别①件数", ("green_delay", "SPX", "class_1")), ("SPX 类别②件数", ("green_delay", "SPX", "class_2")),
           ("SPX 类别③件数", ("green_delay", "SPX", "class_3")), ("QQQ 类别①件数", ("green_delay", "QQQ", "class_1")),
           ("QQQ 类别②件数", ("green_delay", "QQQ", "class_2")), ("QQQ 类别③件数", ("green_delay", "QQQ", "class_3")),
           ("净值累计收益（双资产）", ("nav_portfolio", "cumulative")),
           ("净值年化收益（双资产）", ("nav_portfolio", "annualized")),
           ("全程最大回撤（双资产）", ("nav_portfolio", "max_drawdown")),
           ("最大回撤峰值到谷底（交易日）", ("nav_portfolio", "decline_days")),
           ("最大回撤谷底到窗口末日（交易日）", ("nav_portfolio", "days_after_trough")))
TEXTUAL = (("最大回撤峰值日", ("nav_portfolio", "peak_date")), ("最大回撤谷底日", ("nav_portfolio", "trough_date")),
           ("最大回撤恢复日", ("nav_portfolio", "recovery_date")),
           ("最大回撤谷底到恢复（交易日）", ("nav_portfolio", "recovery_days")))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def manifest(worktree: Path) -> dict[str, str]:
    """一个版本写出的全部文件（四条命令的输出目录与固定设定汇总）→ SHA-256。"""
    files = {FIXED: sha256(worktree / FIXED)}
    for name in OUTPUT_DIRS:
        base = worktree / OUTPUT_ROOT / name
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            files[path.relative_to(worktree).as_posix()] = sha256(path)
    return files


def differences(first: dict[str, str], second: dict[str, str]) -> list[str]:
    """文件集合或内容的全部差异；空表即完全一致。白名单对文件内容没有任何排除项。"""
    result = [f"只在前者：{name}" for name in sorted(set(first) - set(second))]
    result += [f"只在后者：{name}" for name in sorted(set(second) - set(first))]
    result += [f"内容不同：{name}" for name in sorted(set(first) & set(second)) if first[name] != second[name]]
    return result


def lookup(data: dict, path: tuple[str, ...]) -> object:
    value: object = data
    for key in path:
        value = value[key]  # type: ignore[index]
    return value


def decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def attribution_rows(fixed: dict[str, dict]) -> list[tuple[object, ...]]:
    """各版本的取值、单项影响、合并影响与非加和影响（合并影响减去各项单独影响之和）。"""
    rows: list[tuple[object, ...]] = []
    for label, path in NUMERIC:
        values = [decimal(lookup(fixed[name], path)) for name in VERSIONS]
        if any(value is None for value in values):
            rows.append((label, *(v if v is not None else "" for v in values), "", "", "", ""))
            continue
        base, bw, lab, both = values  # type: ignore[misc]
        effect_bw, effect_label, merged = bw - base, lab - base, both - base
        rows.append((label, base, bw, lab, both, effect_bw, effect_label, merged, merged - effect_bw - effect_label))
    for label, path in TEXTUAL:
        values = [lookup(fixed[name], path) for name in VERSIONS]
        rows.append((label, *("" if v is None else v for v in values), "", "", "", ""))
    lights = [fixed[name]["executed_lights"] for name in VERSIONS]
    counts = [sum(a != b for a, b in zip(lights[0], item, strict=True)) for item in lights]
    rows.append(("执行灯色与 B 不同的区间数", *counts, counts[1], counts[2], counts[3],
                 counts[3] - counts[1] - counts[2]))
    return rows


HEADER = ("item", *VERSIONS, "effect_f_BW", "effect_f_label", "effect_merged", "non_additive")


def tagged(main: Path, name: str) -> bytes | None:
    shown = subprocess.run([GIT, "-C", str(main), "show", f"{BASE_TAG}:{name}"], capture_output=True, check=False)
    return shown.stdout if shown.returncode == 0 else None


APPENDED = "本行在压缩后追加".encode()


def normalised(data: bytes) -> bytes:
    """换行统一为 LF；去掉入库 README 里压缩逐日明细之后手工追加的那一行（行内自带“本行在压缩后追加”）。"""
    lines = data.replace(b"\r\n", b"\n").split(b"\n")
    return b"\n".join(line for line in lines if APPENDED not in line)


def asrun_differences(worktree: Path, main: Path) -> list[str]:
    """版本 B 重新写出的开发期评价，与标签里已入库的原始输出逐文件比较（同名文件）。

    入库的文本文件按仓库设置存为 LF 换行，命令写出的 CSV 是 CRLF，所以先把换行统一再比较；
    逐日明细入库的是 gzip 压缩件，解压后与重新写出的文件逐字节比较。
    """
    result = []
    base = worktree / OUTPUT_ROOT / "evaluation_development"
    for path in sorted(item for item in base.iterdir() if item.is_file()):
        name = f"{OUTPUT_ROOT}/evaluation_development/{path.name}"
        written = path.read_bytes()
        if path.name == "daily_selected.csv":
            packed = tagged(main, name + ".gz")
            same = packed is not None and gzip.decompress(packed) == written
        else:
            stored = tagged(main, name)
            same = stored is not None and normalised(stored) == normalised(written)
        if not same:
            result.append(f"不同或标签中没有：{path.name}")
    return result


def short(value: object) -> str:
    if isinstance(value, Decimal):
        return f"{value:.6f}" if value != value.to_integral_value() else f"{value:f}".split(".")[0]
    return str(value) if value != "" else "—"


def report(rows: list[tuple[object, ...]], versions: dict[str, dict], checks: dict[str, list[str]]) -> list[str]:
    lines = ["# 单项与合并归因（开发期，固定设定 K=5、θ_P=2.5%，不重选）", "",
             "本报告仅使用开发期数据。四个版本都从标签 `v1.4-asrun`（B）出发，在各自独立的工作目录里只打对应的补丁，"
             "不使用主仓库连续提交后的累计状态。结果只作纠错证据，不自动恢复候选资格；v1.4 继续暂停。", "",
             "## 版本", "",
             "| 版本 | 补丁（SHA-256） | 代码树哈希 | 相对 B 改动的文件 |", "|---|---|---|---|"]
    for name in VERSIONS:
        meta = versions[name]
        patches = "；".join(f"`{item['file']}`（`{item['sha256']}`）" for item in meta["patches"]) or "无"
        changed = "、".join(f"`{item}`" for item in meta["changed_files"]) or "无"
        lines.append(f"| {name} | {patches} | `{meta['code_tree']}` | {changed} |")
    ok = "一致" if not checks["invariance"] else "不一致"
    lines += ["", f"B 的提交为 `{versions['B']['base_commit']}`。各版本共用同一份未入库的输入 "
              f"`data/processed/tradingview/NDTW.csv`（SHA-256 `{versions['B']['ndtw_sha256']}`），"
              "其余输入都在标签的代码树里。", "",
              "## f_label 的不变性验收", "",
              "白名单见 `invariance_whitelist.md`（写于运行之前；文件内容没有任何白名单项）。比对 B + f_label 与 B "
              f"写出的全部文件（文件集合与逐文件 SHA-256）：**{ok}**，共 {checks['count'][0]} 个文件。",
              *(f"- {item}" for item in checks["invariance"]), "",
              f"附带核对：B + f_BW + f_label 与 B + f_BW 写出的全部文件：{'一致' if not checks['both'] else '不一致'}"
              "（f_label 在 f_BW 之上同样不改变任何结果）。",
              "B 重新写出的开发期评价与标签里已入库的原始输出（同名文件；换行统一后比较，"
              "逐日明细与入库压缩件解压后比较，入库 README 里压缩之后手工追加的一行哈希说明不计）："
              f"{'完全相同' if not checks['asrun'] else '有差异'}。",
              *(f"- {item}" for item in checks["asrun"]), "",
              "## 各项指标", "",
              "单项影响 = 该版本 − B；合并影响 = B + f_BW + f_label − B；非加和影响 = 合并影响 − 各项单独影响之和。"
              "非加和影响只说明效果不能简单相加，不识别两两交互，不是因果证明。", "",
              "| 指标 | B | B + f_BW | B + f_label | B + f_BW + f_label | f_BW 的影响 | f_label 的影响 | 合并影响 "
              "| 非加和影响 |",
              "|---|---|---|---|---|---|---|---|---|"]
    lines += ["| " + " | ".join(short(cell) for cell in row) + " |" for row in rows]
    lines += ["", "T 为安全代理择时得分（越小越好），T价格 = T − 切换项；净值不含分红、现金收益为 0、未计费用。"
              "完整精度见 `attribution.csv` 与各版本的 `fixed_setting.json`。", ""]
    return lines


def main() -> None:
    worktrees, target = Path(sys.argv[1]), Path(sys.argv[2])
    main_root = Path.cwd()
    manifests = {name: manifest(worktrees / name) for name in VERSIONS}
    fixed = {name: json.loads((worktrees / name / FIXED).read_text(encoding="utf-8")) for name in VERSIONS}
    versions = {name: json.loads((worktrees / name / "version.json").read_text(encoding="utf-8-sig"))
                for name in VERSIONS}
    checks = {"invariance": differences(manifests["B"], manifests["B_flabel"]),
              "both": differences(manifests["B_fBW"], manifests["B_fBW_flabel"]),
              "asrun": asrun_differences(worktrees / "B", main_root), "count": [len(manifests["B"])]}
    for name in VERSIONS:
        folder = target / "versions" / name
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(worktrees / name / FIXED, folder / FIXED)
        (folder / "version.json").write_text(json.dumps(versions[name], ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
        (folder / "output_manifest.json").write_text(json.dumps(manifests[name], ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8")
    rows = attribution_rows(fixed)
    with (target / "attribution.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(HEADER)
        writer.writerows(rows)
    result = {"B_flabel_vs_B": {"files": checks["count"][0], "differences": checks["invariance"]},
              "B_fBW_flabel_vs_B_fBW": {"differences": checks["both"]},
              "B_vs_tag_asrun_evaluation": {"differences": checks["asrun"]}}
    (target / "invariance_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
    (target / "归因报告.md").write_text("\n".join(report(rows, versions, checks)), encoding="utf-8")
    print("f_label 不变性：", "一致" if not checks["invariance"] else f"不一致（{len(checks['invariance'])} 处）")


if __name__ == "__main__":
    main()
