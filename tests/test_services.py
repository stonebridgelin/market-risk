"""services.py 测试：业务函数可直接调用，返回数据类或可序列化为 JSON 的结构（CLAUDE.md 第12条）。"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
from conftest import load_sample_raw

from market_risk import services
from market_risk.config import load_settings, load_symbols
from market_risk.models import BreadthReading
from market_risk.storage import db, runs

D = dt.date
GIT = runs.GitInfo("d" * 40, False)
FIX_TV = Path(__file__).resolve().parent / "fixtures" / "tradingview"


@pytest.fixture()
def ctx(tmp_path):
    settings = dataclasses.replace(load_settings(), storage_root=tmp_path)
    return services.Context(settings, services.StoragePaths(tmp_path), db.sqlite_url(tmp_path / "db" / "m.sqlite"))


def as_json(obj) -> str:
    return json.dumps(runs.to_jsonable(obj), ensure_ascii=False)


def test_preview_and_samples():
    p = services.preview_dates(D(2025, 11, 28))
    assert p.refs.oas_o1 == D(2025, 11, 26) and any("holidays.yaml" in n for n in p.notes)
    assert '"t_minus_5": "2025-11-20"' in as_json(p)
    with pytest.raises(services.ServiceError, match="不是股票交易日"):
        services.preview_dates(D(2025, 11, 27))
    s = services.sample_dates(2020)[11]
    assert (s.date, s.weekday) == (D(2020, 12, 31), "周四") and "休市" in s.note


def test_resolve_base_date_and_breadth_inputs(ctx):
    with pytest.raises(services.ServiceError, match="必须提供基准日"):
        services.resolve_base_date(None, "backtest")
    with pytest.raises(services.ServiceError, match="mode"):
        services.resolve_base_date(D(2025, 11, 28), "weekly")
    with pytest.raises(services.ServiceError, match="不是股票交易日"):
        services.resolve_base_date(D(2025, 11, 29), "backtest")
    assert services.resolve_base_date(D(2025, 11, 28), "backtest") == D(2025, 11, 28)

    base = D(2025, 10, 31)
    services.record_breadth_inputs(ctx, base, services.BreadthInput(40.15, 38.56, 52.88, 57.65))
    assert services.breadth_existing(ctx, D(2025, 10, 24)).s5tw == 57.65
    with pytest.raises(services.ServiceError, match="同时提供"):
        services.record_breadth_inputs(ctx, base, services.BreadthInput(40.0, None))
    with pytest.raises(services.ServiceError, match="breadth add --overwrite"):
        services.record_breadth_inputs(ctx, base, services.BreadthInput(41.0, 38.56))


def test_score_raw_returns_serializable_outcome(ctx):
    base = D(2025, 11, 28)
    out = services.score_raw(ctx, load_sample_raw("2025-11-28", {base: BreadthReading(base, 58.44, 76.73)}), GIT)
    assert out.status == "complete" and [r.total for r in out.results] == [0, 0]
    text = as_json(out)
    assert '"version": "v2-M"' in text and '"official_set": true' in text


def test_validate_samples():
    report = services.validate_samples()
    # 离线样本 128 项；仓库中已有 data/market/ 时另有用数据集重新计分的比对（B1-5）
    assert report.ok and sum(1 for c in report.checks if "data/market" not in c.sample) == 128
    assert as_json(report)


def test_breadth_add(ctx):
    r = services.breadth_add(ctx, D(2025, 12, 1), 60, 70)
    assert r.changed and r.previous is None
    assert not services.breadth_add(ctx, D(2025, 12, 1), 60, 70).changed
    with pytest.raises(services.ServiceError, match="--overwrite"):
        services.breadth_add(ctx, D(2025, 12, 1), 61, 70)
    r = services.breadth_add(ctx, D(2025, 12, 1), 61, 70, overwrite=True)
    assert r.changed and r.previous.s5fi == 60
    with pytest.raises(services.ServiceError, match="0–100"):
        services.breadth_add(ctx, D(2025, 12, 1), 161, 70)


def test_outcome_compute_with_injected_loader(ctx):
    from market_risk import calendar as mcal

    base = D(2025, 10, 31)
    days = [base, *mcal.stock_trading_days(D(2025, 11, 3), D(2025, 12, 1))]

    def loader(symbol, start, end):
        drop = 95.59 if symbol == "SPX" else 93.1
        return {d: (100.0 if i == 0 else (drop if i == 10 else 99.0)) for i, d in enumerate(days)}

    with pytest.raises(services.ServiceError, match="才结束"):
        services.outcome_compute(ctx, base, loader=loader, today=D(2025, 12, 1))
    result = services.outcome_compute(ctx, base, loader=loader, today=D(2026, 1, 1))
    assert not result.outcome.is_event and result.outcome.near_event
    assert '"source": "computed"' in as_json(result)
    manual = services.outcome_add(ctx, base, -5.2, -6.0, today=D(2026, 1, 1))
    assert manual.outcome.is_event and manual.differences
    with pytest.raises(services.ServiceError, match="尚未结束"):
        services.outcome_add(ctx, D(2099, 1, 2), -1, -1, today=D(2026, 1, 1))


def test_tv_services_with_injected_loader(ctx):
    raw_dir = ctx.paths.tv_raw_dir(D(2026, 9, 26))
    raw_dir.mkdir(parents=True)
    shutil.copyfile(FIX_TV / "iso" / "INDEX_S5FI, 1D.csv", raw_dir / "INDEX_S5FI, 1D.csv")
    symbols = {k: dataclasses.replace(v, inception=None) for k, v in load_symbols().items()}
    result = services.tv_import(ctx, raw_dir, symbols=symbols)
    assert result.processed == {"S5FI": 29} and as_json(result)
    assert services.tv_list(ctx)[0]["symbol"] == "S5FI"
    assert services.tv_validate(ctx, "S5FI").reports[0].symbol == "S5FI"
    with pytest.raises(services.ServiceError, match="原始文件应放在"):
        services.tv_import(ctx, ctx.paths.root)
    with pytest.raises(services.ServiceError, match="api_source"):
        services.tv_compare(ctx, "S5FI", loader=lambda *a: {})

    report = services.tv_crosscheck(ctx, loader=lambda *a: {}, now=dt.datetime(2026, 9, 27, tzinfo=dt.UTC),
                                    third_party=(None, "未配置"))
    assert report.path == ctx.paths.tv_crosscheck_md and report.path.exists()
    # 尚未导入 crosscheck 标的；口径不同的 DGS10 为"不适用"
    assert {r.symbol: r.status for r in report.results if r.status != "无法比对"} == {"DGS10": "不适用"}

    def failing(*_):
        raise ConnectionError("down")

    oas = load_sample_raw("2025-11-28").oas
    lines = ["time,open,high,low,close"] + [f"{d},{v},{v},{v},{v}" for d, v in sorted(oas.items()) if v is not None]
    (raw_dir / "FRED_BAMLH0A0HYM2, 1D.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    services.tv_import(ctx, raw_dir)
    ok = services.tv_compare(ctx, "BAMLH0A0HYM2", loader=lambda info, s, e: oas)
    assert ok.results[0].ok and ok.path.name == "tradingview_compare_BAMLH0A0HYM2.md"
    bad = services.tv_compare(ctx, "BAMLH0A0HYM2", loader=failing)
    assert "接口数据获取失败" in bad.results[0].error


def test_official_material_legacy_db_stats(ctx, tmp_path):
    base = D(2025, 11, 28)
    out = services.score_raw(ctx, load_sample_raw("2025-11-28", {base: BreadthReading(base, 58.44, 76.73)}), GIT)
    pointer = services.official_set(ctx, base, out.run_id)
    assert pointer["set_by"] == "manual" and not pointer["reviewed"]
    assert services.official_confirm(ctx, base)["reviewed"] is True
    with pytest.raises(services.ServiceError):
        services.official_set(ctx, base, "run_20260101T000000Z_0000000")
    with pytest.raises(services.ServiceError):
        services.official_confirm(ctx, D(2025, 10, 31))

    f = tmp_path / "n.md"
    f.write_text("x", encoding="utf-8")
    services.material_add(ctx, base, "notes", f)
    assert len(services.material_list(ctx)) == 1
    with pytest.raises(services.ServiceError):
        services.material_add(ctx, base, "video", f)

    ctx.paths.legacy_excel.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "data" / "legacy" / "backtest_record_legacy.xlsx",
                    ctx.paths.legacy_excel)
    legacy = services.import_legacy(ctx, git=GIT)
    assert len(legacy.imported) == 4 and legacy.score_differences == []
    with pytest.raises(services.ServiceError, match="找不到"):
        services.import_legacy(ctx, tmp_path / "missing.xlsx", git=GIT)

    counts = services.rebuild_database(ctx)
    assert counts["runs"] == 5 and counts["officials"] == 4 and counts["materials"] == 1
    report = services.run_stats(ctx)
    assert "不确定" in report.text and report.workbook_path.exists()
    assert as_json(report)
