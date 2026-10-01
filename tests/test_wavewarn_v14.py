"""v1.4 修订的测试；期望值均由构造数据手算，推算过程写在各测试的注释里。

除“MR 与 200 日均线参照行信号逐日相同”一项读取开发期价格（截断到 2016-12-30）外，全部数据都是构造的；
没有读取验证期或保留期数据，也没有运行验证期评价。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn.channels import ChannelDay, ChannelPredicate, run_channel, update_channel
from market_risk.wavewarn.config_v14 import (
    DELAY_QQQ,
    DELAY_SPX,
    NON_GREEN,
    REGISTERED_TIERS,
    V14Limits,
    load_v14_config,
    parse_v14_config,
)
from market_risk.wavewarn.evaluation import Candidate
from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.exit_costs import ExitCostEvent
from market_risk.wavewarn.extended_history import truncate_inputs
from market_risk.wavewarn.extended_history_v14 import prepare_price_window
from market_risk.wavewarn.feasibility_v14 import (
    TierItem,
    condition_flags,
    green_delay,
    select_by_tiers,
)
from market_risk.wavewarn.features import asset_features
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.inputs import load_development_inputs
from market_risk.wavewarn.state_machine import ReadyInputs, SystemMemory, step_system
from market_risk.wavewarn.timing import ma200_signals, timing_differences
from market_risk.wavewarn.v14_model import (
    FULL,
    MR_CHANNEL,
    NO_TREND,
    PRICE_ONLY,
    release_f_inputs,
    trend_predicates,
    v14_channels,
    v14_grid,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_v14_config(ROOT / "config/wavewarn_v14.yaml")
LIMITS = V14Limits(Decimal("0.60"), Decimal(8))
START = dt.date(2020, 1, 1)


# ---------------------------------------------------------------------------
# 趋势层 MR
# ---------------------------------------------------------------------------

def test_mr_matches_ma200_reference_signal_on_all_development_days() -> None:
    inputs = load_development_inputs(ROOT, CONFIG.base.development_end())
    assert inputs.days[-1] == dt.date(2016, 12, 30)                    # 只读到开发期末
    closes = tuple(inputs.series["SPX"].get(day) for day in inputs.days)
    statuses = run_channel(inputs.days, trend_predicates(closes, CONFIG.mr_window), "armed")
    first = CONFIG.mr_window - 1                                       # 200 日均线自第 200 个交易日起可用
    signals = ma200_signals(inputs.days, inputs.series["SPX"], inputs.days[first], CONFIG.mr_window)
    # 自均线可用起的每个交易日（含全部开发期评价日）：MR 激活 ⇔ 参照行信号为红，且通道当日有效。
    assert len(signals) == len(inputs.days) - first > 6000
    assert all(row.valid and (row.status == "active") == (light == "红")
               for row, light in zip(statuses[first:], signals, strict=True))
    assert {"绿", "红"} == set(signals)
    # 均线不可用的日子通道无效、状态沿用，不会被当作“未激活”。
    assert all(not row.valid and row.status == "armed" for row in statuses[:first])


def test_mr_has_no_hysteresis_and_rearms_immediately() -> None:
    # 窗口取 3（构造用）。收盘 10、10、10、9、9、10、8：前两日均线不可用；
    # 第3日均线 10，收盘 10 不低于 → 未激活；第4日均线 9.667，收盘 9 低于 → 激活；
    # 第5日均线 9.333，9 低于 → 激活；第6日均线 9.333，10 不低于 → 退出；第7日均线 9，8 低于 → 当日即重新进入。
    closes = tuple(Decimal(value) for value in ("10", "10", "10", "9", "9", "10", "8"))
    days = tuple(START + dt.timedelta(days=offset) for offset in range(7))
    rows = run_channel(days, trend_predicates(closes, 3), "armed")
    assert [row.status == "active" for row in rows] == [False, False, False, True, True, False, True]
    assert [row.valid for row in rows] == [False, False, True, True, True, True, True]
    # 缺收盘价当日无效，状态沿用（此处沿用“激活”）。
    gap = run_channel(days, trend_predicates((*closes[:5], None, closes[6]), 3), "armed")
    assert (gap[5].valid, gap[5].status) == (False, "active")


def test_v14_variants_have_registered_channels() -> None:
    days = tuple(START + dt.timedelta(days=offset) for offset in range(3))
    flat = {day: Decimal(100) for day in days}
    fixed = CONFIG.base.fixed_parameters()
    spx = asset_features(days, flat, {}, Decimal("0.10"), fixed)
    candidate = Candidate(FULL, 3, Decimal("0.015"), None, 0, "F")
    names = {model: set(v14_channels(model, spx, spx, (None,) * 3, candidate, fixed, CONFIG.mr_window))
             for model in (FULL, NO_TREND, PRICE_ONLY)}
    price = {"P_SPX", "PR_SPX", "P_QQQ", "PR_QQQ"}
    # v1.4：MR + 两资产 P、PR + 两资产 BW + V；没有 B、DV。去掉 MR 版少一个 MR；纯价格版只有 MR 与价格通道。
    assert names[FULL] == price | {"BW_SPX", "BW_QQQ", "V", MR_CHANNEL}
    assert names[NO_TREND] == names[FULL] - {MR_CHANNEL} and names[PRICE_ONLY] == price | {MR_CHANNEL}
    grid = v14_grid(CONFIG, (FULL, NO_TREND), 0)
    # 每个变体 9 组，登记顺序先 K 后 θ_P。
    assert [(row.k, row.theta_p) for row in grid[:4]] == [
        (3, Decimal("0.015")), (3, Decimal("0.02")), (3, Decimal("0.025")), (5, Decimal("0.015"))]
    assert len(grid) == 18 and {row.exit_version for row in grid} == {"F"}


# ---------------------------------------------------------------------------
# 解除规则 F
# ---------------------------------------------------------------------------

def _run_release_f(k: int, length: int, invalid_inputs: frozenset[int] = frozenset(),
                   missing_red: frozenset[int] = frozenset()) -> list[tuple[str, str]]:
    """只有价格通道 P_SPX（黄）、PR_SPX（红）。第0日为最终低点：创新低（Q=0）且两通道进入；第 d 日 Q=d。
    通道在 Q≥K 时退出。invalid_inputs 中的日子降级输入缺失；missing_red 中的日子红通道的退出输入缺失。"""
    statuses = {"P_SPX": "armed", "PR_SPX": "armed"}
    memory, result = SystemMemory("绿", 0, 0, 0), []
    for day in range(length):
        date = START + dt.timedelta(days=day)
        rows = {}
        for name, level in (("P_SPX", "黄"), ("PR_SPX", "红")):
            unknown = name == "PR_SPX" and day in missing_red
            predicate = ChannelPredicate(day == 0, None if unknown else day >= k, False, False, "")
            update = update_channel(date, statuses[name], predicate)  # type: ignore[arg-type]
            statuses[name] = update.status
            rows[name] = (level, update)
        inputs = ReadyInputs(day, day, None, None, None, None, None, day not in invalid_inputs)
        system = step_system(date, memory, rows, inputs, k, "F", 3, 5)  # type: ignore[arg-type]
        memory = system.memory
        result.append((memory.light, system.data_status))
    return result


def test_release_rule_f_timeline_for_price_only_event() -> None:
    lights = [light for light, _ in _run_release_f(3, 7)]
    # 第0日两通道进入 → 红。第1、2日 Q=1、2<K，通道仍激活 → 红。
    # 第3日 Q=3=K：两通道退出，全部红灯通道当日有效且未激活、Q≥K → 红转黄（低点后第3天）。
    # 当天黄转绿的条件其实也已满足，但每天最多降一级，所以第3日为黄，第4日才出绿灯信号。
    assert lights == ["红", "红", "红", "黄", "绿", "绿", "绿"]
    # 次日收盘执行：绿灯信号在第4日，执行在第5日。
    days = tuple(START + dt.timedelta(days=offset) for offset in range(7))
    executed = [row.executed for row in execute_asset(days, lights, (Decimal(100),) * 7, Decimal("0.5"))]  # type: ignore[arg-type]
    assert executed.index("绿", 1) == 5 and executed[4] == "黄"
    # 没有静默期：与 E2（退出后还要连续 5 天静默）不同，F 在通道退出当日即可降级。


def test_release_rule_f_does_not_downgrade_on_missing_input() -> None:
    # 第4日降级输入缺失（某指数无收盘价）：黄灯不转绿，数据状态记“沿用”；第5日恢复后才转绿。
    rows = _run_release_f(3, 7, invalid_inputs=frozenset({4}))
    assert [light for light, _ in rows] == ["红", "红", "红", "黄", "黄", "绿", "绿"]
    assert rows[4][1] == "沿用" and rows[3][1] == "完整"
    # 第3日降级输入缺失：红灯不转黄；第4日转黄，第5日转绿（仍是每天一级）。
    assert [light for light, _ in _run_release_f(3, 7, invalid_inputs=frozenset({3}))] == [
        "红", "红", "红", "红", "黄", "绿", "绿"]
    # 第3日红通道 PR 的退出输入缺失：通道无效且仍激活，红灯维持、状态“沿用”；第4日 PR 退出后转黄，第5日转绿。
    rows = _run_release_f(3, 7, missing_red=frozenset({3}))
    assert [light for light, _ in rows] == ["红", "红", "红", "红", "黄", "绿", "绿"] and rows[3][1] == "沿用"


def test_release_rule_f_levels_and_upgrades() -> None:
    day = START
    inputs = ReadyInputs(5, 5, None, None, None, None, None, True)
    quiet = lambda: ChannelDay(day, "armed", True, "")  # noqa: E731
    active = ChannelDay(day, "active", True, "")
    base = {"P_SPX": ("黄", quiet()), "PR_SPX": ("红", quiet()), "BW_SPX": ("红", quiet()), MR_CHANNEL: ("红", quiet())}
    step = lambda light, rows, k=3: step_system(day, SystemMemory(light, 0, 0, 0), rows, inputs, k, "F", 3, 5)  # noqa: E731
    # 升级规则不变：绿灯下 MR 激活直接转红（跨级）；只有黄通道激活则转黄。
    assert step("绿", {**base, MR_CHANNEL: ("红", active)}).memory.light == "红"
    assert step("绿", {**base, "P_SPX": ("黄", active)}).memory.light == "黄"
    # 红→黄只看红灯通道：黄通道 P 仍激活时，红灯通道全部未激活且 Q≥K 即可降到黄；但不会越级到绿。
    assert step("红", {**base, "P_SPX": ("黄", active)}).memory.light == "黄"
    assert step("红", base).memory.light == "黄"
    # 黄→绿要求全部通道当日有效且未激活：P 仍激活则维持黄；任一通道无效也维持黄。
    assert step("黄", {**base, "P_SPX": ("黄", active)}).memory.light == "黄"
    assert step("黄", {**base, "BW_SPX": ("红", ChannelDay(day, "armed", False, ""))}).memory.light == "黄"
    assert step("黄", base).memory.light == "绿"
    # Q<K（K=10 而 Q=5）时两种降级都不发生。
    assert step("黄", base, 10).memory.light == "黄" and step("红", base, 10).memory.light == "红"
    # 解除规则 F 的输入只需要两指数的 Q 与收盘价是否齐全。
    fixed = CONFIG.base.fixed_parameters()
    days = tuple(START + dt.timedelta(days=offset) for offset in range(2))
    full = asset_features(days, {item: Decimal(100) for item in days}, {}, Decimal("0.10"), fixed)
    partial = asset_features(days, {days[0]: Decimal(100)}, {}, Decimal("0.10"), fixed)
    assert [row.downgrade_inputs_valid for row in release_f_inputs(full, partial)] == [True, False]


# ---------------------------------------------------------------------------
# 转绿延迟、可行条件与三级选择
# ---------------------------------------------------------------------------

AXIS = tuple(stock_trading_days(dt.date(2020, 1, 2), dt.date(2020, 3, 31)))


def _cost(symbol: str, trough: int, rebound_class: str, green: int | None) -> ExitCostEvent:
    return ExitCostEvent(symbol, AXIS[trough - 1], AXIS[trough], "纳入", rebound_class, None, 0, None, None,  # type: ignore[arg-type]
                         Decimal("0.3") if green is not None else None, None,
                         AXIS[green] if green is not None else None, ())


def test_green_delay_counts_only_class_two_and_missing_sample_fails() -> None:
    costs = (_cost("SPX", 5, "②低点或之后转绿", 5), _cost("SPX", 10, "②低点或之后转绿", 16),
             _cost("SPX", 20, "②低点或之后转绿", 30), _cost("SPX", 35, "①低点前已绿", None),
             _cost("SPX", 40, "③下一事件前未转绿", None),
             ExitCostEvent("SPX", AXIS[1], AXIS[2], "状态窗口不足：t0 至 τ_E", None, None, 0, None, None, None, None,
                           None, ()))
    delay = green_delay("SPX", AXIS, costs)
    # 纳入 5 件（第6件早于 τ，不纳入）：①1、②3、③1。类别②的延迟 g−Tr 为 0、6、10 个交易日。
    assert (delay.included, delay.class_1, delay.class_2, delay.class_3) == (5, 1, 3, 1)
    # 中位数 6；P75 = 6+0.5×(10−6)=8；最大 10。类别①、③不进入延迟分布。
    assert (delay.delays.n, delay.delays.median, delay.delays.p75, delay.delays.maximum) == (3, 6, 8, 10)
    empty = green_delay("QQQ", AXIS, (_cost("QQQ", 35, "①低点前已绿", None),))
    assert (empty.class_2, empty.delays.median) == (0, None)
    # SPX 中位数 6≤8 满足；QQQ 类别②无样本 → 视为不满足；非绿占比恰为 0.60 满足。
    assert condition_flags(Decimal("0.60"), {"SPX": delay, "QQQ": empty}, LIMITS) == {
        NON_GREEN: True, DELAY_SPX: True, DELAY_QQQ: False}
    # 中位数恰为 8 满足，8.5 不满足：延迟 (8, 8) 与 (8, 9)。
    exact = green_delay("QQQ", AXIS, (_cost("QQQ", 5, "②低点或之后转绿", 13), _cost("QQQ", 20, "②低点或之后转绿", 28)))
    over = green_delay("QQQ", AXIS, (_cost("QQQ", 5, "②低点或之后转绿", 13), _cost("QQQ", 20, "②低点或之后转绿", 29)))
    assert condition_flags(Decimal("0.61"), {"SPX": delay, "QQQ": exact}, LIMITS) == {
        NON_GREEN: False, DELAY_SPX: True, DELAY_QQQ: True}
    assert over.delays.median == Decimal("8.5")
    assert condition_flags(Decimal("0.5"), {"SPX": delay, "QQQ": over}, LIMITS)[DELAY_QQQ] is False


def _item(order: int, score: str, non_green: bool, spx: bool, qqq: bool, days: int = 10,
          switches: int = 5) -> TierItem:
    return TierItem(f"设定{order}", Decimal(score), days, switches, order,
                    {NON_GREEN: non_green, DELAY_SPX: spx, DELAY_QQQ: qqq})


def test_three_tier_selection_each_tier_can_be_reached() -> None:
    # 第一级：设定2、3 三条全部满足；取 T 较小的设定3（设定1 的 T 更小但转绿不达标，不在第一级）。
    trace = select_by_tiers([_item(1, "-0.9", True, False, True), _item(2, "-0.1", True, True, True),
                             _item(3, "-0.2", True, True, True), _item(4, "-0.8", False, True, True)],
                            REGISTERED_TIERS)
    assert (trace.tier, trace.note, trace.selected.order, trace.sizes) == (1, "", 3, (2, 3, 4))
    # 第二级：没有三条全满足的；只满足非绿占比的有设定1、2 → 取 T 较小的设定1，标注“转绿要求未达标”。
    trace = select_by_tiers([_item(1, "-0.3", True, False, True), _item(2, "-0.1", True, True, False),
                             _item(3, "-0.9", False, True, True)], REGISTERED_TIERS)
    assert (trace.tier, trace.note, trace.selected.order, trace.sizes) == (2, "转绿要求未达标", 1, (0, 2, 3))
    # 第三级：没有一组满足非绿占比 → 在全部候选中取 T 最小的设定2，标注两项均未达标。
    trace = select_by_tiers([_item(1, "0.2", False, True, True), _item(2, "-0.4", False, False, False)],
                            REGISTERED_TIERS)
    assert (trace.tier, trace.note, trace.selected.order, trace.sizes) == (
        3, "亮灯上限与转绿要求均未达标", 2, (0, 0, 2))
    # 并列：T 相同依次比较执行非绿天数、计费切换次数、登记顺序。
    trace = select_by_tiers([_item(1, "0.1", True, True, True, days=10),
                             _item(2, "0.1", True, True, True, days=9, switches=9),
                             _item(3, "0.1", True, True, True, days=9, switches=8),
                             _item(4, "0.1", True, True, True, days=9, switches=8)], REGISTERED_TIERS)
    assert trace.selected.order == 3
    with pytest.raises(ValueError, match="没有候选"):
        select_by_tiers([], REGISTERED_TIERS)


# ---------------------------------------------------------------------------
# 主检验的日度差、补充历史窗口、配置与依赖方向
# ---------------------------------------------------------------------------

def test_timing_differences_for_v14_against_ma200() -> None:
    # 三个区间；始终绿逐日 g=(0.02, 0, 0.01)，始终红逐日 r=(0.01, 0.03, −0.01)，L_G=L_R=0.03。
    green = (Decimal("0.02"), Decimal("0"), Decimal("0.01"))
    red = (Decimal("0.01"), Decimal("0.03"), Decimal("-0.01"))
    v14 = (Decimal("0.010"), Decimal("0.015"), Decimal("0.003"))          # ē_V4=0.5
    average = (Decimal("0.018"), Decimal("0.004"), Decimal("0.009"))      # ē_MA=0.9
    d = timing_differences(v14, average, green, red, Decimal("0.5"), Decimal("0.9"))
    # v1.4 侧：0.010−0.5×0.02−0.5×0.01=−0.005；0.015−0−0.5×0.03=0；0.003−0.5×0.01+0.5×0.01=0.003。
    # 均线侧：0.018−0.9×0.02−0.1×0.01=−0.001；0.004−0−0.1×0.03=0.001；0.009−0.9×0.01+0.1×0.01=0.001。
    assert d == (Decimal("-0.004"), Decimal("-0.001"), Decimal("0.002"))
    # Σd = T_V4 − T_MA：T_V4=0.028−0.03=−0.002；T_MA=0.031−0.03=0.001 → −0.003。
    assert sum(d, Decimal(0)) == Decimal("-0.003")


def test_price_only_window_boundaries() -> None:
    days = tuple(stock_trading_days(dt.date(2000, 1, 3), dt.date(2001, 12, 31)))
    spx = {day: Decimal(100) for day in days}
    qqq = {day: Decimal(50) for day in days[150:]}                       # 第二个资产自下标150才有价格
    inputs = DevelopmentInputs(days, {"SPX": spx, "QQQ": qqq})
    config = dataclasses.replace(CONFIG, history_start=days[10], history_end=days[350])
    prepared = prepare_price_window(config, truncate_inputs(inputs, config.history_end))
    # t0′：两资产 P0 所需输入首次全部具备——QQQ 的 63 日高点在下标 150+62=212 首次可用；
    # SPX（含 MR 的 200 日均线，下标199 起可用）更早。窗口起点下标10 更早，不起约束。
    assert prepared.t0 == days[212]
    # τ′ = max(t0′+63, 九组收敛日)：价格不动、没有通道激活，三种初始状态很快一致，故 τ′=下标 275；j₀′ 同日。
    assert max(item.convergence_date for item in prepared.states) < days[275]
    assert (prepared.tau, prepared.first_loss_day) == (days[275], days[275])
    # 状态与执行只算到窗口末日；九组设定，全是纯价格版与解除规则 F。
    assert prepared.inputs.days[-1] == days[350] and len(prepared.states) == 9
    assert all(item.rows[-1].date == days[350] and item.candidate.model == PRICE_ONLY for item in prepared.states)
    assert {row.light for item in prepared.states for row in item.rows} == {"绿"}
    # 窗口起点晚于输入具备日时，t0′ 取窗口起点。
    later = dataclasses.replace(config, history_start=days[230])
    assert prepare_price_window(later, truncate_inputs(inputs, config.history_end)).t0 == days[230]


def test_v14_config_matches_registration_and_rejects_changes() -> None:
    assert (CONFIG.k, CONFIG.theta_p, CONFIG.mr_window) == (
        (3, 5, 10), (Decimal("0.015"), Decimal("0.02"), Decimal("0.025")), 200)
    assert CONFIG.limits == LIMITS and CONFIG.tiers == REGISTERED_TIERS
    assert (CONFIG.median.k, CONFIG.median.theta_p, CONFIG.median.q) == (5, Decimal("0.02"), Decimal("0.10"))
    assert (CONFIG.history_start, CONFIG.history_end) == (dt.date(1999, 3, 10), dt.date(2009, 9, 30))
    raw = yaml.safe_load((ROOT / "config/wavewarn_v14.yaml").read_text(encoding="utf-8"))
    # 转绿延迟上限改成 10、候选多加一个 K、熊市区间改动，都必须被拒绝。
    with pytest.raises(ValueError, match="上限"):
        parse_v14_config({**raw, "feasibility": {**raw["feasibility"], "green_delay_median_max": 10}}, CONFIG.base)
    with pytest.raises(ValueError, match="候选"):
        parse_v14_config({**raw, "candidates": {**raw["candidates"], "k": [3, 5, 10, 20]}}, CONFIG.base)
    with pytest.raises(ValueError, match="终止规则"):
        parse_v14_config({**raw, "selection_tiers": raw["selection_tiers"][:2]}, CONFIG.base)


def test_v14_algorithm_modules_do_not_import_io_modules() -> None:
    """依赖方向：v1.4 的纯计算模块不得导入读写模块、services 或 CLI（含传递依赖）。"""
    code = ("import importlib, json, sys\n"
            "for name in ('v14_model', 'feasibility_v14', 'evaluation_v14', 'evaluation_v14_tables',"
            " 'evaluation_v14_report', 'extended_history_v14'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.evaluation_v13_run',"
            " 'market_risk.wavewarn.evaluation_v14_run', 'market_risk.wavewarn.extended_history_run',"
            " 'market_risk.wavewarn.extended_history_v14_run', 'market_risk.wavewarn.calibration_run',"
            " 'market_risk.wavewarn.diagnostics', 'market_risk.services', 'market_risk.cli')"
            " or m.startswith('market_risk.scoring')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
