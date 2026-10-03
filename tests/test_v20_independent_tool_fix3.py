"""波段预警 v2.0 独立复核工具：修正三（D13）的构造测试。

依据：《独立工具会话指令：修正三（D13）》（SHA-256 e66b36dde5ba234a38dddaacf80175be209029134d3447dc830739d9a0ca3fa2）
第二、三节；补充第 11 条（c）、第 14 条；登记第五节第 2 小节与补充条文第五节的出口优先级。
只在子进程中运行 docs/audit/独立复核/v20/audit_v20.py，不导入任何项目代码；构造数据与输出写在 pytest 临时目录。
预期值按条文人工推算，写在各测试的注释中。场景由 build_scenarios() 构造。
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
UNAVAILABLE = "无法计算（R2 事件不可得）"


def axis_of(n: int) -> list[str]:
    return [f"D{i:03d}" for i in range(n)]


def r2_prices() -> list[str]:
    """规格第八节例子三的价格：P = 第 100 日（100.0），T3 = 103，T5 = 105，Tr = 106，End = 109。"""
    base = [f"{90 + 0.1 * i:.2f}" for i in range(100)]
    tail = ["100.0", "98.5", "97.6", "96.8", "95.5", "94.9", "94.0", "95.0", "97.0", "99.0"]
    return base + tail + [f"{99.5 + 0.1 * i:.2f}" for i in range(31)]


def r2_sc(s_prev_f: str | None) -> dict:
    """r2 场景：窗口第一个信号日 f = D050，S_f = 一级（提示段从 f 开始），S_{f−1} 由参数给出，其余为正常。
    QQQ 在 D060 缺价，其 R2 事件不可得；SPX 价格完整。"""
    p = r2_prices()
    n = len(p)
    s = [NORMAL] * n
    s[50] = LV1
    s[49] = s_prev_f
    qqq: list[str | None] = list(p)
    qqq[60] = None
    return {"kind": "r2", "axis": axis_of(n), "prices": {"SPX": p, "QQQ": qqq}, "S": s, "f": 50, "E": n - 1}


def build_scenarios() -> dict[str, dict]:
    n = 320
    spx = ["100.00"] * 280 + ["97.00"] * (n - 280)
    qqq: list[str | None] = ["50.00"] * n
    qqq[300] = None
    return {
        "D13_一资产不可得_另一资产f日起段且前日无法确定": r2_sc(None),
        "D13_对照_一资产不可得_另一资产前日已确定": r2_sc(NORMAL),
        "D13_对照_full_一资产不可得": {
            "kind": "full",
            "axis": axis_of(n),
            "prices": {"SPX": spx, "QQQ": qqq},
            "params": [[5, "0.02", 3], [3, "0.02", 1]],
        },
    }


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    base = tmp_path_factory.mktemp("v20_fix3")
    res = {}
    for name, sc in build_scenarios().items():
        p = base / f"{name}.json"
        p.write_text(json.dumps({"name": name, **sc}, ensure_ascii=False), encoding="utf-8")
        o = base / f"{name}.out.json"
        proc = subprocess.run([sys.executable, str(TOOL), str(p), str(o)], capture_output=True, text=True)
        res[name] = {"code": proc.returncode, "out": json.loads(o.read_text(encoding="utf-8"))}
    return res


def test_一资产不可得时另一资产的起始状态无法确定仍停下报告(out):
    # 修正三第二节第 1、2 条与第三节第 1 条：QQQ 的 R2 事件因 D060 缺价不可得，只使 QQQ 记“无法计算”；
    # SPX 照常计算提示段账。SPX 的提示段从 f = D050 开始，S_{f−1} = S_D049 无法确定 → 补充第 11 条（c）：
    # 抛异常、停下报告。预期：停止原因“提示段起始状态无法确定”，退出码 3，不输出提示段账或选择出口。
    # （修正前：任一资产不可得即整体不算段账，ledger 记“无法计算”，退出码 0。）
    r = out["D13_一资产不可得_另一资产f日起段且前日无法确定"]
    assert r["code"] == 3
    assert r["out"]["stop_reason"]["reason"] == "提示段起始状态无法确定"
    assert "ledger" not in r["out"] and "selection" not in r["out"]


def test_对照_前日已确定时另一资产的段账照常计算(out):
    # 修正三第三节第 2 条（r2 场景）：同上但 S_D049 = 正常（已确定）。提示段 D050 单日：不在任何 SPX 事件的
    # [P, Tr) 或 [Tr, End) 内（P = D100），(D050, D070] 内没有 T5（T5 = D105），且 D070 不晚于窗口末日 → 误报。
    # 预期：退出码 0；SPX 段账为误报 1 件、误报比例 [1, 1]；QQQ 的 R2 判定与段账均为“无法计算（R2 事件不可得）”；
    # SPX 的 R2 判定照常计算（P = D100 的事件，[P, T3) 与 [T3, Tr] 内都没有提示 → 漏报）。
    r = out["D13_对照_一资产不可得_另一资产前日已确定"]
    o = r["out"]
    assert r["code"] == 0 and "stop_reason" not in o
    assert o["ledger"]["segments"][0]["start"] == "D050" and o["ledger"]["segments"][0]["pre_window"] is False
    assert o["ledger"]["by_asset"]["SPX"]["counts"]["误报"] == 1
    assert o["ledger"]["by_asset"]["SPX"]["false_alarm_ratio"] == [1, 1]
    assert o["ledger"]["by_asset"]["QQQ"] == UNAVAILABLE
    assert o["judge"]["QQQ"] == "无法计算"
    assert o["judge"]["SPX"]["counts"]["漏报"] == 1


def test_对照_full场景各组均有另一资产的段账与R2判定(out):
    # 修正三第三节第 2 条（full 场景，两组参数 K = 5、θ_P = 2%、h = 3 与 K = 3、θ_P = 2%、h = 1）：
    # 平稳路径；SPX 自 D280 起 100 → 97（D = 3% ≥ θ_P → P_SPX 激活；200·97 < 均线之和 → MR 激活；L = 1，
    # S 由正常转一级，此后 MR 持续激活到窗口末）；QQQ 在 D300 缺价，其 R2 事件不可得。
    # t0 = D199，j0 = D262，f = D261，S_D279 = 正常。SPX 的唯一提示段自 D280 起，没有 SPX 回调事件（跌幅 3% < 5%），
    # D300 不晚于 E = D319 → 误报。预期：每一组的 SPX 段账为误报 1 件，起始 D280；SPX 的 R2 判定照常计算
    # （没有非左截断事件，分母为 0）；QQQ 的 R2 判定与段账为“无法计算（R2 事件不可得）”；
    # 全部组完成且没有停止 → 选择出口“缺值无法评价”，原因含 QQQ 的 R2 缺价与净值缺价；退出码 0。
    r = out["D13_对照_full_一资产不可得"]
    o = r["out"]
    assert r["code"] == 0 and "stop_reason" not in o
    assert o["j0"] == "D262" and o["window_first_signal_day"] == "D261"
    assert set(o["groups"]) == {"K=5,θ=0.02,h=3", "K=3,θ=0.02,h=1"}
    for key, g in o["groups"].items():
        led = g["ledger"]
        assert [s["start"] for s in led["segments"]] == ["D280"], key
        assert led["by_asset"]["SPX"]["counts"]["误报"] == 1, key
        assert led["by_asset"]["QQQ"] == UNAVAILABLE, key
        assert g["r2"]["QQQ"] == UNAVAILABLE, key
        assert g["r2"]["SPX"]["denominator"] == 0, key
    sel = o["selection"]
    assert sel["exit"] == "缺值无法评价"
    objs = {(x["category"], x["object"]) for x in sel["reasons"]}
    assert ("缺少必需价格", "R2 QQQ") in objs and ("缺少必需价格", "净值（全部对象）") in objs
