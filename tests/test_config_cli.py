"""配置读取与命令行（阶段1部分）测试。"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from typer.testing import CliRunner

from market_risk.cli import app
from market_risk.config import (
    PROJECT_ROOT,
    ConfigError,
    get_fred_api_key,
    load_holidays,
    load_settings,
)


def test_load_settings_defaults():
    s = load_settings()
    assert s.scored_symbols == ("SPY", "QQQ", "RSP")
    assert s.moving_averages == (5, 10, 20, 30, 50, 200)
    assert s.d1_includes_t_minus_20 is True
    assert s.three_segment_query_offset == 45
    assert s.cache_dir == PROJECT_ROOT / "data" / "cache"
    assert s.near_threshold.oas_bp == 3


def test_load_settings_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(bad)
    partial = tmp_path / "partial.yaml"
    partial.write_text("symbols: {scored: [SPY]}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(partial)


def test_load_holidays(tmp_path):
    h = load_holidays()
    assert dt.date(2025, 10, 13) in h.bond_holidays
    assert dt.date(2025, 10, 13) not in h.stock_holidays
    assert dt.date(2025, 11, 28) in h.stock_early_closes
    f = tmp_path / "h.yaml"
    f.write_text("stock:\n  holidays: ['2025-13-01']\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_holidays(f)
    f.write_text("stock:\n  holidays: ['2025-01-01']\n", encoding="utf-8")
    assert load_holidays(f).stock_holidays == frozenset({dt.date(2025, 1, 1)})


def test_fred_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        get_fred_api_key(empty_env)
    monkeypatch.setenv("FRED_API_KEY", "abc123")
    assert get_fred_api_key(empty_env) == "abc123"


def test_cli_dates():
    result = CliRunner().invoke(app, ["dates", "--date", "2025-11-28"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["t_minus_5"] == "2025-11-20"
    assert payload["oas_o1"] == "2025-11-26"
    assert payload["oas_o6_v3r1"] == "2025-11-19"
    assert payload["is_early_close"] is True


def test_cli_dates_rejects_bad_input():
    runner = CliRunner()
    assert runner.invoke(app, ["dates", "--date", "2025-11-27"]).exit_code == 1
    assert runner.invoke(app, ["dates", "--date", "20251128"]).exit_code != 0
