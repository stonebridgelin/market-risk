"""风险事件标签（SOP 9.3 / docs/STORAGE.md 第5节）。

**隔离要求**：scoring/、report.py（生成 prompt 的部分）、data/snapshot.py 不得读取本模块或标签文件。
标签只在结果窗口结束后计算：基准日收盘价为起点，之后20个交易日内
标普500指数最低收盘价跌幅 ≥ 5%，或 QQQ 最低收盘价跌幅 ≥ 7%，任一成立即为风险事件。只用收盘价。
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from market_risk import calendar as mcal

SPX_THRESHOLD = -5.0   # 百分数
QQQ_THRESHOLD = -7.0
# 辅助字段（SOP 9.3，仅作参考，不改变 is_event 的定义）
SPX_NEAR = -4.0
QQQ_NEAR = -6.0
FIELDS = ["subject", "base_date", "window_start", "window_end", "spx_min_close_drawdown",
          "qqq_min_close_drawdown", "is_event", "event_date", "spx_max_drawdown", "qqq_max_drawdown",
          "near_event", "source", "entered_at"]
TOLERANCE = 0.01


class OutcomeError(ValueError):
    """结果窗口未结束、数据不全等。"""


@dataclass(frozen=True)
class Outcome:
    subject: str
    base_date: dt.date
    window_start: dt.date
    window_end: dt.date
    spx_min_close_drawdown: float     # 百分数，如 −5.23
    qqq_min_close_drawdown: float
    is_event: bool
    event_date: dt.date | None        # 首次达到门槛的日期（用于计算命中样本的提前量）
    source: str                       # manual / computed
    entered_at: str
    spx_max_drawdown: float | None = None   # 辅助：窗口内（含基准日）最高收盘价到其后最低收盘价的跌幅
    qqq_max_drawdown: float | None = None

    @property
    def near_event(self) -> bool:
        """辅助：未构成风险事件，但标普500最低收盘价跌幅 ≥4% 或 QQQ ≥6%（仅作参考）。"""
        return is_near_event(self.spx_min_close_drawdown, self.qqq_min_close_drawdown, self.is_event)


def is_near_event(spx_min: float, qqq_min: float, is_event: bool) -> bool:
    return not is_event and (spx_min <= SPX_NEAR or qqq_min <= QQQ_NEAR)


def max_drawdown(closes: list[float]) -> float:
    """从最高收盘价到其后最低收盘价的最大跌幅（百分数，≤0）。"""
    peak, worst = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        worst = min(worst, (c / peak - 1) * 100)
    return worst


def outcome_window(base: dt.date) -> tuple[dt.date, dt.date]:
    return mcal.shift_trading_days(base, 1), mcal.shift_trading_days(base, mcal.OUTCOME_WINDOW_DAYS)


def window_finished(base: dt.date, today_new_york: dt.date) -> bool:
    """结果窗口的最后一个交易日收盘之后（即美东次日起）才允许计算。"""
    return today_new_york > outcome_window(base)[1]


def compute_outcome(
    base: dt.date,
    spx: Mapping[dt.date, float],
    qqq: Mapping[dt.date, float],
    today_new_york: dt.date,
    now_utc: dt.datetime | None = None,
    subject: str = "MARKET",
) -> Outcome:
    start, end = outcome_window(base)
    if not window_finished(base, today_new_york):
        raise OutcomeError(f"{base} 的结果窗口到 {end} 才结束，现在不得计算标签")
    days = mcal.stock_trading_days(start, end)
    for name, series in (("标普500", spx), ("QQQ", qqq)):
        if base not in series:
            raise OutcomeError(f"{name} 缺少基准日 {base} 的收盘价")
        missing = [d for d in days if d not in series]
        if missing:
            raise OutcomeError(f"{name} 结果窗口缺少收盘价：{missing[:5]}")

    def drawdowns(series: Mapping[dt.date, float]) -> list[tuple[dt.date, float]]:
        return [(d, (series[d] / series[base] - 1) * 100) for d in days]

    spx_dd, qqq_dd = drawdowns(spx), drawdowns(qqq)
    breach = [d for (d, a), (_, b) in zip(spx_dd, qqq_dd, strict=True)
              if a <= SPX_THRESHOLD or b <= QQQ_THRESHOLD]
    return Outcome(
        subject, base, start, end,
        round(min(v for _, v in spx_dd), 4), round(min(v for _, v in qqq_dd), 4),
        bool(breach), breach[0] if breach else None, "computed",
        (now_utc or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat(timespec="seconds"),
        round(max_drawdown([spx[base], *(spx[d] for d in days)]), 4),
        round(max_drawdown([qqq[base], *(qqq[d] for d in days)]), 4),
    )


def read_outcomes(path: Path) -> list[Outcome]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return [
            Outcome(r["subject"], dt.date.fromisoformat(r["base_date"]), dt.date.fromisoformat(r["window_start"]),
                    dt.date.fromisoformat(r["window_end"]), float(r["spx_min_close_drawdown"]),
                    float(r["qqq_min_close_drawdown"]), r["is_event"] == "是",
                    dt.date.fromisoformat(r["event_date"]) if r.get("event_date") else None,
                    r["source"], r["entered_at"],
                    float(r["spx_max_drawdown"]) if r.get("spx_max_drawdown") else None,
                    float(r["qqq_max_drawdown"]) if r.get("qqq_max_drawdown") else None)
            for r in csv.DictReader(f)
        ]


def write_outcomes(path: Path, outcomes: list[Outcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        for o in sorted(outcomes, key=lambda o: (o.subject, o.base_date, o.source)):
            w.writerow({
                "subject": o.subject, "base_date": o.base_date, "window_start": o.window_start,
                "window_end": o.window_end, "spx_min_close_drawdown": o.spx_min_close_drawdown,
                "qqq_min_close_drawdown": o.qqq_min_close_drawdown, "is_event": "是" if o.is_event else "否",
                "event_date": o.event_date or "", "source": o.source, "entered_at": o.entered_at,
                "spx_max_drawdown": "" if o.spx_max_drawdown is None else o.spx_max_drawdown,
                "qqq_max_drawdown": "" if o.qqq_max_drawdown is None else o.qqq_max_drawdown,
                "near_event": "是" if o.near_event else "否",
            })


def record_outcome(path: Path, new: Outcome) -> list[str]:
    """写入（同一对象、基准日、来源只保留一条）；手工与程序计算的标签不一致时返回差异说明。"""
    rows = [o for o in read_outcomes(path)
            if not (o.subject == new.subject and o.base_date == new.base_date and o.source == new.source)]
    rows.append(new)
    write_outcomes(path, rows)
    diffs = []
    for o in rows:
        if o.subject == new.subject and o.base_date == new.base_date and o.source != new.source:
            if (o.is_event != new.is_event
                    or abs(o.spx_min_close_drawdown - new.spx_min_close_drawdown) > TOLERANCE
                    or abs(o.qqq_min_close_drawdown - new.qqq_min_close_drawdown) > TOLERANCE):
                diffs.append(
                    f"{new.base_date} 标签不一致：{o.source} 为 {'是' if o.is_event else '否'}"
                    f"（标普500 {o.spx_min_close_drawdown}%、QQQ {o.qqq_min_close_drawdown}%），"
                    f"{new.source} 为 {'是' if new.is_event else '否'}"
                    f"（标普500 {new.spx_min_close_drawdown}%、QQQ {new.qqq_min_close_drawdown}%）"
                )
    return diffs


def effective_outcomes(outcomes: list[Outcome]) -> dict[tuple[str, dt.date], Outcome]:
    """统计使用的标签：同一样本有手工标签时以手工为准（SOP 9.5：结果标签由用户自行核算）。"""
    result: dict[tuple[str, dt.date], Outcome] = {}
    for o in sorted(outcomes, key=lambda o: o.source != "manual"):
        result.setdefault((o.subject, o.base_date), o)
    return result
