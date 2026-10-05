"""storage/paths.py 测试（docs/STORAGE.md 第2、8节）。只计算路径，不写文件。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from market_risk.storage.paths import (
    MARKET,
    RISK_SCORING,
    PathError,
    StoragePaths,
    make_run_id,
    normalize_subject,
    unique_run_id,
)

ROOT = Path("/repo")
P = StoragePaths(ROOT)
D = dt.date(2025, 11, 28)


def test_market_results_layout():
    assert P.results_date_dir(MARKET, RISK_SCORING, D) == (
        ROOT / "results" / "MARKET" / "risk_scoring" / "2025" / "2025-11-28"
    )
    assert P.official_pointer("market", RISK_SCORING, D).name == "official.json"
    run = "run_20260927T021530Z_a1b2c3d"
    assert P.run_dir(MARKET, RISK_SCORING, D, run).name == run
    assert P.run_inputs_dir(MARKET, RISK_SCORING, D, run) == (
        P.run_dir(MARKET, RISK_SCORING, D, run) / "inputs"
    )


def test_stock_layout_uses_stocks_dir_and_uppercase():
    d = dt.date(2026, 9, 25)
    assert P.results_date_dir("nvda", "stock_trend", d) == (
        ROOT / "results" / "stocks" / "NVDA" / "stock_trend" / "2026" / "2026-09-25"
    )
    assert P.materials_dir("nvda", d) == (
        ROOT / "data" / "materials" / "stocks" / "NVDA" / "2026" / "2026-09-25"
    )
    market_dir = ROOT / "data" / "materials" / "MARKET" / "2025" / "2025-11-28"
    assert P.materials_dir(MARKET, D) == market_dir
    assert normalize_subject(" brk.b ") == "BRK.B"


@pytest.mark.parametrize("bad", ["", "..", "A/B", "NV DA", "A..B", "X.", "../MARKET"])
def test_invalid_subject_rejected(bad):
    with pytest.raises(PathError):
        normalize_subject(bad)


def test_invalid_framework_and_run_id_rejected():
    with pytest.raises(PathError):
        P.results_date_dir(MARKET, "Risk-Scoring", D)
    with pytest.raises(PathError):
        P.run_dir(MARKET, RISK_SCORING, D, "run_20260927-103015_a1b2c3d")  # 旧格式
    with pytest.raises(PathError):
        P.run_dir(MARKET, RISK_SCORING, D, "../x")


def test_cache_paths():
    f = P.cache_file("fred", "BAMLH0A0HYM2", dt.date(2025, 1, 1), D)
    assert f == ROOT / "data" / "cache" / "fred" / "BAMLH0A0HYM2_2025-01-01_2025-11-28.csv"
    assert P.cache_meta_file(f).name == "BAMLH0A0HYM2_2025-01-01_2025-11-28.csv.meta.json"
    assert P.cache_file("yahoo", "BRK-B", D, D, ".json").suffix == ".json"
    with pytest.raises(PathError):
        P.cache_dir("tiger")
    with pytest.raises(PathError):
        P.cache_file("fred", "../x", D, D)


def test_fixed_paths():
    assert P.breadth_csv == ROOT / "data" / "manual" / "breadth.csv"
    assert P.outcomes_csv == ROOT / "data" / "manual" / "outcomes.csv"
    assert P.legacy_excel == ROOT / "data" / "legacy" / "backtest_record_legacy.xlsx"
    assert P.db_path == ROOT / "db" / "market_risk.sqlite"
    assert P.backtest_history_xlsx == ROOT / "reports" / "backtest_history.xlsx"
    assert P.backtest_stats_md == ROOT / "reports" / "backtest_stats.md"
    assert P.daily_report_csv(2026, 10) == ROOT / "reports" / "daily" / "2026-10.csv"
    with pytest.raises(PathError):
        P.daily_report_csv(2026, 13)


def test_run_id_uses_utc():
    """运行目录名用 UTC：北京时间 2026-09-27 10:15:30 = UTC 02:15:30。"""
    local = dt.datetime(2026, 9, 27, 10, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert make_run_id(local, "A1B2C3D4E5F6") == "run_20260927T021530Z_a1b2c3d"
    with pytest.raises(PathError):
        make_run_id(dt.datetime(2026, 9, 27, 10, 15, 30), "a1b2c3d")  # 无时区
    with pytest.raises(PathError):
        make_run_id(local, "zzz")


def test_unique_run_id_suffix():
    rid = "run_20260927T021530Z_a1b2c3d"
    assert unique_run_id(rid, []) == rid
    assert unique_run_id(rid, [rid]) == rid + "_2"
    assert unique_run_id(rid, [rid, rid + "_2"]) == rid + "_3"
    # 带后缀的编号也是合法目录名
    assert P.run_dir(MARKET, RISK_SCORING, D, rid + "_2").name == rid + "_2"


def test_v20_research_dir_is_only_computed():
    """M2 第一部分指令第二节第 8 小节：v20_research_dir 只计算路径，不读写、不建目录。"""
    assert P.v20_research_dir == ROOT / "reports" / "research" / "wavewarn_v20"
    assert P.v20_research_dir.parent.parent == P.reports_dir
