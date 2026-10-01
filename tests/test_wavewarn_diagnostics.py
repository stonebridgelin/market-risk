"""开发期诊断仅按此前特征更新，不凭未来行改变过去状态。"""

import csv
import datetime as dt
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.wavewarn.config import CandidateSets, ChannelSelection, load_wavewarn_config
from market_risk.wavewarn.diagnostics import (
    diagnostic_sequence,
    first_complete_day,
    n_diagnostic_sequence,
    write_anchor_126_audit,
    write_input_coverage,
)
from market_risk.wavewarn.features import AssetFeatures
from market_risk.wavewarn.inputs import DevelopmentInputs


def test_anchor_126_audit_separates_p_and_pr_thresholds(tmp_path: Path) -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(126))
    spx = {day: Decimal(99) for day in days}
    qqq = dict(spx)
    spx[days[0]] = qqq[days[0]] = Decimal(100)
    spx[days[-1]], qqq[days[-1]] = Decimal("98.5"), Decimal("97.5")
    inputs = DevelopmentInputs(days, {"SPX": spx, "QQQ": qqq})
    config = load_wavewarn_config(Path(__file__).resolve().parents[1] / "config/wavewarn_v121.yaml")
    candidates = CandidateSets((3,), (Decimal("0.01"),), (Decimal("0.10"),))
    path = tmp_path / "anchor.csv"
    assert write_anchor_126_audit(inputs, path, config.fixed_parameters(), candidates) == 2
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    # SPX短窗高99：1−98.5/99≈0.005<1%，长窗高100：1.5%≥1%，仅P。
    # QQQ短窗高99：1−97.5/99≈1.515%<2%，长窗高100：2.5%≥2%，仅PR。
    assert [(row["symbol"], row["channel"], row["entry_threshold"], row["date"])
            for row in rows] == [("SPX", "P", "0.01", days[-1].isoformat()),
                                ("QQQ", "PR", "0.02", days[-1].isoformat())]


def _feature(day: dt.date, drawdown: str) -> AssetFeatures:
    close = Decimal(100) * (Decimal(1) - Decimal(drawdown))
    return AssetFeatures(day, close, Decimal(100), Decimal(drawdown), 0, 10,
                         Decimal(60), Decimal(0), Decimal(0), Decimal(0), Decimal(50),
                         Decimal(90), Decimal(80), Decimal(100), Decimal(100))


def test_diagnostic_t0_snapshot_and_future_extreme_do_not_change_prior_lights() -> None:
    days = [dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(4)]
    spx = [_feature(day, value) for day, value in zip(days, ("0", "0.02", "0.03", "0"), strict=True)]
    qqq = [_feature(day, "0") for day in days]
    first = diagnostic_sequence(days, spx, qqq, days[0], "P1-E2", Decimal("0.015"), 3)
    modified = list(spx)
    modified[-1] = replace(modified[-1], close=Decimal("0.01"), drawdown63=Decimal("0.9999"))
    second = diagnostic_sequence(days, modified, qqq, days[0], "P1-E2", Decimal("0.015"), 3)
    # t0 虽然能计算谓词也不处理：初始绿灯；第1天 SPX 的 P 触发后转黄，未来第3天的极端值不回写。
    assert first[0].light == "绿"
    assert first[1].light == "黄"
    assert first[:3] == second[:3]
    # 第3天随后才追加进时间轴，前3天的灯色、计数与原因须逐字一致。
    appended = diagnostic_sequence(days[:3], spx[:3], qqq[:3], days[0], "P1-E2", Decimal("0.015"), 3)
    assert first[:3] == appended


def test_t0_requires_ten_valid_vix_ratios_and_three_day_repair_history() -> None:
    days = [dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(12)]
    assets = [_feature(day, "0") for day in days]
    vix = {day: Decimal(20) for day in days}
    vix3m = {day: Decimal(25) for day in days}
    # 其余回看均完整，VIX3M 从第0天开始；V 通道10日退出回看到第9天才完整。
    assert first_complete_day(days, assets, assets, vix, vix3m) == days[9]
    # 第8天广度缺失，使第9、10天修复回看不完整；第11天窗口已排除缺值日。
    altered = list(assets)
    altered[8] = replace(altered[8], breadth=None)
    assert first_complete_day(days, altered, assets, vix, vix3m) == days[11]


def test_n_diagnostic_requires_explicit_four_side_switches() -> None:
    days = [dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(3)]
    spx = [_feature(day, value) for day, value in zip(days, ("0", "0.02", "0.02"), strict=True)]
    qqq = [_feature(day, "0") for day in days]
    ratios = [Decimal("0.8")] * 3
    selection = ChannelSelection(False, False, False, False)
    rows = n_diagnostic_sequence(days, spx, qqq, ratios, days[0], Decimal("0.015"), 3, selection, "E1")
    # 这是构造开关，不写入实际配置；第1天 P_SPX 达2%，N 转黄。
    assert (rows[0].light, rows[1].light) == ("绿", "黄")
    with pytest.raises(ValueError, match="显式填写"):
        n_diagnostic_sequence(days, spx, qqq, ratios, days[0], Decimal("0.015"), 3,
                              ChannelSelection(None, False, False, False), "E1")  # type: ignore[arg-type]


def test_input_coverage_separates_not_started_gap_and_off_calendar(tmp_path: Path) -> None:
    days = [dt.date(2010, 1, 4), dt.date(2010, 1, 5), dt.date(2010, 1, 6)]
    values = {days[1]: Decimal(1), dt.date(2010, 1, 9): Decimal(2)}
    output = tmp_path / "coverage.csv"
    write_input_coverage(DevelopmentInputs(tuple(days), {"X": values}), output)
    # 第0天尚未开始；第2天在开始后却缺值；周六一行属于额外日期。
    assert "X,2010-01-05,2010-01-09,2,1,1,1" in output.read_text(encoding="utf-8")
