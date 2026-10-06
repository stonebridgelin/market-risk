"""N4：波段预警 v2.0 两层独立回算脚本（N1 recompute_a.py、N2 recompute_b.py）的构造验收。

编写：独立回算脚本会话。依据：实施指令修订六第六节第 1 小节（N4 矩阵）与第七节。
三个层次：
  (a) 纯函数小样例：以 importlib.util.spec_from_file_location 按路径加载 N1、N2，核对人工推算值；
  (b) CLI：测试内程序化生成完整结构的构造结果目录（27 个候选 + 主参照 + 一直持有 + 均线对照 + 恒定仓位，n = 6），
      以子进程运行两脚本（timeout 必填），核对报告 JSON；
  (c) 故障注入与写入中断；另含 AST 导入白名单与写入位置检查。
全部期望值由人工按登记推算，推算过程写在注释与 *_HAND 常量旁；不读取任何项目输出或演习产物；不导入 market_risk。
构造目录的生成函数是测试代码的一部分，其正确性由 (a) 层小样例与人工期望值表（test_generator_matches_hand_table）
共同约束，不由项目代码证明。脚本不设简化模式开关，CLI 永远使用完整 27 组集合。
"""

import ast
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "docs" / "audit" / "独立回算" / "v20"
SCRIPT_A = SCRIPT_DIR / "recompute_a.py"
SCRIPT_B = SCRIPT_DIR / "recompute_b.py"
SUBPROCESS_TIMEOUT = 120

# 测试侧例外登记（逐处）：子进程、按路径加载、文件读写。
TEST_EXCEPTIONS = (
    ("subprocess.run", "run_raw", "以子进程运行 N1、N2；timeout=SUBPROCESS_TIMEOUT；登记为子进程测试"),
    ("importlib.util.spec_from_file_location", "load_script", "按路径加载 N1、N2，测试其纯函数"),
    ("Path.write_bytes", "write_project", "在 tmp_path 下写构造结果目录（不写任何真实目录）"),
    (
        "Path.write_bytes",
        "test_a_report_write_failure、test_b_report_write_failure",
        "在 tmp_path 下建一个普通文件作为不可写的父路径",
    ),
    ("Path.read_text", "run_script、run_in_process", "读脚本写出的报告 JSON（tmp_path 下）"),
    ("Path.read_text", "test_scripts_import_whitelist_and_write_sites", "读 N1、N2 与 N4 自身源文件做 AST 检查"),
    ("import sys", "模块顶部", "只取 sys.executable 运行子进程（勘误补充单 K2 已加入 N4 白名单）"),
)

# ---------------------------------------------------------------- 冻结登记（测试侧独立书写）

KEYS = tuple(f"K={k},θ_P={t},h={h}" for k in (3, 5, 10) for t in ("0.015", "0.02", "0.025") for h in (1, 3, 5))
REF = "主参照"
HOLD = "一直持有"
AVG1 = "200 日均线一级版"
AVG2 = "带缓冲带的 200 日均线二级版（101%/99%）"
POS = ("正常", "一级", "二级")
WEIGHT_TABLE = (("正常", 0.6, 0.4), ("一级", 0.6, 0.0), ("二级", 0.3, 0.0), ("全部现金", 0.0, 0.0))
SEGMENT_BOUNDS = (
    ("1999—2009", "1998-12-31", "2009-12-31"),
    ("2010—2016", "2009-12-31", "2016-12-30"),
    ("熊市一", "2000-03-24", "2002-10-09"),
    ("熊市二", "2007-10-09", "2009-03-09"),
)

# ---------------------------------------------------------------- 构造日期与价格

HISTORY = ("2000-03-16", "2000-03-17")  # 评价窗口前的两个轴上日期；s0 = 2000-03-17 为首个信号日
DAYS = ("2000-03-20", "2000-03-21", "2000-03-22", "2000-03-23", "2000-03-24", "2000-03-27", "2000-03-28")
N = 6
J0 = 2
SPX_TEXT = ("99.00", "100.00", "100.00", "102.00", "104.00", "93.60", "96.00", "99.00", "100.00")
QQQ_TEXT = ("49.50", "50.00", "50.00", "51.00", "52.00", "46.80", "48.00", "49.50", "50.00")

# ---------------------------------------------------------------- 人工期望值表（推算过程见注释）

# QQQ 收盘恰为 SPX 的一半，两资产区间收益相同，U_i = 0.5·(r + r) = r_SPX,i：
# U1 = 102/100 − 1 = 0.02；U2 = 104/102 − 1 = 1/51；U3 = 93.6/104 − 1 = −0.1；
# U4 = 96/93.6 − 1 = 1/39；U5 = 99/96 − 1 = 0.03125；U6 = 100/99 − 1 = 1/99。
U_HAND = (0.02, 1 / 51, -0.1, 1 / 39, 0.03125, 1 / 99)
# 候选 o = 9a + 3b + c（a、b、c 依次为 K、θ_P、h 的登记下标），
# 计划目标 t0…t6 = (P[a], 正常, P[b], 正常, P[c], 正常, 正常)，
# P = (正常, 一级, 二级)，暴露 e = 1.4、0.6、0.3。除区间 3（U3 = −0.1，由 b 决定）外各区间 U > 0，净值只在区间 3 下跌：
# 信号 MDD = e_b × 0.1（b = 0、1、2 时为 0.14、0.06、0.03）；一直持有 MDD = 1.4 × 0.1 = 0.14。
# R1：e_b·0.1 ≤ 0.5 × 0.14 = 0.07 ⇔ b ≠ 0。
R1_HAND = tuple((order // 3) % 3 != 0 for order in range(27))
SIGNAL_MDD_HAND = (0.14, 0.06, 0.03)  # 按 b
HOLD_MDD_HAND = 0.14
# R2：类别表 L = [持续, 新, 持续, 左截断, 漏报, 新, 迟到, 持续, 输入不足]，事件 j = 0…4 取 L[(s + j) mod 9]，
# SPX 的 s = o mod 9，QQQ 的 s = (o + 1) mod 9。逐个起点人工计数（分母、达标、只计新提示、5·达标 ≥ 3·分母）：
R2_BY_START_HAND = (
    (4, 3, 1, True),  # s0：持续 新 持续 左截断 漏报 → 15 ≥ 12
    (4, 3, 2, True),  # s1：新 持续 左截断 漏报 新 → 15 ≥ 12
    (4, 2, 1, False),  # s2：持续 左截断 漏报 新 迟到 → 10 < 12
    (4, 2, 1, False),  # s3：左截断 漏报 新 迟到 持续 → 10 < 12
    (5, 2, 1, False),  # s4：漏报 新 迟到 持续 输入不足 → 10 < 15（输入不足计入分母）
    (5, 3, 1, True),  # s5：新 迟到 持续 输入不足 持续 → 15 ≥ 15（恰 60%）
    (5, 3, 1, True),  # s6：迟到 持续 输入不足 持续 新 → 15 ≥ 15
    (5, 4, 1, True),  # s7：持续 输入不足 持续 新 持续 → 20 ≥ 15
    (4, 3, 1, True),  # s8：输入不足 持续 新 持续 左截断 → 15 ≥ 12
)
# 可行 ⇔ R1（b ≠ 0）且两资产 R2 满足（o mod 9 ∈ {0,5,6,7,8}，因 s 与 s + 1 须同在 {0,1,5,6,7,8}）；
# o mod 9 = 3b + c，b ≠ 0 时只剩 {5,6,7,8}：
FEASIBLE_HAND = (5, 6, 7, 8, 14, 15, 16, 17, 23, 24, 25, 26)
# 可行者中 ln W 最大：a = 0（区间 1 的 U > 0，暴露 1.4 最好）、b = 2（区间 3 下跌，暴露 0.3 最好）、
# c = 0（区间 5 的 U > 0）
# → o = 0 + 6 + 0 = 6，唯一最大，并列组只含它。
SELECTED_HAND = "K=3,θ_P=0.025,h=1"
M_HAND = (
    math.log1p(1.4 * 0.02)
    + math.log1p(1.4 / 51)
    + math.log1p(0.3 * -0.1)
    + math.log1p(1.4 / 39)
    + math.log1p(1.4 * 0.03125)
    + math.log1p(1.4 / 99)
)
# 切换：t0→t1 在 a ≠ 0 时切换一次；b ≠ 0 时 t1→t2、t2→t3 两次；c ≠ 0 时 t3→t4、t4→t5 两次。
SWITCHES_HAND = (0, 2, 2, 2, 4, 4, 2, 4, 4, 1, 3, 3, 3, 5, 5, 3, 5, 5, 1, 3, 3, 3, 5, 5, 3, 5, 5)
# 幅度：|Δe| 一级 0.8、二级 1.1；幅度 = m(a) + 2·m(b) + 2·m(c)。
MAGNITUDE_STEP_HAND = (0.0, 0.8, 1.1)
# 恒定仓位：主参照 t0…t5 = (二级, 二级, 一级, 正常, 正常, 正常)：core 和 3.0、leverage 和 1.2，
# 除以 6 → (0.5, 0.2)，ē = 0.9；
# 选定候选 o = 6 的 t0…t5 = (正常, 正常, 二级, 正常, 正常, 正常)：core 和 3.3、leverage 和 2.0 → (0.55, 1/3)，
# ē = 0.55 + 2/3。
CONST_REF_HAND = (0.5, 0.2, 0.9)
CONST_SELECTED_HAND = (0.55, 1 / 3, 0.55 + 2 / 3)
# 执行政策：偶数序号候选在 t5 改为全部现金，区间 6 收益为 0，差额 = −ln(1 + 1.4·U6) = −log1p(1.4/99)；奇数为 0。
POLICY_DIFF_EVEN_HAND = -math.log1p(1.4 / 99)
# 市场环境：区间 1—4 起点 2000-03-20…03-23 早于熊市一起点 2000-03-24，年份 2000 的年末 2000-12-29 晚于轴末日 → 不可得；
# 区间 5、6 起点 2000-03-24、03-27 满足 P ≤ d < Tr → 熊市。
ENV_HAND = ("完整年度分类不可得",) * 4 + ("熊市",) * 2
# 分段映射（days = DAYS）：1999—2009：i1 = max(0, 1) = 1、i2 = 6 → a = 0、b = 6、部分覆盖；2010—2016：
# i1 = 7 > i2 = 6 → 无收益区间；
# 熊市一：i1 = bisect_right(days, 03-24) = 5、i2 = 6 → a = 4、b = 6、部分覆盖（段末 2002-10-09 晚于 days[6]）；
# 熊市二：无收益区间。
SEGMENT_MAP_HAND = (("部分覆盖", 0, 6), ("无收益区间", None, None), ("部分覆盖", 4, 6), ("无收益区间", None, None))
# 暴露替换：c = 1（t4 = 2000-03-24 为一级且在熊市）的候选有一个区间 2000-03-24/2000-03-27，U5 = 0.03125：
# registered = log1p(0.6·0.03125) = log1p(0.01875)，substituted = log1p(0.3·0.03125) = log1p(0.009375)。
SUBSTITUTION_HAND = (math.log1p(0.01875), math.log1p(0.009375), math.log1p(0.009375) - math.log1p(0.01875))

OTHER_POSITIONS = (
    (REF, ("二级", "二级", "一级", "正常", "正常", "正常", "正常")),
    (AVG1, ("一级", "正常", "正常", "一级", "一级", "正常", "正常")),
    (AVG2, ("正常", "正常", "二级", "二级", "一级", "正常", "正常")),
)
PRIOR_STATES = ((REF, "二级"),)  # 首个信号日的前一日状态（窗口外，由生成规则设定）；其余对象为“正常”
R2_PASS_LIST = (
    "持续覆盖达标",
    "新提示达标",
    "持续覆盖达标",
    "左截断",
    "漏报",
    "新提示达标",
    "迟到",
    "持续覆盖达标",
    "输入不足",
)
R2_FAIL_LIST = ("左截断", "输入不足", "持续覆盖达标", "新提示达标", "提示中断", "迟到", "漏报")  # 任一 5 连窗都不满足
ENVIRONMENT_NAMES = ("熊市", "上涨年", "下跌年", "平淡年", "完整年度分类不可得")
LEDGER_LIST = ("窗口前已启动", "事件内提示", "低点后提示", "提前提示", "误报", "观察不完整")


# ---------------------------------------------------------------- 加载与运行


def load_script(path: Path, name: str) -> object:
    """按路径加载脚本模块（不经过包导入）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod_a() -> object:
    """N1 模块。"""
    return load_script(SCRIPT_A, "v20_recompute_a_under_test")


@pytest.fixture(scope="module")
def mod_b() -> object:
    """N2 模块。"""
    return load_script(SCRIPT_B, "v20_recompute_b_under_test")


def run_raw(script: Path, result_dir: Path, out: Path) -> tuple[int, str]:
    """以子进程运行脚本（-X utf8 使标准错误为 UTF-8），返回退出码与标准错误。"""
    command = [sys.executable, "-X", "utf8", "-B", str(script), "--result-dir", str(result_dir), "--out", str(out)]
    completed = subprocess.run(command, capture_output=True, timeout=SUBPROCESS_TIMEOUT, check=False)
    return completed.returncode, completed.stderr.decode("utf-8", errors="replace")


def run_script(script: Path, result_dir: Path, out: Path) -> tuple[int, object, str]:
    """以子进程运行脚本，返回退出码、报告 JSON（无报告为 None）与标准错误。"""
    code, stderr = run_raw(script, result_dir, out)
    report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return code, report, stderr


def items_where(report: object, **criteria: str) -> list[dict[str, object]]:
    """报告中满足条件（item/object 精确，status 前缀）的比对项。"""
    found = []
    for item in report["items"]:
        fields_match = all(item.get(key) == value for key, value in criteria.items() if key != "status")
        status_match = "status" not in criteria or item["status"].startswith(criteria["status"])
        if fields_match and status_match:
            found.append(item)
    return found


def problems(report: object) -> list[dict[str, object]]:
    """不一致与计算失败的比对项。"""
    return [item for item in report["items"] if item["status"].startswith(("不一致", "计算失败"))]


# ---------------------------------------------------------------- 生成规则（测试代码）


def weights_of(position: str) -> tuple[float, float]:
    """登记权重。"""
    for name, core, leverage in WEIGHT_TABLE:
        if name == position:
            return core, leverage
    raise KeyError(position)


def candidate_positions(order: int) -> tuple[str, ...]:
    """候选的计划目标 t0…t6。"""
    a, b, c = order // 9, (order // 3) % 3, order % 3
    return (POS[a], "正常", POS[b], "正常", POS[c], "正常", "正常")


def positions_of(obj: str) -> tuple[str, ...]:
    """任一 O_d 对象的计划目标 t0…t6。"""
    if obj in KEYS:
        return candidate_positions(KEYS.index(obj))
    return dict(OTHER_POSITIONS)[obj]


def gen_us(spx: tuple[str, ...], qqq: tuple[str, ...]) -> list[float]:
    """U_i = 0.5·(r_SPX + r_QQQ)（生成规则，按第四节第 4 小节同式书写）。"""
    return [
        0.5 * ((float(spx[i]) / float(spx[i - 1]) - 1.0) + (float(qqq[i]) / float(qqq[i - 1]) - 1.0))
        for i in range(1, len(spx))
    ]


def gen_returns(weights: list[tuple[float, float]], us: list[float]) -> list[float]:
    """R_i = core·U_i + leverage·2·U_i。"""
    return [core * u + leverage * 2.0 * u for (core, leverage), u in zip(weights, us, strict=True)]


def gen_wealth(returns: list[float]) -> list[float]:
    """W_0 = 1，W_i = W_{i−1}·(1 + R_i)。"""
    wealth = [1.0]
    for value in returns:
        wealth.append(wealth[-1] * (1.0 + value))
    return wealth


def gen_mdd(wealth: list[float]) -> float:
    """含起点的最大回撤。"""
    peak, worst = wealth[0], 0.0
    for value in wealth:
        peak = max(peak, value)
        worst = max(worst, 1.0 - value / peak)
    return worst


def gen_r2(order: int, asset: str, passing: bool) -> list[str]:
    """候选 order 在某资产的 5 个事件类别。"""
    table = R2_PASS_LIST if passing else R2_FAIL_LIST
    start = (order + (0 if asset == "SPX" else 1)) % len(table)
    return [table[(start + j) % len(table)] for j in range(5)]


def gen_r2_counts(categories: list[str]) -> tuple[bool, int, int, int, bool | None]:
    """R2 计数（生成规则）。"""
    denominator = len(categories) - categories.count("左截断")
    if denominator == 0:
        return False, 0, 0, 0, None
    achieved = categories.count("持续覆盖达标") + categories.count("新提示达标")
    return True, denominator, achieved, categories.count("新提示达标"), 5 * achieved >= 3 * denominator


def gen_switches(positions: tuple[str, ...]) -> tuple[int, float, float]:
    """切换次数、Σ|Δe|、ΣΔe（生成规则）。"""
    exposure = [weights_of(p)[0] + 2.0 * weights_of(p)[1] for p in positions]
    deltas = [exposure[i] - exposure[i - 1] for i in range(1, len(positions)) if positions[i] != positions[i - 1]]
    return len(deltas), math.fsum(abs(d) for d in deltas), math.fsum(deltas)


def gen_states(obj: str, positions: tuple[str, ...]) -> dict[str, object]:
    """四项状态统计（生成规则；风险状态序列与计划目标相同，前一日状态见 PRIOR_STATES）。
    truncated 元素 [position, days[s], days[e], s = 0, e = n]，只收左或右截断的一级、二级段，先一级后二级、各按段序。"""
    signal_days = (HISTORY[-1], *DAYS[:-1])
    sequence = (dict(PRIOR_STATES).get(obj, "正常"), *positions)
    direct = [signal_days[k] for k in range(N + 1) if positions[k] == "二级" and sequence[k] == "正常"]
    via = [signal_days[k] for k in range(N + 1) if positions[k] == "二级" and sequence[k] == "一级"]
    level1 = level2 = 0
    truncated = []
    start = 0
    for index in range(1, N + 1):
        if index < N and positions[index] == positions[start]:
            continue
        if positions[start] != "正常":
            level1 += positions[start] == "一级"
            level2 += positions[start] == "二级"
            if start == 0 or index == N:
                truncated.append([positions[start], DAYS[start], DAYS[index], start == 0, index == N])
        start = index
    truncated = [t for t in truncated if t[0] == "一级"] + [t for t in truncated if t[0] == "二级"]
    return {
        "object": obj,
        "direct_level2": len(direct),
        "direct_level2_days": direct,
        "via_level1": len(via),
        "via_level1_days": via,
        "level1_intervals": level1,
        "level2_intervals": level2,
        "truncated": truncated,
        "start_note": "构造",
    }


def variant(**changes: object) -> dict[str, object]:
    """构造变体参数。"""
    base = {"evaluable2": True, "r2_pass": True, "missing_qqq": None, "history_gap": False, "usage": None}
    assert set(changes) <= set(base)
    base.update(changes)
    return base


def table(header: str, rows: list[list[object]]) -> dict[str, object]:
    """CSV 表（值在写出时转文本）。"""
    return {"header": header.split(","), "rows": rows}


def text(value: object) -> str:
    """写出规则：None 为空、布尔为 true/false、浮点为 repr。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


# ---------------------------------------------------------------- 生成：模型


def build_model(v: dict[str, object]) -> dict[str, object]:
    """所有对象的路径与汇总（生成规则）。"""
    spx, qqq = list(SPX_TEXT), list(QQQ_TEXT)
    known = v["missing_qqq"]
    if known is not None:
        qqq[len(HISTORY) + known] = ""
    if v["history_gap"]:
        qqq[0] = ""
    window_spx, window_qqq = tuple(spx[len(HISTORY) :]), tuple(qqq[len(HISTORY) :])
    limit = N if known is None else max(known - 1, 0)
    us = gen_us(window_spx[: limit + 1], window_qqq[: limit + 1]) if limit > 0 else []
    evaluable = (AVG1, AVG2) if v["evaluable2"] else (AVG1,)
    daily = (*KEYS, REF, *evaluable)
    structure = "A" if known is None else "B"
    model: dict[str, object] = {
        "spx": spx,
        "qqq": qqq,
        "us": us,
        "known": known,
        "structure": structure,
        "daily": daily,
        "evaluable": evaluable,
        "v": v,
        "paths": {},
    }
    if structure == "A":
        for obj in daily:
            signal = gen_returns([weights_of(p) for p in positions_of(obj)[:N]], us)
            policy_positions = list(positions_of(obj))
            if obj in KEYS and KEYS.index(obj) % 2 == 0:
                policy_positions[5] = "全部现金"
            policy = gen_returns([weights_of(p) for p in policy_positions[:N]], us)
            model["paths"][(obj, "信号模拟")] = signal
            model["paths"][(obj, "执行政策研究模拟")] = policy
            model.setdefault("policy_positions", {})[obj] = tuple(policy_positions)
        model["paths"][(HOLD, "一直持有")] = gen_returns([weights_of("正常")] * N, us)
    add_selection(model)
    add_constants(model)
    return model


def add_selection(model: dict[str, object]) -> None:
    """R1、R2、可行集与选择（生成规则）。"""
    paths = model["paths"]
    hold_mdd = gen_mdd(gen_wealth(paths[(HOLD, "一直持有")])) if model["structure"] == "A" else None
    records = []
    for order, key in enumerate(KEYS):
        r2 = {asset: gen_r2_counts(gen_r2(order, asset, model["v"]["r2_pass"])) for asset in ("SPX", "QQQ")}
        if model["structure"] == "A":
            wealth = gen_wealth(paths[(key, "信号模拟")])
            mdd = gen_mdd(wealth)
            r1 = (True, mdd, hold_mdd, mdd <= 0.5 * hold_mdd)
            log_wealth = math.log(wealth[-1])
        else:
            r1, log_wealth = (False, None, None, None), None
        records.append(
            {
                "order": order,
                "key": key,
                "r1": r1,
                "r2": r2,
                "log_wealth": log_wealth,
                "switches": gen_switches(positions_of(key))[0],
            }
        )
    model["records"] = records
    if model["structure"] == "B":
        model["selection"] = ("缺值无法评价", [], None, [], None)
        return
    feasible = [r for r in records if r["r1"][3] and r["r2"]["SPX"][4] and r["r2"]["QQQ"][4]]
    if not feasible:
        model["selection"] = ("无合格候选", [], None, [], None)
        return
    maximum = max(r["log_wealth"] for r in feasible)
    tied = [r for r in feasible if r["log_wealth"] >= maximum - 1e-10]
    chosen = min(tied, key=lambda r: (r["switches"], r["order"]))
    model["selection"] = ("选定", [r["key"] for r in feasible], maximum, [r["key"] for r in tied], chosen["key"])


def add_constants(model: dict[str, object]) -> None:
    """恒定仓位：主参照（总是）与选定候选（只在出口为选定时）；结构 B 下均不计算。"""
    sources = [REF] + ([model["selection"][4]] if model["selection"][0] == "选定" else [])
    model["constants"] = {}
    if model["structure"] == "B":
        return
    for source in sources:
        weights = [weights_of(p) for p in positions_of(source)[:N]]
        core = math.fsum(w[0] for w in weights) / N
        leverage = math.fsum(w[1] for w in weights) / N
        model["constants"][source] = (core, leverage, core + 2.0 * leverage)
        model["paths"][("恒定仓位：" + source, "恒定仓位")] = gen_returns([(core, leverage)] * N, model["us"])


# ---------------------------------------------------------------- 生成：逐日表


def daily_tables(model: dict[str, object]) -> dict[str, object]:
    """daily_signals、daily_targets、daily_policy、daily_nav、input_snapshot。"""
    signal_days = (HISTORY[-1], *DAYS[:-1])
    signals, targets, policy, nav = [], [], [], []
    for index, obj in enumerate(model["daily"]):
        positions = positions_of(obj)
        for k in range(N + 1):
            signals.append([signal_days[k], obj, positions[k], POS.index(positions[k]), 0, 0, True])
            core, leverage = weights_of(positions[k])
            targets.append([DAYS[k], obj, positions[k], core, leverage, "系统目标", False])
        policy.extend(policy_rows(model, obj, index))
    for (obj, path), returns in model["paths"].items():
        wealth = gen_wealth(returns)
        for k in range(N + 1):
            nav.append([DAYS[k], obj, path, wealth[k], None if k == 0 else returns[k - 1]])
    snapshot = [[day, s, q] for day, s, q in zip((*HISTORY, *DAYS), model["spx"], model["qqq"], strict=True)]
    return {
        "input_snapshot.csv.gz": table("date,spx_close,qqq_close", snapshot),
        "daily_signals.csv.gz": table("date,object,risk,level,c1,c2,all_valid", signals),
        "daily_targets.csv.gz": table("date,object,position,core,leverage,source,cap_active", targets),
        "daily_policy.csv.gz": table("date,object,position,core,leverage,source,cap_active,wealth", policy),
        "daily_nav.csv.gz": table("date,object,path,wealth,return_to_date", nav),
    }


def policy_rows(model: dict[str, object], obj: str, index: int) -> list[list[object]]:
    """执行政策逐日行：结构 A 为完整 n + 1 行；结构 B 为 known + 1 + (index mod 3) 行，wealth 只前 known 行非空。"""
    if model["structure"] == "A":
        positions = model["policy_positions"][obj]
        wealth = gen_wealth(model["paths"][(obj, "执行政策研究模拟")])
        count = N + 1
    else:
        positions = positions_of(obj)
        known = model["known"]
        prefix = gen_returns([weights_of(p) for p in positions[: max(known - 1, 0)]], model["us"])
        wealth = gen_wealth(prefix)[:known]
        count = min(N + 1, known + 1 + index % 3)
    rows = []
    for k in range(count):
        core, leverage = weights_of(positions[k])
        source = "止损现金" if positions[k] == "全部现金" else "系统目标"
        rows.append([DAYS[k], obj, positions[k], core, leverage, source, False, wealth[k] if k < len(wealth) else None])
    return rows


def judgement_tables(model: dict[str, object]) -> dict[str, object]:
    """r2_judgements 与 segment_ledgers。"""
    judgements, ledgers = [], []
    for order, key in enumerate(KEYS):
        for asset in ("SPX", "QQQ"):
            for j, category in enumerate(gen_r2(order, asset, model["v"]["r2_pass"])):
                judgements.append([key, asset, f"1999-0{j + 1}-15", category, None, None, None, False])
            for j in range(3):
                ledgers.append(
                    [
                        key,
                        asset,
                        f"1999-0{j + 1}-01",
                        f"1999-0{j + 1}-10",
                        j == 0,
                        LEDGER_LIST[(order + j) % len(LEDGER_LIST)],
                    ]
                )
    return {
        "r2_judgements.csv.gz": table(
            "object,asset,peak,category,first_new_day,executable_day,executable_offset,peak_new_uncertain", judgements
        ),
        "segment_ledgers.csv.gz": table("object,asset,start,end,pre_window,category", ledgers),
    }


# ---------------------------------------------------------------- 生成：汇总文件


def path_summary(model: dict[str, object], obj: str, path: str) -> tuple[float | None, float | None]:
    """某路径的 ln W_末 与 MDD；不可计算为 (None, None)。"""
    returns = model["paths"].get((obj, path))
    if returns is None:
        return None, None
    wealth = gen_wealth(returns)
    return math.log(wealth[-1]), gen_mdd(wealth)


def candidates_summary(model: dict[str, object]) -> dict[str, object]:
    """candidates_summary.csv。"""
    rows = []
    for record in model["records"]:
        key, order = record["key"], record["order"]
        signal = path_summary(model, key, "信号模拟")
        policy = path_summary(model, key, "执行政策研究模拟")
        count, magnitude, _ = gen_switches(positions_of(key))
        states = gen_states(key, positions_of(key))
        r1 = record["r1"]
        r2 = [value for asset in ("SPX", "QQQ") for value in record["r2"][asset]]
        diff = None if policy[0] is None or signal[0] is None else policy[0] - signal[0]
        k, theta, h = (3, 5, 10)[order // 9], ("0.015", "0.02", "0.025")[(order // 3) % 3], (1, 3, 5)[order % 3]
        rows.append(
            [
                order,
                key,
                k,
                theta,
                h,
                signal[0],
                signal[1],
                policy[0],
                policy[1],
                count,
                magnitude,
                r1[0],
                r1[1],
                r1[2],
                r1[3],
                *r2,
                diff,
                states["direct_level2"],
                states["via_level1"],
                states["level1_intervals"],
                states["level2_intervals"],
            ]
        )
    return table(
        "order,object,k,theta_p,h,signal_log_wealth,signal_max_drawdown,policy_log_wealth,policy_max_drawdown,"
        "switches,exposure_magnitude,r1_computable,r1_signal_drawdown,r1_hold_drawdown,r1_satisfied,"
        "spx_r2_computable,spx_r2_denominator,spx_r2_achieved,spx_r2_new_only,spx_r2_meets,"
        "qqq_r2_computable,qqq_r2_denominator,qqq_r2_achieved,qqq_r2_new_only,qqq_r2_meets,"
        "policy_minus_signal,direct_level2,via_level1_into_level2,level1_intervals,level2_intervals",
        rows,
    )


def reference_summary(model: dict[str, object]) -> dict[str, object]:
    """reference_summary.csv：恰 4 行。"""
    rows = []
    for obj in (REF, HOLD, AVG1, AVG2):
        if obj == HOLD:
            signal = path_summary(model, HOLD, "一直持有")
            rows.append([obj, signal[0], signal[1], None, None, 0, 0.0])
        elif obj in model["daily"]:
            signal = path_summary(model, obj, "信号模拟")
            policy = path_summary(model, obj, "执行政策研究模拟")
            count, magnitude, _ = gen_switches(positions_of(obj))
            rows.append([obj, signal[0], signal[1], policy[0], policy[1], count, magnitude])
        else:
            rows.append([obj, None, None, None, None, 0, 0.0])
    return table(
        "object,signal_log_wealth,signal_max_drawdown,policy_log_wealth,policy_max_drawdown,switches,"
        "exposure_magnitude",
        rows,
    )


def segments_table(model: dict[str, object]) -> dict[str, object]:
    """segments.csv：映射取人工表 SEGMENT_MAP_HAND；比值由生成规则计算。"""
    rows = []
    hold = model["paths"].get((HOLD, "一直持有"))
    for key in KEYS:
        signal = model["paths"].get((key, "信号模拟"))
        for (name, start, end), (coverage, a, b) in zip(SEGMENT_BOUNDS, SEGMENT_MAP_HAND, strict=True):
            if a is None:
                rows.append([key, name, start, end, coverage, None, None, 0, None, False])
                continue
            if signal is None or hold is None:
                rows.append([key, name, start, end, "不可计算", DAYS[a], DAYS[b], b - a, None, False])
                continue
            hold_mdd = gen_mdd(gen_wealth(hold)[a : b + 1])
            ratio = None if hold_mdd == 0 else gen_mdd(gen_wealth(signal)[a : b + 1]) / hold_mdd
            rows.append([key, name, start, end, coverage, DAYS[a], DAYS[b], b - a, ratio, hold_mdd == 0])
    return table(
        "object,segment,start,end,coverage,first_nav_day,last_nav_day,returns_count,drawdown_ratio,undefined", rows
    )


def environment_files(model: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """environments.csv 与 descriptive.environment_summary（分类取人工表 ENV_HAND；summary 键恰为五个环境名，
    log_return_sums 键恰为 27 候选 + 主参照 + 一直持有；结构 A 为各分类区间 log1p(R) 之和，空分类为 0.0；
    结构 B 全为 null）。"""
    rows = [[i + 1, DAYS[i], ENV_HAND[i], None] for i in range(N)]
    summary: dict[str, object] = {}
    members = [(key, "信号模拟") for key in KEYS] + [(REF, "信号模拟"), (HOLD, "一直持有")]
    for category in ENVIRONMENT_NAMES:
        indexes = [i for i in range(N) if ENV_HAND[i] == category]
        sums: dict[str, object] = {}
        for obj, path in members:
            if model["structure"] == "A":
                sums[obj] = math.fsum(math.log1p(model["paths"][(obj, path)][i]) for i in indexes)
            else:
                sums[obj] = None
        summary[category] = {"intervals": len(indexes), "log_return_sums": sums}
    return table("interval,start,category,year_return", rows), summary


def substitution_table(model: dict[str, object]) -> dict[str, object]:
    """exposure_substitution.csv：c = 1 的候选有区间 2000-03-24/2000-03-27（生成规则）。"""
    rows = []
    for order, key in enumerate(KEYS):
        if model["structure"] == "B":
            rows.append([key, False, "净值不可计算", 0, None, None, None, None])
        elif order % 3 == 1:
            u = model["us"][4]
            registered = math.fsum([math.log1p(0.6 * u + 0.0 * 2.0 * u)])
            substituted = math.fsum([math.log1p(0.3 * u + 0.0 * 2.0 * u)])
            rows.append([key, True, None, 1, registered, substituted, substituted - registered, f"{DAYS[4]}/{DAYS[5]}"])
        else:
            rows.append([key, True, None, 0, 0.0, 0.0, 0.0, None])
    return table("object,computed,note,intervals,registered_log,substituted_log,difference,interval_days", rows)


def reconciliation_doc(model: dict[str, object]) -> dict[str, object]:
    """reconciliation.json：单一路径与相对主参照（生成规则）。恒定仓位行的 object 为原对象名（主参照或候选键），
    不带 daily_nav 中的“恒定仓位：”前缀（第四次交付指令第二节，已绑定的输出接口契约）。"""
    rows = []
    for (nav_obj, path), returns in model["paths"].items():
        obj = nav_obj.removeprefix("恒定仓位：") if path == "恒定仓位" else nav_obj
        difference = abs(math.fsum(math.log1p(r) for r in returns) - math.log(gen_wealth(returns)[-1]))
        rows.append(
            {
                "kind": "单一路径",
                "object": obj,
                "path": path,
                "difference": difference,
                "tolerance": 1e-10,
                "passed": difference <= 1e-10,
            }
        )
    if model["structure"] == "A":
        reference = model["paths"][(REF, "信号模拟")]
        for key in KEYS:
            mine = model["paths"][(key, "信号模拟")]
            total = math.fsum(math.log1p(a) - math.log1p(b) for a, b in zip(mine, reference, strict=True))
            difference = abs(total - (math.log(gen_wealth(mine)[-1]) - math.log(gen_wealth(reference)[-1])))
            rows.append(
                {
                    "kind": "相对主参照",
                    "object": key,
                    "path": "信号模拟",
                    "difference": difference,
                    "tolerance": 1e-10,
                    "passed": difference <= 1e-10,
                }
            )
    return {"tolerance_scope": "构造", "rows": rows}


def selection_doc(model: dict[str, object]) -> dict[str, object]:
    """selection.json。"""
    outcome, feasible, maximum, tied, selected = model["selection"]
    records = [
        {
            "order": r["order"],
            "object": r["key"],
            "failed": False,
            "r1": r["r1"][3],
            "r2": {"SPX": r["r2"]["SPX"][4], "QQQ": r["r2"]["QQQ"][4]},
            "log_wealth": r["log_wealth"],
            "switches": r["switches"],
        }
        for r in model["records"]
    ]
    return {
        "outcome": outcome,
        "tolerance": 1e-10,
        "selected": selected,
        "feasible": feasible,
        "maximum": maximum,
        "tied": tied,
        "records": records,
    }


def nav_block(model: dict[str, object], obj: str, path: str) -> dict[str, object]:
    """descriptive 中的净值摘要。"""
    log_wealth, mdd = path_summary(model, obj, path)
    final = None if log_wealth is None else math.exp(log_wealth)
    return {"computable": log_wealth is not None, "log_wealth": log_wealth, "max_drawdown": mdd, "final_wealth": final}


def average_blocks(model: dict[str, object]) -> list[dict[str, object]]:
    """descriptive.averages：不可评价者 switches、signal、policy 为 JSON null（答复单第 8 项）。"""
    averages = []
    for name in (AVG1, AVG2):
        evaluable = name in model["evaluable"]
        entry: dict[str, object] = {
            "name": name,
            "domain": [],
            "convergence_day": HISTORY[0],
            "evaluable": evaluable,
            "note": None if evaluable else "不可评价：未在 j₀ − 1 前收敛",
            "signal": None,
            "policy": None,
            "switches": None,
        }
        if evaluable:
            count, magnitude, change = gen_switches(positions_of(name))
            entry["signal"] = nav_block(model, name, "信号模拟")
            entry["policy"] = nav_block(model, name, "执行政策研究模拟")
            entry["switches"] = {"count": count, "magnitude_total": magnitude, "change_total": change}
        averages.append(entry)
    return averages


def descriptive_doc(model: dict[str, object], environment_summary: dict[str, object]) -> dict[str, object]:
    """descriptive.json（constants 未计算者 core、leverage、exposure、nav 为 null；
    state_statistics 只含 27 候选 + 主参照；
    policy_minus_signal 键为 27 候选 + 主参照 + 两条均线对照）。"""
    constants = []
    selected = model["selection"][4]
    for source in (REF, selected if selected is not None else "选定候选"):
        weights = model["constants"].get(source)
        computed = weights is not None
        constants.append(
            {
                "object": source,
                "computed": computed,
                "note": None,
                "core": weights[0] if computed else None,
                "leverage": weights[1] if computed else None,
                "exposure": weights[2] if computed else None,
                "nav": nav_block(model, "恒定仓位：" + source, "恒定仓位") if computed else None,
            }
        )
    differences = {}
    for obj in (*KEYS, REF, AVG1, AVG2):
        signal = path_summary(model, obj, "信号模拟")[0]
        policy = path_summary(model, obj, "执行政策研究模拟")[0]
        differences[obj] = {"difference": None if signal is None else policy - signal, "note": "含整套止损路径"}
    return {
        "averages": average_blocks(model),
        "constants": constants,
        "environment_summary": environment_summary,
        "state_statistics": [gen_states(obj, positions_of(obj)) for obj in (*KEYS, REF)],
        "policy_events": {},
        "policy_minus_signal": differences,
        "note": "构造样本",
    }


def make_project(v: dict[str, object]) -> dict[str, object]:
    """完整构造结果目录的全部文件内容（CSV 为表、JSON 为对象）。"""
    model = build_model(v)
    environments, summary = environment_files(model)
    files = {**daily_tables(model), **judgement_tables(model)}
    files.update(
        {
            "candidates_summary.csv": candidates_summary(model),
            "reference_summary.csv": reference_summary(model),
            "segments.csv": segments_table(model),
            "environments.csv": environments,
            "exposure_substitution.csv": substitution_table(model),
            "selection.json": selection_doc(model),
            "reconciliation.json": reconciliation_doc(model),
            "descriptive.json": descriptive_doc(model, summary),
            "window.json": {
                "purpose": "构造样本",
                "last_day": DAYS[-1],
                "histories": {},
                "candidates_requested": 27,
                "stop": None,
                "window": {
                    "t0": 0,
                    "t0_day": HISTORY[0],
                    "j0": J0,
                    "j0_min": J0,
                    "kappa_all": 0,
                    "kappa_all_day": HISTORY[0],
                    "reference_index": 0,
                    "reference_day": HISTORY[0],
                    "e_index": J0 + N,
                    "n": N,
                    "first_day": DAYS[0],
                    "last_day": DAYS[-1],
                    "start_basis": "构造",
                    "history_first": HISTORY[0],
                    "history_last": DAYS[-1],
                    "convergences": [],
                },
            },
            "run_record.json": {
                "usage_restriction": v["usage"],
                "cutoff": "2016-12-30",
                "data_files": {
                    "SPX": {"bytes": 0, "raw_sha256": "构造样本无原始文件"},
                    "QQQ": {"bytes": 0, "raw_sha256": "构造样本无原始文件"},
                },
                "registered_parameters": {},
            },
        }
    )
    return files


def csv_bytes(header: list[str], rows: list[list[object]]) -> bytes:
    """RFC 4180 CSV（LF 行尾）。"""
    lines: list[str] = []

    class Sink:
        def write(self, value: str) -> None:
            lines.append(value)

    writer = csv.writer(Sink(), lineterminator="\n")
    writer.writerow(header)
    writer.writerows([[text(value) for value in row] for row in rows])
    return "".join(lines).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """SHA-256 十六进制，只用于写构造夹具 MANIFEST.sha256 行（N4 直接导入 hashlib，负责人裁决 P3，2026-10-06）。"""
    return hashlib.sha256(data).hexdigest()


def write_project(root: Path, files: dict[str, object]) -> Path:
    """在 tmp_path 下写出构造结果目录，最后写 MANIFEST.sha256（`<sha256> <字节数> <文件名>`，按文件名排序，
    不含自身）。"""
    root.mkdir(parents=True)
    written: dict[str, bytes] = {}
    for name, content in files.items():
        if name.endswith(".json"):
            data = (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        else:
            data = csv_bytes(content["header"], content["rows"])
            if name.endswith(".gz"):
                data = gzip.compress(data, mtime=0)
        (root / name).write_bytes(data)
        written[name] = data
    lines = [f"{sha256_hex(data)} {len(data)} {name}\n" for name, data in sorted(written.items())]
    (root / "MANIFEST.sha256").write_bytes("".join(lines).encode("utf-8"))
    return root


def edit_manifest(root: Path, transform: object) -> None:
    """改写构造目录的 MANIFEST.sha256（故障注入用）。"""
    path = root / "MANIFEST.sha256"
    path.write_bytes(transform(path.read_text(encoding="utf-8")).encode("utf-8"))


def column(files: dict[str, object], name: str, column_name: str) -> int:
    """表的列下标。"""
    return files[name]["header"].index(column_name)


def rows_of(files: dict[str, object], name: str, **criteria: object) -> list[list[object]]:
    """满足条件的行（可原地修改）。"""
    found = []
    for row in files[name]["rows"]:
        if all(row[column(files, name, key)] == value for key, value in criteria.items()):
            found.append(row)
    return found


def run_both(
    tmp_path: Path, files: dict[str, object], tag: str
) -> tuple[tuple[int, object, str], tuple[int, object, str]]:
    """写出目录并运行两脚本。"""
    result = write_project(tmp_path / f"dir_{tag}", files)
    return (
        run_script(SCRIPT_A, result, tmp_path / f"a_{tag}.json"),
        run_script(SCRIPT_B, result, tmp_path / f"b_{tag}.json"),
    )


def run_one(script: Path, tmp_path: Path, files: dict[str, object], tag: str) -> tuple[int, object, str]:
    """写出目录并运行一个脚本。"""
    result = write_project(tmp_path / f"dir_{tag}", files)
    return run_script(script, result, tmp_path / f"report_{tag}.json")


# ================================================================ (a) 纯函数：甲层


def test_a_wealth_chain_and_log_wealth(mod_a: object) -> None:
    """r = 0.01、−0.02、0.005 → W = 1、1.01、0.9898、0.994749；ln W_末 = ln 0.994749。"""
    wealth = mod_a.wealth_chain([0.01, -0.02, 0.005], "构造")
    for mine, hand in zip(wealth, (1.0, 1.01, 0.9898, 0.994749), strict=True):
        assert abs(mine - hand) <= 1e-15
    assert abs(math.log(wealth[-1]) - math.log(0.994749)) <= 1e-15
    assert abs(mod_a.log_return_sum([0.01, -0.02, 0.005], "构造") - math.log(0.994749)) <= 1e-15


def test_a_r1_from_drawdowns(mod_a: object) -> None:
    """信号 1、0.95、1.02、0.98：MDD = max(0.05, 1 − 0.98/1.02 = 0.0392…) = 0.05；一直持有 1、0.8、0.9、1.0：
    0.2；0.05 ≤ 0.1。"""
    signal = mod_a.max_drawdown([1.0, 0.95, 1.02, 0.98], "构造")
    hold = mod_a.max_drawdown([1.0, 0.8, 0.9, 1.0], "构造")
    assert abs(signal - 0.05) <= 1e-15 and abs(hold - 0.2) <= 1e-15
    result = mod_a.r1_judgement(signal, hold)
    assert (result.computable, result.satisfied) == (True, True)
    assert result.signal_drawdown == signal and result.hold_drawdown == hold


def test_a_r1_both_zero_satisfied(mod_a: object) -> None:
    """信号与一直持有均单调不降：回撤 0 ≤ 0.5 × 0 → 可计算且满足（不是“无定义”）。"""
    signal = mod_a.max_drawdown([1.0, 1.0, 1.01, 1.02], "构造")
    hold = mod_a.max_drawdown([1.0, 1.02, 1.03, 1.03], "构造")
    result = mod_a.r1_judgement(signal, hold)
    assert (signal, hold, result.computable, result.satisfied) == (0.0, 0.0, True, True)


def test_a_r1_hold_zero_signal_positive_not_satisfied(mod_a: object) -> None:
    """一直持有单调不降、信号 1、0.99、1.0：0.01 ≤ 0 不成立 → 可计算且不满足。"""
    result = mod_a.r1_judgement(
        mod_a.max_drawdown([1.0, 0.99, 1.0], "构造"), mod_a.max_drawdown([1.0, 1.01, 1.02], "构造")
    )
    assert result.computable is True and result.satisfied is False
    assert abs(result.signal_drawdown - 0.01) <= 1e-15


def test_a_r1_incomputable_when_nav_unavailable(mod_a: object) -> None:
    """无信号净值 → R1 不可计算；选择出口为“缺值无法评价”。"""
    result = mod_a.r1_judgement(None, 0.2)
    assert (result.computable, result.signal_drawdown, result.hold_drawdown, result.satisfied) == (
        False,
        None,
        None,
        None,
    )
    record = mod_a.CandidateRecord(0, KEYS[0], False, result.satisfied, True, True, 0.1, 0)
    assert mod_a.select_candidate([record]).outcome == "缺值无法评价"


def test_a_segment_mapping_full_partial_empty(mod_a: object) -> None:
    """d0…d6（n = 6）：(d2, d5] 完整覆盖 a = 2、b = 5、计数 3；(d−1, d3] 部分覆盖 a = 0、b = 3；(d5, d5] 无收益区间。"""
    day_type = mod_a.date  # 日期类型取自被测模块，不另行导入
    days = [day_type.fromisoformat(d) for d in DAYS]
    full = mod_a.segment_mapping(days, days[2], days[5])
    assert (full.coverage, full.a, full.b, full.returns_count, full.first_nav_day, full.last_nav_day) == (
        "完整覆盖",
        2,
        5,
        3,
        days[2],
        days[5],
    )
    partial = mod_a.segment_mapping(days, day_type.fromisoformat(HISTORY[-1]), days[3])
    assert (partial.coverage, partial.a, partial.b, partial.returns_count) == ("部分覆盖", 0, 3, 3)
    empty = mod_a.segment_mapping(days, days[5], days[5])
    assert (empty.coverage, empty.a, empty.returns_count, empty.first_nav_day) == ("无收益区间", None, 0, None)


def test_a_segment_ratio_undefined(mod_a: object) -> None:
    """段内一直持有单调不降 → MDD 为 0 → 比值空、undefined 为真；另一段比值 = 0.05 ÷ 0.2 = 0.25。"""
    signal = [1.0, 0.95, 1.0, 1.02, 1.03]
    hold = [1.0, 0.8, 1.0, 1.01, 1.02]
    assert mod_a.segment_ratio(signal, hold, 2, 4, "构造") == (None, True)
    ratio, undefined = mod_a.segment_ratio(signal, hold, 0, 2, "构造")
    assert undefined is False and abs(ratio - 0.25) <= 1e-15


def test_a_r2_ratio_meets(mod_a: object) -> None:
    """左截断 1、持续覆盖达标 2、新提示达标 1、漏报 1：分母 4、达标 3、新 1，15 ≥ 12 → 满足。"""
    result = mod_a.r2_counts(["左截断", "持续覆盖达标", "持续覆盖达标", "新提示达标", "漏报"])
    assert (result.computable, result.denominator, result.achieved, result.new_only, result.meets) == (
        True,
        4,
        3,
        1,
        True,
    )


def test_a_r2_exact_threshold(mod_a: object) -> None:
    """分母 5、达标 3：15 ≥ 15 → 满足（恰 60%）。"""
    result = mod_a.r2_counts(["持续覆盖达标", "新提示达标", "持续覆盖达标", "迟到", "漏报"])
    assert (result.denominator, result.achieved, result.meets) == (5, 3, True)


def test_a_r2_insufficient_counts_in_denominator(mod_a: object) -> None:
    """左截断 1、输入不足 2、持续覆盖达标 2：分母 4（输入不足计入）、达标 2，10 < 12 → 不满足。"""
    result = mod_a.r2_counts(["左截断", "输入不足", "输入不足", "持续覆盖达标", "持续覆盖达标"])
    assert (result.computable, result.denominator, result.achieved, result.meets) == (True, 4, 2, False)


def test_a_r2_incomputable(mod_a: object) -> None:
    """全部左截断：分母 0 → 不可计算，其余 0/0/0/空。"""
    result = mod_a.r2_counts(["左截断"] * 5)
    assert (result.computable, result.denominator, result.achieved, result.new_only, result.meets) == (
        False,
        0,
        0,
        0,
        None,
    )


def test_a_switch_count_and_magnitude(mod_a: object) -> None:
    """正常、正常、一级、一级、二级、正常：e = 1.4、1.4、0.6、0.6、0.3、1.4；切换 3；
    幅度 0.8 + 0.3 + 1.1 = 2.2；方向和 0。"""
    positions = ["正常", "正常", "一级", "一级", "二级", "正常"]
    exposures = [mod_a.exposure(*weights_of(p)) for p in positions]
    result = mod_a.switch_summary(positions, exposures)
    assert result.count == 3
    assert abs(result.magnitude - 2.2) <= 1e-12 and abs(result.change) <= 1e-12


def state_case(mod_a: object, s0: str) -> tuple[list[object], list[str], list[str]]:
    """第六节矩阵的对齐表：信号日 s0…s6（s0 为 d0 的前一交易日）与计划目标 d0…d6。"""
    signal_days = [mod_a.date.fromisoformat(d) for d in (HISTORY[-1], *DAYS[:-1])]
    risks = [s0, "一级", "二级", "二级", "正常", "二级", "二级"]
    positions = ["一级", "一级", "二级", "正常", "一级", "一级", "正常"]
    return signal_days, risks, positions


def test_a_state_statistics(mod_a: object) -> None:
    """s2 二级/s1 一级 → 经一级 1；s5 二级/s4 正常 → 直接 1；执行区间 targets[0..5]：一级段 [0,2)（左截断）、
    二级段 [2,3)、
    一级段 [4,6)（终点 6 = n，右截断）→ 一级 2、二级 1、截断 2 条。"""
    signal_days, risks, positions = state_case(mod_a, "正常")
    days = [mod_a.date.fromisoformat(d) for d in DAYS]
    result = mod_a.state_statistics(signal_days, risks, positions, days, [], [])
    assert (result.direct, result.via, result.level1_intervals, result.level2_intervals) == (1, 1, 2, 1)
    assert result.direct_days == (signal_days[5],) and result.via_days == (signal_days[2],)
    # truncated 元素 (position, days[s], days[e], 左截断, 右截断)：一级 [0,2) → (一级, d0, d2, 真, 假)；
    # 一级 [4,6) → (一级, d4, d6, 假, 真)；二级 [2,3) 未截断不收（答复单第 5 项）
    day = mod_a.date.fromisoformat
    assert result.truncated == (
        ("一级", day(DAYS[0]), day(DAYS[2]), True, False),
        ("一级", day(DAYS[4]), day(DAYS[6]), False, True),
    )
    assert result.first_day_dependency is None


def test_a_state_statistics_first_day_dependency(mod_a: object) -> None:
    """s0 为二级、项目 direct_level2_days 含 s0：按项目所取的前一日状态计入直接 1 次，并标注“依赖窗口外状态，
    未独立核对”。"""
    signal_days, risks, positions = state_case(mod_a, "二级")
    days = [mod_a.date.fromisoformat(d) for d in DAYS]
    result = mod_a.state_statistics(signal_days, risks, positions, days, [signal_days[0]], [])
    assert (result.direct, result.via) == (2, 1)
    assert result.first_day_dependency is not None and "依赖窗口外状态" in result.first_day_dependency


def test_a_selection_tie_group_and_switches(mod_a: object) -> None:
    """ln W = 0.10、0.10 − 5e−11、0.09，切换 2、1、0：M = 0.10，并列组前两者，选第二个；变体差 2e−10 → 不并列，
    选第一个。"""

    def records(second: float) -> list[object]:
        values = ((0.10, 2), (second, 1), (0.09, 0))
        return [mod_a.CandidateRecord(i, KEYS[i], False, True, True, True, lw, sw) for i, (lw, sw) in enumerate(values)]

    tied = mod_a.select_candidate(records(0.10 - 5e-11))
    assert (tied.outcome, tied.maximum, tied.tied, tied.selected) == ("选定", 0.10, KEYS[:2], KEYS[1])
    apart = mod_a.select_candidate(records(0.10 - 2e-10))
    assert (apart.tied, apart.selected) == (KEYS[:1], KEYS[0])


def test_a_exits_priority(mod_a: object, tmp_path: Path) -> None:
    """无可行 → 无合格候选；某 ln W 空 → 缺值无法评价；某 failed 为真 → 计算失败（优先级最高）；
    项目写成其他出口 → 不一致。"""

    def record(i: int, r1: object, lw: object, failed: bool) -> object:
        return mod_a.CandidateRecord(i, KEYS[i], failed, r1, True, True, lw, 0)

    assert mod_a.select_candidate([record(0, False, 0.1, False), record(1, False, 0.2, False)]).outcome == "无合格候选"
    assert (
        mod_a.select_candidate([record(0, True, None, False), record(1, False, 0.2, False)]).outcome == "缺值无法评价"
    )
    assert mod_a.select_candidate([record(0, True, None, False), record(1, False, 0.2, True)]).outcome == "计算失败"
    files = make_project(variant())
    files["selection.json"]["outcome"] = "无合格候选"
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "exit")
    assert code == 1
    assert items_where(report, item="outcome", status="不一致")


def test_a_policy_minus_signal(mod_a: object) -> None:
    """signal 末 W = 1.2、policy 末 W = 1.1：ln 1.1 − ln 1.2 = −0.0870…；任一不可得为空。"""
    value = mod_a.policy_minus_signal(1.1, 1.2, "构造")
    assert abs(value - (math.log(1.1) - math.log(1.2))) <= 1e-15 and -0.0871 < value < -0.0870
    assert mod_a.policy_minus_signal(None, 1.2, "构造") is None


# ================================================================ (a) 纯函数：乙层


def test_b_equal_weight_return_and_leverage(mod_b: object) -> None:
    """SPX 100→101→99.99，QQQ 50→50.5→49.5：U1 = 0.01，R1 = 0.6·0.01 + 0.4·2·0.01 = 0.014；
    U2 = 0.5·(99.99/101 − 1) + 0.5·(49.5/50.5 − 1) = 0.5·(−0.01) + 0.5·(−0.0198019…) = −0.0149009…。"""
    Dec = mod_b.Decimal  # Decimal 类型取自被测模块，不另行导入
    u1 = mod_b.equal_weight_return((Dec("100.00"), Dec("101.00")), (Dec("50.00"), Dec("50.50")), "区间 1")
    u2 = mod_b.equal_weight_return((Dec("101.00"), Dec("99.99")), (Dec("50.50"), Dec("49.50")), "区间 2")
    assert abs(u1 - 0.01) <= 1e-12
    assert abs(mod_b.portfolio_return(0.6, 0.4, u1, "区间 1") - 0.014) <= 1e-12
    assert abs(u2 - 0.5 * (-0.01 - 1 / 50.5)) <= 1e-12 and abs(u2 + 0.0149009900990099) <= 1e-12


def test_b_leverage_factor_negative(mod_b: object) -> None:
    """两资产 100→40：U = −0.60，1 + 2U = −0.20 → 计算失败并定位区间。"""
    with pytest.raises(mod_b.ComputeFailure) as caught:
        mod_b.portfolio_return(0.6, 0.4, -0.6, "区间 3")
    assert "区间 3" in caught.value.location


def test_b_leverage_factor_zero_boundary(mod_b: object) -> None:
    """两资产 100→50：U = −0.50，1 + 2U = 0（不大于 0）→ 计算失败。"""
    with pytest.raises(mod_b.ComputeFailure):
        mod_b.portfolio_return(0.6, 0.4, -0.5, "区间 1")


def test_b_leverage_factor_positive_passes(mod_b: object, tmp_path: Path) -> None:
    """两资产均跌 40%：U = −0.40，因子 0.20 > 0，不失败，R = 0.6·(−0.4) + 0.4·2·(−0.4) = −0.56；
    CLI 中因子为负 → 退出码 3。"""
    assert abs(mod_b.portfolio_return(0.6, 0.4, -0.4, "区间 1") + 0.56) <= 1e-12
    files = make_project(variant())
    row = rows_of(files, "input_snapshot.csv.gz", date=DAYS[1])[0]
    row[1], row[2] = "40.00", "20.00"  # 由 100.00、50.00 跌至 40%：U1 = −0.60
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "lev")
    assert code == 3 and report["error"]["kind"] == "计算失败" and "区间 1" in report["error"]["location"]


def test_b_hold_constant_weights(mod_b: object) -> None:
    """一直持有恒为 (0.6, 0.4)：U = 0.01 → R = 0.014；原对象 core 0.6、0.6、0.3，leverage 0.4、0、0（n = 3）→
    w̄_c = 0.5、w̄_l = 0.4/3、ē = 0.5 + 0.8/3 = 0.7666…。"""
    assert mod_b.HOLD_WEIGHTS == (0.6, 0.4)
    core, leverage, exposure = mod_b.constant_weights([0.6, 0.6, 0.3], [0.4, 0.0, 0.0])
    assert abs(core - 0.5) <= 1e-12 and abs(leverage - 0.4 / 3) <= 1e-12 and abs(exposure - (0.5 + 0.8 / 3)) <= 1e-12


def test_b_constant_exposure_average(mod_b: object) -> None:
    """按 (0.5, 0.4/3) 逐区间：U = 0.01、−0.02 → R = 0.5·U + (0.4/3)·2·U = 0.7666…·U；W = 1、1.007666…、0.98221…。"""
    returns = mod_b.target_returns([(0.5, 0.4 / 3)] * 2, [0.01, -0.02], "恒定仓位")
    e = 0.5 + 0.8 / 3
    assert abs(returns[0] - e * 0.01) <= 1e-12 and abs(returns[1] + e * 0.02) <= 1e-12
    wealth = mod_b.wealth_chain(returns, "恒定仓位")
    assert abs(wealth[2] - (1 + e * 0.01) * (1 - e * 0.02)) <= 1e-12


def test_b_environment_classification(mod_b: object) -> None:
    """轴 1998-12-31 至 2001-01-02，年末 1998-12-31、1999-12-31、2000-12-29 收盘 100、112、95：
    r_1999 = 0.12 → 上涨年；r_2000 = 95/112 − 1 = −0.1517… → 2000 年熊市前区间为下跌年；2000-03-24 起熊市优先；
    起点 1998-12-31 的区间：1997 年末不在轴上 → 完整年度分类不可得。
    （矩阵写“2001 年区间不可得”：按登记 d 为区间起点，该轴上没有起点在 2001 年的区间；且 2001 年处于熊市一内，
    即使有也按熊市优先。差异已在交付报告列出。）"""
    snapshot, days = environment_case(mod_b)
    classes = mod_b.classify_environments(snapshot, days, mod_b.to_date("2016-12-30", "截止日"))
    categories = [c for c, _ in classes]
    assert categories == ["完整年度分类不可得", "上涨年", "上涨年", "下跌年", "熊市", "熊市", "熊市"]
    assert str(classes[1][1]) == "0.12"
    assert str(classes[3][1]) == "-0.1517857142857142857142857143"  # Decimal 28 位：95.00/112.00 − 1


def environment_case(mod_b: object) -> tuple[object, tuple[object, ...]]:
    """市场环境构造：轴与 SPX 收盘（Decimal）。"""
    Dec = mod_b.Decimal
    axis = (
        "1998-12-31",
        "1999-06-01",
        "1999-12-31",
        "2000-03-01",
        "2000-03-24",
        "2000-06-01",
        "2000-12-29",
        "2001-01-02",
    )
    closes = ("100.00", "105.00", "112.00", "110.00", "100.00", "98.00", "95.00", "96.00")
    days = tuple(mod_b.to_date(d, "构造") for d in axis)
    snapshot = mod_b.Snapshot(days, tuple(Dec(c) for c in closes), tuple(Dec(c) for c in closes))
    return snapshot, days


def test_b_environment_sums(mod_b: object) -> None:
    """各环境对数收益之和 = 该环境各区间 fsum(log1p(R_i))：用一直持有 (0.6, 0.4) 与上例价格人工列式。"""
    snapshot, days = environment_case(mod_b)
    classes = mod_b.classify_environments(snapshot, days, mod_b.to_date("2016-12-30", "截止日"))
    closes = (100.0, 105.0, 112.0, 110.0, 100.0, 98.0, 95.0, 96.0)
    hand_r = [1.4 * (closes[i] / closes[i - 1] - 1) for i in range(1, 8)]  # U = r（两资产同价），R = 1.4·U
    us = mod_b.interval_returns(snapshot, 0, 7, None)
    returns = mod_b.target_returns([mod_b.HOLD_WEIGHTS] * 7, list(us), "一直持有")
    for category, indexes in (("上涨年", (1, 2)), ("熊市", (4, 5, 6)), ("下跌年", (3,))):
        assert [i for i, (c, _) in enumerate(classes) if c == category] == list(indexes)
        mine = mod_b.log_sum([returns[i] for i in indexes], category)
        assert abs(mine - math.fsum(math.log1p(hand_r[i]) for i in indexes)) <= 1e-12


def test_b_year_end_table_and_1990_unavailable(mod_b: object) -> None:
    """常量表 1989—2016 共 28 项；轴自 1990-01-02 起时 1989 年末不在轴上 → 1990 年不可得；
    轴范围内年末缺失即输入错误。"""
    Dec = mod_b.Decimal
    assert [y for y, _ in mod_b.YEAR_END_NYSE] == list(range(1989, 2017))
    axis = tuple(mod_b.to_date(d, "构造") for d in ("1990-01-02", "1990-06-01", "1990-12-31", "1991-01-02"))
    closes = {axis[2]: Dec("330.22")}
    assert mod_b.year_return(1990, closes, mod_b.to_date("2016-12-30", "截止日")) is None
    mod_b.validate_year_ends(axis)
    with pytest.raises(mod_b.InputError):
        mod_b.validate_year_ends((*axis[:2], mod_b.to_date("1990-12-28", "构造"), axis[3]))


def test_b_substitution_arithmetic(mod_b: object) -> None:
    """U = 0.01、−0.02：一级 (0.6, 0) R = 0.006、−0.012；二级 (0.3, 0) R = 0.003、−0.006；差 = 后 − 前。"""
    registered, substituted, difference = mod_b.substitution_arithmetic([0.01, -0.02], "构造")
    assert abs(registered - (math.log1p(0.006) + math.log1p(-0.012))) <= 1e-12
    assert abs(substituted - (math.log1p(0.003) + math.log1p(-0.006))) <= 1e-12
    assert abs(difference - (substituted - registered)) <= 1e-15


# ================================================================ (b) 生成规则与人工期望值表


def test_generator_matches_hand_table() -> None:
    """生成规则的关键输出与人工推算表一致（约束生成函数本身）。"""
    model = build_model(variant())
    for mine, hand in zip(model["us"], U_HAND, strict=True):
        assert abs(mine - hand) <= 1e-15
    records = model["records"]
    assert tuple(r["r1"][3] for r in records) == R1_HAND
    for r in records:
        assert abs(r["r1"][1] - SIGNAL_MDD_HAND[(r["order"] // 3) % 3]) <= 1e-12
        assert abs(r["r1"][2] - HOLD_MDD_HAND) <= 1e-12
        assert r["r2"]["SPX"][1:] == R2_BY_START_HAND[r["order"] % 9]
        assert r["r2"]["QQQ"][1:] == R2_BY_START_HAND[(r["order"] + 1) % 9]
        a, b, c = r["order"] // 9, (r["order"] // 3) % 3, r["order"] % 3
        magnitude = MAGNITUDE_STEP_HAND[a] + 2 * MAGNITUDE_STEP_HAND[b] + 2 * MAGNITUDE_STEP_HAND[c]
        assert abs(gen_switches(positions_of(r["key"]))[1] - magnitude) <= 1e-12
    assert tuple(r["switches"] for r in records) == SWITCHES_HAND
    outcome, feasible, maximum, tied, selected = model["selection"]
    assert (outcome, selected, tied) == ("选定", SELECTED_HAND, [SELECTED_HAND])
    assert feasible == [KEYS[i] for i in FEASIBLE_HAND] and abs(maximum - M_HAND) <= 1e-12
    for got, hand in (
        (model["constants"][REF], CONST_REF_HAND),
        (model["constants"][SELECTED_HAND], CONST_SELECTED_HAND),
    ):
        assert all(abs(x - y) <= 1e-12 for x, y in zip(got, hand, strict=True))
    hold = path_summary(model, HOLD, "一直持有")[0]
    assert abs(hold - math.fsum(math.log1p(1.4 * u) for u in U_HAND)) <= 1e-12
    even = path_summary(model, KEYS[0], "执行政策研究模拟")[0] - path_summary(model, KEYS[0], "信号模拟")[0]
    assert abs(even - POLICY_DIFF_EVEN_HAND) <= 1e-12
    row = rows_of({"t": substitution_table(model)}, "t", object=KEYS[1])[0]
    assert all(abs(x - y) <= 1e-12 for x, y in zip(row[4:7], SUBSTITUTION_HAND, strict=True))


# ================================================================ (b) CLI：完整 27 组目录


def test_ab_full_cli_on_generated_directory(tmp_path: Path) -> None:
    """结构 A（count(E) = count(K) = 2）与另一例 count(E) = count(K) = 1（出口无合格候选）：两脚本退出码 0、
    报告逐项一致。"""
    (code_a, report_a, err_a), (code_b, report_b, err_b) = run_both(tmp_path, make_project(variant()), "full")
    assert code_a == 0, (err_a, problems(report_a)[:3])
    assert code_b == 0, (err_b, problems(report_b)[:3])
    assert report_a["summary"]["mismatches"] == 0 and report_b["summary"]["mismatches"] == 0
    assert report_a["object_sets"]["count_O_d"] == 30 and report_a["object_sets"]["count_K"] == 2
    single = [i for i in report_a["items"] if i["item"].startswith("单一路径") and i["item"].endswith("passed")]
    relative = [i for i in report_a["items"] if i["item"] == "相对主参照 passed"]
    assert (len(single), len(relative)) == (2 * 30 + 1 + 2, 27)
    selected = items_where(report_a, item="selected")[0]
    assert selected["script"] == SELECTED_HAND
    assert abs(items_where(report_a, item="maximum")[0]["script"] - M_HAND) <= 1e-12
    feasible = items_where(report_a, item="feasible")[0]["script"]
    assert feasible == [KEYS[i] for i in FEASIBLE_HAND]
    exposure = items_where(report_b, item="exposure", object="恒定仓位：主参照")[0]["script"]
    assert abs(exposure - CONST_REF_HAND[2]) <= 1e-12
    (code_a, report_a, err_a), (code_b, report_b, err_b) = run_both(
        tmp_path, make_project(variant(evaluable2=False, r2_pass=False)), "e1k1"
    )
    assert (code_a, code_b) == (0, 0), (err_a, err_b, problems(report_a)[:3], problems(report_b)[:3])
    assert report_a["object_sets"]["count_O_d"] == 29 and report_a["object_sets"]["count_K"] == 1
    assert items_where(report_a, item="outcome")[0]["script"] == "无合格候选"


def test_ab_full_cli_detects_single_mismatch(tmp_path: Path) -> None:
    """改动任一汇总值 → 退出码 1，首个不一致定位到该项。"""
    files = make_project(variant())
    row = rows_of(files, "candidates_summary.csv", object=SELECTED_HAND)[0]
    row[column(files, "candidates_summary.csv", "switches")] = 3  # 人工值为 2
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "mis_a")
    assert code == 1 and report["first_mismatch"]["object"] == SELECTED_HAND
    assert report["first_mismatch"]["item"] == "switches" and report["summary"]["mismatches"] == 1
    files = make_project(variant())
    files["descriptive.json"]["constants"][0]["exposure"] = 0.9 + 1e-9  # 人工值 0.9，差 1e-9 > 1e-12
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "mis_b")
    assert code == 1 and report["first_mismatch"]["item"] == "exposure"
    assert report["first_mismatch"]["object"] == "恒定仓位：主参照"


def test_b_log_wealth_summary(tmp_path: Path) -> None:
    """乙层汇总：各对象 ln W_末与项目值之差 ≤ 1e-10；一直持有 ln W 等于人工 Σlog1p(1.4·U_i)。"""
    code, report, err = run_one(SCRIPT_B, tmp_path, make_project(variant()), "sum")
    assert code == 0, err
    logs = items_where(report, item="signal_log_wealth")
    assert len(logs) == 27 + 4
    assert all(i["status"].startswith("一致") and (i["difference"] is None or i["difference"] <= 1e-10) for i in logs)
    hold = items_where(report, item="signal_log_wealth", object=HOLD)[0]["script"]
    assert abs(hold - math.fsum(math.log1p(1.4 * u) for u in U_HAND)) <= 1e-12


# ================================================================ (b)(c) CLI：甲层故障注入


def test_a_reconciliation_detects_mismatch(tmp_path: Path) -> None:
    """某对象末行 wealth 改动（差值 > 1e-10）→ 不一致；首个差异定位到该对象末行。"""
    files = make_project(variant())
    row = rows_of(files, "daily_nav.csv.gz", object=KEYS[3], path="信号模拟", date=DAYS[-1])[0]
    row[3] = row[3] * (1 + 1e-4)
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "recon")
    assert code == 1
    first = report["first_mismatch"]
    assert (first["object"], first["item"]) == (KEYS[3], "逐日净值") and DAYS[-1] in first["position"]
    assert items_where(report, item="对账 Σlog1p 与项目 ln W", object=KEYS[3], status="不一致")


def test_a_r2_unknown_category_is_input_error(tmp_path: Path) -> None:
    """一行 category 为“达标” → 退出码 2。"""
    files = make_project(variant())
    files["r2_judgements.csv.gz"]["rows"][0][3] = "达标"
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "r2cat")
    assert code == 2 and "表外取值" in report["error"]["message"]


def test_a_candidate_records_from_registration(tmp_path: Path) -> None:
    """records 少一组 / 顺序颠倒 / 多一个未知对象 → 均不一致并定位。"""
    files = make_project(variant())
    files["selection.json"]["records"].pop()
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "rec1")
    assert code == 1 and items_where(report, item="候选记录集合", object=KEYS[26], status="不一致")
    files = make_project(variant())
    records = files["selection.json"]["records"]
    records[0], records[1] = records[1], records[0]
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "rec2")
    assert code == 1 and items_where(report, item="候选记录集合", object=KEYS[0], status="不一致")
    files = make_project(variant())
    files["selection.json"]["records"].append(dict(files["selection.json"]["records"][0], order=27, object="未知对象"))
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "rec3")
    assert code == 1 and items_where(report, item="候选记录集合", object="未知对象", status="不一致")


def test_a_reference_rows_required(tmp_path: Path) -> None:
    """reference_summary 缺“一直持有”行 → 不一致，定位到该对象。"""
    files = make_project(variant())
    files["reference_summary.csv"]["rows"] = [r for r in files["reference_summary.csv"]["rows"] if r[0] != HOLD]
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "ref")
    assert code == 1 and items_where(report, item="参照行集合", object=HOLD, status="不一致")


def test_a_constants_follow_outcome(tmp_path: Path) -> None:
    """出口非“选定”但 constants[1].computed 为真 → 不一致，定位到该对象。"""
    files = make_project(variant(evaluable2=False, r2_pass=False))
    files["descriptive.json"]["constants"][1]["computed"] = True
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "const")
    assert code == 1 and items_where(report, item="恒定仓位条件", object="选定候选", status="不一致")


def test_a_illegal_object_path_combination(tmp_path: Path) -> None:
    """daily_nav 含“一直持有”对象的“信号模拟”行 → 不一致：对象或路径不在合法组合表内。"""
    files = make_project(variant())
    files["daily_nav.csv.gz"]["rows"].append([DAYS[0], HOLD, "信号模拟", 1.0, None])
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "illegal")
    assert code == 1
    assert items_where(report, item="合法组合", object=HOLD, status="不一致：对象或路径不在合法组合表内")


def test_a_missing_row_is_input_error(tmp_path: Path) -> None:
    """daily_targets 某对象少一个执行日 → 退出码 2，指出表、对象、首个不符日期。"""
    files = make_project(variant())
    files["daily_targets.csv.gz"]["rows"].remove(
        rows_of(files, "daily_targets.csv.gz", object=KEYS[5], date=DAYS[3])[0]
    )
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "miss")
    assert code == 2
    assert "daily_targets" in report["error"]["location"] and KEYS[5] in report["error"]["location"]
    assert DAYS[3] in report["error"]["message"]


def test_a_duplicate_row_is_input_error(tmp_path: Path) -> None:
    """daily_targets 多一行重复日期 → 退出码 2。"""
    files = make_project(variant())
    files["daily_targets.csv.gz"]["rows"].append(
        list(rows_of(files, "daily_targets.csv.gz", object=KEYS[2], date=DAYS[1])[0])
    )
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "dup")
    assert code == 2 and "同日重复" in report["error"]["message"] and DAYS[1] in report["error"]["message"]


def test_a_first_wealth_must_be_one(tmp_path: Path) -> None:
    """项目首行 wealth = 1.01 → 不一致：首行净值不为 1（独立链仍从 1 起）。"""
    files = make_project(variant())
    rows_of(files, "daily_nav.csv.gz", object=KEYS[7], path="信号模拟", date=DAYS[0])[0][3] = 1.01
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "first")
    assert code == 1 and items_where(report, item="首行净值", object=KEYS[7], status="不一致：首行净值不为 1")


def test_a_structure_b_partial_policy(tmp_path: Path) -> None:
    """结构 B（QQQ 在 days[2] 缺价，known = 2）：daily_policy 行数各为 known + k，wealth 只前 2 行非空 → 通过。"""
    code, report, err = run_one(SCRIPT_A, tmp_path, make_project(variant(missing_qqq=2)), "b_part")
    assert code == 0, (err, problems(report)[:3])
    assert report["structure"] == "B"
    assert items_where(report, item="outcome")[0]["script"] == "缺值无法评价"


def test_a_structure_b_wealth_prefix(tmp_path: Path) -> None:
    """变体：某对象 wealth 非空行不构成前缀 → 退出码 2。"""
    files = make_project(variant(missing_qqq=2))
    rows = rows_of(files, "daily_policy.csv.gz", object=KEYS[0])
    rows[0][7], rows[2][7] = None, 1.0
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "b_prefix")
    assert code == 2 and "前缀" in report["error"]["message"]


def test_a_mixed_computable_flags_stop(tmp_path: Path) -> None:
    """变体：r1_computable 有真有假 → 退出码 2“结构不在已定义集合内”。"""
    files = make_project(variant(missing_qqq=2))
    files["candidates_summary.csv"]["rows"][0][column(files, "candidates_summary.csv", "r1_computable")] = True
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "mixed")
    assert code == 2 and "结构不在已定义集合内" in report["error"]["message"]


def test_a_computable_without_hold_path_stops(tmp_path: Path) -> None:
    """变体：r1_computable 全真但无“一直持有”路径 → 退出码 2。"""
    files = make_project(variant())
    files["daily_nav.csv.gz"]["rows"] = [r for r in files["daily_nav.csv.gz"]["rows"] if r[2] != "一直持有"]
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "nohold")
    assert code == 2 and "结构不在已定义集合内" in report["error"]["message"]


def test_a_usage_restriction_does_not_change_structure_a(tmp_path: Path) -> None:
    """用途限制非空（历史缺价在评价窗口之前）而 r1_computable 全真、daily_nav 完整 → 按 A 核对，照录用途限制。"""
    files = make_project(variant(history_gap=True, usage="历史读取缺价，不得形成正式选定结论"))
    code, report, err = run_one(SCRIPT_A, tmp_path, files, "usage")
    assert code == 0, (err, problems(report)[:3])
    assert report["structure"] == "A" and report["usage_restriction"] == "历史读取缺价，不得形成正式选定结论"


def test_a_structure_b_without_usage_restriction(tmp_path: Path) -> None:
    """用途限制为空而 r1_computable 全假、daily_nav 无行 → 按 B 核对。"""
    code, report, err = run_one(SCRIPT_A, tmp_path, make_project(variant(missing_qqq=2, usage=None)), "b_nousage")
    assert code == 0, err
    assert report["structure"] == "B" and report["usage_restriction"] is None


def test_a_structure_fields_must_be_boolean(tmp_path: Path) -> None:
    """averages[0].evaluable 为字符串 "true" → 退出码 2“结构判定字段非法”。"""
    files = make_project(variant())
    files["descriptive.json"]["averages"][0]["evaluable"] = "true"
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "bool")
    assert code == 2 and "结构判定字段非法" in report["error"]["message"]


def test_a_structure_fields_missing(tmp_path: Path) -> None:
    """constants 缺 computed → 退出码 2。"""
    files = make_project(variant())
    del files["descriptive.json"]["constants"][0]["computed"]
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "missing")
    assert code == 2 and "结构判定字段非法" in report["error"]["message"]


def test_a_duplicate_average_names(tmp_path: Path) -> None:
    """averages 两项 name 相同 → 退出码 2。"""
    files = make_project(variant())
    files["descriptive.json"]["averages"][1]["name"] = AVG1
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "dupname")
    assert code == 2 and "结构判定字段非法" in report["error"]["message"]


def test_a_constant_identity_checked(tmp_path: Path) -> None:
    """constants[0].object 不是主参照（改为某候选键）→ 不一致。"""
    files = make_project(variant())
    files["descriptive.json"]["constants"][0]["object"] = KEYS[0]
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "ident")
    assert code == 1 and items_where(report, item="恒定仓位身份", object=KEYS[0], status="不一致")


def test_a_unevaluable_average_has_no_rows(tmp_path: Path) -> None:
    """averages[1].evaluable 为假、无任何逐日行、reference_summary 该行为空 → E 一条，O_d 29，不判缺行。"""
    code, report, err = run_one(SCRIPT_A, tmp_path, make_project(variant(evaluable2=False)), "uneval")
    assert code == 0, (err, problems(report)[:3])
    assert report["object_sets"]["E"] == [AVG1] and report["object_sets"]["count_O_d"] == 29
    single = [i for i in report["items"] if i["item"].startswith("单一路径") and i["item"].endswith("passed")]
    assert len(single) == 2 * 29 + 1 + 2


def test_a_evaluable_average_missing_rows(tmp_path: Path) -> None:
    """变体：evaluable 为真却无 daily_targets 行 → 退出码 2 缺行。"""
    files = make_project(variant(evaluable2=False))
    files["descriptive.json"]["averages"][1]["evaluable"] = True
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "evalmiss")
    assert code == 2 and "行数或日期不符" in report["error"]["message"] and AVG2 in report["error"]["location"]


def test_a_unevaluable_average_with_rows_is_mismatch(tmp_path: Path) -> None:
    """变体：evaluable 为假却有 daily_nav 行 → 不一致：对象或路径不在合法组合表内。"""
    files = make_project(variant())
    files["descriptive.json"]["averages"][1]["evaluable"] = False
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "uneval_rows")
    assert code == 1
    assert items_where(report, item="合法组合", object=AVG2, status="不一致：对象或路径不在合法组合表内")


def test_ab_project_nan_not_passed_by_tolerance(tmp_path: Path) -> None:
    """candidates_summary.signal_log_wealth 为 nan：独立结果有限 → 不一致：项目值非有限，退出码 1（两层）。"""
    files = make_project(variant())
    files["candidates_summary.csv"]["rows"][0][column(files, "candidates_summary.csv", "signal_log_wealth")] = "nan"
    (code_a, report_a, _), (code_b, report_b, _) = run_both(tmp_path, files, "nan")
    for code, report in ((code_a, report_a), (code_b, report_b)):
        assert code == 1
        assert items_where(report, item="signal_log_wealth", object=KEYS[0], status="不一致：项目值非有限")


# ================================================================ (b)(c) CLI：乙层故障注入


def test_b_missing_price_mid_chain(tmp_path: Path) -> None:
    """QQQ 在 days[2] 空：known = 2（由快照算出），前 2 行 wealth 重建 W_0 = 1、W_1 = 1 + R_1 并一致。"""
    code, report, err = run_one(SCRIPT_B, tmp_path, make_project(variant(missing_qqq=2)), "mid")
    assert code == 0, (err, problems(report)[:3])
    assert (report["structure"], report["known"]) == ("B", 2)
    prefix = items_where(report, item="已知前缀净值", object=REF)
    assert len(prefix) == 2 and all(i["status"] == "一致" for i in prefix)
    # 主参照 t0 为二级 (0.3, 0)：W_1 = 1 + 0.3·U1 = 1.006
    assert abs(prefix[1]["script"] - 1.006) <= 1e-12


def test_b_missing_price_at_first_day(tmp_path: Path) -> None:
    """QQQ 在 days[0] 空：known = 0，不建立已知净值，只核对 wealth 非空行数为 0。"""
    code, report, err = run_one(SCRIPT_B, tmp_path, make_project(variant(missing_qqq=0)), "first")
    assert code == 0, (err, problems(report)[:3])
    assert report["known"] == 0 and not items_where(report, item="已知前缀净值")
    counts = items_where(report, item="wealth 非空行数等于 known")
    assert len(counts) == 30 and all(i["project"] == 0 for i in counts)


def test_b_structure_b_rejects_nav_rows(tmp_path: Path) -> None:
    """结构 B（快照缺价）而构造目录含 daily_nav 行 → 退出码 2“结构不在已定义集合内”。"""
    files = make_project(variant(missing_qqq=2))
    files["daily_nav.csv.gz"]["rows"].append([DAYS[0], REF, "信号模拟", 1.0, None])
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "bnav")
    assert code == 2 and "结构不在已定义集合内" in report["error"]["message"]


def test_b_known_from_snapshot_not_from_rows(tmp_path: Path) -> None:
    """daily_policy 某对象 wealth 非空行数 3 ≠ known 2 → 退出码 1 不一致（known 不从行数反推）。"""
    files = make_project(variant(missing_qqq=2))
    rows_of(files, "daily_policy.csv.gz", object=KEYS[1])[2][7] = 1.0
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "known")
    assert code == 1 and report["known"] == 2
    item = items_where(report, item="wealth 非空行数等于 known", object=KEYS[1])[0]
    assert (item["script"], item["project"], item["status"]) == (2, 3, "不一致：值不同")


def test_b_history_gap_before_window_is_structure_a(tmp_path: Path) -> None:
    """历史段（days[0] 之前）QQQ 空、评价窗口齐全：known 为无，项目须为结构 A；项目若为结构 B → 不一致：
    结构与快照不符。"""
    files = make_project(variant(history_gap=True))
    code, report, err = run_one(SCRIPT_B, tmp_path, files, "hist")
    assert code == 0, err
    assert (report["structure"], report["known"]) == ("A", None)
    files["daily_nav.csv.gz"]["rows"] = []
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "hist_b")
    assert code == 1 and items_where(report, item="结构与快照", status="不一致：结构与快照不符")


def test_b_target_axis_mismatch(tmp_path: Path) -> None:
    """daily_targets 少一个执行日 → 退出码 2。"""
    files = make_project(variant())
    files["daily_targets.csv.gz"]["rows"].remove(rows_of(files, "daily_targets.csv.gz", object=REF, date=DAYS[4])[0])
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "axis")
    assert code == 2 and "行数或日期不符" in report["error"]["message"]


def test_b_zero_price_non_finite(tmp_path: Path) -> None:
    """spx_close 为 0.00 → 下一区间除零 → 退出码 3。"""
    files = make_project(variant())
    rows_of(files, "input_snapshot.csv.gz", date=DAYS[1])[0][1] = "0.00"
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "zero")
    assert code == 3 and report["error"]["kind"] == "计算失败" and "区间 2" in report["error"]["location"]


def test_b_substitution_preconditions(tmp_path: Path) -> None:
    """变体：区间起点 position 为“正常”→ 不一致；起点不在熊市 → 不一致。"""
    files = make_project(variant())
    row = rows_of(files, "exposure_substitution.csv", object=KEYS[0])[0]  # c = 0：2000-03-24 为正常
    u = U_HAND[4]
    row[3:8] = [
        1,
        math.log1p(0.6 * u),
        math.log1p(0.3 * u),
        math.log1p(0.3 * u) - math.log1p(0.6 * u),
        f"{DAYS[4]}/{DAYS[5]}",
    ]
    row = rows_of(files, "exposure_substitution.csv", object=KEYS[3])[0]  # b = 1：2000-03-22 为一级，但不在熊市
    row[3:8] = [
        1,
        math.log1p(0.6 * -0.1),
        math.log1p(0.3 * -0.1),
        math.log1p(0.3 * -0.1) - math.log1p(0.6 * -0.1),
        f"{DAYS[2]}/{DAYS[3]}",
    ]
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "subst")
    assert code == 1
    assert items_where(report, item="替换区间", object=KEYS[0], status="不一致：起点 position 不是一级")
    assert items_where(report, item="替换区间", object=KEYS[3], status="不一致：起点不满足熊市条件")
    assert not items_where(report, item="registered_log", object=KEYS[3], status="不一致")


def run_dir(script: Path, root: Path, tag: str) -> tuple[int, object, str]:
    """对已写出的构造目录运行一个脚本。"""
    return run_script(script, root, root.parent / f"report_{tag}.json")


def test_b_manifest_snapshot_line(tmp_path: Path) -> None:
    """乙层只取 MANIFEST.sha256 中 input_snapshot.csv.gz 的唯一一行，与快照实际 SHA-256、字节数核对（勘误 K4）：
    缺失、重复、格式非法（大写十六进制）、字节数不符 → 退出码 2；其他文件的行被改动不影响（不读清单所指其他文件）。"""
    files = make_project(variant())
    name = "input_snapshot.csv.gz"

    def keep_other(text: str) -> str:
        return "".join(line for line in text.splitlines(keepends=True) if not line.rstrip("\n").endswith(" " + name))

    def snapshot_line(text: str) -> str:
        return next(line for line in text.splitlines(keepends=True) if line.rstrip("\n").endswith(" " + name))

    cases = {
        "missing": keep_other,
        "duplicate": lambda text: text + snapshot_line(text),
        "upper": lambda text: keep_other(text) + snapshot_line(text).upper().replace(name.upper(), name),
        "bytes": lambda text: keep_other(text) + snapshot_line(text).replace(" ", " 1", 1),
    }
    for tag, transform in cases.items():
        root = write_project(tmp_path / f"m_{tag}", files)
        edit_manifest(root, transform)
        code, report, _ = run_dir(SCRIPT_B, root, tag)
        assert code == 2 and "MANIFEST" in report["error"]["location"], (tag, report["error"])
    root = write_project(tmp_path / "m_other", files)
    edit_manifest(root, lambda text: text.replace(" window.json", " window.json.旧"))
    code, report, _ = run_dir(SCRIPT_B, root, "other")
    assert code == 0 and items_where(report, item="快照 SHA-256 与字节数")[0]["status"] == "一致"


def test_a_state_statistics_object_set_and_truncated(tmp_path: Path) -> None:
    """state_statistics 对象恰为 27 候选 + 主参照：缺主参照、多一条均线对照、缺 start_note → 退出码 2；
    truncated 逐条精确：候选 o = 18（a = 2，t0 二级、t1 正常）人工值 [二级, d0, d1, 真, 假]，改右截断标志 → 不一致；
    主参照 t0、t1 二级、t2 一级 → [二级, d0, d2, 真, 假]。"""
    files = make_project(variant())
    entries = files["descriptive.json"]["state_statistics"]
    assert entries[18]["truncated"] == [["二级", DAYS[0], DAYS[1], True, False]]
    assert entries[27]["object"] == REF and entries[27]["truncated"] == [["二级", DAYS[0], DAYS[2], True, False]]
    code, report, err = run_one(SCRIPT_A, tmp_path, files, "st_ok")
    assert code == 0, err
    assert items_where(report, item="truncated", object=REF)[0]["script"] == [["二级", DAYS[0], DAYS[2], True, False]]
    variants = (
        ("no_ref", lambda e: e.pop()),
        ("extra", lambda e: e.append(dict(e[0], object=AVG1))),
        ("no_note", lambda e: e[3].pop("start_note")),
    )
    for tag, change in variants:
        files = make_project(variant())
        change(files["descriptive.json"]["state_statistics"])
        code, report, _ = run_one(SCRIPT_A, tmp_path, files, f"st_{tag}")
        assert code == 2 and "state_statistics" in report["error"]["location"], tag
    files = make_project(variant())
    files["descriptive.json"]["state_statistics"][18]["truncated"][0][4] = True
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "st_trunc")
    assert code == 1 and items_where(report, item="truncated", object=KEYS[18], status="不一致")


def test_a_policy_minus_signal_keys(tmp_path: Path) -> None:
    """policy_minus_signal 键恰为 27 候选 + 主参照 + 两条均线对照：缺一键或缺 note → 退出码 2；
    不可评价均线对照的 difference 为 null（E1K1 目录通过，见 test_ab_full_cli_on_generated_directory）。"""
    files = make_project(variant())
    del files["descriptive.json"]["policy_minus_signal"][AVG2]
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "pms_key")
    assert code == 2 and "policy_minus_signal" in report["error"]["location"]
    files = make_project(variant())
    del files["descriptive.json"]["policy_minus_signal"][REF]["note"]
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "pms_note")
    assert code == 2
    files = make_project(variant(evaluable2=False))
    assert files["descriptive.json"]["policy_minus_signal"][AVG2]["difference"] is None


def test_a_unevaluable_average_null_blocks(tmp_path: Path) -> None:
    """evaluable 为假的均线对照：switches、signal、policy 须为 JSON null，
    写成 {0,0.0,0.0} 或 {} → 不一致（null、0、{} 不相等）。"""
    for tag, key, value in (
        ("sw", "switches", {"count": 0, "magnitude_total": 0.0, "change_total": 0.0}),
        ("sig", "signal", {}),
    ):
        files = make_project(variant(evaluable2=False))
        files["descriptive.json"]["averages"][1][key] = value
        code, report, _ = run_one(SCRIPT_A, tmp_path, files, f"avg_{tag}")
        assert code == 1 and items_where(report, object=AVG2, status="不一致"), tag


def test_ab_constants_not_computed_null(tmp_path: Path) -> None:
    """computed 为假的恒定仓位：nav 写成 {} → 甲层不一致；core 写成 0.0 → 乙层不一致（答复单第 14 项）。"""
    files = make_project(variant(evaluable2=False, r2_pass=False))
    files["descriptive.json"]["constants"][1]["nav"] = {}
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "cn_a")
    assert code == 1 and items_where(report, object="恒定仓位：选定候选", status="不一致")
    files = make_project(variant(evaluable2=False, r2_pass=False))
    files["descriptive.json"]["constants"][1]["core"] = 0.0
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "cn_b")
    assert code == 1 and items_where(report, object="恒定仓位：选定候选", status="不一致")


def test_a_selection_fields_by_outcome(tmp_path: Path) -> None:
    """选定：feasible 顺序颠倒 → 不一致；元素写成顺序号 → 退出码 2。无合格候选：maximum 写成 0.0 → 不一致；
    出口写成“缺值无法评价” → outcome 与“出口按 records 成立”均不一致（records 无空值、无可行 → 应为无合格候选）。"""
    files = make_project(variant())
    files["selection.json"]["feasible"].reverse()
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "sel_rev")
    assert code == 1 and items_where(report, item="feasible", status="不一致")
    files = make_project(variant())
    files["selection.json"]["tied"] = [6]
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "sel_int")
    assert code == 2 and "tied" in report["error"]["location"]
    files = make_project(variant(evaluable2=False, r2_pass=False))
    files["selection.json"]["maximum"] = 0.0
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "sel_max")
    assert code == 1 and items_where(report, item="maximum", status="不一致")
    files = make_project(variant(evaluable2=False, r2_pass=False))
    files["selection.json"]["outcome"] = "缺值无法评价"
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "sel_out")
    assert code == 1
    item = items_where(report, item="出口按 records 成立")[0]
    assert (item["script"], item["project"]) == ("无合格候选", "缺值无法评价")


def compute_failure_files(feasible: list[str], failed_order: int | None) -> dict[str, object]:
    """构造目录（结构 A，R2 全部失败的变体，项目出口原为“无合格候选”，constants[1] 为选定候选且 computed 为假、
    无其恒定仓位逐日行）改写为项目出口“计算失败”：selected、maximum 为 null，tied 为空列表，feasible 取参数；
    failed_order 不为空时把该候选记录的 failed 设为真（候选分支），否则 records 无 failed（主参照分支）。"""
    files = make_project(variant(r2_pass=False))
    selection = files["selection.json"]
    selection.update({"outcome": "计算失败", "selected": None, "feasible": feasible, "maximum": None, "tied": []})
    if failed_order is not None:
        selection["records"][failed_order]["failed"] = True
    return files


def test_a_compute_failure_reference_branch(tmp_path: Path) -> None:
    """答复单定稿修订一第 7 项与负责人补充执行限定（人工推算见人工期望说明第 8 节）：
    项目出口“计算失败”、records 无 failed、四个结果字段为空 → 主参照分支：outcome 一项记“保留：
    主参照失败原因未独立核验”，
    不判不一致、单列计数（reserved = 1），旁证为主参照行 signal_log_wealth、
    signal_max_drawdown 均非空（结构 A），退出码 0；
    同条件但 feasible = [o = 6 的键] → feasible 一项不一致（期望空列表），退出码 1。"""
    code, report, err = run_one(SCRIPT_A, tmp_path, compute_failure_files([], None), "cf_ref")
    assert code == 0, (err, problems(report)[:3])
    outcome = items_where(report, item="outcome")[0]
    assert outcome["status"] == "保留：主参照失败原因未独立核验" and outcome["project"] == "计算失败"
    assert report["summary"]["reserved"] == 1 and report["first_mismatch"] is None
    evidence = items_where(report, item="主参照失败旁证")[0]["project"]
    assert evidence == {"signal_log_wealth 为空": False, "signal_max_drawdown 为空": False}
    assert not items_where(report, item="出口按 records 成立")
    for key in ("selected", "feasible", "maximum", "tied"):
        assert items_where(report, item=key)[0]["status"].startswith("一致"), key
    code, report, _ = run_one(SCRIPT_A, tmp_path, compute_failure_files([SELECTED_HAND], None), "cf_ref_feasible")
    assert code == 1
    item = items_where(report, item="feasible")[0]
    assert (item["script"], item["project"], item["status"]) == ([], [SELECTED_HAND], "不一致：值不同")


def test_a_compute_failure_candidate_branch(tmp_path: Path) -> None:
    """项目出口“计算失败”且候选 o = 3 的 failed 为真：候选分支，脚本出口亦为“计算失败”（优先级最高），outcome 与
    “出口按 records 成立”两项一致，无保留项；四个结果字段按空值核对一致；退出码 0。"""
    code, report, err = run_one(SCRIPT_A, tmp_path, compute_failure_files([], 3), "cf_cand")
    assert code == 0, (err, problems(report)[:3])
    assert items_where(report, item="outcome")[0]["status"] == "一致"
    assert items_where(report, item="出口按 records 成立")[0]["script"] == "计算失败"
    assert report["summary"]["reserved"] == 0


def constant_recon_rows(files: dict[str, object]) -> list[dict[str, object]]:
    """构造目录 reconciliation.json 中路径为“恒定仓位”的单一路径对账行。"""
    rows = files["reconciliation.json"]["rows"]
    return [row for row in rows if row["kind"] == "单一路径" and row["path"] == "恒定仓位"]


def test_a_constant_recon_naming(tmp_path: Path) -> None:
    """恒定仓位对象命名（第四次交付指令；人工推算见人工期望说明第 9 节）。主目录 K = {恒定仓位：主参照,
    恒定仓位：K=3,θ_P=0.025,h=1}：(a) 对账行 object 为原对象名 主参照、K=3,θ_P=0.025,h=1 → 对账一致、退出码 0；
    (b) 对账行 object 写成 daily_nav 的“恒定仓位：<对象>” → 每个恒定仓位各一缺一多（缺 2、多 2），退出码 1；
    (c) 对账行 object 写成半角冒号前缀“恒定仓位:<对象>” → 同样一缺一多，退出码 1；
    另 daily_nav 恒定仓位行改用半角冒号前缀
    → 该（对象，路径）不在合法组合表内；K 成员“恒定仓位：主参照”的必需行随之缺失，按既有缺行规则退出码 2。"""
    files = make_project(variant())
    assert [row["object"] for row in constant_recon_rows(files)] == [REF, SELECTED_HAND]
    code, report, err = run_one(SCRIPT_A, tmp_path, files, "cn_a")
    assert code == 0, (err, problems(report)[:3])
    passed = items_where(report, item="单一路径 恒定仓位 passed")
    assert len(passed) == 2 and all(item["status"] == "一致" for item in passed)
    for tag, prefix in (("cn_b", "恒定仓位："), ("cn_c", "恒定仓位:")):
        files = make_project(variant())
        for row in constant_recon_rows(files):
            row["object"] = prefix + row["object"]
        code, report, _ = run_one(SCRIPT_A, tmp_path, files, tag)
        assert code == 1, tag
        missing = items_where(report, item="对账行", status="不一致：缺少对账行")
        extra = items_where(report, item="对账行", status="不一致：多余对账行")
        assert sorted(item["object"] for item in missing) == sorted(["恒定仓位：主参照", "恒定仓位：" + SELECTED_HAND])
        assert sorted(item["object"] for item in extra) == sorted([prefix + REF, prefix + SELECTED_HAND]), tag
        assert len(problems(report)) == 4, tag
    files = make_project(variant())
    for row in rows_of(files, "daily_nav.csv.gz", object="恒定仓位：主参照"):
        row[1] = "恒定仓位:主参照"
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "cn_nav")
    assert code == 2 and "恒定仓位：主参照" in report["error"]["location"]


def test_a_reference_hold_row_and_segment_undefined(tmp_path: Path) -> None:
    """一直持有行 exposure_magnitude 期望 0.0（改为 0.1 → 不一致）；
    “无收益区间”行 undefined 期望 False（改为 True → 不一致）；
    coverage 为四种文字以外 → 退出码 2。"""
    files = make_project(variant())
    rows_of(files, "reference_summary.csv", object=HOLD)[0][6] = 0.1
    rows_of(files, "segments.csv", object=KEYS[0], segment="2010—2016")[0][9] = True
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "hold_seg")
    assert code == 1
    assert items_where(report, item="exposure_magnitude", object=HOLD, status="不一致")
    assert items_where(report, item="undefined", object=KEYS[0], status="不一致")
    files = make_project(variant())
    rows_of(files, "segments.csv", object=KEYS[0], segment="熊市二")[0][4] = "未知覆盖"
    code, report, _ = run_one(SCRIPT_A, tmp_path, files, "seg_cov")
    assert code == 2 and "表外取值" in report["error"]["message"]


def test_b_environment_summary_contract(tmp_path: Path) -> None:
    """environment_summary 键恰为五个环境名、log_return_sums 键恰为 29 个：缺键 → 退出码 2；
    结构 A 某值为 null → 不一致；
    结构 B 某值为数值 → 不一致；结构 B 的 intervals 照常比对（改动 → 不一致）。
    空分类（上涨年等）结构 A 下人工值为 0.0。"""
    files = make_project(variant())
    assert files["descriptive.json"]["environment_summary"]["上涨年"] == {
        "intervals": 0,
        "log_return_sums": {obj: 0.0 for obj in (*KEYS, REF, HOLD)},
    }
    del files["descriptive.json"]["environment_summary"]["上涨年"]
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "env_key")
    assert code == 2 and "environment_summary" in report["error"]["location"]
    files = make_project(variant())
    del files["descriptive.json"]["environment_summary"]["熊市"]["log_return_sums"][HOLD]
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "env_obj")
    assert code == 2
    files = make_project(variant())
    files["descriptive.json"]["environment_summary"]["熊市"]["log_return_sums"][KEYS[2]] = None
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "env_null")
    assert code == 1 and items_where(report, item="log_return_sums", object=KEYS[2], status="不一致：一方缺值")
    files = make_project(variant(missing_qqq=2))
    summary = files["descriptive.json"]["environment_summary"]
    summary["熊市"]["log_return_sums"][REF] = 0.0
    summary["完整年度分类不可得"]["intervals"] = 3
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "env_b")
    assert code == 1
    assert items_where(report, object=REF, status="不一致") and items_where(report, item="intervals", status="不一致")


def test_b_year_return_and_substitution_empty_fields(tmp_path: Path) -> None:
    """熊市区间 year_return 须为空（写 0.1 → 不一致）；computed 为假的替换行 intervals 须为 0、其余数值与区间须为空。"""
    files = make_project(variant())
    rows_of(files, "environments.csv", interval=5)[0][3] = "0.1"
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "yr")
    assert code == 1 and items_where(report, item="year_return", object="区间 5", status="不一致")
    files = make_project(variant(missing_qqq=2))
    row = rows_of(files, "exposure_substitution.csv", object=KEYS[4])[0]
    row[3], row[4] = 1, 0.0
    code, report, _ = run_one(SCRIPT_B, tmp_path, files, "subst_b")
    assert code == 1 and len(items_where(report, object=KEYS[4], status="不一致")) == 2


# ================================================================ (c) 写入中断与 AST 检查


def report_write_failure(
    module: object, script: Path, tmp_path: Path, tag: str, monkeypatch: object, capsys: object
) -> None:
    """--out 已存在 → 退出码 2 且原文件不变；--out 父路径为普通文件（Windows 上目录只读属性不阻止写入，故以此构造
    不可写目标）→ 退出码 3；写入中途注入异常 → 退出码 3、列明已写出字节、不留临时文件与目标文件。"""
    result = write_project(tmp_path / f"dir_{tag}", make_project(variant()))
    blocker = tmp_path / f"blocker_{tag}"
    blocker.write_bytes(b"x")
    code, _ = run_raw(script, result, blocker)
    assert code == 2 and blocker.read_bytes() == b"x"
    code, report, err = run_script(script, result, blocker / "report.json")
    assert code == 3 and report is None and "报告写入失败" in err
    target = tmp_path / f"out_{tag}" / "report.json"
    target.parent.mkdir()
    original = module.os.write
    calls: list[int] = []

    def partial_write(handle: int, data: object) -> int:
        calls.append(len(data))
        if len(calls) == 1:
            return original(handle, data)  # 第一块（65536 字节）照常写出
        raise OSError("注入：写入中途失败")

    monkeypatch.setattr(module.os, "write", partial_write)
    code = module.main(["--result-dir", str(result), "--out", str(target)])
    monkeypatch.undo()
    stderr = capsys.readouterr().err
    assert code == 3 and "已写出 65536/" in stderr and "临时文件已删除" in stderr and len(calls) == 2
    assert not target.exists() and list(target.parent.iterdir()) == []


def test_a_report_write_failure(mod_a: object, tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    """甲层：报告写入失败 → 退出码 3，标准错误列明已写出部分，不留下与完整报告同名的文件。"""
    report_write_failure(mod_a, SCRIPT_A, tmp_path, "a", monkeypatch, capsys)


def test_b_report_write_failure(mod_b: object, tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    """乙层：同上。"""
    report_write_failure(mod_b, SCRIPT_B, tmp_path, "b", monkeypatch, capsys)


WHITELIST = {
    "argparse",
    "csv",
    "gzip",
    "json",
    "math",
    "hashlib",
    "pathlib",
    "dataclasses",
    "datetime",
    "decimal",
    "bisect",
    "typing",
    "sys",
    "os",
    "tempfile",
}
N4_WHITELIST = {
    "ast",
    "csv",
    "gzip",
    "hashlib",  # 用途限定为构造夹具 MANIFEST.sha256 行的 SHA-256（负责人裁决 P3，2026-10-06）
    "importlib",  # importlib.util
    "json",
    "math",
    "subprocess",
    "sys",  # 只取 sys.executable（勘误补充单 K2）
    "pathlib",
    "pytest",
}
WRITE_ATTRIBUTES = {"write_text", "write_bytes", "touch", "mkdir", "makedirs", "rmdir", "unlink", "rename"}
OS_WRITES = {
    "write",
    "replace",
    "rename",
    "remove",
    "unlink",
    "open",
    "fsync",
    "makedirs",
    "mkdir",
    "rmdir",
    "truncate",
    "close",
}


def write_sites(tree: ast.AST) -> list[tuple[str, str]]:
    """（所在函数, 调用文字）：open()、os 的写入类调用、tempfile 的任何调用、Path 写入类方法。"""
    sites = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                sites.append((function.name, "open"))
            elif isinstance(func, ast.Attribute):
                owner = func.value.id if isinstance(func.value, ast.Name) else ""
                if (owner == "os" and func.attr in OS_WRITES) or owner == "tempfile" or func.attr in WRITE_ATTRIBUTES:
                    sites.append((function.name, f"{owner}.{func.attr}"))
    return sites


def imported_modules(tree: ast.AST) -> set[str]:
    """源文件直接导入的顶层模块名集合。"""
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    return imported


def hashlib_use_sites(tree: ast.AST) -> set[str]:
    """N4 中引用 hashlib 的函数名集合（只应为 sha256_hex）。"""
    sites = set()
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if isinstance(node, ast.Name) and node.id == "hashlib":
                    sites.add(function.name)
    return sites


def test_scripts_import_whitelist_and_write_sites() -> None:
    """N1、N2 的 import 集合 ⊆ 第三节白名单（不含 market_risk、numpy、pandas、yaml）；文件写入只在 --out 处理函数内。
    N4 自身的直接导入 ⊆ N4 白名单（含勘误 K2 的 sys、负责人裁决 P3 的 hashlib），且 hashlib 只在 sha256_hex 中使用。"""
    own = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assert imported_modules(own) <= N4_WHITELIST, imported_modules(own) - N4_WHITELIST
    assert hashlib_use_sites(own) == {"sha256_hex"}
    for script in (SCRIPT_A, SCRIPT_B):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        imported = imported_modules(tree)
        assert imported <= WHITELIST, (script.name, imported - WHITELIST)
        assert not imported & {"market_risk", "numpy", "pandas", "pandas_market_calendars", "yaml"}
        sites = write_sites(tree)
        assert sites, script.name
        assert {name for name, _ in sites} == {"write_report"}, (script.name, sites)
        assert "open" not in {call for _, call in sites}
        top_level_calls = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
        assert not top_level_calls
