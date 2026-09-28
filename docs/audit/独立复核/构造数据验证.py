# ruff: noqa
# 构造数据验证（2026-09-28）：在数据集副本上人为制造 H-03、H-04、H-05、H-06、H-09 与"只更新一个序列"的情形，
# 用正式代码（market_risk）跑一段回测，再用独立复核脚本 audit_indep.py（不导入 market_risk）复核，比较两者结果。
# 本脚本需要调用正式代码，因此导入 market_risk；被验证的 audit_indep.py 本身仍不导入。
# 用法：uv run python docs/audit/独立复核/构造数据验证.py <输出文件>
from __future__ import annotations

import csv
import datetime as dt
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from market_risk import services
from market_risk.config import PROJECT_ROOT, load_settings
from market_risk.storage import backtests, db
from market_risk.storage.paths import StoragePaths
from market_risk.storage.runs import GitInfo

D = dt.date
OUT = Path(sys.argv[1])
SCRIPT = PROJECT_ROOT / "docs" / "audit" / "独立复核" / "audit_indep.py"

# 构造的情形（全部在 2018 年，回测区间 2018-01-02 至 2018-09-28）
CASES = {
    "H-03/H-04 QQQ 评分窗口内缺价": ("QQQ", [D(2018, 3, 1)]),          # 影响此后 49 个交易日内的基准日
    "H-03/H-04 RSP 基准日缺价": ("RSP", [D(2018, 6, 15)]),
    "H-04 SPY T−100 附近缺价": ("SPY", [D(2017, 11, 1)]),              # SPY 窗口 T−199，影响 2018 年上半年
    "H-05 S5TW 缺一天（只有 S5FI）": ("S5TW", [D(2018, 2, 6), D(2018, 2, 13)]),
    "H-05 S5FI 缺一天（只有 S5TW）": ("S5FI", [D(2018, 4, 10)]),
    "H-09 SPX 窗口内缺价": ("SPX", [D(2018, 5, 1)]),
}
WEEKEND_OAS = D(2018, 3, 10)          # H-06：周六、非月末的 OAS 观测
SPX_LAST = D(2018, 8, 31)             # "只更新了一个序列"：SPX 截到 08-31，QQQ 完整


def drop_rows(path: Path, dates: list[dt.date]) -> None:
    text = path.read_text(encoding="utf-8").splitlines(keepends=True)
    keep = [line for line in text if not any(line.startswith(d.isoformat() + ",") for d in dates)]
    path.write_text("".join(keep), encoding="utf-8")


def truncate(path: Path, last: dt.date) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(lines[0] + "".join(x for x in lines[1:] if x[:10] <= last.isoformat()), encoding="utf-8")


def add_weekend_oas(path: Path, day: dt.date) -> None:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    prev = max((r for r in rows if r["date"] < day.isoformat()), key=lambda r: r["date"])
    rows.append({"date": day.isoformat(), "value": str(float(prev["value"]) + 0.5), "source": "tradingview"})
    rows.sort(key=lambda r: r["date"])
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "value", "source"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    lines = ["# 构造数据验证：正式代码与独立复核脚本", ""]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(PROJECT_ROOT / "data" / "market", root / "data" / "market")
        (root / "config").mkdir()
        shutil.copyfile(PROJECT_ROOT / "config" / "data_decisions.yaml", root / "config" / "data_decisions.yaml")
        daily = root / "data" / "market" / "daily"
        for name, (sym, dates) in CASES.items():
            drop_rows(daily / f"{sym}.csv", dates)
            lines.append(f"- {name}：删除 {sym} {', '.join(map(str, dates))}")
        add_weekend_oas(daily / "BAMLH0A0HYM2.csv", WEEKEND_OAS)
        lines.append(f"- H-06 非月末周末 OAS：增加 {WEEKEND_OAS}（周六）观测，数值为前一观测 +0.50")
        truncate(daily / "SPX.csv", SPX_LAST)
        lines.append(f"- 只更新了一个序列：SPX 截到 {SPX_LAST}，QQQ 完整")
        paths = StoragePaths(root)
        ctx = services.Context(load_settings(), paths, db.default_url(paths))
        run = services.backtest_run(ctx, D(2018, 1, 2), D(2018, 9, 28), git=GitInfo("c" * 40, False),
                                    now=dt.datetime(2026, 9, 28, 12, tzinfo=dt.UTC))
        backtests.set_official(paths, run.run_id)
        scores = backtests.read_csv(run.run_dir / "daily_scores.csv")
        outcomes = backtests.read_csv(run.run_dir / "outcomes.csv")
        pending_price = sum(1 for r in scores if r["price"].startswith("待补"))
        lines += ["", f"正式代码回测：{run.days} 个交易日；价格维度待补 {pending_price} 行；"
                      f"广度待补 {sum(1 for r in scores if r['breadth'].startswith('待补'))} 行；"
                      f"结果标签 {len(outcomes)} 行，其中数据不齐 {sum(1 for r in outcomes if r['data_note'])} 行；"
                      f"最后一个标签行基准日 {max((r['base_date'] for r in outcomes), default='无')}"]
        out_dir = root / "audit_out"
        res = subprocess.run([sys.executable, str(SCRIPT), str(root), str(out_dir)], capture_output=True, text=True,
                             encoding="utf-8")
        if res.returncode != 0:
            raise SystemExit(res.stderr)
        lines += ["", f"独立复核脚本输出：`{res.stdout.strip()}`（依次为分数不一致、抽样中间值不一致、回调不一致、标签不一致）", ""]
        # 负对照：用口径更新前的脚本（提交 6314c36 的版本）复核同一份构造数据，应报出差异，证明各情形确实被检验到
        old_script = root / "audit_indep_old.py"
        old_script.write_text(subprocess.run(
            ["git", "show", "6314c36:docs/audit/独立复核/audit_indep.py"], cwd=PROJECT_ROOT, capture_output=True,
            text=True, encoding="utf-8", check=True).stdout, encoding="utf-8")
        old_out = root / "audit_out_old"
        old = subprocess.run([sys.executable, str(old_script), str(root), str(old_out)], capture_output=True, text=True,
                             encoding="utf-8")
        lines += ["负对照（口径更新前的脚本 6314c36）：" + ("正常结束" if old.returncode == 0 else
                  "分数比较写出后，在抽样部分因回测区间只有 2018 年而中断（旧脚本固定抽取 2008 年起的日期）"), ""]
        for name in ("score_compare.txt", "label_compare.txt"):
            if (old_out / name).exists():
                body = (old_out / name).read_text(encoding="utf-8").strip().splitlines()
                if name == "score_compare.txt":
                    counts = {dim: sum(1 for x in body if f"'{dim}'" in x)
                              for dim in ("price", "breadth", "vix", "rates", "credit", "total")}
                    lines += ["旧脚本差异按维度计数（旧脚本只列出前 200 条）：" + "，".join(f"{k} {v}" for k, v in counts.items()), ""]
                    lines += ["旧脚本中广度与信用的差异：", "", "```",
                              *[x for x in body if "'breadth'" in x or "'credit'" in x][:20], "```", ""]
                lines += [f"旧脚本 {name}（前 25 行）：", "", "```", *body[:25], "```", ""]
        for name in ("score_compare.txt", "label_compare.txt"):
            lines += [f"## {name}", "", "```", (out_dir / name).read_text(encoding="utf-8").strip(), "```", ""]
        lines += ["## zigzag_compare.md（首行）", "", (out_dir / "zigzag_compare.md").read_text(encoding="utf-8").splitlines()[0],
                  "", "## sample_compare.md（首行）", "", (out_dir / "sample_compare.md").read_text(encoding="utf-8").splitlines()[0]]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(res.stdout.strip())


if __name__ == "__main__":
    main()
