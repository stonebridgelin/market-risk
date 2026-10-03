"""波段预警 v2.0 独立复核工具：修正二的限定构造测试（覆盖表第 1 至 13 项）。

依据：《v20 独立工具规格补充条文（修订二）》（SHA-256 6f6c62087fb22d9ea1fe642e88bab453d87739195c12e38653e25355fc27f3f5，
下称“修订二”）与《规格补充条文》（2026-10-02，SHA-256 f3ee75c9…f7f3f，下称“原条文”）。
只在子进程中运行 docs/audit/独立复核/v20/audit_v20.py，不导入任何项目代码。构造数据与输出都写在 pytest 临时目录。

预期值全部按修订二人工推算，写在各测试的注释中，不由工具输出反推。场景由 build_scenarios() 构造；
构造验收时同一函数把场景导出到仓库外的“新增场景”目录（不在本测试中执行）。

公共构造（执行政策场景）：交易日 D000…D044，j0 = 1，S 全为正常、五通道全部有效，两资产第 0—19 日收盘 100.00、
第 20 日起 97.00。于是 U_20 = −3%，R_20 = 1.4 × (−3%) = −4.2%，W_20 = 0.958 ≤ 0.96 × 基准 1：
第 20 日止损确认，第 21 日（s）止损执行，冷却期 22—31，第 31 日（= s+10）信号正常且全部有效 → 重入信号，
第 32 日（r）重入执行（目标 0.6），之后价格不变。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "docs" / "audit" / "独立复核" / "v20" / "audit_v20.py"

NORMAL, LV1 = "正常", "一级"
N_EXEC = 45


def axis_of(n: int) -> list[str]:
    return [f"D{i:03d}" for i in range(n)]


def ramp(n: int, a: float, b: float) -> list[str]:
    return [f"{a + (b - a) * i / (n - 1):.2f}" for i in range(n)]


def stop_path() -> list[str | None]:
    return ["100.00"] * 20 + ["97.00"] * (N_EXEC - 20)


def exec_sc(spx: list[str | None], s_list: list[str] | None = None) -> dict:
    return {
        "kind": "exec",
        "axis": axis_of(N_EXEC),
        "S": s_list or [NORMAL] * N_EXEC,
        "all_valid": [True] * N_EXEC,
        "prices": {"SPX": spx, "QQQ": stop_path()},
        "j0": 1,
    }


def with_none(xs: list, *idx: int) -> list:
    ys = list(xs)
    for i in idx:
        ys[i] = None
    return ys


def flat_full(n: int) -> dict:
    return {
        "kind": "full",
        "axis": axis_of(n),
        "prices": {"SPX": ["100.00"] * n, "QQQ": ["50.00"] * n},
        "params": [[5, "0.02", 3]],
    }


def nyse_sessions(start: str, n: int) -> list[str]:
    import pandas_market_calendars as mcal

    sched = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=f"{int(start[:4]) + 3}-12-31")
    return [x.strftime("%Y-%m-%d") for x in sched.index][:n]


def r2_prices() -> list[str]:
    """规格第八节例子三的价格：P = 第 100 日（100.0），T3 = 103，T5 = 105，Tr = 106，End = 109。"""
    base = [f"{90 + 0.1 * i:.2f}" for i in range(100)]
    tail = ["100.0", "98.5", "97.6", "96.8", "95.5", "94.9", "94.0", "95.0", "97.0", "99.0"]
    return base + tail + [f"{99.5 + 0.1 * i:.2f}" for i in range(31)]


def build_scenarios() -> dict[str, dict]:
    sc: dict[str, dict] = {}
    # 1 缺值分歧 A、B、C1、C2
    sc["A_窗口首日缺价"] = exec_sc(with_none(stop_path(), 1))
    sc["B_重入执行日缺价"] = exec_sc(with_none(stop_path(), 32))
    sc["C1_冷却期缺价后重入"] = exec_sc(with_none(stop_path(), 25))
    sc["C2_止损执行日缺价后重入"] = exec_sc(with_none(stop_path(), 21))
    # 3 缺价后保持现金，窗口内没有重入
    sc["缺价后保持现金"] = exec_sc(with_none(stop_path(), 25), [NORMAL] * 25 + [LV1] * (N_EXEC - 25))
    # 5 价格完整
    sc["价格完整_止损与重入"] = exec_sc(stop_path())
    # 6 截止日前缀相同、未来记录不同
    n = 260
    base = {
        "kind": "signal",
        "axis": axis_of(n),
        "prices": {"SPX": ramp(n, 80, 100), "QQQ": ramp(n, 40, 50)},
        "params": [[5, "0.02", 3]],
        "cutoff": "D249",
    }
    sc["截止_基准无未来"] = {
        **base,
        "axis": axis_of(250),
        "prices": {a: v[:250] for a, v in base["prices"].items()},
        "cutoff": None,
    }
    sc["截止_未来正常"] = base
    sc["截止_未来缺值"] = {**base, "prices": {a: v[:250] + [None] * 10 for a, v in base["prices"].items()}}
    sc["截止_未来非法"] = {
        **base,
        "axis": axis_of(250) + ["D100", 123, "C000", "D249"] + ["坏日期"] * 6,
        "prices": {a: v[:250] + ["abc", -5, None, "1e999"] + ["0"] * 6 for a, v in base["prices"].items()},
    }
    sc["截止_未来长度不同"] = {
        **base,
        "axis": axis_of(270),
        "prices": {"SPX": base["prices"]["SPX"][:250] + ["1.00"] * 3, "QQQ": base["prices"]["QQQ"] + ["2.00"] * 5},
    }
    nf = 320
    sc["截止_full_基准"] = flat_full(nf)
    fut = flat_full(nf)
    fut["axis"] = fut["axis"] + ["D100", 7, "坏"]
    fut["prices"] = {a: [*v, "abc", -1, None] for a, v in fut["prices"].items()}
    fut["cutoff"] = f"D{nf - 1:03d}"
    sc["截止_full_未来非法"] = fut
    # 7 整行缺失；与非法多余日期同时存在
    ses = nyse_sessions("2004-01-02", 260)
    keep = [i for i in range(260) if i != 230]
    sc["整行缺失"] = {
        "kind": "signal",
        "axis": [ses[i] for i in keep],
        "calendar": "NYSE",
        "prices": {"SPX": [ramp(260, 80, 100)[i] for i in keep], "QQQ": [ramp(260, 40, 50)[i] for i in keep]},
        "params": [[5, "0.02", 3]],
    }
    bad = json.loads(json.dumps(sc["整行缺失"]))
    bad["axis"].insert(1, "2004-01-03")  # 星期六：非 NYSE 交易日
    for a in ("SPX", "QQQ"):
        bad["prices"][a].insert(1, "90.00")
    sc["整行缺失_含多余日期"] = bad
    dup = json.loads(json.dumps(sc["整行缺失"]))
    dup["axis"][5] = dup["axis"][4]
    sc["整行缺失_含重复日期"] = dup
    # 8 空窗口两种细分；未收敛；t0 不存在
    sc["空窗口_起点等于末日"] = flat_full(263)
    sc["空窗口_起点晚于末日"] = flat_full(262)
    sc["未收敛"] = flat_full(201)
    sc["t0不存在"] = flat_full(150)
    # 9 杠杆收益因子非法；非有限值（对账失败见夹具测试）
    sc["杠杆因子非法"] = {
        **exec_sc(["100.00"] * 5 + ["40.00"] * (N_EXEC - 5)),
        "prices": {"SPX": ["100.00"] * 5 + ["40.00"] * (N_EXEC - 5), "QQQ": ["100.00"] * 5 + ["40.00"] * (N_EXEC - 5)},
    }
    u = [0.0] * 6
    u[2] = u[3] = 1e308
    sc["非有限值"] = {
        "kind": "exec",
        "axis": axis_of(6),
        "S": [NORMAL] * 6,
        "all_valid": [True] * 6,
        "U": u,
        "j0": 1,
    }
    # 11 提示段起始状态无法确定；确认性检验计算无效
    p3 = r2_prices()
    s_r2 = [NORMAL] * len(p3)
    s_r2[60] = None
    s_r2[61] = s_r2[62] = LV1
    sc["提示段起始状态无法确定"] = {
        "kind": "r2",
        "axis": axis_of(len(p3)),
        "prices": {"SPX": p3},
        "S": s_r2,
        "f": 50,
        "E": len(p3) - 1,
    }
    nc = 400
    sc["确认性检验_缺价计算无效"] = {
        "kind": "confirm",
        "axis": axis_of(nc),
        "prices": {"SPX": with_none(["100.00"] * nc, 320), "QQQ": ["50.00"] * nc},
        "params": [5, "0.02", 3],
        "window_start": "D300",
        "half_split": "D350",
    }
    # 13（Q11）单个缺失日进入、停留、离开 63 日与 200 日窗口
    sc["单日缺价窗口边界"] = {
        "kind": "signal",
        "axis": axis_of(470),
        "prices": {"SPX": with_none(["100.00"] * 470, 250), "QQQ": ["50.00"] * 470},
        "params": [[5, "0.02", 3]],
    }
    return sc


def run_tool(scenario_path: Path, out_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), str(scenario_path), str(out_path)], capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    base = tmp_path_factory.mktemp("v20_fix2")
    res = {}
    for name, sc in build_scenarios().items():
        p = base / f"{name}.json"
        p.write_text(json.dumps({"name": name, **sc}, ensure_ascii=False), encoding="utf-8")
        o = base / f"{name}.out.json"
        proc = run_tool(p, o)
        res[name] = {"code": proc.returncode, "stderr": proc.stderr, "out": json.loads(o.read_text(encoding="utf-8"))}
    return res


def ex_day(o: dict, i: int) -> dict:
    return next(r for r in o["exec_sim"]["days"] if r["idx"] == f"D{i:03d}")


def ev(o: dict) -> list[tuple[str, str]]:
    return [(e["type"], e["idx"]) for e in o["exec_sim"]["events"]]


STOP_EVENTS = [
    ("止损确认", "D020"),
    ("止损执行", "D021"),
    ("重入信号", "D031"),
    ("上限设置", "D031"),
    ("重入执行", "D032"),
]


# ---------------------------------------------------------------- 覆盖项 1：四类缺值分歧


def test_A_窗口首日缺价(out):
    # 修订二第四节第 1、5 条：m = j0 = D001（SPX 缺价），j0 当日持仓（目标 1.4，来自 S_0 = 正常）。
    # 预期：j0 的目标保留（held_exposure = 1.4）；W = 1 不作为已知净值（W 为 null）；j0 当日的止损判断无法确定，
    # 从 j0+1 = D002 的政策目标起“无法确定”：undetermined_from = D001，D002 起 determined = false；
    # 缺价日需要建仓，execution_computable = false；窗口净值“无法计算”；退出码 0。
    r = out["A_窗口首日缺价"]
    assert r["code"] == 0 and "stop_reason" not in r["out"]
    o = r["out"]
    d1 = ex_day(o, 1)
    assert d1["held_exposure"] == "1.4" and d1["W"] is None
    assert d1["stop_check"] == "无法确定" and d1["determined"] is False and "next_target" not in d1
    assert d1["execution_computable"] is False
    assert o["exec_sim"]["undetermined_from"] == "D001"
    assert ex_day(o, 2)["determined"] is False and "held_exposure" not in ex_day(o, 2)
    assert ex_day(o, 2)["signal_target"] == "1.4"
    assert o["exec_sim"]["failed"] == {"type": "缺价", "missing": [["SPX", "D001"]]}
    assert o["signal_sim"]["failed"]["type"] == "缺价" and o["signal_sim"]["nav"] is None


def test_B_重入执行日缺价(out):
    # 修订二第四节第 6 条：m = r = D032，前一区间持现金。预期：止损 20、离场 21、重入信号 31、重入执行 32 保留；
    # 重入目标保留（D031 的次日目标 0.6，来源“重入”；D032 held_exposure = 0.6）；D032 净值不可得（W = null），
    # 需要新一轮基准的止损判断“无法确定”，自 D033 的政策目标起无法确定；W_31 = 0.958 已知。
    o = out["B_重入执行日缺价"]["out"]
    assert ev(o) == [*STOP_EVENTS, ("路径无法确定", "D032")]
    assert ex_day(o, 31)["next_target"]["exposure"] == "0.6" and ex_day(o, 31)["next_target"]["source"] == "重入"
    assert ex_day(o, 31)["W"] == pytest.approx(0.958, abs=1e-12)
    d32 = ex_day(o, 32)
    assert d32["held_exposure"] == "0.6" and d32["W"] is None and d32["stop_check"] == "无法确定"
    assert d32["execution_computable"] is False
    assert o["exec_sim"]["undetermined_from"] == "D032" and ex_day(o, 33)["determined"] is False


def test_C1_冷却期缺价后重入(out):
    # 修订二第四节第 2、4、6 条：m = D025（冷却期，持现金）。预期：W 自 D025 起为 null（不因持现金或价格恢复而恢复，
    # 覆盖项 4）；冷却 22—31、重入信号 31、重入执行 32 保留；D025 不需要成交（现金到现金），不标 execution_computable；
    # 重入执行日 W 不可得，自 r+1 = D033 的政策目标起无法确定。
    o = out["C1_冷却期缺价后重入"]["out"]
    assert ev(o) == [*STOP_EVENTS, ("路径无法确定", "D032")]
    assert ex_day(o, 24)["W"] == pytest.approx(0.958, abs=1e-12)
    for i in range(25, 33):
        assert ex_day(o, i)["W"] is None, i
    assert [ex_day(o, i)["cooldown"] for i in range(22, 32)] == [True] * 10
    assert "execution_computable" not in ex_day(o, 25)
    assert ex_day(o, 32)["held_exposure"] == "0.6" and ex_day(o, 32)["stop_check"] == "无法确定"
    assert o["exec_sim"]["undetermined_from"] == "D032"


def test_C2_止损执行日缺价后重入(out):
    # 覆盖项 1 的 C2 与覆盖项 2：止损于 D020 确认（缺价前），执行日 D021 的 SPX 缺价；D021 收盘离场后持现金，
    # 而最后一个持仓区间（D020→D021）已使 W 未知。预期：止损确认与离场保留（D021 held_exposure = 0，
    # execution_computable = false），冷却与重入目标保留；不得用本轮相对净值判断新的止损（输出中没有 round_W），
    # 自 r+1 = D033 起无法确定。
    o = out["C2_止损执行日缺价后重入"]["out"]
    assert ev(o) == [*STOP_EVENTS, ("路径无法确定", "D032")]
    d21 = ex_day(o, 21)
    assert d21["held_exposure"] == "0" and d21["W"] is None and d21["execution_computable"] is False
    assert all("round_W" not in r for r in o["exec_sim"]["days"])
    assert o["exec_sim"]["undetermined_from"] == "D032"


def test_缺价后保持现金_窗口内没有重入(out):
    # 覆盖项 3：m = D025（现金），S 自 D025 起为一级，冷却期满后也不重入。预期：全部记录可唯一确定
    # （undetermined_from = null），D025 至 D044 的次日目标都为现金 0；没有重入事件；
    # W 自 D025 起为 null；净值“无法计算”。
    o = out["缺价后保持现金"]["out"]
    assert ev(o) == [("止损确认", "D020"), ("止损执行", "D021")]
    assert o["exec_sim"]["undetermined_from"] is None
    assert all(r["determined"] for r in o["exec_sim"]["days"])
    assert all(ex_day(o, i)["next_target"]["exposure"] == "0" for i in range(25, 45))
    assert all(ex_day(o, i)["W"] is None for i in range(25, 45))
    assert o["exec_sim"]["summary"] is None and o["exec_sim"]["failed"]["type"] == "缺价"


def test_价格完整时结果不变(out):
    # 覆盖项 5：价格完整的同一路径。预期与登记第一节第 6 小节情形甲相同：止损 20、离场 21、重入 31/32，
    # 第 32 日信号正常且全部有效 → 上限解除；W_末 = 0.958；对账通过；没有“无法确定”；退出码 0。
    r = out["价格完整_止损与重入"]
    o = r["out"]
    assert r["code"] == 0
    assert ev(o) == [*STOP_EVENTS, ("上限解除", "D032")]
    assert o["exec_sim"]["undetermined_from"] is None and o["exec_sim"]["failed"] is None
    s = o["exec_sim"]["summary"]
    assert s["stops"] == 1 and s["reentries"] == 1 and s["recon_ok"] is True
    assert s["W_end"] == pytest.approx(0.958, abs=1e-12)
    assert ex_day(o, 32)["next_target"]["exposure"] == "1.4"


# ---------------------------------------------------------------- 覆盖项 6：截止日前缀相同、未来记录不同


def strip(o: dict) -> dict:
    return {k: v for k, v in o.items() if k not in ("name", "input_checks")}


@pytest.mark.parametrize("name", ["截止_未来正常", "截止_未来缺值", "截止_未来非法", "截止_未来长度不同"])
def test_截止日之后的内容不影响历史结果(out, name):
    # 修订二第一节第 1—5 条：只验证、只使用截止日以内的前缀（D000—D249）。预期：与没有未来记录的基准逐字段相同，
    # 退出码同为 0；未来内容只进 post_cutoff 诊断字段，标注“不影响历史计算”，内容记为“未验证”。
    base, r = out["截止_基准无未来"], out[name]
    assert r["code"] == base["code"] == 0
    assert strip(r["out"]) == strip(base["out"])
    pc = r["out"]["input_checks"]["post_cutoff"]
    assert pc["note"] == "不影响历史计算" and pc["status"] == "未验证"


def test_截止日之后的行数只作诊断(out):
    # 截止_未来长度不同：轴有 270 行（截止日之后 20 行），SPX 价格 253 个（之后 3 个），QQQ 265 个（之后 15 个）。
    pc = out["截止_未来长度不同"]["out"]["input_checks"]["post_cutoff"]
    assert pc["axis_rows_after_cutoff"] == 20
    assert pc["price_rows_after_cutoff"] == {"SPX": 3, "QQQ": 15}


def test_截止日之后的非法内容不改变出口与退出码(out):
    # 修订二第一节第 2 条：full 场景截止日之后有重复日期、非字符串日期与非法价格，出口、原因与退出码与基准相同。
    base, r = out["截止_full_基准"], out["截止_full_未来非法"]
    assert r["code"] == base["code"]
    assert strip(r["out"]) == strip(base["out"])
    assert r["out"]["selection"]["exit"] == base["out"]["selection"]["exit"]


# ---------------------------------------------------------------- 覆盖项 7：整行缺失


def test_整行缺失_派生轴标记缺价(out):
    # 修订二第二节：原始轴缺下标 230 的 NYSE 交易日（t0 = 第 199 日仍存在）。预期：原始轴 259 日、派生轴 260 日
    # 分别输出；added_dates 列出该日、资产 SPX 与 QQQ、原因“预期交易日整行缺失”；派生输入层该日两资产都缺价
    # （不补价、不压缩窗口）；退出码 0。
    r = out["整行缺失"]
    ses = nyse_sessions("2004-01-02", 260)
    ic = r["out"]["input_checks"]
    assert r["code"] == 0
    assert len(ic["raw_axis"]) == 259 and ic["derived_axis"] == ses
    assert ic["added_dates"] == [{"date": ses[230], "assets": ["SPX", "QQQ"], "reason": "预期交易日整行缺失"}]
    for a in ("SPX", "QQQ"):
        assert r["out"]["inputs"][a][230]["day"] == ses[230] and r["out"]["inputs"][a][230]["has_close"] is False


@pytest.mark.parametrize("name", ["整行缺失_含多余日期", "整行缺失_含重复日期"])
def test_整行缺失与非法日期同时存在时输入校验失败(out, name):
    # 修订二第二节第 2 条最后一项：同时存在多余日期（星期六 2004-01-03）或重复日期时按“输入校验失败”停止，
    # 不得用缺失标记掩盖错误；退出码 3。
    r = out[name]
    assert r["code"] == 3 and r["out"]["stop_reason"]["reason"] == "输入校验失败"


# ---------------------------------------------------------------- 覆盖项 8：空窗口、未收敛、t0 不存在


def test_空窗口两种细分码(out):
    # 修订二第五节。平稳路径（SPX 100、QQQ 50），参数 K = 5、θ_P = 2%、h = 3：
    # t0 = D199（200 日均线首次完整）；通道 t0+2 = D201 收敛，系统 κ通道 + h + 1 = D205 收敛，主参照 D201 收敛；
    # j0 = max(t0 + 63, κ全 + 1) = 262。263 日（E = 262）：起点等于最后一个收盘日；262 日（E = 261）：起点晚于。
    # 两者 n = 0，原因码“评价窗口为空”，退出码 3。
    for name, sub, e_idx in (
        ("空窗口_起点等于末日", "起点等于最后一个收盘日", 262),
        ("空窗口_起点晚于末日", "起点晚于最后一个收盘日", 261),
    ):
        r = out[name]
        sr = r["out"]["stop_reason"]
        assert r["code"] == 3, name
        assert sr["reason"] == "评价窗口为空" and sr["sub_reason"] == sub and sr["n"] == 0, name
        assert sr["j0_index"] == 262 and sr["E_index"] == e_idx and r["out"]["t0"] == "D199", name


def test_未收敛(out):
    # 201 日平稳路径：t0 = D199，E = D200。P、PR 从三种初始状态出发，D200 时为（未武装、已武装、已武装），
    # 需要 D201 才相同，超出数据；主参照三种运行 D200 时为（一级、正常、正常）。
    # 预期：原因“未收敛”（不是空窗口），退出码 3。
    r = out["未收敛"]
    sr = r["out"]["stop_reason"]
    assert r["code"] == 3 and sr["reason"] == "未收敛"
    assert "主参照" in sr["objects"] and any("（通道）" in x for x in sr["objects"])


def test_t0不存在(out):
    # 150 日：200 日均线从不完整，五个通道从未同时有效。修订二第六节 Q16：独立停止原因“t0 不存在”，退出码 3。
    r = out["t0不存在"]
    assert r["code"] == 3 and r["out"]["stop_reason"]["reason"] == "t0 不存在"


# ---------------------------------------------------------------- 覆盖项 9：杠杆因子、非有限值、对账失败


def test_杠杆收益因子非法(out):
    # 第 5 日两资产 100 → 40：U_5 = −60%，杠杆暴露为正（正常），1 + 2U = −0.2 ≤ 0。
    # 预期：信号模拟、执行政策模拟、一直持有都报“杠杆因子不为正”（D005）；原因“计算失败”，退出码 3。
    r = out["杠杆因子非法"]
    o = r["out"]
    assert r["code"] == 3 and o["stop_reason"]["reason"] == "计算失败"
    for k in ("signal_sim", "exec_sim", "hold"):
        assert o[k]["failed"] == {"type": "杠杆因子不为正", "idx": "D005"}, k


def test_非有限值(out):
    # U_2 = U_3 = 1e308：R_2 = 1.4e308（有限），W_2 = 1.4e308；W_3 = W_2 × (1 + 1.4e308) 溢出为无穷。
    # 预期：三个对象都报“非有限值”（D003）；原因“计算失败”，退出码 3。
    r = out["非有限值"]
    o = r["out"]
    assert r["code"] == 3 and o["stop_reason"]["reason"] == "计算失败"
    for k in ("signal_sim", "exec_sim", "hold"):
        assert o[k]["failed"] == {"type": "非有限值", "idx": "D003"}, k


FIXTURE = r'''
import importlib.util, math, sys
spec = importlib.util.spec_from_file_location("audit_v20_fixture", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mode = sys.argv[2]
if mode == "recon":
    class Biased:
        """测试夹具：只给 log1p 加 1e-6 的偏差，使对账不符；其余数学函数照旧。"""
        def __getattr__(self, k):
            return getattr(math, k)
        @staticmethod
        def log1p(x):
            return math.log1p(x) + 1e-6
    mod.math = Biased()
elif mode == "unexpected":
    def boom(*a, **k):
        raise RuntimeError("构造的内部故障")
    mod.asset_inputs = boom
sys.exit(mod.main(["audit_v20.py", sys.argv[3], sys.argv[4]]))
'''


def run_fixture(tmp_path: Path, mode: str, sc: dict) -> tuple[int, dict]:
    runner = tmp_path / "fixture_runner.py"
    runner.write_text(FIXTURE, encoding="utf-8")
    p, o = tmp_path / "sc.json", tmp_path / "out.json"
    p.write_text(json.dumps(sc, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(runner), str(TOOL), mode, str(p), str(o)], capture_output=True, text=True
    )
    return proc.returncode, json.loads(o.read_text(encoding="utf-8"))


def test_对账失败(tmp_path):
    # 原条文第一节第 3 条：|fsum(log1p R) − ln W| > 1e-10 即对账不符；修订二第三节：计算失败整体停止，退出码 3。
    # 夹具只把 log1p 加 1e-6（价格完整的止损重入路径，44 个区间，偏差 4.4e-5）。预期：信号模拟、执行政策模拟、
    # 一直持有都报“对账不符”；原因“计算失败”，退出码 3。
    code, o = run_fixture(tmp_path, "recon", build_scenarios()["价格完整_止损与重入"])
    assert code == 3 and o["stop_reason"]["reason"] == "计算失败"
    for k in ("signal_sim", "exec_sim", "hold"):
        assert o[k]["failed"] == {"type": "对账不符"}, k


# ---------------------------------------------------------------- 覆盖项 10：已捕获的未预期异常


def test_未预期异常保留类型信息与调用栈(tmp_path):
    # 修订二第三节：已捕获的未预期异常保留异常类型、原始信息与调用栈，不伪装成缺值；退出码 3。
    code, o = run_fixture(tmp_path, "unexpected", build_scenarios()["单日缺价窗口边界"])
    sr = o["stop_reason"]
    assert code == 3 and sr["reason"] == "未预期异常"
    assert sr["exception_type"] == "RuntimeError" and sr["message"] == "构造的内部故障"
    assert "Traceback" in sr["traceback"] and "run_signal" in sr["traceback"]


# ---------------------------------------------------------------- 覆盖项 11：提示段起始状态无法确定；确认性检验计算无效


def test_提示段起始状态无法确定(out):
    # 补充第 14 条：窗口内 D060 状态为 null（无法确定），D061 起提示，提示段起始日的前一信号日无法确定 → 停下报告。
    # 预期：原因“提示段起始状态无法确定”，退出码 3。
    r = out["提示段起始状态无法确定"]
    assert r["code"] == 3 and r["out"]["stop_reason"]["reason"] == "提示段起始状态无法确定"


def test_确认性检验缺价时计算无效(out):
    # 登记第七节第 5 小节与原条文第五节：窗口（D300 收盘起）内 D320 的 SPX 缺价 → 净值无法计算 → “计算无效”；
    # SPX 的 R2 标签因缺价无法生成；平稳路径没有回调事件，QQQ 的 R2 分母为 0。预期：valid = false、类别“计算无效”，
    # 不是停止报告，退出码 0。
    r = out["确认性检验_缺价计算无效"]
    o = r["out"]
    assert r["code"] == 0 and "stop_reason" not in o
    assert o["valid"] is False and o["category"] == "计算无效"
    reasons = o["invalid_reasons"]
    assert {"reason": "缺少必需价格", "object": "净值", "missing": [["SPX", "D320"]]} in reasons
    assert any(x.get("object") == "R2 SPX" and x.get("reason") == "缺少必需价格" for x in reasons)
    assert {"reason": "R2 无法计算（非左截断事件为 0 个）", "object": "R2 QQQ"} in reasons


# ---------------------------------------------------------------- 覆盖项 12：命令行退出码


def test_退出码0与3(out):
    # 修订二第三节：正常完成为 0（含“缺值无法评价”），结构化停止报告为 3。
    assert out["截止_full_基准"]["code"] == 0
    assert out["截止_full_基准"]["out"]["selection"]["exit"] == "缺值无法评价"
    assert out["t0不存在"]["code"] == 3


def test_退出码2_参数错误(tmp_path):
    proc = subprocess.run([sys.executable, str(TOOL), "只有一个参数"], capture_output=True, text=True)
    assert proc.returncode == 2


def test_退出码1_无法写出报告(tmp_path):
    # 修订二第三节：未能写出有效报告的未捕获故障为 1。输出路径所在目录不存在，写文件失败。
    p = tmp_path / "sc.json"
    p.write_text(json.dumps(build_scenarios()["t0不存在"], ensure_ascii=False), encoding="utf-8")
    o = tmp_path / "不存在的目录" / "out.json"
    proc = subprocess.run(
        [sys.executable, str(TOOL), str(p), str(o)], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert proc.returncode == 1 and not o.exists() and "Traceback" in proc.stderr


# ---------------------------------------------------------------- 覆盖项 13（修订二第六节 Q11）：单个缺失日的窗口边界


def test_单个缺失日进入停留离开63日与200日窗口(out):
    # SPX 第 250 日缺价（平稳路径）。63 日窗口：第 249 日完整，第 250—312 日不完整（缺价日在窗口内），第 313 日完整；
    # 200 日窗口：第 249 日完整，第 250—449 日不完整，第 450 日完整。缺价当日 NL 未知、Q = 0。
    i = out["单日缺价窗口边界"]["out"]["inputs"]
    h = [i["SPX"][d]["h_complete"] for d in (249, 250, 312, 313)]
    m = [i["SPX_MA"][d]["ma_complete"] for d in (249, 250, 449, 450)]
    assert h == [True, False, False, True]
    assert m == [True, False, False, True]
    assert i["SPX"][250]["nl"] == "未知" and i["SPX"][250]["q"] == 0
