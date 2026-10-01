"""v1.4 验证期评价流程的写出（读写边界）：标签、汇总、主检验、逐日明细与四部分报告。

正式验证期与开发期演练共用；本模块只负责把已经算好的结果写到给定目录。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from market_risk.wavewarn.config import PairedParameters
from market_risk.wavewarn.config_v14 import ValidationConfig
from market_risk.wavewarn.evaluation import SYMBOLS
from market_risk.wavewarn.evaluation_run import file_sha256, write_csv
from market_risk.wavewarn.evaluation_tables import (
    ALERT_HEADER,
    ALERT_SUMMARY_HEADER,
    EVENT_CLASS_HEADER,
    EVENT_HEADER,
    MISSING_HEADER,
    YEARLY_HEADER,
    CandidateTables,
    Row,
    candidate_tables,
)
from market_risk.wavewarn.evaluation_v13_tables import (
    REFERENCE_HEADER,
    V13_DAILY_HEADER,
    reference_prefix,
    reference_row,
    v13_daily_rows,
)
from market_risk.wavewarn.evaluation_v14 import setting_label
from market_risk.wavewarn.evaluation_v14_tables import SETTING_HEADER, setting_prefix
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.validation_flow import WindowEvaluation
from market_risk.wavewarn.validation_report import (
    DIFFERENCE_HEADER,
    DROP_HEADER,
    LEAVE_ONE_HEADER,
    SUMMARY_HEADER,
    TEST_HEADER,
    LockInfo,
    difference_rows,
    drop_rows,
    leave_one_rows,
    report_lines,
    summary_rows,
    test_rows,
)

REPORT_NAME = "评价报告.md"
LOCK_FILE = "lock_record.json"


def write_lock_info(destination: Path, lock: LockInfo, head: str) -> None:
    """运行开始时写入：锁定记录的路径与 SHA-256、记录中的代码提交号、运行时的 HEAD。"""
    payload = {"lock_record": lock.path, "lock_record_sha256": lock.sha256, "code_commit": lock.code_commit,
               "head": head, "formal": lock.formal}
    (destination / LOCK_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")


def write_labels(destination: Path, result: WindowEvaluation) -> None:
    """本次运行生成的 ZZ 标签：分资产事件、合并事件、尾段未定区间。"""
    flag = lambda value: "是" if value else "否"  # noqa: E731
    events = sorted((event for symbol in SYMBOLS for event in result.events[symbol]),
                    key=lambda event: (event.peak_date, event.symbol))
    write_csv(destination / "zz_events.csv",
              ("symbol", "peak_date", "t0_date", "trough_date", "end_date", "peak_close", "trough_close",
               "right_censored"),
              [(event.symbol, event.peak_date, event.t0_date, event.trough_date, event.end_date or "",
                event.peak_close, event.trough_close, flag(event.right_censored)) for event in events])
    write_csv(destination / "zz_merged.csv", ("peak_date", "t0_date", "trough_date", "source", "member_count"),
              [(group.peak_date, group.t0_date, group.trough_date, group.source, len(group.members))
               for group in result.merged])
    unknown = result.unknown
    rows = sorted(((symbol, day, unknown.reasons_by_asset[symbol][day], unknown.label_end)
                   for symbol in SYMBOLS for day in unknown.days_by_asset[symbol]),
                  key=lambda row: (row[1], row[0]))
    write_csv(destination / "zz_unknown.csv", ("symbol", "date", "reason", "label_end"), rows)


def _with_prefix(rows: Sequence[Row], prefix: Row) -> list[Row]:
    return [(*prefix, *row[4:]) for row in rows]


def write_locked_detail(destination: Path, result: WindowEvaluation, tables: CandidateTables) -> None:
    """锁定设定与四条参照行的逐日明细；锁定设定的事件账与警报账。"""
    prefix = setting_prefix(result.locked_states.candidate)
    weights = configured_loss_settings(result.prepared.config).weights
    daily = _with_prefix(tables.daily, prefix)
    for reference in result.references:
        daily.extend(v13_daily_rows(reference_prefix(reference.name), reference.evaluated, weights,
                                    result.prepared.inputs.series))
    write_csv(destination / "daily_selected.csv", V13_DAILY_HEADER, daily)
    for name, header, rows in (("event_ledger_selected", EVENT_HEADER, tables.events),
                               ("event_class_summary_selected", EVENT_CLASS_HEADER, tables.event_classes),
                               ("alert_ledger_selected", ALERT_HEADER, tables.alerts),
                               ("alert_summary_selected", ALERT_SUMMARY_HEADER, [tables.alert_summary]),
                               ("yearly_alert_selected", YEARLY_HEADER, tables.yearly)):
        write_csv(destination / f"{name}.csv", (*SETTING_HEADER, *header[4:]), _with_prefix(rows, prefix))


def write_window_outputs(destination: Path, result: WindowEvaluation, lock: LockInfo, config: ValidationConfig,
                         params: PairedParameters) -> str:
    """写出全部结果文件，返回未压缩逐日明细的 SHA-256。destination 须已存在。"""
    prepared = result.prepared
    weights = configured_loss_settings(prepared.config).weights
    tables = candidate_tables(prepared, result.locked_states, result.locked, result.events, result.merged,
                              result.window.rule, weights, result.unknown)
    write_labels(destination, result)
    write_csv(destination / "settings_summary.csv", SUMMARY_HEADER, summary_rows(result))
    write_csv(destination / "reference_rows.csv", REFERENCE_HEADER,
              [reference_row(row) for row in result.references])
    write_csv(destination / "main_test.csv", TEST_HEADER, test_rows(result.main_test))
    write_csv(destination / "paired_differences.csv", DIFFERENCE_HEADER, difference_rows(result))
    write_csv(destination / "leave_one_event.csv", LEAVE_ONE_HEADER, leave_one_rows(result.main_test))
    write_csv(destination / "big_drop_events.csv", DROP_HEADER, drop_rows(result))
    write_csv(destination / "missing_audit.csv", MISSING_HEADER, result.missing)
    write_locked_detail(destination, result, tables)
    label = setting_label(result.locked_states.candidate)
    lines = report_lines(result, lock, config, params,
                         tuple((label, *row[1:]) for row in tables.event_classes),
                         ((label, *tables.alert_summary[1:]),))
    (destination / REPORT_NAME).write_text("\n".join(lines), encoding="utf-8")
    return file_sha256(destination / "daily_selected.csv")
