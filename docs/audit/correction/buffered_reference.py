"""带缓冲带的 200 日均线参照行（开发期评价窗口）：机械重跑的对比报告里另算的一行。

用法：在某个版本的工作目录里，用该版本的源码运行
    python buffered_reference.py <版本工作目录> <输出 JSON 路径>
只读开发期输入（截至 2016-12-30）。带宽取自诊断配置里登记的固定参考值（1%），不扫描其他取值。
本脚本只调用标签 v1.4-asrun 中已有的函数：与 200 日均线参照行走同一条执行与损失路径，
同暴露基准用的是同一次运行里“始终绿”“始终红”两条参照行的主损失。对修正前后两个版本用的是同一份脚本。
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.buffered_ma import buffered_ma_states
from market_risk.wavewarn.config_v14 import load_round2_config, load_validation_config
from market_risk.wavewarn.evaluation import CandidateEvaluation, development_labels, evaluate_candidate
from market_risk.wavewarn.inputs import load_inputs_until
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.timing import TimingResult, reference_rows, timing_result
from market_risk.wavewarn.v14_model import prepare_v14

COMPONENTS = ("danger_loss", "drawdown_loss", "opportunity_loss", "switch_cost", "full_exposure_cost")


def row(evaluated: CandidateEvaluation, timing: TimingResult) -> dict[str, object]:
    """一条参照行：主损失与分项、ē、同暴露基准、T、T价格、非绿占比、计费切换次数。"""
    parts = {name: sum((getattr(day, name) for day in evaluated.daily_losses), Decimal(0)) for name in COMPONENTS}
    return {"intervals": timing.intervals, "total_loss": str(evaluated.total_loss),
            **{name: str(value) for name, value in parts.items()},
            "mean_exposure": str(timing.mean_exposure), "benchmark_loss": str(timing.benchmark_loss),
            "timing_score": str(timing.score), "price_score": str(timing.score - parts["switch_cost"]),
            "non_green_share": str(timing.non_green_share),
            "executed_non_green_days": evaluated.executed_non_green_days,
            "billed_switches": evaluated.billed_switches}


def buffered_reference(root: Path) -> dict[str, object]:
    validation = load_validation_config(root / "config/wavewarn_v14_validation.yaml")
    config = load_round2_config(root / "config/wavewarn_v14_diagnostics.yaml")
    model = validation.model
    prepared = prepare_v14(model, load_inputs_until(root, model.base.development_end(), validation.vix3m_file))
    events, unknown = development_labels(prepared)
    references = reference_rows(prepared, events, unknown, model.mr_window)
    green, red = references[0].evaluated.total_loss, references[2].evaluated.total_loss
    eta = configured_loss_settings(prepared.config).parameters.eta
    states = buffered_ma_states(prepared, model.mr_window, config.buffer_band)
    evaluated = evaluate_candidate(prepared, states, events, unknown)
    return {"window": {"t0": str(prepared.t0), "tau": str(prepared.tau),
                       "first_loss_day": str(prepared.first_loss_day), "last_day": str(evaluated.days[-1])},
            "band": str(config.buffer_band), "ma_window": model.mr_window,
            "rows": {references[3].name: row(references[3].evaluated, references[3].timing),
                     states.candidate.model: row(evaluated, timing_result(evaluated, eta, green, red))}}


def main() -> None:
    root, target = Path(sys.argv[1]), Path(sys.argv[2])
    target.write_text(json.dumps(buffered_reference(root), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
