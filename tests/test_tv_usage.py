"""TradingView 数据的使用（docs/TRADINGVIEW.md 第6节）：广度读取顺序、OAS 长历史、重叠比对。

使用手工构造的样本（或由离线接口数据生成的 TradingView 格式文件），只写入临时目录。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import shutil
from pathlib import Path

import pytest
from conftest import load_sample_raw

from market_risk.config import SymbolInfo, load_symbols
from market_risk.data import tradingview as tv
from market_risk.data.breadth import load_breadth, make_reading, merge_breadth, upsert_breadth
from market_risk.data.fred import fill_long_history
from market_risk.data.tv_compare import compare_series, format_results
from market_risk.models import BreadthReading
from market_risk.storage.paths import StoragePaths

FIX = Path(__file__).resolve().parent / "fixtures" / "tradingview"
D = dt.date
# 测试样本只有几十行：去掉登记的起始日期，避免"历史未完整加载"警告（该检查另有测试）
SYMBOLS = {k: dataclasses.replace(v, inception=None) for k, v in load_symbols().items()}
EXPORT = D(2026, 9, 26)


@pytest.fixture()
def paths(tmp_path):
    return StoragePaths(tmp_path)


def _import(paths: StoragePaths, files: dict[str, str | Path]):
    d = paths.tv_raw_dir(EXPORT)
    d.mkdir(parents=True, exist_ok=True)
    for name, src in files.items():
        if isinstance(src, Path):
            shutil.copyfile(src, d / name)
        else:
            (d / name).write_text(src, encoding="utf-8")
    return tv.import_directory(d, paths, SYMBOLS)


# ---------------------------------------------------------------------------
# 6.1 广度读取顺序
# ---------------------------------------------------------------------------


def test_merge_breadth_tradingview_first():
    manual = {
        D(2025, 11, 28): BreadthReading(D(2025, 11, 28), 58.40, 76.73),   # 与 TV 不一致
        D(2025, 12, 1): BreadthReading(D(2025, 12, 1), 60.00, 70.00),     # TV 未覆盖
        D(2025, 11, 26): BreadthReading(D(2025, 11, 26), 55.00, 70.00),   # 与 TV 一致
    }
    tv_fi = {D(2025, 11, 28): 58.44, D(2025, 11, 26): 55.0, D(2025, 11, 25): 50.0}
    tv_tw = {D(2025, 11, 28): 76.73, D(2025, 11, 26): 70.0}   # 11-25 只有 S5FI
    merged, conflicts = merge_breadth(tv_fi, tv_tw, manual)
    assert merged[D(2025, 11, 28)].s5fi == 58.44 and merged[D(2025, 11, 28)].source == "tradingview"
    assert merged[D(2025, 12, 1)].source == "manual"
    assert D(2025, 11, 25) not in merged
    assert list(conflicts) == [D(2025, 11, 28)]
    assert "以 TradingView 为准" in conflicts[D(2025, 11, 28)]


def test_load_breadth_from_processed_and_manual(paths):
    _import(paths, {"INDEX_S5FI, 1D.csv": FIX / "iso" / "INDEX_S5FI, 1D.csv",
                    "INDEX_S5TW, 1D.csv": FIX / "unix" / "INDEX_S5TW, 1D.csv"})
    upsert_breadth(paths.breadth_csv, make_reading(D(2025, 12, 1), 61.0, 71.0))
    upsert_breadth(paths.breadth_csv, make_reading(D(2025, 10, 31), 40.00, 38.56))
    readings, conflicts = load_breadth(paths)
    assert readings[D(2025, 10, 31)].s5fi == 40.15   # TradingView 为准
    assert readings[D(2025, 11, 28)].s5tw == 76.73
    assert readings[D(2025, 12, 1)].source == "manual"
    assert list(conflicts) == [D(2025, 10, 31)]


def test_prompt_names_breadth_source(paths):
    from market_risk.config import load_settings
    from market_risk.pipeline import run_scoring
    from market_risk.storage.runs import GitInfo

    base = D(2025, 11, 28)
    raw = load_sample_raw("2025-11-28", {base: BreadthReading(base, 58.44, 76.73, "tradingview")})
    out = run_scoring(raw, load_settings(), paths, GitInfo("a" * 40, False))
    prompt = (out.run_dir / "prompt.md").read_text("utf-8")
    assert "S5FI、S5TW：2025-11-28：TradingView 导出数据" in prompt


# ---------------------------------------------------------------------------
# 6.2 OAS 长历史
# ---------------------------------------------------------------------------


def test_fill_long_history_only_before_fred_start():
    fred_values = {D(2023, 9, 26): 4.04, D(2023, 9, 27): 4.03}
    tv_values = {D(2023, 9, 22): 3.90, D(2023, 9, 25): 3.95, D(2023, 9, 26): 9.99, D(2023, 1, 3): 4.5}
    merged, notes = fill_long_history(fred_values, tv_values, D(2023, 6, 1), D(2023, 9, 27))
    assert merged[D(2023, 9, 26)] == 4.04          # FRED 有的日期不用 TradingView
    assert merged[D(2023, 9, 25)] == 3.95 and D(2023, 1, 3) not in merged
    assert "2023-09-22 至 2023-09-25 共 2 个观测来自 TradingView" in notes[0]
    assert fill_long_history(fred_values, {}, D(2023, 6, 1), D(2023, 9, 27)) == (fred_values, [])
    merged, _ = fill_long_history({}, tv_values, D(2023, 1, 1), D(2023, 9, 30))
    assert len(merged) == 4


def test_long_history_setting_parsed(tmp_path):
    from market_risk.config import ConfigError, load_settings

    assert load_settings().oas_long_history_source == "tradingview"   # 2026-09-27 开启
    text = (Path(__file__).resolve().parents[1] / "config" / "settings.yaml").read_text("utf-8")
    bad = tmp_path / "s.yaml"
    bad.write_text(text.replace("long_history_source: tradingview", "long_history_source: yes please"), "utf-8")
    with pytest.raises(ConfigError, match="long_history_source"):
        load_settings(bad)


# ---------------------------------------------------------------------------
# 6.2 / 6.3 重叠比对与交叉校验
# ---------------------------------------------------------------------------


def _tv_file_from_series(series: dict[dt.date, float | None]) -> str:
    lines = ["time,open,high,low,close"]
    for d, v in sorted(series.items()):
        if v is not None:
            lines.append(f"{d.isoformat()},{v},{v},{v},{v}")
    return "\n".join(lines) + "\n"


def test_oas_tradingview_file_matches_fred(paths):
    """用离线 FRED 数据生成 TradingView 格式的 OAS 文件：导入通过，重叠比对完全一致。"""
    oas = load_sample_raw("2025-11-28").oas
    result = _import(paths, {"FRED_BAMLH0A0HYM2, 1D.csv": _tv_file_from_series(oas)})
    rep = result.reports[0]
    assert rep.symbol == "BAMLH0A0HYM2" and rep.status == tv.PASSED, rep.issues
    msgs = [i.message for i in rep.issues]
    assert any("月末周末观测" in m and "2025-08-31" in m for m in msgs)
    assert any("股市休市日有数据" in m for m in msgs)   # 感恩节的沿用值
    info = SYMBOLS["FRED:BAMLH0A0HYM2"]
    cmp = compare_series(info, tv.read_processed(paths, "BAMLH0A0HYM2"), oas, info.tolerance)
    assert cmp.ok and cmp.overlap == len([v for v in oas.values() if v is not None])
    assert cmp.status == "一致"


def test_compare_reports_mismatch_and_gaps():
    info = SymbolInfo("SPY", "AMEX:SPY", api_source="yahoo:SPY")
    tv_s = {D(2025, 11, 24): 668.73, D(2025, 11, 25): 675.03, D(2025, 11, 26): 679.68, D(2025, 11, 28): 683.39}
    api = {D(2025, 11, 24): 668.73, D(2025, 11, 25): 675.02, D(2025, 11, 28): 683.39, D(2025, 11, 21): 659.03}
    r = compare_series(info, tv_s, api, 0.005)
    assert r.mismatches == [(D(2025, 11, 25), 675.03, 675.02, 0.01)]
    assert r.tv_only == [D(2025, 11, 26)] and r.api_only == []
    assert (r.first, r.last, r.overlap) == (D(2025, 11, 24), D(2025, 11, 28), 3)
    assert r.status == "有差异"
    assert compare_series(info, tv_s, api, 0.02).ok
    text = format_results([r], "测试", "2026-09-27T00:00:00+00:00")
    assert "| 2025-11-25 | 675.03 | 675.02 | +0.0100 |" in text
    assert "仅 TradingView 有数据的日期（1 个）：2025-11-26" in text
    none = compare_series(info, {D(2020, 1, 2): 1.0}, api, 0.005)
    assert none.status == "无重叠"
    assert compare_series(info, {}, api, 0.005).status == "无法比对"


def test_exchange_prefix_fallback(paths):
    """导出前缀与登记不同（AMEX_SPY vs 登记的 BATS:SPY）：按代码唯一匹配，并用 known_values 校验。"""
    closes = load_sample_raw("2025-11-28").closes["SPY"]
    rows = {d: v for d, v in closes.items() if d >= D(2025, 10, 1)}
    rep = _import(paths, {"AMEX_SPY, 1D.csv": _tv_file_from_series(rows)}).reports[0]
    assert rep.tv_symbol == "BATS:SPY" and rep.symbol == "SPY"
    assert rep.status == tv.PASSED
    assert any("交易所前缀与登记不同" in i.message for i in rep.issues)
    assert any("已知读数核对通过 2/4" in i.message for i in rep.issues)


def _daily(start: dt.date, n: int) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(n)]


def test_dividend_adjustment_detected():
    """TV 相对 Yahoo 不复权收盘价系统性偏低、越早偏差越大：报告疑似开启了股息调整。"""
    info = SymbolInfo("SPY", "BATS:SPY", api_source="yahoo:SPY")
    days = _daily(D(2020, 1, 1), 200)
    api = dict.fromkeys(days, 100.0)
    tv_adj = {d: 100.0 * (1 - 0.03 * (len(days) - i) / len(days)) for i, d in enumerate(days)}   # 早期低 3%
    r = compare_series(info, tv_adj, api, 0.005)
    assert r.adjustment_suspected and r.max_abs_diff > 2.5
    assert "疑似开启了股息调整" in format_results([r], "t", "t")
    same = compare_series(info, dict(api), api, 0.005)
    assert not same.adjustment_suspected and same.max_abs_diff == 0.0
    assert "未见股息调整迹象" in format_results([same], "t", "t")


def test_no_adjustment_check_for_fred():
    info = SymbolInfo("VIX", "CBOE_DLY:VIX", api_source="fred:VIXCLS")
    days = _daily(D(2020, 1, 1), 50)
    r = compare_series(info, dict.fromkeys(days, 15.0), dict.fromkeys(days, 15.0), 0.005)
    assert not r.check_adjustment and "股息调整检查" not in format_results([r], "t", "t")


def test_period_stats_and_listed_dates():
    """按 2008-08-01 分段统计；回测区间内 |差值| > 0.02 的日期（SPY、QQQ、RSP）全部列出。"""
    info = SymbolInfo("SPY", "BATS:SPY", api_source="yahoo:SPY")
    days = [D(2008, 7, 30), D(2008, 7, 31), D(2008, 8, 1), D(2009, 7, 16), D(2012, 1, 20), D(2015, 8, 24)]
    api = dict.fromkeys(days, 100.0)
    tv = {**api, D(2008, 7, 31): 100.5, D(2008, 8, 1): 100.02, D(2009, 7, 16): 101.04,
          D(2012, 1, 20): 99.59, D(2015, 8, 24): 100.01}
    r = compare_series(info, tv, api, 0.005)
    before, after = r.periods()
    assert (before.overlap, before.mismatches, before.max_abs_diff, before.over_report) == (2, 1, 0.5, 1)
    assert (after.overlap, after.mismatches, after.over_report, after.positive, after.negative) == (4, 4, 3, 3, 1)
    assert after.max_abs_diff == 1.04
    assert [m[0] for m in r.listed_dates()] == [D(2009, 7, 16), D(2012, 1, 20)]   # 2008-08-01 差 0.02 不列
    text = format_results([r], "t", "t")
    assert "## 按时期统计" in text and "2008-08-01 起（回测区间）" in text and "| 2009-07-16 |" in text
    hyg = compare_series(SymbolInfo("HYG", "BATS:HYG", api_source="yahoo:HYG"), tv, api, 0.005)
    assert hyg.listed_dates() == []


def test_crosscheck_not_applicable_and_third_party(paths, monkeypatch):
    """口径不同的标的标为"不适用"；第三方数据先用已知读数验证不复权，再判断哪一方正确。"""
    from market_risk import services

    known = {D(2025, 10, 31): 682.06, D(2025, 11, 28): 683.39}
    symbols = {
        "BATS:SPY": SymbolInfo("SPY", "BATS:SPY", usage="crosscheck", api_source="yahoo:SPY", known_values=known),
        "FRED:DGS10": SymbolInfo("DGS10", "FRED:DGS10", usage="crosscheck", api_source="treasury:10Y",
                                 crosscheck_note="口径不同"),
    }
    monkeypatch.setattr(services, "load_symbols", lambda: symbols)
    days = [D(2009, 7, 15), D(2009, 7, 16), D(2012, 1, 20), *known]
    api = {**dict.fromkeys(days, 100.0), **known}
    tv_rows = {**api, D(2009, 7, 16): 101.04, D(2012, 1, 20): 99.59}
    out = paths.tv_processed_file("SPY")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("date,close\n" + "".join(f"{d},{v}\n" for d, v in sorted(tv_rows.items())), encoding="utf-8")
    from market_risk.config import load_settings
    from market_risk.storage import db

    ctx = services.Context(load_settings(), paths, db.sqlite_url(paths.root / "db" / "m.sqlite"))
    third = {**known, D(2009, 7, 16): 100.0, D(2012, 1, 20): 99.59}
    report = services.tv_crosscheck(ctx, loader=lambda info, s, e: api,
                                    third_party=(lambda sym, s, e: third, "测试来源"))
    spy, dgs = report.results
    assert dgs.status == "不适用" and "不适用" in report.text and "无法比对" not in report.text
    assert [c.verdict for c in spy.third_party] == ["Yahoo 正确", "TradingView 正确"]
    assert "已用已知读数验证为不复权" in spy.third_party_note
    # 第三方未通过不复权验证：不采用
    bad = {**third, D(2025, 10, 31): 679.5}
    report = services.tv_crosscheck(ctx, loader=lambda info, s, e: api,
                                    third_party=(lambda sym, s, e: bad, "测试来源"))
    assert report.results[0].third_party == [] and "未通过不复权验证" in report.results[0].third_party_note
    report = services.tv_crosscheck(ctx, loader=lambda info, s, e: api, third_party=(None, "未配置"))
    assert report.results[0].third_party_note == "未配置"


def test_tiingo_parse_and_verify():
    from market_risk.data import tiingo
    from market_risk.data.cache import DataFetchError

    text = ('[{"date": "2025-10-31T00:00:00.000Z", "close": 682.06, "adjClose": 675.1},'
            ' {"date": "2025-11-28T00:00:00.000Z", "close": 683.39, "adjClose": 676.4}]')
    closes = tiingo.parse_prices(text)
    assert closes == {D(2025, 10, 31): 682.06, D(2025, 11, 28): 683.39}          # 取 close，不取 adjClose
    assert tiingo.verify_unadjusted(closes, {D(2025, 10, 31): 682.06}) == []
    assert tiingo.verify_unadjusted(closes, {D(2025, 10, 31): 680.0, D(2025, 9, 26): 661.82}) == [
        "2025-09-26 无数据", "2025-10-31 为 682.06，已知读数 680.0"]
    with pytest.raises(DataFetchError):
        tiingo.parse_prices("not json token=abc")
