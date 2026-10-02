"""数据留痕配置的读取（config/provenance.yaml）。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from market_risk.provenance import MANUAL_CORRECTION, SOURCE_REVISION, IndicatorSpec, ProvenanceConfig

PROVENANCE_CONFIG = "provenance.yaml"


def parse_provenance_config(raw: dict[str, Any]) -> ProvenanceConfig:
    if raw.get("version") != "provenance-1":
        raise ValueError("数据留痕配置版本错误")
    indicators = {str(code): IndicatorSpec(str(code), str(item["unit"]), str(item["basis"]),
                                           Decimal(str(item["minimum"])), Decimal(str(item["maximum"])))
                  for code, item in (raw.get("indicators") or {}).items()}
    config = ProvenanceConfig(indicators, str(raw.get("timezone")), dt.time.fromisoformat(str(raw.get("late_cutoff"))),
                              int(raw.get("max_precision", 0)),
                              tuple(str(item) for item in raw.get("acquisition_methods", ())),
                              tuple(str(item) for item in raw.get("revision_kinds", ())))
    if (config.timezone, config.late_cutoff) != ("America/New_York", dt.time(18, 30)):
        raise ValueError("迟到判断的时区或截止时刻与负责人裁决不一致（美东 18:30:00）")
    if not indicators or any(spec.minimum > spec.maximum for spec in indicators.values()):
        raise ValueError("数据留痕配置缺少指标，或取值范围不合法")
    if not 0 < config.max_precision <= 8:
        raise ValueError("原始值小数位数的上限须在 1 至 8 之间（规范化值存为 Numeric(20, 8)）")
    if set(config.revision_kinds) != {SOURCE_REVISION, MANUAL_CORRECTION} or not config.methods:
        raise ValueError("修订类型或取得方式与设计说明不一致")
    return config


def load_provenance_config(config_dir: Path) -> ProvenanceConfig:
    with (config_dir / PROVENANCE_CONFIG).open(encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    if not isinstance(raw, dict):
        raise ValueError("数据留痕配置格式错误")
    return parse_provenance_config(raw)
