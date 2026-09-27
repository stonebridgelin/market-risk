"""按基准日联网获取全部原始数据，组装为 RawInputs（交给 snapshot.build_snapshot 截断与计算）。"""

from __future__ import annotations

import datetime as dt
import logging

from market_risk.config import Settings
from market_risk.data import cboe, fred, prices, treasury
from market_risk.data.breadth import load_breadth
from market_risk.data.cache import DataFetchError
from market_risk.data.snapshot import RawInputs
from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths

logger = logging.getLogger(__name__)

# 逐日数据回看的自然日数：覆盖 T−45（约65个自然日）、T−20、O6
DAILY_LOOKBACK_DAYS = 100
VINTAGE_LOOKBACK_DAYS = DAILY_LOOKBACK_DAYS  # 与当前版本同一区间，保证覆盖两个口径的 O6


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

    long_history = settings.oas_long_history_source == "tradingview"
    try:
        oas, info = fred.fetch_series(
            paths, settings.oas_series, start, base_date, api_key, refresh=refresh, **retry
        )
        sources.append(info)
    except DataFetchError as exc:
        if not long_history:
            raise DataFetchError(
                f"{exc}（若基准日早于三年前：自2026年4月起 FRED 只提供 ICE 系列最近三年的观测）"
            ) from exc
        oas = {}
        notes.append(f"FRED 未返回 {settings.oas_series}（{exc}）")
    fred_has_data = bool(oas)
    ice_note = fred.ice_history_note(settings.oas_series, start, oas)
    if ice_note:
        notes.append(ice_note)
    if long_history:
        # TRADINGVIEW 6.2：只有 FRED API 取不到的日期才用 TradingView 导出数据
        from market_risk.data.tradingview import read_processed

        oas, fill_notes = fred.fill_long_history(
            oas, read_processed(paths, settings.oas_series), start, base_date
        )
        notes.extend(fill_notes)

    oas_vintage = None
    if settings.oas_revision_check and not fred_has_data:
        notes.append("FRED API 没有该区间的 OAS（早于三年），不做历史修订比对；不影响计分")
    elif settings.oas_revision_check:
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

    # SPEC 6.5：广度先读 TradingView 导出数据，再读手工录入；只报告基准日附近的差异
    breadth, conflicts = load_breadth(paths)
    for d, msg in sorted(conflicts.items()):
        if base_date - dt.timedelta(days=15) <= d <= base_date:
            notes.append(msg)

    return RawInputs(
        base_date=base_date,
        closes=closes,
        vix_fred=vix_fred,
        vix_cboe=vix_cboe,
        treasury=ten_year,
        treasury_coverage_start=start,
        oas=oas,
        oas_vintage=oas_vintage,
        breadth=breadth,
        sources=tuple(sources),
        notes=tuple(notes),
        mode=mode,
    )
