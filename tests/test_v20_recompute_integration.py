"""两层独立回算脚本与项目的联检（N4b；阶段四 M2 第二部分指令修订六第三节、第六节第 1 小节末行）。

- 输入：既有正常成功演习（tests/test_wavewarn_v20_development_run.py 的 normal 夹具，按 pytest 夹具导入方式复用，
  不改该文件）在 tmp_path 构造根下产生的正式目录 evaluation_development。
- 运行冻结后的 N1（recompute_a.py）、N2（recompute_b.py）：先核对两脚本 SHA-256 等于
  docs/audit/独立回算/v20/冻结清单.txt 所列，再以子进程运行（带 timeout），断言退出码 0 且报告“不一致”计数为 0。
- 本文件只证明“项目与脚本相互一致”，不含人工期望值，不能代替 N4 各行的人工推算。
- 两脚本的命令行写法与报告计数字段按 N3 README 第一节（第三次交付，集成时核对）：`--result-dir <目录> --out <报告>`；
  不一致计数为 summary.mismatches。甲层报告的 summary.reserved（“保留：主参照失败原因未独立核验”计数）照录为
  测试属性，不计为一致，非零时列入实施记录；乙层报告无该字段（照录为 None）。
- 带拦截运行时照实标“通过但覆盖不完整”（子进程中的访问不在外层拦截范围内）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import test_wavewarn_v20_development_run as rehearsal

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "docs" / "audit" / "独立回算" / "v20"
FROZEN_LIST = SCRIPTS / "冻结清单.txt"
normal = rehearsal.normal                          # 夹具按导入方式复用（不改 D41）
LAYERS = {"甲": "recompute_a.py", "乙": "recompute_b.py"}
TIMEOUT = 900                                      # 单个脚本子进程的时限（秒）


def get(path: Path) -> bytes:
    """读脚本源码（只算哈希）、冻结清单与脚本写出的报告。"""
    return path.read_bytes()


def frozen_hashes() -> dict[str, str]:
    """冻结清单.txt：每行“<SHA-256>  <字节数>  <相对路径>”（相对 docs/audit/独立回算/v20/）。"""
    table = {}
    for line in get(FROZEN_LIST).decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 3 and len(parts[0]) == 64:
            table[parts[2]] = parts[0]
    return table


def script_command(script: Path, formal: Path, out: Path) -> list[str]:
    """两脚本的命令行（N3 README 第一节）：--result-dir 为结果目录，--out 为新报告。"""
    return [sys.executable, "-B", str(script), "--result-dir", str(formal), "--out", str(out)]


def run_script(script: Path, formal: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(script_command(script, formal, out), cwd=out.parent, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, timeout=TIMEOUT)


def inconsistent_count(report: dict) -> int:
    """报告中“不一致”的汇总计数（N3 README 第一节：summary.mismatches）。"""
    return report["summary"]["mismatches"]


def reserved_count(report: dict, layer: str) -> int | None:
    """甲层 summary.reserved（须存在）；乙层报告无该字段，返回 None。"""
    return report["summary"]["reserved"] if layer == "甲" else report["summary"].get("reserved")


@pytest.mark.parametrize("layer", list(LAYERS), ids=list(LAYERS))
def test_ab_on_constructed_formal_directory(normal: tuple[Path, subprocess.CompletedProcess, float], layer: str,
                                            tmp_path: Path, record_property) -> None:
    root, done, _ = normal
    assert done.returncode == 0, done.stderr
    formal = rehearsal.research_dir(root) / "evaluation_development"
    script = SCRIPTS / LAYERS[layer]
    actual = hashlib.sha256(get(script)).hexdigest()
    assert actual == frozen_hashes()[LAYERS[layer]], f"{LAYERS[layer]} 与冻结清单不符：{actual}"
    out = tmp_path / f"{layer}层报告.json"
    result = run_script(script, formal, out)
    record_property("正式目录 MANIFEST.sha256", hashlib.sha256(get(formal / "MANIFEST.sha256")).hexdigest())
    record_property("脚本退出码", result.returncode)
    assert out.is_file(), f"{layer}层未写出报告：退出码 {result.returncode}；{result.stderr[-2000:]}"
    report = json.loads(get(out).decode("utf-8"))
    record_property("报告 SHA-256", hashlib.sha256(get(out)).hexdigest())
    record_property("summary", json.dumps(report["summary"], ensure_ascii=False))
    record_property("summary.reserved", reserved_count(report, layer))
    assert (result.returncode, inconsistent_count(report)) == (0, 0), result.stderr[-2000:]
