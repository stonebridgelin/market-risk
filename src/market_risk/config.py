"""读取 config/settings.yaml、config/holidays.yaml 与 .env。

所有配置以不可变 dataclass 返回，调用方显式传递，不使用全局单例。
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
    three_segment_query_offset: int
    oas_series: str
    oas_revision_check: bool
    oas_long_history_source: str
    vix_series: str
    cboe_vix_history_url: str
    treasury_fallback_series: str
    storage_root: Path
    database_url: str
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


def _long_history(value: Any) -> str:
    v = str(value).lower()
    if v not in {"none", "tradingview"}:
        raise ConfigError(f"oas.long_history_source 只能是 none 或 tradingview：{value!r}")
    return v


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
            three_segment_query_offset=int(raw["three_segment"]["query_start_offset"]),
            oas_series=str(raw["oas"]["series"]),
            oas_revision_check=bool(raw["oas"]["revision_check"]),
            oas_long_history_source=_long_history(raw["oas"].get("long_history_source", "none")),
            vix_series=str(raw["vix"]["series"]),
            cboe_vix_history_url=str(raw["vix"]["cboe_history_url"]),
            treasury_fallback_series=str(raw["treasury"]["fallback_series"]),
            storage_root=_resolve(str(raw["storage"]["root"]), root).resolve(),
            database_url=str((raw.get("database") or {}).get("url", "sqlite:///db/market_risk.sqlite")),
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


DEFAULT_SYMBOLS_PATH = PROJECT_ROOT / "config" / "symbols.yaml"
_USAGES = {"scoring", "crosscheck", "reference"}
_UNITS = {"percent", "index", "price", "ratio", "count", "net"}
_CALENDARS = {"nyse", "bond", "none"}


@dataclass(frozen=True)
class SymbolInfo:
    """config/symbols.yaml 中的一个标的（docs/TRADINGVIEW.md 第4节）。"""

    symbol: str
    tv_symbol: str
    name: str = ""
    category: str = "other"
    usage: str = "reference"
    unit: str | None = None
    timezone: str = "America/New_York"
    calendar: str = "nyse"
    inception: dt.date | None = None
    filename_aliases: tuple[str, ...] = ()
    known_values: dict[dt.date, float] | None = None
    api_source: str | None = None      # 交叉校验用的接口数据，如 yahoo:SPY、fred:BAMLH0A0HYM2
    tolerance: float = 0.005           # 交叉校验容差
    crosscheck_note: str | None = None  # 口径不同、交叉校验"不适用"的原因


DEFAULT_DECISIONS_PATH = PROJECT_ROOT / "config" / "data_decisions.yaml"
_DECISIONS = {"exclude", "keep", "invalid", "correct"}


@dataclass(frozen=True)
class DataDecision:
    """已裁定日期表中的一条（config/data_decisions.yaml）。"""

    date: dt.date
    symbol: str
    decision: str          # exclude / keep / invalid / correct（人工价格修正）
    reason: str
    decided_on: dt.date
    corrected_value: Decimal | None = None
    evidence_source: str | None = None

    def __post_init__(self) -> None:
        if self.decision == "correct":
            value = self.corrected_value
            if value is None or not value.is_finite():
                raise ConfigError("correct 必须提供有限数值 corrected_value")
            if (not isinstance(self.evidence_source, str) or not self.evidence_source.strip()
                    or not self.reason.strip()):
                raise ConfigError("correct 必须提供 evidence_source 和 reason")
            if value <= 0 or value >= Decimal('1000000000000') or value != value.quantize(Decimal('0.0001')):
                raise ConfigError("价格修正值必须为正数、最多4位小数，且能存入 Numeric(20, 8)")
        elif self.corrected_value is not None or self.evidence_source is not None:
            raise ConfigError("只有 correct 可以包含 corrected_value 和 evidence_source")


def load_data_decisions(path: Path = DEFAULT_DECISIONS_PATH) -> tuple[DataDecision, ...]:
    """读取已裁定日期表；文件不存在时返回空。同一日期与标的只能裁定一次。"""
    if not path.exists():
        return ()
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    if not isinstance(raw, list):
        raise ConfigError(f"{path} 顶层应为列表")
    out: list[DataDecision] = []
    seen: set[tuple[dt.date, str]] = set()
    for i, item in enumerate(raw, start=1):
        try:
            d = DataDecision(_one_date(item["date"], "date"), str(item["symbol"]), str(item["decision"]),
                             str(item.get("reason", "")), _one_date(item["decided_on"], "decided_on"),
                             Decimal(str(item["corrected_value"])) if "corrected_value" in item else None,
                             item.get("evidence_source"))
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ConfigError(f"{path} 第{i}条格式错误：{exc}") from exc
        if d.decision not in _DECISIONS:
            raise ConfigError(f"{path} 第{i}条：decision 应为 {sorted(_DECISIONS)}")
        if (d.date, d.symbol) in seen:
            raise ConfigError(f"{path} 中 {d.symbol} {d.date} 重复裁定")
        seen.add((d.date, d.symbol))
        out.append(d)
    return tuple(out)


def _one_date(value: Any, field: str) -> dt.date:
    return next(iter(_date_set([value], field)))


def load_symbols(path: Path = DEFAULT_SYMBOLS_PATH) -> dict[str, SymbolInfo]:
    """读取标的登记表，返回 {tv_symbol: SymbolInfo}。"""
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    if not isinstance(raw, list):
        raise ConfigError(f"{path} 顶层应为列表")
    result: dict[str, SymbolInfo] = {}
    for i, item in enumerate(raw, start=1):
        try:
            info = SymbolInfo(
                symbol=str(item["symbol"]),
                tv_symbol=str(item["tv_symbol"]).upper(),
                name=str(item.get("name", "")),
                category=str(item.get("category", "other")),
                usage=str(item.get("usage", "reference")),
                unit=item.get("unit"),
                timezone=str(item.get("timezone", "America/New_York")),
                calendar=str(item.get("calendar", "nyse")),
                inception=(
                    _one_date(item["inception"], "inception") if item.get("inception") else None
                ),
                filename_aliases=tuple(str(a) for a in item.get("filename_aliases") or ()),
                api_source=item.get("api_source"),
                tolerance=float(item.get("tolerance", 0.005)),
                crosscheck_note=item.get("crosscheck_note"),
                known_values={
                    _one_date(d, "known_values"): float(v)
                    for d, v in (item.get("known_values") or {}).items()
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"{path} 第{i}个标的格式错误：{exc}") from exc
        if info.usage not in _USAGES:
            raise ConfigError(f"{info.symbol}：usage 应为 {sorted(_USAGES)}")
        if info.unit is not None and info.unit not in _UNITS:
            raise ConfigError(f"{info.symbol}：unit 应为 {sorted(_UNITS)}")
        if info.calendar not in _CALENDARS:
            raise ConfigError(f"{info.symbol}：calendar 应为 {sorted(_CALENDARS)}")
        if info.tv_symbol in result:
            raise ConfigError(f"{path} 中 {info.tv_symbol} 重复登记")
        result[info.tv_symbol] = info
    return result


def get_optional_secret(name: str, placeholder: str = "", env_path: Path | None = None) -> str | None:
    """从 .env 或环境变量读取可选的密钥或连接地址；未设置（或仍为占位符）时返回 None。不输出其内容。"""
    load_dotenv(env_path or PROJECT_ROOT / ".env", override=False)
    value = os.environ.get(name, "").strip()
    return None if not value or value == placeholder else value


def get_tiingo_api_key(env_path: Path | None = None) -> str | None:
    """TIINGO_API_KEY（可选）：只用于 tv crosscheck 的第三方收盘价核对。"""
    return get_optional_secret("TIINGO_API_KEY", "your_tiingo_api_key_here", env_path)


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
