"""联网测试（默认跳过）：uv run pytest -m network

在临时目录中重新生成数据集（data build 按需下载），用样本4的基准日组装输入，
确认与离线测试数据一致（发现数据源修订或接口变化）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import load_sample_raw

from market_risk import services
from market_risk.config import load_settings
from market_risk.data.market import load_raw_inputs
from market_risk.data.snapshot import build_snapshot
from market_risk.storage import db
from market_risk.storage.paths import StoragePaths


@pytest.mark.network
def test_live_dataset_matches_fixture(tmp_path):
    base = dt.date(2025, 11, 28)
    settings = load_settings()
    paths = StoragePaths(tmp_path)
    ctx = services.Context(settings, paths, db.sqlite_url(tmp_path / "db" / "m.sqlite"))
    services.data_build(ctx, end=dt.date(2025, 12, 31))
    live = build_snapshot(load_raw_inputs(paths, settings, base, revision_check=False))
    offline = build_snapshot(load_sample_raw("2025-11-28"))
    assert live.etfs == offline.etfs
    assert (live.vix, live.vix_t5, live.y, live.h, live.y_t20) == (
        offline.vix, offline.vix_t5, offline.y, offline.h, offline.y_t20
    )
    assert (live.oas_o1, live.oas_o6_v3r1) == (offline.oas_o1, offline.oas_o6_v3r1)
