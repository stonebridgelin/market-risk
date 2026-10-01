"""只使用基准日及此前观测值的预登记研究特征。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from market_risk import calendar as market_calendar
from market_risk.research.io import ResearchInputs

VIX3M_START = dt.date(2009, 9, 18)


def _dec(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _difference(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return a - b if a is not None and b is not None else None


def _ratio(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return a / b if a is not None and b not in (None, Decimal(0)) else None


@dataclass(frozen=True)
class FeatureEngine:
    """交易日位移取 NYSE 日历，缺少任一所需端点即留空。"""

    inputs: ResearchInputs
    days: tuple[dt.date, ...]
    index: Mapping[dt.date, int]
    bond_days: tuple[dt.date, ...]
    bond_index: Mapping[dt.date, int]

    @classmethod
    def create(cls, inputs: ResearchInputs) -> FeatureEngine:
        days = tuple(market_calendar.stock_trading_days(dt.date(2007, 1, 1), dt.date(2022, 12, 31)))
        # O1 日期已由正式回测按债市日历确定；历史差分在该日历内回看。
        bond_days = tuple(sorted(day for day in inputs.market["UST10Y"] if day <= dt.date(2022, 12, 31)))
        return cls(inputs, days, {day: i for i, day in enumerate(days)}, bond_days,
                   {day: i for i, day in enumerate(bond_days)})

    def prior(self, day: dt.date, n: int) -> dt.date | None:
        i = self.index.get(day)
        return self.days[i - n] if i is not None and i >= n else None

    def window(self, day: dt.date, n: int) -> tuple[dt.date, ...]:
        i = self.index.get(day)
        return self.days[i - n + 1:i + 1] if i is not None and i + 1 >= n else ()

    def value(self, name: str, day: dt.date | None) -> Decimal | None:
        if day is None:
            return None
        source = self.inputs.market.get(name) or self.inputs.tradingview.get(name)
        return source.get(day) if source is not None else None

    def change(self, name: str, day: dt.date, n: int) -> Decimal | None:
        return _difference(self.value(name, day), self.value(name, self.prior(day, n)))

    def mean(self, name: str, day: dt.date, n: int) -> Decimal | None:
        window = self.window(day, n)
        values = [self.value(name, d) for d in window]
        return sum(values) / Decimal(n) if len(values) == n and all(v is not None for v in values) else None

    def _score(self, day: dt.date, version: str, field: str) -> Decimal | None:
        return _dec(self.inputs.scores[day, version][field])

    def _metric(self, day: dt.date, field: str) -> Decimal | None:
        return _dec(self.inputs.metrics[day].get(field))

    def _hyg_lqd(self, day: dt.date) -> Decimal | None:
        return _ratio(self.value("HYG", day), self.value("LQD", day))

    def _oas(self, day: dt.date, n: int) -> Decimal | None:
        o1_text = self.inputs.metrics[day].get("oas_o1_date_v3r1", "")
        if not o1_text:
            return None
        o1 = dt.date.fromisoformat(o1_text)
        index = self.bond_index.get(o1)
        if index is None or index < n:
            return None
        source = self.inputs.market["BAMLH0A0HYM2"]
        return _difference(source.get(o1), source.get(self.bond_days[index - n]))

    def values(self, day: dt.date) -> dict[str, Decimal | None]:
        """所有读取限于 day 及之前，评分直接读取冻结回测输出。"""
        result: dict[str, Decimal | None] = {}
        for name in ("S5FI", "S5TW", "MMFI", "MMTW", "R2FI", "R2TW", "NDTW"):
            result[name] = self.value(name, day)
        for name in ("S5FI", "S5TW", "NDTW"):
            for n in ((5, 10, 20) if name in ("S5TW", "NDTW") else (5, 10)):
                result[f"{name}_change_{n}"] = self.change(name, day, n)
        for name in ("MMFI", "MMTW", "R2FI", "R2TW"):
            result[f"{name}_change_10"] = self.change(name, day, 10)
        s5tw_window = self.window(day, 60)
        s5tw_values = [self.value("S5TW", d) for d in s5tw_window]
        result["S5TW_below_60d_high"] = (_difference(self.value("S5TW", day), max(s5tw_values))
                                              if len(s5tw_values) == 60 and all(v is not None for v in s5tw_values)
                                              else None)
        ratio_days = self.window(day, 20)
        ratios = [_ratio(self.value("RSP", d), self.value("SPY", d)) for d in ratio_days]
        current_ratio = _ratio(self.value("RSP", day), self.value("SPY", day))
        ratio_mean = sum(ratios) / Decimal(20) if len(ratios) == 20 and all(v is not None for v in ratios) else None
        relative = _ratio(current_ratio, ratio_mean)
        result["RSP_SPY_ratio_vs_ma20_pct"] = (relative - 1) * 100 if relative is not None else None
        add_days = self.window(day, 10)
        add_values = [self.value("ADD", d) for d in add_days]
        result["ADD_sum_10"] = (sum(add_values) if len(add_values) == 10 and all(v is not None for v in add_values)
                                else None)
        spread = _difference(self.value("HIGN", day), self.value("LOWN", day))
        old = self.prior(day, 10)
        result["HIGN_minus_LOWN"] = spread
        result["HIGN_minus_LOWN_change_10"] = _difference(
            spread, _difference(self.value("HIGN", old), self.value("LOWN", old)))
        for symbol in ("SPY", "QQQ"):
            close = self.value(symbol, day)
            ma5 = self._metric(day, f"{symbol}.ma5")
            for n in (20, 50):
                ma = self._metric(day, f"{symbol}.ma{n}")
                quotient = _ratio(close, ma)
                result[f"{symbol}_close_vs_ma{n}_pct"] = (quotient - 1) * 100 if quotient is not None else None
            ma20 = self._metric(day, f"{symbol}.ma20")
            quotient = _ratio(ma5, ma20)
            result[f"{symbol}_ma5_vs_ma20_pct"] = (quotient - 1) * 100 if quotient is not None else None
        vix = self._metric(day, "VIX")
        result["VIX"] = vix
        vix_old_date = self.prior(day, 5)
        if vix_old_date in self.inputs.metrics:
            vix_old = self._metric(vix_old_date, "VIX")
        else:
            vix_old = self.value("VIXCLS", vix_old_date) or self.value("VIX_CBOE", vix_old_date)
        result["VIX_change_5"] = _difference(vix, vix_old)
        vix3m = self.value("VIX3M", day) if day >= VIX3M_START else None
        result["VIX_VIX3M"] = _ratio(vix, vix3m)
        result["PCCE"] = self.value("PCCE", day)
        result["PCCE_ma10"] = self.mean("PCCE", day, 10)
        result["OAS_O1"] = self._metric(day, "oas_o1_v3r1")
        result["OAS_change_5"] = self._oas(day, 5)
        result["OAS_change_20"] = self._oas(day, 20)
        old_ratio = self._hyg_lqd(self.prior(day, 20)) if self.prior(day, 20) else None
        result["HYG_LQD_change_20"] = _difference(self._hyg_lqd(day), old_ratio)
        result["UST10Y_change_20"] = self._metric(day, "dy_bp")
        for version in ("v2-M", "v3-R1"):
            prefix = "v2m" if version == "v2-M" else "v3r1"
            for name in ("total_min", "total_max", "price", "breadth", "vix", "rates", "credit"):
                result[f"{prefix}_{name}"] = self._score(day, version, name)
        for symbol in ("SPX", "QQQ"):
            prices = [self.value(symbol, d) for d in self.window(day, 63)]
            current = self.value(symbol, day)
            result[f"{symbol}_drawdown_63"] = (1 - current / max(prices) if len(prices) == 63
                                                   and current is not None and all(v is not None for v in prices)
                                                   else None)
        return result


def high_position_days(engine: FeatureEngine, days: Sequence[dt.date], threshold: Decimal,
                       symbol: str = "SPX") -> tuple[dt.date, ...]:
    """高位判定比较指定指数的当日价与含当日60日最高价；缺价留空。"""
    result = []
    for day in days:
        window = engine.window(day, 60)
        values = [engine.value(symbol, d) for d in window]
        today = engine.value(symbol, day)
        if len(values) == 60 and all(value is not None for value in values) and today >= max(values) * threshold:
            result.append(day)
    return tuple(result)
