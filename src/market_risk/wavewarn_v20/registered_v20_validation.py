"""阶段五验证期与保留期运行的登记值（V1 设计稿定稿第二节、第四节、第九节；锁定补充记录（一）第二节、第六节）。
算法侧，纯常量与组装。

- 共用登记值一律从 registered_v20 导入，不复制数值：历史起点、初始状态、输入窗口、仓位、执行政策、共同起点偏移、
  容差、R2 规则与门槛、运行参数。
- 冻结候选从登记的 27 组中按 K = 3、θ_P = 0.025、h = 3 取出，恰有一个（锁定记录第一节）。
- 固定起点 j₀ 与截止日 E 以锁定补充记录（一）第二节为唯一来源（批准版 SHA-256 见 LOCK_SUPPLEMENT_SHA256）；
  预期的首个信号日、R2 状态起日与首末计入区间也照该节写定，由入口在组合层返回后逐项断言（第六节第 2 条）。
- 确认性检验参数照 V1 设计稿第四节与《验证期通过标准》第二节第 2 小节“甲选项的具体规定”。
- 保留期模式默认禁用；启用条件由入口检查（V1 第九节；裁决第（3）项）。
- 不读文件、不读配置；不提供任何覆盖参数（不接受命令行或环境变量修改登记值）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn_v20.confirmatory import BlockSetting, ConfirmatoryParameters
from market_risk.wavewarn_v20.convergence import Candidate
from market_risk.wavewarn_v20.registered_v20 import (
    CANDIDATES,
    CONTINUITY,
    HISTORIES,
    INITIAL,
    OFFSET,
    POLICY,
    POSITIONS,
    R2_RULE,
    R2_THRESHOLDS,
    TOLERANCE,
    WINDOWS,
    run_parameters,
)
from market_risk.wavewarn_v20.research_run import Purpose, WindowSpec

__all__ = (
    "CANDIDATES", "CONTINUITY", "HISTORIES", "INITIAL", "OFFSET", "POLICY", "POSITIONS", "R2_RULE", "R2_THRESHOLDS",
    "TOLERANCE", "WINDOWS", "run_parameters",
)

# ---- 依据文件的批准版 SHA-256（写入运行记录；V2 指令第四节第 1 条）----
LOCK_SUPPLEMENT_SHA256 = "39de913cc7b765e3c0c1158b5c9a8630c7dfaa7ae9c9b45c5bee903440facf84"   # 锁定补充记录（一）
STANDARD_R3_SHA256 = "652a5c5e17230f9dad28956d86646f8f67f728fefc7ff6555ea73fa0f240511e"       # 验证期通过标准修订三
V1_DESIGN_SHA256 = "9b1a054276c7389b79b1da0d2d9f1973c08924ac99484ad3fb0e4de49fb8ae32"         # V1 设计稿定稿
LOCK_RECORD_SHA256 = "f62d9670fd8485e852106de6a727958013f5c59fbc1e20564aee8a6071c34ba1"        # 锁定记录定稿

# ---- 冻结候选（锁定记录第一节）：从登记的 27 组中取出，恰有一个 ----
_FROZEN = tuple(item for item in CANDIDATES if (item.k, item.theta, item.h) == (3, Decimal("0.025"), 3))
if len(_FROZEN) != 1:
    raise ValueError(f"登记候选中 K = 3、θ_P = 0.025、h = 3 的组须恰有一个，实际 {len(_FROZEN)} 个")
FROZEN_CANDIDATE: Candidate = _FROZEN[0]

# ---- 验证期（锁定补充记录（一）第二节）----
PURPOSE = Purpose.FROZEN
CUTOFF = dt.date(2022, 12, 30)                         # E；标签与 R2 事件截止日
FIRST_RETURN_DAY = dt.date(2016, 12, 30)               # j₀
FIRST_SIGNAL_DAY = dt.date(2016, 12, 29)               # f = j₀ − 1：R2 左截断首日、诊断窗口首日、置零首日
STATUS_FIRST_DAY = dt.date(2016, 12, 28)               # R2 状态起日（j₀ − 2）
FIRST_INTERVAL = (dt.date(2016, 12, 30), dt.date(2017, 1, 3))      # 首个计入收益区间（收盘 → 收盘）
LAST_INTERVAL = (dt.date(2022, 12, 29), dt.date(2022, 12, 30))     # 最后计入收益区间

# ---- 保留期（锁定补充记录（一）第二节；默认禁用）----
HOLDOUT_CUTOFF = dt.date(2026, 9, 25)
HOLDOUT_FIRST_RETURN_DAY = dt.date(2022, 12, 30)
HOLDOUT_FIRST_SIGNAL_DAY = dt.date(2022, 12, 29)
HOLDOUT_STATUS_FIRST_DAY = dt.date(2022, 12, 28)
HOLDOUT_FIRST_INTERVAL = (dt.date(2022, 12, 30), dt.date(2023, 1, 3))
HOLDOUT_LAST_INTERVAL = (dt.date(2026, 9, 24), dt.date(2026, 9, 25))

# ---- 确认性检验参数（V1 第四节；标准第二节第 2 小节“甲选项的具体规定”第 4、5 条）----
RESAMPLES = 10_000
ALPHA = 0.05
WARNING_P = 0.10
ANNUAL_DAYS = 252
MINIMUM_GROWTH = 0.01
CONFIRMATORY_TOLERANCE = 1e-10
PADDING = 20
VALIDATION_SPLIT = dt.date(2020, 1, 1)                 # 区间末日早于它的属于前一半（2017—2019 / 2020—2022）
HOLDOUT_SPLIT = dt.date(2025, 1, 1)                    # 2023-01-03 至 2024-12-31 / 2025-01-02 至 2026-09-25


def confirmatory_parameters(main: BlockSetting, sensitivities: tuple[BlockSetting, ...],
                            split: dt.date) -> ConfirmatoryParameters:
    return ConfirmatoryParameters(main, sensitivities, RESAMPLES, ALPHA, WARNING_P, ANNUAL_DAYS, MINIMUM_GROWTH,
                                  CONFIRMATORY_TOLERANCE, PADDING, split)


VALIDATION_CONFIRMATORY = confirmatory_parameters(
    BlockSetting(20, 20261020),
    (BlockSetting(10, 20261010), BlockSetting(40, 20261040), BlockSetting(60, 20261060), BlockSetting(120, 202610120)),
    VALIDATION_SPLIT)
HOLDOUT_CONFIRMATORY = confirmatory_parameters(
    BlockSetting(20, 20261120),
    (BlockSetting(10, 20261110), BlockSetting(40, 20261140), BlockSetting(60, 20261160), BlockSetting(120, 202611120)),
    HOLDOUT_SPLIT)

# ---- 重叠比对的基准文件（V2 指令第四节第 1、3 条；A，项目负责人读自 M3 清单）----
M3_FORMAL_NAME = "evaluation_development"
MANIFEST_NAME = "MANIFEST.sha256"
SIGNALS_NAME = "daily_signals.csv.gz"
M3_MANIFEST_SHA256 = "a8453dd505280af75e0bf108baf2b4bac431886e5deeb4964fbeb19a9b543183"
M3_SIGNALS_SHA256 = "1af662faba75587f8e947cbb97ed19e23b8f7d55ca95cdb06e794f672185c768"
M3_SIGNALS_BYTES = 375_434
SIGNALS_HEADER = ("date", "object", "risk", "level", "c1", "c2", "all_valid")

# ---- 两种模式 ----
VALIDATION_MODE = "validation"
HOLDOUT_MODE = "holdout"


@dataclass(frozen=True)
class PeriodSpec:
    """一种评价期的全部登记值。入口与组合步骤只经它取用日期与检验参数（构造测试可给出构造的评价期）。"""

    mode: str                                   # validation 或 holdout
    period: str                                 # 措辞用的期间名称：验证期、保留期
    cutoff: dt.date                             # E
    first_return_day: dt.date                   # j₀
    first_signal_day: dt.date                   # 预期 f = j₀ − 1
    status_first_day: dt.date                   # 预期 R2 状态起日 j₀ − 2
    first_interval: tuple[dt.date, dt.date]     # 预期首个计入收益区间（起点收盘, 末日收盘）
    last_interval: tuple[dt.date, dt.date]      # 预期最后计入收益区间
    confirmatory: ConfirmatoryParameters
    formal_name: str                            # 正式目录名（研究目录之下）


VALIDATION = PeriodSpec(VALIDATION_MODE, "验证期", CUTOFF, FIRST_RETURN_DAY, FIRST_SIGNAL_DAY, STATUS_FIRST_DAY,
                        FIRST_INTERVAL, LAST_INTERVAL, VALIDATION_CONFIRMATORY, "evaluation_validation")
HOLDOUT = PeriodSpec(HOLDOUT_MODE, "保留期", HOLDOUT_CUTOFF, HOLDOUT_FIRST_RETURN_DAY, HOLDOUT_FIRST_SIGNAL_DAY,
                     HOLDOUT_STATUS_FIRST_DAY, HOLDOUT_FIRST_INTERVAL, HOLDOUT_LAST_INTERVAL, HOLDOUT_CONFIRMATORY,
                     "evaluation_holdout")
PERIODS = {VALIDATION_MODE: VALIDATION, HOLDOUT_MODE: HOLDOUT}


def window_spec(period: PeriodSpec) -> WindowSpec:
    """冻结候选评价的窗口：用途 FROZEN、固定起点 j₀、E 为截止日、登记历史起点、登记初始状态、完整交易日轴。"""
    return WindowSpec(PURPOSE, period.first_return_day, period.cutoff, HISTORIES, INITIAL, CONTINUITY)


def _block(setting: BlockSetting) -> dict:
    return {"block": setting.block, "seed": setting.seed}


def describe(period: PeriodSpec) -> dict:
    """本评价期的登记值的可序列化表示（写入 run_record.json；键序固定）。"""
    parameters = period.confirmatory
    return {
        "mode": period.mode, "period": period.period, "purpose": PURPOSE.value,
        "frozen_candidate": {"K": FROZEN_CANDIDATE.k, "theta_P": str(FROZEN_CANDIDATE.theta),
                             "h": FROZEN_CANDIDATE.h},
        "cutoff": period.cutoff.isoformat(), "first_return_day": period.first_return_day.isoformat(),
        "expected": {"first_signal_day": period.first_signal_day.isoformat(),
                     "status_first_day": period.status_first_day.isoformat(),
                     "first_interval": [day.isoformat() for day in period.first_interval],
                     "last_interval": [day.isoformat() for day in period.last_interval]},
        "histories": {asset: day.isoformat() for asset, day in HISTORIES.items()},
        "continuity": CONTINUITY.name, "offset": OFFSET, "tolerance": TOLERANCE,
        "confirmatory": {"main": _block(parameters.main),
                         "sensitivities": [_block(item) for item in parameters.sensitivities],
                         "resamples": parameters.resamples, "alpha": parameters.alpha,
                         "warning_p": parameters.warning_p, "annual_days": parameters.annual_days,
                         "minimum_growth": parameters.minimum_growth, "tolerance": parameters.tolerance,
                         "padding": parameters.padding, "split": parameters.split.isoformat()},
        "basis_sha256": {"lock_record": LOCK_RECORD_SHA256, "lock_supplement_1": LOCK_SUPPLEMENT_SHA256,
                         "standard_revision_3": STANDARD_R3_SHA256, "v1_design": V1_DESIGN_SHA256},
        "formal_name": period.formal_name,
    }
