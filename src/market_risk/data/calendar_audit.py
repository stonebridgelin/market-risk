"""休市日历审计（阶段6之前补齐 holidays.yaml）与"债市休市日 OAS 数值不同"清单。纯函数，不联网。

- 股市休市日、提前收盘日：以 NYSE 日历（pandas-market-calendars）为准；
- 债市休市日：以财政部数据缺失的工作日为准；
- 与 SIFMA 常见规则对照（哥伦布日、退伍军人节全天休市；长周末前提前收盘），不一致的日期列出；
  已裁定的见 CALENDAR_RESOLUTIONS（以财政部数据为准，2026-09-27）。
- 债市休市日 OAS 观测的处理（SPEC 5.6 第10条）：一律排除；自然月末例外（v2-M 计入，v3-R1 不计入）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from market_risk import calendar as mcal
from market_risk.config import DataDecision

# 已知的非规则休市（国丧日、飓风等），只用于给日期命名
SPECIAL_CLOSURES = {
    dt.date(2001, 9, 11): "9·11 事件", dt.date(2001, 9, 12): "9·11 事件", dt.date(2001, 9, 13): "9·11 事件",
    dt.date(2001, 9, 14): "9·11 事件", dt.date(2004, 6, 11): "里根国丧日", dt.date(2007, 1, 2): "福特国丧日",
    dt.date(2012, 10, 29): "飓风桑迪", dt.date(2012, 10, 30): "飓风桑迪", dt.date(2018, 12, 5): "老布什国丧日",
    dt.date(2025, 1, 9): "卡特国丧日",
}


# 财政部数据与 SIFMA 常见规则不一致的日期：2026-09-27 裁定以财政部数据为准
RESOLVED_ON = dt.date(2026, 9, 27)
CALENDAR_RESOLUTIONS: dict[dt.date, str] = {
    dt.date(2012, 10, 30): "飓风桑迪：财政部无数据，按债市休市",
    dt.date(2018, 12, 5): "老布什国丧日：财政部无数据，按债市休市",
    **{d: "耶稣受难日：财政部有数据，按债市开市（提前收盘；当天发布非农就业数据）"
       for d in (dt.date(2010, 4, 2), dt.date(2012, 4, 6), dt.date(2015, 4, 3), dt.date(2021, 4, 2),
                 dt.date(2023, 4, 7), dt.date(2026, 4, 3))},
}


def is_natural_month_end(d: dt.date) -> bool:
    return (d + dt.timedelta(days=1)).month != d.month


def easter(year: int) -> dt.date:
    """公历复活节（匿名公历算法）。"""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: dt.date, saturday_to_friday: bool = True) -> dt.date | None:
    if d.weekday() == 5:
        return d - dt.timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def sifma_rule_holidays(year: int) -> dict[dt.date, str]:
    """SIFMA 常见规则下的债市全天休市日（工作日）。仅作对照，不代表 SIFMA 每年的实际建议。"""
    rules: list[tuple[dt.date | None, str]] = [
        (_observed(dt.date(year, 1, 1), saturday_to_friday=False), "元旦"),
        (_nth_weekday(year, 1, 0, 3), "马丁·路德·金纪念日"),
        (_nth_weekday(year, 2, 0, 3), "总统日"),
        (easter(year) - dt.timedelta(days=2), "耶稣受难日"),
        (_last_weekday(year, 5, 0), "阵亡将士纪念日"),
        (_observed(dt.date(year, 7, 4)), "独立日"),
        (_nth_weekday(year, 9, 0, 1), "劳动节"),
        (_nth_weekday(year, 10, 0, 2), "哥伦布日"),
        (_observed(dt.date(year, 11, 11), saturday_to_friday=False), "退伍军人节"),
        (_nth_weekday(year, 11, 3, 4), "感恩节"),
        (_observed(dt.date(year, 12, 25)), "圣诞节"),
    ]
    if year >= 2022:
        rules.append((_observed(dt.date(year, 6, 19)), "六月节"))
    return {d: name for d, name in rules if d is not None and d.year == year}


def sifma_rule_early_closes(years: Iterable[int], bond_holidays: set[dt.date]) -> dict[dt.date, str]:
    """SIFMA 常见规则下的提前收盘日（推断，未经数据核实）：长周末前一个营业日、感恩节次日、12-24、12-31。

    只对实际休市（bond_holidays 中）的规则假日推断"长周末前"，规则假日当天实际开市的不推断。
    """
    out: dict[dt.date, str] = {}

    def business(d: dt.date) -> bool:
        return d.weekday() < 5 and d not in bond_holidays

    for year in years:
        for h, name in sifma_rule_holidays(year).items():
            if h in bond_holidays and h.weekday() in (0, 4) and name != "感恩节":   # 周一或周五的假日构成长周末
                prev = h - dt.timedelta(days=1)
                while not business(prev):
                    prev -= dt.timedelta(days=1)
                out.setdefault(prev, f"{name}前（长周末）")
        tg = _nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1)
        for d, name in ((tg, "感恩节次日"), (dt.date(year, 12, 24), "平安夜"), (dt.date(year, 12, 31), "除夕")):
            if business(d):
                out.setdefault(d, name)
    return out


def holiday_name(d: dt.date) -> str:
    return SPECIAL_CLOSURES.get(d) or sifma_rule_holidays(d.year).get(d, "")


@dataclass
class CalendarAudit:
    start: dt.date
    end: dt.date                                       # 财政部数据的最后日期
    stock_holidays: list[dt.date] = field(default_factory=list)
    stock_early_closes: list[dt.date] = field(default_factory=list)
    bond_holidays: list[dt.date] = field(default_factory=list)       # 财政部缺失的工作日
    bond_early_closes: dict[dt.date, str] = field(default_factory=dict)  # SIFMA 规则推断 + 已裁定
    only_treasury: list[dt.date] = field(default_factory=list)       # 财政部缺失、但不在 SIFMA 规则中
    only_rule: list[dt.date] = field(default_factory=list)           # SIFMA 规则休市、但财政部有数据

    @property
    def unresolved(self) -> list[dt.date]:
        """不一致且尚未裁定的日期。"""
        return sorted(d for d in (*self.only_treasury, *self.only_rule) if d not in CALENDAR_RESOLUTIONS)


def audit_calendar(treasury_dates: Iterable[dt.date], start: dt.date, end: dt.date) -> CalendarAudit:
    """按财政部数据推出债市休市日，与 NYSE 日历和 SIFMA 规则对照（只列出，不修正）。"""
    have = {d for d in treasury_dates if start <= d <= end}
    weekdays = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    weekdays = [d for d in weekdays if d.weekday() < 5]
    trading = set(mcal.stock_trading_days(start, end))
    a = CalendarAudit(start, end)
    a.stock_holidays = [d for d in weekdays if d not in trading]
    a.stock_early_closes = [d for d in sorted(trading) if mcal.is_early_close(d)]
    a.bond_holidays = [d for d in weekdays if d not in have]
    rule = {d for y in range(start.year, end.year + 1) for d in sifma_rule_holidays(y) if start <= d <= end}
    a.only_treasury = [d for d in a.bond_holidays if d not in rule]
    a.only_rule = sorted(d for d in rule if d in have)
    a.bond_early_closes = {d: n for d, n in sifma_rule_early_closes(range(start.year, end.year + 1),
                                                                    set(a.bond_holidays)).items()
                           if start <= d <= end}
    # 已裁定：规则假日当天财政部有数据的耶稣受难日，按债市开市、提前收盘
    for d in a.only_rule:
        if d in CALENDAR_RESOLUTIONS and "提前收盘" in CALENDAR_RESOLUTIONS[d]:
            a.bond_early_closes[d] = f"耶稣受难日（债市开市、提前收盘；{RESOLVED_ON} 裁定以财政部数据为准）"
    a.bond_early_closes = dict(sorted(a.bond_early_closes.items()))
    return a


@dataclass(frozen=True)
class HolidayOasDiff:
    symbol: str
    date: dt.date
    value: float
    previous_date: dt.date | None
    previous_value: float | None
    decision: DataDecision | None

    @property
    def month_end(self) -> bool:
        """债市休市日恰为自然月末：月末例外（v2-M 计入，v3-R1 不计入）。"""
        return is_natural_month_end(self.date)

    @property
    def treatment(self) -> str:
        """按统一规则（SPEC 5.6 第10条）的处理。"""
        return "月末例外：v2-M 计入，v3-R1 不计入" if self.month_end else "排除：两个版本都不计入"


def oas_holiday_differences(
    symbol: str, series: Mapping[dt.date, float | None], treasury_dates: Iterable[dt.date],
    start: dt.date, end: dt.date, decisions: Iterable[DataDecision] = (),
) -> list[HolidayOasDiff]:
    """债市休市日（财政部缺失的工作日）有 OAS 数值、且与前一观测不同的日期。"""
    cal = mcal.bond_calendar_from_dates(treasury_dates, start, end)
    decided = {d.date: d for d in decisions if d.symbol == symbol}
    return [
        HolidayOasDiff(symbol, h.date, h.value, h.previous_date, h.previous_value, decided.get(h.date))
        for h in mcal.bond_holiday_observations(series, cal) if not h.carried_forward
    ]


def _fmt(ds: Iterable[dt.date]) -> list[str]:
    return [f"| {d} | 周{'一二三四五六日'[d.weekday()]} | {holiday_name(d) or '-'} | "
            f"{CALENDAR_RESOLUTIONS.get(d, '待判断')} |" for d in ds]


def render_audit(a: CalendarAudit, diffs: list[HolidayOasDiff], generated_at: str, oas_start: dt.date) -> str:
    lines = [
        "# 休市日历审计与债市休市日 OAS 清单", "",
        f"生成时间（UTC）：{generated_at}。只列出，不自动修正。", "",
        "## 一、holidays.yaml 的来源", "",
        f"- 范围：{a.start} 至 {a.end}（财政部数据的最后日期）；之后的日期沿用人工登记。",
        "- 股市休市日、提前收盘日：NYSE 日历（pandas-market-calendars）。",
        "- 债市休市日：财政部 Daily Treasury Par Yield Curve 数据缺失的工作日。",
        "- 债市提前收盘日：按 SIFMA 常见规则推断（长周末前一营业日、感恩节次日、12-24、12-31），**未经数据核实**；"
        "另加已裁定为开市、提前收盘的耶稣受难日。", "",
        f"股市休市日 {len(a.stock_holidays)} 个，股市提前收盘日 {len(a.stock_early_closes)} 个，"
        f"债市休市日 {len(a.bond_holidays)} 个，债市提前收盘日（推断）{len(a.bond_early_closes)} 个。", "",
        "## 二、财政部数据与 SIFMA 常见规则不一致的日期", "",
        f"{RESOLVED_ON} 裁定：以财政部数据为准。未裁定的标为\"待判断\"。", "",
        "### 财政部无数据、但不在 SIFMA 常见规则中", "",
        "| 日期 | 星期 | 说明 | 裁定 |", "|---|---|---|---|", *(_fmt(a.only_treasury) or ["| 无 | | | |"]), "",
        "### SIFMA 常见规则为休市、但财政部有数据", "",
        "| 日期 | 星期 | 规则假日 | 裁定 |", "|---|---|---|---|", *(_fmt(a.only_rule) or ["| 无 | | | |"]), "",
        f"待判断：{len(a.unresolved)} 个"
        + (f"（{'、'.join(str(d) for d in a.unresolved)}）" if a.unresolved else "") + "。",
        "",
        f"## 三、债市休市日 OAS 数值与前一观测不同（{oas_start} 起，高收益与投资级）", "",
        "债市休市日以财政部数据缺失为准。统一规则（SPEC 5.6 第10条，2026-09-27）：债市休市日的 OAS 观测一律排除，"
        "v2-M 与 v3-R1 都不计入；例外：债市休市日恰为自然月末时视为月末观测，v2-M 计入，v3-R1 不计入。"
        "与前一观测相同的沿用值同样适用该规则，不在此列。", "",
        "| 序列 | 日期 | 星期 | 假日 | 数值 | 前一观测 | 规则处理 | 已裁定日期表 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for x in diffs:
        dec = "-"
        if x.decision:
            consistent = (x.decision.decision == "exclude") != x.month_end
            dec = (f"{x.decision.decision}（{x.decision.decided_on}；"
                   f"{'与规则一致' if consistent else '与规则不同，按裁定'}）")
        wd = "一二三四五六日"[x.date.weekday()]
        lines.append(f"| {x.symbol} | {x.date} | 周{wd} | {holiday_name(x.date) or '-'} | "
                     f"{x.value} | {x.previous_date}：{x.previous_value} | {x.treatment} | {dec} |")
    if not diffs:
        lines.append("| 无 | | | | | | | |")
    month_end = sum(1 for x in diffs if x.month_end)
    decided = sum(1 for x in diffs if x.decision is not None)
    lines += ["", f"共 {len(diffs)} 处：月末例外 {month_end} 处，排除 {len(diffs) - month_end} 处；"
                  f"已裁定日期表中登记 {decided} 处。规则已覆盖全部日期，无需逐一裁定；"
                  "只有规则无法覆盖（超出财政部数据范围）时才提示\"需人工判断\"。"]
    return "\n".join(lines) + "\n"


def render_holidays_yaml(a: CalendarAudit, manual: Mapping[str, Iterable[dt.date]]) -> str:
    """生成 holidays.yaml：财政部数据截止日之前按数据与 NYSE 日历，之后沿用人工登记；
    人工登记（已核实）的债市提前收盘日全部保留。"""

    def items(ds: Iterable[dt.date], names: Mapping[dt.date, str] | None = None, resolved: bool = False) -> list[str]:
        out = []
        for d in sorted(set(ds)):
            name = (names or {}).get(d) or holiday_name(d)
            if resolved and d in CALENDAR_RESOLUTIONS and not (names or {}).get(d):
                name = f"{CALENDAR_RESOLUTIONS[d]}（{RESOLVED_ON} 裁定以财政部数据为准）"
            out.append(f"    - {d}" + (f"   # {name}" if name else ""))
        return out

    after = {k: [d for d in v if d > a.end] for k, v in manual.items()}
    after["bond_early_closes"] = list(manual.get("bond_early_closes", []))
    lines = [
        "# 美国股市与债市休市日、提前收盘日（仅作核对用）",
        "# - 股市以 pandas-market-calendars 的 NYSE 日历为准，债市以财政部收益率数据实际存在的日期为准；",
        "#   本文件与二者不一致时，只在 data_notes 中报告，不自动修正。",
        f"# - {a.start.year}-01-01 至 {a.end}：由 `market-risk audit calendar --write` 生成（股市：NYSE 日历；"
        "债市休市：财政部数据缺失的工作日；",
        "#   债市提前收盘：SIFMA 常见规则推断，未经数据核实）。之后的日期为人工登记。"
        "对照结果见 reports/holiday_calendar_audit.md。",
        f"# - 与 SIFMA 常见规则不一致的日期已于 {RESOLVED_ON} 裁定以财政部数据为准（原因见各日期注释），"
        "不再列为待判断。",
        "",
        "stock:",
        "  holidays:",
        *items([*a.stock_holidays, *after.get("stock_holidays", [])]),
        "  early_closes:",
        *items([*a.stock_early_closes, *after.get("stock_early_closes", [])]),
        "",
        "bond:",
        "  holidays:",
        *items([*a.bond_holidays, *after.get("bond_holidays", [])], resolved=True),
        "  early_closes:",
        *items([*a.bond_early_closes, *after.get("bond_early_closes", [])], a.bond_early_closes),
    ]
    return "\n".join(lines) + "\n"
