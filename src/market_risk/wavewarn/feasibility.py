"""v1.3 可行条件与选择程序（纯计算）。

可行条件：非绿占比 ≤ 0.60；R 中位数 ≤ 0.60 且 P75 ≤ 1.00（主口径，类别②，SPX、QQQ 分别成立）。
选择程序：按 E2→X1→X2 取第一个至少一组 P1 可行的版本，不以 T 挑选版本；再在该版本下选 P1、N（或 N′）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.calibration import INFINITY, Distribution, distribution
from market_risk.wavewarn.config_v13 import FeasibilityLimits
from market_risk.wavewarn.exit_costs import ExitCostEvent, rebound_values

N_MODEL = "N"
N_PRIME_MODEL = "N去B/DV"            # 报告中显示为 N′
STOP_NO_VERSION = "无可行退出版本"
STOP_N_INFEASIBLE = "N 在登记约束下不可行"


@dataclass(frozen=True)
class AssetRebound:
    """一个设定在一个资产上的退出代价分布。"""

    symbol: str
    included: int                # 纳入事件数（P ≥ τ，非右截尾）
    class_1: int
    class_2: int
    class_3: int
    main: Distribution           # 主口径：类别②的 R（含 g=Tr 的零值）
    conservative: Distribution   # 保守口径：类别③按正无穷排序
    half_way: Distribution       # 半山腰转绿的最深跌幅（只含发生的事件）


def asset_rebound(symbol: str, costs: Sequence[ExitCostEvent]) -> AssetRebound:
    """由逐事件退出代价汇总一个资产的 R 分布；三类之和必须等于纳入事件数。"""
    included = [row for row in costs if row.inclusion == "纳入"]
    finite = rebound_values(included)
    counts = [sum(row.rebound_class == name for row in included)
              for name in ("①低点前已绿", "②低点或之后转绿", "③下一事件前未转绿")]
    if sum(counts) != len(included) or counts[1] != len(finite):
        raise ValueError("退出代价三类未穷尽纳入事件，或类别②缺少 R")
    return AssetRebound(symbol, len(included), counts[0], counts[1], counts[2], distribution(finite),
                        distribution((*finite, *(INFINITY for _ in range(counts[2])))),
                        distribution(tuple(row.deepest_decline for row in included
                                           if row.deepest_decline is not None)))


@dataclass(frozen=True)
class Feasibility:
    non_green_ok: bool
    median_ok: Mapping[str, bool | None]     # None：该资产类别②无样本
    p75_ok: Mapping[str, bool | None]
    feasible: bool
    note: str                                # 不可行的原因，可行时为空


def feasibility(non_green_share: Decimal, rebounds: Mapping[str, AssetRebound],
                limits: FeasibilityLimits) -> Feasibility:
    """三项条件同时成立才可行；某资产类别②无样本时视为不可行并写明。保守口径不参与判定。"""
    non_green_ok = non_green_share <= limits.non_green_share_max
    median_ok: dict[str, bool | None] = {}
    p75_ok: dict[str, bool | None] = {}
    notes = [] if non_green_ok else ["非绿占比超过上限"]
    for symbol, rebound in rebounds.items():
        main = rebound.main
        median_ok[symbol] = None if main.median is None else main.median <= limits.r_median_max
        p75_ok[symbol] = None if main.p75 is None else main.p75 <= limits.r_p75_max
        if main.n == 0:
            notes.append(f"{symbol} 类别②无样本")
        elif not (median_ok[symbol] and p75_ok[symbol]):
            notes.append(f"{symbol} 的 R 超出接受范围")
    feasible = non_green_ok and all(median_ok[symbol] is True and p75_ok[symbol] is True for symbol in rebounds)
    return Feasibility(non_green_ok, median_ok, p75_ok, feasible, "；".join(notes))


@dataclass(frozen=True)
class SelectionItem:
    """选择程序只需要的字段；key 由调用方给出，用来对回完整结果。"""

    key: str
    model: str
    exit_version: str
    score: Decimal
    executed_non_green_days: int
    billed_switches: int
    order: int
    feasible: bool


def selection_key(item: SelectionItem) -> tuple[Decimal, int, int, int]:
    """T 最小；并列依次比较执行非绿天数、计费切换次数、登记顺序。"""
    return item.score, item.executed_non_green_days, item.billed_switches, item.order


def best_feasible(items: Sequence[SelectionItem]) -> SelectionItem | None:
    feasible = [item for item in items if item.feasible]
    return min(feasible, key=selection_key) if feasible else None


@dataclass(frozen=True)
class SelectionTrace:
    """选择程序的逐步追踪；stopped 非空表示程序在该处停止。"""

    feasible_p1: tuple[tuple[str, int], ...]     # 各退出版本下可行的 P1 组数，按登记顺序
    exit_version: str | None
    p1: SelectionItem | None
    feasible_n: int | None
    n: SelectionItem | None
    n_prime_used: bool
    feasible_n_prime: int | None
    n_prime: SelectionItem | None
    stopped: str

    @property
    def final_n(self) -> SelectionItem | None:
        """最终用于与 P1 比较的是 N 还是 N′。"""
        return self.n_prime if self.n_prime_used else self.n


def _of(items: Sequence[SelectionItem], model: str, version: str) -> list[SelectionItem]:
    return [item for item in items if item.model == model and item.exit_version == version]


def select(items: Sequence[SelectionItem], versions: Sequence[str]) -> SelectionTrace:
    """登记第四、五节的选择程序；任何一步无可行设定即停止，不放宽条件。"""
    counts = tuple((version, sum(item.feasible for item in _of(items, "P1", version)))
                   for version in versions)
    version = next((name for name, count in counts if count > 0), None)
    if version is None:
        return SelectionTrace(counts, None, None, None, None, False, None, None, STOP_NO_VERSION)
    p1 = best_feasible(_of(items, "P1", version))
    n_items = _of(items, N_MODEL, version)
    n = best_feasible(n_items)
    feasible_n = sum(item.feasible for item in n_items)
    if n is not None:
        return SelectionTrace(counts, version, p1, feasible_n, n, False, None, None, "")
    prime_items = _of(items, N_PRIME_MODEL, version)
    prime = best_feasible(prime_items)
    feasible_prime = sum(item.feasible for item in prime_items)
    return SelectionTrace(counts, version, p1, feasible_n, None, True, feasible_prime, prime,
                          "" if prime is not None else STOP_N_INFEASIBLE)
