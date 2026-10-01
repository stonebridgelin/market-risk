"""v1.2.1 研究配置；业务选择留空时主损失必须拒绝计算。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class BusinessParameters:
    eta: Decimal
    e_version: str
    kappa_d: Decimal
    beta: Decimal


@dataclass(frozen=True)
class ChannelSelection:
    b_spx: bool
    dv_spx: bool
    b_qqq: bool
    dv_qqq: bool


@dataclass(frozen=True)
class FixedParameters:
    anchor: int
    anchor_audit: int
    new_low_prior: int
    new_low_min_valid: int
    breadth_reference: int
    breadth_reference_min_valid: int
    breadth_median: int
    breadth_median_min_valid: int
    ma50: int
    ma200: int
    breadth_repair: int
    volatility_exit_short: int
    volatility_exit_long: int
    e3_high_window: int
    breadth_min_drawdown: Decimal
    divergence_max_drawdown: Decimal
    divergence_delta10_max: Decimal
    collapse_min_drawdown: Decimal
    collapse_breadth_max: Decimal
    collapse_repair_breadth: Decimal
    volatility_enter_ratio: Decimal
    volatility_exit_ratio: Decimal
    e3_near_high: Decimal
    quiet_red_days: int
    quiet_all_days: int
    noise_floor: Decimal
    switch_cost_fraction_of_kappa_d: Decimal
    mu: Decimal
    weight_spx: Decimal
    weight_qqq: Decimal


@dataclass(frozen=True)
class CandidateSets:
    k: tuple[int, ...]
    theta_p: tuple[Decimal, ...]
    q: tuple[Decimal, ...]


@dataclass(frozen=True)
class PairedParameters:
    mean_block_length: int
    short_block_length: int
    long_block_length: int
    resamples: int
    seed_main: int
    seed_block_10: int
    seed_block_40: int
    half_split: dt.date
    event_padding: int


@dataclass(frozen=True)
class WavewarnConfig:
    raw: dict[str, Any]

    def development_end(self) -> dt.date:
        """开发期截止日必须与登记规格一致；解析后传给全部正式研究读取路径。"""
        if str(self.raw.get("development_end")) != dt.date(2016, 12, 30).isoformat():
            raise ValueError("固定参数 development_end 与 v1.2.1 开发期边界不一致")
        return dt.date(2016, 12, 30)

    def fixed_parameters(self) -> FixedParameters:
        """读取固定参数，并逐项拒绝与登记规格不一致的值。"""
        self.development_end()
        expected: tuple[tuple[str, str, str, type], ...] = (
            ("windows", "anchor", "63", int), ("windows", "anchor_audit", "126", int),
            ("windows", "new_low_prior", "19", int), ("windows", "new_low_min_valid", "15", int),
            ("windows", "breadth_reference", "252", int),
            ("windows", "breadth_reference_min_valid", "200", int),
            ("windows", "breadth_median", "60", int),
            ("windows", "breadth_median_min_valid", "50", int),
            ("windows", "ma50", "50", int), ("windows", "ma200", "200", int),
            ("windows", "breadth_repair", "3", int),
            ("windows", "volatility_exit_short", "3", int),
            ("windows", "volatility_exit_long", "10", int),
            ("windows", "e3_high_window", "252", int),
            ("channel_thresholds", "breadth_min_drawdown", "0.01", Decimal),
            ("channel_thresholds", "divergence_max_drawdown", "0.01", Decimal),
            ("channel_thresholds", "divergence_delta10_max", "-10", Decimal),
            ("channel_thresholds", "collapse_min_drawdown", "0.02", Decimal),
            ("channel_thresholds", "collapse_breadth_max", "30", Decimal),
            ("channel_thresholds", "collapse_repair_breadth", "40", Decimal),
            ("channel_thresholds", "volatility_enter_ratio", "1", Decimal),
            ("channel_thresholds", "volatility_exit_ratio", "0.95", Decimal),
            ("channel_thresholds", "e3_near_high", "0.10", Decimal),
            ("quiet", "red_days", "3", int), ("quiet", "all_days", "5", int),
            ("loss", "noise_floor", "0.02", Decimal),
            ("loss", "switch_cost_fraction_of_kappa_d", "0.0025", Decimal),
            ("loss", "mu", "0", Decimal), ("loss", "weight_spx", "0.5", Decimal),
            ("loss", "weight_qqq", "0.5", Decimal),
        )
        values: list[int | Decimal] = []
        for section, name, wanted, kind in expected:
            raw = self.raw.get(section, {}).get(name)
            if raw is None:
                raise ValueError(f"缺少固定参数 {section}.{name}")
            value = kind(str(raw))
            if value != kind(wanted):
                raise ValueError(f"固定参数 {section}.{name} 与 v1.2.1 规格不一致")
            values.append(value)
        if self.zz_thresholds() != {"SPX": (Decimal("0.04"), Decimal("0.05")),
                                    "QQQ": (Decimal("0.05"), Decimal("0.065"))}:
            raise ValueError("ZZ 固定门槛与 v1.2.1 规格不一致")
        return FixedParameters(*values)

    def candidate_sets(self) -> CandidateSets:
        """候选集合与登记顺序均须精确匹配规格。"""
        raw = self.raw.get("candidates", {})
        candidates = CandidateSets(tuple(int(value) for value in raw.get("k", ())),
                                   tuple(Decimal(str(value)) for value in raw.get("theta_p", ())),
                                   tuple(Decimal(str(value)) for value in raw.get("q", ())))
        if (candidates.k != (3, 5, 10)
                or candidates.theta_p != (Decimal("0.015"), Decimal("0.02"), Decimal("0.025"))
                or candidates.q != (Decimal("0.10"), Decimal("0.20"))):
            raise ValueError("候选 K、θ_P、q 或登记顺序与 v1.2.1 规格不一致")
        return candidates

    def paired_parameters(self) -> PairedParameters:
        """固定平稳自助抽样口径与种子。"""
        raw = self.raw.get("paired_test", {})
        expected = (("mean_block_length", 20), ("short_block_length", 10),
                    ("long_block_length", 40), ("resamples", 10_000),
                    ("seed_main", 20260929), ("seed_block_10", 20260910),
                    ("seed_block_40", 20260940), ("event_padding", 20))
        values = []
        for name, wanted in expected:
            if raw.get(name) is None or int(raw[name]) != wanted:
                raise ValueError(f"配对检验参数 paired_test.{name} 与规格不一致")
            values.append(wanted)
        if str(raw.get("half_split")) != dt.date(2020, 1, 1).isoformat():
            raise ValueError("配对检验参数 paired_test.half_split 与规格不一致")
        return PairedParameters(*values[:-1], dt.date(2020, 1, 1), values[-1])

    def zz_thresholds(self) -> dict[str, tuple[Decimal, Decimal]]:
        """ZZ 下跌与反弹门槛分别读取，拒绝缺失或非正值。"""
        values = self.raw.get("zz", {})
        result = {}
        for symbol, prefix in (("SPX", "spx"), ("QQQ", "qqq")):
            decline = values.get(f"{prefix}_decline")
            rebound = values.get(f"{prefix}_rebound")
            if decline is None or rebound is None:
                raise ValueError(f"{symbol} ZZ 下跌和反弹门槛均须配置")
            thresholds = (Decimal(str(decline)), Decimal(str(rebound)))
            if any(value <= 0 for value in thresholds):
                raise ValueError(f"{symbol} ZZ 门槛必须为正")
            result[symbol] = thresholds
        return result

    def require_business_parameters(self) -> BusinessParameters:
        business = self.raw.get("business", {})
        if not all(business.get(name) is not None for name in ("eta", "e_version", "kappa_d", "beta")):
            raise ValueError("η、E版本、κ_D、β待负责人确认；拒绝计算主损失")
        eta = Decimal(str(business["eta"]))
        kappa_d = Decimal(str(business["kappa_d"]))
        beta = Decimal(str(business["beta"]))
        e_version = str(business["e_version"])
        if not (Decimal(0) <= eta <= Decimal(1)):
            raise ValueError("η 必须在0至1之间")
        if e_version not in ("E1", "E2", "E3"):
            raise ValueError("E版本必须是 E1、E2 或 E3")
        if not (kappa_d >= beta > 0):
            raise ValueError("κ_D、β 必须满足 κ_D ≥ β > 0")
        return BusinessParameters(eta, e_version, kappa_d, beta)

    def require_channel_selection(self) -> ChannelSelection:
        selection = self.raw.get("channel_selection", {})
        names = ("b_spx", "dv_spx", "b_qqq", "dv_qqq")
        if any(type(selection.get(name)) is not bool for name in names):
            raise ValueError("N 的 B、DV 分侧去留待开发期预登记研究确认；须显式填写四侧开关")
        return ChannelSelection(*(selection[name] for name in names))


def load_wavewarn_config(path: Path) -> WavewarnConfig:
    with path.open(encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    if not isinstance(raw, dict) or raw.get("version") != "v1.2.1":
        raise ValueError("wavewarn v1.2.1 配置格式或版本错误")
    if raw.get("nl_unknown_policy") != "three_valued":
        raise ValueError("20日新低三值口径须使用 three_valued")
    return WavewarnConfig(raw)
