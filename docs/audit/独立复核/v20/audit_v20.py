"""波段预警 v2.0 独立复核工具。

依据（编写时只读这些资料）：
- docs/research/波段预警研究规格_v2.0_登记.md（下称“登记”）；
- docs/product/产品规格_修订一_第40版.md（下称“规格”）；
- docs/research/v20_实施口径补充.md 第 1 至 20 条（下称“补充”）；
- docs/research/v20_阶段二执行说明.md；
- docs/research/v20_阶段三实施指令.md 第五节。

约束：不导入项目包；不含行情目录的字面路径；没有网络调用；只用 numpy、pandas_market_calendars 与标准库。
输入只来自命令行给出的场景文件；输出只写到命令行给出的输出文件。

用法：
    python audit_v20.py 场景.json 输出.json

场景格式与输出字段见同目录 README.md。规则未覆盖、必须停下报告的情形抛出 StopReport，
输出文件中记为 {"停下报告": 原因}，进程退出码为 3。
"""

from __future__ import annotations

import itertools
import json
import math
import sys
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal
from pathlib import Path
from typing import Any

import numpy as np

TOOL_VERSION = "v20-indep-1"

# ---------------------------------------------------------------- 常量（全部出自登记或补充）

ACTIVE = "激活"
ARMED = "已武装未激活"
UNARMED = "未武装未激活"
PP_DOMAIN = (ACTIVE, ARMED, UNARMED)  # 登记第一节第 1 小节：P、PR 三种状态
MR_DOMAIN = (ACTIVE, ARMED)  # 登记第一节第 1 小节：MR 只有两种状态
NORMAL = "正常"
LV1 = "一级"
LV2 = "二级"
S_DOMAIN = (NORMAL, LV1, LV2)
RANK = {NORMAL: 0, LV1: 1, LV2: 2}
UNKNOWN = "未知"
ASSETS = ("SPX", "QQQ")
CHANNELS = ("P_SPX", "P_QQQ", "PR_SPX", "PR_QQQ", "MR")

H_WIN = 63  # 登记第一节第 1 小节：63 日最高价
NL_PREV = 19  # 补充第 8 条：今日与前 19 日
NL_MIN_PRESENT = 15  # 补充第 8 条：NL = 0 要求前 19 日存在的价格至少 15 个
MA_WIN = 200  # 登记第八节第 2 小节：200 日均线

W_CORE = 0.6  # 登记第六节第 2 小节：核心 60%
W_LEV = 0.4  # 杠杆 40%
LAMBDA = 2.0  # 理想化每日 2 倍
EXPO = {NORMAL: (1.0, 1.0), LV1: (1.0, 0.0), LV2: (0.5, 0.0)}  # (核心暴露, 杠杆暴露)
CASH = (0.0, 0.0)
EXPO_TEXT = {  # (总暴露, 核心权重, 杠杆权重)，精确十进制文字
    (1.0, 1.0): ("1.4", "0.6", "0.4"),
    (1.0, 0.0): ("0.6", "0.6", "0"),
    (0.5, 0.0): ("0.3", "0.3", "0"),
    (0.0, 0.0): ("0", "0", "0"),
}
EXPO_DEC = {k: Decimal(v[0]) for k, v in EXPO_TEXT.items()}
STAGE = {(1.0, 1.0): 0, (1.0, 0.0): 1, (0.5, 0.0): 2}  # 阶段编号；现金不在其中
STOP_RATIO = 0.96  # 登记第一节第 6 小节：不高于基准的 96%
COOLDOWN = 10  # 冷却期 s+1 至 s+10
T0_GAP = 63  # 登记第四节第 3 小节：j0 = max(t0 + 63, κ全 + 1)
TIE_EPS = 1e-10  # 登记第五节第 1 小节：并列组 ln W ≥ M − 1e-10
RECON_EPS = 1e-10  # 对账容差

R2_CONFIRM = Decimal("0.95")  # 补充第 2 条：C ≤ 0.95·H
R2_END = Decimal("1.05")  # C ≥ 1.05·L
R2_T3 = Decimal("0.97")  # 规格第八节第 3 条：T3
ALARM_DAYS = 20  # 规格第八节第 5 条：(s, s+20]
EVENT_PAD = 20  # 登记第七节第 6 小节：[P 前第 20 个交易日, Tr 后第 20 个交易日]

REG_K = (3, 5, 10)
REG_THETA = ("0.015", "0.02", "0.025")
REG_H = (1, 3, 5)
REG_B = 10000
REG_SEEDS = {20: 20261020, 10: 20261010, 40: 20261040, 60: 20261060, 120: 202610120}
MAIN_BLOCK = 20
SENS_BLOCKS = (10, 40, 60, 120)
P_SIG = 0.05
P_SENS_WARN = 0.10

DCTX = Context(prec=28, rounding=ROUND_HALF_EVEN)  # 只用于输出 D 的数值；比较一律用乘法形式
CENT = Decimal("0.01")


class StopReport(Exception):
    """登记未覆盖或要求停下报告的情形。"""


# ---------------------------------------------------------------- 基础工具


def parse_price(v: Any) -> Decimal | None:
    """补充第 1 条：两位小数 ROUND_HALF_UP。None 表示缺价。"""
    if v is None:
        return None
    d = Decimal(str(v))
    if not d.is_finite() or d <= 0:
        raise StopReport(f"价格不是正的有限数：{v!r}")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def seq_sum(xs: Sequence[float]) -> float:
    """从左到右逐个相加的 float64 求和（本工具所有求和统一用这一顺序）。"""
    acc = 0.0
    for x in xs:
        acc += x
    return acc


def dec_text(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


def float_out(x: float | None) -> float | str | None:
    if x is None:
        return None
    if math.isfinite(x):
        return x
    return repr(x)


def theta_key(theta: Decimal) -> str:
    return str(theta)


def group_key(k: int, theta: Decimal, h: int) -> str:
    return f"K={k},θ={theta},h={h}"


def parse_params(spec: Any) -> list[tuple[int, Decimal, int]]:
    """登记第五节：登记顺序先 K、再 θ_P、再 h。"""
    if spec == "registered":
        return [(k, Decimal(t), h) for k in REG_K for t in REG_THETA for h in REG_H]
    out = []
    for item in spec:
        k, t, h = item
        out.append((int(k), Decimal(str(t)), int(h)))
    return out


# ---------------------------------------------------------------- 输入派生量（登记第八节第 2 小节，补充第 8 条）


def asset_inputs(closes: list[Decimal | None], thetas: list[Decimal]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    q_prev = 0
    for d, c in enumerate(closes):
        lo = max(0, d - H_WIN + 1)
        win = [x for x in closes[lo : d + 1] if x is not None]
        h_complete = d >= H_WIN - 1 and len(win) == H_WIN
        h_val = max(win) if win else None
        prev = [x for x in closes[max(0, d - NL_PREV) : d] if x is not None]
        prev_full = d >= NL_PREV and len(prev) == NL_PREV
        if c is None:
            nl: int | str = UNKNOWN
        elif prev_full and c < min(prev):
            nl = 1
        elif len(prev) >= NL_MIN_PRESENT and c >= min(prev):
            nl = 0
        else:
            nl = UNKNOWN
        q = q_prev + 1 if nl == 0 else 0  # 登记：Q 为距最近一次 NL = 1 或未知的交易日数；缺价时为 0
        q_prev = q
        d_val = None
        reach: dict[str, list[bool]] = {}
        for th in thetas:
            reach[theta_key(th)] = [False, False]
        if c is not None and h_val is not None:
            d_val = DCTX.subtract(Decimal(1), DCTX.divide(c, h_val))
            for th in thetas:
                # 补充第 2 条：D ≥ θ 写作 C ≤ (1 − θ)·H（H 不完整时用 Ĥ）
                reach[theta_key(th)] = [c <= (1 - th) * h_val, c <= (1 - 2 * th) * h_val]
        out.append(
            {
                "c": c,
                "has": c is not None,
                "hc": h_complete,
                "h": h_val,
                "d": d_val,
                "reach": reach,
                "nl": nl,
                "q": q,
            }
        )
    return out


def ma_inputs(closes: list[Decimal | None]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d, c in enumerate(closes):
        lo = d - MA_WIN + 1
        complete = lo >= 0 and all(x is not None for x in closes[lo : d + 1])
        if complete:
            s = sum(closes[lo : d + 1], Decimal(0))  # type: ignore[arg-type]
            below = MA_WIN * c < s  # 补充第 2 条：C < MA 写作 200·C < 200 日收盘价之和
            out.append({"complete": True, "sum": s, "below": below})
        else:
            out.append({"complete": False, "sum": None, "below": None})
    return out


def day_view(inp: dict[str, list[dict[str, Any]]], ma: list[dict[str, Any]], d: int) -> dict[str, Any]:
    return {"SPX": inp["SPX"][d], "QQQ": inp["QQQ"][d], "MA": ma[d]}


# ---------------------------------------------------------------- 通道（登记第八节第 2 小节“v2.0 通道更新规则”）


def pp_valid(a: dict[str, Any]) -> bool:
    return a["has"] and a["hc"]


def step_pp(prev: str, a: dict[str, Any], factor: Decimal, k: int) -> str:
    """P_a、PR_a：退出 > 重新武装 > 进入；factor = 1 − 门槛。"""
    if prev not in PP_DOMAIN:
        raise StopReport(f"P/PR 出现未登记的状态：{prev}")
    if prev == ACTIVE:
        if pp_valid(a) and a["q"] >= k:
            return UNARMED  # 转为未武装，当日结束
        return ACTIVE
    state = prev
    if state == UNARMED:
        if a["nl"] in (1, UNKNOWN):
            state = ARMED  # 立即武装并进入下一步
        elif pp_valid(a) and a["c"] > factor * a["h"]:
            return ARMED  # D < 门槛：武装，当日不进入
        else:
            return UNARMED
    # 已武装：C 存在且 D̂ ≥ 门槛 → 激活
    if a["has"] and a["c"] <= factor * a["h"]:
        return ACTIVE
    return ARMED


def step_mr(prev: str, m: dict[str, Any]) -> str:
    if prev not in MR_DOMAIN:
        raise StopReport(f"MR 出现第三种状态：{prev}")  # 登记第一节第 1 小节：遇到即停下报告
    if prev == ACTIVE:
        if m["complete"] and not m["below"]:
            return ARMED
        return ACTIVE
    if m["complete"] and m["below"]:
        return ACTIVE
    return ARMED


def step_channel(name: str, prev: str, v: dict[str, Any], k: int, theta: Decimal) -> str:
    if name == "MR":
        return step_mr(prev, v["MA"])
    kind, asset = name.split("_")
    factor = 1 - theta if kind == "P" else 1 - 2 * theta
    return step_pp(prev, v[asset], factor, k)


def channel_valid(name: str, v: dict[str, Any]) -> bool:
    if name == "MR":
        return bool(v["MA"]["complete"])
    return pp_valid(v[name.split("_")[1]])


# ---------------------------------------------------------------- 状态机（登记第一节第 3 至 5 小节）


def evidence_level(ch: dict[str, str]) -> int:
    if ch["PR_SPX"] == ACTIVE or ch["PR_QQQ"] == ACTIVE:
        return 2
    if ch["P_SPX"] == ACTIVE or ch["P_QQQ"] == ACTIVE or ch["MR"] == ACTIVE:
        return 1
    return 0


def next_s(prev_s: str, lvl: int, c1: int, c2: int, h: int) -> str:
    if lvl == 2:
        return LV2
    if prev_s == NORMAL:
        return LV1 if lvl == 1 else NORMAL
    if prev_s == LV1:
        return NORMAL if c1 >= h else LV1
    if prev_s == LV2:
        return LV1 if c2 >= h else LV2
    raise StopReport(f"S 出现未登记的状态：{prev_s}")


def counter_items(ch: dict[str, str], v: dict[str, Any], k: int) -> list[bool]:
    """A₂ 的第 1 至 4 项与 A₁ 另加的第 5、6 项（登记第一节第 5 小节）。"""
    spx, qqq, ma = v["SPX"], v["QQQ"], v["MA"]
    return [
        spx["has"] and qqq["has"],
        spx["hc"] and qqq["hc"],
        ch["PR_SPX"] != ACTIVE and ch["PR_QQQ"] != ACTIVE,
        spx["q"] >= k and qqq["q"] >= k,
        bool(ma["complete"]) and ch["MR"] != ACTIVE,
        ch["P_SPX"] != ACTIVE and ch["P_QQQ"] != ACTIVE,
    ]


def update_sc(prev: tuple[str, int, int], a1: bool, a2: bool, lvl: int, h: int) -> tuple[str, int, int]:
    s0, c1p, c2p = prev
    c1 = min(h, c1p + 1) if a1 else 0
    c2 = min(h, c2p + 1) if a2 else 0
    return next_s(s0, lvl, c1, c2, h), c1, c2


def check_state(state: dict[str, Any], h: int) -> None:
    for name in CHANNELS:
        dom = MR_DOMAIN if name == "MR" else PP_DOMAIN
        if state["ch"][name] not in dom:
            raise StopReport(f"初始状态中 {name} 不在状态域内：{state['ch'][name]}")
    if state["S"] not in S_DOMAIN:
        raise StopReport(f"初始 S 不在状态域内：{state['S']}")
    for c in ("c1", "c2"):
        if not (0 <= state[c] <= h):
            raise StopReport(f"初始 {c} 超出 0 至 h：{state[c]}")


def run_machine(
    inp: dict[str, list[dict[str, Any]]],
    ma: list[dict[str, Any]],
    k: int,
    theta: Decimal,
    h: int,
    start: int,
    init: dict[str, Any],
    end: int,
) -> list[dict[str, Any]]:
    """从 start 当日的初始状态起（当日只初始化），逐日更新到 end（含）。"""
    check_state(init, h)
    recs = [
        {
            "idx": start,
            "init": True,
            "ch": dict(init["ch"]),
            "S": init["S"],
            "c1": init["c1"],
            "c2": init["c2"],
        }
    ]
    ch = dict(init["ch"])
    sc = (init["S"], init["c1"], init["c2"])
    for d in range(start + 1, end + 1):
        v = day_view(inp, ma, d)
        ch = {name: step_channel(name, ch[name], v, k, theta) for name in CHANNELS}
        items = counter_items(ch, v, k)
        a2 = all(items[:4])
        a1 = a2 and items[4] and items[5]
        lvl = evidence_level(ch)
        prev_s = sc[0]
        sc = update_sc(sc, a1, a2, lvl, h)
        valid = {name: channel_valid(name, v) for name in CHANNELS}
        trans = None
        if sc[0] != prev_s:
            trans = {"type": "升级" if RANK[sc[0]] > RANK[prev_s] else "降级", "from": prev_s, "to": sc[0]}
        recs.append(
            {
                "idx": d,
                "init": False,
                "ch": dict(ch),
                "valid": valid,
                "all_valid": all(valid.values()),
                "items": items,
                "A2": a2,
                "A1": a1,
                "c1": sc[1],
                "c2": sc[2],
                "L": lvl,
                "S": sc[0],
                "transition": trans,
                "direct_to_2": prev_s == NORMAL and lvl == 2,
            }
        )
    return recs


def initial_t0_state() -> dict[str, Any]:
    """登记第四节第 1 小节：t0 当日所有通道已武装未激活，S = 正常，c₁ = c₂ = 0。"""
    return {"ch": dict.fromkeys(CHANNELS, ARMED), "S": NORMAL, "c1": 0, "c2": 0}


def find_t0(inp: dict[str, list[dict[str, Any]]], ma: list[dict[str, Any]], end: int) -> int | None:
    """登记第四节第 1 小节：五个通道的输入与回看期首次同时完整的交易日。"""
    for d in range(end + 1):
        v = day_view(inp, ma, d)
        if all(channel_valid(name, v) for name in CHANNELS):
            return d
    return None


# ---------------------------------------------------------------- 主参照（登记第六节第 1 小节）


def step_ref(prev: str, m: dict[str, Any]) -> tuple[str, int | None]:
    if prev not in S_DOMAIN:
        raise StopReport(f"S_参照 出现未登记的状态：{prev}")
    if not m["complete"]:
        return prev, None  # 均线不完整：保持前一日状态；L_参照 无定义
    lvl = 2 if m["below"] else 0
    if lvl == 2:
        return LV2, 2
    if prev == LV2:
        return LV1, 0
    return NORMAL, 0


def run_ref(ma: list[dict[str, Any]], start: int, init: str, end: int) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = [{"idx": start, "init": True, "L": None, "S": init}]
    s = init
    for d in range(start + 1, end + 1):
        s, lvl = step_ref(s, ma[d])
        recs.append({"idx": d, "init": False, "L": lvl, "S": s})
    return recs


# ---------------------------------------------------------------- 收敛（登记第四节第 2 小节；阶段三第五节第 3 部分）


def first_equal_day(runs: list[list[Any]], start: int) -> int | None:
    """runs[r][i] 为第 start+i 日的完整状态；返回 start 之后全部运行首次逐项相同之日。"""
    length = min(len(r) for r in runs)
    for i in range(1, length):
        first = runs[0][i]
        if all(r[i] == first for r in runs[1:]):
            return start + i
    return None


def channel_convergence(
    inp: dict[str, list[dict[str, Any]]], ma: list[dict[str, Any]], k: int, theta: Decimal, start: int, end: int
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in CHANNELS:
        dom = MR_DOMAIN if name == "MR" else PP_DOMAIN
        runs: dict[str, list[str]] = {}
        for init in dom:
            st = init
            seq = [st]
            for d in range(start + 1, end + 1):
                st = step_channel(name, st, day_view(inp, ma, d), k, theta)
                seq.append(st)
            runs[init] = seq
        conv = first_equal_day(list(runs.values()), start)
        cut = (conv - start + 1) if conv is not None else None
        out[name] = {
            "start": start,
            "conv": conv,
            "runs": {init: seq[:cut] for init, seq in runs.items()},
        }
    return out


def system_convergence(machine: list[dict[str, Any]], kappa: int, h: int) -> dict[str, Any]:
    """在 κ通道 当日以 3 × (h+1)² 种 (S, c₁, c₂) 作初始值各运行一次。

    κ通道 之后通道路径已与初始化无关，所以各运行共用实际运行的通道状态与 A₁、A₂、L。
    """
    base_idx = machine[0]["idx"]
    later = machine[kappa - base_idx + 1 :]
    inits = [(s, c1, c2) for s in S_DOMAIN for c1 in range(h + 1) for c2 in range(h + 1)]
    runs: list[list[tuple[str, int, int]]] = []
    for init in inits:
        sc = init
        seq = [sc]
        for rec in later:
            sc = update_sc(sc, rec["A1"], rec["A2"], rec["L"], h)
            seq.append(sc)
        runs.append(seq)
    conv = first_equal_day(runs, kappa)
    stop = (conv - kappa) if conv is not None else len(runs[0]) - 1
    same_s_days = []
    for i in range(1, stop + 1):
        states = [r[i] for r in runs]
        groups: dict[str, set[tuple[int, int]]] = {}
        for s, c1, c2 in states:
            groups.setdefault(s, set()).add((c1, c2))
        if any(len(v) > 1 for v in groups.values()):
            same_s_days.append(kappa + i)
    cut = (conv - kappa + 1) if conv is not None else None
    return {
        "start": kappa,
        "conv": conv,
        "inits": [list(x) for x in inits],
        "runs": [[list(x) for x in r[:cut]] for r in runs],
        "same_s_diff_counter_days": same_s_days,
    }


def ref_convergence(ma: list[dict[str, Any]], start: int, end: int) -> dict[str, Any]:
    runs = {init: run_ref(ma, start, init, end) for init in S_DOMAIN}
    conv = first_equal_day([[r["S"] for r in seq] for seq in runs.values()], start)
    return {"start": start, "conv": conv, "runs": runs}


# -------------------------------- 模拟（登记第一节第 6、7 小节，第四节第 4 小节，第八节第 4 小节）


def interval_returns(
    prices: dict[str, list[Decimal | None]], j0: int, end: int
) -> tuple[dict[int, float], list[list[Any]]]:
    """U_j：两资产等权日收益，区间 j 为第 j−1 日收盘 → 第 j 日收盘（补充第 12 条，以末日标记）。

    返回 (U, 缺价清单)。缺价清单列出 [j0, end] 内两资产全部缺价日。
    """
    missing = [[a, d] for d in range(j0, end + 1) for a in ASSETS if prices[a][d] is None]
    if missing:
        return {}, missing
    u: dict[int, float] = {}
    for j in range(j0 + 1, end + 1):
        rs = float(prices["SPX"][j]) / float(prices["SPX"][j - 1]) - 1.0  # type: ignore[arg-type]
        rq = float(prices["QQQ"][j]) / float(prices["QQQ"][j - 1]) - 1.0  # type: ignore[arg-type]
        u[j] = 0.5 * rs + 0.5 * rq
    return u, []


def portfolio_return(e: tuple[float, float], u: float) -> tuple[float, float | None, bool | None]:
    """规格第五节：R = w核心·e核心·U + w杠杆·e杠杆·λ·U；杠杆暴露为正时检查 1 + λU。"""
    r = W_CORE * e[0] * u + W_LEV * e[1] * LAMBDA * u
    if e[1] > 0:
        f = 1.0 + LAMBDA * u
        return r, f, f > 0
    return r, None, None


def max_drawdown(ws: Sequence[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for w in ws:
        peak = max(peak, w)
        dd = 1.0 - w / peak
        mdd = max(mdd, dd)
    return mdd


def switch_list(targets: list[tuple[int, tuple[float, float]]]) -> list[dict[str, Any]]:
    """登记第一节第 7 小节：执行日目标与前一执行日不同计一次；首个执行日（初始建仓）不计。"""
    out = []
    for (_, prev), (d, cur) in itertools.pairwise(targets):
        if cur != prev:
            delta = EXPO_DEC[cur] - EXPO_DEC[prev]
            stages = abs(STAGE[cur] - STAGE[prev]) if cur in STAGE and prev in STAGE else None
            out.append(
                {
                    "exec_idx": d,
                    "from": EXPO_TEXT[prev][0],
                    "to": EXPO_TEXT[cur][0],
                    "delta": str(delta),
                    "abs_delta": str(abs(delta)),
                    "stages": stages,
                }
            )
    return out


def nav_path(
    held: dict[int, tuple[float, float]], u: dict[int, float], j0: int, end: int
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """held[j] 为区间 j 内持有的暴露（第 j−1 日收盘执行后的暴露）。"""
    w = 1.0
    rows: list[dict[str, Any]] = [{"idx": j0, "U": None, "R": None, "lev_factor": None, "lev_ok": None, "W": 1.0}]
    for j in range(j0 + 1, end + 1):
        r, f, ok = portfolio_return(held[j], u[j])
        w = w * (1.0 + r)
        rows.append({"idx": j, "U": u[j], "R": r, "lev_factor": f, "lev_ok": ok, "W": w})
        if ok is False:
            return rows, {"type": "杠杆因子不为正", "idx": j}
        if not (math.isfinite(r) and math.isfinite(w)) or w <= 0:
            return rows, {"type": "非有限值", "idx": j}
    return rows, None


def nav_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ws = [r["W"] for r in rows]
    logs = [math.log(1.0 + r["R"]) for r in rows[1:]]
    sum_log = seq_sum(logs)
    ln_w = math.log(ws[-1])
    return {
        "W_end": ws[-1],
        "lnW_end": ln_w,
        "sum_log": sum_log,
        "recon_ok": abs(sum_log - ln_w) <= RECON_EPS,
        "mdd": max_drawdown(ws),
    }


def simulate_signal(
    s_of: Callable[[int], str], u: dict[int, float], missing: list[list[Any]], j0: int, end: int
) -> dict[str, Any]:
    """信号模拟：第 j 日收盘执行映射(S_{j−1})。"""
    targets = [(j, EXPO[s_of(j - 1)]) for j in range(j0, end + 1)]
    plan = [
        {
            "exec_idx": j,
            "signal_idx": j - 1,
            "S": s_of(j - 1),
            "exposure": EXPO_TEXT[e][0],
            "core_w": EXPO_TEXT[e][1],
            "lev_w": EXPO_TEXT[e][2],
            "source": "初始建仓" if j == j0 else "系统",
            "cap_in_force": False,
            "cap_binding": False,
        }
        for j, e in targets
    ]
    sw = switch_list(targets)
    out: dict[str, Any] = {"plan": plan, "switches": len(sw), "switch_list": sw}
    if missing:
        out.update({"failed": {"type": "缺价", "missing": missing}, "nav": None, "summary": None})
        return out
    held = {j + 1: e for j, e in targets if j + 1 <= end}
    rows, fail = nav_path(held, u, j0, end)
    out["nav"] = rows
    out["failed"] = fail
    out["summary"] = nav_summary(rows) if fail is None else None
    if out["summary"] is not None and not out["summary"]["recon_ok"]:
        out["failed"] = {"type": "对账不符"}
    return out


def simulate_exec(
    s_of: Callable[[int], str],
    valid_of: Callable[[int], bool],
    u: dict[int, float],
    missing: list[list[Any]],
    j0: int,
    end: int,
) -> dict[str, Any]:
    """执行政策模拟：4% 止损、冷却 10 日、重入与上限 U（登记第一节第 6 小节，规格第八节第 2 条）。"""
    if missing:
        return {"failed": {"type": "缺价", "missing": missing}, "days": None, "summary": None}
    mode = "持仓"
    base = 1.0
    w = 1.0
    cap = "无"
    k_idx: int | None = None
    s_idx: int | None = None
    target = {j0: EXPO[s_of(j0 - 1)]}
    sources = {j0: "初始建仓"}
    cap_flags = {j0: (False, False)}  # (上限 U = 一级 是否在效, 上限是否实际压低了目标)
    days: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    fail = None
    for d in range(j0, end + 1):
        row: dict[str, Any] = {"idx": d}
        if d > j0:
            r, f, ok = portfolio_return(target[d - 1], u[d])
            w = w * (1.0 + r)
            row.update({"U": u[d], "R": r, "lev_factor": f, "lev_ok": ok})
            if ok is False:
                fail = {"type": "杠杆因子不为正", "idx": d}
            elif not (math.isfinite(r) and math.isfinite(w)) or w <= 0:
                fail = {"type": "非有限值", "idx": d}
        row["W"] = w
        row["held_exposure"] = EXPO_TEXT[target[d]][0]  # 第 d 日收盘执行后的暴露
        if fail is not None:
            days.append(row)
            break
        if mode == "离场待执行" and d == s_idx:
            mode = "现金"
            events.append({"type": "止损执行", "idx": d})
        elif mode == "重入待执行" and d == (k_idx or 0) + 1:
            mode = "持仓"
            base = w  # 新一轮，基准重置为该日收盘的时间加权净值
            events.append({"type": "重入执行", "idx": d})
        row["mode"] = mode
        s_d = s_of(d)
        v_d = valid_of(d)
        row["S"] = s_d
        row["all_valid"] = v_d
        if mode == "持仓":
            base = max(base, w)
            row["base"] = base
            if w <= STOP_RATIO * base:
                events.append({"type": "止损确认", "idx": d})
                mode = "离场待执行"
                s_idx = d + 1
                cap = "无"  # 新的止损确认使 U 作废
                target[d + 1] = CASH
                sources[d + 1] = "止损"
                cap_flags[d + 1] = (False, False)
            else:
                if cap == LV1 and k_idx is not None and d >= k_idx + 1 and s_d == NORMAL and v_d:
                    cap = "无"
                    events.append({"type": "上限解除", "idx": d})
                e = EXPO[s_d]
                capped = cap == LV1 and e == EXPO[NORMAL]
                target[d + 1] = EXPO[LV1] if capped else e
                sources[d + 1] = "系统"
                cap_flags[d + 1] = (cap == LV1, capped)
        elif mode in ("离场待执行", "现金"):
            row["base"] = None
            in_cool = s_idx is not None and s_idx + 1 <= d <= s_idx + COOLDOWN
            row["cooldown"] = in_cool
            if mode == "现金" and s_idx is not None and d >= s_idx + COOLDOWN and s_d == NORMAL and v_d:
                k_idx = d
                cap = LV1
                events.append({"type": "重入信号", "idx": d})
                events.append({"type": "上限设置", "idx": d})
                mode = "重入待执行"
                target[d + 1] = EXPO[LV1]
                sources[d + 1] = "重入"
                cap_flags[d + 1] = (True, s_d == NORMAL)
            else:
                target[d + 1] = CASH
                sources[d + 1] = "现金"
                cap_flags[d + 1] = (False, False)
        row["cap"] = cap
        nxt = target[d + 1]
        row["next_target"] = {
            "exec_idx": d + 1,
            "exposure": EXPO_TEXT[nxt][0],
            "core_w": EXPO_TEXT[nxt][1],
            "lev_w": EXPO_TEXT[nxt][2],
            "source": sources[d + 1],
            "cap_in_force": cap_flags[d + 1][0],
            "cap_binding": cap_flags[d + 1][1],
        }
        days.append(row)
    seq = [(j, target[j]) for j in range(j0, end + 1) if j in target]
    sw = switch_list(seq)
    summary = None
    if fail is None:
        ws = [r["W"] for r in days]
        logs = [math.log(1.0 + r["R"]) for r in days[1:]]
        sum_log = seq_sum(logs)
        ln_w = math.log(ws[-1])
        summary = {
            "W_end": ws[-1],
            "lnW_end": ln_w,
            "sum_log": sum_log,
            "recon_ok": abs(sum_log - ln_w) <= RECON_EPS,
            "mdd": max_drawdown(ws),
            "stops": sum(1 for e in events if e["type"] == "止损确认"),
            "reentries": sum(1 for e in events if e["type"] == "重入执行"),
        }
    return {
        "failed": fail,
        "days": days,
        "events": events,
        "switches": len(sw),
        "switch_list": sw,
        "summary": summary,
    }


def simulate_hold(u: dict[int, float], missing: list[list[Any]], j0: int, end: int) -> dict[str, Any]:
    """同一组合一直持有：每日暴露 1.4（规格第四节）。"""
    if missing:
        return {"failed": {"type": "缺价", "missing": missing}, "nav": None, "summary": None}
    held = {j: EXPO[NORMAL] for j in range(j0 + 1, end + 1)}
    rows, fail = nav_path(held, u, j0, end)
    summary = nav_summary(rows) if fail is None else None
    if summary is not None and not summary["recon_ok"]:
        fail = {"type": "对账不符"}
    return {"failed": fail, "nav": rows, "summary": summary}


# ---------------------------------------------------------------- R2 事件（规格第八节第 3 条）


def r2_events(closes: list[Decimal | None], end: int) -> list[dict[str, Any]]:
    present = [i for i in range(end + 1) if closes[i] is not None]
    if not present:
        raise StopReport("R2：该资产截止日前没有任何收盘价")
    start = present[0]
    gaps = [i for i in range(start, end + 1) if closes[i] is None]
    if gaps:
        # 补充第 18 条：标签函数的输入须已通过交易日完整性校验；缺价即报错
        raise StopReport(f"R2：最早收盘价之后存在缺价，标签无法生成：{gaps}")
    events: list[dict[str, Any]] = []
    mode = "寻峰"
    hi = closes[start]
    hi_idx = start
    low: Decimal = Decimal(0)
    tr = p = t5 = t3 = -1
    for d in range(start + 1, end + 1):
        c = closes[d]
        assert c is not None and hi is not None
        if mode == "寻峰":
            if c >= hi:
                hi, hi_idx = c, d  # 相同时取最后一次出现的日期
            elif c <= R2_CONFIRM * hi:
                p, t5 = hi_idx, d
                cp = closes[p]
                assert cp is not None
                t3 = next(i for i in range(p + 1, d + 1) if closes[i] <= R2_T3 * cp)  # type: ignore[operator]
                low, tr = c, d
                mode = "寻底"
        else:
            if c <= low:
                low, tr = c, d
            elif c >= R2_END * low:
                events.append({"P": p, "T3": t3, "T5": t5, "Tr": tr, "End": d})
                # 从 Tr 之后的第一个交易日开始寻峰，H 取 Tr 之后（含 End）的最高收盘价
                hi, hi_idx = closes[tr + 1], tr + 1
                for i in range(tr + 1, d + 1):
                    ci = closes[i]
                    assert ci is not None
                    if ci >= hi:  # type: ignore[operator]
                        hi, hi_idx = ci, i
                mode = "寻峰"
    if mode == "寻底":
        events.append({"P": p, "T3": t3, "T5": t5, "Tr": tr, "End": None})
    for ev in events:
        ev["C_P"] = closes[ev["P"]]
        ev["C_Tr"] = closes[ev["Tr"]]
        ev["unfinished"] = ev["End"] is None
    return events


def is_prompt(s: str | None) -> bool | None:
    if s is None:
        return None
    return s != NORMAL


def judge_events(
    events: list[dict[str, Any]], s_of: Callable[[int], str | None], f: int, n_axis: int
) -> dict[str, Any]:
    """规格第八节第 4 条：先剔除左截断，再判断输入不足，最后分类；补充第 15、20 条。"""
    rows = []
    for ev in events:
        p, t3, tr = ev["P"], ev["T3"], ev["Tr"]
        row: dict[str, Any] = {
            "P": p,
            "category": None,
            "first_new": None,
            "first_new_confirmable": False,
            "exec_idx": None,
            "offset_vs_T3": None,
            "peak_new_uncertain": False,
        }
        if p <= f:
            row["category"] = "左截断"
            rows.append(row)
            continue
        rng = range(p, t3)
        if any(s_of(d) is None for d in rng):
            row["category"] = "输入不足"
            rows.append(row)
            continue
        pr = {d: is_prompt(s_of(d)) for d in range(p - 1, tr + 1)}
        persistent = pr[p] and all(pr[d] for d in rng)
        starts = [d for d in rng if pr[d] and pr[d - 1] is False]
        if persistent:
            row["category"] = "持续覆盖达标"
        elif starts:
            row["category"] = "新提示达标"
            first = starts[0]
            row["first_new"] = first
            if pr[p] and pr[p - 1] is None:
                # 补充第 20 条：P 当日是否为真实的新提示无法确定，记首次可确认的新提示日
                row["peak_new_uncertain"] = True
                row["first_new_confirmable"] = True
            if first + 1 < n_axis:
                row["exec_idx"] = first + 1
                row["offset_vs_T3"] = first + 1 - t3
        elif pr[p] and pr[p - 1] is None:
            raise StopReport(f"R2：P={p} 当日为提示、前一日无法确定，且 [P, T3) 内没有确定的新提示开始，登记未覆盖")
        elif pr[p]:
            row["category"] = "提示中断"
        else:
            later = [pr[d] for d in range(t3, tr + 1)]
            if any(x is True for x in later):
                row["category"] = "迟到"
            elif any(x is None for x in later):
                raise StopReport(f"R2：P={p} 的 [T3, Tr] 内状态无法确定，无法区分迟到与漏报")
            else:
                row["category"] = "漏报"
        rows.append(row)
    cats = ["左截断", "输入不足", "持续覆盖达标", "新提示达标", "提示中断", "迟到", "漏报"]
    counts = {c: sum(1 for r in rows if r["category"] == c) for c in cats}
    denom = sum(counts[c] for c in cats[1:])
    passed = counts["持续覆盖达标"] + counts["新提示达标"]
    excl = denom - counts["输入不足"]
    return {
        "events": rows,
        "counts": counts,
        "denominator": denom,
        "passed": passed,
        "new_only": counts["新提示达标"],
        "computable": denom > 0,  # 非左截断事件为 0 个时 R2 无法计算
        "ratio_ok": (passed * 10 >= denom * 6) if denom > 0 else None,  # ≥ 60%
        "excluding_insufficient": [passed, excl] if excl > 0 else "无定义",
    }


def prompt_ledger(events: list[dict[str, Any]], s_of: Callable[[int], str | None], f: int, end: int) -> dict[str, Any]:
    """规格第八节第 5 条与补充第 11、14 条。events 为 {资产: 事件列表}。"""
    segs: list[dict[str, Any]] = []
    pre_window = 0
    d = f
    while d <= end:
        cur = is_prompt(s_of(d))
        if cur is None:
            d += 1
            continue
        if not cur:
            d += 1
            continue
        prev = is_prompt(s_of(d - 1))
        start = d
        while d + 1 <= end and is_prompt(s_of(d + 1)) is True:
            d += 1
        seg_end = d
        d += 1
        if prev is None:
            raise StopReport(f"提示段起始日 {start} 的前一信号日状态无法确定（补充第 11 条（c）/第 14 条）")
        if prev and start == f:
            pre_window += 1
            segs.append({"start": start, "end": seg_end, "pre_window": True, "class": None})
            continue
        segs.append({"start": start, "end": seg_end, "pre_window": False, "class": {}})
    classes = ["事件内提示", "低点后提示", "提前提示", "误报", "观察不完整"]
    result: dict[str, Any] = {"segments": segs, "pre_window_count": pre_window, "by_asset": {}}
    for asset, evs in events.items():
        for seg in segs:
            if seg["pre_window"]:
                continue
            s = seg["start"]
            if any(ev["P"] <= s < ev["Tr"] for ev in evs):
                cls = classes[0]
            elif any(ev["Tr"] <= s and (s < ev["End"] if ev["End"] is not None else s <= end) for ev in evs):
                cls = classes[1]
            elif any(s < ev["T5"] <= s + ALARM_DAYS for ev in evs):
                cls = classes[2]
            elif s + ALARM_DAYS <= end:
                cls = classes[3]
            else:
                cls = classes[4]
            seg["class"][asset] = cls
        counts = {c: sum(1 for g in segs if not g["pre_window"] and g["class"][asset] == c) for c in classes}
        fa, early = counts["误报"], counts["提前提示"]
        result["by_asset"][asset] = {
            "counts": counts,
            "false_alarm_ratio": [fa, fa + early] if fa + early > 0 else "无定义",
        }
    return result


# ---------------------------------------------------------------- 选择程序（登记第五节）


def select(records: list[dict[str, Any]], ref_failed: dict[str, Any] | None) -> dict[str, Any]:
    failed = [r["key"] for r in records if r["failure"] is not None]
    if failed or ref_failed is not None:
        return {"exit": "计算失败", "failed_groups": failed, "reference_failed": ref_failed}
    missing = [r["key"] for r in records if r["nav_missing"] or not r["r2_computable"]]
    if missing:
        return {"exit": "缺值无法评价", "groups": missing}
    feasible = [r for r in records if r["r1_ok"] and r["r2_ok"]["SPX"] and r["r2_ok"]["QQQ"]]
    if not feasible:
        return {"exit": "无合格候选", "feasible": []}
    m = max(r["lnW_end"] for r in feasible)
    tie = [r for r in feasible if r["lnW_end"] >= m - TIE_EPS]
    chosen = min(tie, key=lambda r: (r["switches"], r["order"]))
    return {
        "exit": "选定",
        "feasible": [r["key"] for r in feasible],
        "M": m,
        "tie_group": [r["key"] for r in tie],
        "selected": chosen["key"],
    }


# ---------------------------------------------------------------- 场景运行


def load_series(sc: dict[str, Any]) -> tuple[list[str], dict[str, list[Decimal | None]], int]:
    axis = [str(x) for x in sc["axis"]]
    if len(set(axis)) != len(axis):
        raise StopReport("交易日轴有重复")
    if "cutoff" in sc and sc["cutoff"] is not None:
        if sc["cutoff"] not in axis:
            raise StopReport("截止日不在交易日轴上")
        end = axis.index(sc["cutoff"])
    else:
        end = len(axis) - 1
    if sc.get("calendar") == "NYSE":
        check_nyse(axis)
    prices: dict[str, list[Decimal | None]] = {}
    for a in ASSETS:
        raw = sc["prices"][a]
        if len(raw) != len(axis):
            raise StopReport(f"{a} 价格长度与交易日轴不一致")
        # 截断到截止日（含）：截止日之后的数据不参与任何计算
        prices[a] = [parse_price(x) for x in raw[: end + 1]]
    return axis[: end + 1], prices, end


def check_nyse(axis: list[str]) -> None:
    """补充第 18 条：对照明确的交易日轴检测整行缺失。"""
    import pandas_market_calendars as mcal

    sched = mcal.get_calendar("NYSE").schedule(start_date=axis[0], end_date=axis[-1])
    sessions = [x.strftime("%Y-%m-%d") for x in sched.index]
    if sessions != axis:
        raise StopReport("交易日轴与 NYSE 交易日历不一致（整行缺失或多余）")


def ser_inputs(
    axis: list[str],
    inp: dict[str, list[dict[str, Any]]],
    ma: list[dict[str, Any]],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in ASSETS:
        out[a] = [
            {
                "day": axis[i],
                "close": dec_text(r["c"]),
                "has_close": r["has"],
                "h_complete": r["hc"],
                "h": dec_text(r["h"]),
                "d": dec_text(r["d"]),
                "reach": r["reach"],
                "nl": r["nl"],
                "q": r["q"],
            }
            for i, r in enumerate(inp[a])
        ]
    out["SPX_MA"] = [
        {"day": axis[i], "ma_complete": r["complete"], "ma_sum": dec_text(r["sum"]), "below_ma": r["below"]}
        for i, r in enumerate(ma)
    ]
    return out


def ser_machine(axis: list[str], recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in recs:
        row = {"day": axis[r["idx"]], "idx": r["idx"], "init": r["init"], "ch": r["ch"], "S": r["S"]}
        row.update({"c1": r["c1"], "c2": r["c2"]})
        if not r["init"]:
            row.update(
                {
                    "valid": r["valid"],
                    "all_valid": r["all_valid"],
                    "items": r["items"],
                    "A2": r["A2"],
                    "A1": r["A1"],
                    "L": r["L"],
                    "transition": r["transition"],
                    "direct_to_2": r["direct_to_2"],
                }
            )
        out.append(row)
    return out


def given_state(spec: dict[str, Any]) -> dict[str, Any]:
    return {"ch": dict(spec["ch"]), "S": spec["S"], "c1": int(spec["c1"]), "c2": int(spec["c2"])}


def run_signal(sc: dict[str, Any]) -> dict[str, Any]:
    """只计算输入、通道与状态机；可选从指定日起枚举收敛。"""
    axis, prices, end = load_series(sc)
    params = parse_params(sc["params"])
    thetas = sorted({p[1] for p in params})
    inp = {a: asset_inputs(prices[a], thetas) for a in ASSETS}
    ma = ma_inputs(prices["SPX"])
    out: dict[str, Any] = {"inputs": ser_inputs(axis, inp, ma), "groups": {}}
    start_spec = sc.get("start", {"mode": "t0"})
    if start_spec["mode"] == "t0":
        t0 = find_t0(inp, ma, end)
        if t0 is None:
            raise StopReport("t0 不存在：五个通道的输入从未同时完整")
        start, init = t0, initial_t0_state()
    else:
        start, init = int(start_spec["day"]), given_state(start_spec["state"])
    out["start"] = axis[start]
    enum = sc.get("enumerate")
    for k, theta, h in params:
        recs = run_machine(inp, ma, k, theta, h, start, init, end)
        g: dict[str, Any] = {"days": ser_machine(axis, recs)}
        if enum is not None:
            es = int(enum["start"])
            cc = channel_convergence(inp, ma, k, theta, es, end)
            g["channel_conv"] = ser_channel_conv(axis, cc)
            convs = [cc[n]["conv"] for n in CHANNELS]
            if all(c is not None for c in convs):
                kappa = max(convs)  # type: ignore[type-var]
                g["kappa_ch"] = axis[kappa]
                if kappa >= start:
                    g["system_conv"] = ser_system_conv(axis, system_convergence(recs, kappa, h))
        out["groups"][group_key(k, theta, h)] = g
    if sc.get("reference"):
        rs = int(sc["reference"].get("start", start))
        out["reference"] = ser_ref_conv(axis, ref_convergence(ma, rs, end))
    return out


def ser_channel_conv(axis: list[str], cc: dict[str, Any]) -> dict[str, Any]:
    return {
        n: {
            "start": axis[v["start"]],
            "conv_day": None if v["conv"] is None else axis[v["conv"]],
            "runs": v["runs"],
        }
        for n, v in cc.items()
    }


def ser_system_conv(axis: list[str], sc: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": axis[sc["start"]],
        "conv_day": None if sc["conv"] is None else axis[sc["conv"]],
        "inits": sc["inits"],
        "runs": sc["runs"],
        "same_s_diff_counter_days": [axis[i] for i in sc["same_s_diff_counter_days"]],
    }


def ser_ref_conv(axis: list[str], rc: dict[str, Any]) -> dict[str, Any]:
    cut = None if rc["conv"] is None else rc["conv"] - rc["start"] + 1
    return {
        "start": axis[rc["start"]],
        "conv_day": None if rc["conv"] is None else axis[rc["conv"]],
        "runs": {
            init: [{"day": axis[r["idx"]], "L": r["L"], "S": r["S"]} for r in seq[:cut]]
            for init, seq in rc["runs"].items()
        },
    }


def ser_sim(axis: list[str], sim: dict[str, Any]) -> dict[str, Any]:
    """把下标换成交易日标签，浮点数原样输出（repr 可逐位还原）。"""

    def lab(i: int | None) -> str | None:
        if i is None:
            return None
        return axis[i] if 0 <= i < len(axis) else f"窗口外+{i - len(axis) + 1}"

    def conv(x: Any, key: str = "") -> Any:
        if isinstance(x, dict):
            return {kk: conv(vv, kk) for kk, vv in x.items()}
        if isinstance(x, list):
            if key == "missing":
                return [[a, axis[i]] for a, i in x]
            return [conv(v, key) for v in x]
        if isinstance(x, float):
            return float_out(x)
        if (
            key in ("idx", "exec_idx", "signal_idx", "from_idx", "to_idx")
            and isinstance(x, int)
            and not isinstance(x, bool)
        ):
            return lab(x)
        return x

    return conv(sim)


def compute_object_sims(
    s_of: Callable[[int], str],
    valid_of: Callable[[int], bool],
    u: dict[int, float],
    missing: list[list[Any]],
    j0: int,
    end: int,
) -> dict[str, Any]:
    return {
        "signal": simulate_signal(s_of, u, missing, j0, end),
        "exec": simulate_exec(s_of, valid_of, u, missing, j0, end),
    }


def first_failure(*sims: dict[str, Any]) -> dict[str, Any] | None:
    """计算失败（缺价不算失败，记为“缺值无法评价”）。"""
    for sim in sims:
        fail = sim["failed"]
        if fail is not None and fail["type"] != "缺价":
            return fail
    return None


def r1_block(sig: dict[str, Any], hold: dict[str, Any], segments: list[list[int]]) -> dict[str, Any]:
    """登记第五节第 1 小节：R1 为信号模拟最大回撤 ≤ 一直持有最大回撤的一半。"""
    if sig["summary"] is None or hold["summary"] is None:
        return {"computable": False, "ok": None}
    ms, mh = sig["summary"]["mdd"], hold["summary"]["mdd"]
    seg_out = []
    for a, b in segments:
        ws = [r["W"] for r in sig["nav"] if a - 1 <= r["idx"] <= b]
        wh = [r["W"] for r in hold["nav"] if a - 1 <= r["idx"] <= b]
        sm, hm = max_drawdown(ws), max_drawdown(wh)
        seg_out.append(
            {"from_idx": a, "to_idx": b, "mdd_signal": sm, "mdd_hold": hm, "ratio": sm / hm if hm > 0 else "无定义"}
        )
    return {
        "computable": True,
        "mdd_signal": ms,
        "mdd_hold": mh,
        "ok": ms <= mh / 2,
        "ratio": ms / mh if mh > 0 else "无定义",
        "segments": seg_out,
    }


def run_full(sc: dict[str, Any]) -> dict[str, Any]:
    axis, prices, end = load_series(sc)
    params = parse_params(sc["params"])
    thetas = sorted({p[1] for p in params})
    inp = {a: asset_inputs(prices[a], thetas) for a in ASSETS}
    ma = ma_inputs(prices["SPX"])
    out: dict[str, Any] = {"inputs": ser_inputs(axis, inp, ma), "E": axis[end]}
    t0 = find_t0(inp, ma, end)
    if t0 is None:
        out["stop"] = "t0 不存在：五个通道的输入从未同时完整"
        return out
    out["t0"] = axis[t0]
    machines: dict[str, list[dict[str, Any]]] = {}
    groups: dict[str, Any] = {}
    conv_days: list[int] = []
    nonconv: list[str] = []
    cc_cache: dict[tuple[int, Decimal], dict[str, Any]] = {}
    for k, theta, h in params:
        key = group_key(k, theta, h)
        recs = run_machine(inp, ma, k, theta, h, t0, initial_t0_state(), end)
        machines[key] = recs
        g: dict[str, Any] = {"days": ser_machine(axis, recs)}
        if (k, theta) not in cc_cache:
            cc_cache[(k, theta)] = channel_convergence(inp, ma, k, theta, t0, end)
        cc = cc_cache[(k, theta)]
        g["channel_conv"] = ser_channel_conv(axis, cc)
        convs = [cc[n]["conv"] for n in CHANNELS]
        if any(c is None for c in convs):
            nonconv.append(key + "（通道）")
            groups[key] = g
            continue
        kappa = max(c for c in convs if c is not None)
        g["kappa_ch"] = axis[kappa]
        sysc = system_convergence(recs, kappa, h)
        g["system_conv"] = ser_system_conv(axis, sysc)
        if sysc["conv"] is None:
            nonconv.append(key + "（系统）")
        else:
            conv_days.append(sysc["conv"])
        groups[key] = g
    refc = ref_convergence(ma, t0, end)
    out["reference_conv"] = ser_ref_conv(axis, refc)
    if refc["conv"] is None:
        nonconv.append("主参照")
    else:
        conv_days.append(refc["conv"])
    out["groups"] = groups
    if nonconv:
        out["stop"] = f"开发期内不收敛：{nonconv}"
        return out
    kappa_all = max(conv_days)
    j0 = max(t0 + T0_GAP, kappa_all + 1)
    out.update({"kappa_all": axis[kappa_all], "j0": axis[j0] if j0 <= end else None})
    if j0 >= end:
        out["stop"] = "j0 不早于窗口末日，窗口内没有计入收益的区间"
        return out
    f = j0 - 1
    out["window_first_signal_day"] = axis[f]
    # 实施时的验收：j0 − 1 日全部枚举运行的完整状态相同（由收敛日 ≤ κ全 < j0 保证，这里再核对一次）
    u, missing = interval_returns(prices, j0, end)
    hold = simulate_hold(u, missing, j0, end)
    out["hold"] = ser_sim(axis, hold)
    # 事件
    events: dict[str, Any] = {}
    r2_error: dict[str, str] = {}
    for a in ASSETS:
        try:
            events[a] = r2_events(prices[a], end)
        except StopReport as exc:
            r2_error[a] = str(exc)
    out["r2_events"] = {a: [ser_event(axis, ev) for ev in evs] for a, evs in events.items()}
    out["r2_events_error"] = r2_error
    segments = [[axis.index(a), axis.index(b)] for a, b in sc.get("r1_segments", [])]
    records = []
    for order, (k, theta, h) in enumerate(params):
        key = group_key(k, theta, h)
        recs = machines[key]

        def s_of(d: int, recs: list[dict[str, Any]] = recs) -> str:
            return recs[d - t0]["S"]

        def valid_of(d: int, recs: list[dict[str, Any]] = recs) -> bool:
            return recs[d - t0]["all_valid"]

        sims = compute_object_sims(s_of, valid_of, u, missing, j0, end)
        g = groups[key]
        g["signal_sim"] = ser_sim(axis, sims["signal"])
        g["exec_sim"] = ser_sim(axis, sims["exec"])
        r1 = r1_block(sims["signal"], hold, segments)
        g["r1"] = ser_sim(axis, r1)

        def s_opt(d: int, recs: list[dict[str, Any]] = recs) -> str | None:
            return recs[d - t0]["S"] if t0 <= d <= end else None

        r2: dict[str, Any] = {}
        for a in ASSETS:
            if a in events:
                r2[a] = judge_events(events[a], s_opt, f, len(axis))
        g["r2"] = {a: ser_judge(axis, v) for a, v in r2.items()}
        if len(events) == len(ASSETS):
            g["ledger"] = ser_ledger(axis, prompt_ledger(events, s_opt, f, end))
        failure = first_failure(sims["signal"], sims["exec"], hold)
        records.append(
            {
                "key": key,
                "order": order,
                "failure": failure,
                "nav_missing": bool(missing),
                "r2_computable": len(r2) == len(ASSETS) and all(v["computable"] for v in r2.values()),
                "lnW_end": None if sims["signal"]["summary"] is None else sims["signal"]["summary"]["lnW_end"],
                "r1_ok": r1["ok"],
                "r2_ok": {a: (r2[a]["ratio_ok"] if a in r2 else None) for a in ASSETS},
                "switches": sims["signal"]["switches"],
            }
        )
    # 主参照：信号模拟与执行政策模拟（补充第 9 条：输入全部有效 = SPX 收盘价存在且 200 日均线窗口完整）
    ref_runs = refc["runs"]

    def ref_s(d: int) -> str:
        return ref_runs[NORMAL][d - t0]["S"]

    def ref_valid(d: int) -> bool:
        return bool(inp["SPX"][d]["has"] and ma[d]["complete"])

    ref_sims = compute_object_sims(ref_s, ref_valid, u, missing, j0, end)
    out["reference"] = {
        "days": [
            {"day": axis[d], "L": ref_runs[NORMAL][d - t0]["L"], "S": ref_s(d)} for d in range(refc["conv"], end + 1)
        ],
        "signal_sim": ser_sim(axis, ref_sims["signal"]),
        "exec_sim": ser_sim(axis, ref_sims["exec"]),
    }
    ref_fail = first_failure(ref_sims["signal"], ref_sims["exec"])
    out["records"] = [ser_sim(axis, r) for r in records]
    out["selection"] = select(records, ref_fail)
    return out


def ser_event(axis: list[str], ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "P": axis[ev["P"]],
        "C_P": str(ev["C_P"]),
        "T3": axis[ev["T3"]],
        "T5": axis[ev["T5"]],
        "Tr": axis[ev["Tr"]],
        "C_Tr": str(ev["C_Tr"]),
        "End": None if ev["End"] is None else axis[ev["End"]],
        "unfinished": ev["unfinished"],
    }


def ser_judge(axis: list[str], j: dict[str, Any]) -> dict[str, Any]:
    out = dict(j)
    out["events"] = [
        {
            **r,
            "P": axis[r["P"]],
            "first_new": None if r["first_new"] is None else axis[r["first_new"]],
            "exec_idx": None if r["exec_idx"] is None else axis[r["exec_idx"]],
        }
        for r in j["events"]
    ]
    return out


def ser_ledger(axis: list[str], led: dict[str, Any]) -> dict[str, Any]:
    out = dict(led)
    out["segments"] = [{**s, "start": axis[s["start"]], "end": axis[s["end"]]} for s in led["segments"]]
    return out


def run_exec(sc: dict[str, Any]) -> dict[str, Any]:
    """直接给定逐日 S、五通道是否全部有效与价格（或 U_j），只做模拟。"""
    axis = [str(x) for x in sc["axis"]]
    n = len(axis)
    s_list = list(sc["S"])
    v_list = [bool(x) for x in sc["all_valid"]]
    j0 = int(sc["j0"])
    end = int(sc.get("E", n - 1))
    for s in s_list:
        if s not in S_DOMAIN:
            raise StopReport(f"S 不在状态域内：{s}")
    if "U" in sc:
        u = {j: float(sc["U"][j]) for j in range(j0 + 1, end + 1)}
        missing: list[list[Any]] = []
    else:
        prices = {a: [parse_price(x) for x in sc["prices"][a]] for a in ASSETS}
        u, missing = interval_returns(prices, j0, end)
    sims = compute_object_sims(lambda d: s_list[d], lambda d: v_list[d], u, missing, j0, end)
    hold = simulate_hold(u, missing, j0, end)
    return {
        "signal_sim": ser_sim(axis, sims["signal"]),
        "exec_sim": ser_sim(axis, sims["exec"]),
        "hold": ser_sim(axis, hold),
    }


def run_r2(sc: dict[str, Any]) -> dict[str, Any]:
    """直接给定逐日 S（可为 null 表示无法确定）与价格，计算事件、判定与提示段账。"""
    axis = [str(x) for x in sc["axis"]]
    end = int(sc["E"])
    f = int(sc["f"])
    s_list = sc["S"]

    def s_of(d: int) -> str | None:
        return s_list[d] if 0 <= d < len(s_list) else None

    events: dict[str, Any] = {}
    out: dict[str, Any] = {"events": {}, "judge": {}}
    for a, raw in sc["prices"].items():
        closes = [parse_price(x) for x in raw[: end + 1]]
        events[a] = r2_events(closes, end)
        out["events"][a] = [ser_event(axis, ev) for ev in events[a]]
        out["judge"][a] = ser_judge(axis, judge_events(events[a], s_of, f, len(axis)))
    out["ledger"] = ser_ledger(axis, prompt_ledger(events, s_of, f, end))
    return out


# ---------------------------------------------------------------- 平稳自助法与确认性检验（登记第七节）


def stationary_bootstrap_indices(n: int, b: int, seed: int, count: int) -> list[list[int]]:
    """阶段二执行说明第 4 部分：先抽首位置；之后每个位置先抽 u，u < 1/b 时重抽位置，否则环形续行。

    生成器为 NumPy Generator(PCG64(seed))，全部序列共用一个生成器、依次抽取。
    逐次调用 rng.integers(0, n) 与 rng.random()（标量调用）；调用形式见 README 疑问 Q9。
    """
    rng = np.random.Generator(np.random.PCG64(seed))
    p = 1.0 / b
    out = []
    for _ in range(count):
        pos = int(rng.integers(0, n))
        seq = [pos]
        for _i in range(1, n):
            if rng.random() < p:
                pos = int(rng.integers(0, n))
            else:
                pos = (pos + 1) % n
            seq.append(pos)
        out.append(seq)
    return out


def run_bootstrap(sc: dict[str, Any]) -> dict[str, Any]:
    items = []
    for it in sc["items"]:
        seed, b, n, count = int(it["seed"]), int(it["b"]), int(it["n"]), int(it["count"])
        items.append({"seed": seed, "b": b, "n": n, "sequences": stationary_bootstrap_indices(n, b, seed, count)})
    return {"items": items}


def bootstrap_row(d: np.ndarray, delta: float, b: int, seed: int, big_b: int) -> dict[str, Any]:
    n = len(d)
    if n < 2 * b:
        return {"b": b, "seed": seed, "valid": False, "note": "n ÷ b < 2"}
    rng = np.random.Generator(np.random.PCG64(seed))
    p_jump = 1.0 / b
    stars = np.empty(big_b)
    for kk in range(big_b):
        pos = int(rng.integers(0, n))
        idx = np.empty(n, dtype=np.int64)
        idx[0] = pos
        for i in range(1, n):
            if rng.random() < p_jump:
                pos = int(rng.integers(0, n))
            else:
                pos = (pos + 1) % n
            idx[i] = pos
        stars[kk] = float(np.cumsum(d[idx])[-1])
    hits = int(np.sum(stars - delta >= delta))
    p = (1 + hits) / (big_b + 1)
    q = np.quantile(stars, [0.025, 0.975])
    return {"b": b, "seed": seed, "valid": True, "p": p, "q025": float(q[0]), "q975": float(q[1]), "note": ""}


def run_confirm(sc: dict[str, Any]) -> dict[str, Any]:
    axis, prices, end = load_series(sc)
    k, theta, h = parse_params([sc["params"]])[0]
    jv = axis.index(sc["window_start"])
    split = axis.index(sc["half_split"])
    big_b = int(sc.get("B", REG_B))
    seeds = {int(kk): int(v) for kk, v in sc.get("seeds", REG_SEEDS).items()}
    registered = big_b == REG_B and seeds == REG_SEEDS
    inp = {a: asset_inputs(prices[a], [theta]) for a in ASSETS}
    ma = ma_inputs(prices["SPX"])
    out: dict[str, Any] = {"registered_settings": registered, "params": group_key(k, theta, h)}
    invalid: list[str] = []
    t0 = find_t0(inp, ma, end)
    if t0 is None:
        raise StopReport("t0 不存在")
    recs = run_machine(inp, ma, k, theta, h, t0, initial_t0_state(), end)
    cc = channel_convergence(inp, ma, k, theta, t0, end)
    convs = [cc[n]["conv"] for n in CHANNELS]
    sys_conv = None
    if all(c is not None for c in convs):
        sys_conv = system_convergence(recs, max(c for c in convs if c is not None), h)["conv"]
    refc = ref_convergence(ma, t0, end)
    if sys_conv is None or refc["conv"] is None or max(sys_conv, refc["conv"]) > jv - 1:
        invalid.append("窗口开始前未收敛")
    f = jv - 1

    def s_of(d: int) -> str:
        return recs[d - t0]["S"]

    def ref_s(d: int) -> str:
        return refc["runs"][NORMAL][d - t0]["S"]

    u, missing = interval_returns(prices, jv, end)
    cand = simulate_signal(s_of, u, missing, jv, end)
    ref = simulate_signal(ref_s, u, missing, jv, end)
    hold = simulate_hold(u, missing, jv, end)
    if missing:
        invalid.append("净值所需价格缺失")
    for name, sim in (("候选", cand), ("参照", ref), ("一直持有", hold)):
        if sim["failed"] is not None and sim["failed"]["type"] != "缺价":
            invalid.append(f"{name}计算失败：{sim['failed']['type']}")
    out["window"] = {"start": axis[jv], "first_signal_day": axis[f], "E": axis[end], "half_split": axis[split]}
    # R2（按验证期截止日生成的事件）
    r2: dict[str, Any] = {}
    events: dict[str, Any] = {}
    for a in ASSETS:
        try:
            events[a] = r2_events(prices[a], end)
            r2[a] = judge_events(events[a], lambda d: s_of(d) if t0 <= d <= end else None, f, len(axis))
            if not r2[a]["computable"]:
                invalid.append(f"R2 无法计算（{a} 非左截断事件为 0 个）")
        except StopReport as exc:
            invalid.append(f"R2 无法计算（{a}）：{exc}")
    out["r2"] = {a: ser_judge(axis, v) for a, v in r2.items()}
    if cand["summary"] is None or ref["summary"] is None or hold["summary"] is None:
        out.update({"valid": False, "invalid_reasons": invalid, "category": "计算无效", "conclusion": "计算无效"})
        return out
    rc = [r["R"] for r in cand["nav"][1:]]
    rr = [r["R"] for r in ref["nav"][1:]]
    js = [r["idx"] for r in cand["nav"][1:]]
    dj = [math.log(1.0 + a) - math.log(1.0 + b) for a, b in zip(rc, rr, strict=True)]
    n = len(dj)
    delta = seq_sum(dj)
    ln_c, ln_r, ln_h = cand["summary"]["lnW_end"], ref["summary"]["lnW_end"], hold["summary"]["lnW_end"]
    if n == 0:
        invalid.append("n = 0")
    if not all(math.isfinite(x) for x in dj):
        invalid.append("d_j 出现非有限值")
    for name, sim in (("候选", cand), ("参照", ref), ("一直持有", hold)):
        if not sim["summary"]["recon_ok"]:
            invalid.append(f"{name}对账不符")
    if abs(delta - (ln_c - ln_r)) > RECON_EPS:
        invalid.append("|Σ d_j − Δ| > 1e-10")
    d_min = n / 252 * math.log(1.01)
    annual = math.exp(252 * delta / n) - 1 if n > 0 else None
    darr = np.array(dj, dtype=np.float64)
    rows = []
    for b in (MAIN_BLOCK, *SENS_BLOCKS):
        rows.append(bootstrap_row(darr, delta, b, seeds[b], big_b))
    if not rows[0]["valid"]:
        invalid.append("主设定 n ÷ b < 2")
    r1 = r1_block(cand, hold, [])
    r1_ok = r1["ok"]
    r2_ok = all(r2.get(a, {}).get("ratio_ok") is True for a in ASSETS)
    # 稳定性警示
    sens_warn = any(r["valid"] and r["p"] >= P_SENS_WARN for r in rows[1:])
    first_half = seq_sum([x for x, j in zip(dj, js, strict=True) if j < split])
    second_half = seq_sum([x for x, j in zip(dj, js, strict=True) if j >= split])
    halves_consistent = (first_half > 0 and second_half > 0) or (first_half < 0 and second_half < 0)
    zero_rows = []
    for a in ASSETS:
        for ev in events.get(a, []):
            if ev["P"] <= f:
                continue  # 不含左截断事件
            lo, hi = ev["P"] - EVENT_PAD, ev["Tr"] + EVENT_PAD
            dz = [0.0 if lo <= j <= hi else x for x, j in zip(dj, js, strict=True)]
            dz_sum = seq_sum(dz)
            zero_rows.append(
                {
                    "asset": a,
                    "P": axis[ev["P"]],
                    "Tr": axis[ev["Tr"]],
                    "delta_zeroed": dz_sum,
                    "not_positive": dz_sum <= 0,
                }
            )
    zero_warn = any(z["not_positive"] for z in zero_rows)
    warnings = []
    if sens_warn:
        warnings.append("敏感性区块下 p ≥ 0.10")
    if not halves_consistent:
        warnings.append("前后两半的 Δ 方向不一致")
    if zero_warn:
        warnings.append("事件窗口置零后不再为正")
    main = rows[0]
    test_pass = bool(main["valid"] and delta > 0 and main["p"] < P_SIG)
    point_ok = delta >= d_min
    valid = not invalid
    if not valid:
        cat = "计算无效"
    elif not (r1_ok and r2_ok):
        cat = "D"
    elif delta <= 0 or main["p"] >= P_SIG:
        cat = "C"
    elif delta < d_min:
        cat = "B"
    else:
        cat = "A"
    unstable = cat in ("A", "B") and bool(warnings)
    texts = {
        "计算无效": "计算无效，不写任何优劣结论",
        "D": "未通过资格检查",
        "C": "未证明长期收益优于参照规则",
        "B": "收益改善检验通过，但改善点估计未达到登记幅度",
        "A": "在冻结后的历史检验中，满足风险与提示条件，收益改善检验通过，且改善点估计达到登记幅度",
    }
    conclusion = texts[cat]
    if unstable:
        conclusion = f"证据不稳定（原类别 {cat}：{texts[cat]}）"
    loss_text = None
    if valid and ln_c < 0:
        # 补充第 16 条：只列三者自身收益与年化相对净值增长率；候选亏损且 Δ > 0 时才写“相对参照少亏”
        loss_text = (
            f"候选自身收益 {(math.exp(ln_c) - 1) * 100:.4f}%，参照自身收益 {(math.exp(ln_r) - 1) * 100:.4f}%，"
            f"一直持有自身收益 {(math.exp(ln_h) - 1) * 100:.4f}%；年化相对净值增长率 {annual * 100:.4f}%（n = {n}）"
        )
        if delta > 0:
            loss_text += "；相对参照少亏"
    out.update(
        {
            "valid": valid,
            "invalid_reasons": invalid,
            "n": n,
            "delta": delta,
            "delta_min": d_min,
            "annual_relative_growth": annual,
            "bootstrap": rows,
            "r1": r1,
            "r2_ok": r2_ok,
            "category": cat,
            "unstable": unstable,
            "warnings": warnings,
            "judgement_test_pass": test_pass,
            "judgement_point_estimate": point_ok,
            "halves": {"first": first_half, "second": second_half, "consistent": halves_consistent},
            "event_zeroing": zero_rows,
            "conclusion": conclusion,
            "own_loss_text": loss_text,
            "lnW": {"candidate": ln_c, "reference": ln_r, "hold": ln_h},
        }
    )
    return out


# ---------------------------------------------------------------- 入口

RUNNERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "signal": run_signal,
    "full": run_full,
    "exec": run_exec,
    "r2": run_r2,
    "bootstrap": run_bootstrap,
    "confirm": run_confirm,
}


def sanitize(x: Any) -> Any:
    """非有限的浮点数写成文字，Decimal 写成十进制文字，元组写成列表。"""
    if isinstance(x, dict):
        return {str(k): sanitize(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [sanitize(v) for v in x]
    if isinstance(x, float):
        return float_out(x)
    if isinstance(x, Decimal):
        return str(x)
    return x


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法：python audit_v20.py 场景.json 输出.json", file=sys.stderr)
        return 2
    sc = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    kind = sc["kind"]
    code = 0
    try:
        result: dict[str, Any] = RUNNERS[kind](sc)
    except StopReport as exc:
        result = {"停下报告": str(exc)}
        code = 3
    if "stop" in result:
        code = 3  # full 场景中途停下报告（不收敛、t0 不存在、窗口为空），已算出的部分照常输出
    result = sanitize({"tool": TOOL_VERSION, "kind": kind, "name": sc.get("name"), **result})
    Path(argv[2]).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
