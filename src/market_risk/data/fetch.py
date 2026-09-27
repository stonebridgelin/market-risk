"""按基准日联网获取全部原始数据，组装为 RawInputs（交给 snapshot.build_snapshot 截断与计算）。"""

from __future__ import annotations

import datetime as dt
import logging

from market_risk.config import Settings
from market_risk.data import cboe, fred, prices, treasury
from market_risk.data.breadth import read_breadth
from market_risk.data.cache import DataFetchError
from market_risk.data.snapshot import RawInputs
from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths

logger = logging.getLogger(__name__)

# 逐日数据回看的自然日数：覆盖 T−45（约65个自然日）、T−20、O6
DAILY_LOOKBACK_DAYS = 100
VINTAGE_LOOKBACK_DAYS = 30


def fetch_raw_inputs(
    base_date: dt.date,
    settings: Settings,
    paths: StoragePaths,
    api_key: str,
    mode: str = "backtest",
    refresh: bool = False,
) -> RawInputs:  # pragma: no cover - 网络请求（离线测试见 fixtures）
    retry = {"max_retries": settings.max_retries, "backoff_seconds": settings.backoff_seconds}
    sources: list[SourceInfo] = []
    notes: list[str] = []
    start = base_date - dt.timedelta(days=DAILY_LOOKBACK_DAYS)

    closes = {}
    for symbol in (*settings.scored_symbols, *settings.reference_symbols):
        series, info = prices.fetch_closes(
            paths, symbol, base_date, settings.lookback_calendar_days, refresh, **retry
        )
        closes[symbol] = series
        sources.append(info)

    vix_fred, info = fred.fetch_series(
        paths, settings.vix_series, start, base_date, api_key, refresh=refresh, **retry
    )
    sources.append(info)
    vix_cboe = None
    try:
        vix_cboe, info = cboe.fetch_vix_history(
            paths, start, base_date, settings.cboe_vix_history_url, refresh, **retry
        )
        sources.append(info)
    except DataFetchError as exc:
        notes.append(f"Cboe VIX 备用源获取失败（{exc}），只使用 FRED VIXCLS")

    ten_year, infos, t_notes = treasury.fetch_ten_year(
        paths, start, base_date, api_key, refresh, **retry
    )
    sources.extend(infos)
    notes.extend(t_notes)

    try:
        oas, info = fred.fetch_series(
            paths, settings.oas_series, start, base_date, api_key, refresh=refresh, **retry
        )
    except DataFetchError as exc:
        raise DataFetchError(
            f"{exc}（若基准日早于三年前：自2026年4月起 FRED 只提供 ICE 系列最近三年的观测）"
        ) from exc
    sources.append(info)
    ice_note = fred.ice_history_note(settings.oas_series, start, oas)
    if ice_note:
        notes.append(ice_note)

    oas_vintage = None
    if settings.oas_revision_check:
        try:
            oas_vintage, info = fred.fetch_series(
                paths,
                settings.oas_series,
                base_date - dt.timedelta(days=VINTAGE_LOOKBACK_DAYS),
                base_date,
                api_key,
                realtime=base_date,
                refresh=refresh,
                **retry,
            )
            sources.append(info)
        except DataFetchError as exc:
            notes.append(
                f"ALFRED 基准日版本无法取得（{exc}），不做历史修订比对；不影响计分"
            )

    return RawInputs(
        base_date=base_date,
        closes=closes,
        vix_fred=vix_fred,
        vix_cboe=vix_cboe,
        treasury=ten_year,
        treasury_coverage_start=start,
        oas=oas,
        oas_vintage=oas_vintage,
        breadth=read_breadth(paths.breadth_csv),
        sources=tuple(sources),
        notes=tuple(notes),
        mode=mode,
    )
