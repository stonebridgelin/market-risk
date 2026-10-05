"""阶段四开发期运行的登记参数（阶段四字段级设计稿第一节；M2 第一部分指令第二节第 1 小节）。算法侧，纯常量与组装。

- 已有常量一律导入复用，不复制数值：27 组候选、通道与系统与主参照的初始状态、两资产的登记历史起点。
- 设计稿第一节表中“新写一次”的值各在本模块写一次：输入窗口、仓位映射、执行政策、R2 门槛、R2 规则。
- 分段四项与熊市两项按设计稿第一节第 2 小节写定。
- 不读文件、不读配置；不提供任何覆盖参数（不接受命令行或环境变量修改登记值）。
  历史起点与 config/wavewarn_v20.yaml 的 first_date 的交叉断言由 development_run 在加载配置后执行。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from market_risk.wavewarn_v20.convergence import REGISTERED_CHANNELS, REGISTERED_REFERENCE, REGISTERED_SYSTEM
from market_risk.wavewarn_v20.execution import PolicyParameters, PositionMap, Weights
from market_risk.wavewarn_v20.inputs import InputWindows
from market_risk.wavewarn_v20.labels_r2 import R2Thresholds
from market_risk.wavewarn_v20.provenance_v20 import REGISTERED_FIRST_DAY
from market_risk.wavewarn_v20.r2 import R2Rule
from market_risk.wavewarn_v20.research_run import (
    REGISTERED_CANDIDATES,
    Continuity,
    InitialStates,
    Purpose,
    RunParameters,
    SegmentSpec,
    WindowSpec,
)

# ---- 复用的已有常量（不复制数值）----
CANDIDATES = REGISTERED_CANDIDATES                       # 登记第五节：27 组，先 K 再 θ_P 再 h
HISTORIES = REGISTERED_FIRST_DAY                         # 登记第三节：SPX 1990-01-02；QQQ 1999-03-10
INITIAL = InitialStates(REGISTERED_CHANNELS, REGISTERED_SYSTEM, REGISTERED_REFERENCE)   # 登记第四节第 1 小节

# ---- 设计稿第一节表中“新写一次”的值 ----
WINDOWS = InputWindows(high=63, low_prior=19, low_minimum=15, average=200)            # 登记第一节、补充第 8 条
POSITIONS = PositionMap(Weights(0.6, 0.4), Weights(0.6, 0.0), Weights(0.3, 0.0), 2.0)  # 登记第六节第 2 小节
POLICY = PolicyParameters(stop_ratio=0.96, cooldown=10)                               # 登记第一节第 6 小节
R2_THRESHOLDS = R2Thresholds(Decimal("0.95"), Decimal("1.05"), Decimal("0.97"))       # 登记第三节；规格第八节第 3 条
R2_RULE = R2Rule(3, 5, 20)                                                            # 产品规格第八节

# ---- 其余登记值（设计稿第一节表）----
OFFSET = 63                                  # 共同起点偏移（登记第四节第 3 小节）
TOLERANCE = 1e-10                            # 净值对账与并列容差（登记第五节第 1 小节）
R1_RATIO = 0.5                               # R1 比例（登记第五节第 1 小节）
DIAGNOSTICS = True                           # 诊断开（组合层设计第十四节）
CUTOFF = dt.date(2016, 12, 30)               # 截止日（登记第三节）
PURPOSE = Purpose.DEVELOPMENT                # 开发期选参；first_return_day 为 None
CONTINUITY = Continuity.COMPLETE_TRADING_AXIS

# ---- 分段（收益区间末日归属，边界 (start, end]；登记第五节“R1 的报告边界”；设计稿第一节第 2 小节）----
SEGMENT_BASIS = "登记第五节“R1 的报告边界”；收益区间按末日归属，边界 (start, end]"
BEAR_BASIS = ("波段预警研究规格_v1.4_修订登记.md 第 38 行；config/wavewarn_v14_failure_path.yaml 第 21—23 行"
              "（SPX 收盘高点 P 至低点 Tr）")
# 两次熊市：（名称，P，Tr）。市场环境按收益区间起点 d 判断 P ≤ d < Tr。
BEARS: tuple[tuple[str, dt.date, dt.date], ...] = (
    ("熊市一", dt.date(2000, 3, 24), dt.date(2002, 10, 9)),
    ("熊市二", dt.date(2007, 10, 9), dt.date(2009, 3, 9)),
)
SEGMENTS: tuple[SegmentSpec, ...] = (
    SegmentSpec("1999—2009", dt.date(1998, 12, 31), dt.date(2009, 12, 31), SEGMENT_BASIS),
    SegmentSpec("2010—2016", dt.date(2009, 12, 31), dt.date(2016, 12, 30), SEGMENT_BASIS),
    *(SegmentSpec(name, peak, trough, f"{SEGMENT_BASIS}；熊市日期：{BEAR_BASIS}") for name, peak, trough in BEARS),
)

# ---- 市场环境（登记第三节“市场环境口径同 T2”；设计稿第一节第 2 小节）----
ENVIRONMENT_UP = Decimal("0.10")             # r_y ≥ +10% → 上涨年
ENVIRONMENT_DOWN = Decimal("-0.10")          # r_y ≤ −10% → 下跌年
ENVIRONMENT_ASSET = "SPX"                    # 价格指数年度收益取 SPX 收盘价


def run_parameters() -> RunParameters:
    """组合层运行参数（设计稿第一节表）。"""
    return RunParameters(WINDOWS, POSITIONS, POLICY, R2_THRESHOLDS, R2_RULE, OFFSET, TOLERANCE, R1_RATIO, SEGMENTS,
                         DIAGNOSTICS)


def window_spec() -> WindowSpec:
    """开发期选参的窗口：用途开发期选参、first_return_day=None、E 为截止日、登记历史起点、登记初始状态。"""
    return WindowSpec(PURPOSE, None, CUTOFF, HISTORIES, INITIAL, CONTINUITY)


def describe() -> dict:
    """全部登记参数的可序列化表示（写入 run_record.json；键序固定）。"""
    return {
        "candidates": [{"order": order, "K": item.k, "theta_P": str(item.theta), "h": item.h}
                       for order, item in enumerate(CANDIDATES)],
        "initial_states": {"channels": {name: getattr(INITIAL.channels, name).value
                                        for name in ("p_spx", "p_qqq", "pr_spx", "pr_qqq", "mr")},
                           "system": {"S": INITIAL.system.risk.name, "c1": INITIAL.system.c1,
                                      "c2": INITIAL.system.c2},
                           "reference": INITIAL.reference.name},
        "input_windows": {"high": WINDOWS.high, "low_prior": WINDOWS.low_prior, "low_minimum": WINDOWS.low_minimum,
                          "average": WINDOWS.average},
        "positions": {"normal": [POSITIONS.normal.core, POSITIONS.normal.leverage],
                      "level1": [POSITIONS.level1.core, POSITIONS.level1.leverage],
                      "level2": [POSITIONS.level2.core, POSITIONS.level2.leverage], "lambda": POSITIONS.leverage},
        "policy": {"stop_ratio": POLICY.stop_ratio, "cooldown": POLICY.cooldown},
        "r2_thresholds": {"confirm": str(R2_THRESHOLDS.confirm), "finish": str(R2_THRESHOLDS.finish),
                          "early": str(R2_THRESHOLDS.early)},
        "r2_rule": {"numerator": R2_RULE.numerator, "denominator": R2_RULE.denominator,
                    "observation": R2_RULE.observation},
        "offset": OFFSET, "tolerance": TOLERANCE, "r1_ratio": R1_RATIO, "diagnostics": DIAGNOSTICS,
        "cutoff": CUTOFF.isoformat(), "purpose": PURPOSE.value, "first_return_day": None,
        "continuity": CONTINUITY.name,
        "histories": {asset: day.isoformat() for asset, day in HISTORIES.items()},
        "segments": [{"name": item.name, "start": item.start.isoformat(), "end": item.end.isoformat(),
                      "basis": item.basis} for item in SEGMENTS],
        "bears": [{"name": name, "peak": peak.isoformat(), "trough": trough.isoformat()}
                  for name, peak, trough in BEARS],
        "environment": {"asset": ENVIRONMENT_ASSET, "up": str(ENVIRONMENT_UP), "down": str(ENVIRONMENT_DOWN)},
    }
