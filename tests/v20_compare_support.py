"""v2.0 构造比对接线（A，乙方案范围）的适配函数：不含任何信号或交易规则。

依据：《接线会话指令：构造比对接线（A）定稿》、《补充三条》与《乙方案范围调整》（以后者为准）。职责只有：
(a) 场景 JSON → 构造 CSV 与配置（仓库外的专用临时目录）；
(b) CSV 入口路径（read_until，预期的入口异常在场景记录边界捕获并结束该路径）与快照适配路径（前置验证复用已验收函数）；
(c) 项目各模块的结果 → 与工具同构的字段字典（只调用已有函数，只做登记的表示换算与窗口对齐）；
(d) “无法确定”字段的语义换算（定稿第七节）；
(e) 精确、容差与结论优先的比对与记录。
不比对、不组装：R2、提示段账、R1 资格判断、选择程序、确认性检验（项目尚缺组合层）；
主参照执行政策模拟（all_valid 待裁决）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from wavewarn_v20_helpers import POLICY, POSITIONS, TOLERANCE, WINDOWS

from market_risk.calendar import stock_trading_days
from market_risk.storage.paths import StoragePaths
from market_risk.wavewarn_v20 import config_v20, confirmatory, data_v20, nav, r1, research_run
from market_risk.wavewarn_v20.confirmatory import stationary_bootstrap_indices
from market_risk.wavewarn_v20.convergence import (
    CHANNEL_NAMES,
    REGISTERED_CHANNELS,
    REGISTERED_REFERENCE,
    REGISTERED_SYSTEM,
    Candidate,
    ConvergenceError,
    EmptyWindowError,
    candidate_convergence,
    common_start_index,
    first_valid_index,
    reference_convergence,
)
from market_risk.wavewarn_v20.execution import (
    Position,
    TargetSource,
    position_of,
    reference_signal_records,
    signal_records,
    signal_targets,
    switches,
)
from market_risk.wavewarn_v20.inputs import below_average, drawdown_reaches, snapshot_asset_days, snapshot_trend_days
from market_risk.wavewarn_v20.labels_r2 import R2Thresholds
from market_risk.wavewarn_v20.r2 import R2Rule
from market_risk.wavewarn_v20.reference import run_reference
from market_risk.wavewarn_v20.selection import registered_candidates
from market_risk.wavewarn_v20.snapshot import Snapshot, make_snapshot
from market_risk.wavewarn_v20.state_machine import (
    Risk,
    SystemState,
    direct_level2_days,
    evidence_series,
    level1_release_checks,
    run_channels,
    run_system,
    transitions,
)

# ---------------------------------------------------------------------------
# 登记值与表示换算（只换算表示方式，不新增推导）
# ---------------------------------------------------------------------------

ASSETS = ("SPX", "QQQ")
# 登记的 27 组：先 K、再 θ_P、再 h（登记第七节；selection.registered_candidates 的登记顺序）。
CANDIDATES = registered_candidates((3, 5, 10), (Decimal("0.015"), Decimal("0.02"), Decimal("0.025")), (1, 3, 5))
COMMON_START_OFFSET = 63            # j₀ = max(t0 + 63, κ_全 + 1)（登记第四节；convergence.common_start_index 的说明）
ACQUIRED_AT = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)       # 构造快照的取得时间：只作快照字段，不参与任何计算
ABS_TOL = 1e-12                      # 登记容差：math.isclose(rel_tol=0.0, abs_tol=1e-12)
RISK_NAMES = {Risk.NORMAL: "正常", Risk.LEVEL1: "一级", Risk.LEVEL2: "二级"}
SOURCE_NAMES = {TargetSource.SYSTEM: "系统", TargetSource.STOP_CASH: "止损", TargetSource.OUT_CASH: "现金",
                TargetSource.REENTRY_CAP: "重入"}
# 固定的裁定条目（定稿第五节第 1 条）：复用 test_wavewarn_v20_config_entry.py 中 DECISIONS 前两条（correct、exclude）
# 的已验收结构，日期改为 2030-01-02、2030-01-03，decided_on 改为 2030-01-05。
DECISIONS_TEXT = """\
- date: 2030-01-02
  symbol: QQQ
  decision: correct
  reason: 构造的修正条目
  decided_on: 2030-01-05
  corrected_value: 43.31
  evidence_source: 构造的证据
- date: 2030-01-03
  symbol: SPX
  decision: exclude
  reason: 构造
  decided_on: 2030-01-05
"""
DECISION_DATES = (dt.date(2030, 1, 2), dt.date(2030, 1, 3))


def group_key(candidate: Candidate) -> str:
    """工具的组名写法：K=5,θ=0.02,h=3。"""
    return f"K={candidate.k},θ={candidate.theta},h={candidate.h}"


def day_text(day: dt.date) -> str:
    return day.isoformat()


def close(first: float, second: float) -> bool:
    return math.isclose(first, second, rel_tol=0.0, abs_tol=ABS_TOL)


# ---------------------------------------------------------------------------
# 文件读写（只在仓库外的专用目录与给定的场景、输出目录中）
# ---------------------------------------------------------------------------


def get_bytes(path: Path) -> bytes:
    """读取场景、manifest 或工具输出文件的全部字节。"""
    return path.read_bytes()


def put_bytes(path: Path, data: bytes) -> Path:
    """把字节写进给定路径（必要时建立上级目录）；只用于构造目录与比对输出目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_json(data: bytes) -> dict:
    """价格等十进制数按原文读成 Decimal，不经过 float。"""
    return json.loads(data.decode("utf-8"), parse_float=Decimal)


def load_json(path: Path) -> dict:
    return parse_json(get_bytes(path))


def dump_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=1, default=str).encode("utf-8")


# ---------------------------------------------------------------------------
# (a) 场景 → 构造 CSV 与配置
# ---------------------------------------------------------------------------


def scenario_cutoff(scenario: Mapping) -> str:
    """场景截止日：给出时取 cutoff，省略时取轴末日（工具 README 第三节）。"""
    return scenario.get("cutoff") or scenario["axis"][-1]


def csv_bytes(scenario: Mapping, asset: str) -> bytes:
    """表头 date,value,source；轴上全部日期逐行写入（含截止日之后的行）；缺价行 value 为空；source 固定 yahoo。"""
    rows = ["date,value,source"]
    for day, price in zip(scenario["axis"], scenario["prices"][asset], strict=True):
        rows.append(f"{day},{'' if price is None else price},yahoo")
    return ("\n".join(rows) + "\n").encode("utf-8")


def dataset_yaml(metadata: Mapping[str, data_v20.FileMetadata]) -> bytes:
    """wavewarn_v20.yaml：登记值由 data_v20.file_metadata 对构造文件计算后写入。"""
    lines = ["dataset:"]
    for asset in ASSETS:
        item = metadata[asset]
        lines += [f"  {asset}:", f"    raw_sha256: {item.raw_sha256}",
                  f"    normalized_sha256: {item.normalized_sha256}",
                  f"    data_rows: {item.data_rows}", f"    first_date: {item.first_date}",
                  f"    last_date: {item.last_date}"]
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ConstructedInputs:
    root: Path
    csv: Mapping[str, bytes]
    hashes: Mapping[str, str]          # 文件相对路径 → SHA-256
    metadata_error: str | None         # file_metadata 对构造文件报错时的原因（此时没有写配置）


def construct(scenario: Mapping, root: Path) -> ConstructedInputs:
    """按仓库布局写构造 CSV 与两份配置；路径由 StoragePaths 与 config_v20 的登记文件名生成。"""
    paths = StoragePaths(root)
    data = {asset: csv_bytes(scenario, asset) for asset in ASSETS}
    hashes = {}
    for asset in ASSETS:
        target = put_bytes(paths.market_daily_file(asset), data[asset])
        hashes[target.relative_to(root).as_posix()] = sha256(data[asset])
    try:
        metadata = {asset: data_v20.file_metadata(data[asset]) for asset in ASSETS}
    except data_v20.DataEntryError as error:
        return ConstructedInputs(root, data, hashes, f"{type(error).__name__}：{error}")
    for name, content in ((config_v20.DATASET_FILE, dataset_yaml(metadata)),
                          (config_v20.DECISIONS_FILE, DECISIONS_TEXT.encode("utf-8"))):
        target = put_bytes(root.joinpath(*name), content)
        hashes[target.relative_to(root).as_posix()] = sha256(content)
    return ConstructedInputs(root, data, hashes, None)


# ---------------------------------------------------------------------------
# (b) CSV 入口路径与快照适配路径
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryResult:
    """CSV 入口路径的实际结果。预期的入口异常在场景记录边界捕获，并结束该路径。"""

    snapshot: Snapshot | None
    stop_class: str | None
    stop_reason: str | None
    message: str | None
    decision_hits: Mapping[str, int]


def csv_entry(constructed: ConstructedInputs, cutoff: dt.date) -> EntryResult:
    if constructed.metadata_error is not None:
        return EntryResult(None, "DataInputError", data_v20.REASON_INVALID_INPUT, constructed.metadata_error, {})
    config = config_v20.load_v20_config(constructed.root)
    # 固定裁定条目的日期都晚于截止日，check_corrections 只取截止日以内的条目：实际命中条目数为 0（逐资产记录）。
    hits = {asset: sum(1 for entry in config.decisions if entry.symbol == asset and entry.date <= cutoff)
            for asset in ASSETS}
    paths = StoragePaths(constructed.root)
    series = {}
    try:
        for asset in ASSETS:
            series[asset] = data_v20.read_until(paths.market_daily_file(asset), asset, cutoff,
                                                config.registered[asset], config.decisions)
    except data_v20.DataEntryError as error:
        return EntryResult(None, type(error).__name__, error.reason, str(error), hits)
    assembled = data_v20.assemble_snapshot(series, cutoff, ACQUIRED_AT)
    return EntryResult(assembled.snapshot, None, None, None, hits)


@dataclass(frozen=True)
class Adaptation:
    """快照适配路径：截止日以内前缀的逐项验证结果，以及通过后构造的快照 S₂。"""

    axis: tuple[str, ...]                           # 截止日以内的原始日期文字（含重复）
    checks: Mapping[str, str]                       # 验证项 → “通过” 或失败原因
    missing: Mapping[str, tuple[str, ...]]          # 资产 → 缺价日
    snapshot: Snapshot | None


def adapt(scenario: Mapping, constructed: ConstructedInputs, cutoff_text: str) -> Adaptation:
    """直接构造快照之前，用已验收函数逐项验证截止日前缀；任一不符即停止，不进入算法层（定稿第五节第 2 条）。"""
    axis = list(scenario["axis"])
    end = axis.index(cutoff_text) + 1                  # 截止日在轴上第一次出现的位置
    prefix = axis[:end]
    checks: dict[str, str] = {}
    try:
        # 日期词法与合法性：data_v20._date_field（ASCII YYYY-MM-DD 的 fullmatch 词法与真实日期，补充三条第三条）。
        days = [data_v20._date_field(f"{text},".encode()) for text in prefix]
        checks["日期词法与合法性"] = "通过"
    except data_v20.DataEntryError as error:
        checks["日期词法与合法性"] = f"{type(error).__name__}：{error}"
        return Adaptation(tuple(prefix), checks, {}, None)
    closes: dict[str, dict[dt.date, Decimal]] = {}
    missing: dict[str, tuple[str, ...]] = {}
    for asset in ASSETS:
        values = scenario["prices"][asset][:end]
        try:
            data_v20.check_trading_axis(asset, days[0], days[-1], days)
            checks[f"{asset} 交易日轴"] = "通过"
        except data_v20.DataEntryError as error:
            checks[f"{asset} 交易日轴"] = f"{type(error).__name__}（{error.reason}）：{error}"
            return Adaptation(tuple(prefix), checks, missing, None)
        try:
            closes[asset] = {day: data_v20.parse_price(str(value)) for day, value in zip(days, values, strict=True)
                             if value is not None}
            data_v20.parse_source("yahoo")
            checks[f"{asset} 价格词法与精度、来源"] = "通过"
        except data_v20.DataEntryError as error:
            checks[f"{asset} 价格词法与精度、来源"] = f"{type(error).__name__}：{error}"
            return Adaptation(tuple(prefix), checks, missing, None)
        missing[asset] = tuple(day_text(day) for day, value in zip(days, values, strict=True) if value is None)
    try:
        # 顺序与重复：make_snapshot 的严格升序检查。
        snapshot = make_snapshot(days, closes, days[-1], ACQUIRED_AT,
                                 {asset: sha256(constructed.csv[asset]) for asset in ASSETS})
        checks["日期顺序与重复（make_snapshot）"] = "通过"
    except ValueError as error:
        checks["日期顺序与重复（make_snapshot）"] = f"{type(error).__name__}：{error}"
        return Adaptation(tuple(prefix), checks, missing, None)
    return Adaptation(tuple(prefix), checks, missing, snapshot)


@dataclass(frozen=True)
class InputFacts:
    """输入层比对所需的事实：CSV 入口的停止类与原因、适配路径的前缀轴、验证项、缺价日，以及是否构造出 S₂。"""

    stop_class: str | None
    stop_reason: str | None
    axis: tuple[str, ...]
    checks: Mapping[str, str]
    missing: Mapping[str, tuple[str, ...]]
    snapshot_built: bool


def input_facts(entry: EntryResult, adaptation: Adaptation) -> InputFacts:
    return InputFacts(entry.stop_class, entry.stop_reason, adaptation.axis, dict(adaptation.checks),
                      {asset: tuple(days) for asset, days in adaptation.missing.items()},
                      adaptation.snapshot is not None)


SNAPSHOT_CHECK = "日期顺序与重复（make_snapshot）"


def saved_input_facts(scenario: Mapping, entry_info: Mapping, cutoff_text: str) -> InputFacts:
    """重比较（乙补修第三节）：由第二次运行保存的“入口与适配结果”还原输入层事实，不重新运行入口与适配路径。
    前缀轴按 adapt 的同一规则从场景切出；adapt 只在最后一项验证（make_snapshot）通过时构造 S₂。"""
    axis = list(scenario["axis"])
    entry = entry_info["CSV 入口实际结果"]
    checks = dict(entry_info["适配路径验证"])
    return InputFacts(entry["class"], entry["reason"], tuple(axis[:axis.index(cutoff_text) + 1]), checks,
                      {asset: tuple(days) for asset, days in entry_info["适配路径缺价日"].items()},
                      checks.get(SNAPSHOT_CHECK) == "通过")


def snapshot_fields(snapshot: Snapshot) -> dict:
    return {"day": day_text(snapshot.day), "days": [day_text(day) for day in snapshot.days],
            "closes": {asset: {day_text(day): str(value) for day, value in sorted(snapshot.closes[asset].items())}
                       for asset in sorted(snapshot.closes)},
            "acquired_at": snapshot.acquired_at.isoformat(),
            "source_hashes": dict(sorted(snapshot.source_hashes.items()))}


# ---------------------------------------------------------------------------
# (c) 项目结果 → 与工具同构的字段（只调用已有函数）
# ---------------------------------------------------------------------------


def exposure_of(position: Position) -> float:
    return POSITIONS.exposure(position)


def target_fields(target, source_name: str | None = None) -> dict:
    return {"exec_idx": day_text(target.day), "exposure": exposure_of(target.position), "core_w": target.core,
            "lev_w": target.leverage, "source": source_name or SOURCE_NAMES[target.source],
            "cap_active": target.cap_active, "reentry_cap": target.source is TargetSource.REENTRY_CAP}


def switch_fields(items) -> list[dict]:
    return [{"exec_idx": day_text(item.day), "from": exposure_of(item.before), "to": exposure_of(item.after),
             "delta": item.exposure_magnitude, "delta_signed": item.exposure_change, "stages": item.stages}
            for item in items]


def nav_fields(result: nav.NavResult, basket: Sequence[float]) -> dict:
    days = [{"idx": day_text(item.day), "W": item.wealth} for item in result.executions]
    for index in range(1, len(days)):
        days[index]["U"], days[index]["R"] = basket[index - 1], result.returns[index - 1]
    wealth = [item.wealth for item in result.executions]
    return {"nav": days, "summary": {"W_end": wealth[-1], "lnW_end": result.log_wealth, "sum_log": result.log_sum,
                                     "recon_ok": True, "mdd": r1.max_drawdown(wealth)}}


def signal_simulation(window: Sequence[dt.date], signals, closes: Mapping[str, list]) -> dict:
    """信号模拟：计划目标、切换，以及净值（窗口内有缺价时为“无法计算”，列出缺失日期）。"""
    targets = signal_targets(window, signals, POSITIONS)
    plan = []
    for index, (target, record) in enumerate(zip(targets, signals, strict=True)):
        # 登记的表示：首个执行日的目标为“初始建仓”（switches 不计初始建仓）；其余为系统目标。
        item = target_fields(target, "初始建仓" if index == 0 else None)
        item.update({"signal_idx": day_text(record.day), "S": RISK_NAMES[record.risk]})
        plan.append(item)
    result: dict = {"plan": plan, "switches": len(switches(targets, POSITIONS)),
                    "switch_list": switch_fields(switches(targets, POSITIONS)), "failed": None, "nav": None,
                    "summary": None}
    try:
        basket = nav.basket_returns(window, closes)
    except nav.NavUnavailable as error:
        result["failed"] = {"type": "缺价", "missing": [[asset, day_text(day)] for asset, day in error.missing]}
        return result
    try:
        result.update(nav_fields(nav.simulate_targets(targets, basket, POSITIONS, TOLERANCE), basket))
    except nav.NavError as error:
        result["failed"] = {"type": "计算失败", "class": type(error).__name__, "message": str(error)}
    return result


def policy_simulation(window: Sequence[dt.date], signals, closes: Mapping[str, list]) -> dict:
    """候选的执行政策研究模拟：simulate_policy_with_gaps（价格完整时即 simulate_policy）。"""
    try:
        result = nav.simulate_policy_with_gaps(window, signals, closes, POSITIONS, POLICY, TOLERANCE)
    except nav.NavError as error:
        return {"failed": {"type": "计算失败", "class": type(error).__name__, "message": str(error)}}
    if isinstance(result, nav.PolicyResult):
        basket = nav.basket_returns(window, closes)
        fields = nav_fields(result.nav, basket)
        return {"complete": True, "targets": [target_fields(item) for item in result.targets],
                "days": fields["nav"], "summary": fields["summary"], "undetermined": None, "missing": [],
                "switches": len(switches(result.targets, POSITIONS)),
                "switch_list": switch_fields(switches(result.targets, POSITIONS)), "failed": None}
    basket = nav.basket_prefix(window, closes).values
    days = [{"idx": day_text(item.day), "W": item.wealth} for item in result.executions]
    for index in range(1, len(days)):
        days[index]["U"], days[index]["R"] = basket[index - 1], result.returns[index - 1]
    undetermined = None if result.undetermined is None else {
        "day": day_text(result.undetermined.day), "reason": result.undetermined.reason}
    return {"complete": False, "targets": [target_fields(item) for item in result.targets], "days": days,
            "summary": None, "undetermined": undetermined,
            "missing": [[asset, day_text(day)] for asset, day in result.missing],
            "switches": len(switches(result.targets, POSITIONS)),
            "switch_list": switch_fields(switches(result.targets, POSITIONS)), "failed": None}


def window_alignment(days: Sequence[dt.date], j0: int, records_by_day: Mapping[dt.date, object]) -> tuple[list, list]:
    """窗口对齐（乙方案第一节第 3 条）：收盘日期切片为 j₀ 至末日（n + 1 日，对应 n 个收益区间）；
    信号切片按 nav 的接口契约 signals[i + 1].day == days[i]、signals[0] 为 j₀ 的前一信号日，逐日期断言。"""
    window = list(days[j0:])
    signals = [records_by_day[days[j0 - 1]], *(records_by_day[day] for day in window[:-1])]
    assert signals[0].day == days[j0 - 1], "信号切片首项须为 j₀ 的前一信号日"
    for index in range(len(window) - 1):
        assert signals[index + 1].day == window[index], f"信号切片与执行日不对齐：{index}"
    return window, signals


def project_full(snapshot: Snapshot) -> dict:
    """一个 full 场景的项目字段：输入派生量、27 组通道与状态机、收敛、主参照、信号模拟、候选执行政策研究模拟、
    一直持有；以及窗口对齐的依据与首末日期。预期的停止（t0 不存在、未收敛、评价窗口为空）记为 stop。"""
    days = list(snapshot.days)
    spx, qqq = (snapshot_asset_days(snapshot, asset, WINDOWS) for asset in ASSETS)
    ma = snapshot_trend_days(snapshot, "SPX", WINDOWS)
    thetas = sorted({candidate.theta for candidate in CANDIDATES})
    inputs = {}
    for asset, items in (("SPX", spx), ("QQQ", qqq)):
        inputs[asset] = [{
            "day": day_text(item.day), "has_close": item.close is not None, "h_complete": item.high_complete,
            "h": None if item.high is None else str(item.high), "nl": item.new_low.value, "q": item.q,
            # 回撤门槛只在收盘价与高点都存在时由 drawdown_reaches 给出；缺价日项目没有定义（不补规则）。
            "reach": None if item.close is None or item.high is None else {
                str(theta): [drawdown_reaches(item.close, item.high, theta),
                             drawdown_reaches(item.close, item.high, 2 * theta)] for theta in thetas},
        } for item in items]
    inputs["SPX_MA"] = [{
        "day": day_text(item.day), "ma_complete": item.complete,
        "ma_sum": None if item.total is None else str(item.total),
        "below_ma": below_average(item.close, item.total, WINDOWS.average)
        if item.complete and item.close is not None else None} for item in ma]
    result: dict = {"inputs": inputs, "stop": None, "E": day_text(days[-1]), "E_index": len(days) - 1}
    try:
        t0 = first_valid_index(spx, qqq, ma)
    except ConvergenceError as error:
        result["stop"] = {"class": type(error).__name__, "reason": error.reason, "message": str(error)}
        return result
    result["t0"] = day_text(days[t0])
    after = slice(t0 + 1, None)
    groups: dict = {}
    system_indices = []
    records: dict = {}
    for candidate in CANDIDATES:
        key = group_key(candidate)
        paths = run_channels(REGISTERED_CHANNELS, spx[after], qqq[after], ma[after], candidate.k, candidate.theta,
                             WINDOWS.average)
        evidence = evidence_series(spx[after], qqq[after], ma[after], paths)
        system = run_system(REGISTERED_SYSTEM, evidence, candidate.k, candidate.h)
        moves = {day: (before, after_) for day, before, after_ in transitions(REGISTERED_SYSTEM.risk, system)}
        direct = set(direct_level2_days(REGISTERED_SYSTEM.risk, system))
        init = {"day": day_text(days[t0]), "idx": t0, "init": True,
                "ch": dict.fromkeys(CHANNEL_NAMES, REGISTERED_CHANNELS.p_spx.value),
                "S": RISK_NAMES[REGISTERED_SYSTEM.risk], "c1": REGISTERED_SYSTEM.c1, "c2": REGISTERED_SYSTEM.c2}
        assert {getattr(REGISTERED_CHANNELS, name) for name in ("p_spx", "p_qqq", "pr_spx", "pr_qqq", "mr")} == {
            REGISTERED_CHANNELS.p_spx}
        group_days = [init]
        channel_lists = (paths.p_spx, paths.p_qqq, paths.pr_spx, paths.pr_qqq, paths.mr)
        for position, (item, proof) in enumerate(zip(system, evidence, strict=True)):
            states = {name: path[position] for name, path in zip(CHANNEL_NAMES, channel_lists, strict=True)}
            move = moves.get(item.day)
            group_days.append({
                "day": day_text(item.day), "idx": t0 + 1 + position, "init": False,
                "ch": {name: state.state.value for name, state in states.items()},
                "valid": {name: state.valid for name, state in states.items()}, "all_valid": proof.all_valid,
                "items": list(level1_release_checks(proof, candidate.k)), "A2": item.a2, "A1": item.a1,
                "c1": item.c1, "c2": item.c2, "L": item.level, "S": RISK_NAMES[item.risk],
                "transition": None if move is None else {"from": RISK_NAMES[move[0]], "to": RISK_NAMES[move[1]]},
                "direct_to_2": item.day in direct})
        group: dict = {"days": group_days}
        try:
            convergence = candidate_convergence(spx, qqq, ma, t0, candidate, WINDOWS.average)
        except ConvergenceError as error:
            result["stop"] = {"class": type(error).__name__, "reason": error.reason, "message": str(error),
                              "group": key}
            groups[key] = group
            result["groups"] = groups
            return result
        group["channel_conv"] = {name: day_text(days[index]) for name, index in convergence.channel_indices.items()}
        group["kappa_ch"] = day_text(days[convergence.kappa_channel])
        group["system_conv"] = day_text(days[convergence.system_index])
        system_indices.append(convergence.system_index)
        records[key] = {proof.day: record for proof, record in zip(evidence, signal_records(system, evidence),
                                                                     strict=True)}
        groups[key] = group
    result["groups"] = groups
    reference_days = run_reference(REGISTERED_REFERENCE, ma[after], WINDOWS.average)
    result["reference"] = {"days": [{"day": day_text(days[t0]), "L": None, "S": RISK_NAMES[REGISTERED_REFERENCE],
                                     "init": True}] + [
        {"day": day_text(item.day), "L": item.level, "S": RISK_NAMES[item.risk], "init": False}
        for item in reference_days]}
    try:
        reference_index = t0 + 1 + reference_convergence(ma[after], WINDOWS.average)
    except ConvergenceError as error:
        result["stop"] = {"class": type(error).__name__, "reason": error.reason, "message": str(error),
                          "group": "主参照"}
        return result
    result["reference"]["conv_day"] = day_text(days[reference_index])
    result["kappa_all"] = day_text(days[max([*system_indices, reference_index])])
    try:
        j0 = common_start_index(t0, [*system_indices, reference_index], COMMON_START_OFFSET, len(days))
    except EmptyWindowError as error:
        result["stop"] = {"class": type(error).__name__, "reason": error.reason, "detail": error.detail,
                          "message": str(error)}
        return result
    result.update({"j0": day_text(days[j0]), "j0_index": j0, "n": len(days) - 1 - j0,
                   "window_first_signal_day": day_text(days[j0 - 1])})
    closes_all = {asset: [snapshot.closes[asset].get(day) for day in days] for asset in ASSETS}
    window_closes = {asset: values[j0:] for asset, values in closes_all.items()}
    alignment = None
    for candidate in CANDIDATES:
        key = group_key(candidate)
        window, signals = window_alignment(days, j0, records[key])
        alignment = {"收盘日期切片": [day_text(window[0]), day_text(window[-1]), len(window)],
                     "信号切片": [day_text(signals[0].day), day_text(signals[-1].day), len(signals)],
                     "依据": "j₀ 由 common_start_index 给出；E 为截止日（快照末日）；n = E 索引 − j₀ 索引；"
                             "signals[0] 为 j₀ − 1，signals[i + 1].day == days[i] 逐日期断言"}
        groups[key]["signal_sim"] = signal_simulation(window, signals, window_closes)
        groups[key]["exec_sim"] = policy_simulation(window, signals, window_closes)
        groups[key]["exec_signals"] = [{"idx": day_text(record.day), "S": RISK_NAMES[record.risk],
                                        "all_valid": record.all_valid,
                                        "signal_target": exposure_of(position_of(record.risk))}
                                       for record in (records[key][day] for day in window)]
    result["alignment"] = alignment
    # 主参照的信号模拟：signal_targets 只读取风险状态；all_valid 的登记取值待裁决，这里分别以全真、全假构造，
    # 断言两者的计划目标完全相同，证明信号模拟不依赖该取值。主参照的执行政策模拟不运行。
    reference_by_day = {}
    variants = []
    for flag in (True, False):
        built = reference_signal_records(reference_days, [flag] * len(reference_days))
        by_day = {record.day: record for record in built}
        window, signals = window_alignment(days, j0, by_day)
        variants.append(signal_targets(window, signals, POSITIONS))
        reference_by_day = by_day
    assert variants[0] == variants[1], "主参照信号模拟依赖了 all_valid"
    window, signals = window_alignment(days, j0, reference_by_day)
    result["reference"]["signal_sim"] = signal_simulation(window, signals, window_closes)
    try:
        basket = nav.basket_returns(window, window_closes)
        result["hold"] = {"failed": None, **nav_fields(nav.simulate_hold(window, basket, POSITIONS, TOLERANCE), basket)}
    except nav.NavUnavailable as error:
        missing = [[asset, day_text(day)] for asset, day in error.missing]
        result["hold"] = {"failed": {"type": "缺价", "missing": missing},
                          "nav": None, "summary": None}
    return result


def project_bootstrap(items: Sequence[Mapping]) -> list[dict]:
    """抽样索引：confirmatory.stationary_bootstrap_indices 的前 count 条完整索引序列。"""
    result = []
    for item in items:
        sequences = []
        for sequence in stationary_bootstrap_indices(item["n"], item["b"], item["count"], item["seed"]):
            sequences.append(list(sequence))
        result.append({"seed": item["seed"], "b": item["b"], "n": item["n"], "sequences": sequences})
    return result


# ---------------------------------------------------------------------------
# (d)、(e) 比对与记录
# ---------------------------------------------------------------------------

# 四种比对状态，另加“路径差异”（乙补修二第二节：项目两条路径的异常类别不同，已知原因，不计一致）。
# 窗口末日边界按负责人 2026-10-03 裁决记“未比较”，另以 window_end() 单独计数。
STATUSES = ("一致", "不一致", "未比较", "接口差异", "路径差异", "裁决差异（D13-甲）")
# 第二轮 B（《第二轮 B 设计（修订二）》第五节）新增两种单列状态，不计入一致；
# 只在 B 的记录中出现，A 的计数仍按 STATUSES。
TOLERANCE_STATUS = "容差内"
D15_STATUS = "构造边界差异（D15）"
B_STATUSES = (*STATUSES, TOLERANCE_STATUS, D15_STATUS)


@dataclass
class Recorder:
    """逐项比对的记录：按（层、字段）计数；非“一致”的逐条保留前若干条明细。"""

    counts: dict = field(default_factory=dict)
    details: list = field(default_factory=list)
    limit: int = 30

    def add(self, layer: str, item: str, status: str, where: str = "", tool: object = None,
            project: object = None, note: str = "") -> None:
        assert status in B_STATUSES, status
        key = (layer, item, status)
        self.counts[key] = self.counts.get(key, 0) + 1
        if status != "一致" and self.counts[key] <= self.limit:
            self.details.append({"layer": layer, "item": item, "status": status, "where": where,
                                 "tool": tool, "project": project, "note": note})

    def check(self, layer: str, item: str, ok: bool, where: str, tool: object, project: object) -> None:
        self.add(layer, item, "一致" if ok else "不一致", where, tool, project)

    def exact(self, layer: str, item: str, where: str, tool: object, project: object) -> None:
        self.check(layer, item, tool == project, where, tool, project)

    def number(self, layer: str, item: str, where: str, tool: object, project: object) -> None:
        """float64 量：显式绝对容差 1e-12；两边都为空时一致。"""
        if tool is None or project is None:
            self.check(layer, item, tool is None and project is None, where, tool, project)
        else:
            self.check(layer, item, close(float(tool), float(project)), where, tool, project)

    def inconsistent(self) -> list:
        return [item for item in self.details if item["status"] == "不一致"]

    def summary(self) -> dict:
        table: dict = {}
        for (layer, item, status), count in sorted(self.counts.items()):
            table.setdefault(layer, {}).setdefault(item, {})[status] = count
        return table

    def total(self, status: str) -> int:
        return sum(count for (_, _, kind), count in self.counts.items() if kind == status)

    def drop(self, layer: str, item: str | None = None) -> int:
        """删去某层（或某层某项）的全部计数与明细，返回删去的条数（第二轮消化第一轮的未比较占位）。"""
        keys = [key for key in self.counts if key[0] == layer and (item is None or key[1] == item)]
        removed = sum(self.counts.pop(key) for key in keys)
        self.details = [entry for entry in self.details
                        if not (entry["layer"] == layer and (item is None or entry["item"] == item))]
        return removed

    def window_end(self) -> int:
        """窗口末日边界的条数（工具在 E 日标“止损无法确定”的对象数）。"""
        return sum(count for (_, item, _), count in self.counts.items() if item == WINDOW_END_ITEM)


WINDOW_END_ITEM = "窗口末日边界：工具 E 日的止损无法确定标记"


# “无法确定”原因的映射表（乙补修第一节第 3 条）：工具 exec_sim 在 d 日的 stop_check 值 → 项目 Undetermined.reason。
# 两边各读实际字段；工具值不在表中时记“未比较”，项目原因与映射值不同时记“不一致”。
UNDETERMINED_REASONS = {"无法确定": "持仓期间当日净值不可得，止损是否触发无法判断"}
UNDETERMINED_ITEM = "undetermined.reason（工具 d 日 stop_check ↔ 项目 Undetermined.reason）"
GAP_ITEM = "W 自首个缺价日 m 起不可计算且不恢复（工具 W 为空，项目 executions 止于 m 之前）"
GAP_RETURN_NOTE = "项目接口未提供：PartialPolicyResult 不保留缺价后收益"


def compare_undetermined_reason(record: Recorder, key: str, tool_reason: object, mine_reason: object,
                                layer: str = "候选执行政策研究模拟") -> None:
    expected = UNDETERMINED_REASONS.get(tool_reason) if isinstance(tool_reason, str) else None
    if expected is None:
        record.add(layer, UNDETERMINED_ITEM, "未比较", key, tool_reason, mine_reason, "工具的原因不在映射表中")
    else:
        record.check(layer, UNDETERMINED_ITEM, mine_reason == expected, key, tool_reason, mine_reason)


def first_gap_index(axis: Sequence[str], tool_failed: Mapping | None) -> int | None:
    """首个缺价日 m 在执行日轴上的位置：取工具 failed.missing 中落在窗口内的最早日期（双方缺价清单另由
    compare_failed 比对）；没有缺价时为 None。"""
    days = sorted(day for _, day in (tool_failed or {}).get("missing", []) if day in axis)
    return axis.index(days[0]) if days else None


TOOL_ONLY_EXEC = ("mode", "base", "cooldown", "cap", "stop_check", "execution_computable", "events",
                  "summary.stops", "summary.reentries")


def compare_inputs(record: Recorder, tool: Mapping, project: Mapping, cutoff_index: int) -> None:
    for asset in ASSETS:
        tool_days, mine = tool["inputs"][asset][:cutoff_index + 1], project["inputs"][asset]
        record.exact("输入派生量", f"{asset} 日数", asset, len(tool_days), len(mine))
        for left, right in zip(tool_days, mine, strict=False):
            where = f"{asset} {left['day']}"
            record.exact("输入派生量", "day", where, left["day"], right["day"])
            for name in ("has_close", "h_complete", "h", "q"):
                record.exact("输入派生量", name, where, left[name], right[name])
            record.exact("输入派生量", "nl", where, str(left["nl"]), right["nl"])
            if right["reach"] is None:
                record.add("输入派生量", "reach", "未比较", where, left["reach"], None,
                           "项目接口未提供：缺价日没有回撤判定（drawdown_reaches 需要收盘价）")
            else:
                record.exact("输入派生量", "reach", where, left["reach"], right["reach"])
            record.add("输入派生量", "d", "未比较", where, note="项目接口未提供：D 只以乘法形式比较，不输出数值")
    tool_ma, mine_ma = tool["inputs"]["SPX_MA"][:cutoff_index + 1], project["inputs"]["SPX_MA"]
    record.exact("输入派生量", "SPX_MA 日数", "SPX_MA", len(tool_ma), len(mine_ma))
    for left, right in zip(tool_ma, mine_ma, strict=False):
        for name in ("ma_complete", "ma_sum", "below_ma"):
            record.exact("输入派生量", name, f"SPX_MA {left['day']}", left[name], right[name])


def compare_group_days(record: Recorder, key: str, tool_days: Sequence[Mapping], mine: Sequence[Mapping]) -> None:
    record.exact("通道与状态机", "日数", key, len(tool_days), len(mine))
    for left, right in zip(tool_days, mine, strict=False):
        where = f"{key} {left['day']}"
        for name in ("day", "idx", "init", "ch", "S", "c1", "c2"):
            record.exact("通道与状态机", name, where, left[name], right[name])
        if left["init"]:
            continue
        for name in ("valid", "all_valid", "items", "A2", "A1", "L", "direct_to_2"):
            record.exact("通道与状态机", name, where, left[name], right[name])
        tool_move = None if left["transition"] is None else {"from": left["transition"]["from"],
                                                              "to": left["transition"]["to"]}
        record.exact("通道与状态机", "transition（from、to）", where, tool_move, right["transition"])


def compare_signal_sim(record: Recorder, layer: str, key: str, tool: Mapping, mine: Mapping) -> None:
    record.exact(layer, "plan 条数", key, len(tool["plan"]), len(mine["plan"]))
    for left, right in zip(tool["plan"], mine["plan"], strict=False):
        where = f"{key} {left['exec_idx']}"
        for name in ("exec_idx", "signal_idx", "S", "source"):
            record.exact(layer, f"plan.{name}", where, left[name], right[name])
        for name, other in (("exposure", "exposure"), ("core_w", "core_w"), ("lev_w", "lev_w")):
            record.number(layer, f"plan.{name}", where, Decimal(left[name]), right[other])
        record.exact(layer, "plan.cap_in_force", where, left["cap_in_force"], False)
        record.exact(layer, "plan.cap_binding", where, left["cap_binding"], False)
    compare_switches(record, layer, key, tool, mine)
    compare_failed(record, layer, key, tool.get("failed"), mine.get("failed"))
    compare_nav(record, layer, key, tool.get("nav"), mine.get("nav"), tool.get("summary"), mine.get("summary"))


def compare_switches(record: Recorder, layer: str, key: str, tool: Mapping, mine: Mapping) -> None:
    record.exact(layer, "switches", key, tool["switches"], mine["switches"])
    record.exact(layer, "switch_list 条数", key, len(tool["switch_list"]), len(mine["switch_list"]))
    for left, right in zip(tool["switch_list"], mine["switch_list"], strict=False):
        where = f"{key} {left['exec_idx']}"
        record.exact(layer, "switch.exec_idx", where, left["exec_idx"], right["exec_idx"])
        record.exact(layer, "switch.stages", where, left["stages"], right["stages"])
        for name in ("from", "to", "delta", "delta_signed"):
            record.number(layer, f"switch.{name}", where, Decimal(left[name]), right[name])


def normalized_missing(items) -> list:
    return sorted([str(asset), str(day)] for asset, day in items)


def compare_failed(record: Recorder, layer: str, key: str, tool, mine) -> None:
    if tool is None or mine is None:
        record.exact(layer, "failed", key, tool, mine)
        return
    record.exact(layer, "failed.type", key, tool.get("type"), mine.get("type"))
    if tool.get("type") == "缺价":
        record.exact(layer, "failed.missing", key, normalized_missing(tool["missing"]),
                     normalized_missing(mine.get("missing", [])))


def compare_nav(record: Recorder, layer: str, key: str, tool_nav, mine_nav, tool_summary, mine_summary) -> None:
    if tool_nav is None or mine_nav is None:
        record.exact(layer, "nav 是否存在", key, tool_nav is None, mine_nav is None)
    else:
        record.exact(layer, "nav 日数", key, len(tool_nav), len(mine_nav))
        for left, right in zip(tool_nav, mine_nav, strict=False):
            where = f"{key} {left['idx']}"
            record.exact(layer, "nav.idx", where, left["idx"], right["idx"])
            for name in ("U", "R", "W"):
                record.number(layer, f"nav.{name}", where, left.get(name), right.get(name))
            record.add(layer, "nav.lev_factor、lev_ok", "未比较", where, note="项目接口未提供：杠杆因子只在内部检查")
    if tool_summary is None or mine_summary is None:
        record.exact(layer, "summary 是否存在", key, tool_summary is None, mine_summary is None)
        return
    for name in ("W_end", "lnW_end", "sum_log", "mdd"):
        record.number(layer, f"summary.{name}", key, tool_summary[name], mine_summary[name])
    record.exact(layer, "summary.recon_ok", key, tool_summary["recon_ok"], mine_summary["recon_ok"])


def next_trading_day(axis: Sequence[str], day: str) -> str | None:
    """共同交易日轴上 day 的下一交易日；day 为末日时没有（不补出次日）。"""
    position = axis.index(day)
    return axis[position + 1] if position + 1 < len(axis) else None


def compare_exec_sim(record: Recorder, key: str, tool: Mapping, mine: Mapping, signals: Sequence[Mapping],
                     layer: str = "候选执行政策研究模拟") -> None:
    """layer 默认为候选；第二轮主参照执行政策研究模拟用同一函数，只换层名（《比对第二轮A》第三节第 6 项）。"""
    tool_failed = tool.get("failed")
    if mine.get("failed") is not None or (tool_failed is not None and tool_failed.get("type") != "缺价"):
        record.exact(layer, "failed", key, tool.get("failed"), mine.get("failed"))
        return
    tool_days = tool["days"]
    axis = [item["idx"] for item in tool_days]
    record.exact(layer, "执行日轴", key, axis, [item["idx"] for item in signals])
    targets = mine["targets"]
    by_day = {item["exec_idx"]: item for item in targets}
    wealth = {item["idx"]: item for item in mine["days"]}
    # “无法确定”的换算（定稿第七节）：工具 d = undetermined_from；项目 e = Undetermined.day；判定 e == d⁺。
    d, e = tool.get("undetermined_from"), (mine.get("undetermined") or {}).get("day")
    stop_index = len(axis)
    if d is not None and d == axis[-1]:
        # 窗口末日边界（负责人 2026-10-03 裁决）：先判 d 是否为 E。到 E 为止的目标照常逐日比对（下面的循环）；
        # 工具在 E 日的“止损无法确定”标记记未比较（项目接口未提供，窗口内无后续目标），不判不一致。
        # 项目在窗口末日不再推进，不应给出 e；若给出，判为不一致。
        record.add(layer, WINDOW_END_ITEM, "未比较", key, d, e, "项目接口未提供，窗口内无后续目标")
        record.check(layer, "窗口末日边界：项目无 e", e is None, key, d, e)
    elif d is None or e is None:
        record.exact(layer, "undetermined（工具 d、项目 e）", key, d, e)
    else:
        plus = next_trading_day(axis, d)
        record.check(layer, "undetermined（e == d⁺）", e == plus, key, {"d": d, "d⁺": plus}, e)
        stop_index = axis.index(plus)
        compare_undetermined_reason(record, key, tool_days[axis.index(d)].get("stop_check"),
                                    mine["undetermined"].get("reason"), layer)
    gap = first_gap_index(axis, tool_failed)
    for index, (left, signal) in enumerate(zip(tool_days, signals, strict=True)):
        where = f"{key} {left['idx']}"
        record.exact(layer, "S", where, left["S"], signal["S"])
        record.exact(layer, "all_valid", where, left["all_valid"], signal["all_valid"])
        record.number(layer, "signal_target", where, Decimal(left["signal_target"]), signal["signal_target"])
        mine_target = by_day.get(left["idx"])
        # 三件事分别核对、各自计数（乙补修第一节第 2 条）：d 日及之前的目标；d⁺ 起目标无法确定；m 起 W 不可计算。
        if index >= stop_index:
            # d⁺ 起：工具 determined = false，项目无目标。
            record.check(layer, "d⁺ 起无法确定", left.get("determined") is False and mine_target is None, where,
                         left.get("determined"), mine_target)
        else:
            compare_exec_target(record, layer, where, left, mine_target, by_day, axis, index, stop_index)
        compare_exec_wealth(record, layer, where, left, wealth.get(left["idx"]), mine["complete"],
                            gap is not None and index >= gap)
        for name in ("mode", "base", "cooldown", "cap", "stop_check", "execution_computable"):
            if name in left:
                record.add(layer, f"days.{name}", "未比较", where, note="项目接口未提供")
    finish_exec_sim(record, layer, key, tool, mine, targets)


def compare_exec_target(record: Recorder, layer: str, where: str, left: Mapping, mine_target: Mapping | None,
                        by_day: Mapping, axis: Sequence[str], index: int, stop_index: int) -> None:
    """d 日及之前已确定的目标：双方逐日相同。"""
    if "held_exposure" in left or mine_target is not None:
        if "held_exposure" not in left or mine_target is None:
            record.check(layer, "held_exposure 是否存在", False, where, left.get("held_exposure"), mine_target)
        else:
            record.number(layer, "held_exposure", where, Decimal(left["held_exposure"]), mine_target["exposure"])
    following = by_day.get(axis[index + 1]) if index + 1 < len(axis) else None
    nxt = left.get("next_target")
    if nxt is not None and str(nxt["exec_idx"]).startswith("窗口外"):
        record.add(layer, "next_target（窗口外）", "未比较", where, nxt, None,
                   "项目接口未提供：窗口止于 E，项目不产生窗口外执行日的计划目标")
    elif "next_target" in left or (following is not None and index + 1 < stop_index):
        if nxt is None or following is None:
            record.check(layer, "next_target 是否存在", False, where, nxt, following)
        else:
            record.exact(layer, "next_target.exec_idx", where, nxt["exec_idx"], following["exec_idx"])
            for name in ("exposure", "core_w", "lev_w"):
                record.number(layer, f"next_target.{name}", where, Decimal(nxt[name]), following[name])
            record.exact(layer, "next_target.cap_in_force", where, nxt["cap_in_force"], following["cap_active"])
            record.exact(layer, "next_target.cap_binding", where, nxt["cap_binding"], following["reentry_cap"])
            if nxt["source"] == following["source"]:
                record.add(layer, "next_target.source", "一致")
            else:
                record.add(layer, "next_target.source", "接口差异", where, nxt["source"], following["source"],
                           "来源标签不同；暴露、权重与上限标记已分别比对")


def compare_exec_wealth(record: Recorder, layer: str, where: str, left: Mapping, mine_wealth: Mapping | None,
                        complete: bool, after_gap: bool) -> None:
    """净值：m 之前逐日比对 W、U、R；自 m 起工具 W 为空（不恢复）且项目无该日执行记录，U、R 记未比较。"""
    if after_gap:
        record.check(layer, GAP_ITEM, left.get("W") is None and mine_wealth is None, where, left.get("W"), mine_wealth)
        for name in ("U", "R"):
            if left.get(name) is not None:
                record.add(layer, f"days.{name}", "未比较", where, left[name], None, GAP_RETURN_NOTE)
        return
    mine_wealth = mine_wealth or {}
    for name in ("W", "U", "R"):
        tool_value, mine_value = left.get(name), mine_wealth.get(name)
        if tool_value is not None and mine_value is None and name in ("U", "R") and not complete:
            record.add(layer, f"days.{name}", "未比较", where, tool_value, None,
                       "项目接口未提供：PartialPolicyResult 只保留第一个缺价日之前的收益")
        else:
            record.number(layer, f"days.{name}", where, tool_value, mine_value)


def finish_exec_sim(record: Recorder, layer: str, key: str, tool: Mapping, mine: Mapping, targets: Sequence) -> None:
    record.add(layer, "events", "未比较", key, note="项目接口未提供")
    through = tool.get("switches_determined_through")
    last = targets[-1]["exec_idx"] if targets else None
    if mine["complete"] or through == last:
        compare_switches(record, layer, key, tool, mine)
    else:
        record.add(layer, "switches（已确定部分）", "未比较", key, through, last,
                   "工具的切换覆盖至 switches_determined_through，与项目已确定目标的末日不同")
    compare_failed(record, layer, key, tool.get("failed"),
                   None if mine["complete"] else {"type": "缺价", "missing": mine["missing"]})
    if mine["complete"]:
        summary = tool.get("summary")
        for name in ("W_end", "lnW_end", "sum_log", "mdd"):
            record.number(layer, f"summary.{name}", key, summary[name], mine["summary"][name])
        record.exact(layer, "summary.recon_ok", key, summary["recon_ok"], mine["summary"]["recon_ok"])
        record.add(layer, "summary.stops、reentries", "未比较", key, note="项目接口未提供")
    else:
        record.exact(layer, "summary 是否存在", key, tool.get("summary") is None, True)


EMPTY_WINDOW_DETAILS = {"j₀ 超出日期轴": "起点晚于最后一个收盘日", "j₀ 等于最后一个收盘日": "起点等于最后一个收盘日"}
STOP_REASONS = {"无法确定 t0": "t0 不存在", "始终不收敛": "未收敛", "评价窗口为空": "评价窗口为空"}


def compare_full(record: Recorder, tool: Mapping, project: Mapping) -> None:
    """乙方案范围内的算法层比对。"""
    cutoff_index = project["E_index"]
    compare_inputs(record, tool, project, cutoff_index)
    stop = project["stop"]
    tool_stop = tool.get("stop_reason")
    record.exact("停止原因", "退出码与原因", "场景",
                 {"exit": tool["exit_code"], "reason": (tool_stop or {}).get("reason")},
                 {"exit": 3 if stop else 0, "reason": STOP_REASONS.get(stop["reason"]) if stop else None})
    if stop and stop["class"] == "EmptyWindowError":
        record.exact("停止原因", "评价窗口为空的细分", "场景", tool_stop.get("sub_reason"),
                     EMPTY_WINDOW_DETAILS.get(stop["detail"]))
        record.exact("停止原因", "n", "场景", tool_stop.get("n"), 0)
        record.add("停止原因", "j0_index、E_index", "未比较", "场景", tool_stop.get("j0_index"), None,
                   "项目接口未提供：EmptyWindowError 只给出细分，不给出下标")
    if "t0" in project:
        record.exact("收敛", "t0", "场景", tool.get("t0"), project["t0"])
    for key, group in project.get("groups", {}).items():
        tool_group = tool["groups"][key]
        compare_group_days(record, key, tool_group["days"], group["days"])
        if "channel_conv" in group:
            for name in CHANNEL_NAMES:
                record.exact("收敛", "channel_conv.conv_day", f"{key} {name}",
                             tool_group["channel_conv"][name]["conv_day"], group["channel_conv"][name])
            record.exact("收敛", "kappa_ch", key, tool_group["kappa_ch"], group["kappa_ch"])
            record.exact("收敛", "system_conv.conv_day", key, tool_group["system_conv"]["conv_day"],
                         group["system_conv"])
            record.add("收敛", "channel_conv.runs、system_conv.runs、same_s_diff_counter_days", "未比较", key,
                       note="项目接口未提供：收敛函数只返回收敛位置")
    if "reference" in project:
        reference = project["reference"]
        tool_days = tool["reference"]["days"] if "reference" in tool else None
        if tool_days is not None:
            record.exact("主参照", "日数", "主参照", len(tool_days), len(reference["days"]))
            for left, right in zip(tool_days, reference["days"], strict=False):
                record.exact("主参照", "day", left["day"], left["day"], right["day"])
                record.exact("主参照", "S", left["day"], left["S"], right["S"])
                if right["init"]:
                    record.add("主参照", "L（t0 初始化日）", "未比较", left["day"], left["L"], None,
                               "项目接口未提供：t0 当日只初始化，不计算 L")
                else:
                    record.exact("主参照", "L", left["day"], left["L"], right["L"])
        if "conv_day" in reference:
            record.exact("收敛", "reference_conv.conv_day", "主参照", tool["reference_conv"]["conv_day"],
                         reference["conv_day"])
    for name in ("kappa_all",):
        if name in project:
            record.exact("收敛", name, "场景", tool.get(name), project[name])
    if "j0" not in project:
        return
    record.exact("收敛", "j0", "场景", tool["j0"], project["j0"])
    record.exact("收敛", "n", "场景", tool["n"], project["n"])
    record.exact("收敛", "E", "场景", tool["E"], project["E"])
    record.exact("收敛", "window_first_signal_day", "场景", tool["window_first_signal_day"],
                 project["window_first_signal_day"])
    for key, group in project["groups"].items():
        tool_group = tool["groups"][key]
        compare_signal_sim(record, "信号模拟", key, tool_group["signal_sim"], group["signal_sim"])
        compare_exec_sim(record, key, tool_group["exec_sim"], group["exec_sim"], group["exec_signals"])
        for name in ("r1", "r2", "ledger"):
            record.add("未比较（项目尚缺组合层）", name, "未比较", key)
    compare_signal_sim(record, "主参照信号模拟", "主参照", tool["reference"]["signal_sim"],
                       project["reference"]["signal_sim"])
    record.add("未比较（all_valid 的登记取值待组合层设计裁决）", "主参照执行政策模拟", "未比较", "主参照")
    hold = project["hold"]
    compare_failed(record, "一直持有", "hold", tool["hold"].get("failed"), hold.get("failed"))
    compare_nav(record, "一直持有", "hold", tool["hold"].get("nav"), hold.get("nav"), tool["hold"].get("summary"),
                hold.get("summary"))
    for name in ("r2_events", "selection", "records"):
        record.add("未比较（项目尚缺组合层）", name, "未比较", "场景")
    record.add("未比较（项目接口未提供）", "selection.reasons、ledger.pre_window_count", "未比较", "场景")


# ---------------------------------------------------------------------------
# 整行缺失：分层处理差异的三项核对（乙补修二第一节；负责人裁决 D3）
# ---------------------------------------------------------------------------

WHOLE_ROW_ITEM = "整行缺失：分层处理差异（已批准，裁决 D3）"
WHOLE_ROW_NOTE = "一致（分层处理差异：缺失日期、涉及资产及派生轴对应）"


@dataclass(frozen=True)
class AxisEvidence:
    """项目入口层对构造 CSV 日期列表的交易日轴核对（每资产各做一次，不因前一资产失败而略过）。

    missing：stock_trading_days(登记首日, 截止日) 与 CSV 日期列表（截止日以内）的集合差；
    derived：stock_trading_days(登记首日, 截止日)；entry：check_trading_axis 实际抛出的（异常类名, 原因码）。
    """

    missing: Mapping[str, tuple[str, ...]]
    derived: Mapping[str, tuple[str, ...]]
    entry: Mapping[str, tuple[str | None, str | None]]


def axis_evidence(dates: Mapping[str, Sequence[dt.date]], first: Mapping[str, dt.date],
                  cutoff: dt.date) -> AxisEvidence:
    """缺失日期由集合差得到，不解析异常文字；同时记录 check_trading_axis 实际抛出的异常类别与原因码。"""
    missing, derived, entry = {}, {}, {}
    for asset in ASSETS:
        expected = stock_trading_days(first[asset], cutoff)
        listed = [day for day in dates[asset] if day <= cutoff]
        missing[asset] = tuple(day_text(day) for day in sorted(set(expected) - set(listed)))
        derived[asset] = tuple(day_text(day) for day in expected)
        try:
            data_v20.check_trading_axis(asset, first[asset], cutoff, listed)
            entry[asset] = (None, None)
        except data_v20.DataEntryError as error:
            entry[asset] = (type(error).__name__, error.reason)
    return AxisEvidence(missing, derived, entry)


def constructed_axis_inputs(root: Path) -> tuple[dict[str, list[dt.date]], dict[str, dt.date]]:
    """构造目录中两资产 CSV 的日期列与登记首日（config_v20 读出的 first_date）。只读，不读价格。"""
    config = config_v20.load_v20_config(root)
    paths = StoragePaths(root)
    dates = {}
    for asset in ASSETS:
        lines = get_bytes(paths.market_daily_file(asset)).decode("utf-8").splitlines()[1:]
        dates[asset] = [dt.date.fromisoformat(line.split(",", 1)[0]) for line in lines if line]
    return dates, {asset: config.registered[asset].first_date for asset in ASSETS}


def compare_whole_row_missing(record: Recorder, added: Sequence[Mapping], derived_axis: Sequence[str] | None,
                              evidence: AxisEvidence) -> dict:
    """三项全部相等才记一致：(1) 逐资产缺失日期集合；(2) 涉及资产集合；(3) 派生日期轴。
    另要求有缺失的资产，check_trading_axis 确实抛 MissingPriceEntryError（原因码“缺少必需价格”）。"""
    tool_missing: dict[str, list[str]] = {}
    for item in added:
        for asset in item["assets"]:
            tool_missing.setdefault(asset, []).append(item["date"])
    tool_missing = {asset: sorted(days) for asset, days in sorted(tool_missing.items())}
    mine_missing = {asset: list(days) for asset, days in sorted(evidence.missing.items()) if days}
    entry_ok = all(evidence.entry[asset] == ("MissingPriceEntryError", data_v20.REASON_MISSING_PRICE)
                   for asset in mine_missing)
    axes = {tuple(axis) for axis in evidence.derived.values()}
    mine_axis = list(next(iter(axes))) if len(axes) == 1 else None
    items = {
        "1 实际缺失日期集合（按资产）": (tool_missing, mine_missing, tool_missing == mine_missing and entry_ok),
        "2 涉及资产集合": (sorted(tool_missing), sorted(mine_missing), sorted(tool_missing) == sorted(mine_missing)),
        "3 派生日期轴": (None if derived_axis is None else len(derived_axis), None if mine_axis is None else len(
            mine_axis), derived_axis is not None and mine_axis is not None and list(derived_axis) == mine_axis),
    }
    ok = all(value[2] for value in items.values())
    if ok:
        record.add("输入层", WHOLE_ROW_ITEM, "一致")
    else:
        record.add("输入层", WHOLE_ROW_ITEM, "不一致", "场景", {key: value[0] for key, value in items.items()},
                   {key: value[1] for key, value in items.items()}, "三项核对有不等")
    return {"结论": WHOLE_ROW_NOTE if ok else "不一致",
            "各项": {key: {"工具": value[0], "项目": value[1], "相等": value[2]} for key, value in items.items()},
            "项目入口 check_trading_axis": {asset: list(value) for asset, value in evidence.entry.items()},
            "派生轴首末": None if mine_axis is None else [mine_axis[0], mine_axis[-1], len(mine_axis)]}


def failed_check_class(checks: Mapping[str, str]) -> str | None:
    """适配路径验证中失败项的异常类名（取失败文字开头的类名；通过时为 None）。"""
    for value in checks.values():
        if value != "通过":
            return value.split("（", 1)[0].split("：", 1)[0]
    return None


def compare_input_layer(record: Recorder, tool: Mapping, facts: InputFacts, cutoff_text: str,
                        evidence: AxisEvidence | None = None) -> dict | None:
    """输入层：原始轴、截止日之后、派生轴与整行缺失、缺价日期集合、入口停止（定稿第五节第 3 条，第九节对应表）。"""
    checks = tool.get("input_checks") or {}
    tool_stop = tool.get("stop_reason") or {}
    added = checks.get("added_dates") or []
    axis_ok = all(value == "通过" for name, value in facts.checks.items() if "交易日轴" in name)
    if "input_checks" not in tool and tool_stop.get("reason") == "输入校验失败":
        # 负责人 2026-10-03 裁决：工具因输入校验失败停止时不输出 input_checks。
        for name in ("截止日以内原始轴", "截止日之后", "派生轴与整行缺失"):
            mine = list(facts.axis) if name == "截止日以内原始轴" else None
            record.add("输入层", name, "未比较", "场景", None, mine, "未比较（工具在输入校验失败时不输出该字段）")
    else:
        record.exact("输入层", "截止日以内原始轴", "场景", checks.get("raw_axis"), list(facts.axis))
        post = checks.get("post_cutoff") or {}
        record.add("输入层", "截止日之后", "一致" if post.get("status") == "未验证" else "不一致", "场景",
                   post, "项目不读取截止日之后的行", "只记录双方都未用于计算")
    if "input_checks" in tool and not added:
        record.exact("输入层", "派生轴（无整行缺失）", "场景", checks.get("derived_axis"),
                     list(facts.axis) if axis_ok else None)
    whole_row = None
    if "input_checks" in tool and added:
        if evidence is None:
            record.add("输入层", WHOLE_ROW_ITEM, "不一致", "场景", added, None, "缺少项目入口层的交易日轴证据")
        else:
            whole_row = compare_whole_row_missing(record, added, checks.get("derived_axis"), evidence)
    if facts.snapshot_built or facts.missing:
        for asset in ASSETS:
            tool_missing = [item["day"] for item in tool["inputs"][asset] if not item["has_close"]
                            and item["day"] <= cutoff_text] if "inputs" in tool else None
            record.exact("输入层", f"{asset} 缺价日期集合", "场景", tool_missing,
                         list(facts.missing.get(asset, ())))
    tool_reason = tool_stop.get("reason")
    if facts.stop_class is None:
        record.exact("输入层", "入口停止", "场景", tool_reason if tool_reason == "输入校验失败" else None, None)
    elif facts.stop_class == "DataInputError":
        record.check("输入层", "入口停止：CSV 入口 ↔ 工具 stop_reason（输入校验失败）",
                     tool["exit_code"] == 3 and tool_reason == "输入校验失败",
                     "场景", {"exit": tool["exit_code"], "reason": tool_reason}, facts.stop_reason)
    elif facts.stop_class == "MissingPriceEntryError":
        tool_has_missing = bool(added) or any(not item["has_close"] for asset in ASSETS
                                              for item in tool.get("inputs", {}).get(asset, []))
        record.check("输入层", "入口停止（缺少必需价格）", tool_has_missing and tool_reason != "输入校验失败", "场景",
                     {"exit": tool["exit_code"], "reason": tool_reason, "缺价": tool_has_missing}, facts.stop_reason)
    else:
        record.add("输入层", "入口停止", "不一致", "场景", tool_reason, facts.stop_class, "未列入对应表的入口异常")
    # 乙补修二第二节：适配路径验证的异常类别与 CSV 入口不同时单列一行，记“路径差异”，不计一致。
    adapted = failed_check_class(facts.checks)
    if facts.stop_class is not None and adapted is not None and adapted != facts.stop_class:
        record.add("输入层", f"适配路径验证：{adapted}，与 CSV 入口类别不同", "路径差异", "场景", facts.stop_class,
                   adapted, "路径差异（已知：适配路径先去重后对照轴）")
    return whole_row


def compare_bootstrap(record: Recorder, tool: Mapping, project: Sequence[Mapping]) -> None:
    items = tool["items"]
    record.exact("抽样索引", "组数", "抽样索引", len(items), len(project))
    for left, right in zip(items, project, strict=False):
        where = f"seed={left['seed']},b={left['b']},n={left['n']}"
        for name in ("seed", "b", "n"):
            record.exact("抽样索引", name, where, left[name], right[name])
        record.exact("抽样索引", "sequences", where, left["sequences"], right["sequences"])


# ---------------------------------------------------------------------------
# 第二轮（A2）：研究组合层接入（《接线会话指令：构造比对第二轮（A2）》）
# 只做表示换算，不新增推导规则；字段映射表见接线说明第十一节。
# ---------------------------------------------------------------------------

A2_THRESHOLDS = R2Thresholds(Decimal("0.95"), Decimal("1.05"), Decimal("0.97"))   # 登记第三节
A2_RULE = R2Rule(3, 5, 20)                                                       # 产品规格第八节
A2_R1_RATIO = 0.5                                                                # 登记第五节第 1 小节
SELF_SAME, SELF_DIFFERENT, SELF_NO_BASELINE, SELF_NO_FIELD = "相同", "不同", "无第一轮基准", "停止前无字段"
# 《A2 补充二》第四节第 2 条：Unavailable 只接受这两种（source, reason_code）组合，其他记“未比较（映射表外）”。
ALLOWED_UNAVAILABLE = {("basket_prefix", "缺少必需价格"), ("labels_r2.r2_events", "缺少必需价格")}
# 第二轮消化的第一轮“未比较”占位：（层，项目；None 为整层）。只在组合层未停止时删去并以新层的比对代替。
SUPERSEDED = (("未比较（项目尚缺组合层）", None), ("未比较（all_valid 的登记取值待组合层设计裁决）", None),
              ("未比较（项目接口未提供）", "selection.reasons、ledger.pre_window_count"), ("输入派生量", "d"),
              ("信号模拟", "nav.lev_factor、lev_ok"), ("主参照信号模拟", "nav.lev_factor、lev_ok"),
              ("收敛", "channel_conv.runs、system_conv.runs、same_s_diff_counter_days"))
EXIT_TO_TOOL = {research_run.Exit.NO_START: "t0 不存在", research_run.Exit.NOT_CONVERGED: "未收敛",
                research_run.Exit.EMPTY_WINDOW: "评价窗口为空"}


def self_state(check: object) -> str:
    return check["状态"] if isinstance(check, dict) else str(check)


def assert_self_consistent(checks: Mapping[str, object], name: str) -> None:
    """自洽检查任一“不同”即停（《A2 修订二》第二节、第六节）；“无第一轮基准”“停止前无字段”不计相同也不计不同。"""
    different = [field for field, check in checks.items() if self_state(check) == SELF_DIFFERENT]
    assert not different, f"{name} 自洽检查不同：{different}"


def research_histories(root: Path) -> dict[str, dt.date]:
    """登记历史起点：构造配置 config/wavewarn_v20.yaml 中两资产的 first_date（经 config_v20 读取，不手写）。"""
    config = config_v20.load_v20_config(root)
    return {asset: config.registered[asset].first_date for asset in ASSETS}


def declared_late_starts(scenario: Mapping) -> list[dict]:
    """《A2 补充二》第三节第 2 条：原始场景定义（meta，由生成脚本写入，工具不读）明确声明某资产自某日起才开始
    提供价格。识别条件：meta.类型 以“晚开始”结尾且含资产名，且 meta.缺价 中该资产有“起 = 0”的一项；
    声明开始下标 = 该项“长度”（生成脚本把该资产前“长度”日的价格置空）。只读场景定义，不读生成脚本运行结果。"""
    meta = scenario.get("meta") or {}
    kind = meta.get("类型")
    found: list[dict] = []
    if not isinstance(kind, str) or not kind.endswith("晚开始"):
        return found
    for gap in meta.get("缺价") or []:
        asset = gap.get("资产")
        if asset in ASSETS and asset in kind and gap.get("起") == 0 and isinstance(gap.get("长度"), int):
            start = gap["长度"]
            found.append({"资产": asset, "声明开始下标": start, "声明开始日": scenario["axis"][start],
                          "依据": {"meta.类型": kind, "meta.缺价项": dict(gap)},
                          "声明日之前有价格": any(value is not None for value in scenario["prices"][asset][:start])})
    return found


def construct_histories(scenario: Mapping, configured: Mapping[str, dt.date]) -> tuple[dict[str, dt.date], list[dict]]:
    """构造历史起点（裁决 2 收紧版）：默认取构造配置 first_date；只对场景定义明确声明晚开始、且声明日之前该资产
    无任何价格的资产，改为声明开始日。返回（histories，逐资产依据表）。"""
    histories = dict(configured)
    table = []
    for item in declared_late_starts(scenario):
        asset = item["资产"]
        corrected = not item["声明日之前有价格"]
        if corrected:
            histories[asset] = dt.date.fromisoformat(item["声明开始日"])
        table.append({"资产": asset, "原起点（配置 first_date）": day_text(configured[asset]),
                      "新起点": day_text(histories[asset]), "依据（场景定义字段原文）": item["依据"],
                      "声明开始日": item["声明开始日"], "声明日之前有价格": "有" if item["声明日之前有价格"] else "无",
                      "处理": "修正为声明开始日" if corrected else "维持原起点（声明日之前有价格）"})
    return histories, table


def start_disclosure(corrected: research_run.WindowResult, corrected_choice: object,
                     original: research_run.WindowResult, original_choice: object) -> dict:
    """修正起点的场景：两套起点下 R2 事件可得性、记录差异与选择出口的摘要（《A2 补充二》第三节第 4 条）。"""
    def events(result: research_run.WindowResult) -> dict:
        if result.common is None:
            return {}
        return {asset: (f"不可得（{value.source}，{value.reason_code}，缺 {len(value.missing)} 日）"
                        if isinstance(value, research_run.Unavailable) else f"可得（{len(value)} 个事件）")
                for asset, value in result.common.events.items()}

    differences = []
    for left, right in zip(corrected.records, original.records, strict=True):
        for name in ("failed", "r1", "r2", "log_wealth", "switches"):
            a, b = getattr(left, name), getattr(right, name)
            if (dict(a) if name == "r2" else a) != (dict(b) if name == "r2" else b):
                differences.append({"组": group_key(left.candidate), "字段": name,
                                    "修正起点": dict(a) if name == "r2" else a,
                                    "原起点": dict(b) if name == "r2" else b})

    def outcome(choice: object) -> object:
        return None if choice is None else choice.selection.outcome.value          # type: ignore[attr-defined]

    return {"R2 事件（修正起点）": events(corrected), "R2 事件（原起点）": events(original),
            "记录差异条数": len(differences), "记录差异": differences,
            "选择出口（修正起点）": outcome(corrected_choice), "选择出口（原起点）": outcome(original_choice)}


def run_research(snapshot: Snapshot, histories: Mapping[str, dt.date]) -> tuple[research_run.WindowResult,
                                                                              research_run.DevelopmentSelection | None]:
    """开发期、登记 27 组、诊断开启的一次运行；未停止时调用 select_development（容差 1e-10）。"""
    spec = research_run.WindowSpec(research_run.Purpose.DEVELOPMENT, None, snapshot.day, histories,
                                   research_run.InitialStates(REGISTERED_CHANNELS, REGISTERED_SYSTEM,
                                                              REGISTERED_REFERENCE),
                                   research_run.Continuity.COMPLETE_TRADING_AXIS)
    parameters = research_run.RunParameters(WINDOWS, POSITIONS, POLICY, A2_THRESHOLDS, A2_RULE, COMMON_START_OFFSET,
                                            TOLERANCE, A2_R1_RATIO, (), True)
    result = research_run.run_window(snapshot, spec, parameters, research_run.REGISTERED_CANDIDATES)
    chosen = research_run.select_development(result, TOLERANCE) if result.stop is None else None
    return result, chosen


def missing_fields(missing: Sequence[tuple[str, dt.date]]) -> list[list[str]]:
    return [[asset, day_text(day)] for asset, day in missing]


def signal_sim_fields(outcome: research_run.ObjectOutcome, basket: Sequence[float]) -> dict:
    """组合层对象结果 → 第一轮 signal_simulation 的同构表示（首个执行日来源记“初始建仓”）。"""
    plan = []
    for index, (target, record) in enumerate(zip(outcome.targets, outcome.signals, strict=True)):
        item = target_fields(target, "初始建仓" if index == 0 else None)
        item.update({"signal_idx": day_text(record.day), "S": RISK_NAMES[record.risk]})
        plan.append(item)
    result: dict = {"plan": plan, "switches": len(outcome.switches), "switch_list": switch_fields(outcome.switches),
                    "failed": None, "nav": None, "summary": None}
    if isinstance(outcome.signal_nav, research_run.Unavailable):
        result["failed"] = {"type": "缺价", "missing": missing_fields(outcome.signal_nav.missing)}
        return result
    result.update(nav_fields(outcome.signal_nav, basket))
    return result


def policy_fields(policy: nav.PolicyResult | nav.PartialPolicyResult, basket: Sequence[float]) -> dict:
    """执行政策研究模拟结果 → 第一轮 policy_simulation 的同构表示。basket 为窗口的收益前缀。"""
    changes = switches(policy.targets, POSITIONS)
    if isinstance(policy, nav.PolicyResult):
        fields = nav_fields(policy.nav, basket)
        return {"complete": True, "targets": [target_fields(item) for item in policy.targets], "days": fields["nav"],
                "summary": fields["summary"], "undetermined": None, "missing": [], "switches": len(changes),
                "switch_list": switch_fields(changes), "failed": None}
    days = [{"idx": day_text(item.day), "W": item.wealth} for item in policy.executions]
    for index in range(1, len(days)):
        days[index]["U"], days[index]["R"] = basket[index - 1], policy.returns[index - 1]
    undetermined = None if policy.undetermined is None else {
        "day": day_text(policy.undetermined.day), "reason": policy.undetermined.reason}
    return {"complete": False, "targets": [target_fields(item) for item in policy.targets], "days": days,
            "summary": None, "undetermined": undetermined, "missing": missing_fields(policy.missing),
            "switches": len(changes), "switch_list": switch_fields(changes), "failed": None}


BASKET_UNAVAILABLE = ("basket_prefix", "缺少必需价格")        # ALLOWED_UNAVAILABLE 中信号净值与一直持有唯一允许的组合
LABELS_UNAVAILABLE = ("labels_r2.r2_events", "缺少必需价格")
SELF_OUT_OF_TABLE = "未比较（映射表外）"


def unavailable_allowed(value: object, combination: tuple[str, str]) -> bool:
    """Unavailable 的 (source, reason_code) 是否为映射表内的组合（补充二第四节第 2 条；补充三修订二第一节第 3 条）。"""
    return (getattr(value, "source", None), getattr(value, "reason_code", None)) == combination and \
        combination in ALLOWED_UNAVAILABLE


def nav_or_missing(value: object, basket: Sequence[float]) -> dict:
    """信号净值或一直持有 → 第一轮同构表示；Unavailable 只接受 (basket_prefix, 缺少必需价格)，表外组合保留原值。"""
    if isinstance(value, research_run.Unavailable):
        if not unavailable_allowed(value, BASKET_UNAVAILABLE):
            return {"映射表外": {"source": value.source, "reason_code": value.reason_code}}
        return {"failed": {"type": "缺价", "missing": missing_fields(value.missing)}, "nav": None, "summary": None}
    return {"failed": None, **nav_fields(value, basket)}


def research_fields(result: research_run.WindowResult, axis: Sequence[dt.date]) -> dict:
    """组合层结果 → 第一轮项目字段的同构表示（自洽检查用；只做表示换算）。只放入实际已产生的部分：
    窗口（window）、主参照（reference 且 common）、一直持有（common）、已完成的候选组（candidates）。"""
    window, common, reference = result.window, result.common, result.reference
    fields: dict = {"映射表外": {}, "groups": {}}
    if window is not None:
        fields.update({"t0": day_text(axis[window.t0]), "kappa_all": day_text(axis[window.kappa_all]),
                       "j0": day_text(axis[window.j0]), "j0_index": window.j0, "n": window.n,
                       "E": day_text(axis[-1]), "E_index": window.e_index,
                       "window_first_signal_day": day_text(axis[window.j0 - 1]),
                       "reference": {"conv_day": day_text(axis[window.reference_index])}})
    basket = common.prefix.values if common is not None else ()
    if window is not None and reference is not None and common is not None:
        signal = nav_or_missing(reference.outcome.signal_nav, basket)
        fields["reference"].update({
            "days": [{"day": day_text(item.day), "L": item.level, "S": RISK_NAMES[item.risk]}
                     for item in reference.days],
            "signal_sim": None if "映射表外" in signal else signal_sim_fields(reference.outcome, basket),
            "all_valid": list(reference.all_valid)})
        if "映射表外" in signal:
            fields["映射表外"]["reference.signal_sim"] = signal["映射表外"]
    if common is not None:
        fields["hold"] = nav_or_missing(common.hold, basket)
        fields["common.prefix"] = {"values": list(common.prefix.values), "first_missing": common.prefix.first_missing}
    for candidate, outcome in result.candidates.items():
        assert window is not None
        convergence = window.convergences[candidate]
        signal = nav_or_missing(outcome.outcome.signal_nav, basket)
        key = group_key(candidate)
        fields["groups"][key] = {
            "channel_conv": {name: day_text(axis[index]) for name, index in convergence.channel_indices.items()},
            "kappa_ch": day_text(axis[convergence.kappa_channel]),
            "system_conv": day_text(axis[convergence.system_index]),
            "days": [{"day": day_text(item.day), "S": RISK_NAMES[item.risk], "c1": item.c1, "c2": item.c2,
                      "L": item.level, "A1": item.a1, "A2": item.a2} for item in outcome.system],
            "signal_sim": None if "映射表外" in signal else signal_sim_fields(outcome.outcome, basket),
            "exec_sim": policy_fields(outcome.outcome.policy, basket),
            # 第一轮 exec_signals 为窗口各执行日当日的信号；组合层的净值模拟信号覆盖 axis[j0−1 .. E−1]，取其第 2 项起。
            "exec_signals": [{"idx": day_text(item.day), "S": RISK_NAMES[item.risk], "all_valid": item.all_valid,
                              "signal_target": exposure_of(position_of(item.risk))}
                             for item in outcome.outcome.signals[1:]]}
        if "映射表外" in signal:
            fields["映射表外"][f"{key}.signal_sim"] = signal["映射表外"]
    return fields


def canonical(value: object) -> object:
    """经同一 JSON 序列化与解析，消除元组与列表、float 与 Decimal 文字的表示差别。"""
    return parse_json(dump_json(value))


MISSING = object()          # 哨兵：第一轮项目字段缺键（不与合法的 None 混同）


def leaves(value: object, path: str = "") -> dict[str, object]:
    """嵌套字典与列表展开为 路径 → 叶值；列表下标写入路径。空字典、空列表本身作为叶保留（路径与类型都参与比较）。"""
    found: dict[str, object] = {}
    if isinstance(value, dict) and value:
        for key, item in value.items():
            found.update(leaves(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            found.update(leaves(item, f"{path}[{index}]"))
    else:
        found[path] = value
    return found


def leaf_same(left: object, right: object) -> bool:
    """浮点（float、Decimal）沿用第一轮容差口径 abs_tol = 1e-12；布尔、整数、文字、None、空容器精确相等（类型须同）。"""
    if isinstance(left, bool) or isinstance(right, bool) or left is None or right is None:
        return type(left) is type(right) and left == right
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    if isinstance(left, float | Decimal) and isinstance(right, float | Decimal):
        return close(float(left), float(right))
    return type(left) is type(right) and left == right


def grown_from_empty(path: str, value: object, left: Mapping[str, object]) -> bool:
    """第一轮该路径为空字典或空列表，而组合层在同一路径下有同类型的子项：只是组合层多出的键。"""
    if isinstance(value, dict) and not value:
        return any((key.startswith(f"{path}.") if path else not key.startswith("[")) for key in left)
    if isinstance(value, list) and not value:
        return any(key.startswith(f"{path}[") for key in left)
    return False


def compare_group(mine: object, saved: object) -> dict:
    """一组字段逐叶比较（负责人口径）：组合层多出的键记“无第一轮基准”；第一轮已有的键在组合层缺失、或由非空变为空
    （字典、列表同样），一律判“不同”；任一叶不同则该组“不同”。第一轮缺该组为“无第一轮基准”。"""
    if saved is MISSING:
        return {"状态": SELF_NO_BASELINE, SELF_SAME: 0, SELF_DIFFERENT: 0, SELF_NO_BASELINE: len(leaves(mine)),
                "组合层实际值": mine if not isinstance(mine, dict | list) else "（见逐场景记录）"}
    left, right = leaves(mine), leaves(saved)
    counts = {SELF_SAME: 0, SELF_DIFFERENT: 0, SELF_NO_BASELINE: 0}
    for path, value in left.items():
        if path not in right:
            counts[SELF_NO_BASELINE] += 1
        else:
            counts[SELF_SAME if leaf_same(value, right[path]) else SELF_DIFFERENT] += 1
    for path, value in right.items():
        if path not in left and not grown_from_empty(path, value, left):
            counts[SELF_DIFFERENT] += 1
    if counts[SELF_DIFFERENT]:
        state = SELF_DIFFERENT
    elif counts[SELF_SAME]:
        state = SELF_SAME
    else:
        state = SELF_NO_BASELINE
    return {"状态": state, **counts}


def key_of(mapping: object, key: str) -> object:
    return mapping[key] if isinstance(mapping, dict) and key in mapping else MISSING


def no_field() -> dict:
    return {"状态": SELF_NO_FIELD, SELF_SAME: 0, SELF_DIFFERENT: 0, SELF_NO_BASELINE: 0}


def compare_stop(stop: research_run.StopRecord, saved_stop: object) -> dict[str, dict]:
    """停止字段按已登记映射分别比较（补充三修订二第一节第 2 条）：已登记的只有 STOP_REASONS 表的三个原因码
    （无法确定 t0、始终不收敛、评价窗口为空），其取值与组合层出口文字、原因码同一套。出口或原因码在表内的，
    与第一轮 stop.reason 比较，异常类名与第一轮 stop.class、细分与第一轮同名字段直接比较；不在表内的一律记
    “无第一轮基准”并保留实际值，不为补齐映射猜测对应。第一轮明确记录未停止（stop 为 null）而组合层以表内
    出口停止，判“不同”。"""
    registered = stop.exit.value in STOP_REASONS
    if saved_stop is None:
        reference: object = {"reason": None, "class": None, **{name: None for name in stop.detail}}
    else:
        reference = saved_stop if isinstance(saved_stop, dict) else {}
    checks = {
        "stop.exit ↔ 第一轮 stop.reason": compare_group(stop.exit.value, key_of(reference, "reason")
                                                         if registered else MISSING),
        "stop.reason_code ↔ 第一轮 stop.reason": compare_group(
            stop.reason_code, key_of(reference, "reason") if stop.reason_code in STOP_REASONS else MISSING),
        "stop.exception_type ↔ 第一轮 stop.class": compare_group(stop.exception_type, key_of(reference, "class")
                                                                 if registered else MISSING)}
    for name, value in stop.detail.items():
        checks[f"stop.detail.{name}"] = compare_group(canonical(value), key_of(reference, name)
                                                      if registered else MISSING)
    return checks


def self_consistency(result: research_run.WindowResult, project: Mapping,
                     axis: Sequence[dt.date]) -> dict[str, dict]:
    """组合层输出与 _2 项目字段逐字段组比较（《A2 修订二》第二节；补充二第四节第 1 条；补充三修订二第一节）。
    四种状态：相同 / 不同 / 无第一轮基准 / 停止前无字段；信号净值或一直持有的 Unavailable 为表外组合时，该组记
    “未比较（映射表外）”。逐字段组判断是否在停止前已产生：已产生且第一轮有对应项的照常比较，未产生的记
    “停止前无字段”。project 已由 parse_json 解析，不再重复序列化。"""
    checks: dict[str, dict] = {}
    saved_stop = key_of(project, "stop")
    if result.stop is not None:
        checks.update(compare_stop(result.stop, saved_stop))
    else:
        checks["stop"] = compare_group(None, saved_stop)
    fields = research_fields(result, axis)
    out_of_table = fields.pop("映射表外")
    mine = canonical(fields)
    saved_reference = key_of(project, "reference")
    mine_reference = mine.get("reference", {})

    def produced(name: str, value: object, saved: object) -> None:
        checks[name] = no_field() if value is MISSING else compare_group(value, saved)

    for name in ("t0", "kappa_all", "j0", "j0_index", "n", "E", "E_index", "window_first_signal_day"):
        produced(name, mine.get(name, MISSING), key_of(project, name))
    produced("reference.conv_day", mine_reference.get("conv_day", MISSING), key_of(saved_reference, "conv_day"))
    first = day_text(axis[result.window.j0 - 2]) if result.window is not None else ""
    saved_days = key_of(saved_reference, "days")
    produced("reference.days", mine_reference.get("days", MISSING), MISSING if saved_days is MISSING else [
        {name: item[name] for name in ("day", "L", "S") if name in item} for item in saved_days
        if item["day"] >= first])
    if "reference.signal_sim" in out_of_table:
        checks["reference.signal_sim"] = {"状态": SELF_OUT_OF_TABLE, **out_of_table["reference.signal_sim"]}
    else:
        produced("reference.signal_sim", mine_reference.get("signal_sim", MISSING),
                 key_of(saved_reference, "signal_sim"))
    produced("reference.all_valid", mine_reference.get("all_valid", MISSING), MISSING)
    if isinstance(mine.get("hold"), dict) and "映射表外" in mine["hold"]:
        checks["hold"] = {"状态": SELF_OUT_OF_TABLE, **mine["hold"]["映射表外"]}
    else:
        produced("hold", mine.get("hold", MISSING), key_of(project, "hold"))
    produced("common.prefix", mine.get("common.prefix", MISSING), MISSING)
    saved_groups = key_of(project, "groups")
    completed = list(mine["groups"])
    if result.stop is None:
        checks["groups 键"] = compare_group(sorted(completed),
                                           MISSING if saved_groups is MISSING else sorted(saved_groups))
    elif completed:
        # 候选阶段停止：只比已完成的候选组，各组须在第一轮组中。
        checks["groups 键（已完成候选）"] = compare_group(completed, MISSING if saved_groups is MISSING else [
            key for key in saved_groups if key in mine["groups"]])
    else:
        checks["groups 键（已完成候选）"] = no_field()
    for key, group in mine["groups"].items():
        other = key_of(saved_groups, key)
        for name in ("channel_conv", "kappa_ch", "system_conv", "exec_sim"):
            checks[f"{key}.{name}"] = compare_group(group[name], key_of(other, name))
        if f"{key}.signal_sim" in out_of_table:
            checks[f"{key}.signal_sim"] = {"状态": SELF_OUT_OF_TABLE, **out_of_table[f"{key}.signal_sim"]}
        else:
            checks[f"{key}.signal_sim"] = compare_group(group["signal_sim"], key_of(other, "signal_sim"))
        other_signals = key_of(other, "exec_signals")
        last = group["exec_signals"][-1]["idx"] if group["exec_signals"] else ""
        checks[f"{key}.exec_signals（axis[j0 .. E−1]）"] = compare_group(
            group["exec_signals"], MISSING if other_signals is MISSING else [
                item for item in other_signals if item["idx"] <= last])
        other_days = key_of(other, "days")
        checks[f"{key}.days"] = compare_group(group["days"], MISSING if other_days is MISSING else [
            {name: item[name] for name in ("day", "S", "c1", "c2", "L", "A1", "A2") if name in item}
            for item in other_days if item["day"] >= first])
    return checks


# 映射表第 1 至 11 项（接线说明第十一节）


def compare_r2_events(record: Recorder, tool: Mapping, events: Mapping) -> None:
    layer = "R2 事件"
    errors = tool.get("r2_events_error") or {}
    for asset in ASSETS:
        mine = events[asset]
        if isinstance(mine, research_run.Unavailable) and (mine.source, mine.reason_code) != (
                "labels_r2.r2_events", "缺少必需价格"):
            record.add(layer, "不可得（映射表外）", "未比较", asset, errors.get(asset),
                       [mine.source, mine.reason_code], "未比较（映射表外）：只映射 labels_r2.r2_events + 缺少必需价格")
            continue
        if isinstance(mine, research_run.Unavailable):
            error = errors.get(asset)
            record.check(layer, "不可得（reason_code ↔ r2_events_error.reason）", error is not None and
                         error.get("reason") == mine.reason_code, asset, error, mine.reason_code)
            record.exact(layer, "不可得（missing ↔ missing_days）", asset,
                         None if error is None else error.get("missing_days"),
                         [day_text(day) for _, day in mine.missing])
            continue
        listed = (tool.get("r2_events") or {}).get(asset)
        record.exact(layer, "可得", asset, listed is not None and asset not in errors, True)
        if listed is None:
            continue
        record.exact(layer, "事件数", asset, len(listed), len(mine))
        for left, right in zip(listed, mine, strict=False):
            where = f"{asset} {left['P']}"
            for name, value in (("P", day_text(right.peak)), ("T3", day_text(right.t3)), ("T5", day_text(right.t5)),
                                ("Tr", day_text(right.trough)),
                                ("End", None if right.end is None else day_text(right.end)),
                                ("unfinished", right.unfinished)):
                record.exact(layer, name, where, left[name], value)
            record.exact(layer, "C_P", where, Decimal(str(left["C_P"])), right.peak_close)
            record.exact(layer, "C_Tr", where, Decimal(str(left["C_Tr"])), right.trough_close)


def ratio_pair(value: object) -> object:
    """工具的 [a, b] 或 "无定义" ↔ 项目的 (a, b) 或 None。"""
    return None if value == "无定义" else (None if value is None else list(value))


UNAVAILABLE_TEXT = "无法计算（R2 事件不可得）"     # 工具修正三的登记字串（工具 README 1709880 第 279、301 行）


def availability_premise(tool: Mapping, events: Mapping) -> dict[str, bool]:
    """前提 P(a)（《A2 第三轮补充二（修订一）》第一节）：项目 common.events[a] 为
    Unavailable(labels_r2.r2_events, 缺少必需价格)，工具 r2_events_error[a] 存在，且第 1′ 项（不可得原因与缺价日）
    双方一致。不凭字串或 None 单独认定。"""
    errors = tool.get("r2_events_error") or {}
    premise = {}
    for asset in ASSETS:
        mine, error = events.get(asset), errors.get(asset)
        premise[asset] = (isinstance(mine, research_run.Unavailable) and unavailable_allowed(mine, LABELS_UNAVAILABLE)
                          and isinstance(error, Mapping) and error.get("reason") == mine.reason_code
                          and error.get("missing_days") == [day_text(day) for _, day in mine.missing])
    return premise


def compare_r2_judgements(record: Recorder, key: str, tool_group: Mapping, results: Mapping,
                          premise: Mapping[str, bool] | None = None) -> None:
    """不可比时按修订一第一节第 1 条有序分支判定（命中即止）；双方均为对象时照既有逐字段比较。"""
    layer = "R2 判定"
    tool_r2 = tool_group.get("r2") or {}
    premise = premise or {}
    for asset in ASSETS:
        mine = results[asset]
        where = f"{key} {asset}"
        raw = tool_r2.get(asset, MISSING)
        tool_object, p = isinstance(raw, Mapping), premise.get(asset, False)
        if p and (tool_object or mine is not None):                                                     # ①
            record.add(layer, "P 成立而出现判定对象", "不一致", where, "对象" if tool_object else raw,
                       "对象" if mine is not None else None)
            continue
        if p and (raw is MISSING or raw == UNAVAILABLE_TEXT) and mine is None:                          # ②
            record.exact(layer, "R2 无法计算（双方均无判定）", where, True, True)
            continue
        if not p and (raw == UNAVAILABLE_TEXT or mine is None):                                         # ③
            record.add(layer, "P 不成立而出现不可得表示", "不一致", where,
                       None if raw is MISSING else ("对象" if tool_object else raw),
                       "对象" if mine is not None else None)
            continue
        if isinstance(raw, str):                                                                         # ⑤
            record.add(layer, "R2 判定（映射表外字串）", "未比较", where, raw, None, "未比较（映射表外）")
            continue
        if not tool_object:                                       # 工具键缺失而项目有判定、且 P 不成立：沿用第二轮
            record.exact(layer, "R2 无法计算（双方均无判定）", where, True, mine is None)
            continue
        left = raw                                                                                       # ④
        record.exact(layer, "事件数", where, len(left["events"]), len(mine.judgements))
        for event, judgement in zip(left["events"], mine.judgements, strict=False):
            at = f"{where} {event['P']}"
            pairs = (("P", day_text(judgement.event.peak)), ("category", judgement.category.value),
                     ("first_new", None if judgement.first_new_day is None else day_text(judgement.first_new_day)),
                     ("exec_idx", None if judgement.executable_day is None else day_text(judgement.executable_day)),
                     ("offset_vs_T3", judgement.executable_offset),
                     ("peak_new_uncertain", judgement.peak_new_uncertain),
                     ("first_new_confirmable", judgement.peak_new_uncertain))
            for name, value in pairs:
                record.exact(layer, f"events.{name}", at, event[name], value)
        record.exact(layer, "counts", where, dict(left["counts"]),
                     {category.value: count for category, count in mine.counts.items()})
        for name, value in (("denominator", mine.denominator), ("passed", mine.achieved),
                            ("new_only", mine.new_only), ("computable", mine.computable),
                            ("ratio_ok", mine.meets)):
            record.exact(layer, name, where, left[name], value)
        record.exact(layer, "excluding_insufficient", where, ratio_pair(left["excluding_insufficient"]),
                     None if mine.excluding_insufficient is None else list(mine.excluding_insufficient))


LEDGER_STRUCTURE_ITEM = "段账结构（双方均无段）"
KEY_MISSING_TEXT = "缺键"                     # 结构核验记录值：键不存在（与显式 null 区分）


def compare_ledgers(record: Recorder, key: str, tool_group: Mapping, ledgers: Mapping,
                    premise: Mapping[str, bool] | None = None, source_label: str | None = None) -> None:
    """不可比时按修订一第一节第 2 条有序分支判定（命中即止）；可得资产按既有换算逐项比较。
    ledger 键整体缺失（旧工具形态）只允许在 _2 来源（或未设第三轮来源）时照第二轮记录（⑥）。"""
    layer = "提示段账"
    premise = premise or {}
    if "ledger" not in tool_group:                                                                       # ⑥
        for asset in ASSETS:
            mine, where = ledgers[asset], f"{key} {asset}"
            if source_label == SOURCE_THIRD:
                record.add(layer, "ledger 键缺失（_1 来源）", "不一致", where, None,
                           "有" if mine is not None else None)
            elif mine is not None:
                record.add(layer, "段账", "未比较", where, None, "有",
                           "工具在任一资产 R2 事件不可得时不输出 ledger")
            else:
                record.exact(layer, "段账（双方均无）", where, True, True)
        return
    left = tool_group["ledger"] if isinstance(tool_group["ledger"], Mapping) else {}
    by_asset = left.get("by_asset") or {}
    unavailable = {asset for asset in ASSETS if premise.get(asset, False)}
    if unavailable == set(ASSETS):                                                                       # ⑤
        # 补充三第一节第 1 条：三项均要求“键存在且取值符合”，缺键不当作显式 null；记录值缺键写“缺键”。
        shown = lambda mapping, name: mapping[name] if name in mapping else KEY_MISSING_TEXT   # noqa: E731
        for name, ok, value in (
                (f"by_asset 均为“{UNAVAILABLE_TEXT}”",
                 all(asset in by_asset and by_asset[asset] == UNAVAILABLE_TEXT for asset in ASSETS),
                 [shown(by_asset, asset) for asset in ASSETS]),
                ("segments 为 null", "segments" in left and left["segments"] is None, shown(left, "segments")),
                ("pre_window_count 为 null", "pre_window_count" in left and left["pre_window_count"] is None,
                 shown(left, "pre_window_count"))):
            record.check(layer, LEDGER_STRUCTURE_ITEM, ok, f"{key} {name}", value, None)
    for asset in ASSETS:
        mine = ledgers[asset]
        where = f"{key} {asset}"
        raw = by_asset.get(asset, MISSING)
        tool_object, p = isinstance(raw, Mapping), asset in unavailable
        if p and (tool_object or mine is not None):                                                      # ①
            record.add(layer, "P 成立而出现段账对象", "不一致", where, "对象" if tool_object else raw,
                       "对象" if mine is not None else None)
            continue
        if p and raw == UNAVAILABLE_TEXT and mine is None:                                               # ②
            record.exact(layer, "段账（双方均无）", where, True, True)
            continue
        if not p and (raw == UNAVAILABLE_TEXT or mine is None):                                          # ③
            record.add(layer, "P 不成立而出现不可得表示", "不一致", where,
                       None if raw is MISSING else ("对象" if tool_object else raw),
                       "对象" if mine is not None else None)
            continue
        if isinstance(raw, str):                                                                          # ⑦
            record.add(layer, "by_asset（映射表外字串）", "未比较", where, raw, None, "未比较（映射表外）")
            continue
        if not tool_object or not isinstance(left.get("segments"), list):        # 其余形态：不在任何映射分支内
            record.add(layer, "段账形态（映射表外）", "不一致", where, None if raw is MISSING else raw,
                       "对象" if mine is not None else None)
            continue
        segments = left["segments"]                                                                       # ④
        record.exact(layer, "段数", where, len(segments), len(mine.classes))
        for segment, (span, category) in zip(segments, mine.classes, strict=False):
            at = f"{where} {segment['start']}"
            record.exact(layer, "segments.start、end、pre_window", at,
                         [segment["start"], segment["end"], segment["pre_window"]],
                         [day_text(span.start), day_text(span.end), span.pre_window])
            classes = segment["class"]
            if classes is None:
                # class 为 null 只在 pre_window 为真且项目该段为“窗口前已启动”时记一致（修订一第一节第 2 条④）。
                record.check(layer, "segments.class（null ↔ 窗口前已启动）",
                             segment["pre_window"] is True and category.value == research_pre_window_class(), at,
                             None, category.value)
                continue
            record.check(layer, "segments.class 不含不可得资产的键", not set(classes) & unavailable, at,
                         sorted(classes), sorted(unavailable))
            record.exact(layer, "segments.class", at, classes.get(asset), category.value)
        counts = {category.value: count for category, count in mine.counts.items()}
        pre = counts.pop(research_pre_window_class())
        record.exact(layer, "by_asset.counts", where, dict(left["by_asset"][asset]["counts"]), counts)
        record.exact(layer, "pre_window_count", where, left["pre_window_count"], pre)
        record.exact(layer, "false_alarm_ratio", where, ratio_pair(left["by_asset"][asset]["false_alarm_ratio"]),
                     None if mine.false_alarm_ratio is None else list(mine.false_alarm_ratio))


def research_pre_window_class() -> str:
    return "窗口前已启动"


def compare_r1(record: Recorder, key: str, tool_group: Mapping, mine: object) -> None:
    layer = "R1"
    left = tool_group.get("r1") or {}
    record.exact(layer, "computable", key, left.get("computable"), mine.computable)
    record.exact(layer, "ok", key, left.get("ok"), mine.satisfied)
    record.number(layer, "mdd_signal", key, left.get("mdd_signal"), mine.signal_drawdown)
    record.number(layer, "mdd_hold", key, left.get("mdd_hold"), mine.hold_drawdown)
    if "ratio" in left:
        record.add(layer, "ratio", "未比较", key, left["ratio"], None, "项目接口未提供：R1Result 不给出回撤比")
    for _ in left.get("segments") or []:
        record.add(layer, "segments", "未比较", key, note="本轮不比分段报告（工具无对应的项目分段输入）")


def compare_records_and_selection(record: Recorder, tool: Mapping, result: research_run.WindowResult,
                                  chosen: research_run.DevelopmentSelection | None) -> None:
    layer = "候选记录与选择"
    records = tool.get("records") or []
    record.exact(layer, "记录数", "records", len(records), len(result.records))
    for left, right in zip(records, result.records, strict=False):
        where = left["key"]
        record.exact(layer, "key", where, left["key"], group_key(right.candidate))
        record.exact(layer, "order", where, left["order"], right.order)
        record.exact(layer, "failure ↔ failed", where, left["failure"] is not None, right.failed)
        record.number(layer, "lnW_end", where, left["lnW_end"], right.log_wealth)
        record.exact(layer, "r1_ok", where, left["r1_ok"], right.r1)
        record.exact(layer, "r2_ok", where, dict(left["r2_ok"]), dict(right.r2))
        record.exact(layer, "switches", where, left["switches"], right.switches)
        for name in ("nav_missing", "r2_computable"):
            record.add(layer, name, "未比较", where, left[name], None,
                       "项目接口未提供：CandidateRecord 不给出合并标记（以 lnW_end、r2_ok 的 None 分项比对）")
    selection = tool.get("selection")
    if selection is None or chosen is None:
        record.exact(layer, "selection 是否存在", "selection", selection is None, chosen is None)
        return
    mine = chosen.selection
    record.exact(layer, "exit ↔ outcome", "selection", selection["exit"], mine.outcome.value)
    record.exact(layer, "selected", "selection", selection.get("selected"),
                 None if mine.selected is None else group_key(mine.selected))
    record.exact(layer, "feasible", "selection", selection.get("feasible", []), [group_key(c) for c in mine.feasible])
    record.exact(layer, "tie_group ↔ tied", "selection", selection.get("tie_group", []),
                 [group_key(c) for c in mine.tied])
    record.number(layer, "M ↔ maximum", "selection", selection.get("M"), mine.maximum)
    if "reasons" in selection:
        record.add(layer, "selection.reasons", "未比较", "selection", selection["reasons"], None,
                   "项目接口未提供：SelectionResult 不给出原因文字")


def reference_exec_signals(reference: research_run.ReferenceOutcome) -> list[dict]:
    """主参照窗口各执行日（axis[j0 .. E]）当日的信号：reference.days 与 all_valid 自 axis[j0−2] 起，取第 3 项起。"""
    return [{"idx": day_text(item.day), "S": RISK_NAMES[item.risk], "all_valid": valid,
             "signal_target": exposure_of(position_of(item.risk))}
            for item, valid in zip(reference.days[2:], reference.all_valid[2:], strict=True)]


def compare_drawdowns(record: Recorder, tool: Mapping, diagnostics: research_run.Diagnostics,
                      cutoff_index: int) -> None:
    layer = "诊断 D/D̂"
    for asset in ASSETS:
        tool_days = tool["inputs"][asset][:cutoff_index + 1]
        mine = diagnostics.drawdowns[asset]
        record.exact(layer, "日数", asset, len(tool_days), len(mine))
        for left, right in zip(tool_days, mine, strict=False):
            where = f"{asset} {left['day']}"
            record.exact(layer, "day", where, left["day"], day_text(right.day))
            record.exact(layer, "d ↔ drawdown（Decimal）", where,
                         None if left.get("d") is None else Decimal(str(left["d"])), right.drawdown)
            record.exact(layer, "h_complete ↔ not is_estimate", where, left["h_complete"], not right.is_estimate)


def compare_leverage(record: Recorder, key: str, tool_nav: Sequence[Mapping] | None,
                     mine: Sequence[research_run.LeverageDiag]) -> None:
    layer = "诊断杠杆因子"
    if tool_nav is None:
        for item in mine:
            record.add(layer, "lev_factor、lev_ok", "未比较", f"{key} {day_text(item.end_day)}", None, item.factor,
                       "工具在窗口内有缺价时不输出 nav")
        return
    by_day = {entry["idx"]: entry for entry in tool_nav}
    record.exact(layer, "区间数", key, len(tool_nav) - 1, len(mine))
    for item in mine:
        where = f"{key} {day_text(item.end_day)}"
        left = by_day.get(day_text(item.end_day), {})
        if item.checked:
            record.number(layer, "lev_factor", where, left.get("lev_factor"), item.factor)
            record.exact(layer, "lev_ok", where, left.get("lev_ok"), item.ok)
        else:
            record.exact(layer, "杠杆权重为 0（工具 lev_factor、lev_ok 为 null）", where,
                         [left.get("lev_factor"), left.get("lev_ok")], [None, None])


def state_text(state: object) -> object:
    if isinstance(state, SystemState):
        return [RISK_NAMES[state.risk], state.c1, state.c2]
    if isinstance(state, Risk):
        return RISK_NAMES[state]
    return state.value                                                             # ChannelState


def compare_enumeration(record: Recorder, where: str, tool_runs: Sequence[Sequence], tool_inits: Sequence,
                        tool_start: str, tool_conv: str | None, mine: research_run.EnumeratedConvergence,
                        axis: Sequence[dt.date]) -> None:
    """工具每条运行自起点（初始状态）至收敛日（含）；项目运行自起点的下一日起到 E。表示换算：
    工具运行 = [初始状态] + 项目逐日状态的前 len − 1 项；工具起点 = axis[local_origin − 1]；
    收敛日 = axis[first_common_global]；工具运行中的收敛位置 = first_common_local + 1。"""
    layer = "诊断收敛枚举"
    # 两侧初始状态的排列顺序不同（工具按其输出顺序，项目按状态域枚举顺序），按初始状态对齐，集合另比。
    tool_by_init = {json.dumps(init, ensure_ascii=False): run for init, run in zip(tool_inits, tool_runs, strict=True)}
    mine_inits = [json.dumps(state_text(run.initial), ensure_ascii=False) for run in mine.runs]
    record.exact(layer, "初始状态集合", where, sorted(tool_by_init), sorted(mine_inits))
    record.exact(layer, "start", where, tool_start, day_text(axis[mine.local_origin - 1]))
    record.exact(layer, "conv_day ↔ first_common_global", where, tool_conv, day_text(axis[mine.first_common_global]))
    for init, run in zip(mine_inits, mine.runs, strict=True):
        if init not in tool_by_init:
            continue
        tool_run = tool_by_init[init]
        expected = [state_text(run.initial), *(state_text(state) for state in run.states[:len(tool_run) - 1])]
        record.exact(layer, "runs（逐日完整状态）", f"{where} {state_text(run.initial)}", list(tool_run), expected)
        record.exact(layer, "收敛位置 ↔ first_common_local + 1", f"{where} {state_text(run.initial)}",
                     len(tool_run) - 1, mine.first_common_local + 1)


def compare_convergence_diag(record: Recorder, tool: Mapping, diagnostics: research_run.Diagnostics,
                             axis: Sequence[dt.date]) -> None:
    for candidate, found in diagnostics.convergence.items():
        key = group_key(candidate)
        group = tool["groups"][key]
        for name in CHANNEL_NAMES:
            conv = group["channel_conv"][name]
            runs = conv["runs"]
            compare_enumeration(record, f"{key} {name}", list(runs.values()), list(runs), conv["start"],
                                conv["conv_day"], found.channels[name], axis)
        system = group["system_conv"]
        compare_enumeration(record, f"{key} 系统", system["runs"], system["inits"], system["start"],
                            system["conv_day"], found.system, axis)
        record.exact("诊断收敛枚举", "same_s_diff_counter_days ↔ same_risk_different_counters", key,
                     list(system["same_s_diff_counter_days"]),
                     [day_text(day) for day in found.same_risk_different_counters])
    conv = tool["reference_conv"]
    runs = {name: [entry["S"] for entry in days] for name, days in conv["runs"].items()}
    compare_enumeration(record, "主参照", list(runs.values()), list(runs), conv["start"], conv["conv_day"],
                        diagnostics.reference, axis)
    for name in conv["runs"]:
        record.add("诊断收敛枚举", "主参照 runs.L", "未比较", f"主参照 {name}",
                   note="项目接口未提供：主参照枚举路径只保存 Risk")


D13_STATUS = "裁决差异（D13-甲）"


def d13_evidence(tool: Mapping, result: research_run.WindowResult) -> dict:
    """D13 的五项条件逐项核实（补充三修订二第零节），返回逐项结果与证据；五项全部成立才记“裁决差异（D13-甲）”。
    1 某资产 R2 事件不可得：项目 Unavailable（labels_r2.r2_events，缺少必需价格），工具 r2_events_error 有该资产；
    2 另一资产的段账因段前信号状态无法确定而停止：exit = 分类无法确定，exception_type = R2Undeterminable，
      stage = R2，object 为候选键或另一资产，message 含“起始日无法判断”，且另一资产的 R2 事件可得；
    3 工具不输出任何组的 ledger；4 工具选择出口为“缺值无法评价”；5 工具没有 stop_reason。"""
    stop, common = result.stop, result.common
    events = dict(common.events) if common is not None else {}
    errors = tool.get("r2_events_error") or {}
    missing = [asset for asset, value in events.items()
               if isinstance(value, research_run.Unavailable) and unavailable_allowed(value, LABELS_UNAVAILABLE)
               and asset in errors]
    others = [asset for asset, value in events.items() if not isinstance(value, research_run.Unavailable)]
    candidate_keys = {research_run.candidate_key(c) for c in research_run.REGISTERED_CANDIDATES}
    stop_ok = (stop is not None and stop.exit is research_run.Exit.UNDETERMINABLE
               and stop.exception_type == "R2Undeterminable" and stop.stage == "R2"
               and (stop.object in candidate_keys or stop.object in others) and "起始日无法判断" in stop.message)
    groups = tool.get("groups") or {}
    items = {
        "1 某资产 R2 事件不可得（项目 labels_r2 缺价、工具 r2_events_error）": bool(missing),
        "2 另一资产段账段前状态无法确定而停止（R2Undeterminable，阶段 R2，起始日无法判断）": stop_ok and bool(others),
        "3 工具不输出任何组的 ledger": bool(groups) and not any("ledger" in group for group in groups.values()),
        "4 工具选择出口为缺值无法评价": (tool.get("selection") or {}).get("exit") == "缺值无法评价",
        "5 工具没有 stop_reason": "stop_reason" not in tool}
    return {"成立": all(items.values()), "逐项": items,
            "证据": {"项目 R2 事件不可得资产": missing, "项目 R2 事件可得资产": others,
                     "项目停止": None if stop is None else {
                         "exit": stop.exit.value, "exception_type": stop.exception_type, "stage": stop.stage,
                         "object": stop.object, "message": stop.message},
                     "工具 r2_events_error 资产": sorted(errors), "工具含 ledger 的组数": sum(
                         1 for group in groups.values() if "ledger" in group),
                     "工具选择出口": (tool.get("selection") or {}).get("exit"),
                     "工具 stop_reason": tool.get("stop_reason")}}


TOOL_UNDETERMINABLE = "提示段起始状态无法确定"         # 工具结构化 stop_reason 的原因字段（退出码 3）


def compare_research(record: Recorder, tool: Mapping, result: research_run.WindowResult,
                     chosen: research_run.DevelopmentSelection | None, axis: Sequence[dt.date],
                     cutoff_index: int, source_label: str | None = None) -> None:
    """映射表第 1 至 11 项。组合层停止时只比停止原因（第 7 项），不继续其他层。
    source_label 为第三轮工具输出来源（_1 / _2，未设第三轮来源时为 None）：第三轮另按修订一第一节第 3 条，
    工具 stop_reason 的原因为“提示段起始状态无法确定”或项目“分类无法确定”任一侧出现即记不一致。"""
    if source_label is not None:
        tool_reason = (tool.get("stop_reason") or {}).get("reason") if isinstance(tool.get("stop_reason"),
                                                                                  Mapping) else None
        project_undeterminable = result.stop is not None and result.stop.exit is research_run.Exit.UNDETERMINABLE
        if tool_reason == TOOL_UNDETERMINABLE or project_undeterminable:
            record.add("停止原因（组合层）", "第三轮：提示段起始状态无法确定 / 分类无法确定（D14 B 下不应出现）",
                       "不一致", "场景", tool_reason, None if result.stop is None else result.stop.exit.value)
        if result.stop is None and "stop_reason" in tool:
            record.add("停止原因（组合层）", "第三轮：工具 stop_reason 而项目未停止", "不一致", "场景",
                       tool.get("stop_reason"), None)
    if result.stop is not None:
        stop = tool.get("stop_reason") or {}
        # 三要素分别映射（《A2 修订二》第三节第 7 项）：出口、原因码、下标。只换算映射表内的出口与原因码，
        # 表外的（如计算失败、未预期异常）按项目原值比较，不自动对应为工具的正常停止。
        # D13（补充三修订二第零节）：五项条件逐项核实全部成立时，只豁免出口差异，记“裁决差异（D13-甲）”。
        d13 = d13_evidence(tool, result) if "stop_reason" not in tool else None
        if d13 is not None and d13["成立"]:
            record.add("停止原因（组合层）", "exit ↔ stop_reason.reason", D13_STATUS, "场景", None,
                       result.stop.exit.value, json.dumps(d13, ensure_ascii=False))
        else:
            record.exact("停止原因（组合层）", "exit ↔ stop_reason.reason", "场景", stop.get("reason"),
                         EXIT_TO_TOOL.get(result.stop.exit, result.stop.exit.value))
            if d13 is not None:
                record.add("停止原因（组合层）", "D13 五项核实（未全部成立）", "未比较", "场景", None, None,
                           json.dumps(d13, ensure_ascii=False))
        record.exact("停止原因（组合层）", "reason_code ↔ stop_reason.reason", "场景", stop.get("reason"),
                     STOP_REASONS.get(result.stop.reason_code, result.stop.reason_code))
        if result.stop.exit is research_run.Exit.EMPTY_WINDOW:
            record.exact("停止原因（组合层）", "detail ↔ sub_reason", "场景", stop.get("sub_reason"),
                         EMPTY_WINDOW_DETAILS.get(result.stop.detail.get("detail")))
            record.add("停止原因（组合层）", "j0_index、E_index", "未比较", "场景",
                       [stop.get("j0_index"), stop.get("E_index")], None,
                       "项目接口未提供：StopRecord.detail 只含评价窗口为空的细分")
        return
    for layer, item in SUPERSEDED:
        record.drop(layer, item)
    common, reference, diagnostics = result.common, result.reference, result.diagnostics
    assert common is not None and reference is not None and diagnostics is not None
    compare_r2_events(record, tool, common.events)
    premise = availability_premise(tool, common.events)
    for candidate, outcome in result.candidates.items():
        key = group_key(candidate)
        group = tool["groups"][key]
        compare_r2_judgements(record, key, group, outcome.r2, premise)
        compare_ledgers(record, key, group, outcome.ledgers, premise, source_label)
        compare_r1(record, key, group, outcome.r1)
        compare_leverage(record, key, group["signal_sim"].get("nav"), diagnostics.leverage[repr(candidate)])
    compare_records_and_selection(record, tool, result, chosen)
    compare_exec_sim(record, "主参照", tool["reference"]["exec_sim"],
                     policy_fields(reference.outcome.policy, common.prefix.values), reference_exec_signals(reference),
                     "主参照执行政策研究模拟")
    compare_leverage(record, "主参照", tool["reference"]["signal_sim"].get("nav"), diagnostics.leverage["reference"])
    compare_drawdowns(record, tool, diagnostics, cutoff_index)
    compare_convergence_diag(record, tool, diagnostics, axis)


# ---------------------------------------------------------------------------
# 第三轮（A3）：混合来源与工具绑定（《A2 第三轮补充一》第二节）。
# 只做来源选择与绑定，不改任何映射、容差、字段组或状态判定。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolBinding:
    """生成某份工具输出的工具：工作树、提交与 audit_v20.py 的 SHA-256。"""

    root: str
    commit: str
    audit_sha256: str


OLD_TOOL = ToolBinding(r"C:\Users\stone\v20_tool_a161387", "a161387fb13954eaa117ba3970e425bb98d244d5",
                       "40abfb78281a4af443618ed384415ae001169720f3d6b256a80add9af27a74c3")
NEW_TOOL = ToolBinding(r"C:\Users\stone\v20_tool_1709880", "170988007994134577296e9931bba4cd1786cd36",
                       "8ff8081828c94134bc0d3e000ea77f3442564241438c8d444c601003bb19ca3d")
NEW_TOOL_VERSION = "v20-indep-3"          # 工具 README（1709880）第 101 行：“`tool`：工具版本（本版 `v20-indep-3`）”
SOURCE_SECOND, SOURCE_THIRD = "_2", "_1"                 # 工具输出来源：第二次运行目录 / 第三轮目录


class ToolSourceError(AssertionError):
    """混合来源不满足：清单缺失、哈希不符、或某场景在两处都有 / 都无输出。不回退、不猜测。"""


@dataclass(frozen=True)
class ToolSource:
    """某场景工具输出的来源：来源标签、相对来源目录的路径、应有的 SHA-256 与生成工具。"""

    label: str
    relative: str
    sha256: str
    binding: ToolBinding


def affected_names(listing_text: str | None) -> tuple[str, ...]:
    """受影响场景清单（_1\\导出\\受影响场景清单.md）表格第一列的场景名；清单缺失即失败。"""
    if listing_text is None:
        raise ToolSourceError("受影响场景清单缺失")
    names = []
    for line in listing_text.splitlines():
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(parts) == 3 and parts[2].startswith("`") and parts[0] != "场景":
            names.append(parts[0])
    if not names or len(set(names)) != len(names):
        raise ToolSourceError("受影响场景清单为空或有重复")
    return tuple(names)


def rerun_hashes(rerun: Mapping | None) -> dict[str, str]:
    """工具重跑记录（_1\\检查记录\\工具重跑记录.json）：场景名 → 新工具输出 SHA-256；须由新工具生成且退出码 0 或 3。"""
    if rerun is None:
        raise ToolSourceError("工具重跑记录缺失")
    if (rerun.get("tool_commit"), rerun.get("audit_sha256")) != (NEW_TOOL.commit, NEW_TOOL.audit_sha256):
        raise ToolSourceError("工具重跑记录的工具提交或 audit_v20.py 哈希与新工具绑定不符")
    result = {}
    for run in rerun.get("runs") or ():
        if run.get("exit_code") not in (0, 3) or not run.get("output_sha256") or run["name"] in result:
            raise ToolSourceError(f"工具重跑记录中 {run.get('name')} 的退出码、哈希或唯一性不符")
        result[run["name"]] = run["output_sha256"]
    return result


def choose_tool_source(name: str, affected: Sequence[str], rerun: Mapping[str, str], third_files: set[str],
                       second_listing: Mapping[str, tuple[int, str]]) -> ToolSource:
    """按《A2 第三轮补充一》第二节第 2 条选择工具输出来源：受影响场景 → _1（按重跑记录核对，绑定新工具）；
    其余 → _2（按 _2 清单核对，绑定旧工具）。受影响清单与重跑记录的场景集合须相同；受影响场景须在 _1 有输出，
    其余场景须在 _1 无输出且在 _2 清单内（“两处都有 / 都无”即失败）。"""
    if set(affected) != set(rerun):
        raise ToolSourceError("受影响场景清单与工具重跑记录的场景集合不同")
    relative = f"工具输出/{name}.json"
    if name in affected:
        if name not in third_files:
            raise ToolSourceError(f"{name} 是受影响场景，但 _1 没有工具输出（两处都无）")
        return ToolSource(SOURCE_THIRD, relative, rerun[name], NEW_TOOL)
    if name in third_files:
        raise ToolSourceError(f"{name} 不是受影响场景，但 _1 有工具输出（两处都有）")
    if relative not in second_listing:
        raise ToolSourceError(f"{name} 不在 _2 清单中（两处都无）")
    return ToolSource(SOURCE_SECOND, relative, second_listing[relative][1], OLD_TOOL)


def checked_tool_bytes(source: ToolSource, data: bytes) -> bytes:
    """核对工具输出字节的 SHA-256 与所选来源应有的哈希；不符即失败。"""
    if sha256(data) != source.sha256:
        raise ToolSourceError(f"{source.relative}（来源 {source.label}）的 SHA-256 与清单或重跑记录不符")
    return data


def tool_source_fields(source: ToolSource) -> dict:
    """逐场景记录新增的四个字段（第二节第 4 条）。"""
    return {"工具输出来源": source.label, "工具输出 SHA-256": source.sha256, "生成工具提交": source.binding.commit,
            "生成工具 audit_v20.py SHA-256": source.binding.audit_sha256}


# ---------------------------------------------------------------------------
# 第二轮 B：确认性检验的构造场景比对（《第二轮 B 设计（修订二）》SHA-256 bbaf96b0…665d；《接线 B 试跑》950cadff…7240）。
# 只做表示换算与比较：不重实现置零、净值、自助法；工具未输出的量记未比较。
# ---------------------------------------------------------------------------

B_PERIOD = "构造验收"
B_LATER_SCENARIO = "confirm_空窗口_later"
B_LATER_MESSAGE = "构造验收给出的固定起点须在轴上"
B_NUMBER_TOLERANCE = 1e-12                  # 设计第二节第 4 项：精确不等且 |差| ≤ 1e-12 记“容差内”
B_TOOL_INVALID_CONCLUSIONS = ("计算无效", "计算无效，不写任何优劣结论")
B_TOOL_REASON_MAIN = "主设定 n ÷ b < 2"         # 设计第二节第 2 项：工具侧登记文字（↔ 项目“主设定无效：n ÷ b < 2…”）
B_TOOL_REASON_DJ = "d_j 出现非有限值"           # 设计第二节第 2 项：工具侧与项目同名
B_WARNINGS = {"敏感性区块下 p ≥ 0.10": confirmatory.WARNING_SENSITIVITY,
              "前后两半的 Δ 方向不一致": confirmatory.WARNING_HALVES,
              "事件窗口置零后不再为正": confirmatory.WARNING_ZEROING}
B_REQUIRED_STOP = ("tool", "kind", "name", "exit_code", "stop_reason")
B_REQUIRED_INVALID = ("tool", "kind", "name", "exit_code", "input_checks", "registered_settings", "params", "window",
                      "r2", "valid", "invalid_reasons", "category", "conclusion")
B_OPTIONAL_INVALID = ("n", "delta", "delta_min", "annual_relative_growth", "bootstrap", "r1", "r2_ok", "unstable",
                      "warnings", "judgement_test_pass", "judgement_point_estimate", "halves", "event_zeroing",
                      "own_loss_text", "lnW")
B_TOOL_EARLY = "未比较（工具提前返回未输出）"
B_PROJECT_SHORT = "未比较（项目无效分支短路未计算）"
B_NOT_PROVIDED = "未比较（项目接口未提供）"
B_UNREGISTERED_EXIT = "未登记的出口组合"


def confirm_parameters(split: dt.date) -> confirmatory.ConfirmatoryParameters:
    """设计第一节登记值：主设定 (20, 20261020)；敏感性 (10, 20261010)、(40, 20261040)、(60, 20261060)、
    (120, 202610120)；B = 10,000；α = 0.05；警示线 0.10；年化 252；最低改善 1%；容差 1e-10；padding 20。"""
    block = confirmatory.BlockSetting
    return confirmatory.ConfirmatoryParameters(
        block(20, 20261020), (block(10, 20261010), block(40, 20261040), block(60, 20261060), block(120, 202610120)),
        10_000, 0.05, 0.10, 252, 0.01, 1e-10, 20, split)


def confirm_run_parameters() -> research_run.RunParameters:
    """组合层运行参数：与 A 第二轮相同的登记值，诊断关闭（设计第一节）。"""
    return research_run.RunParameters(WINDOWS, POSITIONS, POLICY, A2_THRESHOLDS, A2_RULE, COMMON_START_OFFSET,
                                      TOLERANCE, A2_R1_RATIO, (), False)


def confirm_candidate(scenario: Mapping) -> Candidate:
    k, theta, h = scenario["params"]
    return Candidate(int(k), Decimal(str(theta)), int(h))


def b_float(value: object) -> object:
    """补充一第一节第 1 条：工具数值按 A 的约定读成 Decimal，比较前先 float()（与 A 的 record.number 同法）。"""
    return float(value) if isinstance(value, Decimal) else value


def b_number(record: Recorder, layer: str, item: str, where: str, tool: object, project: object) -> str:
    """设计第二节第 4 项的容差规则：精确相等一致；不等且 |差| ≤ 1e-12 记“容差内”（单列，不计一致）；否则不一致。
    比较前工具值 Decimal → float（补充一第一节第 1 条）。"""
    tool, project = b_float(tool), b_float(project)
    if tool == project:
        status = "一致"
    elif (isinstance(tool, int | float) and isinstance(project, int | float) and not isinstance(tool, bool)
          and math.isfinite(tool) and math.isfinite(project) and abs(tool - project) <= B_NUMBER_TOLERANCE):
        status = TOLERANCE_STATUS
    else:
        status = "不一致"
    record.add(layer, item, status, where, tool, project)
    return status


def b_premise(tool: Mapping, events: Mapping) -> dict[str, bool]:
    """confirm 专用不可得前提 P_B(a)（设计第二节第 9 项）：项目 common.events[a] 为
    Unavailable(labels_r2.r2_events, 缺少必需价格) ∧ 工具 invalid_reasons 含 {object: "R2 a", reason: 缺少必需价格}
    且 missing_days 逐日相同 ∧ 工具 r2 无键 a。三条齐备才成立；不凭缺键单独认定。"""
    reasons = tool.get("invalid_reasons") or []
    tool_r2 = tool.get("r2") or {}
    premise = {}
    for asset in ASSETS:
        mine = events.get(asset)
        listed = (isinstance(mine, research_run.Unavailable) and unavailable_allowed(mine, LABELS_UNAVAILABLE)
                  and any(isinstance(item, Mapping) and item.get("object") == f"R2 {asset}"
                          and item.get("reason") == "缺少必需价格"
                          and item.get("missing_days") == [day_text(day) for _, day in mine.missing]
                          for item in reasons))
        premise[asset] = bool(listed) and asset not in tool_r2
    return premise


def b_note_class(note: str | None) -> str | None:
    """自助法行的无效类别（设计第二节第 5 项“note 只比无效类别”）。"""
    if not note:
        return None
    for name in ("n ÷ b < 2", "重抽样过程出错"):
        if name in note:
            return name
    return note


def b_tool_missing(entry: Mapping) -> list | None:
    """工具原因条目的缺价日期：missing_days（日期列表）或 missing（[资产, 日期] 列表），逐日比较。"""
    if "missing_days" in entry:
        return list(entry["missing_days"])
    if "missing" in entry:
        return [list(item) for item in entry["missing"]]
    return None


def b_expected_reasons(project_reason: str, nav_missing: Sequence[tuple[str, dt.date]], events: Mapping,
                       r2: Mapping) -> list[tuple[str, object]] | None:
    """项目短路原因 → 工具应列出的（判定函数, 说明）。设计第二节第 2 项的类别映射；表外原因返回 None。"""
    if project_reason == "净值所需价格缺失，收益无法计算":
        days = [day_text(day) for _, day in nav_missing]
        pairs = [[asset, day_text(day)] for asset, day in nav_missing]
        return [(lambda e, d=days, p=pairs: e.get("reason") == "缺少必需价格" and e.get("object") == "净值"
                 and b_tool_missing(e) in (d, p), f"缺少必需价格 / 净值 / {days}")]
    if project_reason == "R2 无法计算":
        expected = []
        for asset in ASSETS:
            mine = events.get(asset)
            if isinstance(mine, research_run.Unavailable) and unavailable_allowed(mine, LABELS_UNAVAILABLE):
                days = [day_text(day) for _, day in mine.missing]
                expected.append((lambda e, a=asset, d=days: e.get("reason") == "缺少必需价格"
                                  and e.get("object") == f"R2 {a}" and b_tool_missing(e) == d,
                                  f"缺少必需价格 / R2 {asset} / {days}"))
            elif r2.get(asset) is not None and not r2[asset].computable:
                expected.append((lambda e, a=asset: e.get("reason") == "R2 无法计算（非左截断事件为 0 个）"
                                 and e.get("object") == f"R2 {a}", f"R2 无法计算（非左截断事件为 0 个）/ R2 {asset}"))
        return expected or None
    # 《接线 B 全量 补充一》第一节第 1、2 条：工具这两类原因条目不带 object；工具 reason 须精确等于登记的工具侧文字
    # （设计第二节第 2 项），不是等于项目原因文字；条目无 object 键才匹配。
    if project_reason.startswith("主设定无效：") and "n ÷ b < 2" in project_reason:
        return [(lambda e: e.get("reason") == B_TOOL_REASON_MAIN and "object" not in e,
                 f"{B_TOOL_REASON_MAIN}（工具侧登记文字、无 object）↔ {project_reason}")]
    if project_reason == "d_j 出现非有限值":
        return [(lambda e: e.get("reason") == B_TOOL_REASON_DJ and "object" not in e,
                 f"{B_TOOL_REASON_DJ}（工具侧登记文字、无 object）")]
    return None


def compare_b_reasons(record: Recorder, tool_reasons: Sequence[Mapping], project_reason: str,
                      nav_missing: Sequence[tuple[str, dt.date]], events: Mapping, r2: Mapping) -> None:
    """设计第二节第 2 项：项目短路只报一条；把项目原因映射为工具（类别, 对象[, 缺价日期]），在工具列表中找到全同的
    一条才记一致；工具其余原因逐条“未比较（工具多报原因，项目短路未检查）”；找不到即不一致。对账不符待裁决，不记一致。"""
    layer = "检验层"
    if project_reason.startswith("对账不符"):
        record.add(layer, "对账不符（待裁决口径，实际发生即停）", "不一致", "invalid_reasons", tool_reasons,
                   project_reason)
        return
    expected = b_expected_reasons(project_reason, nav_missing, events, r2)
    if expected is None:
        record.add(layer, "invalid_reasons（项目原因不在映射表内）", "不一致", "invalid_reasons", tool_reasons,
                   project_reason)
        return
    used: set[int] = set()
    for predicate, label in expected:
        found = next((index for index, entry in enumerate(tool_reasons)
                      if index not in used and isinstance(entry, Mapping) and predicate(entry)), None)
        if found is None:
            record.add(layer, "invalid_reasons（项目原因在工具列表中找不到）", "不一致", label, tool_reasons,
                       project_reason)
        else:
            used.add(found)
            record.add(layer, "invalid_reasons（类别、对象、缺价日期全同）", "一致", label, tool_reasons[found],
                       project_reason)
    for index, entry in enumerate(tool_reasons):
        if index not in used:
            record.add(layer, "invalid_reasons（工具多报原因）", "未比较", str(entry.get("object")), entry, None,
                       "未比较（工具多报原因，项目短路未检查）")


def b_start_facts(snapshot: Snapshot, candidate: Candidate) -> dict:
    """D15 与未收敛情形 b 的判定依据：t0 与 κ（单候选 + 主参照），用项目已有函数计算；未收敛记 None。"""
    spx, qqq = (snapshot_asset_days(snapshot, asset, WINDOWS) for asset in ASSETS)
    ma = snapshot_trend_days(snapshot, "SPX", WINDOWS)
    try:
        t0 = first_valid_index(spx, qqq, ma)
    except ConvergenceError as error:
        return {"t0": None, "kappa": None, "error": f"{type(error).__name__}：{error}"}
    try:
        system = candidate_convergence(spx, qqq, ma, t0, candidate, WINDOWS.average).system_index
        reference = t0 + 1 + reference_convergence(ma[t0 + 1:], WINDOWS.average)
    except ConvergenceError as error:
        return {"t0": t0, "kappa": None, "error": f"{type(error).__name__}：{error}"}
    return {"t0": t0, "kappa": max(system, reference), "error": None}


def compare_b_exit(record: Recorder, tool: Mapping, stop: research_run.StopRecord | None, error: str | None,
                   axis: Sequence[dt.date], j0: int | None, facts: Mapping | None) -> str:
    """设计第三节出口表。返回登记情形名；未登记的组合记不一致（触发停止）。双方均未停止时返回“双方均完成”。"""
    layer = "出口"
    reason_info = tool.get("stop_reason") if isinstance(tool.get("stop_reason"), Mapping) else {}
    reason, sub = reason_info.get("reason"), reason_info.get("sub_reason")
    tool_stopped = tool.get("exit_code") == 3
    project = None if stop is None else stop.exit.value
    pair = [reason if tool_stopped else f"退出码 {tool.get('exit_code')}", error or project]
    e_index = len(axis) - 1
    if not tool_stopped and stop is None and error is None:
        record.add(layer, "exit_code（双方均完成）", "一致" if tool.get("exit_code") == 0 else "不一致", "场景",
                   tool.get("exit_code"), "完成")
        return "双方均完成"
    if tool_stopped and reason == "评价窗口为空" and sub == "起点等于最后一个收盘日" and (
            stop is not None and stop.exit is research_run.Exit.FIXED_START_UNMET and j0 == e_index):
        case = "window_start == E：工具评价窗口为空 ↔ 项目固定窗口不满足预热或收敛条件"
    elif tool_stopped and reason == "评价窗口为空" and sub == "起点晚于最后一个收盘日" and error == B_LATER_MESSAGE:
        case = "window_start > E：工具评价窗口为空 ↔ 项目 ResearchRunError（构造验收给出的固定起点须在轴上）"
    elif tool_stopped and reason == "未收敛" and stop is not None and stop.exit is research_run.Exit.NOT_CONVERGED:
        case = "未收敛情形 a：始终不收敛"
    elif (tool_stopped and reason == "未收敛" and stop is not None
          and stop.exit is research_run.Exit.FIXED_START_UNMET and facts is not None
          and facts.get("kappa") is not None and j0 is not None and j0 - 1 < facts["kappa"]):
        case = "未收敛情形 b：最终已收敛，但 j0 − 1 早于收敛日"
    elif (not tool_stopped and tool.get("exit_code") == 0 and stop is not None
          and stop.exit is research_run.Exit.FIXED_START_UNMET and facts is not None
          and facts.get("t0") is not None and facts.get("kappa") is not None and j0 is not None
          and facts["kappa"] + 1 <= j0 < facts["t0"] + COMMON_START_OFFSET):
        record.add(layer, "预热不足（D15，口径未统一；本批未核验真实验证期是否触发）", D15_STATUS, "场景",
                   "正常运行", project, json.dumps({"t0": facts["t0"], "kappa": facts["kappa"], "j0": j0}))
        return "D15"
    else:
        record.add(layer, B_UNREGISTERED_EXIT, "不一致", "场景", {"exit_code": tool.get("exit_code"), **reason_info},
                   pair[1], json.dumps({"facts": facts, "j0": j0}, ensure_ascii=False, default=str))
        return B_UNREGISTERED_EXIT
    record.add(layer, case, "接口差异", "场景", {"exit_code": tool.get("exit_code"), **reason_info}, pair[1],
               "" if stop is None else json.dumps({"exception_type": stop.exception_type,
                                                   "detail": dict(stop.detail)}, ensure_ascii=False, default=str))
    return case


def b_alignment(scenario: Mapping, axis: Sequence[dt.date], j0: int, data: confirmatory.ConfirmatoryInput,
                split: dt.date, tool: Mapping) -> dict[str, bool | None]:
    """设计第一节对齐断言（第 1—4 条）。工具提前返回而未产生的字段（含 n）记 None（未比较），不强行读取。"""
    window = tool.get("window") or {}
    start = dt.date.fromisoformat(scenario["window_start"])
    n = len(axis) - 1 - j0
    end_days = tuple(data.end_days)
    before = sum(1 for day in end_days if day < split)
    checks: dict[str, bool | None] = {
        "1 axis[j0] == window_start": axis[j0] == start,
        "1 工具 window.start == window_start": window.get("start") == scenario["window_start"],
        "2 项目 first_signal_day == axis[j0−1]": data.first_signal_day == axis[j0 - 1],
        "2 工具 window.first_signal_day == axis[j0−1]": window.get("first_signal_day") == day_text(axis[j0 - 1]),
        "3 项目 end_days == axis[j0+1 .. E]": end_days == tuple(axis[j0 + 1:]),
        "3 项目 n == E_index − j0": len(end_days) == n,
        "3 工具 n == 项目 n": None if "n" not in tool else tool["n"] == n,
        "3 工具 window.E == axis[E]": window.get("E") == day_text(axis[-1]),
        "4 split == half_split": split == dt.date.fromisoformat(scenario["half_split"]),
        "4 前后两半区间数 n // 2 与 n − n // 2": (before, len(end_days) - before) == (n // 2, n - n // 2),
        "4 工具 window.half_split == half_split": window.get("half_split") == scenario["half_split"]}
    return checks


def b_pair(record: Recorder, layer: str, item: str, tool: Mapping, key: str, project: object, tool_valid: object,
           project_valid: bool) -> tuple[bool, object]:
    """设计第二节第 1 小节的两阶段规则：返回（是否可比，工具值）。应存在而缺键 → 不一致；可不存在而缺 → 未比较
    （工具提前返回）；工具已算而项目短路为 None → 未比较（项目无效分支短路未计算）。
    不设“项目 None + 工具缺键 → 一致”。"""
    if key not in tool:
        if tool_valid is True:
            record.add(layer, f"{item}（缺键）", "不一致", key, None, project)
        else:
            record.add(layer, item, "未比较", key, None, project, B_TOOL_EARLY)
        return False, None
    if project is None and not project_valid:
        record.add(layer, item, "未比较", key, tool[key], None, B_PROJECT_SHORT)
        return False, tool[key]
    return True, tool[key]


def compare_b_zeroing(record: Recorder, tool_items: Sequence[Mapping], zeroed: Sequence[confirmatory.ZeroedEvent],
                      events: Mapping, first_signal_day: dt.date) -> None:
    """设计第二节第 13 项：先核对两侧事件集合（资产、P、Tr）；Tr 取窗口结果中同 P 事件的 trough；
    P ≤ f 的事件两侧均不出现；delta_zeroed 容差规则；not_positive ↔ delta ≤ 0。
    置零范围工具未输出，不在接线中重实现。"""
    layer = "检验层"
    troughs = {(asset, day_text(event.peak)): day_text(event.trough)
               for asset, items in events.items() if not isinstance(items, research_run.Unavailable) for event in items}
    mine = {(item.asset, day_text(item.peak), troughs.get((item.asset, day_text(item.peak)))): item for item in zeroed}
    theirs = {(item.get("asset"), item.get("P"), item.get("Tr")): item for item in tool_items}
    record.exact(layer, "event_zeroing 事件集合（资产、P、Tr）", "场景", sorted(map(list, theirs), key=str),
                 sorted(map(list, mine), key=str))
    record.check(layer, "event_zeroing 无 P ≤ f 的事件", all(str(key[1]) > day_text(first_signal_day) for key in theirs)
                 and all(key[1] > day_text(first_signal_day) for key in mine), "场景",
                 sorted(str(key[1]) for key in theirs), sorted(key[1] for key in mine))
    for key in sorted(set(theirs) & set(mine), key=str):
        where = " ".join(map(str, key))
        b_number(record, layer, "event_zeroing.delta_zeroed", where, theirs[key].get("delta_zeroed"), mine[key].delta)
        record.exact(layer, "event_zeroing.not_positive", where, theirs[key].get("not_positive"), mine[key].delta <= 0)
    record.add(layer, "event_zeroing 置零范围", "未比较", "场景", None, None,
               "工具范围未由输出核验（按共同轴与两侧源码契约：交易日轴 ±20、截在窗口内）")


def compare_b_bootstrap(record: Recorder, tool_rows: Sequence[Mapping],
                        rows: Sequence[confirmatory.BootstrapRow]) -> None:
    """设计第二节第 5—7 项：按 b 对应；b、seed、valid 精确；note 只比无效类别；p 精确；q025、q975 容差规则；
    tail_count、delta_star、tail 未比较。"""
    layer = "检验层"
    theirs = {item.get("b"): item for item in tool_rows}
    numeric = lambda value: math.inf if value is None else float(value)          # noqa: E731  按数值排序（补充一）
    record.exact(layer, "bootstrap 行（b 集合）", "场景", sorted(theirs, key=numeric),
                 sorted((row.block for row in rows), key=numeric))
    for row in rows:
        left = theirs.get(row.block)
        if left is None:
            continue
        where = f"b={row.block}"
        record.exact(layer, "bootstrap.seed", where, left.get("seed"), row.seed)
        record.exact(layer, "bootstrap.valid", where, left.get("valid"), row.valid)
        if not row.valid or not left.get("valid"):
            record.exact(layer, "bootstrap.note（无效类别）", where, b_note_class(left.get("note")),
                         b_note_class(row.note))
        if row.valid and left.get("valid"):
            record.exact(layer, "bootstrap.p（精确）", where, b_float(left.get("p")), row.p_value)   # 换算后仍精确
            b_number(record, layer, "bootstrap.q025", where, left.get("q025"), row.low)
            b_number(record, layer, "bootstrap.q975", where, left.get("q975"), row.high)
        for name in ("tail_count", "delta_star", "tail"):
            if name in left:
                record.add(layer, f"bootstrap.{name}", "未比较", where, None, None, B_NOT_PROVIDED)


B_PERCENT = re.compile(r"[+-]?\d+(?:\.\d+)?(?=%)")
B_N_TEXT = re.compile(r"n = (\d+)")
B_TEXT_UNREGISTERED = "未比较（工具文字格式未登记）"


def b_tool_own_loss(text: str, tool: Mapping) -> tuple[str, list]:
    """补充一第一节第 5 条工具侧：按出现顺序取所有“数字%”，须恰为 4 个，依次对应候选、参照、一直持有的自身收益
    (exp(lnW) − 1)·100 与年化相对净值增长率 annual_relative_growth·100（顺序取自工具实际输出，未见登记文档）；
    每个数字须等于本侧数值按文字所示小数位四舍五入的结果；“n = ”后的整数须等于工具 n。
    返回（状态, 明细）：取不出 4 个数字或无 n → 未比较；取出但不符 → 不一致；全部相符 → 一致。"""
    numbers = B_PERCENT.findall(text)
    found_n = B_N_TEXT.search(text)
    if len(numbers) != 4 or found_n is None:
        return "未比较", [numbers, None if found_n is None else found_n.group(1)]
    lnw = tool.get("lnW") or {}
    values = [math.expm1(float(lnw.get(name))) * 100 if lnw.get(name) is not None else None
              for name in ("candidate", "reference", "hold")]
    growth = tool.get("annual_relative_growth")
    values.append(None if growth is None else float(growth) * 100)
    checks = []
    for shown, value in zip(numbers, values, strict=True):
        places = len(shown.split(".")[1]) if "." in shown else 0
        rounded = None if value is None else Decimal(repr(value)).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
        checks.append([shown, None if rounded is None else str(rounded), rounded is not None
                       and Decimal(shown) == rounded])
    n_ok = int(found_n.group(1)) == tool.get("n")
    checks.append(["n = " + found_n.group(1), tool.get("n"), n_ok])
    return ("一致" if all(item[2] for item in checks) else "不一致"), checks


def compare_b_own_loss(record: Recorder, tool_text: object, tool: Mapping, project_text: str | None,
                       project_expected: str | None) -> None:
    """设计第二节第 15 项与补充一第一节第 5 条：null 与否、“相对参照少亏”有无精确比较；项目侧文字与
    own_result_text 重算逐字相等；工具侧文字中的数字与工具本侧数值核对。不互比两侧舍入文字。"""
    layer = "检验层"
    record.exact(layer, "own_loss_text 是否为 null", "own_loss_text", tool_text is None, project_text is None)
    if tool_text is None or project_text is None:
        return
    record.exact(layer, "own_loss_text “相对参照少亏”有无", "own_loss_text", "相对参照少亏" in str(tool_text),
                 "相对参照少亏" in project_text)
    record.exact(layer, "own_result ↔ own_result_text 重算（项目侧）", "own_result", project_expected, project_text)
    status, checks = b_tool_own_loss(str(tool_text), tool)
    record.add(layer, "own_loss_text 文字与工具本侧数值（工具侧）", status, "own_loss_text", str(tool_text), checks,
               B_TEXT_UNREGISTERED if status == "未比较" else "")


def b_accept_error(name: str, message: str) -> bool:
    """设计第六节第 6 条：只有 confirm_空窗口_later 允许捕获 ResearchRunError，且消息须恰为登记原文。"""
    return name == B_LATER_SCENARIO and message == B_LATER_MESSAGE


def b_invalid_conclusion_ok(tool_text: object, project_text: str) -> bool:
    """设计第二节第 14 项：无效结果的列明映射——工具“计算无效”或“计算无效，不写任何优劣结论”↔ 项目“计算无效”。"""
    return tool_text in B_TOOL_INVALID_CONCLUSIONS and project_text == confirmatory.INVALID


def b_mapped_warnings(tool_texts: Sequence[str]) -> list[str]:
    """设计第二节第 11 项：工具警示文字按三条映射换为项目常量；表外文字原样保留（集合比较时即不相等）。"""
    return sorted(B_WARNINGS.get(text, text) for text in tool_texts)


B_STOP_REQUIRED = ("tool", "kind", "name", "exit_code", "stop_reason.reason", "stop_reason.detail")
B_STOP_OPTIONAL = ("sub_reason", "n", "j0_index", "E_index")


def compare_b_common_top(record: Recorder, tool: Mapping, scenario: Mapping) -> None:
    """《接线 B 全量》第一节第 1 条：共同顶层键在停止／完成分支之前比较，所有场景都记状态——tool 与登记值
    NEW_TOOL_VERSION 精确（补充一第一节第 3 条）；kind、name 与场景精确。缺键即不一致。"""
    for key, expected in (("tool", NEW_TOOL_VERSION), ("kind", scenario["kind"]), ("name", scenario["name"])):
        if key in tool:
            record.exact("顶层键", key, key, tool[key], expected)
        else:
            record.add("顶层键", f"{key}（缺键）", "不一致", key, None, expected)


def compare_b_stop_keys(record: Recorder, tool: Mapping) -> dict:
    """《接线 B 全量》第一节第 2 条：停止类（退出码 3）工具输出必须有 tool、kind、name、exit_code、
    stop_reason{reason, detail}，缺任一记不一致（缺键）；sub_reason、n、j0_index、E_index 为可有键，有则返回原值供记录。
    不对停止类要求 params、registered_settings、input_checks 等；不改接口差异映射。"""
    for path in B_STOP_REQUIRED:
        record.check("顶层键", f"停止类必需键 {path}", b_has(tool, path), path, b_has(tool, path), True)
    reason = tool.get("stop_reason") if isinstance(tool.get("stop_reason"), Mapping) else {}
    return {key: reason[key] for key in B_STOP_OPTIONAL if key in reason}


def compare_b_top(record: Recorder, tool: Mapping, scenario: Mapping, candidate: Candidate) -> None:
    """设计第二节第 2 小节顶层键中完成类专有的两项：params 精确；registered_settings 须为 true，否则不一致。
    tool、kind、name 已由 compare_b_common_top 在分支之前比较。"""
    record.exact("顶层键", "params", "params", tool.get("params"), group_key(candidate))
    record.exact("顶层键", "registered_settings", "registered_settings", tool.get("registered_settings"), True)


def compare_b_confirm(record: Recorder, tool: Mapping, scenario: Mapping, candidate: Candidate,
                      result: research_run.WindowResult, data: confirmatory.ConfirmatoryInput,
                      outcome: confirmatory.ConfirmatoryResult, premise: Mapping[str, bool]) -> None:
    """双方均完成时的检验层比较（设计第二节第 2、3 小节）。"""
    layer = "检验层"
    tool_valid = tool.get("valid")
    required = B_REQUIRED_INVALID if tool_valid is False else (*B_REQUIRED_INVALID, *B_OPTIONAL_INVALID)
    for key in required:
        if key not in tool:
            record.add(layer, f"{key}（应存在而缺键）", "不一致", key, None, None)
    compare_b_top(record, tool, scenario, candidate)
    # 有效性与原因
    record.exact(layer, "valid", "valid", tool_valid, outcome.valid)
    common = result.common
    events = dict(common.events) if common is not None else {}
    item = result.candidates[candidate]
    if not outcome.valid:
        nav = item.outcome.signal_nav
        missing = nav.missing if isinstance(nav, research_run.Unavailable) else ()
        compare_b_reasons(record, list(tool.get("invalid_reasons") or []), outcome.reason, missing, events,
                          dict(item.r2))
    elif tool.get("invalid_reasons"):
        record.add(layer, "invalid_reasons（项目有效而工具列出原因）", "不一致", "invalid_reasons",
                   tool.get("invalid_reasons"), None)
    # n 与类别、结论
    ok, value = b_pair(record, layer, "n", tool, "n", outcome.n, tool_valid, True)
    if ok:
        record.exact(layer, "n", "n", value, outcome.n)
    if outcome.valid:
        record.exact(layer, "category", "category", tool.get("category"), outcome.category)
        record.exact(layer, "conclusion（有效结果精确）", "conclusion", tool.get("conclusion"), outcome.conclusion)
    else:
        record.check(layer, "category（计算无效 ↔ valid=False）", tool.get("category") == confirmatory.INVALID,
                     "category", tool.get("category"), outcome.category)
        record.check(layer, "conclusion（无效结果按列明映射）",
                     b_invalid_conclusion_ok(tool.get("conclusion"), outcome.conclusion), "conclusion",
                     tool.get("conclusion"), outcome.conclusion)
    # 数值（第 4 项）与 lnW
    for name, mine in (("delta", outcome.delta), ("delta_min", outcome.delta_min),
                       ("annual_relative_growth", outcome.annual_growth)):
        ok, value = b_pair(record, layer, name, tool, name, mine, tool_valid, outcome.valid)
        if ok:
            b_number(record, layer, name, name, value, mine)
    ok, value = b_pair(record, layer, "lnW", tool, "lnW", data.candidate_log_wealth, tool_valid, outcome.valid)
    if ok:
        for name, mine in (("candidate", data.candidate_log_wealth), ("reference", data.reference_log_wealth),
                           ("hold", data.hold_log_wealth)):
            b_number(record, layer, f"lnW.{name}", name, (value or {}).get(name), mine)
    # 自助法
    rows = () if outcome.main is None else (outcome.main, *outcome.sensitivities)
    ok, value = b_pair(record, layer, "bootstrap", tool, "bootstrap", rows or None, tool_valid, outcome.valid)
    if ok:
        compare_b_bootstrap(record, list(value or []), rows)
    # R1、R2
    if "r1" in tool:
        compare_b_r1(record, group_key(candidate), tool, item.r1)          # 补充二：先判断子键是否存在
    elif tool_valid is True:
        record.add("R1", "r1（缺键）", "不一致", "r1")
    else:
        record.add("R1", "r1", "未比较", "r1", note=B_TOOL_EARLY)
    if "r2" in tool:
        compare_b_r2(record, group_key(candidate), tool, item.r2, premise)  # 补充二：先判断子键是否存在
    ok, value = b_pair(record, layer, "r2_ok", tool, "r2_ok", True, tool_valid, True)
    if ok:
        record.exact(layer, "r2_ok ↔ all(r2[a] is True)", "r2_ok", value,
                     bool(data.r2) and all(flag is True for flag in data.r2.values()))
    # 判断（第 10 项）
    for name, mine in (("unstable", outcome.unstable if outcome.valid else None),
                       ("judgement_test_pass", outcome.improvement_passed),
                       ("judgement_point_estimate", outcome.magnitude_reached)):
        ok, value = b_pair(record, layer, name, tool, name, mine, tool_valid, outcome.valid)
        if ok:
            record.exact(layer, name, name, value, mine)
    # 警示（第 11 项）
    ok, value = b_pair(record, layer, "warnings", tool, "warnings", outcome.warnings if outcome.valid else None,
                       tool_valid, outcome.valid)
    if ok:
        record.exact(layer, "warnings（文字映射后集合）", "warnings",
                     b_mapped_warnings(value or []), sorted(outcome.warnings))
    # 前后两半（第 12 项）
    ok, value = b_pair(record, layer, "halves", tool, "halves", outcome.halves, tool_valid, outcome.valid)
    if ok:
        halves = value if isinstance(value, Mapping) else {}
        b_number(record, layer, "halves.first", "first", halves.get("first"), outcome.halves[0])
        b_number(record, layer, "halves.second", "second", halves.get("second"), outcome.halves[1])
        record.exact(layer, "halves.consistent", "consistent", halves.get("consistent"),
                     confirmatory.halves_consistent(*outcome.halves))
    # 事件置零（第 13 项）
    ok, value = b_pair(record, layer, "event_zeroing", tool, "event_zeroing",
                       outcome.zeroed if outcome.valid else None, tool_valid, outcome.valid)
    if ok:
        compare_b_zeroing(record, list(value or []), outcome.zeroed, events, data.first_signal_day)
    # 自身亏损措辞（第 15 项）
    ok, value = b_pair(record, layer, "own_loss_text", tool, "own_loss_text",
                       outcome.own_result if outcome.valid else None, tool_valid, outcome.valid)
    if ok:
        expected = (confirmatory.own_result_text(data, outcome.delta, outcome.annual_growth)
                    if outcome.delta is not None and outcome.annual_growth is not None else None)
        compare_b_own_loss(record, value, tool, outcome.own_result, expected)
    # 逐日净值路径（第 16 项）
    record.add(layer, "候选、参照、一直持有逐日净值路径", "未比较", "场景",
               note="未比较（工具 confirm 接口未输出逐日路径）")


# ---------------------------------------------------------------------------
# 第二轮 B 补充二：复用 A 映射时的缺键一次清理（《接线 B 试跑 补充二》c82982ef…5162 第一节）。
# 在 B 调用 A 比较函数之前统一判断键是否存在；A 的比较函数与 A 路径行为不变。
# 清理表（映射项 → 工具键路径）：键在 confirm 输出中存在 → 照 A 比较；
# 不存在 → 记“未比较（工具 confirm 接口未输出 <键>）”。
# ---------------------------------------------------------------------------

B_ABSENT = "未比较（工具 confirm 接口未输出 {key}）"
# 输入层（compare_input_layer）各项读取的工具键路径。“入口停止（缺少必需价格）”在 added_dates 非空时只靠 added_dates。
B_INPUT_KEYS = {
    "截止日以内原始轴": ("input_checks.raw_axis",),
    "截止日之后": ("input_checks.post_cutoff.status",),
    "派生轴（无整行缺失）": ("input_checks.derived_axis", "input_checks.added_dates"),
    WHOLE_ROW_ITEM: ("input_checks.added_dates", "input_checks.derived_axis"),
    "SPX 缺价日期集合": ("inputs",),
    "QQQ 缺价日期集合": ("inputs",),
    "入口停止": ("stop_reason.reason",),
    "入口停止：CSV 入口 ↔ 工具 stop_reason（输入校验失败）": ("exit_code", "stop_reason.reason"),
    "入口停止（缺少必需价格）": ("exit_code", "inputs", "stop_reason.reason"),
}
B_R1_KEYS = ("r1.computable", "r1.ok", "r1.mdd_signal", "r1.mdd_hold")          # ratio、segments 由 A 按“有则记”处理
B_R2_KEYS = ("events", "counts", "denominator", "passed", "new_only", "computable", "ratio_ok",
             "excluding_insufficient")
B_R2_EVENT_KEYS = ("P", "category", "first_new", "exec_idx", "offset_vs_T3", "peak_new_uncertain",
                   "first_new_confirmable")


def b_has(tool: Mapping, path: str) -> bool:
    """工具输出中键路径是否存在（逐层判断 Mapping 与键）；不以缺键推出取值。"""
    node: object = tool
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return True


def b_absent_keys(tool: Mapping, paths: Sequence[str]) -> list[str]:
    return [path for path in paths if not b_has(tool, path)]


def compare_b_input_layer(record: Recorder, tool: Mapping, facts: InputFacts, cutoff_text: str,
                          evidence: AxisEvidence | None) -> None:
    """补充二第一节第 1、2 条：在独立的记录器中调用 A 的 compare_input_layer，再逐项转入；该项所读的工具键在 confirm
    输出中不存在时改记“未比较（工具 confirm 接口未输出 <键>）”，不以缺键推出取值。A 的函数不改。"""
    sandbox = Recorder(limit=10_000)
    compare_input_layer(sandbox, tool, facts, cutoff_text, evidence)
    added = (tool.get("input_checks") or {}).get("added_dates") if b_has(tool, "input_checks.added_dates") else None
    for (layer, item, status), count in sandbox.counts.items():
        paths = B_INPUT_KEYS.get(item, ())
        if item == "入口停止（缺少必需价格）" and added:
            paths = ("exit_code", "input_checks.added_dates", "stop_reason.reason")
        absent = b_absent_keys(tool, paths)
        details = [entry for entry in sandbox.details if (entry["layer"], entry["item"], entry["status"]) ==
                   (layer, item, status)]
        if absent:
            for index in range(count):
                entry = details[index] if index < len(details) else {}
                record.add(layer, item, "未比较", entry.get("where", "场景"), None, entry.get("project"),
                           B_ABSENT.format(key="、".join(absent)))
            continue
        for index in range(count):
            entry = details[index] if index < len(details) else {}
            record.add(layer, item, status, entry.get("where", ""), entry.get("tool"), entry.get("project"),
                       entry.get("note", ""))


def compare_b_r1(record: Recorder, key: str, tool: Mapping, mine: object) -> None:
    """R1：r1 的四个必读子键齐全才照 A 比较；缺任一键则逐键记未比较。r1 整体缺失由 B 两阶段规则处理。"""
    absent = b_absent_keys(tool, B_R1_KEYS)
    if absent:
        for path in absent:
            record.add("R1", path, "未比较", key, None, None, B_ABSENT.format(key=path))
        return
    compare_r1(record, key, {"r1": tool["r1"]}, mine)


def compare_b_r2(record: Recorder, key: str, tool: Mapping, results: Mapping, premise: Mapping[str, bool]) -> None:
    """R2 判定：工具 r2[资产] 为对象时，对象与各事件的必读子键齐全才照 A 比较（A 的 P_B 分支照常）；
    任一对象缺键则该组 R2 判定逐键记未比较，不调用 A 的比较。r2[资产] 缺键（资产不可得）按 P_B 分支，不属此列。"""
    tool_r2 = tool.get("r2") or {}
    absent = []
    for asset, value in tool_r2.items():
        if isinstance(value, Mapping):
            absent += [f"r2.{asset}.{name}" for name in B_R2_KEYS if name not in value]
            for index, event in enumerate(value.get("events") or []):
                absent += [f"r2.{asset}.events[{index}].{name}" for name in B_R2_EVENT_KEYS
                           if not isinstance(event, Mapping) or name not in event]
    if absent:
        for path in absent:
            record.add("R2 判定", path, "未比较", key, None, None, B_ABSENT.format(key=path))
        return
    compare_r2_judgements(record, key, {"r2": tool_r2}, results, premise)
