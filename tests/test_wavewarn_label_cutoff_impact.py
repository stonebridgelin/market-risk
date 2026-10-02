"""旧补充历史的标签影响量化：构造数据测试，期望值人工推算并写在说明里。"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import subprocess
import sys
from decimal import Decimal

import pytest

from market_risk.wavewarn.evaluation import Candidate, CandidateEvaluation
from market_risk.wavewarn.execution import ExecutionDay, execute_asset, exposure
from market_risk.wavewarn.label_cutoff_impact import (
    ImpactError,
    ObjectImpact,
    comparisons,
    interval_class,
    label_differences,
    local_adjustment,
    local_total,
    object_impact,
    ranking,
    reconcile,
)
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.loss import LossParameters, asset_price_loss, daily_main_loss
from market_risk.wavewarn.timing import timing_result

D = Decimal
DAYS = tuple(dt.date(2021, 3, 1) + dt.timedelta(days=index) for index in range(4))
CLOSES = {"SPX": (D(100), D(98), D(99), D(97)), "QQQ": (D(100), D(100), D(102), D(101))}
WEIGHTS = {"SPX": D("0.5"), "QQQ": D("0.5")}
PARAMS = LossParameters(D(2), D(1), D("0.5"))                 # κ_D = 2，β = 1，η = 0.5，κ_0 = β ÷ 2
TOLERANCE = D("1e-10")
# 原标签：SPX 有一个事件，高点在第 1 日，低点在窗口之后 → 第 1、2 日起点的区间为危险；QQQ 没有事件。
OLD_EVENTS = {"SPX": (ZZEvent("SPX", DAYS[1], DAYS[2], dt.date(2021, 3, 10), dt.date(2021, 3, 15), D(98), D(90),
                              False),), "QQQ": ()}
OLD_UNKNOWN = UnknownLabels(dt.date(2021, 12, 31), {"SPX": frozenset(), "QQQ": frozenset()}, {"SPX": {}, "QQQ": {}})
# 截止日标签：没有事件；两个资产自第 1 日起都是尾段（寻峰）未定。
TAIL = {DAYS[1]: "尾段（寻峰）", DAYS[2]: "尾段（寻峰）"}
NEW_EVENTS: dict[str, tuple[ZZEvent, ...]] = {"SPX": (), "QQQ": ()}
NEW_UNKNOWN = UnknownLabels(DAYS[3], {"SPX": frozenset(TAIL), "QQQ": frozenset(TAIL)}, {"SPX": TAIL, "QQQ": TAIL})


def _evaluate(name: str, signals: tuple[str, ...], events, unknown) -> CandidateEvaluation:
    """信号全同的对象按恒定参照行处理（自始至终同一暴露、零切换）；其余按次日收盘执行。"""
    if len(set(signals)) == 1:
        level = exposure(signals[0], PARAMS.eta)  # type: ignore[arg-type]
        executions = {symbol: tuple(ExecutionDay(day, signals[0], signals[0], level, True, False)  # type: ignore[arg-type]
                                    for day in DAYS) for symbol in WEIGHTS}
        lights = signals
    else:
        executions = {symbol: execute_asset(DAYS, signals, CLOSES[symbol], PARAMS.eta)  # type: ignore[arg-type]
                      for symbol in WEIGHTS}
        lights = ("绿", *signals[:-1])
    assets = {symbol: asset_price_loss(DAYS, CLOSES[symbol], executions[symbol], events[symbol], PARAMS, unknown,
                                       symbol) for symbol in WEIGHTS}
    daily = daily_main_loss(DAYS, assets, executions["SPX"], WEIGHTS, PARAMS, (), D(0))
    return CandidateEvaluation(Candidate(name, 0, D(0), None, 0), DAYS, signals, ("完整",) * 4, ((),) * 4,  # type: ignore[arg-type]
                               ("构造",) * 4, executions, assets, daily, lights)


def _objects() -> dict[str, tuple[CandidateEvaluation, CandidateEvaluation]]:
    signals = {"始终绿": ("绿",) * 4, "始终红": ("红",) * 4, "设定": ("绿", "红", "红", "红")}
    return {name: (_evaluate(name, lights, OLD_EVENTS, OLD_UNKNOWN), _evaluate(name, lights, NEW_EVENTS, NEW_UNKNOWN))
            for name, lights in signals.items()}


def _impacts() -> dict[str, ObjectImpact]:
    objects = _objects()
    green, red = objects["始终绿"], objects["始终红"]
    differences = label_differences(green[0].asset_losses, green[1].asset_losses)
    locals_ = {name: local_total(local_adjustment(pair[0], pair[1], differences, WEIGHTS))
               for name, pair in objects.items()}
    result = {}
    for name, (old, new) in objects.items():
        timings = [timing_result(item, PARAMS.eta, ends[0].total_loss, ends[1].total_loss)
                   for item, ends in ((old, (green[0], red[0])), (new, (green[1], red[1])))]
        result[name] = object_impact(name, "参照行" if name != "设定" else "候选设定", (old, timings[0]),
                                     (new, timings[1]), differences, WEIGHTS, locals_["始终绿"], locals_["始终红"])
    return result


def test_label_differences_list_every_asset_interval_whose_class_changed() -> None:
    """三个区间（起点为第 0、1、2 日）。原标签：SPX 的第 1、2 个区间危险，QQQ 全部非危险；

    截止日标签：两个资产的第 1、2 个区间都是尾段（寻峰）未定。第 0 个区间两套标签下都是非危险。
    所以归类不同的资产区间共 4 个：SPX 两个（危险 → 未定），QQQ 两个（非危险 → 未定）。
    """
    green = _objects()["始终绿"]
    differences = label_differences(green[0].asset_losses, green[1].asset_losses)
    assert [(item.symbol, item.start, item.old_class, item.new_class) for item in differences] == [
        ("QQQ", DAYS[1], "非危险", "未定：尾段（寻峰）"), ("SPX", DAYS[1], "危险", "未定：尾段（寻峰）"),
        ("QQQ", DAYS[2], "非危险", "未定：尾段（寻峰）"), ("SPX", DAYS[2], "危险", "未定：尾段（寻峰）")]
    assert interval_class(green[0].asset_losses["SPX"][0]) == "非危险"
    assert label_differences(green[0].asset_losses, green[0].asset_losses) == ()
    with pytest.raises(ImpactError, match="日期轴"):
        label_differences(green[0].asset_losses, {symbol: rows[:-1] for symbol, rows in green[1].asset_losses.items()})


def test_local_adjustments_by_hand_and_linear_score_change() -> None:
    """SPX 收盘 100、98、99、97；QQQ 收盘 100、100、102、101；权重各半；κ_D = 2，β = 1。

    始终绿（e = 1）：原标签下 SPX 第 2 个区间危险且下跌，危险项 = 2 × 1 × ln(99 ÷ 97) = 2 × 0.020409 = 0.040818；
    第 1 个区间危险但上涨，危险项 0。QQQ 的回撤没有超过 2% 的噪声带，回撤项 0；满暴露时机会项 0。
    截止日标签下这四个资产区间都记零 → ΔL_G = −0.5 × 0.040818 = −0.020409，全部来自 SPX。
    始终红（e = 0）：原标签下 QQQ 第 1、2 个区间有机会项 ln(1.02) + ln(101 ÷ 102) = ln(1.01) = 0.009950；
    SPX 的危险区间不计机会项。截止日标签下记零 → ΔL_R = −0.5 × 0.009950 = −0.004975，全部来自 QQQ。
    设定（信号 绿、红、红、红 → 三个区间的执行暴露 1、1、0，ē = 2/3）：
    原标签下 QQQ 第 2 个区间（e = 0）机会项 ln(101 ÷ 102) = −0.009852；SPX 第 2 个区间危险但空仓，危险项 0。
    截止日标签下记零 → ΔL = +0.5 × 0.009852 = +0.004926。
    ΔT = ΔL − ē·ΔL_G − (1 − ē)·ΔL_R = 0.004926 + (2/3) × 0.020409 + (1/3) × 0.004975
       = 0.004926 + 0.013606 + 0.001658 = 0.020190。
    """
    impacts = _impacts()
    green, red, setting = impacts["始终绿"], impacts["始终红"], impacts["设定"]
    danger = 2 * math.log(99 / 97)
    assert float(green.change("total_loss")) == pytest.approx(-0.5 * danger, abs=1e-12)
    assert float(green.change("danger_loss")) == pytest.approx(-0.5 * danger, abs=1e-12)
    assert float(sum(green.local["SPX"].values())) == pytest.approx(-0.020409, abs=1e-6)
    assert sum(green.local["QQQ"].values()) == 0
    assert float(red.change("total_loss")) == pytest.approx(-0.5 * math.log(1.01), abs=1e-12)
    assert float(sum(red.local["QQQ"].values())) == pytest.approx(-0.004975, abs=1e-6) and sum(
        red.local["SPX"].values()) == 0
    assert float(setting.change("total_loss")) == pytest.approx(-0.5 * math.log(101 / 102), abs=1e-12)
    assert setting.old.mean_exposure == setting.new.mean_exposure == D(2) / 3
    expected = -0.5 * math.log(101 / 102) + (2 / 3) * 0.5 * danger + (1 / 3) * 0.5 * math.log(1.01)
    assert float(setting.change("score")) == pytest.approx(expected, abs=1e-12)
    assert float(setting.local_score) == pytest.approx(0.020190, abs=1e-6)
    # 始终绿、始终红的 T 按定义为 0，两套标签下都不变。
    assert abs(green.change("score")) < TOLERANCE and abs(red.change("score")) < TOLERANCE
    # 核对：逐区间的局部调整之和等于汇总的变化（三个价格分项、主损失、T）。
    for item in impacts.values():
        assert max(abs(gap) for gap in reconcile(item, TOLERANCE)) < TOLERANCE


def test_reconcile_stops_when_the_local_sum_or_the_exposure_does_not_match() -> None:
    """局部调整之和与汇总的变化不一致、或暴露与切换在两套标签下不同，都报错停下。"""
    setting = _impacts()["设定"]
    wrong = dataclasses.replace(setting, local_score=setting.local_score + D("0.000001"))
    with pytest.raises(ImpactError, match="不一致"):
        reconcile(wrong, TOLERANCE)
    missing = dataclasses.replace(setting, local={symbol: dict.fromkeys(parts, D(0))
                                                  for symbol, parts in setting.local.items()})
    with pytest.raises(ImpactError, match="不一致"):
        reconcile(missing, TOLERANCE)
    moved = dataclasses.replace(setting, new=dataclasses.replace(setting.new, mean_exposure=D("0.5")))
    with pytest.raises(ImpactError, match="平均暴露"):
        reconcile(moved, TOLERANCE)
    # 暴露不同的区间不能做局部核算。
    objects = _objects()
    differences = label_differences(objects["始终绿"][0].asset_losses, objects["始终绿"][1].asset_losses)
    with pytest.raises(ImpactError, match="执行暴露"):
        local_adjustment(objects["始终绿"][0], objects["始终红"][1], differences, WEIGHTS)


def test_rankings_and_comparison_directions() -> None:
    """排序按主损失或 T 从小到大。比较方向 = 设定 − 参照 的正负：由负变正（或由正变负）才算改变。

    构造例里设定的 T：原标签下 L = 0.000074，L_G = 0.020409，L_R = −0.005126，ē = 2/3，
    T = 0.000074 − (2/3 × 0.020409 − 1/3 × 0.005126) = −0.011823；
    截止日标签下为 −0.011823 + 0.020190 = +0.008367（始终绿的 T 恒为 0）：
    相对始终绿的 T 之差由负变正，方向改变。
    相对始终红的主损失之差：原标签下 设定 0.000074（机会项 −0.004926 加一次切换 0.005）− 始终红 −0.005126 = +0.005200；
    截止日标签下 设定 0.005 − 始终红 −0.010101（只剩第 0 个区间 SPX 的机会项 0.5 × ln(0.98)）= +0.015101：方向不变。
    """
    impacts = _impacts()
    setting, green, red = impacts["设定"], impacts["始终绿"], impacts["始终红"]
    other = dataclasses.replace(setting, name="另一设定", new=dataclasses.replace(
        setting.new, total_loss=setting.new.total_loss - D(1), score=setting.new.score + D(1)))
    items = (setting, other)
    assert ranking(items, "total_loss", False) == ("设定", "另一设定")          # 原标签下两者相同，保持原次序
    assert ranking(items, "total_loss", True) == ("另一设定", "设定")
    assert ranking(items, "score", True) == ("设定", "另一设定")
    rows = {(item.setting, item.reference, item.field): item for item in comparisons(items, (green, red))}
    plain = rows["设定", "始终绿", "score"]
    assert plain.old == setting.old.score - green.old.score and plain.new == setting.new.score - green.new.score
    assert float(setting.old.score) == pytest.approx(-0.011823, abs=1e-6)
    assert float(setting.new.score) == pytest.approx(0.008367, abs=1e-6) and plain.direction_changed
    loss = rows["设定", "始终红", "total_loss"]
    assert float(loss.old) == pytest.approx(0.005200, abs=1e-6) and float(loss.new) == pytest.approx(0.015101, abs=1e-6)
    assert not loss.direction_changed


def test_impact_algorithm_modules_do_not_import_io_modules() -> None:
    code = ("import importlib, json, sys\n"
            "for name in ('label_cutoff_impact', 'label_cutoff_impact_report'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "bad = [m for m in sys.modules if m in ('market_risk.wavewarn.inputs', 'market_risk.wavewarn.export',"
            " 'market_risk.wavewarn.evaluation_run', 'market_risk.wavewarn.label_cutoff_impact_run',"
            " 'market_risk.wavewarn.extended_nav_run', 'market_risk.wavewarn.lock_guard',"
            " 'market_risk.services', 'market_risk.cli') or m.startswith('market_risk.storage')]\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
