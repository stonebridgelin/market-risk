"""事件账的五类互斥分类以手工构造灯色核对。"""

import datetime as dt
from decimal import Decimal

from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.labels_zz import ZZEvent, merge_zz_events
from market_risk.wavewarn.ledgers import (
    build_alert_ledger,
    classify_asset_event,
    classify_event,
    classify_merged_event,
    event_timing_details,
    summarize_event_classes,
    yearly_alert_summary,
)
from market_risk.wavewarn.loss import LossParameters, asset_price_loss


def _days() -> list[dt.date]:
    return [dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(50)]


def _classify(non_green: set[int]) -> object:
    days = _days()
    lights = ["黄" if index in non_green else "绿" for index in range(len(days))]
    return classify_event(days, lights, days[20], days[30], days[40])


def test_event_ledger_five_mutually_exclusive_classes() -> None:
    # T0=30，T0−1=29，事前窗口从 P−20=0 至28；危险后段为30..39。
    cases = [({29}, "新警报"), (set(range(5, 30)), "持续覆盖"),
             ({31}, "迟到"), ({25}, "中断"), (set(), "漏报")]
    assert [_classify(days).classification for days, _ in cases] == [kind for _, kind in cases]


def test_t0_minus_one_signal_is_not_yet_executed_before_t0() -> None:
    result = _classify({29})
    # 只在第29日收盘后首次亮黄：信号已产生，30日收盘才执行，故 T0 前尚未降险。
    assert result.signal_before_t0 is True
    assert result.reduction_executed_before_t0 is False
    assert result.lead_days == 1
    assert result.alert_start == _days()[29]


def test_right_censored_event_keeps_timing_but_is_outside_five_class_summary() -> None:
    days = _days()
    event = ZZEvent("SPX", days[20], days[30], days[40], None,
                    Decimal(100), Decimal(90), True)
    lights = ["绿"] * len(days)
    lights[29] = "黄"
    row = classify_asset_event(days, lights, event)
    # 第29日收盘已亮黄、第30日收盘才执行；低点40只是暂定，不能归入五类比例。
    assert row.right_censored is True
    assert row.classification is None
    assert row.signal_before_t0 is True
    assert row.reduction_executed_before_t0 is False
    assert row.alert_start == days[29]
    assert row.lead_days == 1
    confirmed = _classify({29})
    summary = summarize_event_classes((confirmed, row))
    # 1段已确认“新警报”与1段右截尾：五类比例的分母是1，故新警报为1而非1/2。
    assert (summary.confirmed, summary.right_censored) == (1, 1)
    assert summary.counts["新警报"] == 1
    assert summary.proportions["新警报"] == Decimal(1)
    assert summarize_event_classes((row,)).proportions["新警报"] is None


def test_alert_ledger_protection_uses_executed_exposure_and_flags_no_event() -> None:
    days = _days()[:4]
    closes = [Decimal(100), Decimal(99), Decimal(98), Decimal(99)]
    event = ZZEvent("SPX", days[0], days[1], days[2], days[3], Decimal(100), Decimal(98), False)
    params = LossParameters(Decimal(2), Decimal(1), Decimal("0.5"))
    lights = ["黄", "红", "绿", "绿"]
    executions = execute_asset(days, lights, closes, params.eta, initial_executed="红")
    losses = asset_price_loss(days, closes, executions, [event], params)
    ledger = build_alert_ledger(days, lights, [("P",), ("PR",), (), ()], [event],
                                {"SPX": losses}, {"SPX": Decimal(1)})
    # 第0→1段执行红，避开 ln(100/99)；第1→2段执行黄，避开半数 ln(99/98)。
    expected = (Decimal(100) / Decimal(99)).ln() + Decimal("0.5") * (Decimal(99) / Decimal(98)).ln()
    assert len(ledger) == 1
    assert abs(ledger[0].protected_decline - expected) < Decimal("1e-25")
    assert ledger[0].active_channels == ("P", "PR")
    assert ledger[0].no_event_alert is False


def test_yearly_alert_summary_clips_cross_year_segment() -> None:
    days = [dt.date(2010, 12, 30), dt.date(2010, 12, 31),
            dt.date(2011, 1, 3), dt.date(2011, 1, 4)]
    lights = ["黄", "黄", "红", "红"]
    executions = execute_asset(days, lights, [Decimal(100)] * 4, Decimal("0.5"))
    yearly = yearly_alert_summary(days, lights, executions, [("P",)] * 4)
    # 一段警报横跨两年，但每年只计算本年的连续两日，非绿比例均为2/2=1。
    assert [(row.year, row.longest_alert_days, row.non_green_ratio) for row in yearly] == [
        (2010, 2, Decimal(1)), (2011, 2, Decimal(1))]
    assert [row.active_channel_days["P"] for row in yearly] == [2, 2]


def test_event_first_alert_release_before_trough_and_future_drawdown() -> None:
    days = _days()[:26]
    event = ZZEvent("SPX", days[1], days[3], days[5], days[8], Decimal(100), Decimal(90), False)
    lights = ["绿"] * len(days)
    lights[2:4] = ["黄", "黄"]
    closes = [Decimal(100)] * len(days)
    closes[4], closes[5] = Decimal(95), Decimal(90)
    details = event_timing_details(days, lights, closes, event,
                                   [("P_SPX",) if index in (2, 3) else () for index in range(len(days))],
                                   ["P 退出" if index == 4 else "" for index in range(len(days))])
    # 首警报段第2至3天，首次转绿第4天，早于第5天的最终低点。
    assert (details.first_alert_date, details.first_green_date, details.green_before_trough) == (
        days[2], days[4], True)
    # 事后反弹值=(95/90−1)×100；转绿后五日最低=90，跌幅=(90/95−1)×100。
    assert details.rebound_from_trough_percent == (Decimal(95) / Decimal(90) - 1) * 100
    assert details.decline_after_green_percent[5] == (Decimal(90) / Decimal(95) - 1) * 100
    assert details.first_alert_channels == ("P_SPX",)
    assert details.release_reason == "P 退出"


def test_event_release_after_trough_and_incomplete_future_windows_are_blank() -> None:
    days = _days()[:26]
    event = ZZEvent("SPX", days[1], days[3], days[5], days[8], Decimal(100), Decimal(90), False)
    lights = ["绿"] * len(days)
    lights[2:8] = ["黄"] * 6
    lights[10] = "黄"
    closes: list[Decimal | None] = [Decimal(100)] * len(days)
    closes[8] = Decimal(99)
    closes[9] = Decimal(98)
    closes[14] = None
    details = event_timing_details(days, lights, closes, event, [()] * len(days), [""] * len(days))
    # 首警报段第2至7天，首次转绿第8天晚于第5天低点；事后反弹=(99/90−1)×100。
    assert (details.first_alert_date, details.first_green_date, details.green_before_trough) == (
        days[2], days[8], False)
    assert details.rebound_from_trough_percent == (Decimal(99) / Decimal(90) - 1) * 100
    # 未来5天最低98，跌幅=(98/99−1)×100；10日窗口含缺价，20日窗口未结束，均留空。
    assert details.decline_after_green_percent == {
        5: (Decimal(98) / Decimal(99) - 1) * 100, 10: None, 20: None}
    assert details.reupgraded_after_green == {5: True, 10: True, 20: None}


def test_outside_danger_non_green_days_require_both_assets_outside() -> None:
    days = _days()[:3]
    params = LossParameters(Decimal(2), Decimal(1), Decimal("0.5"))
    event = ZZEvent("SPX", days[0], days[1], days[2], None, Decimal(100), Decimal(90), True)
    # SPX 的[高点,暂定低点)两段危险，QQQ 同时无危险；警报天数不能按 QQQ 单边计入“区间外”。
    spx = [Decimal(100), Decimal(95), Decimal(90)]
    qqq = [Decimal(100), Decimal(101), Decimal(102)]
    lights = ["黄", "黄", "绿"]
    spx_exec = execute_asset(days, lights, spx, params.eta)
    qqq_exec = execute_asset(days, lights, qqq, params.eta)
    losses = {"SPX": asset_price_loss(days, spx, spx_exec, [event], params),
              "QQQ": asset_price_loss(days, qqq, qqq_exec, [], params)}
    rows = build_alert_ledger(days, lights, [(), (), ()], [event], losses,
                              {"SPX": Decimal("0.5"), "QQQ": Decimal("0.5")})
    assert rows[0].outside_danger_days == 0
    # 净机会成本仍按 QQQ 的区间外收益独立计算，不因 SPX 危险而丢掉。
    assert rows[0].net_opportunity_cost > 0


def test_merged_event_uses_earliest_t0_and_latest_trough_for_light_classification() -> None:
    days = _days()
    spx = ZZEvent("SPX", days[20], days[30], days[35], days[38], Decimal(100), Decimal(90), False)
    qqq = ZZEvent("QQQ", days[25], days[32], days[40], days[43], Decimal(200), Decimal(170), False)
    merged = merge_zz_events([spx, qqq])[0]
    lights = ["绿"] * len(days)
    lights[29] = "黄"
    row = classify_merged_event(days, lights, merged)
    # 合并后P=第20天、T0=较早的第30天、Tr=较晚的第40天；第29天的黄灯是新警报。
    assert (row.peak_date, row.t0_date, row.trough_date, row.classification) == (
        days[20], days[30], days[40], "新警报")


def test_merged_event_with_right_censored_member_is_separately_marked() -> None:
    days = _days()
    spx = ZZEvent("SPX", days[20], days[30], days[35], days[38], Decimal(100), Decimal(90), False)
    qqq = ZZEvent("QQQ", days[25], days[32], days[40], None, Decimal(200), Decimal(170), True)
    merged = merge_zz_events([spx, qqq])[0]
    lights = ["绿"] * len(days)
    lights[29] = "黄"
    row = classify_merged_event(days, lights, merged)
    # 两段闭区间重叠，但 QQQ 尚未确认结束，合并账不能归入已确认事件五类。
    assert (row.right_censored, row.classification, row.lead_days) == (True, None, 1)
