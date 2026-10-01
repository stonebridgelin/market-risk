"""v1.4 一致性审计的补充项（docs/research/v1.4_一致性审计.md 第九节起）。

读取真实数据的案例只读开发期（截至 2016-12-30）的输入与已入库的开发期输出；其余为构造数据。
凡与生产代码比对的量，都在本文件里按规格原文另写一遍，不调用被核对的那段实现。
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import gzip
import io
import itertools
import json
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from market_risk.wavewarn.channels import ChannelDay, ChannelPredicate, run_channel, update_channel
from market_risk.wavewarn.config_v14 import load_validation_config
from market_risk.wavewarn.evaluation import SYMBOLS, PreparedEvaluation, evaluate_candidate, grid_features
from market_risk.wavewarn.export import read_zz_events
from market_risk.wavewarn.features import AssetFeatures, asset_features
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.inputs import load_development_inputs, series_until
from market_risk.wavewarn.labels_zz import UnknownLabels, build_unknown_labels, find_zz_events
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.state_machine import ReadyInputs, SystemMemory, release_f_step
from market_risk.wavewarn.state_sequences import first_complete_day
from market_risk.wavewarn.v14_model import (
    FULL,
    prepare_v14,
    release_f_inputs,
    release_f_sequence,
    v14_channels,
    v14_grid,
)

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = load_validation_config(ROOT / "config/wavewarn_v14_validation.yaml")
MODEL = VALIDATION.model
FIXED = MODEL.base.fixed_parameters()
CUTOFF = dt.date(2016, 12, 30)
STORED = ROOT / "reports/research/wavewarn_v14/evaluation_development"
STORED_ASRUN = STORED / "superseded/2026-10-01_修正前_v1.4-asrun"       # 原实现（标签 v1.4-asrun）的输出
D = Decimal
ZZ_LEVELS = {"SPX": (D("0.04"), D("0.05")), "QQQ": (D("0.05"), D("0.065"))}
DAYS = tuple(dt.date(2012, 1, 2) + dt.timedelta(days=index) for index in range(300))


# ---------------------------------------------------------------------------
# 补充 1：回撤纪录由原始收盘价与重置日独立重建
# ---------------------------------------------------------------------------

def _raw_closes(symbol: str) -> list[tuple[dt.date, Decimal]]:
    """直接读数据集文件的 date、value 两列，到截止日之后的第一行即停；收盘价按两位小数四舍五入。"""
    rows: list[tuple[dt.date, Decimal]] = []
    with (ROOT / f"data/market/daily/{symbol}.csv").open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            day = dt.date.fromisoformat(row["date"])
            if day > CUTOFF:
                break
            rows.append((day, D(row["value"]).quantize(D("0.01"), rounding=ROUND_HALF_UP)))
    return rows


def _end_days(closes: list[tuple[dt.date, Decimal]], drop: Decimal, rebound: Decimal) -> set[dt.date]:
    """按规格第四节的 ZZ 规则另写一遍，只取事件结束日（回撤参考高点的重置日）。"""
    ends: set[dt.date] = set()
    peak = closes[0][1]
    low: Decimal | None = None
    for day, close in closes[1:]:
        if low is None:
            if close > peak:
                peak = close
            elif close <= peak * (1 - drop):
                low = close
        elif close < low:
            low = close
        elif close >= low * (1 + rebound):
            ends.add(day)
            peak, low = close, None
    return ends


def _rebuilt_increments(closes: list[Decimal], days: list[dt.date], ends: set[dt.date]) -> list[Decimal]:
    """规格第五节 ΔX 的逐日顺序：结束日或回到高点先重置；X = max(ln(0.98H/C_{j+1}), 0)；增量 = max(X − R, 0)。"""
    high, record = closes[0], D(0)
    result = []
    for index in range(len(closes) - 1):
        if days[index] in ends or closes[index] >= high:
            high, record = closes[index], D(0)
        following = closes[index + 1]
        level = D(0) if following / high >= D("0.98") else (D("0.98") * high / following).ln()
        result.append(max(level - record, D(0)))
        record = max(record, level)
    return result


def test_drawdown_increments_rebuilt_from_raw_closes_match_stored_daily_detail() -> None:
    """不信任输出文件里的增量：由数据集的原始收盘价与另算的重置日重建，再与逐日明细逐行比对。

    逐日明细里每个模型（选定设定与四条参照行）的回撤增量都应等于同一组重建值（增量只由价格决定）；
    被排除的区间（尾段未定）增量为 0，且全部位于窗口末尾，其后没有计入的区间。
    """
    with gzip.open(STORED / "daily_selected.csv.gz", "rt", encoding="utf-8", newline="") as file:
        stored = list(csv.DictReader(file))
    checked = 0
    for symbol in SYMBOLS:
        raw = _raw_closes(symbol)
        ends = _end_days(raw, *ZZ_LEVELS[symbol])
        by_day = dict(raw)
        models = sorted({row["model"] for row in stored})
        assert len(models) == 5
        for model in models:
            rows = [row for row in stored if (row["model"], row["symbol"]) == (model, symbol)]
            days = [dt.date.fromisoformat(row["date"]) for row in rows]
            assert days[0] == dt.date(2009, 12, 31) and days[-1] == CUTOFF and len(rows) == 1763
            # 明细里的收盘价就是数据集的原始收盘价。
            assert all(D(row["close"]) == by_day[day] for row, day in zip(rows, days, strict=True))
            rebuilt = _rebuilt_increments([by_day[day] for day in days], days, ends)
            excluded = [bool(row["excluded_reason"]) for row in rows[:-1]]
            first_excluded = excluded.index(True) if True in excluded else len(excluded)
            assert all(excluded[first_excluded:]) and first_excluded > 1700
            for row, value, skip in zip(rows[:-1], rebuilt, excluded, strict=True):
                if skip:
                    assert D(row["drawdown_increment"]) == 0
                else:
                    assert abs(D(row["drawdown_increment"]) - value) < D("1e-20")
                    checked += 1
    assert checked > 5 * 2 * 1700


# ---------------------------------------------------------------------------
# 补充 2：标签或截止日之后的价格改变时，截止日及之前的实时状态与损失不变
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def development() -> PreparedEvaluation:
    return prepare_v14(MODEL, load_development_inputs(ROOT, CUTOFF))


def test_changing_labels_never_changes_realtime_state(development: PreparedEvaluation) -> None:
    """标签只进入损失：把标签整体换掉（没有任何事件、没有未定区间），信号、执行灯色与暴露逐日不变，损失改变。"""
    prepared = development
    states = next(item for item in prepared.states
                  if (item.candidate.model, item.candidate.k, item.candidate.theta_p) == (FULL, 5, D("0.025")))
    events = {symbol: find_zz_events(symbol, prepared.inputs.days, prepared.inputs.series[symbol], CUTOFF,
                                     prepared.config.zz_thresholds()) for symbol in SYMBOLS}
    unknown = build_unknown_labels(events, prepared.inputs.days,
                                   {symbol: prepared.inputs.series[symbol] for symbol in SYMBOLS}, CUTOFF)
    empty = UnknownLabels(CUTOFF, {symbol: frozenset() for symbol in SYMBOLS}, {symbol: {} for symbol in SYMBOLS})
    real = evaluate_candidate(prepared, states, events, unknown)
    other = evaluate_candidate(prepared, states, {symbol: () for symbol in SYMBOLS}, empty)
    assert (real.signals, real.system_executed, real.status, real.active_channels) == (
        other.signals, other.system_executed, other.status, other.active_channels)
    assert all([row.exposure for row in real.executions[symbol]] == [row.exposure for row in other.executions[symbol]]
               for symbol in SYMBOLS)
    assert real.total_loss != other.total_loss


def _walk(count: int, tail: str | None = None) -> dict[dt.date, Decimal]:
    """构造的价格：先涨后跌再反弹的锯齿；tail 给定时把第 200 天之后全部换成这个极端值。"""
    prices = {}
    for index in range(count):
        swing = (index % 37) - 18
        prices[DAYS[index]] = D(1000 + 3 * index - 9 * abs(swing)) if tail is None or index <= 200 else D(tail)
    return prices


def test_prices_after_cutoff_change_nothing_up_to_cutoff() -> None:
    """截止日取第 200 天。截止日之后的价格换成极端值：

    截止日及之前的特征、以截止日为标签截止日的 ZZ 事件全部不变。
    """
    cutoff = DAYS[200]
    plain, extreme = _walk(300), _walk(300, "1")
    before = asset_features(DAYS, plain, {}, D("0.1"), FIXED)[:201]
    after = asset_features(DAYS, extreme, {}, D("0.1"), FIXED)[:201]
    assert before == after and before[-1].date == cutoff
    thresholds = {"SPX": (D("0.04"), D("0.05"))}
    assert find_zz_events("SPX", DAYS, plain, cutoff, thresholds) == find_zz_events("SPX", DAYS, extreme, cutoff,
                                                                                  thresholds)
    # 只传截止日及以前的价格，得到的也是同一组事件：标签生成没有用到截止日之后的任何价格。
    truncated = {day: value for day, value in plain.items() if day <= cutoff}
    assert find_zz_events("SPX", DAYS[:201], truncated, cutoff, thresholds) == find_zz_events(
        "SPX", DAYS, extreme, cutoff, thresholds)


def test_moving_the_label_cutoff_changes_only_the_open_tail_by_design() -> None:
    """标签是事后定义的：把标签截止日提前到事件中途，尚未走完的那一段由“危险”变为“未定”，这是登记的口径，不是泄漏。

    收盘 100、97、95、94、99.5、100：截止日取行号 3 时事件右截尾（暂定低点行号 3）；取行号 5 时事件已确认
    （低点行号 3、结束日行号 4）。两种截止日下，行号 0—2 都是危险区间；差别只在行号 3 之后。
    """
    closes = dict(zip(DAYS[:6], (D(100), D(97), D(95), D(94), D("99.5"), D(100)), strict=True))
    thresholds = {"SPX": (D("0.04"), D("0.05"))}
    (early,) = find_zz_events("SPX", DAYS[:4], closes, DAYS[3], thresholds)
    (late,) = find_zz_events("SPX", DAYS[:6], closes, DAYS[5], thresholds)
    assert early.right_censored and not late.right_censored
    assert (early.peak_date, early.t0_date, early.trough_date) == (late.peak_date, late.t0_date, late.trough_date)
    assert late.end_date == DAYS[4]


# ---------------------------------------------------------------------------
# 补充 3：缺失数据只引起已登记的状态变化
# ---------------------------------------------------------------------------

def test_one_missing_close_invalidates_exactly_the_registered_windows() -> None:
    """第 250 天缺一个收盘价（其余每天上涨 1）：

    63 日高点与回撤 D 在第 250—312 天缺失（窗口含缺价日），第 313 天恢复；
    200 日均线在第 250—449 天缺失，第 450 天恢复；
    NL 只在缺价当日未知（其后窗口内有缺价，但价格一直高于有效最小值，可以证明未创新低），Q 只清零一次；
    C_{t−5} 只在第 255 天缺失。没有任何别的量受影响。
    """
    days = tuple(dt.date(2000, 1, 1) + dt.timedelta(days=index) for index in range(500))
    prices: dict[dt.date, Decimal | None] = {day: D(100 + index) for index, day in enumerate(days)}
    full = asset_features(days, prices, {}, D("0.1"), FIXED)
    prices[days[250]] = None
    gap = asset_features(days, prices, {}, D("0.1"), FIXED)
    missing = lambda name: [index for index in range(500)  # noqa: E731
                            if getattr(gap[index], name) is None and getattr(full[index], name) is not None]
    assert missing("drawdown63") == list(range(250, 313)) == missing("high63")
    assert missing("ma200") == list(range(250, 450)) and missing("ma50") == list(range(250, 300))
    assert missing("new_low20") == [250] and missing("close_t5") == [255] and missing("close") == [250]
    assert gap[250].q == 0 and [gap[index].q for index in (251, 252, 260)] == [1, 2, 10]
    assert [index for index in range(500) if gap[index].q != full[index].q] == list(range(250, 500))


def test_missing_channel_input_keeps_state_and_never_moves_the_light_by_itself() -> None:
    """通道层：输入缺失时状态沿用、当日无效。系统层（规则 F）穷举：

    任取前一日灯色与三条通道（一黄两红）的状态，把其中一条通道标为“无效”（状态不变），
    当日灯色要么与全部有效时相同，要么停在前一日的灯色（降级被挡住）；缺值不会造成升级，也不会造成降级。
    """
    for status in ("armed", "active", "unarmed"):
        held = update_channel(DAYS[0], status, dataclasses.replace(  # type: ignore[arg-type]
            _predicate(), entry=None, exit=None, rearm=None))
        assert (held.status, held.valid) == (status, False)
    order = {"绿": 0, "黄": 1, "红": 2}
    levels = (("P_SPX", "黄"), ("PR_SPX", "红"), ("MR_SPX", "红"))
    for previous in order:
        for statuses in itertools.product(("armed", "active", "unarmed"), repeat=3):
            for q in (4, 5):
                valid = _light(previous, levels, statuses, (True, True, True), q)
                for broken in range(3):
                    flags = tuple(index != broken for index in range(3))
                    light = _light(previous, levels, statuses, flags, q)
                    assert light == valid or (light == previous and order[valid] < order[previous])


def _predicate() -> ChannelPredicate:
    return ChannelPredicate(True, True, True)


def _light(previous: str, levels: tuple[tuple[str, str], ...], statuses: tuple[str, ...],
           valid: tuple[bool, ...], q: int) -> str:
    channels = {name: (level, ChannelDay(DAYS[0], status, flag, ""))  # type: ignore[arg-type]
                for (name, level), status, flag in zip(levels, statuses, valid, strict=True)}
    inputs = ReadyInputs(q, q, None, None, None, None, None, True)
    return release_f_step(DAYS[0], SystemMemory(previous, 0, 0, 0), channels, inputs, 5).memory.light  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 补充 4：数据集清单的 kind 字段不参与波段预警的任何处理分支
# ---------------------------------------------------------------------------

def test_dataset_kind_is_not_read_by_wavewarn(tmp_path: Path) -> None:
    """清单里 SPX、NDX 的 kind 是 etf（指“带开高低收量的 Yahoo 价格序列”），S5TW、VIX_CBOE 是 value。

    波段预警只按列名读 date 与 value，不读清单、不看 kind：只有这两列的文件照样读取，结果与类别无关；
    导入波段预警的输入与评价模块不会带入数据集生成模块（kind 的分支只在那里）。
    """
    manifest = json.loads((ROOT / "data/market/manifest.json").read_text(encoding="utf-8"))["series"]
    assert {name: manifest[name]["kind"] for name in ("SPX", "NDX", "QQQ", "S5TW", "VIX_CBOE")} == {
        "SPX": "etf", "NDX": "etf", "QQQ": "etf", "S5TW": "value", "VIX_CBOE": "value"}
    path = tmp_path / "SPX.csv"
    path.write_text("date,value\n2016-12-29,2249.255\n2016-12-30,2238.83\n", encoding="utf-8")
    assert series_until(path, "value", CUTOFF, True, CUTOFF) == {
        dt.date(2016, 12, 29): D("2249.26"), CUTOFF: D("2238.83")}
    code = ("import importlib, json, sys\n"
            "for name in ('inputs', 'features', 'channels', 'state_machine', 'v14_model', 'evaluation', 'timing',"
            " 'labels_zz', 'loss', 'evaluation_v14_run', 'diagnostics_round2_run', 'extended_nav_run'):\n"
            "    importlib.import_module('market_risk.wavewarn.' + name)\n"
            "print(json.dumps([m for m in sys.modules if m.startswith('market_risk.data')]))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []


# ---------------------------------------------------------------------------
# 补充 5：截止日控制落实在读取、特征与标签的入口
# ---------------------------------------------------------------------------

def test_development_inputs_hold_nothing_after_the_cutoff(development: PreparedEvaluation) -> None:
    """读取入口：六个输入序列与交易日轴都止于 2016-12-30；特征与状态在这份输入上计算，内存里没有其后的数值。"""
    inputs = development.inputs
    assert inputs.days[-1] == CUTOFF and set(inputs.series) == {"SPX", "QQQ", "S5TW", "NDTW", "VIX", "VIX3M"}
    assert all(max(series) <= CUTOFF for series in inputs.series.values())
    assert all(states.rows[-1].date == CUTOFF for states in development.states)
    with pytest.raises(ValueError, match="开发期"):
        load_development_inputs(ROOT, dt.date(2017, 1, 3))


def test_label_generation_stops_at_the_cutoff_and_rejects_a_longer_axis() -> None:
    """标签入口：find_zz_events 到截止日为止；未定集合要求截止日就是价格轴的最后一天。"""
    prices = _walk(300)
    events = find_zz_events("SPX", DAYS, prices, DAYS[200], {"SPX": (D("0.04"), D("0.05"))})
    assert all(max(event.peak_date, event.t0_date, event.trough_date, event.end_date or event.trough_date)
               <= DAYS[200] for event in events)
    with pytest.raises(ValueError, match="截止日"):
        build_unknown_labels({"SPX": events}, DAYS[:150], {"SPX": prices}, DAYS[200])


def test_label_file_reader_rejects_any_date_after_the_cutoff(tmp_path: Path) -> None:
    """读回已导出的标签文件时，任何一个日期列晚于标签截止日都应拒绝，而不只是高点日期。

    审计时这是入口防护缺口 G-1（只核对高点日期），本案例当时标记为预期失败；补丁 f_label 之后必须通过。
    高点都不晚于截止日 2016-12-30；分别让低点与结束日、只有结束日、只有 T0 之后的各列晚于截止日，都应拒绝。
    全部日期列都不晚于截止日的一行（含结束日为空的右截尾事件）照常读入。
    """
    header = "symbol,peak_date,t0_date,trough_date,end_date,peak_close,trough_close,right_censored\n"
    path = tmp_path / "zz_events.csv"
    late = ("SPX,2016-12-13,2016-12-28,2017-01-05,2017-01-20,100,95,否\n",
            "SPX,2016-12-13,2016-12-20,2016-12-28,2017-01-03,100,95,否\n",
            "SPX,2016-12-13,2017-01-03,2017-01-03,,100,95,是\n",
            "SPX,2017-01-03,2017-01-04,2017-01-05,,100,95,是\n")
    for row in late:
        path.write_text(header + row, encoding="utf-8")
        with pytest.raises(ValueError, match="截止日"):
            read_zz_events(path, CUTOFF)
    path.write_text(header + "SPX,2016-12-13,2016-12-20,2016-12-28,2016-12-30,100,95,否\n"
                    "QQQ,2016-12-13,2016-12-28,2016-12-30,,100,94,是\n", encoding="utf-8")
    events = read_zz_events(path, CUTOFF)
    assert (events["SPX"][0].end_date, events["QQQ"][0].right_censored) == (CUTOFF, True)
    # 现有的开发期标签文件照常读入：224 个事件。
    stored = read_zz_events(ROOT / "reports/research/wavewarn_v121/zz_events_development.csv", CUTOFF)
    assert len(stored["SPX"]) + len(stored["QQQ"]) == 224


def test_stored_development_label_file_has_no_date_after_the_cutoff() -> None:
    """现有的开发期标签文件里，全部日期列都不晚于 2016-12-30：上面的缺口没有影响任何已有输出。"""
    path = ROOT / "reports/research/wavewarn_v121/zz_events_development.csv"
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    dates = [row[name] for row in rows for name in ("peak_date", "t0_date", "trough_date", "end_date") if row[name]]
    assert len(rows) == 224 and max(dates) <= CUTOFF.isoformat()


# ---------------------------------------------------------------------------
# 补充 6：BW 偏差的影响分四层
# ---------------------------------------------------------------------------

def _bw_exits(features: tuple[AssetFeatures, ...], spec: bool) -> list[bool | None]:
    """两种写法各自另写一遍：spec 为规格原文；否则为审计时生产代码的写法（价格条件也连续 3 日）。"""
    breadth = [None if item.breadth is None else item.breadth > 40 for item in features]
    price = [None if item.close is None or item.close_t5 is None else item.close > item.close_t5
             for item in features]
    both = [None if a is None or b is None else a and b for a, b in zip(breadth, price, strict=True)]
    result: list[bool | None] = []
    for index in range(len(features)):
        if spec:
            streak = None if breadth[index] is None else index >= 2 and all(
                value is True for value in breadth[index - 2:index + 1])
            result.append(None if streak is None or price[index] is None else streak and price[index])
        else:
            result.append(None if both[index] is None else index >= 2 and all(
                value is True for value in both[index - 2:index + 1]))
    return result


@dataclasses.dataclass(frozen=True)
class _Layers:
    days: tuple[dt.date, ...]
    start: int                                   # t0 的行号
    exits: dict[tuple[str, bool], list[bool | None]]
    spx: tuple[AssetFeatures, ...]
    qqq: tuple[AssetFeatures, ...]
    ratios: tuple[Decimal | None, ...]
    t0: dt.date


@pytest.fixture(scope="module")
def layers() -> _Layers:
    inputs: DevelopmentInputs = load_development_inputs(ROOT, CUTOFF)
    by_q, ratios = grid_features(MODEL.base, inputs)
    spx, qqq = by_q[MODEL.base.candidate_sets().q[0]]
    t0 = first_complete_day(inputs.days, spx, qqq, inputs.series["VIX"], inputs.series["VIX3M"], FIXED)
    exits = {(symbol, spec): _bw_exits(tuple(features), spec)
             for symbol, features in (("SPX", spx), ("QQQ", qqq)) for spec in (False, True)}
    return _Layers(inputs.days, inputs.days.index(t0), exits, tuple(spx), tuple(qqq), tuple(ratios), t0)


def _channels(data: _Layers, k: int, theta: str, fixed_sides: frozenset[str]) -> tuple[dict, object]:
    candidate = next(item for item in v14_grid(MODEL, (FULL,), 0) if (item.k, item.theta_p) == (k, D(theta)))
    channels = dict(v14_channels(FULL, data.spx, data.qqq, data.ratios, candidate, FIXED, MODEL.mr_window))
    for symbol in SYMBOLS:
        level, predicates = channels[f"BW_{symbol}"]
        exits = data.exits[symbol, symbol in fixed_sides]
        channels[f"BW_{symbol}"] = (level, tuple(dataclasses.replace(item, exit=value)
                                                 for item, value in zip(predicates, exits, strict=True)))
    return channels, candidate


def _lights(data: _Layers, k: int, theta: str, fixed_sides: frozenset[str]) -> list[str]:
    channels, candidate = _channels(data, k, theta, fixed_sides)
    rows = release_f_sequence(data.days, channels, release_f_inputs(data.spx, data.qqq), data.t0,
                              candidate, FIXED)  # type: ignore[arg-type]
    return [row.light for row in rows]


def test_bw_deviation_layer_one_and_two_predicate_and_channel_state(layers: _Layers) -> None:
    """谓词层与通道层（与 K、θ_P 无关），评价窗口为 t0 之后第 63 个交易日（2009-12-31）起。

    谓词层：两种写法的退出谓词取值不同的天数（不论通道是否激活）——SPX 191 天（窗口内 181），QQQ 168 天（156）。
    其中只有通道正处于激活时才起作用：沿现行路径“前一日激活”的有 SPX 19 天（18）、QQQ 20 天（18）。
    通道层：BW 通道状态不同的天数——SPX 44 天（42），QQQ 42 天（39）；激活天数 SPX 519→488，QQQ 565→537。
    每一处差异都是规格写法为真、现行写法为假，没有反方向。
    """
    data, window = layers, layers.start + 63
    assert data.days[window] == dt.date(2009, 12, 31)
    expected = {"SPX": (191, 181, 19, 18, 44, 42, 519, 488), "QQQ": (168, 156, 20, 18, 42, 39, 565, 537)}
    for symbol in SYMBOLS:
        old, new = data.exits[symbol, False], data.exits[symbol, True]
        differ = [index for index in range(data.start + 1, len(data.days)) if old[index] != new[index]]
        assert all(new[index] is True and old[index] is False for index in differ)
        paths = {}
        for sides in (frozenset(), frozenset({symbol})):
            _, predicates = _channels(data, 5, "0.025", sides)[0][f"BW_{symbol}"]
            paths[bool(sides)] = [row.status for row in run_channel(data.days[data.start + 1:],
                                                                    predicates[data.start + 1:])]
        active_days = [index for index in differ if paths[False][index - data.start - 2] == "active"
                       ] if data.start + 2 <= differ[0] else []
        state = [offset for offset, (a, b) in enumerate(zip(paths[False], paths[True], strict=True)) if a != b]
        counts = (len(differ), sum(index >= window for index in differ), len(active_days),
                  sum(index >= window for index in active_days), len(state),
                  sum(offset + data.start + 1 >= window for offset in state),
                  paths[False].count("active"), paths[True].count("active"))
        assert counts == expected[symbol]


def test_bw_deviation_layer_three_and_four_system_light_and_executed_exposure(layers: _Layers) -> None:
    """系统层与执行层（选定设定 K=5、θ_P=2.5%），分别只改 SPX 一侧、只改 QQQ 一侧、两侧都改。

    系统层：系统灯色（信号）不同的天数，自 t0 次日起／其中 2009-12-31 起——只改 SPX 8／8，只改 QQQ 5／3，两侧 22／19。
    执行层：评价窗口 1762 个区间里执行灯色不同的区间数——8、3、19；执行暴露之差的绝对值合计 5.0、1.5、10.5。
    两侧各自的影响不能相加（8 + 3 ≠ 19）：两个资产的 BW 同为红灯通道，一侧提前退出后另一侧是否仍压着红灯决定了系统灯色。
    九组设定两侧都改时，执行灯色不同的区间数为 8 至 21。
    """
    data = layers
    level = {"绿": D(1), "黄": D("0.5"), "红": D(0)}

    def executed(lights: list[str]) -> list[str]:
        return ["绿", *lights[:-1]][63:][:-1]

    base = _lights(data, 5, "0.025", frozenset())
    expected = {frozenset({"SPX"}): (8, 8, 8, D("5.0")), frozenset({"QQQ"}): (5, 3, 3, D("1.5")),
                frozenset(SYMBOLS): (22, 19, 19, D("10.5"))}
    for sides, counts in expected.items():
        lights = _lights(data, 5, "0.025", sides)
        pairs = list(zip(executed(base), executed(lights), strict=True))
        assert len(pairs) == 1762
        assert (sum(a != b for a, b in zip(base, lights, strict=True)),
                sum(a != b for a, b in zip(base[63:], lights[63:], strict=True)),
                sum(a != b for a, b in pairs), sum((abs(level[a] - level[b]) for a, b in pairs), D(0))) == counts
    # 现行写法下的平均执行暴露等于已入库结果的 ē = 0.546822；规格写法下为 0.552781。
    mean = lambda lights: sum((level[item] for item in executed(lights)), D(0)) / 1762  # noqa: E731
    assert (round(mean(base), 6), round(mean(_lights(data, 5, "0.025", frozenset(SYMBOLS))), 6)) == (
        D("0.546822"), D("0.552781"))
    spread = []
    for k in (3, 5, 10):
        for theta in ("0.015", "0.02", "0.025"):
            old, new = _lights(data, k, theta, frozenset()), _lights(data, k, theta, frozenset(SYMBOLS))
            spread.append(sum(a != b for a, b in zip(executed(old), executed(new), strict=True)))
    assert spread == [17, 21, 21, 17, 19, 19, 8, 10, 10]


def _stored_executed(folder: Path) -> list[str]:
    with gzip.open(folder / "daily_selected.csv.gz", "rt", encoding="utf-8", newline="") as file:
        return [row["system_executed_light"] for row in csv.DictReader(io.StringIO(file.read()))
                if (row["model"], row["symbol"]) == ("v1.4", "SPX")]


def test_stored_selected_setting_matches_the_legacy_formula(layers: _Layers) -> None:
    """原实现写出的开发期逐日明细（选定设定，现存于 superseded/）的执行灯色，逐日等于“审计时写法”重算的结果：

    四层统计的基准就是原实现的输出。
    """
    base = _lights(layers, 5, "0.025", frozenset())
    assert _stored_executed(STORED_ASRUN) == ["绿", *base[:-1]][63:]
    assert configured_loss_settings(MODEL.base).parameters.eta == D("0.5")


def test_stored_corrected_selected_setting_matches_the_spec_formula(layers: _Layers) -> None:
    """机械重跑后入库的逐日明细（重选的设定仍是 K=5、θ_P=2.5%）的执行灯色，逐日等于“规格写法”另算的结果，

    与原实现的输出相差 19 个区间（与四层统计的执行层一致）。
    """
    fixed = _lights(layers, 5, "0.025", frozenset(SYMBOLS))
    stored = _stored_executed(STORED)
    assert stored == ["绿", *fixed[:-1]][63:]
    assert sum(a != b for a, b in zip(stored[:-1], _stored_executed(STORED_ASRUN)[:-1], strict=True)) == 19
