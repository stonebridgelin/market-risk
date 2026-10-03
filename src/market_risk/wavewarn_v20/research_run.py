"""研究组合层：阶段四入口的纯计算部分（《研究组合层设计 修订三》；《研究组合层实现指令（定稿）》第三节）。

接收快照与显式参数，按登记顺序调用已验收模块，输出分层结果对象。它不是运行入口：
不读取文件、配置或环境变量，不启动进程，不访问网络，不取得当前时间，不使用随机量，不修改全局 Decimal 精度；
数据入口、交易日轴验证、运行授权与落盘都由外层负责。

三个入口：
- run_window：对显式给出的候选集合计算一个评价窗口的全部分层结果；不选择、不检验；
- select_development：开发期选参，只接受开发期用途、无停止、登记 27 组的完整结果；
- confirmatory_input：组装冻结候选的检验输入；只组装，不运行检验。

模块内唯一写定的登记值是 27 组候选的登记域（登记第五节）；其余参数一律由 RunParameters 显式传入。
"""

from __future__ import annotations

import datetime as dt
import traceback
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import Enum
from types import MappingProxyType

from market_risk.wavewarn_v20.channels import (
    PULLBACK_DOMAIN,
    TREND_DOMAIN,
    ChannelError,
    run_pullback,
    run_trend,
)
from market_risk.wavewarn_v20.confirmatory import ConfirmatoryInput
from market_risk.wavewarn_v20.convergence import (
    CHANNEL_NAMES,
    Candidate,
    CandidateConvergence,
    EmptyWindowError,
    NoStartError,
    NotConvergedError,
    StartInputError,
    candidate_convergence,
    channel_initial_states,
    common_start_index,
    first_common_index,
    first_valid_index,
    reference_convergence,
    reference_initial_states,
    system_initial_states,
)
from market_risk.wavewarn_v20.execution import (
    ExecutionError,
    PlannedTarget,
    PolicyParameters,
    PositionMap,
    SignalRecord,
    Switch,
    reference_signal_records,
    signal_records,
    signal_targets,
    switches,
)
from market_risk.wavewarn_v20.inputs import (
    AssetDay,
    InputError,
    InputWindows,
    TrendDay,
    snapshot_asset_days,
    snapshot_trend_days,
)
from market_risk.wavewarn_v20.labels_r2 import (
    LabelInputError,
    MissingPriceError,
    R2Event,
    R2Thresholds,
    UnexpectedLabelError,
    r2_events,
)
from market_risk.wavewarn_v20.nav import (
    BasketPrefix,
    NavError,
    NavResult,
    NavUnavailable,
    PartialPolicyResult,
    PolicyResult,
    basket_prefix,
    simulate_hold,
    simulate_policy_with_gaps,
    simulate_targets,
)
from market_risk.wavewarn_v20.r1 import R1Error, R1Result, r1_result, segment_drawdown_ratio
from market_risk.wavewarn_v20.r2 import (
    Prompt,
    R2Error,
    R2Result,
    R2Rule,
    R2Undeterminable,
    SegmentLedger,
    Window,
    r2_result,
    segment_ledger,
)
from market_risk.wavewarn_v20.reference import ReferenceDay, run_reference
from market_risk.wavewarn_v20.selection import (
    CandidateRecord,
    SelectionResult,
    registered_candidates,
    select,
)
from market_risk.wavewarn_v20.snapshot import Snapshot, SnapshotError
from market_risk.wavewarn_v20.state_machine import (
    ChannelInitial,
    Evidence,
    Risk,
    StateError,
    SystemDay,
    SystemState,
    evidence_series,
    run_channels,
    run_system,
)

# ---------------------------------------------------------------------------
# 登记常量（模块内唯一写定的登记值；登记第五节第 296 行）
# ---------------------------------------------------------------------------

REGISTERED_KS = (3, 5, 10)
REGISTERED_THETAS = (Decimal("0.015"), Decimal("0.02"), Decimal("0.025"))
REGISTERED_HS = (1, 3, 5)
# 登记第五节：先 K、再 θ_P、再 h，共 27 组。
REGISTERED_CANDIDATES = registered_candidates(REGISTERED_KS, REGISTERED_THETAS, REGISTERED_HS)

ASSETS = ("SPX", "QQQ")
# 组合层判定码：净值所需价格缺失（取值与 labels_r2.REASON_MISSING_PRICE 相同；甲线补充裁决一第三条）。
MISSING_PRICE = "缺少必需价格"
SOURCE_PREFIX = "basket_prefix"
SOURCE_LABELS = "labels_r2.r2_events"
REFERENCE = "主参照"
HOLD = "一直持有"
COMMON = "公共"


# ---------------------------------------------------------------------------
# 类型（设计第二至十四节）
# ---------------------------------------------------------------------------


class ResearchRunError(ValueError):
    """组合层参数不一致（调用方错误）：直接抛出，不记入 stop。"""

    reason = "输入校验失败"


class Purpose(Enum):
    DEVELOPMENT = "开发期选参"
    FROZEN = "冻结候选评价"
    CONSTRUCTED = "构造验收"


class Continuity(Enum):
    COMPLETE_TRADING_AXIS = "自各资产历史起点至 E 的 NYSE 交易日轴完整，由入口以 check_trading_axis 验证"


class Coverage(Enum):
    FULL = "完整覆盖"
    PARTIAL = "部分覆盖"
    EMPTY = "无收益区间"
    UNCOMPUTABLE = "不可计算"


class Exit(Enum):
    NO_START = "无法确定 t0"
    NOT_CONVERGED = "始终不收敛"
    EMPTY_WINDOW = "评价窗口为空"
    FIXED_START_UNMET = "固定窗口不满足预热或收敛条件"
    UNDETERMINABLE = "分类无法确定"
    FAILED = "计算失败"
    INVALID_INPUT = "输入校验失败"
    UNEXPECTED = "未预期异常"


@dataclass(frozen=True)
class InitialStates:
    """登记第四节第 1 小节的初始快照，显式传入，组合层内不写默认值。"""

    channels: ChannelInitial
    system: SystemState
    reference: Risk


@dataclass(frozen=True)
class WindowSpec:
    """用途与窗口（设计第三节）。first_return_day 为评价首个计入收盘日（j₀ 的日期）；last_day 为 E。"""

    purpose: Purpose
    first_return_day: dt.date | None
    last_day: dt.date
    histories: Mapping[str, dt.date]          # 资产 → 登记历史起点
    initial: InitialStates
    continuity: Continuity

    def __post_init__(self) -> None:
        object.__setattr__(self, "histories", MappingProxyType(dict(self.histories)))


@dataclass(frozen=True)
class SegmentSpec:
    """分段：日期边界 (start, end]，按收益区间末日归属（设计第八节）。"""

    name: str
    start: dt.date
    end: dt.date
    basis: str


@dataclass(frozen=True)
class RunParameters:
    """运行参数（设计第四节）：全部显式传入，组合层不写任何默认登记值。"""

    windows: InputWindows
    positions: PositionMap
    policy: PolicyParameters
    r2_thresholds: R2Thresholds
    r2_rule: R2Rule
    offset: int
    tolerance: float
    r1_ratio: float
    segments: tuple[SegmentSpec, ...]
    diagnostics: bool = False          # 诊断开关；关闭时不额外生成诊断路径（diagnostics 为 None）

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", tuple(self.segments))


@dataclass(frozen=True)
class Unavailable:
    """无法计算：所需价格缺失的结构化原因（甲线补充裁决一第三条）。不解析异常文字。

    - 信号模拟净值、一直持有：按 prefix 预先分层，missing 为 BasketPrefix.missing，reason_code 为组合层判定码
      “缺少必需价格”，exception_type 为 None，source 为 "basket_prefix"；
    - 每资产 R2 事件：missing 为该资产标签史中价格为 None 的（资产，日期），reason_code 取异常的 reason 属性，
      exception_type 为实际异常类名，source 为 "labels_r2.r2_events"。
    """

    missing: tuple[tuple[str, dt.date], ...]
    reason_code: str
    exception_type: str | None
    source: str


@dataclass(frozen=True)
class SegmentReport:
    """分段报告：申请边界（spec）与实际净值端点都写明；returns_count = b − a。"""

    spec: SegmentSpec
    coverage: Coverage
    first_nav_day: dt.date | None             # 段首净值端点 days[a]
    last_nav_day: dt.date | None              # 段尾净值端点 days[b]
    returns_count: int | None
    drawdown_ratio: float | None
    undefined: bool                           # 一直持有段内回撤为 0，比值无定义


@dataclass(frozen=True)
class StopRecord:
    """停止记录：出口与根因分开；根因只来自 exception_type 与 reason_code（设计第十二节第 2 条）。"""

    exit: Exit
    object: str
    stage: str
    exception_type: str | None
    reason_code: str | None
    message: str
    detail: Mapping[str, object]
    traceback: str | None


@dataclass(frozen=True)
class WindowSummary:
    """窗口概要：下标都是交易日轴上的行号。"""

    t0: int
    convergences: Mapping[Candidate, CandidateConvergence]
    reference_index: int                      # 主参照全局收敛行号
    kappa_all: int
    j0: int
    j0_min: int
    start_basis: str
    e_index: int
    n: int
    first_day: dt.date                        # days 首日（j₀）
    last_day: dt.date                         # days 末日（E）
    history_first: dt.date                    # 历史输入范围（轴首末日）
    history_last: dt.date


@dataclass(frozen=True)
class ObjectOutcome:
    """一个对象（候选或主参照）的信号模拟与执行政策研究模拟。"""

    signals: tuple[SignalRecord, ...]         # 净值模拟信号，axis[j0−1 .. E−1]，长度 = len(days)
    targets: tuple[PlannedTarget, ...]
    switches: tuple[Switch, ...]
    signal_nav: NavResult | Unavailable
    policy: PolicyResult | PartialPolicyResult


@dataclass(frozen=True)
class ReferenceOutcome:
    days: tuple[ReferenceDay, ...]            # reference_full：axis[j0−2 .. E]
    all_valid: tuple[bool, ...]               # 同切片
    outcome: ObjectOutcome


@dataclass(frozen=True)
class CommonOutcome:
    events: Mapping[str, tuple[R2Event, ...] | Unavailable]
    hold: NavResult | Unavailable
    prefix: BasketPrefix


@dataclass(frozen=True)
class CandidateOutcome:
    system: tuple[SystemDay, ...]             # system_full：axis[j0−2 .. E]
    outcome: ObjectOutcome
    status: Mapping[dt.date, Prompt]          # R2 提示状态，axis[j0−2 .. E]
    r2: Mapping[str, R2Result | None]
    ledgers: Mapping[str, SegmentLedger | None]
    r1: R1Result
    segments: tuple[SegmentReport, ...]
    record: CandidateRecord


@dataclass(frozen=True)
class DrawdownDiag:
    day: dt.date
    drawdown: Decimal | None                  # D 或 D̂ = 1 − C ÷ H（局部 Decimal 上下文）
    is_estimate: bool                         # H 不完整（D̂）


@dataclass(frozen=True)
class LeverageDiag:
    end_day: dt.date                          # 区间末日
    factor: float                             # 1 + λU
    checked: bool                             # 杠杆权重为正（nav 会检查该区间）
    ok: bool                                  # factor > 0


@dataclass(frozen=True)
class EnumeratedRun:
    """一种初始状态下的逐日完整状态：通道 ChannelState、系统 SystemState、主参照 Risk。"""

    initial: object
    states: tuple[object, ...]


@dataclass(frozen=True)
class EnumeratedConvergence:
    object: str
    local_origin: int                         # 运行第 0 日的全局行号
    runs: tuple[EnumeratedRun, ...]
    first_common_local: int
    first_common_global: int


@dataclass(frozen=True)
class ConvergenceDiag:
    channels: Mapping[str, EnumeratedConvergence]
    system: EnumeratedConvergence
    same_risk_different_counters: tuple[dt.date, ...]


@dataclass(frozen=True)
class Diagnostics:
    drawdowns: Mapping[str, tuple[DrawdownDiag, ...]]
    leverage: Mapping[str, tuple[LeverageDiag, ...]]           # 键为 "reference" 与 repr(candidate)
    convergence: Mapping[Candidate, ConvergenceDiag]
    reference: EnumeratedConvergence


@dataclass(frozen=True)
class WindowResult:
    spec: WindowSpec
    parameters: RunParameters
    candidates_requested: tuple[Candidate, ...]
    window: WindowSummary | None
    common: CommonOutcome | None
    reference: ReferenceOutcome | None
    candidates: Mapping[Candidate, CandidateOutcome]
    records: tuple[CandidateRecord, ...]
    stop: StopRecord | None
    diagnostics: Diagnostics | None


@dataclass(frozen=True)
class DevelopmentSelection:
    window: WindowResult
    selection: SelectionResult
    tolerance: float


# ---------------------------------------------------------------------------
# 校验（设计第三、九节；实现指令第三节第 4 部分）
# ---------------------------------------------------------------------------


def candidate_key(candidate: Candidate) -> str:
    return f"K={candidate.k},θ_P={candidate.theta},h={candidate.h}"


def _check_call(snapshot: Snapshot, spec: WindowSpec, parameters: RunParameters,
                candidates: tuple[Candidate, ...]) -> None:
    """调用方参数的一致性；不满足即 ResearchRunError（不记入 stop）。"""
    if not isinstance(snapshot, Snapshot) or not isinstance(spec, WindowSpec) or not isinstance(
            parameters, RunParameters):
        raise ResearchRunError("snapshot、spec、parameters 的类型不符")
    axis = snapshot.days
    if not axis or not spec.last_day == snapshot.day == axis[-1]:
        raise ResearchRunError(f"E 须等于快照日与轴末日：{spec.last_day}、{snapshot.day}")
    if not set(spec.histories) == set(snapshot.closes) == set(ASSETS):
        raise ResearchRunError("登记历史起点与快照资产须恰为 SPX、QQQ")
    on_axis = frozenset(axis)
    for asset in ASSETS:
        start = spec.histories[asset]
        if start not in on_axis:
            raise ResearchRunError(f"{asset} 的登记历史起点 {start} 不在可用轴内")
    if spec.continuity is not Continuity.COMPLETE_TRADING_AXIS:
        raise ResearchRunError("交易日轴的连续性须由入口验证（Continuity.COMPLETE_TRADING_AXIS）")
    if not isinstance(spec.initial, InitialStates):
        raise ResearchRunError("初始状态须为 InitialStates")
    if not candidates:
        raise ResearchRunError("候选集合不得为空")
    if len(set(candidates)) != len(candidates):
        raise ResearchRunError("候选集合有重复")
    for candidate in candidates:
        if not isinstance(candidate, Candidate) or candidate.k not in REGISTERED_KS or (
                candidate.theta not in REGISTERED_THETAS) or candidate.h not in REGISTERED_HS:
            raise ResearchRunError(f"候选不在登记域内：{candidate!r}")
    if spec.purpose is Purpose.DEVELOPMENT:
        if spec.first_return_day is not None:
            raise ResearchRunError("开发期选参不得给出固定起点")
        if candidates != REGISTERED_CANDIDATES:
            raise ResearchRunError("开发期选参的候选须恰为登记的 27 组（集合与顺序）")
    elif spec.purpose is Purpose.FROZEN:
        if spec.first_return_day is None or spec.first_return_day not in on_axis:
            raise ResearchRunError("冻结候选评价须给出位于轴上的固定起点")
    elif spec.purpose is Purpose.CONSTRUCTED:
        if spec.first_return_day is not None and spec.first_return_day not in on_axis:
            raise ResearchRunError("构造验收给出的固定起点须在轴上")
    else:
        raise ResearchRunError(f"用途不在定义之内：{spec.purpose!r}")
    if not isinstance(parameters.offset, int) or isinstance(parameters.offset, bool) or parameters.offset < 1:
        raise ResearchRunError(f"offset 须为正整数：{parameters.offset!r}")
    if not isinstance(parameters.windows, InputWindows):
        raise ResearchRunError("windows 须为 InputWindows（其 average 用作主参照均线长度）")


def _require(condition: bool, message: str) -> None:
    """切片对齐的逐日期断言：不满足即 ResearchRunError。"""
    if not condition:
        raise ResearchRunError(message)


# ---------------------------------------------------------------------------
# 单个对象的模拟（设计第七节）
# ---------------------------------------------------------------------------


def _object_outcome(days: tuple[dt.date, ...], signals: tuple[SignalRecord, ...],
                    closes: dict[str, Sequence[Decimal | None]], prefix: BasketPrefix,
                    parameters: RunParameters) -> ObjectOutcome:
    """信号目标与切换照常生成；价格完整时信号模拟净值用 simulate_targets，有缺价时为 Unavailable（不把前缀传入）；
    执行政策照常 simulate_policy_with_gaps。"""
    targets = signal_targets(days, signals, parameters.positions)
    changes = switches(targets, parameters.positions)
    if prefix.first_missing is None:
        signal_nav: NavResult | Unavailable = simulate_targets(targets, prefix.values, parameters.positions,
                                                               parameters.tolerance)
    else:
        signal_nav = Unavailable(prefix.missing, MISSING_PRICE, None, SOURCE_PREFIX)
    policy = simulate_policy_with_gaps(days, signals, closes, parameters.positions, parameters.policy,
                                       parameters.tolerance)
    return ObjectOutcome(signals, targets, changes, signal_nav, policy)


def _check_signal_slice(signals: Sequence[SignalRecord], axis: Sequence[dt.date], j0: int,
                        days: Sequence[dt.date], name: str) -> None:
    """净值模拟信号：长度 = len(days)；首条为 axis[j0−1]；全部 signals[i+1].day == days[i]；末条为 E 的前一交易日。"""
    _require(len(signals) == len(days), f"{name} 的净值模拟信号条数与执行日数不同")
    _require(signals[0].day == axis[j0 - 1], f"{name} 的净值模拟信号首条不是 j₀ 的前一交易日")
    _require(all(signals[index + 1].day == days[index] for index in range(len(days) - 1)),
             f"{name} 的净值模拟信号与执行日没有逐日对齐")
    _require(signals[-1].day == axis[len(axis) - 2], f"{name} 的净值模拟信号末条不是 E 的前一交易日")


def _check_full_slice(full_days: Sequence[dt.date], axis: Sequence[dt.date], j0: int, name: str) -> None:
    """完整逐日记录：axis[j0−2 .. E] 逐日期对应。"""
    _require(tuple(full_days) == tuple(axis[j0 - 2:]), f"{name} 的完整逐日记录与 axis[j₀−2 .. E] 不对应")


# ---------------------------------------------------------------------------
# 分段（设计第八节）
# ---------------------------------------------------------------------------


def _segment_report(segment: SegmentSpec, days: tuple[dt.date, ...], signal_nav: NavResult | Unavailable,
                    hold: NavResult | Unavailable) -> SegmentReport:
    """日期边界 (start, end] 按收益区间末日映射：i1 = max(bisect_right(days, start), 1)，
    i2 = bisect_right(days, end) − 1；段首净值端点 a = i1 − 1，段尾 b = i2。"""
    n = len(days) - 1
    i1 = max(bisect_right(days, segment.start), 1)
    i2 = bisect_right(days, segment.end) - 1
    if i2 < i1:
        return SegmentReport(segment, Coverage.EMPTY, None, None, 0, None, False)
    a, b = i1 - 1, i2
    if isinstance(signal_nav, Unavailable) or isinstance(hold, Unavailable):
        return SegmentReport(segment, Coverage.UNCOMPUTABLE, days[a], days[b], b - a, None, False)
    full = segment.start >= days[0] and segment.end <= days[n]
    ratio = segment_drawdown_ratio(signal_nav.wealth, hold.wealth, a, b)
    return SegmentReport(segment, Coverage.FULL if full else Coverage.PARTIAL, days[a], days[b], b - a, ratio,
                         ratio is None)


# ---------------------------------------------------------------------------
# 计算过程（设计第六节）。进度记在局部对象里，停止时保留已完成的部分。
# ---------------------------------------------------------------------------


@dataclass
class _Progress:
    """一次 run_window 调用内的进度（局部对象，不是模块级状态）。"""

    object: str = COMMON
    stage: str = "输入派生量"
    window: WindowSummary | None = None
    common: CommonOutcome | None = None
    reference: ReferenceOutcome | None = None
    candidates: dict[Candidate, CandidateOutcome] = field(default_factory=dict)
    stop: StopRecord | None = None
    # 诊断所需的中间量（不进入业务结果）
    spx: tuple[AssetDay, ...] = ()
    qqq: tuple[AssetDay, ...] = ()
    trend: tuple[TrendDay, ...] = ()
    evidence: dict[tuple[int, Decimal], tuple[Evidence, ...]] = field(default_factory=dict)


def _start(axis: tuple[dt.date, ...], spec: WindowSpec, parameters: RunParameters, t0: int,
           system_indices: list[int], reference_index: int, progress: _Progress) -> tuple[int, int, str] | None:
    """起点 j₀（设计第三节）。返回 (j0, j0_min, start_basis)；固定起点不满足时写入停止记录并返回 None。"""
    progress.stage = "起点"
    j0_min = common_start_index(t0, [*system_indices, reference_index], parameters.offset, len(axis))
    scope = "登记 27 组" if spec.purpose is Purpose.DEVELOPMENT else "给定候选"
    if spec.first_return_day is None:
        return j0_min, j0_min, (f"共同起点 j₀ = max(t0 + {parameters.offset}, κ_全 + 1)，按{scope}与主参照计算")
    j0 = axis.index(spec.first_return_day)
    if j0 < j0_min or len(axis) - 1 - j0 < 1:
        message = (f"固定起点 {spec.first_return_day} 不满足预热或收敛条件"
                   f"（j₀_min = {axis[j0_min]}，n = {len(axis) - 1 - j0}）")
        progress.stop = StopRecord(
            Exit.FIXED_START_UNMET, COMMON, "起点", None, None, message,
            MappingProxyType({"j0_min": axis[j0_min], "j0_min_index": j0_min, "first_return_day": spec.first_return_day,
                              "j0_index": j0}), None)
        return None
    return j0, j0_min, f"固定起点（锁定记录给出）；预热与收敛前提 j₀_min = {axis[j0_min]} 已核对"


def _label_rows(snapshot: Snapshot, asset: str, start: dt.date) -> tuple[tuple[dt.date, Decimal | None], ...]:
    """标签史（设计第九节）：自登记起点至 E 的全部轴上日期，缺价为 None，不过滤。"""
    rows = tuple((day, snapshot.closes[asset].get(day)) for day in snapshot.days if start <= day <= snapshot.day)
    first = snapshot.days.index(start)
    _require(tuple(day for day, _ in rows) == snapshot.days[first:] and rows[0][0] == start,
             f"{asset} 的标签史日期不是轴的连续子序列，或首日不是登记起点")
    return rows


def _status(system: Sequence[SystemDay], first_index: int, system_index: int) -> dict[dt.date, Prompt]:
    """提示映射（设计第十节）：行号早于 system_index 为无法确定；否则正常 → 非提示，一级、二级 → 提示。"""
    result: dict[dt.date, Prompt] = {}
    for offset, item in enumerate(system):
        if first_index + offset < system_index:
            result[item.day] = Prompt.UNKNOWN
        else:
            result[item.day] = Prompt.NO if item.risk is Risk.NORMAL else Prompt.YES
    return result


def _compute(snapshot: Snapshot, spec: WindowSpec, parameters: RunParameters, requested: tuple[Candidate, ...],
             progress: _Progress) -> None:
    axis = snapshot.days
    e_index = len(axis) - 1
    windows, average = parameters.windows, parameters.windows.average
    # 2. 输入派生量（只算一次）
    spx = snapshot_asset_days(snapshot, "SPX", windows)
    qqq = snapshot_asset_days(snapshot, "QQQ", windows)
    trend = snapshot_trend_days(snapshot, "SPX", windows)
    progress.spx, progress.qqq, progress.trend = spx, qqq, trend
    # 3. t0
    progress.stage = "t0"
    t0 = first_valid_index(spx, qqq, trend)
    k = t0 + 1                                       # 系统序列第 0 项的全局行号
    # 4. 收敛（逐候选调用 candidate_convergence）
    progress.stage = "收敛"
    convergences: dict[Candidate, CandidateConvergence] = {}
    for candidate in requested:
        progress.object = candidate_key(candidate)
        convergences[candidate] = candidate_convergence(spx, qqq, trend, t0, candidate, average)
    progress.object = REFERENCE
    reference_index = k + reference_convergence(trend[k:], average)
    progress.object = COMMON
    # 5. 起点
    system_indices = [convergences[candidate].system_index for candidate in requested]
    started = _start(axis, spec, parameters, t0, system_indices, reference_index, progress)
    if started is None:
        return
    j0, j0_min, basis = started
    n = e_index - j0
    days = tuple(axis[j0:])
    _require(j0 - 2 >= k, "j₀ − 2 早于系统序列的首日")
    progress.window = WindowSummary(t0, MappingProxyType(convergences), reference_index,
                                    max([*system_indices, reference_index]), j0, j0_min, basis, e_index, n, days[0],
                                    days[-1], axis[0], axis[-1])
    # 6. 收盘价与收益前缀
    closes: dict[str, Sequence[Decimal | None]] = {asset: snapshot.series(asset)[j0:] for asset in ASSETS}
    prefix = basket_prefix(days, closes)
    # 7. 一直持有（只算一次）
    progress.object, progress.stage = HOLD, "一直持有"
    hold: NavResult | Unavailable = (simulate_hold(days, prefix.values, parameters.positions, parameters.tolerance)
                                     if prefix.first_missing is None
                                     else Unavailable(prefix.missing, MISSING_PRICE, None, SOURCE_PREFIX))
    # 8. 每资产标签史与 R2 事件
    progress.stage = "R2 事件"
    events: dict[str, tuple[R2Event, ...] | Unavailable] = {}
    for asset in ASSETS:
        progress.object = asset
        rows = _label_rows(snapshot, asset, spec.histories[asset])
        try:
            events[asset] = r2_events(asset, rows, snapshot.day, parameters.r2_thresholds)
        except MissingPriceError as error:
            events[asset] = Unavailable(tuple((asset, day) for day, close in rows if close is None), error.reason,
                                        type(error).__name__, SOURCE_LABELS)
    progress.common = CommonOutcome(MappingProxyType(events), hold, prefix)
    # 9. 主参照：完整逐日记录与净值模拟信号两种切片
    progress.object, progress.stage = REFERENCE, "主参照"
    reference_days = run_reference(spec.initial.reference, trend[k:], average)
    valid = tuple(trend[index].close is not None and trend[index].complete for index in range(len(axis)))
    reference_full = reference_days[j0 - 2 - k:e_index - k + 1]
    _check_full_slice([item.day for item in reference_full], axis, j0, REFERENCE)
    reference_signals = reference_signal_records(reference_days[j0 - 1 - k:e_index - k], valid[j0 - 1:e_index])
    _check_signal_slice(reference_signals, axis, j0, days, REFERENCE)
    reference_outcome = _object_outcome(days, reference_signals, closes, prefix, parameters)
    progress.reference = ReferenceOutcome(reference_full, valid[j0 - 2:], reference_outcome)
    # 10. 每候选（登记顺序）；同一 (K, θ) 的通道路径与证据序列只算一次
    for order, candidate in enumerate(requested):
        progress.object, progress.stage = candidate_key(candidate), "通道与状态机"
        pair = (candidate.k, candidate.theta)
        if pair not in progress.evidence:
            paths = run_channels(spec.initial.channels, spx[k:], qqq[k:], trend[k:], candidate.k, candidate.theta,
                                 average)
            progress.evidence[pair] = evidence_series(spx[k:], qqq[k:], trend[k:], paths)
        evidence = progress.evidence[pair]
        system = run_system(spec.initial.system, evidence, candidate.k, candidate.h)
        system_full = system[j0 - 2 - k:e_index - k + 1]
        _check_full_slice([item.day for item in system_full], axis, j0, progress.object)
        _require([item.day for item in evidence[j0 - 2 - k:e_index - k + 1]] == [item.day for item in system_full],
                 f"{progress.object} 的证据与系统记录不对应")
        progress.stage = "信号模拟与执行政策"
        signals = signal_records(system[j0 - 1 - k:e_index - k], evidence[j0 - 1 - k:e_index - k])
        _check_signal_slice(signals, axis, j0, days, progress.object)
        outcome = _object_outcome(days, signals, closes, prefix, parameters)
        progress.stage = "R2"
        status = _status(system_full, j0 - 2, convergences[candidate].system_index)
        window = Window(axis, axis[j0 - 1], axis[e_index], MappingProxyType(status))
        results: dict[str, R2Result | None] = {}
        ledgers: dict[str, SegmentLedger | None] = {}
        for asset in ASSETS:
            asset_events = events[asset]
            if isinstance(asset_events, Unavailable):
                results[asset], ledgers[asset] = None, None
            else:
                results[asset] = r2_result(asset_events, window, parameters.r2_rule)
                ledgers[asset] = segment_ledger(asset_events, window, parameters.r2_rule)
        progress.stage = "R1"
        if isinstance(outcome.signal_nav, NavResult) and isinstance(hold, NavResult):
            r1 = r1_result(outcome.signal_nav.wealth, hold.wealth, parameters.r1_ratio)
        else:
            r1 = r1_result(None, None, parameters.r1_ratio)
        progress.stage = "分段"
        segments = tuple(_segment_report(segment, days, outcome.signal_nav, hold) for segment in parameters.segments)
        flags = {asset: (None if result is None or not result.computable else result.meets)
                 for asset, result in results.items()}
        record = CandidateRecord(candidate, order, False, r1.satisfied, MappingProxyType(flags),
                                 outcome.signal_nav.log_wealth if isinstance(outcome.signal_nav, NavResult) else None,
                                 len(outcome.switches))
        progress.candidates[candidate] = CandidateOutcome(system_full, outcome, MappingProxyType(status),
                                                          MappingProxyType(results), MappingProxyType(ledgers), r1,
                                                          segments, record)
    progress.object, progress.stage = COMMON, "完成"


def _stop(exit_: Exit, progress: _Progress, error: BaseException, detail: Mapping[str, object] | None = None,
          trace: str | None = None) -> StopRecord:
    reason = getattr(error, "reason", None)
    return StopRecord(exit_, progress.object, progress.stage, type(error).__name__,
                      reason if isinstance(reason, str) and reason else None, str(error),
                      MappingProxyType(dict(detail or {})), trace)


def run_window(snapshot: Snapshot, spec: WindowSpec, parameters: RunParameters,
               candidates: Sequence[Candidate]) -> WindowResult:
    """对显式给出的候选集合计算一个评价窗口的全部分层结果；不选择、不检验（设计第二、六节）。"""
    requested = tuple(candidates)
    _check_call(snapshot, spec, parameters, requested)
    progress = _Progress()
    try:
        _compute(snapshot, spec, parameters, requested, progress)
    except ResearchRunError:
        raise
    except NoStartError as error:
        progress.stop = _stop(Exit.NO_START, progress, error)
    except NotConvergedError as error:
        progress.stop = _stop(Exit.NOT_CONVERGED, progress, error)
    except EmptyWindowError as error:
        progress.stop = _stop(Exit.EMPTY_WINDOW, progress, error, {"detail": error.detail})
    except StartInputError as error:
        progress.stop = _stop(Exit.INVALID_INPUT, progress, error)
    except LabelInputError as error:
        progress.stop = _stop(Exit.INVALID_INPUT, progress, error)
    except UnexpectedLabelError as error:
        progress.stop = _stop(Exit.UNEXPECTED, progress, error, trace=traceback.format_exc())
    except R2Undeterminable as error:
        progress.stop = _stop(Exit.UNDETERMINABLE, progress, error)
    except R2Error as error:
        progress.stop = _stop(Exit.INVALID_INPUT, progress, error)
    except NavUnavailable as error:
        # 组合层按 prefix 预先分层，不应走到这里；走到即属未预期。
        progress.stop = _stop(Exit.UNEXPECTED, progress, error, trace=traceback.format_exc())
    except NavError as error:
        progress.stop = _stop(Exit.FAILED, progress, error)
    except R1Error as error:
        progress.stop = _stop(Exit.FAILED, progress, error)
    except (ExecutionError, StateError, ChannelError, InputError, SnapshotError) as error:
        progress.stop = _stop(Exit.INVALID_INPUT, progress, error)
    except Exception as error:
        progress.stop = _stop(Exit.UNEXPECTED, progress, error, trace=traceback.format_exc())
    records = tuple(outcome.record for outcome in progress.candidates.values())
    result = WindowResult(spec, parameters, requested, progress.window, progress.common, progress.reference,
                          MappingProxyType(dict(progress.candidates)), records, progress.stop, None)
    if parameters.diagnostics and progress.stop is None:
        result = WindowResult(spec, parameters, requested, result.window, result.common, result.reference,
                              result.candidates, records, None, _diagnostics(result, progress, parameters))
    return result


# ---------------------------------------------------------------------------
# 入口：开发期选参与检验输入（设计第二节）
# ---------------------------------------------------------------------------


def select_development(result: WindowResult, tolerance: float) -> DevelopmentSelection:
    """开发期选参：只接受开发期用途、无停止、登记 27 组且记录完整的结果；再调用 selection.select。"""
    if result.spec.purpose is not Purpose.DEVELOPMENT:
        raise ResearchRunError(f"选参只接受开发期用途：{result.spec.purpose.value}")
    if result.stop is not None:
        raise ResearchRunError(f"窗口已停止（{result.stop.exit.value}），候选记录不完整，不得选择")
    if result.candidates_requested != REGISTERED_CANDIDATES:
        raise ResearchRunError("选参的候选须恰为登记的 27 组")
    if len(result.records) != len(REGISTERED_CANDIDATES) or [item.order for item in result.records] != list(
            range(len(REGISTERED_CANDIDATES))):
        raise ResearchRunError("候选记录须为 27 条，序号 0…26")
    return DevelopmentSelection(result, select(result.records, False, tolerance), tolerance)


def confirmatory_input(result: WindowResult, candidate: Candidate, period: str) -> ConfirmatoryInput:
    """组装冻结候选的检验输入（补充第 12 条）；只组装，不运行检验。"""
    if result.spec.purpose not in (Purpose.FROZEN, Purpose.CONSTRUCTED):
        raise ResearchRunError(f"检验输入只接受冻结评价或构造验收：{result.spec.purpose.value}")
    if result.stop is not None:
        raise ResearchRunError(f"窗口已停止（{result.stop.exit.value}）")
    if candidate not in result.candidates or result.reference is None or result.common is None:
        raise ResearchRunError(f"候选不在结果中：{candidate!r}")
    outcome = result.candidates[candidate]
    days = tuple(target.day for target in outcome.outcome.targets)
    mine, reference, hold = outcome.outcome.signal_nav, result.reference.outcome.signal_nav, result.common.hold
    events = {asset: value for asset, value in result.common.events.items() if not isinstance(value, Unavailable)}
    return ConfirmatoryInput(
        period, days[1:],
        mine.returns if isinstance(mine, NavResult) else None,
        reference.returns if isinstance(reference, NavResult) else None,
        mine.log_wealth if isinstance(mine, NavResult) else None,
        reference.log_wealth if isinstance(reference, NavResult) else None,
        hold.log_wealth if isinstance(hold, NavResult) else None,
        outcome.record.r1, outcome.record.r2, MappingProxyType(events), outcome.outcome.signals[0].day)


# ---------------------------------------------------------------------------
# 诊断（设计第十四节；显式选项，默认关闭，关闭时不额外生成诊断路径；业务结果组装之后计算，不回写业务字段）
# ---------------------------------------------------------------------------


def _drawdowns(days: Sequence[AssetDay]) -> tuple[DrawdownDiag, ...]:
    result = []
    with localcontext() as context:
        context.prec = 28
        for item in days:
            value = None if item.close is None or item.high is None else 1 - item.close / item.high
            result.append(DrawdownDiag(item.day, value, not item.high_complete))
    return tuple(result)


def _leverage(outcome: ObjectOutcome, prefix: BasketPrefix, positions: PositionMap) -> tuple[LeverageDiag, ...]:
    """与 nav.portfolio_return 同一公式；只对 prefix.values 覆盖的区间。"""
    return tuple(LeverageDiag(outcome.targets[index + 1].day, 1.0 + positions.leverage * value,
                              outcome.targets[index].leverage > 0, 1.0 + positions.leverage * value > 0)
                 for index, value in enumerate(prefix.values))


def _enumerated(name: str, origin: int, runs: tuple[EnumeratedRun, ...]) -> EnumeratedConvergence:
    local = first_common_index([run.states for run in runs])
    if local is None:
        raise ResearchRunError(f"{name} 的枚举路径没有共同位置")
    return EnumeratedConvergence(name, origin, runs, local, origin + local)


def _pullback_runs(data: Sequence[AssetDay], k: int, threshold: Decimal) -> tuple[EnumeratedRun, ...]:
    return tuple(EnumeratedRun(initial, tuple(item.state for item in run_pullback(initial, data, k, threshold)))
                 for initial in channel_initial_states(PULLBACK_DOMAIN))


def _trend_runs(data: Sequence[TrendDay], average: int) -> tuple[EnumeratedRun, ...]:
    return tuple(EnumeratedRun(initial, tuple(item.state for item in run_trend(initial, data, average)))
                 for initial in channel_initial_states(TREND_DOMAIN))


def _system_runs(evidence: Sequence[Evidence], k: int, h: int) -> tuple[EnumeratedRun, ...]:
    return tuple(EnumeratedRun(initial, tuple(item.state for item in run_system(initial, evidence, k, h)))
                 for initial in system_initial_states(h))


def _same_risk_different_counters(days: Sequence[dt.date], runs: tuple[EnumeratedRun, ...]) -> tuple[dt.date, ...]:
    """系统各运行中“S 相同而完整状态不同”的日期。"""
    result = []
    for day, states in zip(days, zip(*(run.states for run in runs), strict=True), strict=True):
        risks = {state.risk for state in states if isinstance(state, SystemState)}
        if len(risks) == 1 and len(set(states)) > 1:
            result.append(day)
    return tuple(result)


def _diagnostics(result: WindowResult, progress: _Progress, parameters: RunParameters) -> Diagnostics:
    window, common, reference = result.window, result.common, result.reference
    if window is None or common is None or reference is None:
        raise ResearchRunError("诊断只对完整的窗口结果计算")
    t0, average = window.t0, parameters.windows.average
    k = t0 + 1
    spx, qqq, trend = progress.spx[k:], progress.qqq[k:], progress.trend[k:]
    leverage = {"reference": _leverage(reference.outcome, common.prefix, parameters.positions)}
    convergence: dict[Candidate, ConvergenceDiag] = {}
    for candidate, outcome in result.candidates.items():
        leverage[repr(candidate)] = _leverage(outcome.outcome, common.prefix, parameters.positions)
        theta = candidate.theta
        channels = {"P_SPX": _pullback_runs(spx, candidate.k, theta), "P_QQQ": _pullback_runs(qqq, candidate.k, theta),
                    "PR_SPX": _pullback_runs(spx, candidate.k, 2 * theta),
                    "PR_QQQ": _pullback_runs(qqq, candidate.k, 2 * theta), "MR": _trend_runs(trend, average)}
        kappa = window.convergences[candidate].kappa_channel
        evidence = progress.evidence[(candidate.k, theta)][kappa - t0:]
        system_runs = _system_runs(evidence, candidate.k, candidate.h)
        convergence[candidate] = ConvergenceDiag(
            MappingProxyType({name: _enumerated(name, k, channels[name]) for name in CHANNEL_NAMES}),
            _enumerated("系统", kappa + 1, system_runs),
            _same_risk_different_counters([item.day for item in evidence], system_runs))
    reference_runs = tuple(EnumeratedRun(initial, tuple(item.risk for item in run_reference(initial, trend, average)))
                           for initial in reference_initial_states())
    return Diagnostics(MappingProxyType({"SPX": _drawdowns(progress.spx), "QQQ": _drawdowns(progress.qqq)}),
                       MappingProxyType(leverage), MappingProxyType(convergence),
                       _enumerated(REFERENCE, k, reference_runs))
