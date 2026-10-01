"""v1.4 第二轮开发期诊断的读写边界（补充登记 D）：只读开发期输入，只写诊断目录。

输入截至开发期末（2016-12-30），读取函数在其后的第一行之前停止；不读取验证期与保留期。
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.config_v14 import Round2Config, load_round2_config, load_validation_config
from market_risk.wavewarn.diagnostics_round2 import Round2Result, round2_diagnostics
from market_risk.wavewarn.diagnostics_round2_report import REPORT_NAME, csv_tables, nav_daily, report_lines
from market_risk.wavewarn.evaluation_run import file_sha256, write_csv
from market_risk.wavewarn.inputs import load_inputs_until
from market_risk.wavewarn.lock_guard import find_gzip
from market_risk.wavewarn.v14_model import prepare_v14
from market_risk.wavewarn.validation_output import gzip_file
from market_risk.wavewarn.validation_run import CONFIG_FILE

ROUND2_CONFIG = "config/wavewarn_v14_diagnostics.yaml"
NAV_DAILY = "nav_daily.csv"
HASH_FILE = "output_hashes.json"
NAV_PLACES = Decimal("1e-12")


@dataclass(frozen=True)
class Round2Run:
    output: Path
    intervals: int
    first_interval: dt.date
    last_day: dt.date
    report_sha256: str


def write_round2(destination: Path, result: Round2Result, config: Round2Config, gzip_executable: str) -> None:
    """写出全部表格、逐日净值（gzip -n）与报告，最后写各文件的 SHA-256 清单。destination 须已存在。"""
    for name, header, rows in csv_tables(result, config):
        write_csv(destination / name, header, rows)
    header, rows = nav_daily(result, NAV_PLACES)
    write_csv(destination / NAV_DAILY, header, rows)
    packed = gzip_file(destination / NAV_DAILY, gzip_executable)
    (destination / REPORT_NAME).write_text("\n".join(report_lines(result, config)), encoding="utf-8")
    hashes = {path.name: file_sha256(path) for path in sorted(destination.iterdir())}
    hashes[f"{NAV_DAILY}（压缩前，不入库）"] = packed.raw
    (destination / HASH_FILE).write_text(json.dumps(hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_v14_round2(root: Path) -> Round2Run:
    """第二轮开发期诊断；输出目录已存在时拒绝覆盖。全部写齐后才把临时目录改名为输出目录。"""
    validation = load_validation_config(root / CONFIG_FILE)
    config = load_round2_config(root / ROUND2_CONFIG)
    output = root / config.output
    if output.exists():
        raise FileExistsError(f"诊断目录已存在，拒绝覆盖：{output}")
    gzip_executable = find_gzip(validation.gzip_executable)
    end = validation.model.base.development_end()
    prepared = prepare_v14(validation.model, load_inputs_until(root, end, validation.vix3m_file))
    result = round2_diagnostics(prepared, validation, config)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        write_round2(staging, result, config, gzip_executable)
        staging.rename(output)
    return Round2Run(output, len(result.days) - 1, result.days[0], result.days[-1],
                     file_sha256(output / REPORT_NAME))
