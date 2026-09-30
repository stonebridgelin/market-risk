"""v1.2.1 损失各项以手算价格路径核对。"""

import datetime as dt
from dataclasses import replace
from decimal import Decimal

from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.labels_zz import ZZEvent
from market_risk.wavewarn.loss import (
    LossParameters,
    asset_price_loss,
    daily_main_loss,
    full_exposure_events,
    noise_drawdown,
    switch_loss,
    weighted_price_loss,
)


def _days(count: int) -> list[dt.date]:
    return [dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(count)]


def test_noise_floor_exact_two_percent_boundary() -> None:
    # 98/100=0.98 精确触边，不计回撤；97.9999/100<0.98 才计。
    assert noise_drawdown(Decimal("100"), Decimal("98.0001")) == 0
    assert noise_drawdown(Decimal("100"), Decimal("98.0000")) == 0
    assert noise_drawdown(Decimal("100"), Decimal("97.9999")) > 0


def test_asset_loss_danger_and_excluded_other_asset_are_separate() -> None:
    days = _days(3)
    params = LossParameters(Decimal(2), Decimal(1), Decimal("0.5"))
    event = ZZEvent("SPX", days[0], days[1], days[1], days[2], Decimal(100), Decimal(99), False)
    spx = [Decimal(100), Decimal(99), Decimal(100)]
    qqq = [Decimal(100), None, Decimal(99)]
    spx_execution = execute_asset(days, ["绿", "绿", "绿"], spx, params.eta)
    qqq_execution = execute_asset(days, ["绿", "绿", "绿"], qqq, params.eta)
    spx_loss = asset_price_loss(days, spx, spx_execution, [event], params)
    qqq_loss = asset_price_loss(days, qqq, qqq_execution, [], params)
    # SPX [高点,低点) 的下跌项=2×ln(100/99)，QQQ 跨缺价两段均计零。
    assert abs(spx_loss[0].danger_loss - 2 * (Decimal(100) / Decimal(99)).ln()) < Decimal("1e-25")
    assert all(row.price_loss == 0 and row.excluded_reason == "跨缺价区间" for row in qqq_loss)
    assert weighted_price_loss({"SPX": spx_loss, "QQQ": qqq_loss},
                               {"SPX": Decimal("0.5"), "QQQ": Decimal("0.5")}) >= spx_loss[0].danger_loss / 2


def test_missing_price_recovery_does_not_charge_gap_drawdown_again() -> None:
    days = _days(4)
    closes = [Decimal(100), None, Decimal(90), Decimal(89)]
    params = LossParameters(Decimal(2), Decimal(1), Decimal("0.5"))
    executions = execute_asset(days, ["绿"] * 4, closes, params.eta)
    rows = asset_price_loss(days, closes, executions, [], params)
    # 缺口形成的 ln(98/90) 只衔接纪录；恢复后的新高水位是 ln(98/89)，增量=ln(90/89)。
    assert rows[0].price_loss == rows[1].price_loss == 0
    assert abs(rows[2].drawdown_increment - (Decimal(90) / Decimal(89)).ln()) < Decimal("1e-25")
    assert abs(rows[2].drawdown_loss - (Decimal("0.5") * (Decimal(90) / Decimal(89)).ln())) < Decimal("1e-25")


def test_switch_count_and_full_exposure_include_missing_price_intervals() -> None:
    days = _days(4)
    closes = [Decimal(100), None, Decimal(90), Decimal(91)]
    params = LossParameters(Decimal(2), Decimal(1), Decimal("0.5"))
    event = ZZEvent("SPX", days[0], days[1], days[2], days[3], Decimal(100), Decimal(90), False)
    green = execute_asset(days, ["绿"] * 4, closes, params.eta)
    # [高点,低点) 包含两段跨缺价区间，但系统实际暴露一直为1，仍触发满暴露罚项。
    assert full_exposure_events([event], days, green) == (event,)
    switched = execute_asset(days, ["绿", "红", "黄", "黄"], closes, params.eta)
    # 绿→红、红→黄各一次；即使执行日缺价，系统切换仍单独计费。
    assert switch_loss(switched, params.gamma) == 2 * params.gamma


def test_w_a_excluded_asset_still_charges_switch_and_matches_signed_difference() -> None:
    days = _days(3)
    params = LossParameters(Decimal(2), Decimal(1), Decimal("0.5"))
    event = ZZEvent("SPX", days[0], days[1], days[1], days[2], Decimal(100), Decimal(99), False)
    spx = [Decimal(100), Decimal(99), Decimal(100)]
    qqq = [Decimal(100), None, Decimal(100)]
    n_spx = execute_asset(days, ["红"] * 3, spx, params.eta, initial_executed="红")
    n_qqq = execute_asset(days, ["红"] * 3, qqq, params.eta, initial_executed="红")
    p_spx = execute_asset(days, ["绿"] * 3, spx, params.eta)
    p_qqq = execute_asset(days, ["绿"] * 3, qqq, params.eta)
    # W-A 的第 j 天 N 由绿执行到红，P1 不切换；把执行事实显式放入构造日。
    n_spx = (replace(n_spx[0], switched=True), *n_spx[1:])
    n_losses = {"SPX": asset_price_loss(days, spx, n_spx, [event], params),
                "QQQ": asset_price_loss(days, qqq, n_qqq, [], params)}
    p_losses = {"SPX": asset_price_loss(days, spx, p_spx, [event], params),
                "QQQ": asset_price_loss(days, qqq, p_qqq, [], params)}
    weights = {"SPX": Decimal("0.5"), "QQQ": Decimal("0.5")}
    n_daily = daily_main_loss(days, n_losses, n_spx, weights, params)
    p_daily = daily_main_loss(days, p_losses, p_spx, weights, params)
    # 差额 = 0.005 − 0.5×2×ln(100/99) = −0.00505033585...，四舍五入6位为 −0.005050。
    difference = n_daily[0].total - p_daily[0].total
    assert difference.quantize(Decimal("0.000001")) == Decimal("-0.005050")
    # 两资产同日价格均缺失时，价格差额为零，仅留 N 的一次 γ=0.005。
    assert n_daily[0].switch_cost - p_daily[0].switch_cost == Decimal("0.005")


def test_both_assets_excluded_preserve_switch_penalty_difference() -> None:
    days = _days(2)
    params = LossParameters(Decimal(2), Decimal(1), Decimal("0.5"))
    closes = [Decimal(100), None]
    n = execute_asset(days, ["红", "红"], closes, params.eta, initial_executed="红")
    p = execute_asset(days, ["绿", "绿"], closes, params.eta)
    n = (replace(n[0], switched=True), n[1])
    n_asset = asset_price_loss(days, closes, n, [], params)
    p_asset = asset_price_loss(days, closes, p, [], params)
    weights = {"SPX": Decimal("0.5"), "QQQ": Decimal("0.5")}
    n_total = daily_main_loss(days, {"SPX": n_asset, "QQQ": n_asset}, n, weights, params)
    p_total = daily_main_loss(days, {"SPX": p_asset, "QQQ": p_asset}, p, weights, params)
    # 两资产价格项均因跨缺价被排除，差额只剩 N 的一次切换 γ=0.0025×2=0.005。
    assert n_total[0].total - p_total[0].total == Decimal("0.005")
