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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from wavewarn_v20_helpers import POLICY, POSITIONS, TOLERANCE, WINDOWS

from market_risk.calendar import stock_trading_days
from market_risk.storage.paths import StoragePaths
from market_risk.wavewarn_v20 import config_v20, data_v20, nav, r1
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
from market_risk.wavewarn_v20.reference import run_reference
from market_risk.wavewarn_v20.selection import registered_candidates
from market_risk.wavewarn_v20.snapshot import Snapshot, make_snapshot
from market_risk.wavewarn_v20.state_machine import (
    Risk,
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
STATUSES = ("一致", "不一致", "未比较", "接口差异", "路径差异")


@dataclass
class Recorder:
    """逐项比对的记录：按（层、字段）计数；非“一致”的逐条保留前若干条明细。"""

    counts: dict = field(default_factory=dict)
    details: list = field(default_factory=list)
    limit: int = 30

    def add(self, layer: str, item: str, status: str, where: str = "", tool: object = None,
            project: object = None, note: str = "") -> None:
        assert status in STATUSES, status
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


def compare_undetermined_reason(record: Recorder, key: str, tool_reason: object, mine_reason: object) -> None:
    layer = "候选执行政策研究模拟"
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


def compare_exec_sim(record: Recorder, key: str, tool: Mapping, mine: Mapping, signals: Sequence[Mapping]) -> None:
    layer = "候选执行政策研究模拟"
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
                                    mine["undetermined"].get("reason"))
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
