"""外置 v1.2.1 设计样例回归；仅比较合成路径，不评价历史预警效果。"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import os
import re
import runpy
from collections import defaultdict
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.labels_zz import ZZEvent
from market_risk.wavewarn.loss import LossParameters, asset_price_loss

HASHES = {
    "v12_paths.py": "d52ebb00ec5f35b2fbe709de313bf961caccbc812c45a1e9db6c7b7a72ee89c5",
    "v12_daily_detail.csv": "0b3bcf8971f3aca28afb0ecbe039e81c8687adee465dd74d79d173f208e77de2",
    "v12_run_output.txt": "84bf1fb31767e5bf5d1496062dd5ccb0b178bdc41ce91f3824631ea3b61ad00b",
}
SIGNALS = {"G": "绿", "Y": "黄", "R": "红"}
SUMMARY = re.compile(r"^(\S+)\s+总=([-0-9.]+) 危险=([-0-9.]+) 回撤=([-0-9.]+) "
                     r"机会=([-0-9.]+) 切换=([-0-9.]+)$")


def _sample_directory() -> Path:
    configured = os.environ.get("MARKET_RISK_DESIGN_SAMPLES")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "market-risk-design-samples/v1.2.1"


def _reference_summary(path: Path) -> dict[tuple[str, str], tuple[Decimal, ...]]:
    results = {}
    weights: str | None = None
    formula: str | None = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("===== W-A "):
            weights = "W-A"
        elif line.startswith("===== W-B "):
            weights = "W-B"
        elif line.startswith("===== "):
            weights = None
        elif line == "--- v12":
            formula = "v12"
        elif line.startswith("--- "):
            formula = None
        elif weights is not None and formula == "v12" and (match := SUMMARY.match(line.strip())):
            results[(weights, match[1])] = tuple(Decimal(value) for value in match.groups()[1:])
    return results


def test_v12_design_sample_w_a_w_b_each_loss_component_matches_reference() -> None:
    directory = _sample_directory()
    if not directory.is_dir():
        if os.environ.get("REQUIRE_DESIGN_SAMPLES", "1") == "0":
            pytest.skip("仅在显式关闭外置样例验收时跳过")
        pytest.fail(f"缺少外置设计样例目录：{directory}；可设置 MARKET_RISK_DESIGN_SAMPLES")
    for name, expected_hash in HASHES.items():
        file = directory / name
        assert file.is_file(), f"缺少设计样例：{file}"
        assert hashlib.sha256(file.read_bytes()).hexdigest() == expected_hash

    # 哈希先核对，再载入外置独立脚本的构造路径；runpy 不执行其 __main__ 写文件段。
    with localcontext():
        design = runpy.run_path(str(directory / "v12_paths.py"), run_name="wavewarn_design_reference")
    reference = _reference_summary(directory / "v12_run_output.txt")
    detail: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with (directory / "v12_daily_detail.csv").open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row["weights"] in ("W-A", "W-B") and row["formula"] == "v12":
                detail[(row["weights"], row["scenario"])].append(row)
    assert len(reference) == len(detail) == 28  # 2组主设定×14条合成路径。

    for (weight_name, scenario), expected in reference.items():
        prices, signals = design["SCEN"][scenario]
        weights = design["WEIGHTS"][weight_name]
        days = tuple(dt.date(2000, 1, 1) + dt.timedelta(days=index) for index in range(len(prices)))
        closes = tuple(Decimal(str(value)) for value in prices)
        zz = tuple(ZZEvent("SPX", days[peak], days[t0], days[trough],
                           days[end] if end is not None else None,
                           closes[peak], closes[trough], end is None)
                   for peak, t0, trough, end in design["zz_events"](prices, 0.04, 0.05))
        params = LossParameters(Decimal(str(weights["kD"])), Decimal(str(weights["beta"])),
                                Decimal(str(weights["eta"])))
        lights = tuple(SIGNALS[value] for value in (*signals, signals[-1]))
        executions = execute_asset(days, lights, closes, params.eta, initial_executed=lights[0])
        rows = asset_price_loss(days, closes, executions, zz, params)
        assert len(rows) == len(detail[(weight_name, scenario)]) == len(signals)
        components = (
            sum((row.price_loss for row in rows), Decimal(0))
            + sum((params.gamma for row in executions[:-1] if row.switched), Decimal(0)),
            sum((row.danger_loss for row in rows), Decimal(0)),
            sum((row.drawdown_loss for row in rows), Decimal(0)),
            sum((row.opportunity_loss for row in rows), Decimal(0)),
            sum((params.gamma for row in executions[:-1] if row.switched), Decimal(0)),
        )
        # 参考汇总只公布四位小数；每项允许最多半个末位单位的显示舍入误差。
        for actual, published in zip(components, expected, strict=True):
            assert abs(actual - published) <= Decimal("0.00005"), (weight_name, scenario, actual, published)
        for index, (actual, published) in enumerate(zip(rows, detail[(weight_name, scenario)], strict=True)):
            # 独立设计脚本的逐区间明细按六位小数存储；价格、信号、收益、纪录与损失全部逐行核对。
            assert index == int(published["j"])
            assert (closes[index], closes[index + 1]) == (
                Decimal(published["C_j"]), Decimal(published["C_j1"]))
            assert executions[index].signal == SIGNALS[published["signal_S_j"]]
            assert executions[index].executed == SIGNALS[published["exec_state"]]
            assert actual.exposure == Decimal(published["e"])
            assert actual.dangerous is bool(int(published["danger"]))
            switch = params.gamma if executions[index].switched else Decimal(0)
            assert actual.log_return is not None
            for value, field in ((actual.log_return, "r"),
                                 (actual.drawdown_increment, "dd_record_inc"),
                                 (actual.danger_loss, "loss_danger"),
                                 (actual.drawdown_loss, "loss_pullback"),
                                 (actual.opportunity_loss, "loss_opportunity"),
                                 (switch, "loss_switch"),
                                 (actual.price_loss + switch, "loss_total")):
                assert abs(value - Decimal(published[field])) <= Decimal("0.000001"), (
                    weight_name, scenario, index, field)
