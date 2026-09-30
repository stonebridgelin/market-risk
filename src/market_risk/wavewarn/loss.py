"""v1.2.1 主损失的逐资产价格项与可复算日度分解。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.config import WavewarnConfig
from market_risk.wavewarn.execution import ExecutionDay
from market_risk.wavewarn.labels_zz import ZZEvent, dangerous_interval, right_censored_unknown


@dataclass(frozen=True)
class LossParameters:
    kappa_d: Decimal
    beta: Decimal
    eta: Decimal
    noise_floor: Decimal = Decimal("0.02")
    switch_fraction: Decimal = Decimal("0.0025")

    @property
    def kappa_0(self) -> Decimal:
        return self.beta / 2

    @property
    def gamma(self) -> Decimal:
        return self.kappa_d * self.switch_fraction


@dataclass(frozen=True)
class LossSettings:
    parameters: LossParameters
    mu: Decimal
    weight_spx: Decimal
    weight_qqq: Decimal

    @property
    def weights(self) -> dict[str, Decimal]:
        return {"SPX": self.weight_spx, "QQQ": self.weight_qqq}


def configured_loss_settings(config: WavewarnConfig) -> LossSettings:
    """正式计算入口先确认业务选择与固定口径，不使用函数默认值代替配置。"""
    fixed = config.fixed_parameters()
    business = config.require_business_parameters()
    return LossSettings(LossParameters(business.kappa_d, business.beta, business.eta,
                                      fixed.noise_floor, fixed.switch_cost_fraction_of_kappa_d),
                        fixed.mu, fixed.weight_spx, fixed.weight_qqq)


@dataclass(frozen=True)
class AssetLossDay:
    start: dt.date
    end: dt.date
    exposure: Decimal
    log_return: Decimal | None
    dangerous: bool | None
    drawdown_increment: Decimal
    danger_loss: Decimal
    drawdown_loss: Decimal
    opportunity_loss: Decimal
    excluded_reason: str

    @property
    def price_loss(self) -> Decimal:
        return self.danger_loss + self.drawdown_loss + self.opportunity_loss


def noise_drawdown(reference_high: Decimal, close: Decimal, floor: Decimal = Decimal("0.02")) -> Decimal:
    """百分比边界先用 Decimal 精确比较，恰好2%时为零。"""
    if reference_high <= 0 or close <= 0:
        raise ValueError("收盘价与参考高点必须为正")
    if close / reference_high >= 1 - floor:
        return Decimal(0)
    return ((1 - floor) * reference_high / close).ln()


def asset_price_loss(days: Sequence[dt.date], closes: Sequence[Decimal | None],
                     executions: Sequence[ExecutionDay], events: Sequence[ZZEvent],
                     params: LossParameters) -> tuple[AssetLossDay, ...]:
    """逐日处理结束日重置、危险区间、跨缺价及三项价格损失。"""
    if len(days) != len(closes) or len(days) != len(executions):
        raise ValueError("日期、收盘价与执行数量不一致")
    if not days or closes[0] is None:
        raise ValueError("评价窗口首日须有有效收盘价")
    if params.kappa_d < params.beta or params.beta <= 0:
        raise ValueError("κ_D、β 必须满足 κ_D ≥ β > 0")
    danger_dates: set[dt.date] = set()
    unknown_dates: set[dt.date] = set()
    end_dates: set[dt.date] = set()
    for event in events:
        danger_dates.update(dangerous_interval(event, days))
        unknown_dates.update(right_censored_unknown(event, days, days[-1]))
        if event.end_date is not None:
            end_dates.add(event.end_date)
    high = closes[0]
    assert high is not None
    record = Decimal(0)
    rows: list[AssetLossDay] = []
    for index in range(len(days) - 1):
        day = days[index]
        current = closes[index]
        following = closes[index + 1]
        if current is not None:
            if day in end_dates or current >= high:
                high, record = current, Decimal(0)
            if index > 0 and closes[index - 1] is None:
                # 恢复日先衔接回撤纪录，但跨缺价的跌幅不收费。
                record = max(record, noise_drawdown(high, current, params.noise_floor))
        if current is None or following is None:
            rows.append(AssetLossDay(day, days[index + 1], executions[index].exposure, None,
                                     None if day in unknown_dates else day in danger_dates,
                                     Decimal(0), Decimal(0), Decimal(0), Decimal(0), "跨缺价区间"))
            continue
        if day in unknown_dates:
            rows.append(AssetLossDay(day, days[index + 1], executions[index].exposure, None, None,
                                     Decimal(0), Decimal(0), Decimal(0), Decimal(0), "右截尾后危险归属未知"))
            continue
        return_log = (following / current).ln()
        drawdown = noise_drawdown(high, following, params.noise_floor)
        increment = max(drawdown - record, Decimal(0))
        record = max(record, drawdown)
        exposure = executions[index].exposure
        dangerous = day in danger_dates
        danger_loss = params.kappa_d * exposure * max(-return_log, Decimal(0)) if dangerous else Decimal(0)
        drawdown_loss = params.kappa_0 * exposure * increment if not dangerous else Decimal(0)
        opportunity_loss = params.beta * (1 - exposure) * return_log if not dangerous else Decimal(0)
        rows.append(AssetLossDay(day, days[index + 1], exposure, return_log, dangerous,
                                 increment, danger_loss, drawdown_loss, opportunity_loss, ""))
    return tuple(rows)


def weighted_price_loss(assets: Mapping[str, Sequence[AssetLossDay]],
                        weights: Mapping[str, Decimal]) -> Decimal:
    """资产分别计算后按固定权重相加；被排除资产日价格项为零。"""
    if set(assets) != set(weights):
        raise ValueError("资产与权重不一致")
    return sum((weights[symbol] * sum((row.price_loss for row in rows), Decimal(0))
                for symbol, rows in assets.items()), Decimal(0))


def switch_loss(executions: Sequence[ExecutionDay], gamma: Decimal) -> Decimal:
    """切换按系统执行状态计，与单一资产是否缺价无关。"""
    return gamma * sum(row.switched for row in executions)


def full_exposure_events(events: Sequence[ZZEvent], days: Sequence[dt.date],
                         executions: Sequence[ExecutionDay]) -> tuple[ZZEvent, ...]:
    """已确认事件的整个危险区间均满暴露即触发；含缺价时保持的暴露。"""
    if len(days) != len(executions):
        raise ValueError("交易日与执行数量不一致")
    positions = {day: index for index, day in enumerate(days)}
    triggered = []
    for event in events:
        if event.right_censored:
            continue
        if event.peak_date not in positions or event.trough_date not in positions:
            raise ValueError("事件危险区间不在完整执行时间轴内")
        start, stop = positions[event.peak_date], positions[event.trough_date]
        if all(executions[index].exposure == 1 for index in range(start, stop)):
            triggered.append(event)
    return tuple(triggered)


@dataclass(frozen=True)
class MainLossDay:
    date: dt.date
    danger_loss: Decimal
    drawdown_loss: Decimal
    opportunity_loss: Decimal
    switch_cost: Decimal
    full_exposure_cost: Decimal

    @property
    def total(self) -> Decimal:
        return (self.danger_loss + self.drawdown_loss + self.opportunity_loss
                + self.switch_cost + self.full_exposure_cost)


def daily_main_loss(days: Sequence[dt.date], assets: Mapping[str, Sequence[AssetLossDay]],
                    executions: Sequence[ExecutionDay], weights: Mapping[str, Decimal],
                    params: LossParameters, full_exposure: Sequence[ZZEvent] = (),
                    mu: Decimal = Decimal(0)) -> tuple[MainLossDay, ...]:
    """按日合并两资产价格项、系统切换项和已确认事件的满暴露项。"""
    if set(assets) != set(weights):
        raise ValueError("资产与权重不一致")
    if len(executions) != len(days) or any(len(rows) != len(days) - 1 for rows in assets.values()):
        raise ValueError("主损失各序列未对齐")
    penalty_by_date: dict[dt.date, int] = {}
    positions = {day: index for index, day in enumerate(days)}
    for event in full_exposure:
        if event.trough_date not in positions or positions[event.trough_date] == 0:
            raise ValueError("满暴露罚项无法归属 Tr−1")
        last_interval_start = days[positions[event.trough_date] - 1]
        penalty_by_date[last_interval_start] = penalty_by_date.get(last_interval_start, 0) + 1
    result = []
    for index, day in enumerate(days):
        price_rows = ([(weights[symbol], rows[index]) for symbol, rows in assets.items()]
                      if index < len(days) - 1 else [])
        danger = sum((weight * row.danger_loss for weight, row in price_rows), Decimal(0))
        drawdown = sum((weight * row.drawdown_loss for weight, row in price_rows), Decimal(0))
        opportunity = sum((weight * row.opportunity_loss for weight, row in price_rows), Decimal(0))
        result.append(MainLossDay(day, danger, drawdown, opportunity,
                                  params.gamma if executions[index].switched else Decimal(0),
                                  mu * penalty_by_date.get(day, 0)))
    return tuple(result)
