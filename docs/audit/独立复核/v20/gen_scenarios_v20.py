"""波段预警 v2.0 独立复核：场景生成脚本（不导入项目包，只用 numpy、pandas_market_calendars 与标准库）。

用法：
    python gen_scenarios_v20.py 输出目录 [all|manual]

all：人工例子、收敛反例、随机价格路径、抽样索引与确认性检验场景；manual：只生成人工例子。
全部场景写入给定目录（应为临时目录），并写 manifest.json（场景名、类别、种子、SHA-256）。
人工例子的“关注日”写在各场景的 meta 中；期望值不写在这里，见 tests/test_v20_independent_tool.py。
"""

from __future__ import annotations

import functools
import hashlib
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import numpy as np

MASTER_SEED = 20261002  # 随机路径的主种子；第 i 条路径的种子为 MASTER_SEED + i
CONFIRM_SEED = 20261003
CENT = Decimal("0.01")
NORMAL, LV1, LV2 = "正常", "一级", "二级"
ARMED, ACTIVE, UNARMED = "已武装未激活", "激活", "未武装未激活"


def money(x: float | Decimal) -> str:
    return str(Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP))


def ramp(n: int, a: float, b: float) -> list[str]:
    """n 个由 a 到 b 的等差价格（两位小数）。"""
    if n == 1:
        return [money(a)]
    return [money(Decimal(str(a)) + (Decimal(str(b)) - Decimal(str(a))) * i / (n - 1)) for i in range(n)]


def axis_of(n: int) -> list[str]:
    return [f"D{i:03d}" for i in range(n)]


def state(s: str, ch: dict[str, str] | None = None, c1: int = 0, c2: int = 0) -> dict[str, Any]:
    chs = dict.fromkeys(("P_SPX", "P_QQQ", "PR_SPX", "PR_QQQ", "MR"), ARMED)
    chs.update(ch or {})
    return {"ch": chs, "S": s, "c1": c1, "c2": c2}


# ---------------------------------------------------------------- 人工例子


def manual_scenarios() -> dict[str, dict[str, Any]]:
    sc: dict[str, dict[str, Any]] = {}

    # 登记第一节第 4 小节 例 1：单日急跌（θ_P = 2%，K = 5）
    n = 260
    spx = [*ramp(258, 80, 100), "98.50", "95.70"]
    sc["例1_单日急跌"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ramp(n, 40, 50)},
        "params": [[5, "0.02", 3]],
        "meta": {"出处": "登记第一节第 4 小节例 1", "d": 259},
    }

    # 例 2：新低重新武装（h = 1，使系统在 d−1 日回到正常）
    spx = [*ramp(220, 80, 100), *(["95.50"] * 7), "95.40"]
    n = len(spx)
    sc["例2_新低重新武装"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ramp(n, 40, 50)},
        "params": [[5, "0.02", 1]],
        "meta": {"出处": "登记第一节第 4 小节例 2", "d": n - 1},
    }

    # 例 3：QQQ 的 63 日窗口内有一日缺价，Ĥ 算出 D̂ = 4.1%
    qqq: list[str | None] = [*ramp(240, 80, 100), "95.90"]
    qqq[230] = None
    n = len(qqq)
    sc["例3_回看量不完整时进入"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": ramp(n, 80, 100), "QQQ": qqq},
        "params": [[5, "0.02", 3]],
        "meta": {"出处": "登记第一节第 4 小节例 3", "d": n - 1},
    }

    # 例 4：经一级再入二级
    spx = [*ramp(220, 80, 100), "97.50", "95.80"]
    n = len(spx)
    sc["例4_经一级"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ramp(n, 40, 50)},
        "params": [[5, "0.02", 3]],
        "meta": {"出处": "登记第一节第 4 小节例 4", "d": n - 1},
    }

    # 登记第一节第 5 小节 例 A：QQQ 第 d−30 日缺价；Q_SPX = Q_QQQ = 8（K = 5，h = 3）
    d = 280
    n = 320
    spx = ["100.00"] * n
    qqq = ["100.00"] * n
    for i in range(d - 8, n):
        spx[i] = "99.00"
        qqq[i] = "99.00"
    qqq[d - 30] = None
    sc["例A_二级QQQ窗口内缺价"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": qqq},
        "params": [[5, "0.02", 3]],
        "start": {"mode": "given", "day": d - 1, "state": state(LV2)},
        "meta": {"出处": "登记第一节第 5 小节例 A", "d": d, "离开63日窗口": d + 33},
    }

    # 例 B：SPX 第 d−150 日缺价（只在 200 日窗口内），已连续 3 日 A₂ 成立
    d = 280
    n = 300
    spx = ["100.00"] * n
    spx[d - 150] = None
    sc["例B_二级SPX200日窗口内缺价"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ["100.00"] * n},
        "params": [[5, "0.02", 3]],
        "start": {"mode": "given", "day": d - 3, "state": state(LV2)},
        "meta": {"出处": "登记第一节第 5 小节例 B", "d": d},
    }

    # 例 C：数据完整；PR_QQQ 未激活但 Q_QQQ = 4
    d = 280
    n = 281
    qqq = ["100.00"] * n
    for i in range(d - 4, n):
        qqq[i] = "99.50"
    sc["例C_Q不足"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": ["100.00"] * n, "QQQ": qqq},
        "params": [[5, "0.02", 3]],
        "start": {"mode": "given", "day": d - 1, "state": state(LV2)},
        "meta": {"出处": "登记第一节第 5 小节例 C", "d": d},
    }

    # 登记第八节第 2 小节 例 E、F、G（K = 5，θ_P = 2.5%）：SPX 第 d−40 日缺价
    d = 280
    n = 281
    spx = ["100.00"] * n
    spx[d - 40] = None
    for i in range(d - 6, n):
        spx[i] = "99.00"
    sc["例E_无效不退出"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ["100.00"] * n},
        "params": [[5, "0.025", 3]],
        "start": {"mode": "given", "day": d - 1, "state": state(LV1, {"P_SPX": ACTIVE})},
        "meta": {"出处": "登记第八节第 2 小节例 E", "d": d},
    }
    spx = ["100.00"] * n
    spx[d - 40] = None
    spx[d] = "97.40"
    sc["例F_不完整回看量进入"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ["100.00"] * n},
        "params": [[5, "0.025", 3]],
        "start": {"mode": "given", "day": d - 1, "state": state(NORMAL)},
        "meta": {"出处": "登记第八节第 2 小节例 F", "d": d},
    }
    spx = ["100.00"] * n
    spx[d - 40] = None
    spx[d - 5] = "99.00"
    spx[d] = "99.00"
    sc["例G_无效不武装"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ["100.00"] * n},
        "params": [[5, "0.025", 3]],
        "start": {"mode": "given", "day": d - 1, "state": state(NORMAL, {"P_SPX": UNARMED})},
        "meta": {"出处": "登记第八节第 2 小节例 G", "d": d},
    }

    # 登记第八节第 2 小节 MR 例子：t0+1 均线不完整、t0+2 完整且 C ≥ MA、t0+3 完整且 C < MA。
    # 登记把三日写在 t0 之后；按登记定义 t0 当日均线完整，t0+1 不可能不完整（见 README 疑问 Q1），
    # 所以这里把起点放在一次缺价离开 200 日窗口的前两日：第 10 日缺价，第 209 日不完整、第 210 日起完整。
    n = 215
    spx = ["100.00"] * n
    spx[10] = None
    spx[211] = "99.00"
    sc["MR例子"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ["100.00"] * n},
        "params": [[5, "0.02", 3]],
        "start": {"mode": "given", "day": 208, "state": state(NORMAL)},
        "enumerate": {"start": 208},
        "meta": {"出处": "登记第八节第 2 小节 MR 构造例子（日期平移）", "t0+1": 209, "t0+2": 210, "t0+3": 211},
    }

    # 登记第八节第 2 小节 h = 3 构造边界，及第一节第 6 小节例乙中的有效性窗口：第 32 日缺价始终未补齐
    n = 241
    spx = ramp(n, 80, 100)
    spx[32] = None
    sc["h3边界_第32日缺价"] = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": spx, "QQQ": ramp(n, 40, 50)},
        "params": [[5, "0.02", 3]],
        "start": {"mode": "given", "day": 200, "state": state(LV1)},
        "meta": {"出处": "登记第八节第 2 小节代价说明的构造边界；第一节第 6 小节例乙", "缺价日": 32},
    }

    # 登记第一节第 6 小节 重入上限例子（沿用规格例子二的编号：第 20 日止损确认，s = 21）
    n = 241
    u = [0.0] * n
    u[20] = -0.03  # 1.4 × (−3%) = −4.2%，净值 0.958 ≤ 0.96 × 基准 1

    def exec_sc(s_list: list[str], valid: list[bool], meta: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "exec", "axis": axis_of(n), "S": s_list, "all_valid": valid, "U": u, "j0": 1, "meta": meta}

    s_all = [NORMAL] * n
    sc["甲_重入上限"] = exec_sc(s_all, [True] * n, {"出处": "登记第一节第 6 小节情形甲；规格第八节例子二"})
    valid = [True] * n
    for i in range(32, 232):
        valid[i] = False  # 第 32 日缺口未补齐：P_SPX、PR_SPX 至第 94 日、MR 至第 231 日无效
    sc["乙_第32日缺口未补齐"] = exec_sc(s_all, valid, {"出处": "登记第一节第 6 小节情形乙"})
    s_c = list(s_all)
    s_c[32] = LV2
    s_c[33] = LV1
    sc["丙_重入后再升级"] = exec_sc(s_c, [True] * n, {"出处": "登记第一节第 6 小节情形丙"})
    s_v = list(s_all)
    s_v[31] = LV1
    sc["规格例子二_第31日一级"] = exec_sc(s_v, [True] * n, {"出处": "规格第八节例子二末段"})

    # 规格第八节例子一：按登记第一节第 6 小节，阶段恢复由 S 实现；“信号正常”在二级之后先使 S = 一级
    n7 = 7
    s1 = [NORMAL, LV1, LV2, LV2, LV1, NORMAL, NORMAL]
    sc["规格例子一"] = {
        "kind": "exec",
        "axis": axis_of(n7),
        "S": s1,
        "all_valid": [True] * n7,
        "U": [0.0] * n7,
        "j0": 1,
        "meta": {"出处": "规格第八节例子一"},
    }
    s1b = list(s1)
    s1b[5] = LV1
    sc["规格例子一_第5日一级"] = {**sc["规格例子一"], "S": s1b, "meta": {"出处": "规格第八节例子一末句"}}

    # 规格第八节例子三与复核者例子（R2）
    base = [money(90 + 0.1 * i) for i in range(100)]
    tail = ["100.0", "98.5", "97.6", "96.8", "95.5", "94.9", "94.0", "95.0", "97.0", "99.0"]
    after = [money(99.5 + 0.1 * i) for i in range(31)]
    prices3 = base + tail + after
    n3 = len(prices3)

    def prompt_days(lo: int, hi: int) -> list[str]:
        return [LV1 if lo <= i <= hi else NORMAL for i in range(n3)]

    def r2_sc(prices: list[str], s_list: list[str], meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "r2",
            "axis": axis_of(n3),
            "prices": {"SPX": prices},
            "S": s_list,
            "f": 50,
            "E": n3 - 1,
            "meta": meta,
        }

    sc["例子三_102开始"] = r2_sc(prices3, prompt_days(102, 110), {"出处": "规格第八节例子三第一条"})
    sc["例子三_103开始"] = r2_sc(prices3, prompt_days(103, 110), {"出处": "规格第八节例子三第二条"})
    sc["例子三_97至102"] = r2_sc(prices3, prompt_days(97, 102), {"出处": "规格第八节例子三第三条"})
    rev = list(prices3)
    rev[101:110] = ["96.0", "98.0", "94.9", "94.0", "95.0", "97.0", "99.0", "99.2", "99.3"]
    sc["复核者例子"] = r2_sc(rev, prompt_days(102, 110), {"出处": "规格第八节例子三复核者的例子"})

    # 登记第八节第 4 小节：指数 100 → 110 → 100
    sc["净值例子_100_110_100"] = {
        "kind": "exec",
        "axis": axis_of(4),
        "S": [NORMAL] * 4,
        "all_valid": [True] * 4,
        "prices": {"SPX": ["100", "100", "110", "100"], "QQQ": ["100", "100", "110", "100"]},
        "j0": 1,
        "meta": {"出处": "登记第八节第 4 小节"},
    }
    sc["净值例子_缺价"] = {
        **sc["净值例子_100_110_100"],
        "prices": {"SPX": ["100", "100", None, "100"], "QQQ": ["100", "100", "110", "100"]},
        "meta": {"出处": "登记第八节第 4 小节（缺价时无法计算）"},
    }
    return sc


# ---------------------------------------------------------------- 收敛反例（阶段三第五节第 3 部分）


def counterexample() -> dict[str, Any]:
    """平稳价格：κ通道 之后 A₁、A₂ 每日为真、L = 0。

    初始 (正常, 0, 0) 与 (正常, h, h) 在 κ通道 + 1 日 S 同为正常而 c₁、c₂ 不同；
    系统收敛日为 κ通道 + h + 1（从二级出发的运行最晚回到正常）。
    """
    n = 320
    sessions = nyse_sessions("2004-01-02", n)
    return {
        "kind": "full",
        "axis": sessions,
        "calendar": "NYSE",
        "prices": {"SPX": ["100.00"] * n, "QQQ": ["50.00"] * n},
        "params": "registered",
        "meta": {"出处": "阶段三实施指令第五节第 3 部分：灯色相同、计数器不同"},
    }


# ---------------------------------------------------------------- 随机路径（阶段三第五节第 5 部分第 3 项）


@functools.cache
def _sessions_from(start: str) -> tuple[str, ...]:
    import pandas_market_calendars as mcal

    sched = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=f"{int(start[:4]) + 8}-12-31")
    return tuple(x.strftime("%Y-%m-%d") for x in sched.index)


def nyse_sessions(start: str, n: int) -> list[str]:
    days = _sessions_from(start)
    if len(days) < n:
        raise RuntimeError("交易日历长度不足")
    return list(days[:n])


def price_path(rng: np.random.Generator, n: int, p0: float, beta: float) -> tuple[list[float], list[float]]:
    base = rng.normal(0.0003, 0.011, n)
    # 下跌段：使回撤达到 5% 以上并触发 4% 组合止损
    for _ in range(int(rng.integers(1, 4))):
        a = int(rng.integers(220, n - 20))
        ln = int(rng.integers(5, 16))
        base[a : a + ln] -= rng.uniform(0.008, 0.02)
    # 跳空
    for _ in range(int(rng.integers(2, 6))):
        g = int(rng.integers(1, n))
        base[g] += rng.choice([-1.0, 1.0]) * rng.uniform(0.03, 0.07)
    other = beta * base + rng.normal(0.0, 0.006, n)
    s = p0 * np.exp(np.cumsum(base))
    q = (p0 / 10) * np.exp(np.cumsum(other))
    return list(s), list(q)


def inject_ties(rng: np.random.Generator, xs: list[str]) -> list[dict[str, Any]]:
    """并列高点与并列低点：把窗口内的最高（最低）价复制到其后第 3 日。"""
    notes = []
    n = len(xs)
    for kind in ("高点", "低点"):
        for _ in range(2):
            a = int(rng.integers(30, n - 40))
            seg = [Decimal(x) for x in xs[a : a + 30]]
            m = a + (seg.index(max(seg)) if kind == "高点" else seg.index(min(seg)))
            if m + 3 < n:
                xs[m + 3] = xs[m]
                notes.append({"类型": f"并列{kind}", "日": m, "复制到": m + 3})
    return notes


def random_paths() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    plan = (
        [("无缺价", 450)] * 100
        + [("无缺价长路径", 600)] * 20
        + [("单日缺价", 450)] * 25
        + [("连续缺价", 450)] * 25
        + [("跨越63日窗口的缺口", 500)] * 20
        + [("跨越200日窗口的缺口", 900)] * 10
        + [("QQQ晚开始", 480)] * 10
    )
    out = []
    for i, (ptype, n) in enumerate(plan):
        seed = MASTER_SEED + i
        rng = np.random.Generator(np.random.PCG64(seed))
        s, q = price_path(rng, n, 1000.0, float(rng.uniform(0.9, 1.4)))
        spx: list[str | None] = [money(x) for x in s]
        qqq: list[str | None] = [money(x) for x in q]
        notes: dict[str, Any] = {"类型": ptype, "长度": n, "种子": seed}
        notes["并列"] = inject_ties(rng, spx) + inject_ties(rng, qqq)  # type: ignore[arg-type]
        gaps: list[dict[str, Any]] = []
        if ptype == "单日缺价":
            for _ in range(int(rng.integers(1, 4))):
                a = str(rng.choice(["SPX", "QQQ"]))
                d = int(rng.integers(1, n))
                (spx if a == "SPX" else qqq)[d] = None
                gaps.append({"资产": a, "起": d, "长度": 1})
        elif ptype in ("连续缺价", "跨越63日窗口的缺口", "跨越200日窗口的缺口"):
            lo, hi = {"连续缺价": (3, 21), "跨越63日窗口的缺口": (64, 81), "跨越200日窗口的缺口": (201, 221)}[ptype]
            ln = int(rng.integers(lo, hi))
            a = "SPX" if ptype == "跨越200日窗口的缺口" else str(rng.choice(["SPX", "QQQ"]))
            d = int(rng.integers(150, n - ln - 30))
            for k in range(d, d + ln):
                (spx if a == "SPX" else qqq)[k] = None
            gaps.append({"资产": a, "起": d, "长度": ln})
        elif ptype == "QQQ晚开始":
            late = int(rng.integers(30, 81))
            for k in range(late):
                qqq[k] = None
            gaps.append({"资产": "QQQ", "起": 0, "长度": late})
        notes["缺价"] = gaps
        axis = nyse_sessions("2003-01-02", n)
        sc = {
            "kind": "full",
            "axis": axis,
            "calendar": "NYSE",
            "prices": {"SPX": spx, "QQQ": qqq},
            "params": "registered",
            "r1_segments": [[axis[300], axis[380]], [axis[381], axis[n - 1]]],
            "meta": notes,
        }
        out.append((f"path_{i:03d}", sc, notes))
    return out


def stop_reentry_path() -> dict[str, Any]:
    """确定性路径：平稳上涨后急跌 6%，再回升，保证触发 4% 止损与冷却期后的重入。"""
    n = 460
    spx = ramp(300, 800, 1000) + ramp(8, 990, 930) + ramp(152, 935, 1100)
    qqq = ramp(300, 80, 100) + ramp(8, 99, 93) + ramp(152, 93.5, 110)
    return {
        "kind": "full",
        "axis": nyse_sessions("2005-01-03", n),
        "calendar": "NYSE",
        "prices": {"SPX": spx, "QQQ": qqq},
        "params": "registered",
        "meta": {"出处": "阶段三实施指令第五节第 5 部分第 3 项：触发 4% 止损与重入的路径"},
    }


def bootstrap_scenario() -> dict[str, Any]:
    """登记第七节第 3 小节的 5 组（种子, b），各两个长度，前 3 条完整索引序列。长度由本脚本选定（见 README）。"""
    seeds = {20: 20261020, 10: 20261010, 40: 20261040, 60: 20261060, 120: 202610120}
    items = [{"seed": s, "b": b, "n": n, "count": 3} for b, s in seeds.items() for n in (250, 1500)]
    return {"kind": "bootstrap", "items": items, "meta": {"出处": "阶段三实施指令第五节第 2 部分：抽样索引"}}


def confirm_scenario() -> dict[str, Any]:
    rng = np.random.Generator(np.random.PCG64(CONFIRM_SEED))
    n = 760
    s, q = price_path(rng, n, 1000.0, 1.2)
    axis = nyse_sessions("2003-01-02", n)
    return {
        "kind": "confirm",
        "axis": axis,
        "calendar": "NYSE",
        "prices": {"SPX": [money(x) for x in s], "QQQ": [money(x) for x in q]},
        "params": [5, "0.02", 3],
        "window_start": axis[450],
        "half_split": axis[605],
        "meta": {"出处": "登记第七节（构造数据，登记设定 B = 10000 与 5 个种子）", "种子": CONFIRM_SEED},
    }


# ---------------------------------------------------------------- 写出


def write(out: Path, name: str, sc: dict[str, Any], manifest: list[dict[str, Any]], cat: str) -> None:
    sc = {"name": name, **sc}
    p = out / f"{name}.json"
    data = json.dumps(sc, ensure_ascii=False).encode("utf-8")
    p.write_bytes(data)
    manifest.append(
        {
            "name": name,
            "category": cat,
            "kind": sc["kind"],
            "file": p.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "meta": sc.get("meta"),
        }
    )


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("用法：python gen_scenarios_v20.py 输出目录 [all|manual]", file=sys.stderr)
        return 2
    out = Path(argv[1])
    mode = argv[2] if len(argv) == 3 else "all"
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for name, sc in manual_scenarios().items():
        write(out, name, sc, manifest, "人工例子")
    if mode == "all":
        write(out, "收敛反例", counterexample(), manifest, "收敛反例")
        write(out, "止损与重入", stop_reentry_path(), manifest, "随机路径（确定性补充）")
        for name, sc, _ in random_paths():
            write(out, name, sc, manifest, "随机路径")
        write(out, "抽样索引", bootstrap_scenario(), manifest, "抽样索引")
        write(out, "确认性检验", confirm_scenario(), manifest, "确认性检验")
    (out / "manifest.json").write_text(
        json.dumps({"master_seed": MASTER_SEED, "scenarios": manifest}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
