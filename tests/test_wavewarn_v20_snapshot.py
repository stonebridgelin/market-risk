"""v2.0 数据快照（登记第一节第 6 小节第 1 步、第四节第 5 小节）：只用构造数据。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from wavewarn_v20_helpers import WINDOWS, axis, random_closes, run_model

from market_risk.wavewarn_v20.convergence import Candidate
from market_risk.wavewarn_v20.inputs import snapshot_asset_days, snapshot_trend_days
from market_risk.wavewarn_v20.snapshot import SnapshotError, make_snapshot

ACQUIRED = dt.datetime(2001, 1, 6, 18, 20, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
HASHES = {"SPX": "a" * 64, "QQQ": "b" * 64}


def closes(values: list[str | None]) -> dict[dt.date, Decimal]:
    return {day: Decimal(value) for day, value in zip(axis(len(values)), values, strict=True) if value is not None}


def test_snapshot_rejects_reading_after_snapshot_day() -> None:
    days = axis(8)
    source = {"SPX": closes(["100", "101", "102", "103", "104", "105", "106", "107"]),
              "QQQ": closes(["50", "51", "52", "53", "54", "55", "56", "57"])}
    snapshot = make_snapshot(days, source, days[4], ACQUIRED, HASHES)
    assert snapshot.day == days[4] and snapshot.days == days[:5]
    assert snapshot.close("SPX", days[4]) == Decimal("104")
    # 快照日之后的日期：读取即报错，哪怕原始映射里有这一天。
    with pytest.raises(SnapshotError, match="之后"):
        snapshot.close("SPX", days[5])
    assert all(day <= days[4] for values in snapshot.closes.values() for day in values)
    assert snapshot.series("QQQ") == tuple(Decimal(value) for value in ("50", "51", "52", "53", "54"))


def test_snapshot_records_acquisition_time_and_source_hashes() -> None:
    days = axis(3)
    source = {"SPX": closes(["100", "101", "102"]), "QQQ": closes(["50", None, "52"])}
    snapshot = make_snapshot(days, source, days[2], ACQUIRED, HASHES)
    assert snapshot.acquired_at == ACQUIRED and snapshot.acquired_at.utcoffset() == dt.timedelta(hours=-5)
    assert dict(snapshot.source_hashes) == HASHES
    assert snapshot.close("QQQ", days[1]) is None          # 缺价不是 0，也不沿用前值
    with pytest.raises(SnapshotError, match="时区"):
        make_snapshot(days, source, days[2], ACQUIRED.replace(tzinfo=None), HASHES)
    with pytest.raises(SnapshotError, match="一一对应"):
        make_snapshot(days, source, days[2], ACQUIRED, {"SPX": "a" * 64})


def test_snapshot_is_immutable() -> None:
    days = axis(3)
    snapshot = make_snapshot(days, {"SPX": closes(["100", "101", "102"])}, days[2], ACQUIRED, {"SPX": "a" * 64})
    with pytest.raises(TypeError):
        snapshot.closes["SPX"][days[0]] = Decimal("1")      # type: ignore[index]
    with pytest.raises(AttributeError):
        snapshot.day = days[1]                              # type: ignore[misc]


def test_snapshot_rejects_inputs_outside_registered_states() -> None:
    days = axis(4)
    good = {"SPX": closes(["100", "101", "102", "103"])}
    with pytest.raises(SnapshotError, match="不在交易日轴上"):
        make_snapshot(days, good, days[3] + dt.timedelta(days=30), ACQUIRED, {"SPX": "a" * 64})
    with pytest.raises(SnapshotError, match="严格升序"):
        make_snapshot((days[1], days[0]), good, days[1], ACQUIRED, {"SPX": "a" * 64})
    off_axis = {"SPX": {days[0]: Decimal("100"), days[2]: Decimal("102")}}
    with pytest.raises(SnapshotError, match="不在交易日轴上"):
        make_snapshot((days[0], days[1], days[3]), off_axis, days[3], ACQUIRED, {"SPX": "a" * 64})
    with pytest.raises(SnapshotError, match="Decimal"):
        make_snapshot(days, {"SPX": {days[0]: 100.0}}, days[3], ACQUIRED, {"SPX": "a" * 64})  # type: ignore[dict-item]


def test_changing_data_after_snapshot_day_changes_nothing_up_to_that_day() -> None:
    """截止日不变性：快照日之后的数据不改变快照日及以前的任何输入与状态。"""
    length, cut = 420, 330
    days = axis(length)
    spx, qqq = random_closes(11, length), random_closes(12, length)
    altered_spx = [*spx[:cut + 1], *(value * 2 for value in random_closes(13, length - cut - 1))]
    altered_qqq = [*qqq[:cut + 1], *([None] * (length - cut - 1))]

    def snapshot_at(a: list[Decimal | None], b: list[Decimal | None]):
        source = {"SPX": {day: value for day, value in zip(days, a, strict=True) if value is not None},
                  "QQQ": {day: value for day, value in zip(days, b, strict=True) if value is not None}}
        return make_snapshot(days, source, days[cut], ACQUIRED, HASHES)

    first, second = snapshot_at(spx, qqq), snapshot_at(altered_spx, altered_qqq)
    assert first == second
    for name in ("SPX", "QQQ"):
        assert snapshot_asset_days(first, name, WINDOWS) == snapshot_asset_days(second, name, WINDOWS)
    assert snapshot_trend_days(first, "SPX", WINDOWS) == snapshot_trend_days(second, "SPX", WINDOWS)

    # 用完整序列算到末日，再与只算到快照日的结果比较：重叠部分逐日相同。
    candidate = Candidate(5, Decimal("0.02"), 3)
    short = run_model(spx[:cut + 1], qqq[:cut + 1], candidate)
    for altered in (run_model(spx, qqq, candidate), run_model(altered_spx, altered_qqq, candidate)):
        assert altered.t0 == short.t0
        assert altered.spx[:cut + 1] == short.spx and altered.ma[:cut + 1] == short.ma
        assert altered.evidence[:len(short.evidence)] == short.evidence
        assert altered.system[:len(short.system)] == short.system
