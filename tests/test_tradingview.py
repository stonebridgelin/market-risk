"""TradingView 导入与校验测试（docs/TRADINGVIEW.md 第8节）。

全部使用 tests/fixtures/tradingview/ 下手工构造的小样本（或在临时目录中由其变形），
不依赖真实导出文件；只写入临时目录。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from market_risk.config import SymbolInfo, load_settings, load_symbols
from market_risk.data import tradingview as tv
from market_risk.storage.paths import StoragePaths

FIX = Path(__file__).resolve().parent / "fixtures" / "tradingview"
ISO_FILE = FIX / "iso" / "INDEX_S5FI, 1D.csv"
UNIX_FILE = FIX / "unix" / "INDEX_S5TW, 1D.csv"
NY = ZoneInfo("America/New_York")
D = dt.date
SYMBOLS = load_symbols()
EXPORT = D(2026, 9, 26)


@pytest.fixture()
def paths(tmp_path) -> StoragePaths:
    return StoragePaths(tmp_path)


def put(paths: StoragePaths, src: Path | None, export_date: dt.date = EXPORT,
        name: str | None = None, text: str | None = None) -> Path:
    """把样本放进 raw/<导出日期>/（可改文件名或内容）。"""
    d = paths.tv_raw_dir(export_date)
    d.mkdir(parents=True, exist_ok=True)
    target = d / (name or src.name)  # type: ignore[union-attr]
    if text is not None:
        target.write_text(text, encoding="utf-8")
    else:
        shutil.copyfile(src, target)  # type: ignore[arg-type]
    return target


def issues(report: tv.FileReport, level: str | None = None) -> list[str]:
    return [i.message for i in report.issues if level is None or i.level == level]


# ---------------------------------------------------------------------------
# 文件名与时间换算（第5.1节）
# ---------------------------------------------------------------------------


def test_parse_filename():
    assert tv.parse_filename("INDEX_S5FI, 1D.csv", SYMBOLS) == ("INDEX:S5FI", "1D")
    assert tv.parse_filename("FRED_BAMLH0A0HYM2, 1D_3f2a1.csv", SYMBOLS) == (
        "FRED:BAMLH0A0HYM2", "1D"
    )
    assert tv.parse_filename("AMEX_SPY, 1W (1).csv", SYMBOLS) == ("AMEX:SPY", "1W")
    with pytest.raises(tv.TradingViewError, match="无法解析文件名"):
        tv.parse_filename("s5fi.csv", SYMBOLS)
    with pytest.raises(tv.TradingViewError, match="filename_aliases"):
        tv.parse_filename("S5FI, 1D.csv", SYMBOLS)
    aliased = {"INDEX:S5FI": SymbolInfo("S5FI", "INDEX:S5FI", filename_aliases=("S5FI",))}
    assert tv.parse_filename("S5FI, 1D.csv", aliased) == ("INDEX:S5FI", "1D")


def test_iso_time_uses_local_date_not_utc():
    """美东 20:00 在 UTC 已是次日；ISO 必须取偏移下的本地日期。"""
    d, fmt, _ = tv.parse_time("2025-11-28T20:00:00-05:00", NY)
    assert (d, fmt) == (D(2025, 11, 28), "iso")
    assert tv.parse_time("2025-11-28", NY)[0] == D(2025, 11, 28)


def test_unix_time_uses_symbol_timezone():
    ts = int(dt.datetime(2025, 11, 28, 20, 0, tzinfo=NY).timestamp())  # UTC 11-29 01:00
    assert tv.parse_time(str(ts), NY)[:2] == (D(2025, 11, 28), "unix")
    assert tv.parse_time(str(ts * 1000), NY)[0] == D(2025, 11, 28)   # 毫秒
    assert tv.parse_time(str(ts), ZoneInfo("UTC"))[0] == D(2025, 11, 29)
    with pytest.raises(tv.TradingViewError):
        tv.parse_time("yesterday", NY)


# ---------------------------------------------------------------------------
# 正常导入
# ---------------------------------------------------------------------------


def test_import_iso_and_unix(paths):
    put(paths, ISO_FILE)
    put(paths, UNIX_FILE)
    result = tv.import_directory(paths.tv_raw_dir(EXPORT), paths, SYMBOLS)
    by_symbol = {r.symbol: r for r in result.reports}
    fi, tw = by_symbol["S5FI"], by_symbol["S5TW"]
    assert (fi.status, tw.status) == (tv.PASSED, tv.PASSED)
    assert (fi.time_format, tw.time_format) == ("iso", "unix")
    assert (fi.first_date, fi.last_date) == (D(2025, 10, 20), D(2025, 11, 28))
    assert tw.first_date == D(2025, 10, 20)
    assert len(fi.bars) == 29
    assert any("已知读数核对通过 3/5" in m for m in issues(fi, "info"))
    assert any("2025-08-29 不在导出范围" in m for m in issues(fi, "info"))

    # 清洗结果：保留指标列（extra_ 前缀）与来源文件
    processed = paths.tv_processed_file("S5FI").read_text("utf-8").splitlines()
    assert processed[0] == "date,open,high,low,close,volume,extra_ma,source_file"
    assert processed[-1].startswith("2025-11-28,") and "raw/2026-09-26/INDEX_S5FI, 1D.csv" in processed[-1]
    assert tv.read_processed(paths, "S5TW")[D(2025, 10, 31)] == 38.56
    assert result.processed == {"S5FI": 29, "S5TW": 29}

    # manifest
    manifest = tv.read_manifest(paths.tv_manifest)
    row = manifest["data/manual/tradingview/raw/2026-09-26/INDEX_S5FI, 1D.csv"]
    assert row["tv_symbol"] == "INDEX:S5FI" and row["validation_status"] == "passed"
    assert row["rows"] == "29" and len(row["sha256"]) == 64
    assert list(row) == tv.MANIFEST_FIELDS


def test_raw_files_are_not_modified(paths):
    target = put(paths, ISO_FILE)
    before = target.read_bytes()
    tv.import_directory(paths.tv_raw_dir(EXPORT), paths, SYMBOLS)
    assert target.read_bytes() == before


def test_summary_and_list_are_readable(paths):
    put(paths, ISO_FILE)
    put(paths, UNIX_FILE)
    result = tv.import_directory(paths.tv_raw_dir(EXPORT), paths, SYMBOLS)
    text = tv.format_import_summary(result)
    for word in ("标的", "起止日期", "行数", "结果", "主要问题", "S5FI", "2025-10-20 至 2025-11-28",
                 "通过", "结论：2 个文件，通过 2，警告 0，失败 0"):
        assert word in text
    listing = tv.format_list(paths)
    assert "S5FI" in listing and "INDEX:S5TW" in listing and "29" in listing
    assert tv.format_list(StoragePaths(paths.root / "empty")) == "尚未导入任何 TradingView 文件"


# ---------------------------------------------------------------------------
# 日期偏移（第8节：构造"日期整体错开一天"的文件）
# ---------------------------------------------------------------------------


def _utc_midnight_file() -> str:
    """把 S5TW 的时间戳改成真实日期的 UTC 午夜：按美东换算会早一天。"""
    lines = UNIX_FILE.read_text("utf-8").splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        ts, rest = line.split(",", 1)
        day = dt.datetime.fromtimestamp(int(ts), NY).date()
        midnight = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp())
        out.append(f"{midnight},{rest}")
    return "\n".join(out) + "\n"


def test_shifted_dates_reported(paths):
    put(paths, None, name=UNIX_FILE.name, text=_utc_midnight_file())
    result = tv.import_directory(paths.tv_raw_dir(EXPORT), paths, SYMBOLS)
    rep = result.reports[0]
    assert rep.status == tv.FAILED
    failed = issues(rep, tv.FAILED)
    assert any("疑似日期偏移" in m and "2025-10-31=38.56" in m and "2025-10-30" in m for m in failed)
    assert any("疑似日期整体偏移" in m for m in failed)
    assert any("UTC 午夜" in m for m in issues(rep, tv.WARNING))
    assert "S5TW" not in result.processed  # 失败的文件不进入清洗结果
    assert "需要检查或重新导出" in tv.format_import_summary(result)


def test_shift_fixed_by_timezone_setting(paths):
    put(paths, None, name=UNIX_FILE.name, text=_utc_midnight_file())
    fixed = {**SYMBOLS, "INDEX:S5TW": dataclasses.replace(SYMBOLS["INDEX:S5TW"], timezone="UTC")}
    rep = tv.import_directory(paths.tv_raw_dir(EXPORT), paths, fixed).reports[0]
    assert rep.status == tv.PASSED


def test_single_value_mismatch(paths):
    text = ISO_FILE.read_text("utf-8").replace(",40.15,", ",40.25,")
    rep = tv.import_directory(
        put(paths, None, name=ISO_FILE.name, text=text).parent, paths, SYMBOLS
    ).reports[0]
    assert any("已知读数不符：2025-10-31 程序读到 40.25，已知 40.15" in m for m in issues(rep))


# ---------------------------------------------------------------------------
# 不完整K线（第8节）
# ---------------------------------------------------------------------------


def test_incomplete_last_bar_excluded(paths):
    export = D(2025, 11, 28)
    d = put(paths, ISO_FILE, export_date=export).parent
    before_close = dt.datetime(2025, 11, 28, 12, 0, tzinfo=NY)
    rep = tv.import_directory(d, paths, SYMBOLS, export_time=before_close).reports[0]
    assert rep.last_date == D(2025, 11, 27) or rep.last_date == D(2025, 11, 26)
    assert D(2025, 11, 28) not in rep.bars
    assert any("不完整" in m and "12:00" in m for m in issues(rep, tv.WARNING))
    # 11-28 的已知读数因此不在范围内，只作说明
    assert any("2025-11-28 不在导出范围" in m for m in issues(rep, "info"))


def test_last_bar_after_close_kept(paths):
    export = D(2025, 11, 28)
    d = put(paths, ISO_FILE, export_date=export).parent
    after_close = dt.datetime(2025, 11, 28, 17, 0, tzinfo=NY)
    rep = tv.import_directory(d, paths, SYMBOLS, export_time=after_close).reports[0]
    assert D(2025, 11, 28) in rep.bars and rep.status == tv.PASSED


# ---------------------------------------------------------------------------
# 多次导出合并（第5.3节）
# ---------------------------------------------------------------------------


def test_overlapping_exports_must_agree(paths):
    put(paths, ISO_FILE, export_date=D(2026, 9, 26))
    tv.import_directory(paths.tv_raw_dir(D(2026, 9, 26)), paths, SYMBOLS)
    original = paths.tv_processed_file("S5FI").read_text("utf-8")

    lines = ISO_FILE.read_text("utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("2025-11-03"):
            cells = line.split(",")
            cells[4] = f"{float(cells[4]) + 0.02:.2f}"  # 收盘值差 0.02，超过容差 0.005
            lines[i] = ",".join(cells)
    changed = "\n".join(lines) + "\n"
    put(paths, None, export_date=D(2026, 9, 27), name=ISO_FILE.name, text=changed)
    result = tv.import_directory(paths.tv_raw_dir(D(2026, 9, 27)), paths, SYMBOLS)
    assert result.merge_errors and "2025-11-03" in result.merge_errors[0]
    assert "不自动覆盖" in result.merge_errors[0]
    assert paths.tv_processed_file("S5FI").read_text("utf-8") == original


def test_overlapping_exports_identical_are_merged(paths):
    put(paths, ISO_FILE, export_date=D(2026, 9, 26))
    put(paths, ISO_FILE, export_date=D(2026, 9, 27))
    tv.import_directory(paths.tv_raw_dir(D(2026, 9, 26)), paths, SYMBOLS)
    result = tv.import_directory(paths.tv_raw_dir(D(2026, 9, 27)), paths, SYMBOLS)
    assert result.merge_errors == [] and result.processed["S5FI"] == 29
    assert len(tv.read_manifest(paths.tv_manifest)) == 2


def test_modified_raw_file_detected(paths):
    target = put(paths, ISO_FILE)
    tv.import_directory(target.parent, paths, SYMBOLS)
    target.write_text(ISO_FILE.read_text("utf-8").replace("44.70", "44.71"), encoding="utf-8")
    rep = tv.import_directory(target.parent, paths, SYMBOLS).reports[0]
    assert any("sha256 不符" in m for m in issues(rep, tv.FAILED))
    # 保留原哈希：再次导入仍能发现
    rep = tv.import_directory(target.parent, paths, SYMBOLS).reports[0]
    assert any("sha256 不符" in m for m in issues(rep, tv.FAILED))


# ---------------------------------------------------------------------------
# 其他校验（第5.2节）
# ---------------------------------------------------------------------------


def _one(paths, name, text, symbols=SYMBOLS):
    put(paths, None, name=name, text=text)
    return tv.import_directory(paths.tv_raw_dir(EXPORT), paths, symbols).reports[0]


def test_duplicate_dates_failed(paths):
    lines = ISO_FILE.read_text("utf-8").splitlines()
    rep = _one(paths, ISO_FILE.name, "\n".join([*lines, lines[-1]]) + "\n")
    assert any("日期重复" in m for m in issues(rep, tv.FAILED))


def test_unsorted_dates_warning(paths):
    lines = ISO_FILE.read_text("utf-8").splitlines()
    rep = _one(paths, ISO_FILE.name, "\n".join([lines[0], *reversed(lines[1:])]) + "\n")
    assert rep.status == tv.WARNING and any("顺序" in m for m in issues(rep))


def test_percent_out_of_range_failed(paths):
    text = ISO_FILE.read_text("utf-8").replace(",46.00,", ",146.00,", 1)
    rep = _one(paths, ISO_FILE.name, text)
    assert any("0–100" in m for m in issues(rep, tv.FAILED))


def test_missing_trading_day_warning(paths):
    lines = [x for x in ISO_FILE.read_text("utf-8").splitlines() if not x.startswith("2025-11-05")]
    rep = _one(paths, ISO_FILE.name, "\n".join(lines) + "\n")
    assert rep.status == tv.WARNING
    assert any("缺少 NYSE 交易日 1 个：2025-11-05" in m for m in issues(rep))


def test_unregistered_symbol_and_non_daily(paths):
    text = "time,open,high,low,close,Volume\n2025-11-28T09:30:00-05:00,1,2,0.5,1.5,100\n"
    rep = _one(paths, "AMEX_XYZ, 1D.csv", text)
    assert rep.symbol == "XYZ" and rep.status == tv.WARNING
    assert any("未在 config/symbols.yaml 登记" in m for m in issues(rep))
    assert rep.bars[D(2025, 11, 28)].volume == 100


def test_bad_files_failed(paths):
    rep = _one(paths, "AMEX_XYZ, 1W.csv", "time,close\n2025-11-28,1\n")
    assert any("不是日线" in m for m in issues(rep, tv.FAILED))


def test_unreadable_contents(paths):
    rep = _one(paths, "AMEX_ABC, 1D.csv", "date,value\n2025-11-28,1\n")
    assert any("缺少 time 或 close 列" in m for m in issues(rep, tv.FAILED))


def test_bad_filename_failed(paths):
    rep = _one(paths, "random.csv", "time,close\n2025-11-28,1\n")
    assert rep.status == tv.FAILED and any("无法解析文件名" in m for m in issues(rep))


def test_bond_calendar_and_history_checks(paths):
    info = SymbolInfo("OAS", "FRED:BAMLH0A0HYM2", unit="percent", calendar="bond",
                      inception=D(1996, 12, 31))
    symbols = {"FRED:BAMLH0A0HYM2": info}
    text = ("time,open,high,low,close\n"
            "2025-10-10,3.18,3.18,3.18,3.18\n2025-10-13,3.18,3.18,3.18,3.18\n"
            "2025-10-14,3.11,3.11,3.11,3.11\n")
    rep = _one(paths, "FRED_BAMLH0A0HYM2, 1D.csv", text, symbols)
    assert any("历史可能未完整加载" in m for m in issues(rep, tv.WARNING))
    assert not any("非 NYSE" in m for m in issues(rep))


def test_import_dir_must_be_under_raw(paths, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(tv.TradingViewError, match="原始文件应放在"):
        tv.import_directory(outside, paths, SYMBOLS)
    empty = paths.tv_raw_dir(EXPORT)
    empty.mkdir(parents=True)
    with pytest.raises(tv.TradingViewError, match="没有 CSV"):
        tv.import_directory(empty, paths, SYMBOLS)
    bad_name = paths.tv_raw_root / "latest"
    bad_name.mkdir()
    (bad_name / "INDEX_S5FI, 1D.csv").write_text("time,close\n", encoding="utf-8")
    with pytest.raises(tv.TradingViewError, match="不是导出日期"):
        tv.import_directory(bad_name, paths, SYMBOLS)


# ---------------------------------------------------------------------------
# 命令行（存储根目录改为临时目录）
# ---------------------------------------------------------------------------


def test_cli_tv_import_list_validate(paths, monkeypatch):
    import market_risk.cli as cli

    settings = dataclasses.replace(load_settings(), storage_root=paths.root)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    put(paths, ISO_FILE)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["tv", "import", "--dir", str(paths.tv_raw_dir(EXPORT))])
    assert r.exit_code == 0, r.output
    assert "TradingView 导入汇总" in r.stdout
    r = runner.invoke(cli.app, ["tv", "list"])
    assert r.exit_code == 0 and "S5FI" in r.stdout
    r = runner.invoke(cli.app, ["tv", "validate", "--symbol", "S5FI"])
    assert r.exit_code == 0 and "通过 1" in r.stdout

    put(paths, None, name=UNIX_FILE.name, text=_utc_midnight_file())
    r = runner.invoke(cli.app, ["tv", "import", "--dir", str(paths.tv_raw_dir(EXPORT)),
                                "--export-time", "2026-09-26T15:00-04:00"])
    assert r.exit_code == 2
    r = runner.invoke(cli.app, ["tv", "import", "--dir", str(paths.root)])
    assert r.exit_code == 1
