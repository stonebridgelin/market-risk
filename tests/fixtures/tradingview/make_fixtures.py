"""生成 TradingView 测试样本（手工构造，非真实导出）。运行：uv run python tests/fixtures/tradingview/make_fixtures.py

- iso/INDEX_S5FI, 1D.csv：ISO 时间（美东 09:30，带 -04:00/-05:00 偏移），含一个指标列 MA
- unix/INDEX_S5TW, 1D.csv：UNIX 时间戳（美东 09:30 对应的秒数）
收盘值在 known_values 的日期上与 config/symbols.yaml 一致，其余为构造值。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from market_risk import calendar as mcal

NY = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
DAYS = mcal.stock_trading_days(dt.date(2025, 10, 20), dt.date(2025, 11, 28))

S5FI_KNOWN = {dt.date(2025, 10, 24): 52.88, dt.date(2025, 10, 31): 40.15,
              dt.date(2025, 11, 28): 58.44}
S5TW_KNOWN = {dt.date(2025, 10, 24): 57.65, dt.date(2025, 10, 31): 38.56,
              dt.date(2025, 11, 28): 76.73}


def closes(known: dict[dt.date, float], base: float) -> dict[dt.date, float]:
    return {d: known.get(d, round(base + (i * 7 % 11) * 1.13, 2)) for i, d in enumerate(DAYS)}


def main() -> None:
    (HERE / "iso").mkdir(exist_ok=True)
    (HERE / "unix").mkdir(exist_ok=True)
    fi = closes(S5FI_KNOWN, 45.0)
    lines = ["time,open,high,low,close,MA"]
    for d, c in fi.items():
        t = dt.datetime.combine(d, dt.time(9, 30), NY).isoformat()
        lines.append(f"{t},{c - 0.5:.2f},{c + 1:.2f},{c - 1:.2f},{c:.2f},{c - 0.3:.2f}")
    (HERE / "iso" / "INDEX_S5FI, 1D.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    tw = closes(S5TW_KNOWN, 50.0)
    lines = ["time,open,high,low,close"]
    for d, c in tw.items():
        ts = int(dt.datetime.combine(d, dt.time(9, 30), NY).timestamp())
        lines.append(f"{ts},{c - 0.5:.2f},{c + 1:.2f},{c - 1:.2f},{c:.2f}")
    (HERE / "unix" / "INDEX_S5TW, 1D.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
