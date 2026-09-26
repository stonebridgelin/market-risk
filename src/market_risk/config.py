"""读取 config/settings.yaml、config/holidays.yaml 与 .env。

所有配置以不可变 dataclass 返回，调用方显式传递，不使用全局单例。
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 项目根目录：src/market_risk/config.py 往上三级
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_HOLIDAYS_PATH = PROJECT_ROOT / "config" / "holidays.yaml"


class ConfigError(ValueError):
    """配置文件缺失或内容不合法。"""


@dataclass(frozen=True)
class NearThreshold:
    """贴近门槛的判定阈值（SPEC 第7节）。"""

    close_vs_ma_pct: float
    ma5_vs_ma50_pct: float
    breadth_pts: float
    vix_pts: float
    vix_change_pts: float
    oas_bp: int
    yield_bp: int


@dataclass(frozen=True)
class Settings:
    """settings.yaml 的内容。路径均已解析为绝对路径。"""

    scored_symbols: tuple[str, ...]
    reference_symbols: tuple[str, ...]
    moving_averages: tuple[int, ...]
    lookback_calendar_days: int
    d1_includes_t_minus_20: bool
    three_segment_query_offset: int
    oas_series: str
    verify_oas_vintage: bool
    vix_series: str
    cboe_vix_history_url: str
    treasury_fallback_series: str
    storage_root: Path
    max_retries: int
    backoff_seconds: float
    near_threshold: NearThreshold


@dataclass(frozen=True)
class MarketHolidays:
    """holidays.yaml 的内容（人工维护，仅作核对）。"""

    stock_holidays: frozenset[dt.date]
    stock_early_closes: frozenset[dt.date]
    bond_holidays: frozenset[dt.date]
    bond_early_closes: frozenset[dt.date]


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件格式错误（顶层应为映射）：{path}")
    return data


def _resolve(path_str: str, root: Path) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else root / p


def load_settings(path: Path = DEFAULT_SETTINGS_PATH, root: Path = PROJECT_ROOT) -> Settings:
    """读取 settings.yaml。相对路径以 root 为基准解析。"""
    raw = _read_yaml(path)
    try:
        nt = raw["near_threshold"]
        return Settings(
            scored_symbols=tuple(raw["symbols"]["scored"]),
            reference_symbols=tuple(raw["symbols"]["reference"]),
            moving_averages=tuple(int(x) for x in raw["moving_averages"]),
            lookback_calendar_days=int(raw["prices"]["lookback_calendar_days"]),
            d1_includes_t_minus_20=bool(raw["three_segment"]["d1_includes_t_minus_20"]),
            three_segment_query_offset=int(raw["three_segment"]["query_start_offset"]),
            oas_series=str(raw["oas"]["series"]),
            verify_oas_vintage=bool(raw["oas"]["verify_vintage"]),
            vix_series=str(raw["vix"]["series"]),
            cboe_vix_history_url=str(raw["vix"]["cboe_history_url"]),
            treasury_fallback_series=str(raw["treasury"]["fallback_series"]),
            storage_root=_resolve(str(raw["storage"]["root"]), root).resolve(),
            max_retries=int(raw["network"]["max_retries"]),
            backoff_seconds=float(raw["network"]["backoff_seconds"]),
            near_threshold=NearThreshold(
                close_vs_ma_pct=float(nt["close_vs_ma_pct"]),
                ma5_vs_ma50_pct=float(nt["ma5_vs_ma50_pct"]),
                breadth_pts=float(nt["breadth_pts"]),
                vix_pts=float(nt["vix_pts"]),
                vix_change_pts=float(nt["vix_change_pts"]),
                oas_bp=int(nt["oas_bp"]),
                yield_bp=int(nt["yield_bp"]),
            ),
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"settings.yaml 缺少字段或格式错误：{exc}") from exc


def _date_set(values: list[Any] | None, field: str) -> frozenset[dt.date]:
    result: set[dt.date] = set()
    for v in values or []:
        if isinstance(v, dt.date):
            result.add(v)
        else:
            try:
                result.add(dt.date.fromisoformat(str(v)))
            except ValueError as exc:
                raise ConfigError(f"holidays.yaml 的 {field} 含非法日期：{v!r}") from exc
    return frozenset(result)


def load_holidays(path: Path = DEFAULT_HOLIDAYS_PATH) -> MarketHolidays:
    """读取 holidays.yaml。"""
    raw = _read_yaml(path)
    stock = raw.get("stock") or {}
    bond = raw.get("bond") or {}
    return MarketHolidays(
        stock_holidays=_date_set(stock.get("holidays"), "stock.holidays"),
        stock_early_closes=_date_set(stock.get("early_closes"), "stock.early_closes"),
        bond_holidays=_date_set(bond.get("holidays"), "bond.holidays"),
        bond_early_closes=_date_set(bond.get("early_closes"), "bond.early_closes"),
    )


def get_fred_api_key(env_path: Path | None = None) -> str:
    """从 .env 或环境变量读取 FRED_API_KEY；缺失时报错，不使用任何默认值。"""
    load_dotenv(env_path or PROJECT_ROOT / ".env", override=False)
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key or key == "your_fred_api_key_here":
        raise ConfigError(
            "未配置 FRED_API_KEY：请复制 .env.example 为 .env 并填入密钥"
            "（https://fred.stlouisfed.org/docs/api/api_key.html 免费申请）"
        )
    return key
