"""补充历史真实净值的读写边界：只读 SPX、QQQ 截至窗口末日（2009-09-30）的收盘价，只写诊断目录。"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn.config_v14 import Round2Config, load_round2_config, load_validation_config
from market_risk.wavewarn.data import DEVELOPMENT_END
from market_risk.wavewarn.diagnostics_round2_run import HASH_FILE, NAV_DAILY, ROUND2_CONFIG
from market_risk.wavewarn.evaluation import SYMBOLS
from market_risk.wavewarn.evaluation_run import file_sha256, write_csv
from market_risk.wavewarn.extended_history_v14 import prepare_price_window
from market_risk.wavewarn.extended_nav import ExtendedNavResult, extended_nav
from market_risk.wavewarn.extended_nav_report import REPORT_NAME, csv_tables, nav_daily, report_lines
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.inputs import input_files, series_until
from market_risk.wavewarn.lock_guard import find_gzip
from market_risk.wavewarn.validation_output import gzip_file
from market_risk.wavewarn.validation_run import CONFIG_FILE

NAV_PLACES = Decimal("1e-12")


@dataclass(frozen=True)
class ExtendedNavRun:
    output: Path
    t0: dt.date
    first_interval: dt.date
    last_day: dt.date
    intervals: int
    report_sha256: str


def load_price_inputs(root: Path, end: dt.date, vix3m_file: str) -> DevelopmentInputs:
    """只读 SPX 与 QQQ 的收盘价，在第一条晚于 end 的行之前停止；交易日轴自 SPX 的第一个交易日起至 end。"""
    files = input_files(root, vix3m_file)
    series = {symbol: series_until(files[symbol][0], files[symbol][1], end, files[symbol][2], DEVELOPMENT_END)
              for symbol in SYMBOLS}
    return DevelopmentInputs(tuple(stock_trading_days(min(series["SPX"]), end)), series)


def write_extended_nav(destination: Path, result: ExtendedNavResult, config: Round2Config,
                       gzip_executable: str) -> None:
    """写出表格、逐日净值（gzip -n）与报告，最后写各文件的 SHA-256 清单。destination 须已存在。"""
    for name, header, rows in csv_tables(result, config):
        write_csv(destination / name, header, rows)
    header, rows = nav_daily(result, NAV_PLACES)
    write_csv(destination / NAV_DAILY, header, rows)
    packed = gzip_file(destination / NAV_DAILY, gzip_executable)
    (destination / REPORT_NAME).write_text("\n".join(report_lines(result, config)), encoding="utf-8")
    hashes = {path.name: file_sha256(path) for path in sorted(destination.iterdir())}
    hashes[f"{NAV_DAILY}（压缩前，不入库）"] = packed.raw
    (destination / HASH_FILE).write_text(json.dumps(hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_v14_extended_nav(root: Path) -> ExtendedNavRun:
    """补充历史的真实净值；输出目录已存在时拒绝覆盖。全部写齐后才把临时目录改名为输出目录。"""
    validation = load_validation_config(root / CONFIG_FILE)
    config = load_round2_config(root / ROUND2_CONFIG)
    output = root / config.extended_nav_output
    if output.exists():
        raise FileExistsError(f"补充历史净值目录已存在，拒绝覆盖：{output}")
    gzip_executable = find_gzip(validation.gzip_executable)
    inputs = load_price_inputs(root, validation.model.history_end, validation.vix3m_file)
    result = extended_nav(prepare_price_window(validation.model, inputs), validation, config)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging_", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        write_extended_nav(staging, result, config, gzip_executable)
        staging.rename(output)
    return ExtendedNavRun(output, result.prepared.t0, result.days[0], result.days[-1], len(result.days) - 1,
                          file_sha256(output / REPORT_NAME))
