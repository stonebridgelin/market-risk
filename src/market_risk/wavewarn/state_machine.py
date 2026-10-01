"""v1.2.1 系统灯色、静默计数、Ready 三版本与数据状态；v1.3 增加 X1、X2 两个黄转绿版本。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from market_risk.wavewarn.channels import ChannelDay

Light = Literal["绿", "黄", "红"]
Level = Literal["黄", "红"]
DataStatus = Literal["完整", "沿用"]


ExitVersion = Literal["P0", "E1", "E2", "E3", "X1", "X2"]
PRICE_CHANNEL_PREFIXES = ("P_", "PR_")           # 两资产的 P、PR 为价格通道，其余为非价格通道


def is_price_channel(name: str) -> bool:
    return name.startswith(PRICE_CHANNEL_PREFIXES)


@dataclass(frozen=True)
class SystemMemory:
    light: Light = "绿"
    quiet_red: int = 0
    quiet_all: int = 0
    quiet_nonprice: int = 0      # 非价格通道连续“有效且未激活”的天数（v1.3 的 X1、X2 使用）


@dataclass(frozen=True)
class ReadyInputs:
    q_spx: int
    q_qqq: int
    breadth_repaired_spx: bool | None
    breadth_repaired_qqq: bool | None
    above_ma50_spx: bool | None
    above_ma50_qqq: bool | None
    e3_guard_spx: bool | None
    downgrade_inputs_valid: bool


@dataclass(frozen=True)
class SystemDay:
    date: dt.date
    memory: SystemMemory
    data_status: DataStatus
    ready: bool
    reason: str


@dataclass(frozen=True)
class ChannelQuiet:
    """X1、X2 用到的当日通道情况。"""

    quiet_nonprice: int          # 非价格通道连续有效且未激活的天数
    has_nonprice: bool           # 模型是否含非价格通道；没有时该条件自动满足
    price_clear_today: bool      # 全部价格通道当日有效且未激活


def relaxed_ready(version: Literal["X1", "X2"], k: int, inputs: ReadyInputs, quiet: ChannelQuiet,
                  quiet_all_days: int = 5) -> bool:
    """v1.3 的 X1、X2：价格通道只看当日，非价格通道仍要连续静默；X2 再去掉“高于 MA50”。"""
    x2 = (inputs.q_spx >= k and inputs.q_qqq >= k and quiet.price_clear_today
          and (not quiet.has_nonprice or quiet.quiet_nonprice >= quiet_all_days)
          and inputs.breadth_repaired_spx is True and inputs.breadth_repaired_qqq is True)
    if version == "X2":
        return x2
    if version == "X1":
        return x2 and inputs.above_ma50_spx is True and inputs.above_ma50_qqq is True
    raise ValueError(version)


def ready_condition(version: ExitVersion, quiet_all: int, k: int,
                    inputs: ReadyInputs, quiet_all_days: int = 5,
                    quiet: ChannelQuiet | None = None) -> bool:
    """任一所需输入缺失即不满足 Ready；E3 蕴含 E2，E2 蕴含 E1；E2 蕴含 X1，X1 蕴含 X2。"""
    if version in ("X1", "X2"):
        if quiet is None:
            raise ValueError("X1、X2 需要当日通道情况")
        return relaxed_ready(version, k, inputs, quiet, quiet_all_days)  # type: ignore[arg-type]
    if version == "P0":
        return (quiet_all >= quiet_all_days and inputs.q_spx >= k and inputs.q_qqq >= k
                and inputs.above_ma50_spx is True and inputs.above_ma50_qqq is True)
    e1 = (quiet_all >= quiet_all_days and inputs.q_spx >= k and inputs.q_qqq >= k
          and inputs.breadth_repaired_spx is True and inputs.breadth_repaired_qqq is True)
    if version == "E1":
        return e1
    e2 = e1 and inputs.above_ma50_spx is True and inputs.above_ma50_qqq is True
    if version == "E2":
        return e2
    if version == "E3":
        return e2 and inputs.e3_guard_spx is True
    raise ValueError(version)


def step_system(day: dt.date, previous: SystemMemory, channels: Mapping[str, tuple[Level, ChannelDay]],
                inputs: ReadyInputs, k: int, e_version: ExitVersion,
                red_quiet_days: int = 3, all_quiet_days: int = 5) -> SystemDay:
    """通道先更新；系统升级可跨级，降级每天最多一级。"""
    if not channels:
        raise ValueError("系统至少需要一个通道")
    red_rows = [state for level, state in channels.values() if level == "红"]
    all_rows = [state for _, state in channels.values()]
    price_rows = [state for name, (_, state) in channels.items() if is_price_channel(name)]
    other_rows = [state for name, (_, state) in channels.items() if not is_price_channel(name)]
    quiet_red = previous.quiet_red + 1 if all(row.valid and row.status != "active" for row in red_rows) else 0
    quiet_all = previous.quiet_all + 1 if all(row.valid and row.status != "active" for row in all_rows) else 0
    quiet_nonprice = (previous.quiet_nonprice + 1
                      if all(row.valid and row.status != "active" for row in other_rows) else 0)
    red = any(row.status == "active" for row in red_rows)
    yellow = any(level == "黄" and row.status == "active" for level, row in channels.values())
    quiet = ChannelQuiet(quiet_nonprice, bool(other_rows),
                         all(row.valid and row.status != "active" for row in price_rows))
    ready = ready_condition(e_version, quiet_all, k, inputs, all_quiet_days, quiet)
    status: DataStatus = "完整" if all(row.valid for row in all_rows) and inputs.downgrade_inputs_valid else "沿用"
    if previous.light == "绿":
        light: Light = "红" if red else "黄" if yellow else "绿"
        reason = "红通道激活" if red else "黄通道激活" if yellow else "维持绿灯"
    elif previous.light == "黄":
        light = "红" if red else "绿" if ready else "黄"
        reason = "红通道激活" if red else "Ready成立" if ready else "维持黄灯"
    else:
        light = "红" if red or quiet_red < red_quiet_days or inputs.q_spx < k or inputs.q_qqq < k else "黄"
        reason = "红通道激活或红灯降级条件不足" if light == "红" else "红灯静默与两资产Q满足"
    return SystemDay(day, SystemMemory(light, quiet_red, quiet_all, quiet_nonprice), status, ready, reason)
