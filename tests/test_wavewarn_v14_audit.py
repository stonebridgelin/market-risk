"""v1.4 一致性审计的区分性案例（docs/research/v1.4_一致性审计.md）。

每个案例都从规格原文推出期望值（推算过程写在说明里），并且选在“正确写法”与“最可能的错误写法”给出不同结果的
位置：连续条件的括号范围、等号、缺值、同日冲突与交易日索引。全部是构造数据，不读取任何行情。
对现行代码不成立的案例标记为预期失败并注明对应的偏差；偏差修正后该标记必须去掉。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.wavewarn.calibration import distribution
from market_risk.wavewarn.channels import (
    ChannelDay,
    breadth_predicates,
    price_predicates,
    run_channel,
    update_channel,
    volatility_predicates,
)
from market_risk.wavewarn.config_v14 import NON_GREEN, REGISTERED_TIERS, V14Limits, load_v14_config
from market_risk.wavewarn.convergence import loss_start
from market_risk.wavewarn.evaluation import first_loss_interval
from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.feasibility_v14 import GreenDelay, TierItem, condition_flags, select_by_tiers
from market_risk.wavewarn.features import (
    AssetFeatures,
    asset_features,
    moving_average,
    new_low20,
    q_from_new_lows,
    rolling_high,
)
from market_risk.wavewarn.labels_zz import ZZEvent, dangerous_interval, find_zz_events
from market_risk.wavewarn.loss import LossParameters, asset_price_loss, daily_main_loss
from market_risk.wavewarn.state_machine import ReadyInputs, SystemMemory, release_f_step
from market_risk.wavewarn.timing import ma200_signals
from market_risk.wavewarn.v14_model import trend_predicates

ROOT = Path(__file__).resolve().parents[1]
FIXED = load_v14_config(ROOT / "config/wavewarn_v14.yaml").base.fixed_parameters()
D = Decimal
DAYS = tuple(dt.date(2012, 1, 2) + dt.timedelta(days=index) for index in range(260))
PARAMS = LossParameters(D(2), D(1), D("0.5"), D("0.02"), D("0.0025"))


def _feature(index: int, **changes: object) -> AssetFeatures:
    """中性的一日特征：没有回撤、没有新低、广度 50、收盘价等于各均线。"""
    base = AssetFeatures(DAYS[index], D(100), D(100), D(0), 0, 99, D(50), D(0), D(0), D(-100), D(50), D(100),
                         D(100), D(100), D(100))
    return dataclasses.replace(base, **changes)


def _closes(values: tuple[str | None, ...]) -> tuple[Decimal | None, ...]:
    return tuple(D(value) if value is not None else None for value in values)


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------

def _bw(breadth: tuple[str | None, ...], price_up: tuple[bool, ...]) -> list[bool | None]:
    features = [_feature(index, breadth=D(value) if value is not None else None,
                         close=D(101) if up else D(99), close_t5=D(100))
                for index, (value, up) in enumerate(zip(breadth, price_up, strict=True))]
    return [item.exit for item in breadth_predicates(features, "BW", FIXED)]


@pytest.mark.xfail(strict=True, reason="审计 (b) 类偏差 B-1：现行代码把 BW 退出的价格条件也放进了“连续 3 日”")
def test_bw_exit_needs_three_breadth_days_but_price_only_today() -> None:
    """规格：“W 连续 3 个有效日 > 40，且 C_t > C_{t−5}”——连续 3 日只管广度，价格条件只看当日。

    近 3 日广度都是 41，价格条件依次为假、真、真：第 3 日广度已连续 3 日 > 40、当日价格条件为真 → 退出。
    错误写法（把价格条件也放进连续 3 日的括号里）在第 3 日不退出。
    """
    assert _bw(("41", "41", "41"), (False, True, True)) == [False, False, True]


def test_bw_exit_breadth_streak_and_price_boundaries() -> None:
    """价格条件三日都为真时：广度 41、40、41 不退出（40 不“大于”40，连续被打断）；41、41、41 退出。

    广度三日都 > 40 但当日价格条件为假 → 不退出。两日前广度缺失 → 连续有效日清零，不退出（是“否”，不是缺值）。
    """
    assert _bw(("41", "40", "41"), (True, True, True))[2] is False
    assert _bw(("41", "41", "41"), (True, True, True))[2] is True
    assert _bw(("41", "41", "41"), (True, True, False))[2] is False
    assert _bw((None, "41", "41"), (True, True, True))[2] is False
    # 当日广度缺失：退出谓词为缺值，通道当日无效、状态沿用。
    exits = _bw(("41", "41", None), (True, True, True))
    assert exits[2] is None
    held = update_channel(DAYS[2], "active", breadth_predicates([_feature(2, breadth=None)], "BW", FIXED)[0])
    assert (held.status, held.valid) == ("active", False)


def test_bw_entry_boundaries() -> None:
    """进入：D ≥ 2% 且 W ≤ 30，两个等号都成立。D = 1.99% 或 W = 30.01 不进入；任一输入缺失为缺值。"""
    def entry(drawdown: str | None, breadth: str | None) -> bool | None:
        feature = _feature(0, drawdown63=D(drawdown) if drawdown else None,
                           breadth=D(breadth) if breadth else None)
        return breadth_predicates([feature], "BW", FIXED)[0].entry

    assert entry("0.02", "30") is True
    assert entry("0.0199", "30") is False and entry("0.02", "30.01") is False
    assert entry(None, "30") is None and entry("0.02", None) is None


def test_price_channel_thresholds_exit_and_rearm_sequence() -> None:
    """θ_P = 2%、K = 5。P 在 D = 2% 进入（等号成立）；PR 的门槛是 4%，D = 3.99% 不进入。退出只看 Q ≥ K，不含 D。

    序列（P 通道，自“已武装”起）：
    第 0 日 D=3% → 进入；第 1 日 Q=4 → 维持；第 2 日 Q=5、D 仍为 3% → 退出，且当日不得重新进入；
    第 3 日 D=3%、NL=0 → 进入谓词仍为真，不能重新武装（错误写法会在这里重新进入）；
    第 4 日 D=1% → 进入谓词为假，重新武装，但当日不进入；第 5 日 D=3% → 进入。
    """
    def row(index: int, drawdown: str, q: int, new_low: int = 0) -> AssetFeatures:
        return _feature(index, drawdown63=D(drawdown), q=q, new_low20=new_low)

    theta = D("0.02")
    assert price_predicates([row(0, "0.02", 0)], theta, 5)[0].entry is True
    assert price_predicates([row(0, "0.0199", 0)], theta, 5)[0].entry is False
    assert price_predicates([row(0, "0.0399", 0)], theta, 5, red=True)[0].entry is False
    assert price_predicates([row(0, "0.04", 0)], theta, 5, red=True)[0].entry is True
    rows = [row(0, "0.03", 1), row(1, "0.03", 4), row(2, "0.03", 5), row(3, "0.03", 6), row(4, "0.01", 7),
            row(5, "0.03", 8)]
    states = run_channel(DAYS[:6], price_predicates(rows, theta, 5))
    assert [item.status for item in states] == ["active", "active", "unarmed", "unarmed", "armed", "active"]
    # 当日创 20 日新低：未武装的通道立即武装并可同日进入。
    renewed = update_channel(DAYS[3], "unarmed", price_predicates([row(3, "0.03", 0, 1)], theta, 5)[0])
    assert renewed.status == "active"


def test_v_entry_and_exit_boundaries() -> None:
    """V：R > 1 进入（R = 1 不进入）。退出：R 连续 3 个有效日 < 0.95（0.95 不算），或连续 10 个有效日 ≤ 1（1 算）。

    缺值打断连续计数，缺值当日通道无效。
    """
    def exits(values: tuple[str | None, ...]) -> list[bool | None]:
        return [item.exit for item in volatility_predicates(_closes(values), FIXED)]

    entries = [item.entry for item in volatility_predicates(_closes(("1", "1.0001", None)), FIXED)]
    assert entries == [False, True, None]
    assert exits(("0.94", "0.94", "0.94"))[2] is True
    assert exits(("0.94", "0.94", "0.95"))[2] is False
    assert exits(("1",) * 10)[9] is True and exits(("1",) * 10)[8] is False
    assert exits(("1",) * 9 + ("1.0001",))[9] is False
    broken = exits(("0.94", None, "0.94", "0.94"))
    assert broken[1] is None and broken[3] is False
    assert exits(("0.94", None, "0.94", "0.94", "0.94"))[4] is True


def test_mr_is_active_exactly_when_close_is_below_average() -> None:
    """3 日均线。收盘 100、100、100、97、99.5、90：

    行号 2：均线 100，收盘等于均线 → 不低于 → 未激活；行号 3：均线 99，97 低于 → 激活；
    行号 4：均线 98.83，99.5 不低于 → 退出；行号 5：均线 95.5，90 低于 → 当日重新武装并激活（没有滞后区）。
    与 200 日均线参照行的信号逐日对应：激活 ⇔ 红。
    """
    closes = _closes(("100", "100", "100", "97", "99.5", "90"))
    states = run_channel(DAYS[2:6], trend_predicates(closes, 3)[2:])
    assert [item.status == "active" for item in states] == [False, True, False, True]
    signals = ma200_signals(DAYS[:6], dict(zip(DAYS[:6], closes, strict=True)), DAYS[2], 3)
    assert signals == ("绿", "红", "绿", "红")
    missing = trend_predicates(_closes(("100", "100", None)), 3)[2]
    assert (missing.entry, missing.exit) == (None, None)


# ---------------------------------------------------------------------------
# 基础特征
# ---------------------------------------------------------------------------

def test_new_low_is_strict_and_three_valued() -> None:
    """前 19 个收盘价都是 10：今日 10 不是新低（严格低于才算）；9.99 是新低。

    窗口缺一个价：今日 9.99 低于有效最小值 → 未知；今日 10 → 可以证明未创新低 → 0。
    有效价只有 14 个：即使今日高于最小值也判未知（不足 15 个优先）。今日缺价 → 未知。
    """
    window = [D(10)] * 19
    assert new_low20([*window, D(10)], 19, 19, 15) == 0
    assert new_low20([*window, D("9.99")], 19, 19, 15) == 1
    gap = [D(10)] * 9 + [None] + [D(10)] * 9
    assert new_low20([*gap, D("9.99")], 19, 19, 15) is None
    assert new_low20([*gap, D(10)], 19, 19, 15) == 0
    thin = [D(10)] * 14 + [None] * 5
    assert new_low20([*thin, D(11)], 19, 19, 15) is None
    assert new_low20([*window, None], 19, 19, 15) is None


def test_q_counts_days_since_last_new_low_or_unknown() -> None:
    """NL 序列 0、0、1、0、0、未知、0 → Q 为 1、2、0、1、2、0、1。"""
    assert q_from_new_lows((0, 0, 1, 0, 0, None, 0)) == (1, 2, 0, 1, 2, 0, 1)


def test_rolling_windows_include_today_and_have_exact_length() -> None:
    """收盘价为 100+行号，行号 8 处是 1000。

    63 日高点取 C_{t−62} 至 C_t：行号 70 的窗口从行号 8 起，高点 1000；行号 71 的窗口从行号 9 起，高点就是当日收盘。
    行号 61 只有 62 个收盘价 → 高点缺失；行号 62 恰好 63 个。
    200 日均线取 C_{t−199} 至 C_t：收盘价等于行号时，行号 199 为 99.5，行号 200 为 100.5，行号 198 缺失。
    C_{t−5}、C_{t−10} 是 5、10 个交易日之前的收盘价。
    """
    closes = [D(100 + index) for index in range(80)]
    closes[8] = D(1000)
    assert rolling_high(closes, 70, 63) == D(1000) and rolling_high(closes, 71, 63) == D(171)
    assert rolling_high(closes, 61, 63) is None and rolling_high(closes, 62, 63) == D(1000)
    series = [D(index) for index in range(201)]
    assert moving_average(series, 198, 200) is None
    assert (moving_average(series, 199, 200), moving_average(series, 200, 200)) == (D("99.5"), D("100.5"))
    prices = {DAYS[index]: D(100 + index) for index in range(80)}
    features = asset_features(DAYS[:80], prices, {}, D("0.1"), FIXED)
    assert (features[70].close_t5, features[70].close_t10) == (D(165), D(160))
    assert features[70].drawdown63 == D(0) and features[4].close_t5 is None


# ---------------------------------------------------------------------------
# 解除规则 F
# ---------------------------------------------------------------------------

def _rows(**channels: tuple[str, str, bool]) -> dict[str, tuple[str, ChannelDay]]:
    return {name: (level, ChannelDay(DAYS[0], status, valid, ""))  # type: ignore[arg-type]
            for name, (level, status, valid) in channels.items()}


def _inputs(q_spx: int, q_qqq: int, valid: bool = True) -> ReadyInputs:
    return ReadyInputs(q_spx, q_qqq, None, None, None, None, None, valid)


def _step(previous: str, inputs: ReadyInputs, **channels: tuple[str, str, bool]) -> tuple[str, str]:
    result = release_f_step(DAYS[0], SystemMemory(previous, 0, 0, 0), _rows(**channels), inputs, 5)  # type: ignore[arg-type]
    return result.memory.light, result.data_status


def test_release_f_red_to_yellow_looks_only_at_red_channels_and_both_q() -> None:
    """红→黄：全部红灯通道当日有效且未激活，且两指数 Q ≥ K（K=5，等号成立）。

    黄灯通道 P 仍在激活不妨碍红转黄（错误写法：要求全部通道）；Q_QQQ = 4 不降级；
    静默计数为 0（昨天红灯通道还在激活）也不妨碍——F 不叠加静默期（错误写法：沿用 quietRed ≥ 3）。
    """
    quiet = {"PR_SPX": ("红", "unarmed", True), "MR_SPX": ("红", "armed", True)}
    assert _step("红", _inputs(5, 5), P_SPX=("黄", "active", True), **quiet)[0] == "黄"
    assert _step("红", _inputs(5, 4), P_SPX=("黄", "armed", True), **quiet)[0] == "红"
    assert _step("红", _inputs(5, 5), P_SPX=("黄", "armed", True), PR_SPX=("红", "active", True))[0] == "红"
    # 红灯通道当日无效（未激活）：不降级，数据状态记“沿用”。
    assert _step("红", _inputs(5, 5), PR_SPX=("红", "armed", False)) == ("红", "沿用")
    # 降级所需输入缺失（两指数之一缺价）：不降级。
    assert _step("红", _inputs(5, 5, False), **quiet) == ("红", "沿用")


def test_release_f_drops_one_level_per_day_and_yellow_needs_every_channel_clear() -> None:
    """红灯当日全部条件都满足也只能到黄，次日才到绿；黄→绿要求全部通道（含黄灯通道）当日有效且未激活。"""
    clear = {"P_SPX": ("黄", "armed", True), "PR_SPX": ("红", "armed", True)}
    assert _step("红", _inputs(9, 9), **clear)[0] == "黄"
    assert _step("黄", _inputs(9, 9), **clear) == ("绿", "完整")
    assert _step("黄", _inputs(9, 9), P_SPX=("黄", "armed", False), PR_SPX=("红", "armed", True)) == ("黄", "沿用")
    assert _step("黄", _inputs(9, 4), **clear)[0] == "黄"
    # 升级可以跨级：绿灯当日有红灯通道激活直接到红；黄灯同理。黄灯通道激活只到黄。
    assert _step("绿", _inputs(0, 0), P_SPX=("黄", "active", True), PR_SPX=("红", "active", True))[0] == "红"
    assert _step("黄", _inputs(9, 9), P_SPX=("黄", "armed", True), PR_SPX=("红", "active", True))[0] == "红"
    assert _step("绿", _inputs(9, 9), P_SPX=("黄", "active", True), PR_SPX=("红", "armed", True))[0] == "黄"


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def test_signal_is_executed_at_next_close_and_missing_price_holds_exposure() -> None:
    """e_j = x(S_{j−1})：信号 绿、红、黄、黄 → 执行 绿（初始）、绿、红、黄；切换归属执行当日（第 2、3 日）。

    第 2 日缺价：该资产无法执行，实际暴露保持 1，但系统切换照计；第 3 日执行最新信号（黄），红灯不排队补执行。
    """
    signals = ("绿", "红", "黄", "黄")
    full = execute_asset(DAYS[:4], signals, _closes(("100", "100", "100", "100")), D("0.5"))  # type: ignore[arg-type]
    assert [row.executed for row in full] == ["绿", "绿", "红", "黄"]
    assert [row.switched for row in full] == [False, False, True, True]
    assert [row.exposure for row in full] == [D(1), D(1), D(0), D("0.5")]
    gap = execute_asset(DAYS[:4], signals, _closes(("100", "100", None, "100")), D("0.5"))  # type: ignore[arg-type]
    assert [row.exposure for row in gap] == [D(1), D(1), D(1), D("0.5")]
    assert [row.switched for row in gap] == [False, False, True, True]


# ---------------------------------------------------------------------------
# ZZ 标签
# ---------------------------------------------------------------------------

def test_zz_thresholds_trigger_on_equality_and_ties_keep_the_earliest_day() -> None:
    """SPX 下跌门槛 4%、反弹门槛 5%。收盘 100、100、96.01、96、95、95、99.74、99.75：

    高点并列取最早（行号 0）；96.01 > 96 不触发，96 = 100×0.96 恰好触发 → T0 为行号 3；
    低点 95 并列取最早（行号 4）；99.74 < 95×1.05 = 99.75 不结束，99.75 恰好结束（行号 7）。
    危险区间为 [P, Tr)：行号 0—3 危险，行号 4（低点当日起的区间）不危险。
    """
    closes = dict(zip(DAYS[:8], _closes(("100", "100", "96.01", "96", "95", "95", "99.74", "99.75")), strict=True))
    (event,) = find_zz_events("SPX", DAYS[:8], closes, DAYS[7])
    assert (event.peak_date, event.t0_date, event.trough_date, event.end_date) == (
        DAYS[0], DAYS[3], DAYS[4], DAYS[7])
    assert not event.right_censored and dangerous_interval(event, DAYS[:8]) == DAYS[:4]
    # 期末仍在寻底：右截尾，低点为暂定。
    (open_event,) = find_zz_events("SPX", DAYS[:7], closes, DAYS[6])
    assert open_event.right_censored and open_event.end_date is None


# ---------------------------------------------------------------------------
# 主损失
# ---------------------------------------------------------------------------

def _full(count: int, level: str = "1") -> tuple:
    light = {"1": "绿", "0.5": "黄", "0": "红"}[level]
    return execute_asset(DAYS[:count], (light,) * count, (D(100),) * count, D("0.5"), light)  # type: ignore[arg-type]


def test_drawdown_record_is_not_charged_twice_and_two_percent_is_free() -> None:
    """满暴露、没有事件。收盘 100、98、97、99、96（H = 100，噪声下限 2%）：

    区间 0：到 98，恰好 2% → X = 0。区间 1：到 97，X = ln(98/97)，增量即此值，收费 κ_0×1×ln(98/97)。
    区间 2：到 99，X = 0，纪录不变。区间 3：到 96，X = ln(98/96)，增量 = ln(98/96) − ln(98/97) = ln(97/96)。
    回撤增量合计 ln(98/96)，同一段回撤不重复收费；κ_0 = β/2 = 0.5。
    """
    rows = asset_price_loss(DAYS[:5], _closes(("100", "98", "97", "99", "96")), _full(5), (), PARAMS)
    expected = [D(0), (D(98) / D(97)).ln(), D(0), (D(97) / D(96)).ln()]
    assert all(abs(row.drawdown_increment - value) < D("1e-24") for row, value in zip(rows, expected, strict=True))
    charged = [row.drawdown_loss for row in rows]
    assert all(abs(loss - D("0.5") * value) < D("1e-24") for loss, value in zip(charged, expected, strict=True))


def test_danger_interval_updates_record_without_charging_and_end_day_resets_high() -> None:
    """事件 P=行号 0、Tr=行号 1、结束日=行号 2。收盘 100、99、97、94，满暴露。

    区间 0（危险）：收危险项 κ_D×1×(−ln 0.99)，不收回撤项。
    区间 1（99→97，非危险）：H = 100，X = ln(98/97)，收回撤项。
    区间 2：行号 2 是结束日，先把 H 重置为 97、纪录清零，再算 X = ln(0.98×97/94) = ln(95.06/94)。
    错误写法（不在结束日重置）会用 H = 100 得到 ln(98/94) − ln(98/97)。
    """
    event = ZZEvent("SPX", DAYS[0], DAYS[1], DAYS[1], DAYS[2], D(100), D(99), False)
    rows = asset_price_loss(DAYS[:4], _closes(("100", "99", "97", "94")), _full(4), (event,), PARAMS)
    assert rows[0].dangerous and abs(rows[0].danger_loss - 2 * -(D(99) / D(100)).ln()) < D("1e-24")
    assert rows[0].drawdown_loss == 0 and rows[0].opportunity_loss == 0
    assert abs(rows[1].drawdown_increment - (D(98) / D(97)).ln()) < D("1e-24")
    assert abs(rows[2].drawdown_increment - (D("95.06") / D(94)).ln()) < D("1e-24")


def test_record_advances_inside_danger_and_missed_rebound_is_free() -> None:
    """事件 P=行号 0、Tr=行号 2（结束日在窗口之后）。收盘 100、98、97、96.5、98，暴露恒为 0.5。

    区间 0、1 危险：只按下跌收危险项 κ_D×0.5×(−r)，回撤纪录照常推进到 ln(98/97) 但不收费。
    区间 2（非危险）：X = ln(98/96.5)，增量只有 ln(97/96.5)（错误写法：危险区间内不推进纪录，会收 ln(98/96.5)）。
    区间 3 上涨：机会项 β×(1−0.5)×ln(98/96.5) 为正。
    另：危险区间内上涨不收任何费用——空仓者错过的反弹不计。
    """
    event = ZZEvent("SPX", DAYS[0], DAYS[1], DAYS[2], DAYS[200], D(100), D(97), False)
    closes = _closes(("100", "98", "97", "96.5", "98"))
    rows = asset_price_loss(DAYS[:5], closes, _full(5, "0.5"), (event,), PARAMS)
    assert abs(rows[0].danger_loss - 2 * D("0.5") * -(D(98) / D(100)).ln()) < D("1e-24")
    assert rows[0].drawdown_loss == 0 and rows[1].drawdown_loss == 0 and rows[1].opportunity_loss == 0
    assert abs(rows[2].drawdown_increment - (D(97) / D("96.5")).ln()) < D("1e-24")
    assert abs(rows[3].opportunity_loss - D("0.5") * (D(98) / D("96.5")).ln()) < D("1e-24")
    rebound = ZZEvent("SPX", DAYS[0], DAYS[1], DAYS[3], DAYS[200], D(100), D(95), False)
    flat = asset_price_loss(DAYS[:3], _closes(("100", "97", "99")), _full(3, "0"), (rebound,), PARAMS)
    assert flat[1].dangerous and flat[1].price_loss == 0
    # 危险区间外空仓遇到下跌：机会项为负（净机会成本可以为负）。
    outside = asset_price_loss(DAYS[:2], _closes(("100", "99")), _full(2, "0"), (), PARAMS)
    assert abs(outside[0].opportunity_loss - (D(99) / D(100)).ln()) < D("1e-24") and outside[0].opportunity_loss < 0


def test_switches_are_billed_from_the_first_window_day_but_not_on_the_last() -> None:
    """信号 红、绿、红、红，自前一日连续执行：执行灯色 绿（初始）、红、绿、红。评价窗口取后三天。

    窗口第一天（执行红，前一日为绿）是一次切换，计费——状态机连续运行，这不是初始建仓；
    第二天（红→绿）计费；第三天是窗口末日，其后没有区间，切换另标、不计费。共 2γ = 0.01。
    """
    closes = (D(100),) * 4
    executions = execute_asset(DAYS[:4], ("红", "绿", "红", "红"), closes, D("0.5"))[1:]  # type: ignore[arg-type]
    assert [row.executed for row in executions] == ["红", "绿", "红"]
    assets = {symbol: asset_price_loss(DAYS[1:4], closes[1:], executions, (), PARAMS) for symbol in ("SPX", "QQQ")}
    daily = daily_main_loss(DAYS[1:4], assets, executions, {"SPX": D("0.5"), "QQQ": D("0.5")}, PARAMS)
    assert [row.switch_cost for row in daily] == [D("0.005"), D("0.005"), D(0)]
    assert [row.terminal_switch_unbilled for row in daily] == [False, False, True]
    # 系统切换只计一次，与资产个数无关：窗口内合计 2γ。
    assert sum(row.total for row in daily) == D("0.01")


# ---------------------------------------------------------------------------
# 收敛、评价窗口与选参
# ---------------------------------------------------------------------------

def test_loss_start_and_first_interval_rules() -> None:
    """τ = max(t0 + 63 个交易日, 全部系统收敛日)；j₀ = max(τ, 最晚收敛日之后第 2 个交易日)。

    t0 为行号 10：收敛日在行号 20 时 τ = 行号 73、j₀ = 行号 73；收敛日在行号 80 时 τ = 行号 80、j₀ = 行号 82
    （第 82 天收盘执行第 81 天的信号，切换与第 80 天的信号比较，三者都已与初始状态无关）。
    """
    assert loss_start(DAYS, DAYS[10], [DAYS[20], DAYS[15]], FIXED) == DAYS[73]
    assert first_loss_interval(DAYS, DAYS[73], [DAYS[20], DAYS[15]]) == DAYS[73]
    assert loss_start(DAYS, DAYS[10], [DAYS[20], DAYS[80]], FIXED) == DAYS[80]
    assert first_loss_interval(DAYS, DAYS[80], [DAYS[20], DAYS[80]]) == DAYS[82]


def _delay(median: str | None) -> GreenDelay:
    values = [] if median is None else [D(median)]
    return GreenDelay("SPX", len(values), 0, len(values), 0, distribution(values))


def test_feasibility_boundaries_and_tie_break_order() -> None:
    """可行条件的等号：非绿占比恰为 0.60 满足；转绿延迟中位数恰为 8 满足，8.5 不满足，无样本不满足。

    选择：先比 T，再比执行非绿天数、计费切换次数、登记顺序。
    """
    limits = V14Limits(D("0.60"), D(8))
    ok = condition_flags(D("0.60"), {"SPX": _delay("8"), "QQQ": _delay("8")}, limits)
    assert all(ok.values())
    late = condition_flags(D("0.6001"), {"SPX": _delay("8.5"), "QQQ": _delay(None)}, limits)
    assert not any(late.values())
    flags = dict.fromkeys(ok, True)
    items = [TierItem("a", D("-0.2"), 800, 190, 3, flags), TierItem("b", D("-0.2"), 790, 200, 4, flags),
             TierItem("c", D("-0.2"), 790, 195, 5, flags), TierItem("d", D("-0.2"), 790, 195, 2, flags),
             TierItem("e", D("-0.1"), 100, 10, 0, flags)]
    assert select_by_tiers(items, REGISTERED_TIERS).selected.key == "d"
    # T 更小但不满足亮灯上限的设定不进入第 1、2 级；全部不满足时才进入第 3 级。
    blocked = TierItem("f", D("-0.9"), 1, 1, 9, {**flags, NON_GREEN: False})
    assert select_by_tiers([*items, blocked], REGISTERED_TIERS).selected.key == "d"
