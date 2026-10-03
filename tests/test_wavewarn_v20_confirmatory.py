"""v2.0 的确认性检验（登记第七节；实施口径补充第 3、6、12、13、16、19 条）：只用构造数据。

抽样的期望值有两种来源，都不是新函数自己的输出：
- “旧实现兼容性参照”：由旧函数的输出生成的固定索引（来源、版本、种子与参数见 REFERENCE 上方的说明）。
  它只说明新旧实现逐位相同，不是人工推算，也不是独立复核；独立复核留到阶段三，用不导入项目代码的实现逐位核对。
- 手工构造随机数流：用按预定序列返回结果的假生成器，手工推出期望的索引序列，验证抽样顺序。
旧函数只在本测试文件中导入，产品代码不导入旧代码。
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest
from wavewarn_v20_helpers import numbered

from market_risk.wavewarn.paired_test import stationary_bootstrap_indices as old_stationary_bootstrap_indices
from market_risk.wavewarn_v20.confirmatory import (
    INVALID,
    UNSTABLE,
    WARNING_HALVES,
    WARNING_SENSITIVITY,
    WARNING_ZEROING,
    WORDING,
    BlockSetting,
    BootstrapRow,
    ConfirmatoryError,
    ConfirmatoryInput,
    ConfirmatoryParameters,
    ZeroedEvent,
    bootstrap_row,
    category_of,
    confirmatory_test,
    halves_consistent,
    improvement_passed,
    right_tail_p,
    stability_warnings,
    stationary_bootstrap_indices,
    zero_event_window,
)
from market_risk.wavewarn_v20.labels_r2 import R2Event

# 登记的 5 组（种子, b）：主设定 b = 20，敏感性 b = 10、40、60、120。
SETTINGS = ((20261020, 20), (20261010, 10), (20261040, 40), (20261060, 60), (202610120, 120))
RESAMPLES = 199                     # 构造测试用的小 B；登记的 B = 10,000 只是参数取值不同
N = 120

# 旧实现兼容性参照（不是人工推算，也不是独立复核）。
# 来源：旧函数 market_risk.wavewarn.paired_test.stationary_bootstrap_indices 的输出。
# 版本：生成时的提交为 209e2bd（该函数所在文件最后一次修改的提交为 ad3da07）；NumPy 2.5.3。
# 参数：登记的 5 组（种子, b）；长度 30；每组前 3 条抽样。键为（种子, b）。
REFERENCE = {
    (20261020, 20): (
        (25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 26, 27, 28, 29, 0, 1, 2),
        (14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 0, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
        (16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17),
    ),
    (20261010, 10): (
        (6, 26, 14, 15, 16, 17, 18, 19, 20, 21, 22, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 8, 9, 10, 11),  # noqa: E501
        (12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 29, 0, 13, 14, 15, 16, 13, 14, 15, 16, 17, 18),  # noqa: E501
        (8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7),
    ),
    (20261040, 40): (
        (19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18),
        (10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22),  # noqa: E501
        (21, 22, 23, 24, 25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20),
    ),
    (20261060, 60): (
        (23, 24, 25, 26, 27, 28, 29, 0, 1, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        (12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11),
        (4, 5, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8),
    ),
    (202610120, 120): (
        (23, 24, 25, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28),  # noqa: E501
        (14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13),
        (29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28),
    ),
}


def parameters(split: int = N // 2 + 1, resamples: int = RESAMPLES) -> ConfirmatoryParameters:
    main, *others = (BlockSetting(block, seed) for seed, block in SETTINGS)
    return ConfirmatoryParameters(main=main, sensitivities=tuple(others), resamples=resamples, alpha=0.05,
                                  warning_p=0.10, annual_days=252, minimum_growth=0.01, tolerance=1e-10, padding=20,
                                  split=numbered(split))


def end_days(count: int = N) -> tuple[dt.date, ...]:
    return tuple(numbered(number) for number in range(1, count + 1))


def data(candidate_logs: list[float], reference_logs: list[float] | None = None, **changes) -> ConfirmatoryInput:
    """由逐区间的对数收益构造输入：R = expm1(对数收益)，ln W_末 取 log1p(R) 的精确和。"""
    reference_logs = [0.0] * len(candidate_logs) if reference_logs is None else reference_logs
    candidate = tuple(math.expm1(value) for value in candidate_logs)
    reference = tuple(math.expm1(value) for value in reference_logs)
    fields = dict(period="验证期", end_days=end_days(len(candidate_logs)), candidate_returns=candidate,
                  reference_returns=reference,
                  candidate_log_wealth=math.fsum(math.log1p(value) for value in candidate),
                  reference_log_wealth=math.fsum(math.log1p(value) for value in reference), hold_log_wealth=0.1,
                  r1=True, r2={"SPX": True, "QQQ": True}, events={"SPX": (), "QQQ": ()},
                  first_signal_day=numbered(0))
    fields.update(changes)
    return ConfirmatoryInput(**fields)


def event(asset: str, peak: int, trough: int, end: int | None = None) -> R2Event:
    return R2Event(asset, numbered(peak), Decimal("100"), numbered(peak + 1), numbered(peak + 2), numbered(trough),
                   Decimal("94"), None if end is None else numbered(end), end is None)


def independent_p(differences: list[float], seed: int, block: int, resamples: int = RESAMPLES) -> float:
    """用旧函数的索引独立算一次中心化右尾 p（只在测试里使用）。"""
    delta = math.fsum(differences)
    sums = [math.fsum(differences[index] for index in indices)
            for indices in old_stationary_bootstrap_indices(len(differences), block, resamples, seed)]
    return (1 + sum(1 for value in sums if value - delta >= delta)) / (resamples + 1)


# ---------------------------------------------------------------------------
# 抽样
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("seed", "block"), SETTINGS)
@pytest.mark.parametrize("length", [30, 57])
def test_new_sampler_matches_the_old_function_bit_for_bit(seed: int, block: int, length: int) -> None:
    new = list(stationary_bootstrap_indices(length, block, 5, seed))
    old = list(old_stationary_bootstrap_indices(length, block, 5, seed))
    assert new == old and len(new) == 5 and all(len(row) == length for row in new)


@pytest.mark.parametrize(("seed", "block"), SETTINGS)
def test_new_sampler_matches_the_stored_compatibility_reference(seed: int, block: int) -> None:
    assert tuple(stationary_bootstrap_indices(30, block, 3, seed)) == REFERENCE[(seed, block)]


class FixedGenerator:
    """按预定序列返回 integers 与 random 的结果，并记下调用顺序。"""

    def __init__(self, positions: tuple[int, ...], uniforms: tuple[float, ...]) -> None:
        self.positions, self.uniforms = iter(positions), iter(uniforms)
        self.calls: list[str] = []

    def integers(self, low: int, high: int) -> int:
        assert (low, high) == (0, 5)
        self.calls.append("i")
        return next(self.positions)

    def random(self) -> float:
        self.calls.append("u")
        return next(self.uniforms)


def test_sampling_order_with_a_hand_constructed_random_stream() -> None:
    """n = 5，b = 2（1/b = 0.5），抽 2 条。手工推算：

    第 1 条：先抽首位置 4；u = 0.7 ≥ 0.5 → 环形续行 (4 + 1) mod 5 = 0；u = 0.2 < 0.5 → 重抽位置 1；
            u = 0.9 → 2；u = 0.5（不小于 0.5）→ 3。得 (4, 0, 1, 2, 3)。
    第 2 条：先抽首位置 3；u = 0.49 → 重抽位置 0；u = 0.6 → 1；u = 0.99 → 2；u = 0.0 → 重抽位置 2。得 (3, 0, 1, 2, 2)。
    调用顺序：每条先一次 integers；之后每个位置先一次 random，只有 u < 1/b 时才再调用一次 integers。
    """
    generator = FixedGenerator(positions=(4, 1, 3, 0, 2), uniforms=(0.7, 0.2, 0.9, 0.5, 0.49, 0.6, 0.99, 0.0))
    rows = list(stationary_bootstrap_indices(5, 2, 2, 7, generator))      # type: ignore[arg-type]
    assert rows == [(4, 0, 1, 2, 3), (3, 0, 1, 2, 2)]
    assert "".join(generator.calls) == "iuuiuu" + "iuiuuui"
    assert next(generator.positions, None) is None and next(generator.uniforms, None) is None   # 两个序列恰好用完


def test_sampler_rejects_non_positive_arguments() -> None:
    for arguments in ((0, 20, 5, 1), (30, 0, 5, 1), (30, 20, 0, 1)):
        with pytest.raises(ConfirmatoryError):
            list(stationary_bootstrap_indices(*arguments))


# ---------------------------------------------------------------------------
# 统计量、p 与区间
# ---------------------------------------------------------------------------


def test_centred_right_tail_p_by_hand() -> None:
    # Δ = 1.0；Δ* = 0.5、2.0、2.5、1.9。Δ* − Δ ≥ Δ 即 Δ* ≥ 2.0 的有 2 个 → p = (1 + 2) ÷ (4 + 1) = 0.6。
    assert right_tail_p(1.0, [0.5, 2.0, 2.5, 1.9]) == pytest.approx(0.6)
    assert right_tail_p(1.0, [0.5, 1.9]) == pytest.approx(1 / 3)          # 没有一个达到：(1 + 0) ÷ (2 + 1)
    assert right_tail_p(-1.0, [0.5, -3.0]) == pytest.approx(2 / 3)        # 原样按式计算：Δ* ≥ 2Δ = −2 的有 1 个


def test_interval_uses_linear_interpolation_and_p_follows_the_formula() -> None:
    differences = [0.01 * math.sin(index) + 0.001 for index in range(80)]
    delta = math.fsum(differences)
    row = bootstrap_row(differences, delta, BlockSetting(20, 20261020), 37)
    sums = sorted(math.fsum(differences[index] for index in indices)
                  for indices in old_stationary_bootstrap_indices(80, 20, 37, 20261020))

    def linear(q: float) -> float:        # 线性插值的分位数：位置 (B − 1)·q，在相邻两个顺序统计量之间插值
        position = (len(sums) - 1) * q
        lower = math.floor(position)
        upper = min(lower + 1, len(sums) - 1)
        return sums[lower] + (sums[upper] - sums[lower]) * (position - lower)

    assert row.valid and (row.block, row.seed) == (20, 20261020)
    assert row.low == pytest.approx(linear(0.025), abs=1e-15) and row.high == pytest.approx(linear(0.975), abs=1e-15)
    assert row.p_value == (1 + sum(1 for value in sums if value - delta >= delta)) / 38


def test_delta_reconciles_with_log_wealth_and_registered_magnitude() -> None:
    logs = [0.001] * N
    result = confirmatory_test(data(logs), parameters())
    assert result.valid and result.n == N
    assert result.delta == pytest.approx(0.12, abs=1e-12)                                  # Δ = ln W_候选 − ln W_参照
    assert result.delta_min == pytest.approx(N / 252 * math.log(1.01), rel=1e-12)          # Δ_min = (n/252)·ln(1.01)
    assert result.annual_growth == pytest.approx(math.exp(252 * 0.12 / N) - 1, rel=1e-9)   # exp(252Δ ÷ n) − 1
    assert result.main is not None and result.main.p_value == 1 / (RESAMPLES + 1)          # 每条重抽样都等于 Δ


# ---------------------------------------------------------------------------
# 类别与两个独立判断
# ---------------------------------------------------------------------------


def test_category_a() -> None:
    result = confirmatory_test(data([0.001] * N), parameters())
    assert (result.category, result.unstable, result.warnings) == ("A", False, ())
    assert (result.improvement_passed, result.magnitude_reached) == (True, True)
    wording = "在冻结后的历史检验中，满足风险与提示条件，收益改善检验通过，且改善点估计达到登记幅度"
    assert result.conclusion == WORDING["A"] == wording
    assert result.own_result is None


def test_category_b_improvement_passes_but_magnitude_not_reached() -> None:
    result = confirmatory_test(data([0.00001] * N), parameters())         # Δ = 0.0012 < Δ_min ≈ 0.00474
    assert (result.category, result.improvement_passed, result.magnitude_reached) == ("B", True, False)
    assert result.conclusion == "收益改善检验通过，但改善点估计未达到登记幅度"


def test_magnitude_boundary_is_the_registered_minimum() -> None:
    """n = 252 时 Δ_min = ln(1.01)。Δ 为 Δ_min 的 1.5 倍、1.001 倍时达到登记幅度（A）；0.999 倍时未达到（B）。"""
    minimum = math.log(1.01)
    for factor, reached, category in ((1.5, True, "A"), (1.001, True, "A"), (0.999, False, "B")):
        result = confirmatory_test(data([factor * minimum / 252] * 252), parameters(split=127))
        assert result.delta_min == pytest.approx(minimum, rel=1e-12)
        assert (result.magnitude_reached, result.improvement_passed, result.category) == (reached, True, category)
    # 年化相对净值增长率恰为 1% 的那条线：Δ = Δ_min 时 exp(252Δ ÷ n) − 1 = 1%。
    assert confirmatory_test(data([1.001 * minimum / 252] * 252), parameters(split=127)).annual_growth == pytest.approx(
        1.01 ** 1.001 - 1)


def test_category_c_when_delta_is_not_positive() -> None:
    for logs in ([-0.001] * N, [0.0] * N):                                # Δ < 0 与 Δ = 0
        result = confirmatory_test(data(logs), parameters())
        assert (result.category, result.improvement_passed) == ("C", False)
        assert result.conclusion == "未证明长期收益优于参照规则"


def test_delta_not_positive_is_category_c_even_with_a_small_p() -> None:
    """“Δ ≤ 0 即为 C”由代码显式判断：直接给类别判定函数输入很小的 p，Δ 为 0 或为负时仍是 C。"""
    delta_min, alpha = 0.004, 0.05
    assert category_of(True, 0.0, 0.01, delta_min, alpha) == "C"            # Δ = 0，p = 0.01
    assert category_of(True, -0.001, 0.01, delta_min, alpha) == "C"         # Δ = −0.001，p = 0.01
    assert not improvement_passed(0.0, 0.01, alpha) and not improvement_passed(-0.001, 0.01, alpha)
    # Δ_min 取零或负数的假想情形也不能让 Δ ≤ 0 越过 C。
    assert category_of(True, 0.0, 0.01, 0.0, alpha) == "C" and category_of(True, -0.001, 0.01, -1.0, alpha) == "C"
    # 对照：同样的 p，Δ 为正时按幅度分为 B、A；p 不够小时为 C；资格不通过时为 D，不论 Δ 与 p。
    assert category_of(True, 0.001, 0.01, delta_min, alpha) == "B"
    assert category_of(True, 0.004, 0.01, delta_min, alpha) == "A"
    assert category_of(True, 0.01, 0.05, delta_min, alpha) == "C"            # p 恰为 α：不算小于
    assert category_of(False, 0.01, 0.01, delta_min, alpha) == "D"
    assert category_of(False, -0.01, 0.9, delta_min, alpha) == "D"


def test_category_c_magnitude_reached_but_improvement_test_fails() -> None:
    """Δ > 0 且达到登记幅度，但 p ≥ 0.05：两个独立判断分别为“否”“是”，类别为 C。

    构造：一个 +1.0 的离群日，其余 119 日各 −0.008，Δ = 0.048。重抽样里离群日被抽到两次以上的比例远高于 5%。
    """
    logs = [1.0, *[-0.008] * (N - 1)]
    assert independent_p(logs, 20261020, 20) >= 0.05                      # 构造成立的前提（由旧函数的索引独立算出）
    result = confirmatory_test(data(logs), parameters())
    assert result.delta == pytest.approx(0.048) and result.delta > result.delta_min
    assert (result.improvement_passed, result.magnitude_reached, result.category) == (False, True, "C")
    assert result.main.p_value == independent_p(logs, 20261020, 20)


def test_category_d_when_qualification_fails() -> None:
    for changes in ({"r1": False}, {"r2": {"SPX": True, "QQQ": False}}):
        result = confirmatory_test(data([0.001] * N, **changes), parameters())
        assert result.category == "D" and result.conclusion == "未通过资格检查"
        # 两个独立判断仍分别输出；资格与收益不能互相补偿。
        assert (result.improvement_passed, result.magnitude_reached) == (True, True)


@pytest.mark.parametrize(("changes", "reason"), [
    ({"candidate_returns": None, "candidate_log_wealth": None}, "价格缺失"),
    ({"reference_returns": None}, "价格缺失"),
    ({"r2": {"SPX": True, "QQQ": None}}, "R2 无法计算"),
    ({"r1": None}, "R1 无法计算"),
    ({"candidate_log_wealth": 0.5}, "对账不符"),
    ({"reference_log_wealth": 0.01}, "对账不符"),
])
def test_invalid_computation_exits(changes: dict, reason: str) -> None:
    result = confirmatory_test(data([0.001] * N, **changes), parameters())
    assert (result.valid, result.category, result.conclusion) == (False, None, INVALID)      # 不写任何优劣结论
    assert reason in result.reason and result.own_result is None
    assert (result.improvement_passed, result.magnitude_reached) == (None, None)


def test_invalid_when_n_is_zero_or_values_are_not_finite() -> None:
    empty = confirmatory_test(data([]), parameters())
    # 补修 Q14：原因由“n = 0”改为“评价窗口为空”，类别仍为“计算无效”。
    assert (empty.valid, empty.reason, empty.conclusion, empty.n) == (False, "评价窗口为空", INVALID, 0)
    for bad in (math.inf, math.nan, -1.0):
        returns = (0.001, bad, *[0.001] * (N - 2))
        result = confirmatory_test(data([0.001] * N, candidate_returns=returns), parameters())
        assert not result.valid and "非有限值" in result.reason


def test_block_length_exits() -> None:
    # 主设定 b = 20：n = 39 时 n ÷ b < 2 → 计算无效；n = 40 时主设定有效。
    assert confirmatory_test(data([0.001] * 39), parameters(split=20)).valid is False
    assert "主设定无效" in confirmatory_test(data([0.001] * 39), parameters(split=20)).reason
    result = confirmatory_test(data([0.001] * 40), parameters(split=21))
    assert result.valid and result.category == "A"
    # 敏感性行：b = 10 有效（40 ÷ 10 ≥ 2）；b = 40、60、120 的 n ÷ b < 2，只把该行标为无效，不影响主结论。
    assert [(row.block, row.valid) for row in result.sensitivities] == [(10, True), (40, False), (60, False),
                                                                        (120, False)]
    assert all(row.p_value is None and "n ÷ b < 2" in row.note for row in result.sensitivities[1:])


# ---------------------------------------------------------------------------
# 稳定性警示
# ---------------------------------------------------------------------------


def row(p_value: float | None, valid: bool = True) -> BootstrapRow:
    return BootstrapRow(10, 20261010, valid, p_value, None, None, "")


def test_stability_warning_triggers_one_each() -> None:
    quiet = ([row(0.01), row(0.0999)], (0.1, 0.2), [ZeroedEvent("SPX", numbered(5), 0.3)])
    assert stability_warnings(*quiet, 0.10) == ()
    # （一）有效的敏感性行中任一 p ≥ 0.10。
    assert stability_warnings([row(0.01), row(0.10)], quiet[1], quiet[2], 0.10) == (WARNING_SENSITIVITY,)
    assert stability_warnings([row(0.5, valid=False), row(None, valid=False)], quiet[1], quiet[2], 0.10) == ()
    # （二）前后两半方向不一致。
    assert stability_warnings(quiet[0], (0.1, -0.2), quiet[2], 0.10) == (WARNING_HALVES,)
    # （三）任一事件窗口置零后 Δ ≤ 0（不再为正）。
    zeroed = [ZeroedEvent("SPX", numbered(5), 0.3), ZeroedEvent("QQQ", numbered(9), -0.01)]
    assert stability_warnings(quiet[0], quiet[1], zeroed, 0.10) == (WARNING_ZEROING,)
    assert WARNING_ZEROING == "事件窗口置零后 Δ 不再为正"
    assert stability_warnings([row(0.2)], (0.0, 0.2), zeroed, 0.10) == (WARNING_SENSITIVITY, WARNING_HALVES,
                                                                        WARNING_ZEROING)


def test_zero_boundaries_of_rule_13() -> None:
    assert halves_consistent(0.1, 0.2) and halves_consistent(-0.1, -0.2)          # 同为正或同为负
    for first, second in ((0.1, -0.2), (-0.1, 0.2), (0.0, 0.2), (0.1, 0.0), (0.0, 0.0), (0.0, -0.1)):
        assert not halves_consistent(first, second)                                # 任一半等于 0 都算不一致
    # 置零后重算的 Δ 恰好等于 0：不再为正，触发警示。
    assert stability_warnings([], (0.1, 0.2), [ZeroedEvent("SPX", numbered(5), 0.0)], 0.10) == (WARNING_ZEROING,)
    assert stability_warnings([], (0.1, 0.2), [ZeroedEvent("SPX", numbered(5), 1e-12)], 0.10) == ()


def test_halves_warning_downgrades_a_to_unstable_but_keeps_the_original_label() -> None:
    """前一半每日 +0.002，后一半每日 −0.0001：Δ = 0.114 > 0，但后一半为负 → 方向不一致。"""
    logs = [*[0.002] * 60, *[-0.0001] * 60]
    assert independent_p(logs, 20261020, 20) < 0.05                               # 构造成立的前提
    result = confirmatory_test(data(logs), parameters())
    assert result.halves == pytest.approx((0.12, -0.006))
    assert (result.category, result.unstable) == ("A", True) and WARNING_HALVES in result.warnings
    assert result.conclusion == f"{UNSTABLE}（原类别 A：{WORDING['A']}）"          # 降为“证据不稳定”，保留原类别标注


def test_event_window_zeroing_downgrades_when_delta_is_no_longer_positive() -> None:
    """区间末日第 21 至 70 日每日 +0.004，其余每日 −0.0001：Δ = 0.2 − 0.007 = 0.193。

    SPX 事件 P = 第 41 日、Tr = 第 50 日：置零窗口为 [P 前第 20 个交易日, Tr 后第 20 个交易日] = 第 21 至 70 日，
    置零后 Δ = −0.007，不再为正。QQQ 事件 P = 第 100 日、Tr = 第 105 日：置零第 80 至 120 日（截在窗口内），Δ 仍为正。
    """
    logs = [0.004 if 21 <= number <= 70 else -0.0001 for number in range(1, N + 1)]
    assert independent_p(logs, 20261020, 20) < 0.05
    events = {"SPX": (event("SPX", 41, 50, 60),), "QQQ": (event("QQQ", 100, 105),)}
    result = confirmatory_test(data(logs, events=events), parameters())
    assert [(item.asset, item.peak) for item in result.zeroed] == [("QQQ", numbered(100)), ("SPX", numbered(41))]
    by_asset = {item.asset: item.delta for item in result.zeroed}                  # 按资产分别置零，不合并
    assert by_asset["SPX"] == pytest.approx(-0.007) and by_asset["QQQ"] == pytest.approx(0.193 + 41 * 0.0001)
    assert WARNING_ZEROING in result.warnings and WARNING_HALVES not in result.warnings
    assert (result.category, result.unstable) == ("A", True)
    assert result.conclusion.startswith("证据不稳定（原类别 A：")


def test_warnings_never_upgrade_and_do_not_change_c_or_d() -> None:
    logs = [1.0, *[-0.008] * (N - 1)]                                            # C：Δ > 0 但 p ≥ 0.05；后一半为负
    c_result = confirmatory_test(data(logs), parameters())
    assert WARNING_HALVES in c_result.warnings
    assert (c_result.category, c_result.unstable, c_result.conclusion) == ("C", False, WORDING["C"])
    d_logs = [*[0.002] * 60, *[-0.0001] * 60]
    d_result = confirmatory_test(data(d_logs, r1=False), parameters())
    assert WARNING_HALVES in d_result.warnings
    assert (d_result.category, d_result.unstable, d_result.conclusion) == ("D", False, WORDING["D"])
    # 没有警示时 A 不会变成别的；有警示时 A、B 只会降为“证据不稳定”。
    b_logs = [*[0.00002] * 60, *[-0.000001] * 60]                                # B 且后一半为负
    b_result = confirmatory_test(data(b_logs), parameters())
    assert (b_result.category, b_result.unstable) == ("B", True)
    assert b_result.conclusion == f"{UNSTABLE}（原类别 B：{WORDING['B']}）"


# ---------------------------------------------------------------------------
# 事件窗口置零的细节
# ---------------------------------------------------------------------------


def test_zeroing_goes_by_interval_end_day_and_is_clipped_to_the_window() -> None:
    days = end_days(60)
    ones = [1.0] * 60

    def zeroed_numbers(peak: int, trough: int) -> list[int]:
        kept = zero_event_window(days, ones, numbered(peak), numbered(trough), 20)
        return [number for number, value in zip(range(1, 61), kept, strict=True) if value == 0.0]

    assert zeroed_numbers(30, 35) == list(range(10, 56))          # 第 10 至 55 日：P − 20 到 Tr + 20
    assert zeroed_numbers(5, 8) == list(range(1, 29))             # 左端截在窗口内
    assert zeroed_numbers(50, 58) == list(range(30, 61))          # 右端截在窗口内
    assert zeroed_numbers(0, 3) == list(range(1, 24))             # P 早于第一个区间末日：只截到窗口起点
    assert zeroed_numbers(30, 30) == list(range(10, 51))
    assert zero_event_window(days, ones, numbered(30), numbered(35), 20)[8] == 1.0


def test_left_truncated_events_are_not_zeroed_and_unfinished_events_are() -> None:
    logs = [0.001] * N
    events = {"SPX": (event("SPX", 3, 9, 12), event("SPX", 60, 70)), "QQQ": ()}
    result = confirmatory_test(data(logs, events=events, first_signal_day=numbered(3)), parameters())
    # P = 第 3 日 ≤ f：左截断，不参与置零；P = 第 60 日的未结束事件参与。
    assert [(item.asset, item.peak) for item in result.zeroed] == [("SPX", numbered(60))]
    assert result.zeroed[0].delta == pytest.approx(0.12 - 51 * 0.001)          # 置零第 40 至 90 日，共 51 个区间
    assert result.category == "A" and not result.unstable


# ---------------------------------------------------------------------------
# 自身亏损时的措辞（登记第七节第 7 小节；实施口径补充第 16 条）
# ---------------------------------------------------------------------------

BANNED_WORDS = ("多赚", "盈利", "收益提高")


def test_own_loss_wording_when_candidate_loses_less_than_reference() -> None:
    result = confirmatory_test(data([-0.001] * N, [-0.002] * N, hold_log_wealth=-0.3), parameters())
    text = result.own_result
    assert result.delta == pytest.approx(0.12) and result.category == "A"       # 措辞不改变类别判定
    assert text is not None and text.startswith("候选在验证期自身亏损 11.31%，参照亏损 21.34%；相对参照少亏，")
    assert "年化相对净值增长率 +28.66%" in text and "一直持有的自身收益为 -25.92%" in text
    assert not any(word in text for word in BANNED_WORDS)


def test_own_loss_wording_is_neutral_in_all_other_cases() -> None:
    # 候选亏损、参照盈利：Δ < 0，只列数值，不写“少亏”。
    worse = confirmatory_test(data([-0.001] * N, [0.0005] * N, hold_log_wealth=0.2), parameters()).own_result
    assert worse is not None and "少亏" not in worse
    assert "候选在验证期的自身收益为 -11.31%" in worse and "参照的自身收益为 +6.18%" in worse
    assert "一直持有的自身收益为 +22.14%" in worse and "年化相对净值增长率" in worse
    # 候选与参照都亏损，但候选亏得更多：Δ < 0，同样只列数值。
    more = confirmatory_test(data([-0.002] * N, [-0.001] * N, hold_log_wealth=-0.3), parameters()).own_result
    assert more is not None and "少亏" not in more and "参照的自身收益为 -11.31%" in more
    for text in (worse, more):
        assert not any(word in text for word in BANNED_WORDS)
    # 候选不亏损：不需要这段措辞。
    assert confirmatory_test(data([0.001] * N, [-0.001] * N), parameters()).own_result is None
    with pytest.raises(ConfirmatoryError, match="一直持有"):
        confirmatory_test(data([-0.001] * N, [-0.002] * N, hold_log_wealth=None), parameters())


def test_inputs_are_validated() -> None:
    with pytest.raises(ConfirmatoryError, match="升序"):
        confirmatory_test(data([0.001] * 3, end_days=(numbered(2), numbered(1), numbered(3))), parameters())
    with pytest.raises(ConfirmatoryError, match="等长"):
        confirmatory_test(data([0.001] * 3, end_days=end_days(4)), parameters())


# ---------------------------------------------------------------------------
# 补修：非有限数的输入有效性保护（NaN 参与的比较一律为假，不先检查就会被悄悄放行）
# ---------------------------------------------------------------------------

NON_FINITE = (math.nan, math.inf, -math.inf)


def assert_invalid(result, reason: str) -> None:
    """“计算无效”出口：没有类别、没有两个独立判断、没有任何收益结论文字。"""
    assert (result.valid, result.category, result.conclusion, result.own_result) == (False, None, INVALID, None)
    assert (result.improvement_passed, result.magnitude_reached) == (None, None)
    assert (result.delta, result.delta_min, result.annual_growth, result.main) == (None, None, None, None)
    assert reason in result.reason and "nan%" not in result.reason and "inf%" not in result.reason


@pytest.mark.parametrize("field", ["candidate_log_wealth", "reference_log_wealth"])
@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_log_wealth_is_invalid_computation(field: str, value: float) -> None:
    """候选或参照的累计对数净值为 NaN、+∞、−∞（共 6 例）：一律“计算无效”。

    理由：对账写的是 abs(x − y) > 容差，NaN 参与时为假，若不先检查，非有限值会通过对账并得到有效类别与含 nan% 的文字。
    正常的收益序列本身是有限的（每日 +0.001），只有累计对数净值这一项不是有限数。
    """
    result = confirmatory_test(data([0.001] * N, **{field: value}), parameters())
    name = "候选" if field == "candidate_log_wealth" else "参照"
    assert_invalid(result, f"{name}的累计对数净值不是有限数")
    assert str(value) in result.reason                         # 原因写明是哪一项、什么值


@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_hold_log_wealth_is_invalid_when_candidate_loses(value: float) -> None:
    """候选自身亏损时结论文字要列出一直持有的自身收益：它为 NaN、+∞、−∞（共 3 例）时“计算无效”。"""
    losing = data([-0.001] * N, [-0.002] * N, hold_log_wealth=value)
    assert_invalid(confirmatory_test(losing, parameters()), "一直持有的累计对数净值不是有限数")
    # 对照：同样的输入，一直持有为有限值时是有效结论。
    assert confirmatory_test(data([-0.001] * N, [-0.002] * N, hold_log_wealth=-0.3), parameters()).valid


def test_non_finite_hold_log_wealth_does_not_matter_when_candidate_does_not_lose() -> None:
    """候选不亏损时，一直持有的累计对数净值为 NaN 不影响结果：结论文字不使用它，统计量与类别也不依赖它。"""
    finite = confirmatory_test(data([0.001] * N, hold_log_wealth=0.1), parameters())
    with_nan = confirmatory_test(data([0.001] * N, hold_log_wealth=math.nan), parameters())
    assert with_nan == finite and with_nan.valid and with_nan.category == "A" and with_nan.own_result is None


def test_finite_values_still_give_valid_results_and_finite_mismatch_is_still_invalid() -> None:
    """保留的两种情形：正常有限值得出有效结论；有限值但对账不一致时为“计算无效”。"""
    assert confirmatory_test(data([0.001] * N), parameters()).valid
    mismatch = confirmatory_test(data([0.001] * N, candidate_log_wealth=0.5), parameters())
    assert_invalid(mismatch, "对账不符")


@pytest.mark.parametrize("name", ["alpha", "warning_p", "minimum_growth", "tolerance"])
@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_parameters_are_rejected(name: str, value: float) -> None:
    """参数类输入非有限即抛异常。例如容差为 NaN 时，“差值 > 容差”恒为假，任何对账都会通过。"""
    fields = dict(main=BlockSetting(20, 20261020), sensitivities=(), resamples=RESAMPLES, alpha=0.05, warning_p=0.10,
                  annual_days=252, minimum_growth=0.01, tolerance=1e-10, padding=20, split=numbered(61))
    ConfirmatoryParameters(**fields)                           # 有限值可以构造
    fields[name] = value
    with pytest.raises(ConfirmatoryError, match="有限数"):
        ConfirmatoryParameters(**fields)


def test_helper_functions_reject_non_finite_values() -> None:
    """类别判定等函数直接收到非有限值时抛异常，不把它当作某个有效类别放行。"""
    for value in NON_FINITE:
        for arguments in ((True, value, 0.01, 0.004, 0.05), (True, 0.01, value, 0.004, 0.05),
                          (True, 0.01, 0.01, value, 0.05), (True, 0.01, 0.01, 0.004, value),
                          (False, value, 0.01, 0.004, 0.05)):
            with pytest.raises(ConfirmatoryError, match="有限数"):
                category_of(*arguments)
        with pytest.raises(ConfirmatoryError):
            improvement_passed(value, 0.01, 0.05)
        with pytest.raises(ConfirmatoryError):
            halves_consistent(value, 0.1)
        with pytest.raises(ConfirmatoryError):
            halves_consistent(0.1, value)
        with pytest.raises(ConfirmatoryError):
            right_tail_p(value, [0.1, 0.2])
        with pytest.raises(ConfirmatoryError):
            right_tail_p(0.1, [0.1, value])
        with pytest.raises(ConfirmatoryError):
            stability_warnings([row(value)], (0.1, 0.2), [], 0.10)
        with pytest.raises(ConfirmatoryError):
            stability_warnings([row(0.01)], (0.1, 0.2), [ZeroedEvent("SPX", numbered(5), value)], 0.10)
        with pytest.raises(ConfirmatoryError):
            stability_warnings([row(0.01)], (0.1, 0.2), [], value)
    # 无效的敏感性行没有 p（None），不受此限。
    assert stability_warnings([row(None, valid=False)], (0.1, 0.2), [], 0.10) == ()
