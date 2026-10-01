"""固定设定（v1.4，K=5、θ_P=2.5%）的各项指标汇总：单项与合并归因用。

用法：在某个版本的工作目录里，用该版本的源码运行
    python fixed_setting.py <版本工作目录> <输出 JSON 路径>
只读开发期输入（截至 2016-12-30）。不重选设定：设定取自验证期工程配置里的锁定设定。
本脚本只调用标签 v1.4-asrun 中已有的函数，对四个版本用的是同一份脚本。
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.config_v14 import load_round2_config, load_validation_config
from market_risk.wavewarn.diagnostics_round2 import PORTFOLIO, SELECTED, round2_diagnostics
from market_risk.wavewarn.inputs import load_inputs_until
from market_risk.wavewarn.switch_diagnostics import exposure_change
from market_risk.wavewarn.v14_model import prepare_v14

COMPONENTS = ("danger_loss", "drawdown_loss", "opportunity_loss", "switch_cost", "full_exposure_cost")


def text(value: object) -> object:
    """Decimal 与日期写成字符串，保持完整精度。"""
    return value if value is None or isinstance(value, int | str | bool) else str(value)


def fixed_setting(root: Path) -> dict[str, object]:
    validation = load_validation_config(root / "config/wavewarn_v14_validation.yaml")
    config = load_round2_config(root / "config/wavewarn_v14_diagnostics.yaml")
    end = validation.model.base.development_end()
    prepared = prepare_v14(validation.model, load_inputs_until(root, end, validation.vix3m_file))
    result = round2_diagnostics(prepared, validation, config)
    item = next(row for row in result.objects if row.name == SELECTED)
    nav = next(row for row in result.nav if (row.name, row.scope) == (SELECTED, PORTFOLIO))
    losses = item.evaluated.daily_losses
    drawdown = nav.metrics.drawdown
    green = {symbol: {"median_class_2": text(summary.median_class_2), "class_1": summary.class_1,
                      "class_2": summary.class_2, "class_3": summary.class_3,
                      "median_conservative": text(summary.median_conservative)}
             for symbol, summary in item.green_summaries.items()}
    return {
        "setting": {"k": validation.locked_k, "theta_p": text(validation.locked_theta)},
        "window": {"t0": text(prepared.t0), "tau": text(prepared.tau), "first_interval": text(result.days[0]),
                   "last_day": text(result.days[-1]), "intervals": len(result.days) - 1},
        "total_loss": text(item.evaluated.total_loss),
        "components": {name: text(sum((getattr(row, name) for row in losses), Decimal(0))) for name in COMPONENTS},
        "timing_score": text(item.split.score), "price_score": text(item.split.price_score),
        "mean_exposure": text(item.timing.mean_exposure), "non_green_share": text(item.timing.non_green_share),
        "billed_switches": item.evaluated.billed_switches,
        "exposure_change": text(exposure_change(item.switches)), "green_delay": green,
        "nav_portfolio": {"cumulative": text(nav.metrics.cumulative), "annualized": text(nav.metrics.annualized),
                          "max_drawdown": text(drawdown.depth), "peak_date": text(drawdown.peak_date),
                          "trough_date": text(drawdown.trough_date), "decline_days": drawdown.decline_days,
                          "recovery_date": text(drawdown.recovery_date), "recovery_days": drawdown.recovery_days,
                          "days_after_trough": drawdown.days_after_trough},
        "executed_lights": "".join(item.evaluated.system_executed[:-1]),
    }


def main() -> None:
    root, target = Path(sys.argv[1]), Path(sys.argv[2])
    target.write_text(json.dumps(fixed_setting(root), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
