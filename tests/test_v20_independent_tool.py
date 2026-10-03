"""波段预警 v2.0 独立复核工具：人工例子测试（阶段三实施指令第五节第 6 部分）。

只在子进程中运行 docs/audit/独立复核/v20/ 下的场景生成脚本与复核工具，不导入任何项目代码。
构造数据与输出都写在 pytest 的临时目录。期望值全部来自登记或产品规格中写明的例子，出处写在各测试的注释中：
- “登记”= docs/research/波段预警研究规格_v2.0_登记.md；
- “规格”= docs/product/产品规格_修订一_第40版.md。
例子中没有写明、为构造价格路径而补充的数值（如例 1 的 QQQ 路径、h 的取值），只用于让例子成立，不作为期望值。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "docs" / "audit" / "独立复核" / "v20"
GEN = TOOL_DIR / "gen_scenarios_v20.py"
TOOL = TOOL_DIR / "audit_v20.py"

ACTIVE, ARMED, UNARMED = "激活", "已武装未激活", "未武装未激活"
NORMAL, LV1, LV2 = "正常", "一级", "二级"


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    base = tmp_path_factory.mktemp("v20_independent_tool")
    sc_dir = base / "scenarios"
    out_dir = base / "out"
    out_dir.mkdir()
    subprocess.run([sys.executable, str(GEN), str(sc_dir), "manual"], check=True, cwd=base)
    results = {}
    for f in sorted(sc_dir.glob("*.json")):
        if f.name == "manifest.json":
            continue
        o = out_dir / f.name
        proc = subprocess.run([sys.executable, str(TOOL), str(f), str(o)], cwd=base)
        assert proc.returncode == 0, f.name
        results[f.stem] = json.loads(o.read_text(encoding="utf-8"))
    return results


def day(res, idx):
    """signal 类场景：唯一一组参数的第 idx 日状态记录。"""
    (g,) = res["groups"].values()
    return next(r for r in g["days"] if r["idx"] == idx)


def inp(res, asset, idx):
    return res["inputs"][asset][idx]


def exec_day(res, idx):
    return next(r for r in res["exec_sim"]["days"] if r["idx"] == f"D{idx:03d}")


def next_target(res, idx):
    return exec_day(res, idx)["next_target"]["exposure"]


# -------------------------------- 登记第一节第 4 小节：跨级条件的构造例子（θ_P = 2%，K = 5）


def test_例1_单日急跌直接进入二级(out):
    # 登记第一节第 4 小节例 1：d−1 日正常、P、PR 已武装未激活、D_SPX = 1.5%；
    # d 日 D_SPX = 4.3%，P_SPX、PR_SPX 同日进入，L = 2，正常 → 二级，“是”由正常直接进入二级。
    r = out["例1_单日急跌"]
    d = 259
    prev, cur = day(r, d - 1), day(r, d)
    assert prev["S"] == NORMAL
    assert prev["ch"]["P_SPX"] == ARMED and prev["ch"]["PR_SPX"] == ARMED
    assert inp(r, "SPX", d - 1)["d"] == "0.015"
    assert inp(r, "SPX", d)["d"] == "0.043"
    assert cur["ch"]["P_SPX"] == ACTIVE and cur["ch"]["PR_SPX"] == ACTIVE
    assert cur["L"] == 2
    assert cur["S"] == LV2
    assert cur["direct_to_2"] is True


def test_例2_新低重新武装并进入(out):
    # 登记第一节第 4 小节例 2：d−1 日正常；P_SPX、PR_SPX 此前因 Q ≥ K 退出而未武装，D_SPX = 4.5%；
    # d 日 NL_SPX = 1：两者重新武装并进入，L = 2，正常 → 二级，“是”。
    r = out["例2_新低重新武装"]
    d = len(r["inputs"]["SPX"]) - 1
    prev, cur = day(r, d - 1), day(r, d)
    assert prev["S"] == NORMAL
    assert prev["ch"]["P_SPX"] == UNARMED and prev["ch"]["PR_SPX"] == UNARMED
    assert inp(r, "SPX", d - 1)["d"] == "0.045"
    assert inp(r, "SPX", d)["nl"] == 1
    assert cur["ch"]["P_SPX"] == ACTIVE and cur["ch"]["PR_SPX"] == ACTIVE
    assert cur["L"] == 2 and cur["S"] == LV2 and cur["direct_to_2"] is True


def test_例3_回看量不完整时由Ĥ进入(out):
    # 登记第一节第 4 小节例 3：QQQ 的 63 日窗口内有一日缺价；PR_QQQ 已武装未激活；
    # Ĥ 算出 D̂_QQQ = 4.1% ≥ 4%：PR_QQQ 进入；L = 2；正常 → 二级，“是”。
    r = out["例3_回看量不完整时进入"]
    d = len(r["inputs"]["QQQ"]) - 1
    prev, cur = day(r, d - 1), day(r, d)
    q = inp(r, "QQQ", d)
    assert q["h_complete"] is False
    assert q["d"] == "0.041"
    assert prev["S"] == NORMAL and prev["ch"]["PR_QQQ"] == ARMED
    assert cur["ch"]["PR_QQQ"] == ACTIVE
    assert cur["L"] == 2 and cur["S"] == LV2 and cur["direct_to_2"] is True


def test_例4_经一级再入二级不算直接进入(out):
    # 登记第一节第 4 小节例 4：d−1 日一级（P_SPX 激活）；d 日 PR_SPX 进入，L = 2；一级 → 二级；“否”。
    r = out["例4_经一级"]
    d = len(r["inputs"]["SPX"]) - 1
    prev, cur = day(r, d - 1), day(r, d)
    assert prev["S"] == LV1 and prev["ch"]["P_SPX"] == ACTIVE
    assert cur["ch"]["PR_SPX"] == ACTIVE
    assert cur["L"] == 2 and cur["S"] == LV2
    assert cur["direct_to_2"] is False


# -------------------------------- 登记第一节第 5 小节：恢复确认的构造例子（K = 5，h = 3）


def test_例A_QQQ窗口内缺价时A2为假(out):
    # 登记第一节第 5 小节例 A：系统在二级；QQQ 第 d−30 日缺价（在 63 日窗口内、已离开 20 日窗口）；
    # 两资产 PR 未激活，Q_SPX = Q_QQQ = 8 → A₂ 假（H_QQQ 不完整，PR_QQQ 无效），A₁ 假，c₂ = 0，停在二级；
    # “缺价日离开 63 日窗口后才可能开始计数”：第 d+33 日窗口为 [d−29, d+33]，缺价日离开窗口。
    r = out["例A_二级QQQ窗口内缺价"]
    d = 280
    cur = day(r, d)
    assert inp(r, "SPX", d)["q"] == 8 and inp(r, "QQQ", d)["q"] == 8
    assert cur["ch"]["PR_SPX"] != ACTIVE and cur["ch"]["PR_QQQ"] != ACTIVE
    assert cur["items"][1] is False  # 第 2 项：H_SPX、H_QQQ 都完整
    assert cur["valid"]["PR_QQQ"] is False
    assert cur["A2"] is False and cur["A1"] is False
    assert cur["c2"] == 0 and cur["S"] == LV2
    for i in range(d, d + 33):
        assert day(r, i)["c2"] == 0, i
    assert inp(r, "QQQ", d + 33)["h_complete"] is True
    assert day(r, d + 33)["A2"] is True and day(r, d + 33)["c2"] == 1


def test_例B_SPX只在200日窗口内缺价时降到一级(out):
    # 登记第一节第 5 小节例 B：系统在二级；SPX 第 d−150 日缺价（只在 200 日窗口内）；H 都完整；
    # 两资产 PR 未激活、Q ≥ K，已连续 3 日如此 → A₂ 真，A₁ 假（MA 不完整，MR 无效）；c₂ = 3 ≥ h，降到一级；c₁ = 0。
    r = out["例B_二级SPX200日窗口内缺价"]
    d = 280
    cur = day(r, d)
    assert inp(r, "SPX", d)["h_complete"] is True and inp(r, "QQQ", d)["h_complete"] is True
    assert r["inputs"]["SPX_MA"][d]["ma_complete"] is False
    assert [day(r, i)["A2"] for i in (d - 2, d - 1, d)] == [True, True, True]
    assert cur["A1"] is False and cur["valid"]["MR"] is False
    assert cur["c2"] == 3 and cur["c1"] == 0
    assert day(r, d - 1)["S"] == LV2 and cur["S"] == LV1


def test_例C_Q不足时A2为假(out):
    # 登记第一节第 5 小节例 C：系统在二级；数据完整；PR_QQQ 未激活但 Q_QQQ = 4 → A₂ 假（第 4 项），A₁ 假，c₂ = 0。
    r = out["例C_Q不足"]
    d = 280
    cur = day(r, d)
    assert inp(r, "QQQ", d)["q"] == 4
    assert cur["ch"]["PR_QQQ"] != ACTIVE
    assert cur["items"][3] is False
    assert cur["A2"] is False and cur["A1"] is False
    assert cur["c2"] == 0 and cur["S"] == LV2


# -------------------------------- 登记第八节第 2 小节：与旧入口不同的构造例子（K = 5，θ_P = 2.5%）


def test_例E_通道无效时不退出(out):
    # 登记第八节第 2 小节例 E：P_SPX 前一日激活；SPX 第 d−40 日缺价；Q_SPX,d = 6 →
    # v2.0：P_SPX 当日无效，不退出、保持激活；A₁ 为假。
    r = out["例E_无效不退出"]
    d = 280
    cur = day(r, d)
    assert inp(r, "SPX", d)["q"] == 6
    assert cur["valid"]["P_SPX"] is False
    assert cur["ch"]["P_SPX"] == ACTIVE
    assert cur["A1"] is False


def test_例F_不完整回看量可以进入(out):
    # 登记第八节第 2 小节例 F：同一缺价；P_SPX 已武装未激活；Ĥ = 100，C_SPX,d = 97.4 →
    # D̂ = 2.6% ≥ 2.5%，P_SPX 进入。
    r = out["例F_不完整回看量进入"]
    d = 280
    s = inp(r, "SPX", d)
    assert s["h_complete"] is False and s["h"] == "100.00" and s["close"] == "97.40"
    assert s["d"] == "0.026"
    assert s["reach"]["0.025"][0] is True
    assert day(r, d - 1)["ch"]["P_SPX"] == ARMED
    assert day(r, d)["ch"]["P_SPX"] == ACTIVE


def test_例G_通道无效时不以D低于门槛武装(out):
    # 登记第八节第 2 小节例 G：同一缺价；P_SPX 未武装；NL_SPX,d = 0；D̂ = 1.0% →
    # 通道无效，不能以“D < 门槛”武装，保持未武装。
    r = out["例G_无效不武装"]
    d = 280
    s = inp(r, "SPX", d)
    assert s["nl"] == 0 and s["d"] == "0.01" and s["h_complete"] is False
    assert day(r, d - 1)["ch"]["P_SPX"] == UNARMED
    assert day(r, d)["ch"]["P_SPX"] == UNARMED


def test_MR构造例子(out):
    # 登记第八节第 2 小节 MR 的构造例子：
    #   t0+1：MA 不完整 —— 从“激活”出发保持激活；从“已武装未激活”出发保持已武装未激活；不相同；
    #   t0+2：MA 完整，C ≥ MA —— 激活退出为已武装未激活；另一者保持；相同（此后永久相同）；
    #   t0+3：MA 完整，C < MA —— 进入 → 激活。
    # 按登记定义 t0 当日均线完整，t0+1 不可能不完整，所以场景把三日平移到一次缺价离开 200 日窗口的前后
    # （第 209、210、211 日，枚举起点第 208 日）；规则本身不变（见 README 疑问 Q1）。
    r = out["MR例子"]
    (g,) = r["groups"].values()
    mr = g["channel_conv"]["MR"]
    ma = r["inputs"]["SPX_MA"]
    assert ma[209]["ma_complete"] is False
    assert ma[210]["ma_complete"] is True and ma[210]["below_ma"] is False
    assert ma[211]["ma_complete"] is True and ma[211]["below_ma"] is True
    assert mr["runs"][ACTIVE] == [ACTIVE, ACTIVE, ARMED]
    assert mr["runs"][ARMED] == [ARMED, ARMED, ARMED]
    assert mr["conv_day"] == "D210"
    assert day(r, 211)["ch"]["MR"] == ACTIVE


def test_h3构造边界与例乙的有效性窗口(out):
    # 登记第八节第 2 小节代价说明的构造边界：第 32 日缺价始终未补齐、h = 3、A₁ 自第 232 日起每天为真，
    # 则 c₁ 在第 232、233、234 日依次为 1、2、3，S 最早在第 234 日降为正常。
    # 登记第一节第 6 小节例乙：P_SPX、PR_SPX 自第 95 日起 63 日窗口恢复完整；MR 自第 232 日起 200 日窗口恢复完整。
    r = out["h3边界_第32日缺价"]
    assert inp(r, "SPX", 94)["h_complete"] is False and inp(r, "SPX", 95)["h_complete"] is True
    assert r["inputs"]["SPX_MA"][231]["ma_complete"] is False and r["inputs"]["SPX_MA"][232]["ma_complete"] is True
    assert day(r, 231)["A1"] is False and day(r, 231)["c1"] == 0
    assert [day(r, i)["A1"] for i in (232, 233, 234)] == [True, True, True]
    assert [day(r, i)["c1"] for i in (232, 233, 234)] == [1, 2, 3]
    assert [day(r, i)["S"] for i in (232, 233, 234)] == [LV1, LV1, NORMAL]


# -------------------------------- 登记第一节第 6 小节：重入上限的构造例子（s = 21，冷却期 22—31）


def test_甲_重入与上限解除(out):
    # 登记第一节第 6 小节情形甲（规格第八节例子二）：第 20 日止损确认（收盘净值为基准的 95.8%），
    # 第 21 日（s）全部离场；22—31 冷却期；31：正常、全部有效 → 设 U = 一级，32：0.6（重入，核心），新一轮基准重置；
    # 32：正常、全部有效 → U → 无，33：1.4。
    r = out["甲_重入上限"]
    ev = r["exec_sim"]["events"]
    assert {"type": "止损确认", "idx": "D020"} in ev
    assert {"type": "止损执行", "idx": "D021"} in ev
    assert {"type": "上限设置", "idx": "D031"} in ev
    assert {"type": "重入执行", "idx": "D032"} in ev
    assert {"type": "上限解除", "idx": "D032"} in ev
    assert exec_day(r, 20)["W"] == pytest.approx(0.958, abs=1e-12)
    assert exec_day(r, 21)["held_exposure"] == "0"
    assert [exec_day(r, i)["cooldown"] for i in range(22, 32)] == [True] * 10
    assert exec_day(r, 32)["mode"] == "持仓"  # 已重入
    assert next_target(r, 31) == "0.6" and exec_day(r, 31)["next_target"]["source"] == "重入"
    assert exec_day(r, 32)["held_exposure"] == "0.6"
    assert exec_day(r, 32)["base"] == exec_day(r, 32)["W"]
    assert next_target(r, 32) == "1.4"
    assert exec_day(r, 33)["held_exposure"] == "1.4"


def test_乙_第32日缺口未补齐时上限不解除(out):
    # 登记第一节第 6 小节情形乙：31：设 U = 一级 → 32：0.6；32：S 正常但 P_SPX、PR_SPX、MR 无效 → 不变 → 33：0.6；
    # 33：仍无效 → 34：0.6；第一个 S = 正常且五个通道全部有效的信号日 d（d ≥ 232）：U → 无，
    # d+1：1.4（最早为第 233 日）。
    r = out["乙_第32日缺口未补齐"]
    assert next_target(r, 31) == "0.6"
    assert next_target(r, 32) == "0.6" and exec_day(r, 32)["cap"] == LV1
    assert next_target(r, 33) == "0.6" and exec_day(r, 33)["cap"] == LV1
    for i in range(34, 232):
        assert next_target(r, i) == "0.6", i
    assert {"type": "上限解除", "idx": "D232"} in r["exec_sim"]["events"]
    assert next_target(r, 232) == "1.4"


def test_丙_重入后再升级(out):
    # 登记第一节第 6 小节情形丙：31：设 U = 一级 → 32：0.6；32：二级 → 33：0.3；33：一级 → 34：0.6；
    # 34：正常、全部有效 → U → 无 → 35：1.4。
    r = out["丙_重入后再升级"]
    assert [next_target(r, i) for i in (31, 32, 33, 34)] == ["0.6", "0.3", "0.6", "1.4"]
    assert {"type": "上限解除", "idx": "D034"} in r["exec_sim"]["events"]


def test_规格例子二_第31日信号为一级时继续现金(out):
    # 规格第八节例子二末句：若第 31 日信号为一级，则继续现金；
    # 第一个信号为正常、输入完整的信号日为 k 时，于第 k+1 日重入。
    # 场景中第 32 日为第一个正常且输入完整的信号日，所以第 33 日重入。
    r = out["规格例子二_第31日一级"]
    assert next_target(r, 31) == "0"
    assert {"type": "重入执行", "idx": "D033"} in r["exec_sim"]["events"]
    assert exec_day(r, 33)["held_exposure"] == "0.6"


# -------------------------------- 规格第八节例子一：信号与执行的时点


def test_规格例子一_执行时点与逐级恢复(out):
    # 规格第八节例子一：第 1 至 6 日收盘后的暴露为 1.4、0.6、0.3、0.3、0.6、1.4。
    # 按登记第一节第 6 小节“只有一层阶段恢复”，阶段恢复由 S 实现：规格中第 4 日的“正常”信号在二级之后使 S = 一级，
    # 所以场景的 S 序列为 正常、一级、二级、二级、一级、正常、正常（第 0 至 6 日）。
    r = out["规格例子一"]
    plan = {p["exec_idx"]: p["exposure"] for p in r["signal_sim"]["plan"]}
    assert [plan[f"D{i:03d}"] for i in range(1, 7)] == ["1.4", "0.6", "0.3", "0.3", "0.6", "1.4"]
    assert [exec_day(r, i)["held_exposure"] for i in range(1, 7)] == ["1.4", "0.6", "0.3", "0.3", "0.6", "1.4"]
    # 例子一末句：若第 5 日的信号为一级，第 6 日收盘执行的目标为一级，暴露保持 0.6。
    r2 = out["规格例子一_第5日一级"]
    plan2 = {p["exec_idx"]: p["exposure"] for p in r2["signal_sim"]["plan"]}
    assert plan2["D006"] == "0.6"


# -------------------------------- 规格第八节例子三与复核者例子：R2


def test_规格例子三_事件日期(out):
    # 规格第八节例子三：P = 100（收盘 100.0），第 103 日第一次不高于 97.0，T3 = 103；
    # 第 105 日第一次不高于 95.0，T5 = 105。
    (ev,) = out["例子三_102开始"]["events"]["SPX"]
    assert ev["P"] == "D100" and ev["C_P"] == "100.00"
    assert ev["T3"] == "D103" and ev["T5"] == "D105"


def test_规格例子三_提示在102开始为新提示达标(out):
    # 规格第八节例子三：提示在信号日 102 开始：新提示达标（102 < 103）；可执行日为 103。
    (row,) = out["例子三_102开始"]["judge"]["SPX"]["events"]
    assert row["category"] == "新提示达标"
    assert row["first_new"] == "D102"
    assert row["exec_idx"] == "D103"


def test_规格例子三_提示在103开始为迟到(out):
    # 规格第八节例子三：提示在信号日 103 开始：迟到。
    (row,) = out["例子三_103开始"]["judge"]["SPX"]["events"]
    assert row["category"] == "迟到"


def test_规格例子三_提示自97持续到102为持续覆盖达标(out):
    # 规格第八节例子三：提示自信号日 97 起持续亮到 102：持续覆盖达标。
    (row,) = out["例子三_97至102"]["judge"]["SPX"]["events"]
    assert row["category"] == "持续覆盖达标"


def test_复核者例子_T3后才开始的提示为迟到(out):
    # 规格第八节例子三，复核者的例子：第 101 日收盘 96.0（回撤 4%），则 T3 = 101；
    # 之后反弹到 98.0、在信号日 102 才开始提示，仍判为迟到。
    r = out["复核者例子"]
    (ev,) = r["events"]["SPX"]
    assert ev["T3"] == "D101"
    (row,) = r["judge"]["SPX"]["events"]
    assert row["category"] == "迟到"


# -------------------------------- 登记第八节第 4 小节：净值例子


def test_净值例子_每日两倍产品(out):
    # 登记第八节第 4 小节：指数 100 → 110 → 100，两日净收益为 0；
    # 每日 2 倍产品为 1.2 × (1 − 0.181818) − 1 = −1.818%，而“2 倍 × 跨期收益”得 0。
    r = out["净值例子_100_110_100"]
    rows = r["hold"]["nav"]
    f1, f2 = rows[1]["lev_factor"], rows[2]["lev_factor"]
    assert f1 == pytest.approx(1.2, abs=1e-12)
    assert f2 == pytest.approx(1 - 0.181818, abs=1e-6)
    assert round((f1 * f2 - 1) * 100, 3) == -1.818
    assert r["hold"]["failed"] is None


def test_净值例子_缺价时无法计算(out):
    # 登记第八节第 4 小节：研究计算只用真实逐日价格；仍缺必要价格时，停止受影响窗口的精确净值评价，报告缺失日期，
    # 不用跨期近似冒充每日模拟。
    r = out["净值例子_缺价"]
    for key in ("signal_sim", "exec_sim", "hold"):
        assert r[key]["failed"] == {"type": "缺价", "missing": [["SPX", "D002"]]}, key
        assert r[key]["summary"] is None
