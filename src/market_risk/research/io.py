"""研究输入：从已指定正式回测与市场 CSV 读取，保留期行不进入内存。"""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from market_risk.precision import published_price
from market_risk.research.pullback import DEVELOPMENT_END, END, START, Episode
from market_risk.storage.paths import StoragePaths

MARKET_SYMBOLS = ("SPX", "SPY", "QQQ", "RSP", "HYG", "LQD", "UST10Y", "BAMLH0A0HYM2",
                  "VIXCLS", "VIX_CBOE", "S5FI", "S5TW")
TV_SYMBOLS = ("ADD", "HIGN", "LOWN", "MMFI", "MMTW", "R2FI", "R2TW", "PCCE", "NDTW", "VIX3M")


def _rows(path: Path, date_field: str = "date", end: dt.date = END) -> Iterator[dict[str, str]]:
    """日序列按日期非递减；遇到首个截止日之后的行立即停止。"""
    previous: dt.date | None = None
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            day = dt.date.fromisoformat(row[date_field])
            if day > end:
                break
            if previous is not None and day < previous:
                raise ValueError(f"截止日前研究序列日期乱序：{path}")
            previous = day
            yield row


# 第一类标签文件（本不应含保留期行）的截止日：验证期最后一个交易日（负责人 2026-10-01 规定）。
LABEL_END = dt.date(2022, 12, 30)


def cutoff_violation(end: dt.date) -> str:
    """越界报错的说法随截止日区分：开发期截止日之后未必是保留期。"""
    if end <= DEVELOPMENT_END:
        return "越过开发期截止日"
    if end <= END:
        return "含保留期日期"
    raise ValueError(f"研究读取的截止日不得晚于 {END.isoformat()}：{end.isoformat()}")


def label_rows(path: Path, date_fields: tuple[str, ...],
               end: dt.date = LABEL_END) -> Iterator[dict[str, str]]:
    """分组标签不假设全局排序；所有日期列逐行不得越过截止日，越界即报错，不跳过。"""
    violation = cutoff_violation(end)          # 截止日晚于验证期末时，打开文件前即拒绝
    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            for field in date_fields:
                value = row.get(field, "")
                if value and dt.date.fromisoformat(value) > end:
                    raise ValueError(f"标签文件{violation}：{path.name} / {field} / {value}"
                                     f"（截止日 {end.isoformat()}）")
            yield row


def _values(path: Path, field: str = "value", price: bool = False,
            end: dt.date = END) -> dict[dt.date, Decimal]:
    result = {}
    for row in _rows(path, end=end):
        value = row.get(field, "")
        if value:
            day = dt.date.fromisoformat(row["date"])
            result[day] = published_price(value) if price else Decimal(value)
    return result


@dataclass(frozen=True)
class ResearchInputs:
    run_id: str
    scores: dict[tuple[dt.date, str], dict[str, str]]
    metrics: dict[dt.date, dict[str, str]]
    episodes: tuple[Episode, ...]
    bear_spans: tuple[tuple[dt.date, dt.date], ...]
    excluded_episodes: tuple[dict[str, str], ...]
    episode_window_rows: int
    market: dict[str, dict[dt.date, Decimal]]
    tradingview: dict[str, dict[dt.date, Decimal]]
    days: tuple[dt.date, ...]


def load_development_feature_inputs(paths: StoragePaths, expected_run_id: str) -> ResearchInputs:
    """只读开发期评分、指标和序列，供补充审计；不读取验证期行。"""
    pointer = json.loads(paths.backtest_official.read_text(encoding="utf-8"))
    if pointer["run_id"] != expected_run_id:
        raise ValueError(f"正式回测指针为 {pointer['run_id']}，预期 {expected_run_id}")
    run = paths.backtest_run_dir(expected_run_id)
    scores = {(dt.date.fromisoformat(row["date"]), row["version"]): row
              for row in _rows(run / "daily_scores.csv", end=DEVELOPMENT_END)}
    metrics = {dt.date.fromisoformat(row["date"]): row
               for row in _rows(run / "daily_metrics.csv", end=DEVELOPMENT_END)}
    price_symbols = {"SPX", "SPY", "QQQ", "RSP", "HYG", "LQD"}
    market = {symbol: _values(paths.market_daily_file(symbol), price=symbol in price_symbols,
                              end=DEVELOPMENT_END) for symbol in MARKET_SYMBOLS}
    tradingview = {symbol: _values(paths.tv_processed_file(symbol), "close", end=DEVELOPMENT_END)
                   for symbol in TV_SYMBOLS}
    days = tuple(sorted({day for day, version in scores if version == "v2-M"}))
    return ResearchInputs(expected_run_id, scores, metrics, (), (), (), 0, market, tradingview, days)


def load_inputs(paths: StoragePaths, expected_run_id: str) -> ResearchInputs:
    """正式指针必须精确匹配规格指定运行；所有研究数据截至验证期。"""
    pointer = json.loads(paths.backtest_official.read_text(encoding="utf-8"))
    if pointer["run_id"] != expected_run_id:
        raise ValueError(f"正式回测指针为 {pointer['run_id']}，预期 {expected_run_id}")
    run = paths.backtest_run_dir(expected_run_id)
    scores = {(dt.date.fromisoformat(row["date"]), row["version"]): row
              for row in _rows(run / "daily_scores.csv")}
    metrics = {dt.date.fromisoformat(row["date"]): row for row in _rows(run / "daily_metrics.csv")}
    window_count = 0
    for row in label_rows(run / "episode_windows.csv", ("high_date", "date")):
        day = dt.date.fromisoformat(row["date"])
        for version, prefix in (("v2-M", "v2m"), ("v3-R1", "v3r1")):
            score = scores.get((day, version))
            if score and (row[f"{prefix}_total_min"], row[f"{prefix}_total_max"]) != (
                    score["total_min"], score["total_max"]):
                raise ValueError(f"episode_windows 与 daily_scores 在 {day} / {version} 不一致")
        window_count += 1
    episodes = []
    bear_spans = []
    excluded_episodes = []
    for row in label_rows(run / "pullback_episodes.csv",
                           ("high_date", "low_date", "confirm_date", "recovery_date")):
        if row["symbol"] == "SPX" and row["level"] == "20":
            if row["low_date"] and row["status"] == "已确认":
                low = dt.date.fromisoformat(row["low_date"])
                if low >= dt.date(2008, 8, 11):
                    bear_spans.append((dt.date.fromisoformat(row["high_date"]), min(low, END)))
            elif row["high_date"] == "2022-01-03" and row["status"] == "跨入保留期，未解锁":
                bear_spans.append((dt.date(2022, 1, 3), dt.date(2022, 12, 30)))
        if (row["symbol"], row["level"]) in (("SPX", "5"), ("QQQ", "7")):
            high = dt.date.fromisoformat(row["high_date"])
            low = dt.date.fromisoformat(row["low_date"]) if row["low_date"] else None
            crossed = ((high < START <= low) if low else False) or (
                (high <= DEVELOPMENT_END < low) if low else False) or row["status"] == "跨入保留期，未解锁"
            if crossed and row["counted"] != "是":
                excluded_episodes.append({"symbol": row["symbol"], "level": row["level"],
                                          "high_date": row["high_date"], "low_date": row["low_date"],
                                          "status": row["status"], "reason": "跨区间边界，未计入"})
        if row["status"] != "已确认" or row["counted"] != "是":
            continue
        if (row["symbol"], row["level"]) not in (("SPX", "5"), ("QQQ", "7")):
            continue
        low = dt.date.fromisoformat(row["low_date"])
        if low > END:
            continue
        episodes.append(Episode(row["symbol"], dt.date.fromisoformat(row["high_date"]), low,
                                Decimal(row["high_close"])))
    price_symbols = {"SPX", "SPY", "QQQ", "RSP", "HYG", "LQD"}
    market = {symbol: _values(paths.market_daily_file(symbol), price=symbol in price_symbols)
              for symbol in MARKET_SYMBOLS}
    tradingview = {symbol: _values(paths.tv_processed_file(symbol), "close") for symbol in TV_SYMBOLS}
    days = tuple(sorted({d for d, version in scores if version == "v2-M"}))
    return ResearchInputs(expected_run_id, scores, metrics, tuple(episodes), tuple(bear_spans),
                          tuple(excluded_episodes), window_count, market, tradingview, days)


def series_at(series: Mapping[dt.date, Decimal], day: dt.date) -> Decimal | None:
    """只按精确日期取值，不用邻近日期填补。"""
    return series.get(day)
