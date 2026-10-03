"""v2.0 留痕字段契约（阶段三实施指令第四节）：类型、字段校验与按时判断。只用构造数据。

这些测试只验收字段契约，不代替以后对转换逻辑的验收。例子里的价格与时间都是构造的。
“正确数值在裁定之前已取得”的构造例只验证类型契约，不表示人工修正值可以进入实际的前瞻信号。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from market_risk.wavewarn_v20.provenance_v20 import (
    Capture,
    PriceObservation,
    ProvenanceError,
    SignalInputSnapshot,
    Timeliness,
    timeliness,
    validate_trading_days,
)

ET = ZoneInfo("America/New_York")
UTC = dt.UTC
D = dt.date
SUMMER, WINTER = D(2024, 7, 10), D(2024, 1, 10)          # 夏令时（−04:00）与冬令时（−05:00）的两个周三
SHA = "a" * 64


def at(day: dt.date, hour: int, minute: int = 0, second: int = 0, zone: dt.tzinfo = ET) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=zone)


def observation(**changes) -> PriceObservation:
    """一条合法的实时留痕：SPX 2024-07-10，美东 17:00 取得，17:05 录入。"""
    fields = dict(asset="SPX", trade_day=SUMMER, raw_value="5633.914", close=Decimal("5633.91"), source="yahoo",
                  retrieved_at=at(SUMMER, 17), first_obtained_at_et=at(SUMMER, 17),
                  entered_at_utc=at(SUMMER, 21, 5, zone=UTC), capture=Capture.LIVE, source_sha256=SHA)
    fields.update(changes)
    return PriceObservation(**fields)


def backfill(first: dt.datetime | None, trade_day: dt.date = SUMMER, **changes) -> PriceObservation:
    """历史补录：本次取得时间为 2024-08-01，首次取得时间由参数给出（可为空）。"""
    later = D(2024, 8, 1)
    return observation(trade_day=trade_day, capture=Capture.BACKFILL, first_obtained_at_et=first,
                       retrieved_at=at(later, 10), entered_at_utc=at(later, 15, zone=UTC), **changes)


# ---------------------------------------------------------------------------
# 1、2：PriceObservation 的字段约束
# ---------------------------------------------------------------------------


def test_valid_observation_and_derived_values() -> None:
    record = observation()
    assert record.close == Decimal("5633.91")                      # published_price("5633.914")
    assert record.first_obtained_at_et == at(SUMMER, 17) and record.first_obtained_at_et.utcoffset() == dt.timedelta(
        hours=-4)
    qqq = observation(asset="QQQ", raw_value="497.5", close=Decimal("497.50"))
    assert qqq.asset == "QQQ"


@pytest.mark.parametrize(("changes", "message"), [
    ({"asset": "SPY"}, "资产"),                                                        # 资产非法
    ({"trade_day": D(1989, 12, 29)}, "登记首日"),                                      # SPX 早于 1990-01-02
    ({"asset": "QQQ", "trade_day": D(1999, 3, 9)}, "登记首日"),                        # QQQ 早于 1999-03-10
    ({"raw_value": "-5633.91"}, "raw_value"),                                          # 带符号
    ({"raw_value": "5633.91%"}, "raw_value"),                                          # 带单位
    ({"raw_value": "5,633.91"}, "raw_value"),
    ({"close": Decimal("5633.92")}, "published_price"),                             # 与 published_price(raw_value) 不等
    ({"close": Decimal("5633.914")}, "published_price"),
    ({"source": "tiingo"}, "来源"),                                                    # 来源非法
    ({"retrieved_at": dt.datetime(2024, 7, 10, 17, 0)}, "带时区"),                      # 时间不带时区
    ({"first_obtained_at_et": dt.datetime(2024, 7, 10, 17, 0)}, "带时区"),
    ({"entered_at_utc": dt.datetime(2024, 7, 10, 21, 5)}, "带时区"),
    ({"entered_at_utc": at(SUMMER, 17, 5)}, r"\+00:00"),                               # 录入时间偏移不为 +00:00
    ({"source_sha256": "A" * 64}, "source_sha256"),                                    # 大写
    ({"source_sha256": "a" * 63}, "source_sha256"),                                    # 位数不对
    ({"capture": "实时留痕"}, "capture"),
])
def test_field_constraints_reject_invalid_values(changes: dict, message: str) -> None:
    with pytest.raises(ProvenanceError, match=message):
        observation(**changes)


def test_first_listing_days_are_accepted() -> None:
    assert backfill(None, trade_day=D(1990, 1, 2)).trade_day == D(1990, 1, 2)
    first = backfill(None, trade_day=D(1999, 3, 10), asset="QQQ", raw_value="51.06", close=Decimal("51.06"))
    assert first.asset == "QQQ"


def test_trading_day_validation_uses_the_callers_calendar() -> None:
    days = frozenset({SUMMER, WINTER})
    validate_trading_days(observation(), days)
    with pytest.raises(ProvenanceError, match="不是交易日"):
        validate_trading_days(backfill(None, trade_day=D(2024, 7, 4)), days)      # 独立日，不在集合里


# ---------------------------------------------------------------------------
# 3、4、5：取得方式与时间顺序
# ---------------------------------------------------------------------------


def test_live_capture_requires_first_obtained_equal_to_retrieved() -> None:
    with pytest.raises(ProvenanceError, match="实时留痕"):
        observation(first_obtained_at_et=None)
    with pytest.raises(ProvenanceError, match="实时留痕"):
        observation(first_obtained_at_et=at(SUMMER, 16, 59))
    # 本次取得时间以其他时区给出：换算到美东后相等即可。
    record = observation(retrieved_at=at(SUMMER, 21, zone=UTC), first_obtained_at_et=at(SUMMER, 21, zone=UTC))
    assert record.first_obtained_at_et.utcoffset() == dt.timedelta(hours=-4) and record.first_obtained_at_et.hour == 17


def test_backfill_without_proof_leaves_first_obtained_empty_and_is_never_on_time() -> None:
    record = backfill(None)
    assert record.first_obtained_at_et is None and record.retrieved_at == at(D(2024, 8, 1), 10)
    for signal_day in (SUMMER, D(2024, 8, 1), D(2030, 1, 2)):
        assert timeliness(record, signal_day) is Timeliness.UNPROVEN
    # 有证据的历史补录可以填写首次取得时间。
    assert timeliness(backfill(at(SUMMER, 17)), SUMMER) is Timeliness.ON_TIME


def test_time_order_violations_are_rejected() -> None:
    with pytest.raises(ProvenanceError, match="first_obtained_at_et 不得晚于 retrieved_at"):
        backfill(at(D(2024, 8, 1), 10, 1))                                 # 首次取得晚于本次取得
    with pytest.raises(ProvenanceError, match="retrieved_at 不得晚于 entered_at_utc"):
        observation(entered_at_utc=at(SUMMER, 20, 59, zone=UTC))           # 录入早于取得（20:59 UTC = 16:59 ET）
    with pytest.raises(ProvenanceError, match="美东 00:00"):
        backfill(at(D(2024, 7, 9), 23, 59))                                # 早于交易日的美东 00:00
    assert backfill(at(SUMMER, 0, 0)).first_obtained_at_et == at(SUMMER, 0, 0)   # 恰为 00:00 可以


# ---------------------------------------------------------------------------
# 6：人工修正版本的取得时间
# ---------------------------------------------------------------------------


def snapshot(signal_day: dt.date, spx: list, qqq: list, built: dt.datetime | None = None) -> SignalInputSnapshot:
    built = built or at(D(2024, 8, 2), 0, zone=UTC)
    return SignalInputSnapshot(signal_day, {"SPX": spx, "QQQ": qqq}, built)


def qqq_live(day: dt.date = SUMMER) -> PriceObservation:
    return observation(asset="QQQ", trade_day=day, raw_value="497.5", close=Decimal("497.50"),
                       retrieved_at=at(day, 17), first_obtained_at_et=at(day, 17),
                       entered_at_utc=at(day, 22, 5, zone=UTC))


def test_corrected_value_without_proof_can_be_stored_but_not_enter_a_snapshot() -> None:
    """修正版本的首次取得时间无法证明时留空，可以构造；进入快照时以“无法证明按时”拒绝，不是“晚于截止时间”。"""
    corrected = backfill(None, source="correct:yahoo")
    assert corrected.source == "correct:yahoo" and corrected.first_obtained_at_et is None
    with pytest.raises(ProvenanceError) as caught:
        snapshot(SUMMER, [corrected], [qqq_live()])
    assert "无法证明按时" in str(caught.value) and "晚于截止时间" not in str(caught.value)


def test_correct_value_obtained_before_the_decision_date_is_legal() -> None:
    """正确数值可以在裁定之前已经取得：首次取得时间早于裁定日期，不因裁定日期被拒；若对信号日按时，可以进入快照。

    这只是类型契约的构造例，不授权人工修正值进入实际的前瞻信号。
    """
    decided_on = D(2024, 7, 20)                                 # 该修正在裁定表中的裁定日期（构造）
    corrected = backfill(at(SUMMER, 16, 30), source="correct:yahoo")
    assert corrected.first_obtained_at_et.date() < decided_on
    assert timeliness(corrected, SUMMER) is Timeliness.ON_TIME
    built = snapshot(SUMMER, [corrected], [qqq_live()])
    assert built.observations["SPX"] == (corrected,)
    assert "decided" not in {field.name for field in dataclasses.fields(PriceObservation)}    # 没有裁定日期字段


# ---------------------------------------------------------------------------
# 7：按时判断的边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day", [SUMMER, WINTER])
def test_cutoff_is_1830_eastern_inclusive_in_summer_and_winter(day: dt.date) -> None:
    assert timeliness(backfill(at(day, 18, 30, 0), trade_day=day), day) is Timeliness.ON_TIME       # 18:30:00 整
    assert timeliness(backfill(at(day, 18, 30, 1), trade_day=day), day) is Timeliness.LATE          # 18:30:01
    assert timeliness(backfill(None, trade_day=day), day) is Timeliness.UNPROVEN


def test_cutoff_with_times_given_in_other_time_zones() -> None:
    # 夏令时：美东 18:30 = UTC 22:30；冬令时：美东 18:30 = UTC 23:30。
    assert timeliness(backfill(at(SUMMER, 22, 30, zone=UTC)), SUMMER) is Timeliness.ON_TIME
    assert timeliness(backfill(at(SUMMER, 22, 30, 1, zone=UTC)), SUMMER) is Timeliness.LATE
    assert timeliness(backfill(at(WINTER, 23, 30, zone=UTC), trade_day=WINTER), WINTER) is Timeliness.ON_TIME
    assert timeliness(backfill(at(WINTER, 23, 30, 1, zone=UTC), trade_day=WINTER), WINTER) is Timeliness.LATE
    # 冬令时里的 UTC 22:30:01 仍然按时（美东 17:30:01）；固定按 −04:00 换算会误判。
    assert timeliness(backfill(at(WINTER, 22, 30, 1, zone=UTC), trade_day=WINTER), WINTER) is Timeliness.ON_TIME
    tokyo = dt.timezone(dt.timedelta(hours=9))
    assert timeliness(backfill(at(D(2024, 7, 11), 7, 30, zone=tokyo)), SUMMER) is Timeliness.ON_TIME   # 东京次日 07:30


class NoOffset(dt.tzinfo):
    """构造的时区：tzinfo 存在，但 utcoffset() 返回 None。"""

    def utcoffset(self, value: dt.datetime | None) -> None:
        return None

    def dst(self, value: dt.datetime | None) -> None:
        return None

    def tzname(self, value: dt.datetime | None) -> str:
        return "无偏移"


@pytest.mark.parametrize(("day", "et_hour"), [(SUMMER, 18), (WINTER, 18)])
def test_same_instant_in_three_time_zones_converts_to_the_same_eastern_time(day: dt.date, et_hour: int) -> None:
    """同一时刻分别以 UTC、美东、东京给出：换算到美东的结果相同，按时判断相同。冬季、夏季各一例。"""
    eastern = at(day, et_hour, 30)                                                # 恰为截止时刻
    given = [eastern.astimezone(UTC), eastern, eastern.astimezone(ZoneInfo("Asia/Tokyo"))]
    converted = [backfill(item, trade_day=day).first_obtained_at_et for item in given]
    assert converted[0] == converted[1] == converted[2] == eastern
    assert all(item.tzinfo == ET and item.hour == et_hour and item.minute == 30 for item in converted)
    assert {timeliness(backfill(item, trade_day=day), day) for item in given} == {Timeliness.ON_TIME}
    later = [item + dt.timedelta(seconds=1) for item in given]
    assert {timeliness(backfill(item, trade_day=day), day) for item in later} == {Timeliness.LATE}
    assert converted[0].utcoffset() == dt.timedelta(hours=-4 if day == SUMMER else -5)


def test_times_without_a_usable_offset_are_rejected() -> None:
    naive = dt.datetime(2024, 7, 10, 17, 0)
    with pytest.raises(ProvenanceError, match="带时区"):
        backfill(naive)
    no_offset = dt.datetime(2024, 7, 10, 17, 0, tzinfo=NoOffset())
    assert no_offset.tzinfo is not None and no_offset.utcoffset() is None
    for field in ("first_obtained_at_et", "retrieved_at", "entered_at_utc"):
        with pytest.raises(ProvenanceError, match="带时区"):
            observation(**{field: no_offset})


def test_constants_are_immutable() -> None:
    from market_risk.wavewarn_v20 import provenance_v20

    assert isinstance(provenance_v20.SOURCES, frozenset)
    assert isinstance(provenance_v20.CUTOFF, dt.time) and provenance_v20.CUTOFF == dt.time(18, 30)
    with pytest.raises(TypeError):
        provenance_v20.REGISTERED_FIRST_DAY["SPX"] = D(2000, 1, 3)                 # type: ignore[index]
    assert all(type(day) is dt.date for day in provenance_v20.REGISTERED_FIRST_DAY.values())


def test_cutoff_for_a_historical_observation_follows_the_signal_day() -> None:
    """历史观测的截止时刻按信号日计算，不按观测自己的交易日。"""
    earlier = D(2024, 7, 8)
    late_for_its_own_day = backfill(at(D(2024, 7, 9), 9, 0), trade_day=earlier)     # 次日上午才取得
    assert timeliness(late_for_its_own_day, earlier) is Timeliness.LATE
    assert timeliness(late_for_its_own_day, SUMMER) is Timeliness.ON_TIME


# ---------------------------------------------------------------------------
# 8、9、10：快照
# ---------------------------------------------------------------------------


def test_snapshot_with_all_observations_on_time() -> None:
    earlier = D(2024, 7, 9)
    history = backfill(at(earlier, 17), trade_day=earlier)
    built = snapshot(SUMMER, [observation(), history], [qqq_live()])
    assert set(built.observations) == {"SPX", "QQQ"}
    assert [item.trade_day for item in built.observations["SPX"]] == [earlier, SUMMER]     # 按 trade_day 升序
    assert built.same_day_prices_on_time is True


def test_snapshot_rejects_late_and_unproven_observations_with_separate_reasons() -> None:
    earlier = D(2024, 7, 9)
    late = backfill(at(D(2024, 7, 11), 9, 0), trade_day=earlier)          # 信号日之后才取得：晚于截止时间
    unproven = backfill(None, trade_day=D(2024, 7, 8))
    with pytest.raises(ProvenanceError) as caught:
        snapshot(SUMMER, [late, observation()], [qqq_live()])
    assert f"SPX {earlier}：晚于截止时间" in str(caught.value) and "无法证明按时" not in str(caught.value)
    with pytest.raises(ProvenanceError) as caught:
        snapshot(SUMMER, [unproven, observation()], [qqq_live()])
    assert "2024-07-08：无法证明按时" in str(caught.value) and "晚于截止时间" not in str(caught.value)
    # 两种同时出现：错误信息分别列出，各自原因正确；都不进入快照，也不静默剔除。
    with pytest.raises(ProvenanceError) as caught:
        snapshot(SUMMER, [unproven, late, observation()], [qqq_live()])
    message = str(caught.value)
    assert "2024-07-08：无法证明按时" in message and f"{earlier}：晚于截止时间" in message


def test_snapshot_rejects_observation_under_the_wrong_asset_key() -> None:
    with pytest.raises(ProvenanceError, match="资产与所在的键不一致"):
        snapshot(SUMMER, [observation(), qqq_live(D(2024, 7, 9))], [qqq_live()])       # QQQ 的观测放进 SPX 键下
    with pytest.raises(ProvenanceError, match="键集合"):
        SignalInputSnapshot(SUMMER, {"SPX": [observation()]}, at(D(2024, 8, 2), 0, zone=UTC))
    with pytest.raises(ProvenanceError, match="键集合"):
        SignalInputSnapshot(SUMMER, {"SPX": [], "QQQ": [], "SPY": []}, at(D(2024, 8, 2), 0, zone=UTC))


def test_snapshot_time_and_duplicate_rules() -> None:
    # 观测的录入时间晚于快照生成时间：拒绝。
    with pytest.raises(ProvenanceError, match="录入时间晚于快照生成时间"):
        snapshot(SUMMER, [observation()], [qqq_live()], built=at(SUMMER, 21, 0, zone=UTC))
    # 快照在次日才生成（晚于 18:30）：只要不早于录入时间就可以。
    next_day = snapshot(SUMMER, [observation()], [qqq_live()], built=at(D(2024, 7, 11), 15, 0, zone=UTC))
    assert next_day.built_at_utc.date() == D(2024, 7, 11)
    # 同一资产同一交易日两条观测：拒绝，不覆盖、不取最新。
    with pytest.raises(ProvenanceError, match="重复的观测"):
        snapshot(SUMMER, [observation(), observation(raw_value="5633.92", close=Decimal("5633.92"))], [qqq_live()])
    # 乱序且含重复：排序不放松校验，仍然拒绝。
    earlier = D(2024, 7, 9)
    with pytest.raises(ProvenanceError, match="重复的观测"):
        snapshot(SUMMER, [observation(), backfill(at(earlier, 17), trade_day=earlier),
                          backfill(at(earlier, 17, 1), trade_day=earlier)], [qqq_live()])
    with pytest.raises(ProvenanceError, match="交易日晚于信号日"):
        snapshot(D(2024, 7, 9), [observation()], [qqq_live(D(2024, 7, 9))])
    with pytest.raises(ProvenanceError, match=r"\+00:00"):
        snapshot(SUMMER, [observation()], [qqq_live()], built=at(D(2024, 7, 11), 9, 0))


# ---------------------------------------------------------------------------
# 11、12：深层不可变与推导属性
# ---------------------------------------------------------------------------


def test_snapshot_is_deeply_immutable() -> None:
    spx, qqq = [observation()], [qqq_live()]
    source = {"SPX": spx, "QQQ": qqq}
    built = SignalInputSnapshot(SUMMER, source, at(D(2024, 8, 2), 0, zone=UTC))
    spx.append(backfill(at(D(2024, 7, 9), 17), trade_day=D(2024, 7, 9)))        # 修改调用方原来的列表与字典
    source["QQQ"] = []
    assert len(built.observations["SPX"]) == 1 and len(built.observations["QQQ"]) == 1
    assert isinstance(built.observations["SPX"], tuple)
    with pytest.raises(TypeError):
        built.observations["SPX"] = ()                                           # type: ignore[index]
    with pytest.raises(TypeError):
        del built.observations["QQQ"]                                            # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        built.observations["SPX"][0].close = Decimal("1")                        # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        built.signal_day = WINTER                                                # type: ignore[misc]


def test_same_day_prices_on_time() -> None:
    assert snapshot(SUMMER, [observation()], [qqq_live()]).same_day_prices_on_time is True
    earlier = D(2024, 7, 9)
    only_spx_today = snapshot(SUMMER, [observation()], [qqq_live(earlier)])      # QQQ 只有前一日的观测
    assert only_spx_today.same_day_prices_on_time is False
    assert snapshot(SUMMER, [], []).same_day_prices_on_time is False
    text = SignalInputSnapshot.same_day_prices_on_time.__doc__ or ""
    assert "它不表示历史窗口完整、通道有效或整体信号可计算" in text


@pytest.mark.parametrize("raw_value", ["5633.914\n", "5633.914\r\n", "٥٦٣٣.٩١٤", "５６３３.９１４"])
def test_raw_value_rejects_trailing_newlines_and_non_ascii_digits(raw_value: str) -> None:
    """补修（正则排查）：末尾 \n、末尾 \r\n、阿拉伯-印度数字、全角数字，都被拒绝。"""
    with pytest.raises(ProvenanceError, match="raw_value"):
        observation(raw_value=raw_value)


@pytest.mark.parametrize("digest", ["a" * 64 + "\n", "a" * 64 + "\r\n", "١" * 64, "０" * 64, "ａ" * 64])
def test_source_sha256_rejects_trailing_newlines_and_non_ascii_characters(digest: str) -> None:
    with pytest.raises(ProvenanceError, match="source_sha256"):
        observation(source_sha256=digest)


def test_valid_raw_values_and_digests_still_pass() -> None:
    assert observation(raw_value="5633.914").raw_value == "5633.914"
    assert observation(raw_value="5634", close=Decimal("5634.00")).close == Decimal("5634.00")
    assert observation(source_sha256="0123456789abcdef" * 4).source_sha256 == "0123456789abcdef" * 4
