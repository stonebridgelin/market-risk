"""TradingView 广度指标的早期数据质量检查（只报告，不修改数据，也不改变评分逻辑）。

检查项：
1. "开盘=最高=最低=收盘"的单值K线阶段在哪一天结束；
2. 分界点前后各20个交易日的日变化，列出明显跳变；
3. 节假日（非 NYSE 交易日）有数据的日期是否都落在单值阶段内；
4. 疑似陈旧值：交易日收盘值与前一个交易日完全相同的日期（全部历史）。
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise

from market_risk import calendar as mcal
from market_risk.data.tradingview import Bar

MULTI_RUN = 20          # 连续20个多值K线视为单值阶段结束
BOUNDARY_DAYS = 20      # 分界点前后各看20个交易日
JUMP_FACTOR = 3.0       # 日变化超过窗口中位数的3倍视为明显跳变
JUMP_MIN_POINTS = 5.0   # 且至少5个百分点
SCORING_BREADTH = ("S5FI", "S5TW")
# 名义步长：一只成分股对应的百分点（成分股约 500、100、3000+、5000+、2000 只）
NOMINAL_STEP = {"S5": 0.2, "ND": 1.0, "NC": 0.03, "MM": 0.02, "R2": 0.05}


def is_single(bar: Bar) -> bool:
    """单值K线：开高低收相同（开高低缺失时也视为单值，因为没有日内区间信息）。"""
    values = [bar.open, bar.high, bar.low]
    return all(v is None or v == bar.close for v in values)


def stale_dates(closes: Mapping[dt.date, float]) -> list[dt.date]:
    """疑似陈旧值：NYSE 交易日的收盘值与前一个 NYSE 交易日（数据中存在）完全相同。"""
    if not closes:
        return []
    days = mcal.stock_trading_days(min(closes), max(closes))
    out = []
    for prev, d in pairwise(days):
        if d in closes and prev in closes and closes[d] == closes[prev]:
            out.append(d)
    return out


@dataclass
class Jump:
    date: dt.date
    previous: float
    value: float
    change: float


@dataclass
class QualityResult:
    symbol: str
    first_date: dt.date
    last_date: dt.date
    rows: int
    single_phase_end: dt.date | None          # 单值阶段最后一天；None 表示没有单值阶段
    single_in_phase: int = 0
    single_after_phase: list[dt.date] = field(default_factory=list)
    boundary_before_mean: float | None = None  # 分界点前20个交易日的平均绝对日变化
    boundary_after_mean: float | None = None
    boundary_change: float | None = None       # 分界点次日相对分界点的变化
    jumps: list[Jump] = field(default_factory=list)
    holiday_dates: list[dt.date] = field(default_factory=list)
    holidays_outside_phase: list[dt.date] = field(default_factory=list)
    stale: list[dt.date] = field(default_factory=list)
    step: float | None = None                  # 名义步长：一只成分股对应的百分点（按指标家族给定）
    zero_after_phase: int = 0                  # 单值阶段之后日变化恰为0的次数
    small_after_phase: int = 0                 # 单值阶段之后 0<|日变化|≤步长 的次数

    @property
    def expected_zero(self) -> float | None:
        """偶然情况下"变化恰为0"的期望次数：日变化分布平滑时约为 0<|Δ|≤步长 次数的一半。"""
        return None if self.step is None else self.small_after_phase / 2

    @property
    def stale_after_phase(self) -> list[dt.date]:
        end = self.single_phase_end
        return [d for d in self.stale if end is None or d > end]


def analyze(symbol: str, bars: Mapping[dt.date, Bar]) -> QualityResult:
    dates = sorted(bars)
    trading = set(mcal.stock_trading_days(dates[0], dates[-1]))
    tdays = [d for d in dates if d in trading]
    res = QualityResult(symbol, dates[0], dates[-1], len(dates), None)

    # 1. 单值阶段：第一次出现连续 MULTI_RUN 个多值K线之前的最后一个单值K线
    single = [is_single(bars[d]) for d in tdays]
    start = next((i for i in range(len(tdays) - MULTI_RUN + 1) if not any(single[i:i + MULTI_RUN])), None)
    if start is not None and start > 0:
        end_idx = max(i for i in range(start) if single[i])
        res.single_phase_end = tdays[end_idx]
    elif start is None and any(single):
        res.single_phase_end = tdays[-1]
    end = res.single_phase_end
    if end is not None:
        res.single_in_phase = sum(1 for d, s in zip(tdays, single, strict=True) if s and d <= end)
    res.single_after_phase = [d for d, s in zip(tdays, single, strict=True) if s and (end is None or d > end)]

    # 2. 分界点前后的跳变
    if end is not None and end in tdays and end != tdays[-1]:
        i = tdays.index(end)
        window = tdays[max(0, i - BOUNDARY_DAYS): i + BOUNDARY_DAYS + 2]
        changes = [(d, bars[p].close, bars[d].close, bars[d].close - bars[p].close)  # type: ignore[operator]
                   for p, d in pairwise(window)]
        before = [abs(c) for d, _, _, c in changes if d <= end]
        after = [abs(c) for d, _, _, c in changes if d > end]
        res.boundary_before_mean = sum(before) / len(before) if before else None
        res.boundary_after_mean = sum(after) / len(after) if after else None
        res.boundary_change = next((c for d, _, _, c in changes if d > end), None)
        med = statistics.median([abs(c) for *_, c in changes]) if changes else 0.0
        res.jumps = [Jump(d, p, v, c) for d, p, v, c in changes
                     if abs(c) >= max(JUMP_FACTOR * med, JUMP_MIN_POINTS)]

    # 3. 节假日有数据的日期
    res.holiday_dates = [d for d in dates if d.weekday() < 5 and d not in trading]
    res.holidays_outside_phase = [d for d in res.holiday_dates if end is None or d > end]

    # 4. 疑似陈旧值
    res.stale = stale_dates({d: bars[d].close for d in dates})  # type: ignore[misc]

    # 离散性对照：指标按成分股个数取值，日变化为0可能只是偶然
    res.step = NOMINAL_STEP.get(symbol[:2])
    if res.step is not None:
        after = [d for d in tdays if end is None or d > end]
        diffs = [round(abs(bars[d].close - bars[p].close), 4) for p, d in pairwise(after)]  # type: ignore[operator]
        res.zero_after_phase = sum(1 for x in diffs if x == 0)
        res.small_after_phase = sum(1 for x in diffs if 0 < x <= res.step + 1e-9)
    return res


def _dates(ds: list[dt.date], limit: int | None = None) -> str:
    if not ds:
        return "无"
    shown = ds if limit is None else ds[:limit]
    more = "" if limit is None or len(ds) <= limit else f" 等（共 {len(ds)} 个）"
    return "、".join(str(d) for d in shown) + more


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def render(results: list[QualityResult], generated_at: str) -> str:
    lines = [
        "# TradingView 广度指标数据质量检查", "",
        f"生成时间（UTC）：{generated_at}。只报告，不修改数据，不改变评分逻辑。", "",
        f"- 单值阶段：第一次出现连续 {MULTI_RUN} 个多值K线（开高低收不全相同）之前的最后一个单值K线日。",
        f"- 明显跳变：分界点前后各 {BOUNDARY_DAYS} 个交易日内，日变化绝对值 ≥ 窗口中位数的 {JUMP_FACTOR:g} 倍且 ≥ "
        f"{JUMP_MIN_POINTS:g} 个百分点。",
        "- 疑似陈旧值：NYSE 交易日的收盘值与前一个 NYSE 交易日完全相同（保留原值，不替代）。",
        "- **解读提示**：广度指标按成分股个数取值，是离散的（标普500 每只约 0.2 个百分点，纳斯达克100 约 1 个百分点），"
        "数据正常时也会偶然出现\"与前一天相同\"。对照方法：日变化分布平滑时，\"变化恰为0\"的次数约为 "
        "0<|变化|≤步长（一只成分股）次数的一半，记为偶然期望次数；实际次数明显高于期望时，才更可能是陈旧值。", "",
    ]
    by_symbol = {r.symbol: r for r in results}
    lines += ["## 参与评分的 S5FI、S5TW", ""]
    for sym in SCORING_BREADTH:
        r = by_symbol.get(sym)
        if r is None:
            lines.append(f"- {sym}：未导入")
            continue
        lines += [
            f"### {sym}",
            f"- 单值阶段结束：{r.single_phase_end or '无单值阶段'}（阶段内单值K线 {r.single_in_phase} 根；"
            f"阶段之后仍为单值的 {len(r.single_after_phase)} 根：{_dates(r.single_after_phase, 20)}）",
            f"- 节假日有数据：{_dates(r.holiday_dates)}；落在单值阶段之外：{_dates(r.holidays_outside_phase)}",
            f"- 疑似陈旧值（全部历史，{len(r.stale)} 个）：{_dates(r.stale)}",
            f"- 其中单值阶段之后（{len(r.stale_after_phase)} 个）：{_dates(r.stale_after_phase)}",
            f"- 离散性对照（单值阶段之后）：步长 {_num(r.step)}；日变化恰为0 {r.zero_after_phase} 次，"
            f"偶然期望约 {_num(r.expected_zero)} 次（0<|变化|≤步长 {r.small_after_phase} 次的一半）",
            "",
        ]
    lines += ["## 汇总", "",
              "| 指标 | 起止 | 行数 | 单值阶段结束 | 阶段后单值K线 | 分界前/后平均日变化 | 分界次日变化 | 明显跳变 | "
              "节假日数据（阶段外） | 疑似陈旧值（阶段后） | 步长 / 变化为0 / 偶然期望 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| {r.symbol} | {r.first_date} 至 {r.last_date} | {r.rows} | {r.single_phase_end or '无'} | "
            f"{len(r.single_after_phase)} | {_num(r.boundary_before_mean)} / {_num(r.boundary_after_mean)} | "
            f"{_num(r.boundary_change)} | {len(r.jumps)} | {len(r.holiday_dates)}（{len(r.holidays_outside_phase)}） | "
            f"{len(r.stale)}（{len(r.stale_after_phase)}） | {_num(r.step)} / {r.zero_after_phase} / "
            f"{_num(r.expected_zero)} |")
    lines += ["", "## 分界点前后的明显跳变", ""]
    any_jump = False
    for r in results:
        if r.jumps:
            any_jump = True
            lines.append(f"- {r.symbol}（分界 {r.single_phase_end}）：" + "；".join(
                f"{j.date} {j.previous:.2f}→{j.value:.2f}（{j.change:+.2f}）" for j in r.jumps))
    if not any_jump:
        lines.append("无")
    lines += ["", "## 节假日有数据的日期与单值阶段", ""]
    for r in results:
        if r.holiday_dates:
            status = "全部在单值阶段内" if not r.holidays_outside_phase else \
                f"阶段外 {len(r.holidays_outside_phase)} 个：{_dates(r.holidays_outside_phase)}"
            lines.append(f"- {r.symbol}：{len(r.holiday_dates)} 个，{status}")
    lines += ["", "## 疑似陈旧值（全部历史）", ""]
    for r in results:
        lines.append(f"- {r.symbol}（{len(r.stale)} 个；单值阶段之后 {len(r.stale_after_phase)} 个）："
                     f"{_dates(r.stale)}")
    return "\n".join(lines) + "\n"
