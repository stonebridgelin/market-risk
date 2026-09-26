"""联网测试（默认跳过）：uv run pytest -m network

在临时目录中重新下载样本4的数据，确认与离线测试数据一致（发现数据源修订或接口变化）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import load_sample_raw

from market_risk.config import get_fred_api_key, load_settings
from market_risk.data.fetch import fetch_raw_inputs
from market_risk.data.snapshot import build_snapshot
from market_risk.storage.paths import StoragePaths


@pytest.mark.network
def test_live_fetch_matches_fixture(tmp_path):
    base = dt.date(2025, 11, 28)
    raw = fetch_raw_inputs(base, load_settings(), StoragePaths(tmp_path), get_fred_api_key())
    live = build_snapshot(raw)
    offline = build_snapshot(load_sample_raw("2025-11-28"))
    assert live.etfs == offline.etfs
    assert (live.vix, live.vix_t5, live.y, live.h, live.y_t20) == (
        offline.vix, offline.vix_t5, offline.y, offline.h, offline.y_t20
    )
    assert (live.oas_o1, live.oas_o6_v3r1) == (offline.oas_o1, offline.oas_o6_v3r1)
