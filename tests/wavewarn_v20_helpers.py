"""波段预警 v2.0 构造测试的共用工具：只生成构造数据，不读任何文件。

期望值一律来自登记条文与构造输入；这里的函数只负责搭建输入，不产生期望值。
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn_v20.channels import ChannelDay, ChannelState
from market_risk.wavewarn_v20.convergence import (
    REGISTERED_CHANNELS,
    REGISTERED_SYSTEM,
    Candidate,
    first_valid_index,
)
from market_risk.wavewarn_v20.execution import PolicyParameters, PositionMap, SignalRecord, Weights
from market_risk.wavewarn_v20.inputs import AssetDay, InputWindows, NewLow, TrendDay, asset_days, trend_days
from market_risk.wavewarn_v20.state_machine import (
    ChannelPaths,
    Evidence,
    Risk,
    SystemDay,
    evidence_series,
    run_channels,
    run_system,
)

# 登记的窗口：63 日最高价、20 日新低（此前 19 日、至少 15 个）、200 日均线。
WINDOWS = InputWindows(high=63, low_prior=19, low_minimum=15, average=200)
START = dt.date(2001, 1, 1)
DAY = dt.date(2001, 6, 1)
CENT = Decimal("0.01")
# 登记的仓位映射：正常 0.6/0.4，一级 0.6/0，二级 0.3/0，λ = 2；止损线 96%，冷却 10 个交易日；对账容差 1e-10。
POSITIONS = PositionMap(Weights(0.6, 0.4), Weights(0.6, 0.0), Weights(0.3, 0.0), 2.0)
POLICY = PolicyParameters(stop_ratio=0.96, cooldown=10)
TOLERANCE = 1e-10


def numbered(number: int) -> dt.date:
    """“第 n 日”对应的构造日期。"""
    return START + dt.timedelta(days=number)


def signal(number: int, risk: Risk, valid: bool = True) -> SignalRecord:
    """第 n 日的信号记录；L 与计数在执行层不参与判断，置为 None。"""
    return SignalRecord(numbered(number), risk, None, None, None, valid)


def dec(value: str | int) -> Decimal:
    return Decimal(str(value))


def axis(length: int) -> tuple[dt.date, ...]:
    """构造的日期轴：连续的日历日，只用作行号的标签，不要求是真实交易日。"""
    return tuple(START + dt.timedelta(days=index) for index in range(length))


def asset(close: str | None = "100", high: str | None = "100", complete: bool = True,
          nl: NewLow = NewLow.NO, q: int = 10, day: dt.date = DAY) -> AssetDay:
    return AssetDay(day, None if close is None else dec(close), None if high is None else dec(high), complete, nl, q)


def trend(close: str | None = "100", average: str | None = "90", day: dt.date = DAY) -> TrendDay:
    """average 为 200 日均线的数值；None 表示均线不完整。"""
    total = None if average is None else 200 * dec(average)
    return TrendDay(day, None if close is None else dec(close), total, average is not None)


def channel(state: ChannelState, valid: bool = True, day: dt.date = DAY) -> ChannelDay:
    return ChannelDay(day, state, valid, ())


def evidence(spx: AssetDay | None = None, qqq: AssetDay | None = None, ma: TrendDay | None = None,
             p_spx: ChannelState = ChannelState.ARMED, p_qqq: ChannelState = ChannelState.ARMED,
             pr_spx: ChannelState = ChannelState.ARMED, pr_qqq: ChannelState = ChannelState.ARMED,
             mr: ChannelState = ChannelState.ARMED, day: dt.date = DAY) -> Evidence:
    """手工搭一份证据。通道有效性按登记的定义由输入完整性给出：

    P、PR 有效 ⇔ C 存在且 H 完整；MR 有效 ⇔ C 存在且均线完整。
    """
    spx = spx or asset(day=day)
    qqq = qqq or asset(day=day)
    ma = ma or trend(close=None if spx.close is None else str(spx.close), day=day)
    spx_valid = spx.close is not None and spx.high_complete
    qqq_valid = qqq.close is not None and qqq.high_complete
    mr_valid = ma.close is not None and ma.complete
    return Evidence(spx, qqq, ma, channel(p_spx, spx_valid, day), channel(p_qqq, qqq_valid, day),
                    channel(pr_spx, spx_valid, day), channel(pr_qqq, qqq_valid, day), channel(mr, mr_valid, day))


def random_closes(seed: int, length: int, missing: float = 0.0, warmup: int = 0) -> list[Decimal | None]:
    """构造的随机价格路径（两位小数）；warmup 之后的每一天以 missing 的概率缺价。"""
    rng = random.Random(seed)
    price = Decimal("100.00")
    result: list[Decimal | None] = []
    for index in range(length):
        price = max(Decimal("1.00"), (price * Decimal(str(1 + rng.gauss(0.0002, 0.012)))).quantize(CENT))
        result.append(None if index >= warmup and rng.random() < missing else price)
    return result


@dataclass(frozen=True)
class Model:
    days: tuple[dt.date, ...]
    spx: tuple[AssetDay, ...]
    qqq: tuple[AssetDay, ...]
    ma: tuple[TrendDay, ...]
    t0: int
    paths: ChannelPaths              # 自 t0 + 1 起
    evidence: tuple[Evidence, ...]   # 自 t0 + 1 起
    system: tuple[SystemDay, ...]    # 自 t0 + 1 起

    def position(self, index: int) -> int:
        """日期轴行号 → paths、evidence、system 里的位置。"""
        return index - self.t0 - 1


def run_model(spx_closes: list[Decimal | None], qqq_closes: list[Decimal | None], candidate: Candidate) -> Model:
    """按登记的初始快照（t0 当日全部通道已武装未激活、S 正常、计数为 0）跑完整条构造序列。"""
    days = axis(len(spx_closes))
    spx, qqq = asset_days(days, spx_closes, WINDOWS), asset_days(days, qqq_closes, WINDOWS)
    ma = trend_days(days, spx_closes, WINDOWS)
    t0 = first_valid_index(spx, qqq, ma)
    after = slice(t0 + 1, None)
    paths = run_channels(REGISTERED_CHANNELS, spx[after], qqq[after], ma[after], candidate.k, candidate.theta,
                         WINDOWS.average)
    series = evidence_series(spx[after], qqq[after], ma[after], paths)
    return Model(days, spx, qqq, ma, t0, paths, series,
                 run_system(REGISTERED_SYSTEM, series, candidate.k, candidate.h))
