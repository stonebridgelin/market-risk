"""阶段五确认性检验接线与输出文档（validation_run.confirm、validation_output）的构造测试
（V2 指令第五节：T-INV1—5、T-INV5b、T-INV6、T-INV6b、T-CAT、T-WARN、T-OWN、T-OUT 的文档部分）。纯算法测试。

检验输入多为直接构造的 ConfirmatoryInput（冻结路径上的收益序列），期望值手工推导：
- 常数差 d_j = c：每条重抽样序列的和都等于 Δ = n·c，Δ* − Δ = 0，c > 0 时 #{Δ* − Δ ≥ Δ} = 0，所以 p = 1 ÷ (B + 1)；
- Δ_min = (n ÷ 252)·ln(1.01)；n = 100 时 Δ_min = 0.0039486…；
- 需要非常数差的情形（前后两半、置零），以替身固定 bootstrap_row 的结果（L3 式替身，只验证接线与判定），
  p 值本身由阶段三已验收的 confirmatory 测试覆盖。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from decimal import Decimal

import pytest

from market_risk.calendar import stock_trading_days
from market_risk.storage.paths import StoragePaths
from market_risk.wavewarn_v20 import confirmatory
from market_risk.wavewarn_v20 import registered_v20_validation as registered
from market_risk.wavewarn_v20 import validation_output as output
from market_risk.wavewarn_v20.confirmatory import (
    WARNING_HALVES,
    WARNING_SENSITIVITY,
    WARNING_ZEROING,
    BootstrapRow,
    ConfirmatoryInput,
    confirmatory_test,
)
from market_risk.wavewarn_v20.development_output import PREFLIGHT_FILES as DEVELOPMENT_PREFLIGHT
from market_risk.wavewarn_v20.development_output import csv_bytes, gzip_bytes
from market_risk.wavewarn_v20.execution import SignalRecord
from market_risk.wavewarn_v20.labels_r2 import R2Event
from market_risk.wavewarn_v20.state_machine import Risk
from market_risk.wavewarn_v20.validation_output import OutputError

B = 50                                              # 构造检验的重抽样次数（登记值 10,000 只用于正式运行）
START = dt.date(2017, 1, 3)
DAYS = tuple(START + dt.timedelta(days=index) for index in range(100))      # 100 个收益区间末日（只作标签）
SPLIT = DAYS[50]                                    # 前 50 个属于前一半
PARAMETERS = dataclasses.replace(registered.VALIDATION_CONFIRMATORY, resamples=B, split=SPLIT)
PERIOD = dataclasses.replace(registered.VALIDATION, confirmatory=PARAMETERS)
DELTA_MIN = 100 / 252 * math.log1p(0.01)


def data(differences: list[float] | None = None, candidate: list[float] | None = None,
         reference: list[float] | None = None, r1: bool | None = True, r2: dict | None = None,
         events: dict | None = None, days: tuple[dt.date, ...] = DAYS, log_offset: float = 0.0) -> ConfirmatoryInput:
    """构造检验输入。给 differences 时参照收益为 0、候选收益为 expm1(d)（于是 ln(1+R_c) − ln(1+R_r) = d）。
    ln W 由同一收益序列求和得到（输入的一部分，不是期望值）。"""
    if differences is not None:
        candidate = [math.expm1(value) for value in differences]
        reference = [0.0] * len(differences)
    mine = None if candidate is None else math.fsum(math.log1p(value) for value in candidate) + log_offset
    theirs = None if reference is None else math.fsum(math.log1p(value) for value in reference)
    return ConfirmatoryInput("验证期", days, None if candidate is None else tuple(candidate),
                             None if reference is None else tuple(reference), mine, theirs, 0.0, r1,
                             r2 if r2 is not None else {"SPX": True, "QQQ": True}, events or {}, dt.date(2016, 12, 29))


def document(item: ConfirmatoryInput, period: registered.PeriodSpec = PERIOD) -> dict:
    return output.confirmatory_document(confirmatory_test(item, period.confirmatory), period)


# ---------------------------------------------------------------------------
# T-INV1—5：主检验计算无效（登记第七节第 5 小节前五项）→ 不写类别、不写优劣
# ---------------------------------------------------------------------------


def assert_invalid(result: dict, reason: str) -> None:
    assert result["valid"] is False and result["outcome"] == "计算无效" and result["category"] is None
    assert result["reason"].startswith(reason), result["reason"]
    assert result["conclusion"] == "计算无效" and "不写任何优劣结论" in result["statement"]


def test_inv1_empty_window_and_non_finite() -> None:
    assert_invalid(document(data(differences=[], days=())), "评价窗口为空")
    values = [1e-4] * 100
    values[7] = math.nan
    assert_invalid(document(data(candidate=values, reference=[0.0] * 100)), "候选的累计对数净值不是有限数")
    assert_invalid(document(data(candidate=[0.0] * 99 + [math.inf], reference=[0.0] * 100)),
                   "候选的累计对数净值不是有限数")


def test_inv2_reconciliation_mismatch() -> None:
    """ln W_候选,末 比 Σ ln(1 + R) 大 1e-6（> 容差 1e-10）→ 对账不符。"""
    assert_invalid(document(data(differences=[1e-4] * 100, log_offset=1e-6)), "对账不符")


def test_inv3_missing_price() -> None:
    assert_invalid(document(data(candidate=None, reference=[0.0] * 100)), "净值所需价格缺失")


def test_inv4_r2_uncomputable() -> None:
    assert_invalid(document(data(differences=[1e-4] * 100, r2={"SPX": None, "QQQ": True})), "R2 无法计算")


def test_inv5_main_setting_n_over_b_below_two() -> None:
    """n = 30，主设定 b = 20：30 < 2 × 20 → 主设定无效 → 整体计算无效。"""
    assert_invalid(document(data(differences=[1e-4] * 30, days=DAYS[:30])), "主设定无效：n ÷ b < 2")


def test_inv5b_main_setting_resampling_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """主设定的重抽样过程中注入异常（种子为主设定种子时抛 ValueError）→ 主设定无效 → 整体计算无效。"""
    real = confirmatory.stationary_bootstrap_indices

    def failing(length: int, block: int, resamples: int, seed: int, generator=None):
        if seed == PARAMETERS.main.seed:
            raise ValueError("构造：主设定重抽样出错")
        return real(length, block, resamples, seed, generator)

    monkeypatch.setattr(confirmatory, "stationary_bootstrap_indices", failing)
    assert_invalid(document(data(differences=[1e-4] * 100)), "主设定无效：重抽样过程出错")


# ---------------------------------------------------------------------------
# T-INV6、T-INV6b：敏感性单行无效 → 只标该行，“敏感性证据不完整”，不计通过、不计警示
# ---------------------------------------------------------------------------


def test_inv6_sensitivity_n_over_b_below_two() -> None:
    """n = 100：b = 60（需 120）与 b = 120（需 240）两行 n ÷ b < 2，只标这两行无效；b = 10、40 有效。
    常数差 c = 1e-4 > 0：p = 1/(50 + 1) < 0.05；Δ = 0.01 ≥ Δ_min → A；
    有效行 p < 0.10，无效行不计警示 → 无稳定性警示。"""
    result = document(data(differences=[1e-4] * 100))
    assert result["valid"] is True and result["category"] == "A" and result["unstable"] is False
    assert result["warnings"] == [] and result["sensitivity_incomplete"] is True
    assert [item["block"] for item in result["invalid_sensitivities"]] == [60, 120]
    assert [row["valid"] for row in result["sensitivities"]] == [True, True, False, False]
    assert result["main"]["p_value"] == 1 / (B + 1)
    assert all(row["p_value"] == 1 / (B + 1) for row in result["sensitivities"] if row["valid"])


def test_inv6b_sensitivity_row_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """b = 40 那一行的重抽样注入异常：只该行无效；主检验与其余行照常；类别不变（A），不计警示。"""
    real = confirmatory.stationary_bootstrap_indices

    def failing(length: int, block: int, resamples: int, seed: int, generator=None):
        if seed == 20261040:
            raise ValueError("构造：敏感性行出错")
        return real(length, block, resamples, seed, generator)

    monkeypatch.setattr(confirmatory, "stationary_bootstrap_indices", failing)
    result = document(data(differences=[1e-4] * 100))
    assert result["valid"] is True and result["category"] == "A" and result["warnings"] == []
    rows = {row["block"]: row for row in result["sensitivities"]}
    assert rows[40]["valid"] is False and rows[40]["note"].startswith("重抽样过程出错")
    assert rows[10]["valid"] is True and [item["block"] for item in result["invalid_sensitivities"]] == [40, 60, 120]


def test_report_lists_incomplete_sensitivity_evidence() -> None:
    result = document(data(differences=[1e-4] * 100))
    text = render(result).decode("utf-8")
    assert "敏感性证据不完整（无效行：b = 60、b = 120）：不计为通过，也不计作 p ≥ 0.10 的警示" in text


# ---------------------------------------------------------------------------
# T-CAT：类别 A、B、C、D；资格与收益不互相补偿
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("c", "r1", "expected"), [
    (1e-4, True, "A"),           # Δ = 0.01 ≥ Δ_min = 0.00395，p = 1/51 < 0.05
    (1e-5, True, "B"),           # Δ = 0.001 < Δ_min，p = 1/51 < 0.05
    (-1e-4, True, "C"),          # Δ < 0
    (0.0, True, "C"),            # Δ = 0：不通过改善检验
    (1e-4, False, "D"),          # 收益达到 A 的条件，但 R1 不满足：D（不补偿）
])
def test_categories(c: float, r1: bool, expected: str) -> None:
    result = document(data(differences=[c] * 100, r1=r1))
    assert result["category"] == expected
    assert result["conclusion"] == confirmatory.WORDING[expected]
    assert result["magnitude_reached"] is (c * 100 >= DELTA_MIN)


def test_category_d_when_one_asset_r2_fails() -> None:
    assert document(data(differences=[1e-4] * 100, r2={"SPX": True, "QQQ": False}))["category"] == "D"


# ---------------------------------------------------------------------------
# T-WARN：三种稳定性警示，各一例；只降低 A、B 的强度
# ---------------------------------------------------------------------------


def fixed_rows(monkeypatch: pytest.MonkeyPatch, sensitivity_p: dict[int, float]) -> None:
    """替身：主设定 p = 0.01；敏感性行按给定 p（未给出的为 0.01）；全部有效。"""

    def row(differences, delta, setting, resamples):
        p = 0.01 if setting.block == 20 else sensitivity_p.get(setting.block, 0.01)
        return BootstrapRow(setting.block, setting.seed, True, p, -1.0, 1.0, "")

    monkeypatch.setattr(confirmatory, "bootstrap_row", row)


def test_warning_sensitivity_p_at_least_ten_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    """b = 40 的 p = 0.2 ≥ 0.10 → 敏感性警示；A 降为“证据不稳定”并保留原类别。"""
    fixed_rows(monkeypatch, {40: 0.2})
    result = document(data(differences=[1e-4] * 100))
    assert result["category"] == "A" and result["unstable"] is True and result["warnings"] == [WARNING_SENSITIVITY]
    assert result["conclusion"] == f"证据不稳定（原类别 A：{confirmatory.WORDING['A']}）"


def test_warning_halves_inconsistent(monkeypatch: pytest.MonkeyPatch) -> None:
    """前 50 个区间 d = 2e-4、后 50 个 d = −1e-4：Δ = 0.01 − 0.005 = 0.005 ≥ Δ_min → A；前后两半方向不一致 → 警示。"""
    fixed_rows(monkeypatch, {})
    result = document(data(differences=[2e-4] * 50 + [-1e-4] * 50))
    assert result["category"] == "A" and result["warnings"] == [WARNING_HALVES] and result["unstable"] is True
    assert result["halves"]["first"] > 0 > result["halves"]["second"]


def test_warning_zeroing_and_no_downgrade_for_c(monkeypatch: pytest.MonkeyPatch) -> None:
    """d 只在事件窗口内为正：高点 DAYS[40]、低点 DAYS[45]，置零范围为区间末日落在 [P 前 20 个, Tr 后 20 个] 的区间，
    即下标 20—65；只令下标 40—45 的 d = 1e-3
    （前后两半各有正值：40—45 落在 split=下标 50 之前，另令下标 60 的 d = 1e-3），
    其余为 0。Δ = 7e-3 > Δ_min；置零后 Δ = 0 ≤ 0 → 置零警示 → A 降为不稳定。
    同一警示在 C 类（Δ ≤ 0）时只列出，不改变类别与结论。"""
    fixed_rows(monkeypatch, {})
    values = [0.0] * 100
    for index in (*range(40, 46), 60):
        values[index] = 1e-3
    event = R2Event("SPX", DAYS[40], Decimal("100"), DAYS[42], DAYS[43], DAYS[45], Decimal("90"), DAYS[50], False)
    result = document(data(differences=values, events={"SPX": (event,)}))
    assert result["category"] == "A" and WARNING_ZEROING in result["warnings"] and result["unstable"] is True
    assert [item["delta"] for item in result["zeroed"]] == [0.0]
    negative = document(data(differences=[-x for x in values], events={"SPX": (event,)}))
    assert negative["category"] == "C" and negative["unstable"] is False
    assert negative["conclusion"] == confirmatory.WORDING["C"]


# ---------------------------------------------------------------------------
# T-OWN：自身亏损时的措辞
# ---------------------------------------------------------------------------


def test_own_loss_wording() -> None:
    """候选每区间 −0.1%、参照 −0.2%：候选自身亏损且 Δ > 0 → 写“相对参照少亏”，并注明期间“验证期”。
    候选与参照同为 −0.1%（Δ = 0）时只用中性的数值描述，不写“少亏”。"""
    result = document(data(candidate=[-0.001] * 100, reference=[-0.002] * 100))
    assert result["own_result"].startswith("候选在验证期自身亏损") and "相对参照少亏" in result["own_result"]
    neutral = document(data(candidate=[-0.001] * 100, reference=[-0.001] * 100))
    assert neutral["own_result"].startswith("候选在验证期的自身收益为") and "少亏" not in neutral["own_result"]


# ---------------------------------------------------------------------------
# 报告固定文字与措辞上限（T-OUT 的文档部分）
# ---------------------------------------------------------------------------

WINDOW = {"first_day": dt.date(2016, 12, 30), "last_day": dt.date(2022, 12, 30), "n": 100}
DATES = {"checks": [{"item": "j₀（first_return_day）", "expected": dt.date(2016, 12, 30),
                     "actual": dt.date(2016, 12, 30), "passed": True}],
         "d15": {"j0": 6804, "t0": 2383, "offset": 63, "kappa_all": 2400, "warmup_ok": True, "convergence_ok": True}}
OVERLAP = {"baseline": "evaluation_development/daily_signals.csv.gz", "outcome": "相同"}
WARNING = ("**警示：SPX 的 R2 资格依赖单一提示段（2018-01-02—2018-03-01，40 个信号日）对 3 个事件的持续覆盖；"
           "剔除该段的持续覆盖后，R2 比例低于 60%。**")
DIAGNOSTICS = {"computed": True, "d1_new_prompt_share": [],
               "statements": ["SPX 的 R2 达标部分依赖持续覆盖；只计新提示时未达到 60%。"],
               "d2_abnormal_occupancy": {"window_days": 1, "level1_days": 0, "level2_days": 0, "undetermined_days": 0},
               "d3_long_hang": [], "warnings": [WARNING], "reclassification_note": "重分类压力诊断"}


def render(confirmed: dict, diagnostics: dict | None = None, gate: dict | None = None) -> bytes:
    return output.render_report(PERIOD, WINDOW, DATES, OVERLAP, confirmed, diagnostics or DIAGNOSTICS, [], gate)


def test_report_fixed_texts_and_references() -> None:
    text = render(document(data(differences=[1e-4] * 100))).decode("utf-8")
    assert text.startswith("# 波段预警 v2.0 验证期运行报告\n")
    for fixed in (output.TIMING_TEXT, *output.SECTION0_ITEM7, *output.SECTION3_ITEM5, output.WORDING_LIMIT,
                  output.OVERLAP_SCOPE, output.data_scope_text(PERIOD)):
        assert fixed in text
    for digest in (registered.LOCK_RECORD_SHA256, registered.LOCK_SUPPLEMENT_SHA256, registered.STANDARD_R3_SHA256,
                   registered.V1_DESIGN_SHA256):
        assert digest in text
    assert "实盘有效" not in text
    # 显著警示：报告摘要首屏列出，并在结论段的类别措辞之后紧接一行加粗文字。
    summary = text.split("## 依据")[0]
    assert WARNING in summary
    lines = text.splitlines()
    category = next(index for index, line in enumerate(lines) if line.startswith("- 类别："))
    assert lines[category + 1] == WARNING


def test_report_for_invalid_computation_has_no_category() -> None:
    invalid = document(data(candidate=None, reference=[0.0] * 100))
    text = render(invalid, {"computed": False, "reason": "计算无效时不计算本节"}).decode("utf-8")
    assert "- 类别：" not in text and "不写任何优劣结论" in text and "未计算：计算无效时不计算本节" in text
    assert WARNING not in text and "- 结论：计算无效（净值所需价格缺失" in text
    # A 类措辞只出现在固定的“措辞上限”一句中；B、C、D 的措辞都不出现。
    assert text.count(confirmatory.WORDING["A"]) == 1 and confirmatory.WORDING["A"] in output.WORDING_LIMIT
    assert all(confirmatory.WORDING[name] not in text for name in ("B", "C", "D"))


def test_report_holdout_gate_statement() -> None:
    gate = {"validation_manifest_sha256": "a" * 64, "validation_category": "A", "authorization_sha256": "b" * 64}
    holdout = dataclasses.replace(registered.HOLDOUT, confirmatory=PARAMETERS)
    text = output.render_report(holdout, WINDOW, DATES, OVERLAP, document(data(differences=[1e-4] * 100)),
                                DIAGNOSTICS, [], gate).decode("utf-8")
    assert text.startswith("# 波段预警 v2.0 保留期运行报告\n")
    assert "（入口只校验格式并照录，程序未验证授权）" in text and "a" * 64 in text and "b" * 64 in text


# ---------------------------------------------------------------------------
# 位置、预检集合、基准解析与格式化（不读文件）
# ---------------------------------------------------------------------------


def test_locations_replace_only_the_directory_name(tmp_path) -> None:
    research = StoragePaths(tmp_path).v20_research_dir
    for period, name in ((registered.VALIDATION, "evaluation_validation"), (registered.HOLDOUT, "evaluation_holdout")):
        places = output.locations(tmp_path, period.formal_name, "20261017T000000Z", "abcdef0")
        stamp = "20261017T000000Z_abcdef0"
        assert (places.formal, places.staging, places.failed, places.tail, places.publish_failure) == (
            research / name, research / f".staging_{name}_{stamp}", research / f"{name}_failed_{stamp}",
            research / f"{name}_audit_tail_{stamp}.jsonl", research / f"{name}_publish_failure_{stamp}.json")
    manifest, signals = output.development_baseline(tmp_path)
    assert (manifest, signals) == (research / "evaluation_development" / "MANIFEST.sha256",
                                   research / "evaluation_development" / "daily_signals.csv.gz")
    assert output.diagnostics_name(registered.VALIDATION) == "diagnostics_validation.json"
    assert output.diagnostics_name(registered.HOLDOUT) == "diagnostics_holdout.json"


def test_preflight_set_is_the_development_set_plus_four_modules() -> None:
    assert output.PREFLIGHT_FILES == (*DEVELOPMENT_PREFLIGHT, *(
        f"src/market_risk/wavewarn_v20/{name}.py"
        for name in ("registered_v20_validation", "validation_diagnostics", "validation_output", "validation_run")))
    assert len(output.PREFLIGHT_FILES) == 39


def test_parse_signals_header_and_fields() -> None:
    rows = [("2016-12-29", "主参照", "正常", "0", None, None, "true")]
    good = gzip_bytes(csv_bytes(("date", "object", "risk", "level", "c1", "c2", "all_valid"), rows))
    assert output.parse_signals(good) == [("2016-12-29", "主参照", "正常", "0", "", "", "true")]
    bad = gzip_bytes(csv_bytes(("date", "object", "risk", "level", "c1", "c2", "valid"), rows))
    with pytest.raises(OutputError, match="表头"):
        output.parse_signals(bad)
    short = gzip_bytes(b"date,object,risk,level,c1,c2,all_valid\n2016-12-29,x,y\n")
    with pytest.raises(OutputError, match="字段数"):
        output.parse_signals(short)


def test_signal_cells_follow_the_development_rules() -> None:
    day = dt.date(2016, 12, 29)
    assert output.signal_cells(SignalRecord(day, Risk.LEVEL1, 2, 0, 1, True)) == ("一级", "2", "0", "1", "true")
    assert output.signal_cells(SignalRecord(day, Risk.NORMAL, None, None, None, False)) == ("正常", "", "", "", "false")
    assert output.signal_cells(SignalRecord(day, Risk.LEVEL2, 2, None, None, True))[0] == "二级"


# ---------------------------------------------------------------------------
# confirm 的接线（组合层结果 → 检验输入 → 检验）
# ---------------------------------------------------------------------------


def test_confirm_wiring_on_a_constructed_window() -> None:
    """构造轴（2002—2005，固定起点 2003-12-31）上组合层的冻结候选结果：confirm 以期间名“验证期”与给定参数组装并检验；
    n 等于窗口的收益区间数；首个信号日等于 j₀ 的前一交易日。"""
    from wavewarn_v20_helpers import random_closes

    from market_risk.wavewarn_v20.research_run import Continuity, Purpose, WindowSpec, confirmatory_input, run_window
    from market_risk.wavewarn_v20.snapshot import make_snapshot
    from market_risk.wavewarn_v20.validation_run import confirm

    axis = tuple(stock_trading_days(dt.date(2002, 1, 2), dt.date(2005, 12, 30)))
    closes = {"SPX": random_closes(2, len(axis)), "QQQ": random_closes(3, len(axis))}
    prices = {asset: dict(zip(axis, values, strict=True)) for asset, values in closes.items()}
    snapshot = make_snapshot(axis, prices, axis[-1], dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                             {"SPX": "0" * 64, "QQQ": "0" * 64})
    spec = WindowSpec(Purpose.FROZEN, dt.date(2003, 12, 31), axis[-1], {"SPX": axis[0], "QQQ": axis[0]},
                      registered.INITIAL, Continuity.COMPLETE_TRADING_AXIS)
    result = run_window(snapshot, spec, registered.run_parameters(), (registered.FROZEN_CANDIDATE,))
    assert result.stop is None
    period = dataclasses.replace(PERIOD, confirmatory=dataclasses.replace(PARAMETERS, split=dt.date(2005, 1, 1)))
    tested = confirm(result, registered.FROZEN_CANDIDATE, period)
    assembled = confirmatory_input(result, registered.FROZEN_CANDIDATE, "验证期")
    assert tested == confirmatory_test(assembled, period.confirmatory)
    assert tested.n == result.window.n == len(axis) - 1 - axis.index(dt.date(2003, 12, 31))
    assert assembled.first_signal_day == dt.date(2003, 12, 30) and assembled.period == "验证期"
    assert tested.main is not None and (tested.main.block, tested.main.seed) == (20, 20261020)
