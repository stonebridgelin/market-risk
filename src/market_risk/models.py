"""核心数据模型（SPEC 第4节）。

约定：
- 所有百分比统一用"百分数"表示（4.11 表示 4.11%）；计算 bp 时乘以 100。
- 数据类优先 frozen=True；序列字段使用 tuple，保证不可变。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class DateReferences:
    """一个基准日的全部日期参照（SPEC 5.2 / SOP 第6节）。"""

    base_date: dt.date
    t_minus_5: dt.date
    t_minus_20: dt.date
    window_start: dt.date                      # T−19
    window_end: dt.date                        # T
    window_days: tuple[dt.date, ...]           # 20日窗口内的全部股票交易日
    oas_o1: dt.date                            # 基准日之前最近一个债市营业日
    oas_o6_v3r1: dt.date                       # O1 之前第5个债市营业日
    oas_o1_v2m: dt.date | None                 # 基准日之前最新的有数值观测；无列表时为 None
    oas_o6_v2m: dt.date | None                 # 从 v2-M 的 O1 往前数第5个有数值观测
    oas_o1_to_o6_sequence: tuple[dt.date, ...]  # 从 O1 往前的债市营业日：O1, O1−1, …, O6
    o1_lag_stock_days: int                     # O1 之后（不含）至基准日（含）的股票交易日个数
    o1_v2m_lag_stock_days: int | None          # 同上，针对 v2-M 的 O1（SPEC 5.6 第4条）
    three_segment_query_start: dt.date         # T−45
    outcome_window_start: dt.date              # 基准日之后第1个股票交易日
    outcome_window_end: dt.date                # 基准日之后第20个股票交易日
    stock_holidays_in_window: tuple[dt.date, ...]
    bond_holidays_in_window: tuple[dt.date, ...]
    is_early_close: bool
    is_last_trading_day_of_week: bool


@dataclass(frozen=True)
class SourceInfo:
    """一次数据获取的来源信息（写入缓存元数据与 meta.json）。URL 中不含密钥。"""

    source: str                    # yahoo / fred / treasury / cboe
    key: str                       # 代码或系列名
    url: str
    downloaded_at_utc: str         # ISO 8601
    rows: int
    data_start: dt.date | None
    data_end: dt.date | None       # 数据截止日期
    from_cache: bool
    cache_file: str


@dataclass(frozen=True)
class EtfSnapshot:
    """一只 ETF 在基准日的收盘价与简单均线（不复权 Close）。"""

    symbol: str
    close: Decimal | float
    ma5: Decimal | float
    ma10: Decimal | float
    ma20: Decimal | float
    ma30: Decimal | float
    ma50: Decimal | float
    ma200: Decimal | float
    closes: tuple[tuple[dt.date, Decimal | float], ...]  # 截至基准日（含）的逐日收盘价


@dataclass(frozen=True)
class BreadthReading:
    """手工录入的广度读数（百分数，58.44 表示 58.44%）。"""

    date: dt.date
    s5fi: Decimal | float
    s5tw: Decimal | float
    source: str = "manual"
    note: str = ""
    stale_fields: tuple[str, ...] = ()   # 预留：疑似陈旧值字段；S5FI、S5TW 重复值属偶然，不再触发（SPEC 5.6 第11条）


@dataclass(frozen=True)
class ThreeSegmentTrace:
    """三环节遍历中一个候选 d1 的记录（SPEC 5.4）。"""

    symbol: str
    d1: dt.date
    d1_close: Decimal | float
    lc: Decimal | float
    lc_date: dt.date
    step1: bool
    d2_dates: tuple[dt.date, ...]
    step3: bool
    completed: bool


@dataclass(frozen=True)
class ThreeSegmentResult:
    """一只 ETF 按一种 d1 口径的三环节结果。"""

    symbol: str
    d1_includes_t_minus_20: bool
    traces: tuple[ThreeSegmentTrace, ...]

    @property
    def completed(self) -> bool:
        return any(t.completed for t in self.traces)


@dataclass(frozen=True)
class DimensionScore:
    """一个维度的评分（SPEC 第4节）。score 为 None 表示待补。"""

    name: str                                  # 价格 / 广度 / VIX / 利率 / 信用
    score: int | None
    possible_scores: tuple[int, ...]           # 确定时只有一个元素
    triggered_conditions: tuple[str, ...]      # 如 ("2分(a)",)
    calculation: str
    pending_reason: str | None = None


@dataclass(frozen=True)
class ScoreResult:
    """一个规则版本的评分结果（SPEC 第4节）。"""

    version: str                               # "v2-M" / "v3-R1"
    price: DimensionScore
    breadth: DimensionScore
    vix: DimensionScore
    rates: DimensionScore
    credit: DimensionScore
    total: int | None                          # 全部确定时
    total_range: tuple[int, int]               # (下限, 上限)；确定时两者相等
    stage: str | None                          # 范围跨越阶段时为 None
    clear_deterioration: tuple[tuple[str, str], ...]  # 证据链：(项目, 是/否/无法核验)
    notes: tuple[str, ...] = ()
    review_flags: tuple[str, ...] = ()         # 需要用户判断的事项（不自动处理）

    @property
    def dimensions(self) -> tuple[DimensionScore, ...]:
        return (self.price, self.breadth, self.vix, self.rates, self.credit)

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(d.name for d in self.dimensions if d.score is None)


@dataclass(frozen=True)
class NearThresholdItem:
    """贴近门槛的读数（SPEC 第7节）。"""

    item: str                                  # 如 "SPY 收盘价 vs MA50"、"ΔOAS（v2-M）"
    value: float
    threshold: float
    gap: float                                 # value − threshold，单位见 unit
    unit: str                                  # "%"、"个百分点"、"bp" 等


@dataclass(frozen=True)
class OasVintageValue:
    """OAS 历史修订比对（SPEC 5.6 第3条）：某个 O1/O6 在基准日版本与当前版本中的数值。"""

    label: str                       # 如 "v3-R1 O1"、"v2-M O6"
    date: dt.date
    vintage_value: float | None      # 基准日版本（ALFRED）；版本中没有时为 None
    current_value: float | None      # 当前版本（计分使用）

    @property
    def comparable(self) -> bool:
        """两个版本都有数值才能比对。"""
        return self.vintage_value is not None and self.current_value is not None

    @property
    def revised(self) -> bool:
        return self.comparable and self.vintage_value != self.current_value


@dataclass(frozen=True)
class MarketSnapshot:
    """截至基准日的全部评分输入（SPEC 第4节）。缺失值为 None，由评分记待补。"""

    refs: DateReferences
    etfs: dict[str, EtfSnapshot]               # SPY / QQQ / RSP
    breadth: BreadthReading | None
    breadth_t5: BreadthReading | None
    vix: float | None
    vix_t5: float | None
    yields: dict[dt.date, Decimal | float]     # 利率窗口内逐日财政部数值，另含 T−20
    y: Decimal | float | None
    h: Decimal | float | None
    h_date: dt.date | None                     # 并列最高时为最早一天
    h_dates: tuple[dt.date, ...]               # 全部并列最高日期
    y_t20: Decimal | float | None
    oas_o1: Decimal | float | None             # v3-R1 的 O1 数值
    oas_o6_v3r1: Decimal | float | None
    oas_o1_v2m: Decimal | float | None
    oas_o6_v2m: Decimal | float | None
    oas_vintage: tuple[OasVintageValue, ...]   # 历史修订比对；未取得版本数据时为空
    hyg_lqd: Decimal | float | None
    spy_window_max_close: Decimal | float | None  # 20日窗口内 SPY 最高收盘价（v2-M 广度 b）
    three_segment: dict[bool, tuple[ThreeSegmentResult, ...]]  # 键：d1 是否包含 T−20
    data_notes: tuple[str, ...]
    mode: str = "backtest"                     # backtest / daily
    # 评分 ETF 当日或回看窗口内缺数据（数据集已覆盖基准日）：(标的, 原因)。价格维度记待补（准备层 prepare.py）
    missing_etfs: tuple[tuple[str, str], ...] = ()
