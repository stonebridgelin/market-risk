"""交易日与债市营业日计算（SPEC 5.2 / SOP 第6节）。

- 股票交易日：pandas-market-calendars 的 NYSE 日历，提前收盘日也由它识别。
- 债市营业日：以财政部收益率数据中实际存在的日期为准，由调用方以 BondCalendar 传入；
  本模块不联网。config/holidays.yaml 只用于核对，不一致时返回说明文字，不自动修正。

所有公开函数都是纯函数（NYSE 日历按年份缓存，结果确定）。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache

import pandas_market_calendars as mcal

from market_risk.config import MarketHolidays
from market_risk.models import DateReferences

OUTCOME_WINDOW_DAYS = 20
O6_OFFSET = 5  # O6 为 O1 之前第5个债市营业日 / 第5个观测


class CalendarError(ValueError):
    """日期不合法或日历数据不足以完成计算。"""


# ---------------------------------------------------------------------------
# 股票交易日（NYSE）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=64)
def _nyse_year(year: int) -> tuple[tuple[dt.date, ...], frozenset[dt.date]]:
    """返回某年的 NYSE 交易日（升序）与提前收盘日。"""
    cal = mcal.get_calendar("NYSE")
    schedule = cal.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
    days = tuple(ts.date() for ts in schedule.index)
    early = frozenset(ts.date() for ts in cal.early_closes(schedule).index)
    return days, early


def stock_trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """start 至 end（含两端）之间的全部股票交易日，升序。"""
    if start > end:
        return []
    days: list[dt.date] = []
    for year in range(start.year, end.year + 1):
        days.extend(d for d in _nyse_year(year)[0] if start <= d <= end)
    return days


def is_stock_trading_day(d: dt.date) -> bool:
    return d in _nyse_year(d.year)[0]


def is_early_close(d: dt.date) -> bool:
    """是否为股市提前收盘日。"""
    return d in _nyse_year(d.year)[1]


def shift_trading_days(d: dt.date, n: int) -> dt.date:
    """从 d 起按股票交易日移动 n 天（n<0 往前，n>0 往后）。

    d 本身必须是交易日；n=0 返回 d。
    """
    if not is_stock_trading_day(d):
        raise CalendarError(f"{d} 不是股票交易日")
    if n == 0:
        return d
    # 每年约252个交易日，按需扩展年份范围
    span_years = abs(n) // 240 + 1
    if n < 0:
        days = stock_trading_days(dt.date(d.year - span_years, 1, 1), d)
        idx = len(days) - 1 + n
    else:
        days = stock_trading_days(d, dt.date(d.year + span_years, 12, 31))
        idx = n
    if idx < 0 or idx >= len(days):
        raise CalendarError(f"无法从 {d} 移动 {n} 个交易日：日历范围不足")
    return days[idx]


def count_trading_days_after(start_exclusive: dt.date, end_inclusive: dt.date) -> int:
    """start（不含）至 end（含）之间的股票交易日个数。"""
    if end_inclusive <= start_exclusive:
        return 0
    return len(stock_trading_days(start_exclusive + dt.timedelta(days=1), end_inclusive))


def is_last_trading_day_of_week(d: dt.date) -> bool:
    """d 是否为所在周（周一至周日）的最后一个股票交易日。"""
    if not is_stock_trading_day(d):
        return False
    week_end = d + dt.timedelta(days=6 - d.weekday())
    return not stock_trading_days(d + dt.timedelta(days=1), week_end)


def monthly_sample_dates(year: int) -> list[dt.date]:
    """SOP 9.2：每月最后一个周五；若该日休市，取该月最后一个交易日。"""
    result: list[dt.date] = []
    for month in range(1, 13):
        next_month = dt.date(year + (month == 12), month % 12 + 1, 1)
        month_end = next_month - dt.timedelta(days=1)
        last_friday = month_end - dt.timedelta(days=(month_end.weekday() - 4) % 7)
        if is_stock_trading_day(last_friday):
            result.append(last_friday)
        else:
            days = stock_trading_days(dt.date(year, month, 1), month_end)
            if not days:
                raise CalendarError(f"{year}-{month:02d} 没有股票交易日")
            result.append(days[-1])
    return result


# ---------------------------------------------------------------------------
# 债市营业日（以财政部数据实际存在的日期为准）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BondCalendar:
    """债市营业日集合及其覆盖范围。

    coverage_start 至 coverage_end 之间、不在 days 中的工作日视为债市休市；
    覆盖范围以外的日期无法判断，查询时报错，避免把"没下载数据"误当成"休市"。
    """

    days: frozenset[dt.date]
    coverage_start: dt.date
    coverage_end: dt.date

    def _check_covered(self, d: dt.date) -> None:
        if not (self.coverage_start <= d <= self.coverage_end):
            raise CalendarError(
                f"债市日历覆盖范围为 {self.coverage_start} 至 {self.coverage_end}，"
                f"无法判断 {d} 是否为债市营业日"
            )

    def is_business_day(self, d: dt.date) -> bool:
        self._check_covered(d)
        return d in self.days

    def business_days_before(self, d: dt.date, n: int) -> list[dt.date]:
        """d 之前（不含 d）最近的 n 个债市营业日，按日期降序。"""
        result: list[dt.date] = []
        cur = d
        while len(result) < n:
            cur -= dt.timedelta(days=1)
            if self.is_business_day(cur):
                result.append(cur)
        return result

    def holidays_between(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """start 至 end（含）之间的工作日中，债市休市的日期。"""
        self._check_covered(start)
        self._check_covered(end)
        return [
            d
            for d in _weekdays(start, end)
            if d not in self.days
        ]


def _weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    days: list[dt.date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += dt.timedelta(days=1)
    return days


def bond_calendar_from_dates(
    dates: Iterable[dt.date], coverage_start: dt.date, coverage_end: dt.date
) -> BondCalendar:
    """由财政部数据中实际存在的日期构造债市日历（只保留覆盖范围内的工作日）。"""
    if coverage_start > coverage_end:
        raise CalendarError("债市日历覆盖范围起点晚于终点")
    days = frozenset(
        d for d in dates if coverage_start <= d <= coverage_end and d.weekday() < 5
    )
    return BondCalendar(days=days, coverage_start=coverage_start, coverage_end=coverage_end)


def bond_calendar_from_holidays(
    holidays: MarketHolidays, coverage_start: dt.date, coverage_end: dt.date
) -> BondCalendar:
    """由 holidays.yaml 推出债市日历（工作日扣除债市休市日）。

    仅用于离线预览日期；正式计算以财政部数据为准（SPEC 5.2）。
    """
    days = [d for d in _weekdays(coverage_start, coverage_end) if d not in holidays.bond_holidays]
    return bond_calendar_from_dates(days, coverage_start, coverage_end)


def oas_o1(base_date: dt.date, bond_cal: BondCalendar) -> dt.date:
    """O1：基准日之前（不含基准日）最近一个债市营业日。"""
    return bond_cal.business_days_before(base_date, 1)[0]


def oas_o1_to_o6_sequence(o1: dt.date, bond_cal: BondCalendar) -> list[dt.date]:
    """从 O1 往前的债市营业日：[O1, O1之前第1个, …, O1之前第5个(=O6)]。

    只数债市营业日，月末周末观测自然不计入（v3-R1 口径）。
    """
    if not bond_cal.is_business_day(o1):
        raise CalendarError(f"O1={o1} 不是债市营业日")
    return [o1, *bond_cal.business_days_before(o1, O6_OFFSET)]


def oas_o6_v3r1(o1: dt.date, bond_cal: BondCalendar) -> dt.date:
    """O6（v3-R1）：O1 之前第5个债市营业日；月末周末观测不计入。"""
    return oas_o1_to_o6_sequence(o1, bond_cal)[-1]


def _is_missing(value: float | None) -> bool:
    return value is None or math.isnan(value)


def valued_observation_dates(observations: Mapping[dt.date, float | None]) -> list[dt.date]:
    """FRED 观测中有数值的日期，升序。值为 "."（已转为 None/NaN）的行不算观测（SPEC 5.6 第8条）。"""
    return sorted(d for d, v in observations.items() if not _is_missing(v))


@dataclass(frozen=True)
class HolidayObservation:
    """FRED 在债市休市日（工作日）返回的有数值观测（SPEC 5.6 第10条）。"""

    date: dt.date
    value: float
    previous_date: dt.date | None
    previous_value: float | None

    @property
    def carried_forward(self) -> bool:
        """是否为沿用值：与前一个有数值观测相同（按两位小数）。"""
        return self.previous_value is not None and round(self.value, 2) == round(
            self.previous_value, 2
        )


def bond_holiday_observations(
    observations: Mapping[dt.date, float | None], bond_cal: BondCalendar
) -> list[HolidayObservation]:
    """找出落在债市休市日（覆盖范围内、非债市营业日的工作日）的有数值观测。周末观测不在此列。"""
    valued = valued_observation_dates(observations)
    result: list[HolidayObservation] = []
    for i, d in enumerate(valued):
        if d.weekday() >= 5 or not (bond_cal.coverage_start <= d <= bond_cal.coverage_end):
            continue
        if bond_cal.is_business_day(d):
            continue
        prev = valued[i - 1] if i > 0 else None
        result.append(
            HolidayObservation(
                date=d,
                value=float(observations[d]),  # type: ignore[arg-type]
                previous_date=prev,
                previous_value=None if prev is None else float(observations[prev]),  # type: ignore[arg-type]
            )
        )
    return result


def v2m_observation_dates(
    observations: Mapping[dt.date, float | None], bond_cal: BondCalendar
) -> list[dt.date]:
    """v2-M 计数用的观测：有数值的行，排除债市休市日的沿用值；月末周末观测保留。

    债市休市日的数值若与前一个观测不同，不自动排除（仍计入），由调用方报告（SPEC 5.6 第10条）。
    """
    carried = {h.date for h in bond_holiday_observations(observations, bond_cal)
               if h.carried_forward}
    return [d for d in valued_observation_dates(observations) if d not in carried]


def oas_o1_v2m(base_date: dt.date, observation_dates: Iterable[dt.date]) -> dt.date | None:
    """O1（v2-M）：基准日之前（不含基准日）最新的一个有数值观测；没有时返回 None。"""
    earlier = [d for d in observation_dates if d < base_date]
    return max(earlier) if earlier else None


def oas_o6_v2m(o1: dt.date, observation_dates: Iterable[dt.date]) -> dt.date | None:
    """O6（v2-M）：按 FRED 实际返回的观测列表，从 O1 往前数第5个观测（月末周末观测计入）。

    observation_dates 只应包含有数值的观测（见 valued_observation_dates），且必须包含 O1；
    O1 之前的观测不足5个时返回 None（由调用方记待补）。
    """
    obs = sorted(set(observation_dates))
    if o1 not in obs:
        raise CalendarError(f"FRED 观测列表中没有 O1={o1}")
    earlier = [d for d in obs if d < o1]
    if len(earlier) < O6_OFFSET:
        return None
    return earlier[-O6_OFFSET]


def o1_lag_stock_days(o1: dt.date, base_date: dt.date) -> int:
    """O1 滞后天数：O1 之后（不含）至基准日（含）之间的股票交易日个数（SPEC 5.6 第4条）。"""
    return count_trading_days_after(o1, base_date)


def rate_window_observations(
    values: Mapping[dt.date, float | None], window_days: Iterable[dt.date]
) -> tuple[dict[dt.date, float], list[dt.date]]:
    """把财政部逐日数值限定到20日窗口（只按股票交易日，SPEC 5.6 第9条）。

    返回 (窗口内的有效数值, 被排除的日期)。被排除的是位于窗口日期范围内、
    财政部有数值但股市休市的日子（如 2026-04-03），由调用方写入 data_notes。
    窗口内股市开市但财政部无数值的日子（债市休市）直接缺失，不插值。
    """
    days = sorted(window_days)
    if not days:
        return {}, []
    day_set = set(days)
    included: dict[dt.date, float] = {}
    excluded: list[dt.date] = []
    for d in sorted(values):
        v = values[d]
        if not (days[0] <= d <= days[-1]) or _is_missing(v):
            continue
        if d in day_set:
            included[d] = float(v)  # type: ignore[arg-type]
        else:
            excluded.append(d)
    return included, excluded


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def compute_date_references(
    base_date: dt.date,
    bond_cal: BondCalendar,
    oas_observations: Mapping[dt.date, float | None] | None = None,
    three_segment_offset: int = 45,
) -> DateReferences:
    """计算基准日的全部日期参照。

    oas_observations：FRED 实际返回的 OAS 观测（日期 → 数值，"." 已转为 None/NaN）；
    v2-M 只数有数值的行，并排除债市休市日的沿用值（v2m_observation_dates）。
    为 None 时 v2-M 的 O1/O6 无法确定，记为 None。
    休市日列表的范围为 min(T−20, O6) 至基准日。
    """
    if not is_stock_trading_day(base_date):
        raise CalendarError(f"基准日 {base_date} 不是股票交易日")

    t5 = shift_trading_days(base_date, -5)
    t20 = shift_trading_days(base_date, -20)
    window_start = shift_trading_days(base_date, -19)
    window_days = tuple(stock_trading_days(window_start, base_date))

    o1 = oas_o1(base_date, bond_cal)
    sequence = oas_o1_to_o6_sequence(o1, bond_cal)
    o6_v3 = sequence[-1]
    o1_v2: dt.date | None = None
    o6_v2: dt.date | None = None
    if oas_observations is not None:
        valued = v2m_observation_dates(oas_observations, bond_cal)
        o1_v2 = oas_o1_v2m(base_date, valued)
        if o1_v2 is not None:
            o6_v2 = oas_o6_v2m(o1_v2, valued)

    span_start = min(t20, o6_v3)
    weekdays = _weekdays(span_start, base_date)
    trading = set(stock_trading_days(span_start, base_date))
    stock_holidays = tuple(d for d in weekdays if d not in trading)
    # 每日模式下财政部可能尚未发布基准日数值，此时覆盖范围止于前一日，不把基准日误列为休市
    bond_holidays = tuple(
        bond_cal.holidays_between(span_start, min(base_date, bond_cal.coverage_end))
    )

    return DateReferences(
        base_date=base_date,
        t_minus_5=t5,
        t_minus_20=t20,
        window_start=window_start,
        window_end=base_date,
        window_days=window_days,
        oas_o1=o1,
        oas_o6_v3r1=o6_v3,
        oas_o1_v2m=o1_v2,
        oas_o6_v2m=o6_v2,
        oas_o1_to_o6_sequence=tuple(sequence),
        o1_lag_stock_days=o1_lag_stock_days(o1, base_date),
        o1_v2m_lag_stock_days=None if o1_v2 is None else o1_lag_stock_days(o1_v2, base_date),
        three_segment_query_start=shift_trading_days(base_date, -three_segment_offset),
        outcome_window_start=shift_trading_days(base_date, 1),
        outcome_window_end=shift_trading_days(base_date, OUTCOME_WINDOW_DAYS),
        stock_holidays_in_window=stock_holidays,
        bond_holidays_in_window=bond_holidays,
        is_early_close=is_early_close(base_date),
        is_last_trading_day_of_week=is_last_trading_day_of_week(base_date),
    )


def o6_difference_note(refs: DateReferences) -> str | None:
    """两种口径的 O1 或 O6 不同时，说明原因（SPEC 5.2）。"""
    if refs.oas_o1_v2m is not None and refs.oas_o1_v2m != refs.oas_o1:
        return (
            f"两种口径的 O1 不同：v3-R1 要求基准日之前最近一个债市营业日 {refs.oas_o1}，"
            f"FRED 在基准日之前最新的有数值观测为 {refs.oas_o1_v2m}（v2-M 采用），"
            "说明 FRED 在该营业日缺少数值或数据滞后。"
        )
    if refs.oas_o6_v2m is None or refs.oas_o6_v2m == refs.oas_o6_v3r1:
        return None
    return (
        f"两种口径的 O6 不同：v3-R1 按债市营业日数为 {refs.oas_o6_v3r1}，"
        f"v2-M 按 FRED 观测列表数为 {refs.oas_o6_v2m}。"
        "通常原因是 O1 至 O6 之间存在月末周末观测（v2-M 计入、v3-R1 不计入），"
        "或 FRED 在某个债市营业日缺少观测。"
    )


# ---------------------------------------------------------------------------
# 与 holidays.yaml 核对（只报告，不修正）
# ---------------------------------------------------------------------------


def check_stock_calendar(
    holidays: MarketHolidays, start: dt.date, end: dt.date
) -> list[str]:
    """比较 NYSE 日历与 holidays.yaml 在 [start, end] 内的股市休市日与提前收盘日。"""
    notes: list[str] = []
    trading = set(stock_trading_days(start, end))
    for d in _weekdays(start, end):
        in_yaml = d in holidays.stock_holidays
        closed = d not in trading
        if closed and not in_yaml:
            notes.append(f"{d}：NYSE 日历休市，但 holidays.yaml 未列为股市休市日")
        elif in_yaml and not closed:
            notes.append(f"{d}：holidays.yaml 列为股市休市日，但 NYSE 日历开市")
        if d in trading:
            early_yaml = d in holidays.stock_early_closes
            if is_early_close(d) != early_yaml:
                notes.append(
                    f"{d}：提前收盘不一致（NYSE 日历={is_early_close(d)}，"
                    f"holidays.yaml={early_yaml}）"
                )
    return notes


def check_bond_calendar(
    bond_cal: BondCalendar, holidays: MarketHolidays, start: dt.date, end: dt.date
) -> list[str]:
    """比较财政部数据推出的债市营业日与 holidays.yaml（范围与覆盖范围取交集）。"""
    lo = max(start, bond_cal.coverage_start)
    hi = min(end, bond_cal.coverage_end)
    notes: list[str] = []
    for d in _weekdays(lo, hi):
        in_yaml = d in holidays.bond_holidays
        has_data = d in bond_cal.days
        if not has_data and not in_yaml:
            notes.append(f"{d}：工作日但财政部无数据，holidays.yaml 未列为债市休市日")
        elif has_data and in_yaml:
            notes.append(f"{d}：holidays.yaml 列为债市休市日，但财政部有数据")
    return notes
