"""前瞻特征逐日依赖与缺值原因审计；只供研究层调用。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from market_risk.research.features import VIX3M_START, FeatureEngine

SINGLE_VALUE_END = {"S5FI": dt.date(2010, 8, 31), "S5TW": dt.date(2010, 10, 1),
                    "NDTW": dt.date(2010, 10, 1)}
REASON_ORDER = ("序列未开始", "窗口不足", "缺口", "单值阶段")


def _numeric(text: str | None) -> bool:
    if not text:
        return False
    try:
        Decimal(text)
    except InvalidOperation:
        return False
    return True


@dataclass(frozen=True)
class FeatureQuality:
    missing: bool
    primary_reason: str | None
    all_reasons: tuple[str, ...]
    single_value_stage: bool


def single_value_stage(engine: FeatureEngine, feature: str, day: dt.date) -> bool:
    """所用最早日期落在单值 K 线阶段时，作质量标记。"""
    for symbol, cutoff in SINGLE_VALUE_END.items():
        if not feature.startswith(symbol):
            continue
        lag = 59 if feature == "S5TW_below_60d_high" else 20 if feature.endswith("_change_20") else 10 \
            if feature.endswith("_change_10") else 5 if feature.endswith("_change_5") else 0
        earliest = engine.prior(day, lag) if lag else day
        return earliest is None or earliest <= cutoff
    return False


def _window(engine: FeatureEngine, day: dt.date, n: int) -> tuple[dt.date, ...]:
    return engine.window(day, n)


def _dates_for_feature(engine: FeatureEngine, feature: str, day: dt.date
                       ) -> tuple[list[tuple[str, dt.date]], bool]:
    """返回基础序列依赖及交易日窗口是否完整；复合特征逐项展开。"""
    refs: list[tuple[str, dt.date]] = []
    short = False

    def add_lag(symbols: Sequence[str], lag: int) -> None:
        nonlocal short
        past = engine.prior(day, lag)
        if past is None:
            short = True
        else:
            refs.extend((symbol, past) for symbol in symbols)

    def add_window(symbols: Sequence[str], n: int) -> None:
        nonlocal short
        window = _window(engine, day, n)
        if len(window) != n:
            short = True
        else:
            refs.extend((symbol, date) for date in window for symbol in symbols)

    if feature in ("S5FI", "S5TW", "MMFI", "MMTW", "R2FI", "R2TW", "NDTW", "PCCE"):
        refs.append((feature, day))
    elif "_change_" in feature and feature.split("_change_")[0] in (
            "S5FI", "S5TW", "NDTW", "MMFI", "MMTW", "R2FI", "R2TW"):
        symbol, lag = feature.split("_change_")
        refs.append((symbol, day))
        add_lag((symbol,), int(lag))
    elif feature == "S5TW_below_60d_high":
        add_window(("S5TW",), 60)
    elif feature == "RSP_SPY_ratio_vs_ma20_pct":
        add_window(("RSP", "SPY"), 20)
    elif feature == "ADD_sum_10":
        add_window(("ADD",), 10)
    elif feature in ("HIGN_minus_LOWN", "HIGN_minus_LOWN_change_10"):
        refs.extend((("HIGN", day), ("LOWN", day)))
        if feature.endswith("_change_10"):
            add_lag(("HIGN", "LOWN"), 10)
    elif feature.startswith(("SPY_close_vs_ma", "QQQ_close_vs_ma")):
        symbol = feature[:3]
        n = int(feature.split("ma")[-1].split("_")[0])
        add_window((symbol,), n)
    elif feature in ("SPY_ma5_vs_ma20_pct", "QQQ_ma5_vs_ma20_pct"):
        add_window((feature[:3],), 20)
    elif feature == "VIX_change_5":
        refs.append(("@metric:VIX", day))
        past = engine.prior(day, 5)
        if past is None:
            short = True
        elif past in engine.inputs.metrics:
            refs.append(("@metric:VIX", past))
        else:
            refs.append(("@vix_fallback", past))
    elif feature == "VIX":
        refs.append(("@metric:VIX", day))
    elif feature == "VIX_VIX3M":
        refs.extend((("@metric:VIX", day), ("VIX3M", day)))
    elif feature == "PCCE_ma10":
        add_window(("PCCE",), 10)
    elif feature == "OAS_O1":
        refs.append(("@metric:oas_o1_v3r1", day))
    elif feature in ("OAS_change_5", "OAS_change_20"):
        text = engine.inputs.metrics.get(day, {}).get("oas_o1_date_v3r1", "")
        if not text:
            refs.append(("@metric:oas_o1_date_v3r1", day))
        else:
            o1 = dt.date.fromisoformat(text)
            index = engine.bond_index.get(o1)
            if index is None or index < int(feature.split("_")[-1]):
                short = True
            else:
                old = engine.bond_days[index - int(feature.split("_")[-1])]
                refs.extend((("BAMLH0A0HYM2", o1), ("BAMLH0A0HYM2", old)))
    elif feature == "HYG_LQD_change_20":
        refs.extend((("HYG", day), ("LQD", day)))
        add_lag(("HYG", "LQD"), 20)
    elif feature == "UST10Y_change_20":
        refs.append(("@metric:dy_bp", day))
        add_lag(("UST10Y",), 20)
    elif feature.startswith(("v2m_", "v3r1_")):
        version = "v2-M" if feature.startswith("v2m_") else "v3-R1"
        refs.append((f"@score:{version}:{feature.split('_', 1)[1]}", day))
    elif feature in ("SPX_drawdown_63", "QQQ_drawdown_63"):
        add_window((feature[:3],), 63)
    else:
        raise ValueError(f"未知研究特征，不能解释缺值：{feature}")
    return refs, short


def feature_quality(engine: FeatureEngine, feature: str, day: dt.date,
                    value: Decimal | None) -> FeatureQuality:
    refs, short = _dates_for_feature(engine, feature, day)
    stage = single_value_stage(engine, feature, day)
    reasons: set[str] = set()
    if short:
        reasons.add("窗口不足")
    for symbol, needed in refs:
        if symbol.startswith("@metric:"):
            field = symbol.split(":", 1)[1]
            if not _numeric(engine.inputs.metrics.get(needed, {}).get(field)) and not field.endswith("_date_v3r1"):
                reasons.add("缺口")
            elif field.endswith("_date_v3r1") and not engine.inputs.metrics.get(needed, {}).get(field):
                reasons.add("缺口")
        elif symbol.startswith("@score:"):
            _, version, field = symbol.split(":", 2)
            if not _numeric(engine.inputs.scores.get((needed, version), {}).get(field)):
                reasons.add("缺口")
        elif symbol == "@vix_fallback":
            if engine.value("VIXCLS", needed) is None and engine.value("VIX_CBOE", needed) is None:
                reasons.add("缺口")
        else:
            series = engine.inputs.market.get(symbol) or engine.inputs.tradingview.get(symbol) or {}
            start = VIX3M_START if symbol == "VIX3M" else min(series) if series else None
            if start is None or needed < start:
                reasons.add("序列未开始")
            elif needed not in series:
                reasons.add("缺口")
    if stage:
        reasons.add("单值阶段")
    missing = value is None
    if missing and not reasons:
        raise ValueError(f"无法解释缺值：{feature} / {day}")
    primary = next((reason for reason in REASON_ORDER if reason in reasons), None) if missing else None
    return FeatureQuality(missing, primary, tuple(reason for reason in REASON_ORDER if reason in reasons), stage)
