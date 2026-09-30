"""开发期 ZZ 研究标签导出；与现有回测标签和评分文件完全分离。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from market_risk.wavewarn.config import WavewarnConfig
from market_risk.wavewarn.inputs import DevelopmentInputs
from market_risk.wavewarn.labels_zz import LABEL_VERSION, ZZEvent, find_zz_events, merge_zz_events


def _event_row(event: ZZEvent) -> tuple[str, ...]:
    return (event.symbol, event.peak_date.isoformat(), event.t0_date.isoformat(),
            event.trough_date.isoformat(), event.end_date.isoformat() if event.end_date else "",
            str(event.peak_close), str(event.trough_close), "是" if event.right_censored else "否")


def write_development_labels(inputs: DevelopmentInputs, destination: Path,
                             config: WavewarnConfig) -> tuple[int, int]:
    """只使用开发期截断输入；保留各资产 ZZ 与闭区间合并结果。"""
    destination.mkdir(parents=True, exist_ok=True)
    all_events = []
    thresholds = config.zz_thresholds()
    for symbol in ("SPX", "QQQ"):
        all_events.extend(find_zz_events(symbol, inputs.days, inputs.series[symbol], config.development_end(),
                                         thresholds))
    events = tuple(sorted(all_events, key=lambda event: (event.peak_date, event.symbol)))
    merged = merge_zz_events(events)
    with (destination / "zz_events_development.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("symbol", "peak_date", "t0_date", "trough_date", "end_date",
                         "peak_close", "trough_close", "right_censored"))
        writer.writerows(_event_row(event) for event in events)
    with (destination / "zz_merged_development.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("peak_date", "t0_date", "trough_date", "source", "member_count", "members"))
        for group in merged:
            members = ";".join(f"{event.symbol}:{event.peak_date.isoformat()}:{event.trough_date.isoformat()}"
                               for event in group.members)
            writer.writerow((group.peak_date.isoformat(), group.t0_date.isoformat(),
                             group.trough_date.isoformat(), group.source, len(group.members), members))
    metadata = {"label_version": LABEL_VERSION, "date_scope": "development_only",
                "last_date": config.development_end().isoformat(), "asset_events": len(events),
                "merged_events": len(merged),
                "thresholds": {symbol: [str(value) for value in pair] for symbol, pair in thresholds.items()}}
    (destination / "zz_development_meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(events), len(merged)
