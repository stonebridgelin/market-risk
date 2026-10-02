"""确认性检验（登记第七节；实施口径补充第 3、6、12、13、16、18、19 条）。纯计算，float64。

主张：选定的信号候选在 R1、R2 约束下，信号模拟的长期收益高于主参照规则。
结论由三个独立判断共同决定：R1 与 R2 的资格检查、收益改善检验、改善点估计是否达到登记幅度。
检验在冻结路径上进行：重抽样时不重跑状态机、不重新选参；结论是“在实际发生的信号路径上”的。
收益区间一律以区间末日标记。检验力参考不在本模块实现。
"""

from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from market_risk.wavewarn_v20.labels_r2 import R2Event

INVALID = "计算无效"
WORDING = {
    "A": "在冻结后的历史检验中，满足风险与提示条件，收益改善检验通过，且改善点估计达到登记幅度",
    "B": "收益改善检验通过，但改善点估计未达到登记幅度",
    "C": "未证明长期收益优于参照规则",
    "D": "未通过资格检查",
}
UNSTABLE = "证据不稳定"
WARNING_SENSITIVITY = "敏感性区块下 p 不低于警示线"
WARNING_HALVES = "前后两半的 Δ 方向不一致"
WARNING_ZEROING = "事件窗口置零后 Δ 不再为正"


class ConfirmatoryError(ValueError):
    """输入不合法。"""


def _finite_parameter(name: str, value: float) -> None:
    """参数类输入必须是有限数：NaN 参与的比较一律为假，会让检查被悄悄放行，所以直接报错。"""
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise ConfirmatoryError(f"{name} 必须是有限数：{value!r}")


@dataclass(frozen=True)
class BlockSetting:
    block: int          # 平均区块长度 b
    seed: int


@dataclass(frozen=True)
class ConfirmatoryParameters:
    """登记：主设定 b = 20、种子 20261020；敏感性 b = 10、40、60、120；B = 10,000；α = 0.05；警示线 0.10；

    年化用 252 日；最低有意义改善为年化 1%；对账容差 1e-10；事件窗口前后各 20 个交易日；前后两半的分界日。
    """

    main: BlockSetting
    sensitivities: tuple[BlockSetting, ...]
    resamples: int
    alpha: float
    warning_p: float
    annual_days: int
    minimum_growth: float
    tolerance: float
    padding: int
    split: dt.date                 # 区间末日早于它的属于前一半

    def __post_init__(self) -> None:
        for name in ("alpha", "warning_p", "minimum_growth", "tolerance"):
            _finite_parameter(name, getattr(self, name))


@dataclass(frozen=True)
class ConfirmatoryInput:
    """冻结路径上的输入。收益为 None 表示净值所需价格缺失、无法计算。"""

    period: str                                         # 评价期的名称，用于措辞（如“验证期”）
    end_days: tuple[dt.date, ...]                       # 各收益区间的末日，升序
    candidate_returns: tuple[float, ...] | None
    reference_returns: tuple[float, ...] | None
    candidate_log_wealth: float | None                  # ln W_候选,末
    reference_log_wealth: float | None
    hold_log_wealth: float | None                       # 同一组合一直持有的 ln W_末（只用于措辞）
    r1: bool | None                                     # None 为无法计算
    r2: Mapping[str, bool | None]                       # 各资产；None 为“R2 无法计算”
    events: Mapping[str, Sequence[R2Event]]             # 各资产按该窗口截止日生成的 R2 事件
    first_signal_day: dt.date                           # f：P ≤ f 的事件为左截断，不参与置零


@dataclass(frozen=True)
class BootstrapRow:
    block: int
    seed: int
    valid: bool
    p_value: float | None
    low: float | None           # Δ* 的 2.5% 分位数
    high: float | None          # Δ* 的 97.5% 分位数
    note: str


@dataclass(frozen=True)
class ZeroedEvent:
    asset: str
    peak: dt.date
    delta: float                # 该事件窗口置零后重算的 Δ


@dataclass(frozen=True)
class ConfirmatoryResult:
    valid: bool
    reason: str                                   # 计算无效的原因；有效时为空
    n: int
    delta: float | None
    delta_min: float | None
    annual_growth: float | None                   # 年化相对净值增长率 exp(252Δ ÷ n) − 1
    main: BootstrapRow | None
    sensitivities: tuple[BootstrapRow, ...]
    category: str | None                          # A、B、C、D；计算无效时为 None
    unstable: bool                                # A、B 因稳定性警示降为“证据不稳定”（保留原类别标注）
    warnings: tuple[str, ...]                     # 触发的稳定性警示（对任何类别都列出，只对 A、B 起降级作用）
    improvement_passed: bool | None               # “收益改善检验通过”：Δ > 0 且 p < α
    magnitude_reached: bool | None                # “改善点估计达到登记幅度”：Δ ≥ Δ_min
    halves: tuple[float, float] | None            # 前后两半的 Δ
    zeroed: tuple[ZeroedEvent, ...]
    conclusion: str
    own_result: str | None                        # 候选自身亏损时须同时写明的措辞


def stationary_bootstrap_indices(length: int, block_length: int, resamples: int, seed: int,
                                 generator: np.random.Generator | None = None) -> Iterator[tuple[int, ...]]:
    """平稳自助法（Politis–Romano，环形）的抽样索引，抽样顺序与 v1.2.1 规格相同。

    随机数用 Generator(PCG64(seed))，全部 B 条序列共用同一个随机数流；每条序列先 integers(0, n) 取第一个位置；
    此后每个位置先抽 u = random()，u < 1/b 时 integers(0, n) 重抽位置，否则 (i + 1) mod n。
    """
    if length <= 0 or block_length <= 0 or resamples <= 0:
        raise ConfirmatoryError("长度、区块均长与重抽次数必须为正")
    generator = generator if generator is not None else np.random.Generator(np.random.PCG64(seed))
    for _ in range(resamples):
        position = int(generator.integers(0, length))
        indices = [position]
        for _ in range(1, length):
            uniform = float(generator.random())
            position = int(generator.integers(0, length)) if uniform < 1 / block_length else (position + 1) % length
            indices.append(position)
        yield tuple(indices)


def right_tail_p(delta: float, resampled: Sequence[float]) -> float:
    """中心化右尾 p = (1 + #{k : Δ*_k − Δ ≥ Δ}) ÷ (B + 1)，按此式原样计算。"""
    _finite_parameter("Δ", delta)
    if any(not math.isfinite(value) for value in resampled):
        raise ConfirmatoryError("重抽样的 Δ* 出现非有限值")
    return (1 + sum(1 for value in resampled if value - delta >= delta)) / (len(resampled) + 1)


def bootstrap_row(differences: Sequence[float], delta: float, setting: BlockSetting, resamples: int) -> BootstrapRow:
    """一个区块设定下的 p 与区间；n ÷ b < 2 或抽样过程出错时该行无效。"""
    n = len(differences)
    if n < 2 * setting.block:
        return BootstrapRow(setting.block, setting.seed, False, None, None, None, f"n ÷ b < 2（n = {n}）")
    try:
        resampled = [math.fsum(differences[index] for index in indices)
                     for indices in stationary_bootstrap_indices(n, setting.block, resamples, setting.seed)]
        if any(not math.isfinite(value) for value in resampled):
            raise ConfirmatoryError("重抽样的 Δ* 出现非有限值")
        low, high = np.quantile(np.asarray(resampled, dtype=np.float64), [0.025, 0.975], method="linear")
    except (ArithmeticError, ValueError, IndexError) as error:
        return BootstrapRow(setting.block, setting.seed, False, None, None, None, f"重抽样过程出错：{error}")
    return BootstrapRow(setting.block, setting.seed, True, right_tail_p(delta, resampled), float(low), float(high), "")


def zero_event_window(end_days: Sequence[dt.date], differences: Sequence[float], peak: dt.date, trough: dt.date,
                      padding: int) -> list[float]:
    """把区间末日落在 [P 前第 padding 个交易日, Tr 后第 padding 个交易日] 内的 d_j 置零（截在窗口内）。"""
    start = max(0, bisect_left(end_days, peak) - padding)
    stop = min(len(end_days), bisect_right(end_days, trough) + padding)
    return [0.0 if start <= index < stop else value for index, value in enumerate(differences)]


def zeroed_events(data: ConfirmatoryInput, differences: Sequence[float], padding: int) -> tuple[ZeroedEvent, ...]:
    """按资产分别取事件（不合并资产；含未结束事件，不含左截断事件），每个事件单独置零一次并重算 Δ。"""
    result: list[ZeroedEvent] = []
    for asset in sorted(data.events):
        for event in data.events[asset]:
            if event.peak <= data.first_signal_day:
                continue
            kept = zero_event_window(data.end_days, differences, event.peak, event.trough, padding)
            result.append(ZeroedEvent(asset, event.peak, math.fsum(kept)))
    return tuple(result)


def halves_consistent(first: float, second: float) -> bool:
    """只有同为正或同为负才算方向一致；任一半等于 0 都算不一致。"""
    _finite_parameter("前一半的 Δ", first)
    _finite_parameter("后一半的 Δ", second)
    return (first > 0 and second > 0) or (first < 0 and second < 0)


def improvement_passed(delta: float, p_value: float, alpha: float) -> bool:
    """“收益改善检验通过”：Δ > 0 且 p < α。Δ ≤ 0 时不论 p 多小都不通过。"""
    for name, value in (("Δ", delta), ("p", p_value), ("α", alpha)):
        _finite_parameter(name, value)
    return delta > 0 and p_value < alpha


def category_of(qualified: bool, delta: float, p_value: float, delta_min: float, alpha: float) -> str:
    """类别判定（登记第七节第 6 小节）：先判资格，再判收益；资格与收益不能互相补偿。

    D：R1 或 R2 不通过。C：资格通过，且 Δ ≤ 0 或 p ≥ α。B：Δ > 0、p < α、Δ < Δ_min。A：Δ > 0、p < α、Δ ≥ Δ_min。
    """
    for name, value in (("Δ", delta), ("p", p_value), ("Δ_min", delta_min), ("α", alpha)):
        _finite_parameter(name, value)
    if not qualified:
        return "D"
    if delta <= 0 or not improvement_passed(delta, p_value, alpha):
        return "C"
    return "A" if delta >= delta_min else "B"


def stability_warnings(sensitivities: Sequence[BootstrapRow], halves: tuple[float, float],
                       zeroed: Sequence[ZeroedEvent], warning_p: float) -> tuple[str, ...]:
    """稳定性警示的三个触发条件，任一出现即列出：

    有效的敏感性行中任一 p ≥ 警示线；前后两半的 Δ 方向不一致；任一事件窗口置零后重算的 Δ ≤ 0（不再为正）。
    """
    _finite_parameter("警示线", warning_p)
    for row in sensitivities:
        if row.valid and row.p_value is not None:
            _finite_parameter(f"敏感性行（b = {row.block}）的 p", row.p_value)
    for item in zeroed:
        _finite_parameter(f"{item.asset} 高点 {item.peak} 的事件置零后的 Δ", item.delta)
    warnings: list[str] = []
    if any(row.valid and row.p_value is not None and row.p_value >= warning_p for row in sensitivities):
        warnings.append(WARNING_SENSITIVITY)
    if not halves_consistent(*halves):
        warnings.append(WARNING_HALVES)
    if any(item.delta <= 0 for item in zeroed):
        warnings.append(WARNING_ZEROING)
    return tuple(warnings)


def percent(log_wealth: float) -> str:
    """自身收益的百分数文字（带符号，两位小数）。"""
    return f"{math.expm1(log_wealth) * 100:+.2f}%"


def own_result_text(data: ConfirmatoryInput, delta: float, annual_growth: float) -> str | None:
    """候选自身亏损（ln W_候选,末 < 0）时须同时写明的措辞；不亏损时为 None（实施口径补充第 16 条）。

    只如实列出候选、参照、一直持有三者的自身收益，以及年化相对净值增长率。
    只有候选确实比参照少亏（候选亏损，且 Δ > 0）时才写“相对参照少亏”；其余情形只用中性的数值描述。
    措辞不改变类别判定。
    """
    candidate, reference, hold = data.candidate_log_wealth, data.reference_log_wealth, data.hold_log_wealth
    if candidate is None or reference is None or candidate >= 0:
        return None
    if hold is None:
        raise ConfirmatoryError("候选自身亏损时须同时给出一直持有的 ln W_末")
    growth = f"年化相对净值增长率 {annual_growth * 100:+.2f}%"
    held = f"一直持有的自身收益为 {percent(hold)}"
    if delta > 0:        # 候选亏损且 Δ > 0：参照亏得更多
        return (f"候选在{data.period}自身亏损 {-math.expm1(candidate) * 100:.2f}%，"
                f"参照亏损 {-math.expm1(reference) * 100:.2f}%；相对参照少亏，{growth}；{held}")
    return (f"候选在{data.period}的自身收益为 {percent(candidate)}，参照的自身收益为 {percent(reference)}；"
            f"{growth}；{held}")


def _invalid(reason: str, n: int) -> ConfirmatoryResult:
    return ConfirmatoryResult(False, reason, n, None, None, None, None, (), None, False, (), None, None, None, (),
                              INVALID, None)


def confirmatory_test(data: ConfirmatoryInput, parameters: ConfirmatoryParameters) -> ConfirmatoryResult:
    """登记口径的确认性检验：先判计算是否有效，再判资格，再判收益；资格与收益不能互相补偿。"""
    n = len(data.end_days)
    if any(later <= earlier for earlier, later in pairwise(data.end_days)):
        raise ConfirmatoryError("区间末日须严格升序")
    if data.candidate_returns is None or data.reference_returns is None or (
            data.candidate_log_wealth is None or data.reference_log_wealth is None):
        return _invalid("净值所需价格缺失，收益无法计算", n)
    # 输入有效性保护：NaN 参与的比较一律为假，若不先检查，非有限的累计对数净值会通过下面的对账。
    for name, value in (("候选", data.candidate_log_wealth), ("参照", data.reference_log_wealth)):
        if not math.isfinite(value):
            return _invalid(f"{name}的累计对数净值不是有限数：{value}", n)
    if data.candidate_log_wealth < 0 and data.hold_log_wealth is not None and not math.isfinite(
            data.hold_log_wealth):
        # 只在结论文字实际使用它时检查：候选自身亏损时须同时列出一直持有的自身收益。
        reason = f"一直持有的累计对数净值不是有限数：{data.hold_log_wealth}（候选自身亏损，结论文字要用到它）"
        return _invalid(reason, n)
    if len(data.candidate_returns) != n or len(data.reference_returns) != n:
        raise ConfirmatoryError("收益序列须与区间末日等长")
    if n == 0:
        return _invalid("n = 0", n)
    if data.r1 is None:
        return _invalid("R1 无法计算", n)
    if not data.r2 or any(value is None for value in data.r2.values()):
        return _invalid("R2 无法计算", n)
    returns = (*data.candidate_returns, *data.reference_returns)
    if any(not math.isfinite(value) or value <= -1 for value in returns):
        return _invalid("收益出现非有限值或不大于 −1", n)
    logs_candidate = [math.log1p(value) for value in data.candidate_returns]
    logs_reference = [math.log1p(value) for value in data.reference_returns]
    differences = [a - b for a, b in zip(logs_candidate, logs_reference, strict=True)]
    delta = math.fsum(differences)
    if not math.isfinite(delta) or any(not math.isfinite(value) for value in differences):
        return _invalid("d_j 出现非有限值", n)
    if abs(math.fsum(logs_candidate) - data.candidate_log_wealth) > parameters.tolerance or abs(
            math.fsum(logs_reference) - data.reference_log_wealth) > parameters.tolerance:
        return _invalid("对账不符：Σ ln(1 + R_j) 与 ln W_末 之差超出容差", n)
    if abs(delta - (data.candidate_log_wealth - data.reference_log_wealth)) > parameters.tolerance:
        return _invalid("对账不符：Σ d_j 与 Δ 之差超出容差", n)

    main = bootstrap_row(differences, delta, parameters.main, parameters.resamples)
    if not main.valid or main.p_value is None:
        return _invalid(f"主设定无效：{main.note}", n)
    sensitivities = tuple(bootstrap_row(differences, delta, setting, parameters.resamples)
                          for setting in parameters.sensitivities)
    delta_min = n / parameters.annual_days * math.log1p(parameters.minimum_growth)
    annual_growth = math.expm1(parameters.annual_days * delta / n)
    improvement = improvement_passed(delta, main.p_value, parameters.alpha)
    magnitude = delta >= delta_min
    qualified = data.r1 is True and all(value is True for value in data.r2.values())
    category = category_of(qualified, delta, main.p_value, delta_min, parameters.alpha)

    first = math.fsum(value for day, value in zip(data.end_days, differences, strict=True) if day < parameters.split)
    second = math.fsum(value for day, value in zip(data.end_days, differences, strict=True) if day >= parameters.split)
    zeroed = zeroed_events(data, differences, parameters.padding)
    warnings = stability_warnings(sensitivities, (first, second), zeroed, parameters.warning_p)
    unstable = category in ("A", "B") and bool(warnings)        # 警示只降低结论强度；C、D 不因它改变
    conclusion = f"{UNSTABLE}（原类别 {category}：{WORDING[category]}）" if unstable else WORDING[category]
    return ConfirmatoryResult(True, "", n, delta, delta_min, annual_growth, main, sensitivities, category, unstable,
                              warnings, improvement, magnitude, (first, second), zeroed, conclusion,
                              own_result_text(data, delta, annual_growth))
