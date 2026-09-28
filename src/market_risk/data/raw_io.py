"""RawInputs 的保存与读取（用于离线测试数据，以及运行目录 inputs/ 的复现）。

目录内容：
  closes_<SYMBOL>.csv、vix_fred.csv、vix_cboe.csv（可无）、treasury_10y.csv、
  oas.csv、oas_vintage.csv（可无）、meta.json（基准日、模式、覆盖起点、来源、说明）
广度为手工数据，不随原始数据保存。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from market_risk.config import DataDecision
from market_risk.data.cache import series_from_csv, series_to_csv
from market_risk.data.snapshot import RawInputs
from market_risk.models import BreadthReading, SourceInfo


def _write(path: Path, series: dict | None) -> None:
    if series is not None:
        path.write_text(series_to_csv(dict(series)), encoding="utf-8")


def _read(path: Path) -> dict | None:
    return series_from_csv(path.read_text(encoding="utf-8")) if path.exists() else None


def save_raw_inputs(raw: RawInputs, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for symbol, series in raw.closes.items():
        _write(directory / f"closes_{symbol}.csv", dict(series))
    _write(directory / "vix_fred.csv", dict(raw.vix_fred))
    _write(directory / "vix_cboe.csv", None if raw.vix_cboe is None else dict(raw.vix_cboe))
    _write(directory / "treasury_10y.csv", dict(raw.treasury))
    _write(directory / "oas.csv", dict(raw.oas))
    vintage = None if raw.oas_vintage is None else dict(raw.oas_vintage)
    _write(directory / "oas_vintage.csv", vintage)

    def _src(s: SourceInfo) -> dict:
        d = dataclasses.asdict(s)
        for k in ("data_start", "data_end"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    meta = {
        "base_date": raw.base_date.isoformat(),
        "mode": raw.mode,
        "treasury_coverage_start": raw.treasury_coverage_start.isoformat(),
        "symbols": sorted(raw.closes),
        "sources": [_src(s) for s in raw.sources],
        "notes": list(raw.notes),
        "oas_symbol": raw.oas_symbol,
        "oas_sources": {d.isoformat(): source for d, source in raw.oas_sources.items()},
        "treasury_sources": {d.isoformat(): source for d, source in raw.treasury_sources.items()},
        "decisions": [
            {**dataclasses.asdict(d), "date": d.date.isoformat(), "decided_on": d.decided_on.isoformat(),
             "corrected_value": str(d.corrected_value) if d.corrected_value is not None else None}
            for d in raw.decisions
        ],
    }
    (directory / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_raw_inputs(
    directory: Path, breadth: dict[dt.date, BreadthReading] | None = None
) -> RawInputs:
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))

    def _d(v: str | None) -> dt.date | None:
        return dt.date.fromisoformat(v) if v else None

    sources = tuple(
        SourceInfo(**{**s, "data_start": _d(s["data_start"]), "data_end": _d(s["data_end"])})
        for s in meta["sources"]
    )
    closes = {}
    for symbol in meta["symbols"]:
        series = _read(directory / f"closes_{symbol}.csv")
        if series is None:
            raise FileNotFoundError(directory / f"closes_{symbol}.csv")
        closes[symbol] = series
    return RawInputs(
        base_date=dt.date.fromisoformat(meta["base_date"]),
        closes=closes,
        vix_fred=_read(directory / "vix_fred.csv") or {},
        vix_cboe=_read(directory / "vix_cboe.csv"),
        treasury=_read(directory / "treasury_10y.csv") or {},
        treasury_coverage_start=dt.date.fromisoformat(meta["treasury_coverage_start"]),
        oas=_read(directory / "oas.csv") or {},
        oas_vintage=_read(directory / "oas_vintage.csv"),
        breadth=breadth or {},
        sources=sources,
        notes=tuple(meta["notes"]),
        mode=meta["mode"],
        decisions=tuple(
            DataDecision(dt.date.fromisoformat(d["date"]), d["symbol"], d["decision"], d["reason"],
                         dt.date.fromisoformat(d["decided_on"]),
                         Decimal(d["corrected_value"]) if d.get("corrected_value") is not None else None,
                         d.get("evidence_source"))
            for d in meta.get("decisions", [])
        ),
        oas_symbol=meta.get("oas_symbol", "BAMLH0A0HYM2"),
        oas_sources={dt.date.fromisoformat(d): source for d, source in meta.get("oas_sources", {}).items()},
        treasury_sources={dt.date.fromisoformat(d): source
                          for d, source in meta.get("treasury_sources", {}).items()},
    )
