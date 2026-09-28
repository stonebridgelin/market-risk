# ruff: noqa
# 长期使用的独立复核脚本（docs/audit/，不属于 market_risk 程序包），不按项目代码规范做类型标注。
"""独立复核：不导入 market_risk 的任何模块，只读 data/market/ 的原始 CSV，按 SOP/SPEC 原文重新计算。

用法：uv run python docs/audit/独立复核/audit_indep.py <项目根目录> <输出目录>
输出：<输出目录>/score_compare.txt、sample_compare.md、zigzag_compare.md、label_compare.txt
第三方库只用 pandas_market_calendars（NYSE 交易日）；不 import market_risk。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import random
import sys
from decimal import ROUND_HALF_UP, Decimal as D
from pathlib import Path

import pandas_market_calendars as pmc

ROOT = Path(sys.argv[1])
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "audit_out"
OUT.mkdir(exist_ok=True)
BACKTESTS = ROOT / "results/MARKET/risk_scoring/backtests"
RUN = BACKTESTS / json.loads((BACKTESTS / "official.json").read_text(encoding="utf-8"))["run_id"]
C2 = D("0.01")


def rows(name):
    with open(ROOT / "data/market/daily" / f"{name}.csv", encoding="utf-8") as f:
        return {dt.date.fromisoformat(r["date"]): r for r in csv.DictReader(f)}


def p2(s):
    return D(s).quantize(C2, ROUND_HALF_UP)


# ---------------- 交易日（NYSE，第三方库，与项目 calendar.py 无关） ----------------
sched = pmc.get_calendar("NYSE").schedule("1989-01-01", "2026-12-31")
TD = [ts.date() for ts in sched.index]
TI = {d: i for i, d in enumerate(TD)}

# ---------------- 数据 ----------------
ETF = {s: rows(s) for s in ("SPY", "QQQ", "RSP")}
CL = {s: {d: p2(r["value"]) for d, r in ETF[s].items() if r["value"]} for s in ETF}
CL_ROUND = {s: {d: round(float(r["value"]), 2) for d, r in ETF[s].items() if r["value"]} for s in ETF}
FI = {d: p2(r["value"]) for d, r in rows("S5FI").items() if r["value"]}
TW = {d: p2(r["value"]) for d, r in rows("S5TW").items() if r["value"]}
VX = rows("VIXCLS")
VXC = {d: p2(r["value"]) for d, r in rows("VIX_CBOE").items() if r["value"]}
UST = {d: p2(r["value"]) for d, r in rows("UST10Y").items() if r["value"]}
OAS_ROWS = rows("BAMLH0A0HYM2")
OAS = {d: p2(r["value"]) for d, r in OAS_ROWS.items() if r["value"]}
EXCLUDED = {dt.date(2015, 1, 19)}   # config/data_decisions.yaml：BAMLH0A0HYM2 exclude
SPX = {d: p2(r["value"]) for d, r in rows("SPX").items() if r["value"]}

BOND_DAYS = {d for d in UST if d.weekday() < 5}   # 债市营业日 = 财政部有数值的工作日（SPEC 5.2）


def vix(d):
    r = VX.get(d)
    if r is not None and r["value"] and r["source"] == "fred":
        return p2(r["value"])
    return VXC.get(d)


def ma(sym, i, n):
    days = TD[i - n + 1: i + 1]
    vals = [CL[sym].get(x) for x in days]
    if any(v is None for v in vals):
        return None
    return sum(vals) / D(n)


def etf_ok(sym, base):
    """回看窗口（与项目相同的 420 个自然日内全部交易日）无缺日。"""
    lo = base - dt.timedelta(days=420)
    first = min(d for d in CL[sym] if d >= lo)
    return all(d in CL[sym] for d in TD[TI[first]: TI[base] + 1])


def three_seg(sym, i):
    c = CL[sym]
    base = c[TD[i]]
    for k in range(20, 1, -1):               # d1 = T−20 … T−2
        j = i - k
        d1 = c[TD[j]]
        lc = min(c[TD[x]] for x in range(j - 20, j))
        if not d1 < lc:
            continue
        if not any(d1 < c[TD[x]] <= lc for x in range(j + 1, i)):
            continue
        if base < d1:
            return True
    return False


def bool_opts(known_val):
    return [known_val] if known_val is not None else [True, False]


# ---------------- 各维度（SOP 7.2 / 7.3 原文） ----------------
def price_v2m(i, missing):
    if missing:
        return None, (0, 1, 2)
    b50 = b20 = 0
    for s in ("SPY", "QQQ", "RSP"):
        c = CL[s][TD[i]]
        b50 += c < ma(s, i, 50)
        b20 += c < ma(s, i, 20)
    seg = any(three_seg(s, i) for s in ("SPY", "QQQ", "RSP"))
    spy = CL["SPY"][TD[i]] < ma("SPY", i, 200)
    if b50 >= 2 or seg or spy:
        return 2, (2,)
    if b50 == 0 and b20 <= 1:
        return 0, (0,)
    return 1, (1,)


def price_v3r1(i, missing):
    if missing:
        return None, (0, 1, 2)
    both = b50 = b20 = 0
    for s in ("SPY", "QQQ", "RSP"):
        c, m5, m20, m50 = CL[s][TD[i]], ma(s, i, 5), ma(s, i, 20), ma(s, i, 50)
        b50 += c < m50
        b20 += c < m20
        both += (c < m50 and m5 < m50)
    spy = CL["SPY"][TD[i]] < ma("SPY", i, 200)
    if both >= 2 or spy:
        return 2, (2,)
    if b50 == 0 and b20 <= 1:
        return 0, (0,)
    return 1, (1,)


def breadth(i, version, near_high):
    """F、W 已知时，F5、W5 缺失按"F<F5 / W<W5 两种真值"枚举（与项目的临界点取值方法不同）。
    near_high 为 None 表示 SPY 缺失（v2-M 条件 b 两种假设）。"""
    d, d5 = TD[i], TD[i - 5]
    f, w = FI.get(d) if d in TW else None, TW.get(d) if d in FI else None
    f5, w5 = (FI.get(d5), TW.get(d5)) if (d5 in FI and d5 in TW) else (None, None)
    if f is None or w is None:
        return "BASE_MISSING", None
    low = min(f, w)
    outs = set()
    for ff5 in bool_opts(None if f5 is None else f < f5):
        if f5 is None and ff5 and f >= 100:
            continue
        for ww5 in bool_opts(None if w5 is None else w < w5):
            if w5 is None and ww5 and w >= 100:
                continue
            for nh in bool_opts(near_high) if version == "v2-M" else [None]:
                a = low < 40 and ff5 and ww5
                b = (f < 40 and nh) if version == "v2-M" else (f < 40 and ff5)
                outs.add(2 if (a or b) else 0 if low >= 50 else 1)
    outs = tuple(sorted(outs))
    return (outs[0] if len(outs) == 1 else None), outs


def vix_dim(i):
    v, v5 = vix(TD[i]), vix(TD[i - 5])

    def rule(vv, g):
        if vv >= 25 or (vv >= 20 and g >= 30):
            return 2
        if vv < 18 and g < 20:
            return 0
        return 1
    if v is not None and v5 is not None:
        s = rule(v, (v / v5 - 1) * 100)
        return s, (s,)
    if v is not None:   # V5 ∈ [0.01, 200]
        gs = [(v / D(200) - 1) * 100, D("19.99"), D(20), D("29.99"), D(30), (v / D("0.01") - 1) * 100]
        gmin, gmax = (v / D(200) - 1) * 100, (v / D("0.01") - 1) * 100
        outs = tuple(sorted({rule(v, g) for g in gs if gmin <= g <= gmax}))
    else:
        outs = (0, 1, 2)
    return (outs[0] if len(outs) == 1 else None), outs


def rates(i):
    win = TD[i - 19: i + 1]
    vals = {d: UST[d] for d in win if d in UST}
    y, y20 = UST.get(TD[i]), UST.get(TD[i - 20])
    others = [v for d, v in vals.items() if d != TD[i]]
    if y is None:
        # y 缺失（基准日债市休市）：按逻辑推出可能取值。0：存在 y<H；1：存在 y≥H_others 且 y<y20+0.25；
        # 2：存在 y≥H_others 且 y≥y20+0.25（y 取值上限 30%）
        ho = max(others) if others else None
        poss = set()
        if ho is not None:
            poss.add(0)
        if y20 is None:
            poss |= {1, 2}
        else:
            if ho is None or ho < y20 + D("0.25"):
                poss.add(1)
            poss.add(2)
        poss = tuple(sorted(poss))
        return (poss[0] if len(poss) == 1 else None), poss, vals, y, y20
    h = max([y, *others])
    if y < h:
        return 0, (0,), vals, y, y20
    if y20 is None:
        return None, (1, 2), vals, y, y20
    s = 2 if (y - y20) * 100 >= 25 else 1
    return s, (s,), vals, y, y20


def credit_rule(o1, o6):
    dd = (o1 - o6) * 100
    if dd >= 20 or o1 >= 4:
        return 2
    if dd <= 5 and o1 < 4:
        return 0
    return 1


def prev_bond(d, n):
    out, cur = [], d
    while len(out) < n:
        cur -= dt.timedelta(days=1)
        if cur in BOND_DAYS:
            out.append(cur)
    return out


def v3r1_credit(base):
    o1d = prev_bond(base, 1)[0]
    o6d = prev_bond(o1d, 5)[-1]
    o1 = OAS.get(o1d) if o1d not in EXCLUDED else None
    o6 = OAS.get(o6d) if o6d not in EXCLUDED else None
    if o1 is None:
        return None, (0, 1, 2), o1d, o6d, o1, o6
    if o6 is None:
        if o1 >= 4:
            return 2, (2,), o1d, o6d, o1, o6
        return None, (0, 1, 2), o1d, o6d, o1, o6
    s = credit_rule(o1, o6)
    return s, (s,), o1d, o6d, o1, o6


def month_end(d):
    return (d + dt.timedelta(days=1)).month != d.month


def v2m_obs_before(base):
    """v2-M 观测：有数值；排除债市休市日（工作日且财政部无数值）的观测，自然月末除外；排除已裁定 exclude。"""
    out = []
    d = base
    while len(out) < 6:
        d -= dt.timedelta(days=1)
        if d not in OAS or d in EXCLUDED:
            continue
        if d.weekday() < 5 and d not in BOND_DAYS and not month_end(d):
            continue
        out.append(d)
    return out


def v2m_credit(base):
    obs = v2m_obs_before(base)
    o1d, o6d = obs[0], obs[5]
    o1, o6 = OAS[o1d], OAS[o6d]
    lag = sum(1 for x in TD[TI[base] - 15: TI[base] + 1] if o1d < x <= base)
    s = credit_rule(o1, o6)
    if lag > 1:
        s = min(s, 1)
    return s, (s,), o1d, o6d, o1, o6, lag


def total(dims):
    lo = sum(min(p) for _, p in dims)
    hi = sum(max(p) for _, p in dims)
    return lo, hi


def fmt(s, p):
    return str(s) if s is not None else f"待补{list(p)}"


# ---------------- 正式回测输出 ----------------
def read_run(name):
    with open(RUN / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


official = {(r["date"], r["version"]): r for r in read_run("daily_scores.csv")}
metrics = {r["date"]: r for r in read_run("daily_metrics.csv")}
bases = sorted({dt.date.fromisoformat(d) for d, _ in official})

score_diffs, base_missing, round_diffs, ties = [], [], [], []
indep = {}
for base in bases:
    i = TI[base]
    missing = [s for s in ("SPY", "QQQ", "RSP") if base not in CL[s] or not etf_ok(s, base)]
    near_high = None
    if "SPY" not in missing:
        hi20 = max(CL["SPY"][x] for x in TD[i - 19: i + 1])
        near_high = CL["SPY"][base] >= hi20 * D("0.98")
        for s in ("SPY", "QQQ", "RSP"):
            for x in TD[i - 199: i + 1]:
                if x in CL[s] and x in CL_ROUND[s] and D(repr(CL_ROUND[s][x])) != CL[s][x]:
                    round_diffs.append((s, x))
            c = CL[s][base]
            for n in (5, 20, 50, 200):
                m = ma(s, i, n)
                if m is not None and c == m:
                    ties.append((base, s, f"close=MA{n}", c))
            if ma(s, i, 5) == ma(s, i, 50):
                ties.append((base, s, "MA5=MA50", ma(s, i, 5)))
    res = {}
    rv = rates(i)
    vx = vix_dim(i)
    c3 = v3r1_credit(base)
    c2 = v2m_credit(base)
    for ver in ("v2-M", "v3-R1"):
        pr = price_v2m(i, missing) if ver == "v2-M" else price_v3r1(i, missing)
        br = breadth(i, ver, near_high)
        if br[0] == "BASE_MISSING":
            base_missing.append((base, ver))
            br = (None, None)
        cr = c2[:2] if ver == "v2-M" else c3[:2]
        dims = [pr, br, vx, rv[:2], cr]
        res[ver] = dims
        off = official[(base.isoformat(), ver)]
        names = ("price", "breadth", "vix", "rates", "credit")
        for (s, p), nm in zip(dims, names, strict=True):
            if p is None:
                continue
            if fmt(s, p) != off[nm]:
                score_diffs.append((base, ver, nm, fmt(s, p), off[nm]))
        if all(p is not None for _, p in dims):
            lo, hi = total(dims)
            if (str(lo), str(hi)) != (off["total_min"], off["total_max"]):
                score_diffs.append((base, ver, "total", f"{lo}-{hi}", f"{off['total_min']}-{off['total_max']}"))
    indep[base] = (res, rv, vx, c3, c2, missing, near_high)

with open(OUT / "score_compare.txt", "w", encoding="utf-8") as f:
    f.write(f"基准日 {len(bases)} 个 × 2 个版本\n")
    f.write(f"维度或总分不一致：{len(score_diffs)}\n")
    for x in score_diffs[:200]:
        f.write(f"  {x}\n")
    f.write(f"基准日广度缺失（未独立枚举）：{len(base_missing)} {base_missing[:20]}\n")
    f.write(f"四舍五入方式差异（ROUND_HALF_UP vs round()，回看200日内）：{len(set(round_diffs))} {sorted(set(round_diffs))[:20]}\n")
    f.write(f"收盘价恰等于均线 / MA5 恰等于 MA50 的情形：{len(ties)}\n")
    for t in ties:
        f.write(f"  {t}\n")

# ---------------- 抽样 30+ 个基准日的中间值比对 ----------------
special = [dt.date(2008, 8, 11), dt.date(2008, 10, 14), dt.date(2008, 11, 28), dt.date(2008, 12, 31),
           dt.date(2009, 1, 20), dt.date(2009, 7, 17), dt.date(2010, 11, 12), dt.date(2011, 8, 31),
           dt.date(2012, 11, 23), dt.date(2012, 10, 31), dt.date(2013, 7, 3), dt.date(2013, 9, 3),
           dt.date(2014, 10, 14), dt.date(2015, 1, 20), dt.date(2015, 4, 6), dt.date(2016, 6, 27),
           dt.date(2017, 11, 24), dt.date(2018, 12, 6), dt.date(2019, 12, 24), dt.date(2020, 3, 16),
           dt.date(2021, 4, 5), dt.date(2021, 6, 1), dt.date(2022, 6, 21), dt.date(2022, 7, 5),
           dt.date(2023, 10, 10), dt.date(2024, 11, 29), dt.date(2025, 10, 14), dt.date(2025, 11, 28),
           dt.date(2026, 4, 6), dt.date(2026, 9, 25)]
rnd = random.Random(20260927)
by_year = {}
for b in bases:
    by_year.setdefault(b.year, []).append(b)
sample = sorted(set(special) | {rnd.choice(v) for v in by_year.values()})
checks = []


def q6(x):
    return None if x is None else x.quantize(D("0.000001"), ROUND_HALF_UP)


def cmp(label, mine, theirs):
    t = theirs if theirs != "" else None
    m = None if mine is None else (mine if isinstance(mine, str) else format(q6(D(mine)), "f"))
    ok = (m == t) if not (m is None and t is None) else True
    checks.append(ok)
    return f"{label}={m}{'' if ok else f' ≠ 正式 {t}'}"


lines = []
for base in sample:
    i = TI[base]
    met = metrics[base.isoformat()]
    res, rv, vx, c3, c2, missing, near_high = indep[base]
    t5, t20 = TD[i - 5], TD[i - 20]
    parts = [f"## {base}（T−5 {t5}，T−20 {t20}，窗口 {TD[i-19]} 至 {base}；提前收盘={base in set(pmc.get_calendar('NYSE').early_closes(sched).index.date)}）"]
    for s in ("SPY", "QQQ", "RSP"):
        if s in missing:
            parts.append(f"{s} 缺失")
            continue
        parts.append(" ".join([cmp(f"{s}.close", CL[s][base], met[f"{s}.close"])] +
                              [cmp(f"{s}.ma{n}", ma(s, i, n), met[f"{s}.ma{n}"]) for n in (5, 20, 50, 200)]))
    hi20 = max(CL["SPY"][x] for x in TD[i - 19: i + 1])
    parts.append(cmp("SPY20日最高", hi20, met["spy_window_max_close"]))
    parts.append(" ".join([cmp("F", FI.get(base), met["F"]), cmp("W", TW.get(base), met["W"]),
                           cmp("F5(T−5)", FI.get(t5), met["F5"]), cmp("W5(T−5)", TW.get(t5), met["W5"])]))
    v, v5 = vix(base), vix(t5)
    g = None if v is None or v5 is None else (v / v5 - 1) * 100
    parts.append(" ".join([cmp("VIX", v, met["VIX"]), cmp("VIX_T5", v5, met["VIX_T5"]), cmp("g%", g, met["g_pct"])]))
    _, _, vals, y, y20 = rv
    h = max(vals.values()) if vals else None
    hdate = min(d for d, x in vals.items() if x == h).isoformat() if vals else None
    dy = None if y is None or y20 is None else (y - y20) * 100
    parts.append(" ".join([cmp("y", y, met["y"]), cmp("H", h, met["H"]), cmp("H日期", hdate, met["H_date"]),
                           cmp("y(T−20)", y20, met["y_t20"]), cmp("Δy bp", dy, met["dy_bp"])]))
    _, _, o1d, o6d, o1, o6 = c3
    parts.append(" ".join([cmp("v3R1 O1日", o1d.isoformat(), met["oas_o1_date_v3r1"]),
                           cmp("O6日", o6d.isoformat(), met["oas_o6_date_v3r1"]),
                           cmp("O1", o1, met["oas_o1_v3r1"]), cmp("O6", o6, met["oas_o6_v3r1"]),
                           cmp("ΔOAS", None if o1 is None or o6 is None else (o1 - o6) * 100, met["doas_v3r1_bp"])]))
    _, _, o1d, o6d, o1, o6, lag = c2
    parts.append(" ".join([cmp("v2M O1日", o1d.isoformat(), met["oas_o1_date_v2m"]),
                           cmp("O6日", o6d.isoformat(), met["oas_o6_date_v2m"]),
                           cmp("O1", o1, met["oas_o1_v2m"]), cmp("O6", o6, met["oas_o6_v2m"]),
                           cmp("ΔOAS", (o1 - o6) * 100, met["doas_v2m_bp"]), f"滞后{lag}"]))
    for ver in ("v2-M", "v3-R1"):
        off = official[(base.isoformat(), ver)]
        dims = res[ver]
        mine = [fmt(s, p) if p is not None else "（未独立枚举）" for s, p in dims]
        theirs = [off[k] for k in ("price", "breadth", "vix", "rates", "credit")]
        ok = all(m == t or m == "（未独立枚举）" for m, t in zip(mine, theirs, strict=True))
        checks.append(ok)
        tot = f"{total(dims)}" if all(p is not None for _, p in dims) else "-"
        parts.append(f"{ver}：独立 {mine} 总分范围 {tot}；正式 {theirs} 总分 {off['total'] or off['total_min'] + '–' + off['total_max']}"
                     f" {'一致' if ok else '【不一致】'}")
    lines.append("\n".join(parts))

with open(OUT / "sample_compare.md", "w", encoding="utf-8") as f:
    f.write(f"抽样 {len(sample)} 个基准日；比对项 {len(checks)}，不一致 {checks.count(False)}\n\n")
    f.write("\n\n".join(lines) + "\n")

# ---------------- 独立 ZigZag ----------------
def my_zigzag(series, lv):
    """按已确认口径的文字重新实现：候选高点从序列第一天开始；严格创新高才更新；
    跌幅≥门槛确认高点；此后严格创新低才更新低点；自低点反弹≥门槛确认低点，确认日作为新的候选高点。"""
    out = []
    state = "up"
    hd, hv = series[0]
    ld = lv_ = None
    for d, c in series[1:]:
        if state == "up":
            if c > hv:
                hd, hv = d, c
            elif (c / hv - 1) <= -lv:
                state, ld, lv_ = "down", d, c
        else:
            if c < lv_:
                ld, lv_ = d, c
            elif (c / lv_ - 1) >= lv:
                out.append((hd, hv, ld, lv_, d))
                state, hd, hv = "up", d, c
    if state == "down":
        out.append((hd, hv, ld, lv_, None))
    return out


QQQP = {d: p2(r["value"]) for d, r in ETF["QQQ"].items() if r["value"]}
eps = read_run("pullback_episodes.csv")
zz_lines = []
mism = 0
for sym, ser in (("SPX", sorted(SPX.items())), ("QQQ", sorted(QQQP.items()))):
    for lvs in (("0.05", "0.10", "0.20") if sym == "SPX" else ("0.07", "0.10", "0.20")):
        lv = D(lvs)
        mine = my_zigzag(ser, lv)
        lvtxt = format((lv * 100).normalize(), "f")
        theirs = {r["high_date"]: r for r in eps if r["symbol"] == sym and r["level"] == lvtxt}
        mine_in = [m for m in mine if m[0] >= dt.date(2008, 1, 1) and m[0] < dt.date(2023, 1, 1)]
        for hd, hv, ld, lval, cd in mine_in:
            r = theirs.get(hd.isoformat())
            dd = ((lval / hv - 1) * 100).quantize(D("0.0001"), ROUND_HALF_UP)
            if r is None:
                if hd >= dt.date(2008, 8, 11) or (cd or dt.date.max) >= dt.date(2008, 8, 11):
                    zz_lines.append(f"{sym} {lvtxt}% 独立有 {hd}→{ld} {dd}% 正式无")
                    mism += 1
                continue
            if r["status"].startswith("跨入保留期"):
                continue
            ok = (r["low_date"] == ld.isoformat() and r["high_close"] == format(hv, "f")
                  and r["low_close"] == format(lval, "f") and r["drawdown_pct"] == format(dd, "f")
                  and (r["confirm_date"] or None) == (cd.isoformat() if cd else None))
            if not ok:
                mism += 1
                zz_lines.append(f"{sym} {lvtxt}% {hd}: 独立 {ld} {dd}% 确认{cd}；正式 {r['low_date']} {r['drawdown_pct']}% 确认{r['confirm_date']}")
        mine_highs = {m[0].isoformat() for m in mine}
        for hd, r in theirs.items():
            if hd not in mine_highs:
                mism += 1
                zz_lines.append(f"{sym} {lvtxt}% 正式有 {hd} 独立无")

focus = [(dt.date(2011, 7, 1), dt.date(2011, 10, 31)), (dt.date(2015, 5, 1), dt.date(2015, 9, 30)),
         (dt.date(2018, 9, 1), dt.date(2018, 12, 31)), (dt.date(2020, 2, 1), dt.date(2020, 4, 30)),
         (dt.date(2022, 3, 1), dt.date(2022, 6, 30))]
with open(OUT / "zigzag_compare.md", "w", encoding="utf-8") as f:
    f.write(f"全部层级（高点在 2008-01-01 至 2022-12-31）逐段比对，不一致 {mism}\n")
    for x in zz_lines:
        f.write(f"- {x}\n")
    for a, b in focus:
        f.write(f"\n### {a} 至 {b}\n\n| 标的 | 层级 | 高点日 | 高点 | 低点日 | 低点 | 跌幅 | 确认日 | 正式 |\n|---|---|---|---|---|---|---|---|---|\n")
        for sym, ser in (("SPX", sorted(SPX.items())), ("QQQ", sorted(QQQP.items()))):
            for lvs in (("0.05", "0.10", "0.20") if sym == "SPX" else ("0.07", "0.10", "0.20")):
                lv = D(lvs)
                lvtxt = format((lv * 100).normalize(), "f")
                theirs = {r["high_date"]: r for r in eps if r["symbol"] == sym and r["level"] == lvtxt}
                for hd, hv, ld, lval, cd in my_zigzag(ser, lv):
                    if ld < a or hd > b:
                        continue
                    r = theirs.get(hd.isoformat())
                    if cd is None or cd >= dt.date(2023, 1, 1):
                        # 保留期屏蔽原则：低点确认需要保留期的数据，独立复核的输出同样只写高点
                        masked_ok = r is not None and r["status"].startswith("跨入保留期")
                        f.write(f"| {sym} | {lvtxt}% | {hd} | {hv} | （屏蔽） | （屏蔽） | （屏蔽） | （屏蔽） | "
                                f"{'正式同样为跨入保留期，未解锁' if masked_ok else '【正式未屏蔽】'} |\n")
                        continue
                    dd = ((lval / hv - 1) * 100).quantize(D("0.0001"), ROUND_HALF_UP)
                    same = r is not None and r["low_date"] == ld.isoformat() and r["drawdown_pct"] == format(dd, "f")
                    if same:
                        verdict = "一致"
                    elif r is None:
                        verdict = "正式无"
                    elif r["status"].startswith("跨入保留期") and cd is not None and cd >= dt.date(2023, 1, 1):
                        verdict = "正式为跨入保留期（低点确认在保留期，已按规则屏蔽）"
                    else:
                        verdict = "不一致：" + r["low_date"] + " " + r["drawdown_pct"]
                    f.write(f"| {sym} | {lvtxt}% | {hd} | {hv} | {ld} | {lval} | {dd}% | {cd} | {verdict} |\n")

# ---------------- 结果标签（全部基准日）----------------
outs = {r["base_date"]: r for r in read_run("outcomes.csv")}
lab_diff = 0
lab_lines = []
for base in bases:
    i = TI[base]
    if i + 20 >= len(TD) or TD[i + 20] >= dt.date(2023, 1, 1):
        continue
    win = TD[i + 1: i + 21]
    if any(d not in SPX or d not in QQQP for d in win):
        continue
    sd = [(SPX[d] / SPX[base] - 1) * 100 for d in win]
    qd = [(QQQP[d] / QQQP[base] - 1) * 100 for d in win]
    ev = [d for d, a, b in zip(win, sd, qd, strict=True) if a <= -5 or b <= -7]
    near = not ev and (min(sd) <= -4 or min(qd) <= -6)
    r = outs.get(base.isoformat())
    mine = ("是" if ev else "否", ev[0].isoformat() if ev else "", "是" if near else "否",
            format(min(sd).quantize(D("0.0001"), ROUND_HALF_UP), "f"))
    theirs = None if r is None else (r["is_event"], r["event_date"], r["is_near_event"], r["spx_drawdown_from_base"])
    if mine != theirs:
        lab_diff += 1
        lab_lines.append(f"{base}: 独立 {mine} 正式 {theirs}")
with open(OUT / "label_compare.txt", "w", encoding="utf-8") as f:
    f.write(f"结果标签不一致 {lab_diff}\n" + "\n".join(lab_lines[:50]) + "\n")
print("done", len(score_diffs), checks.count(False), mism, lab_diff)
