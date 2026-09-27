"""价格修正：先恢复来源原值进行修订检查，再叠加已批准裁定。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from market_risk.config import DataDecision


def restore_originals(
    rows: Mapping[dt.date, dict[str, Any]], corrections: Sequence[dict[str, Any]],
) -> dict[dt.date, dict[str, Any]]:
    """恢复上次构建保存的修正前整行，禁止拿修正值与接口原值比较。"""
    restored = {day: dict(row) for day, row in rows.items()}
    for item in corrections:
        day = dt.date.fromisoformat(item["date"])
        if day not in restored:
            raise ValueError(f"修正元数据对应日期不存在：{day}")
        restored[day] = dict(item["original_row"])
    return restored


def apply_corrections(
    symbol: str, kind: str, rows: Mapping[dt.date, dict[str, Any]], decisions: Sequence[DataDecision],
) -> tuple[dict[dt.date, dict[str, Any]], list[dict[str, Any]]]:
    """仅修改价格序列的 value/close；开高低量原样保留，证据写入 manifest。"""
    result = {day: dict(row) for day, row in rows.items()}
    audit: list[dict[str, Any]] = []
    seen: set[dt.date] = set()
    for item in decisions:
        if item.symbol != symbol or item.decision != "correct":
            continue
        if item.date in seen:
            raise ValueError(f"重复价格修正：{symbol} {item.date}")
        seen.add(item.date)
        if kind != "etf":
            raise ValueError(f"correct 当前只支持价格序列：{symbol}")
        value = item.corrected_value
        if value is None or not value.is_finite() or value <= 0 or value != value.quantize(Decimal("0.0001")):
            raise ValueError("价格修正值必须为正数，最多4位小数")
        if item.date not in rows:
            raise ValueError(f"修正日期不存在：{symbol} {item.date}")
        original = dict(rows[item.date])
        result[item.date].update(value=float(value), close=float(value), source=f"correct:{original['source']}")
        audit.append({"date": item.date.isoformat(), "original_value": original.get("value"),
                      "corrected_value": str(value), "original_row": original,
                      "evidence_source": item.evidence_source, "reason": item.reason,
                      "decided_on": item.decided_on.isoformat()})
    return result, sorted(audit, key=lambda row: row["date"])
