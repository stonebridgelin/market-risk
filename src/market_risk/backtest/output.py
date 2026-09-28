"""回测结果转为运行目录的文件行（纯函数）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from market_risk.backtest.engine import DayResult
from market_risk.backtest.labels import Episode, OutcomeRow, WindowRow
from market_risk.metrics import DAILY_METRICS_NOTE, format_metric
from market_risk.models import DimensionScore, ScoreResult
from market_risk.outcomes import OUT_Q
from market_risk.storage.db import alert_status

VERSION_KEYS = {"v2-M": "v2m", "v3-R1": "v3r1"}


def yes(v: bool) -> str:
    return "是" if v else "否"


def dim_text(d: DimensionScore) -> str:
    return str(d.score) if d.score is not None else f"待补{list(d.possible_scores)}"


def score_row(day: DayResult, version: str, r: ScoreResult) -> dict[str, Any]:
    lo, hi = r.total_range
    chain = dict(r.clear_deterioration)
    return {
        "date": day.date, "version": version,
        "price": dim_text(r.price), "breadth": dim_text(r.breadth), "vix": dim_text(r.vix),
        "rates": dim_text(r.rates), "credit": dim_text(r.credit),
        "total": r.total, "total_min": lo, "total_max": hi, "stage": r.stage or "范围跨越阶段",
        "pending_dimensions": "、".join(d.name for d in r.dimensions if d.score is None),
        "clear_deterioration": chain.get("大盘明确恶化"),
        "clear_deterioration_chain": "；".join(f"{k}:{v}" for k, v in r.clear_deterioration),
        "alert": alert_status(r.total, lo, hi),                 # SOP 9.4：总分≥3（是/否/未知）
        "flags": "；".join(day.flags.get(version, ())),
        "alt_source_scores": day.alt_scores.get(version),
    }


def daily_score_rows(days: Sequence[DayResult]) -> list[dict[str, Any]]:
    return [score_row(d, v, r) for d in days for v, r in d.results.items()]


def daily_metric_rows(days: Sequence[DayResult]) -> tuple[list[str], list[dict[str, Any]]]:
    keys: dict[str, None] = {}
    for d in days:
        keys.update(dict.fromkeys(d.metrics))
    fields = ["date", *keys]
    rows = [{"date": d.date, **{k: format_metric(d.metrics.get(k)) for k in keys}} for d in days]  # type: ignore[arg-type]
    return fields, rows


def _q(v: Decimal | None) -> str | None:
    return None if v is None else format(v.quantize(OUT_Q), "f")


def outcome_row(o: OutcomeRow) -> dict[str, Any]:
    lab = o.label
    return {"base_date": lab.base_date, "window_start": lab.window_start, "window_end": lab.window_end,
            "spx_drawdown_from_base": _q(lab.spx_drawdown_from_base),
            "qqq_drawdown_from_base": _q(lab.qqq_drawdown_from_base),
            "spx_peak_to_trough_drawdown": _q(lab.spx_peak_to_trough_drawdown),
            "qqq_peak_to_trough_drawdown": _q(lab.qqq_peak_to_trough_drawdown),
            "is_event": yes(lab.is_event), "event_date": lab.event_date, "is_near_event": yes(lab.is_near_event),
            "period": o.period, "crosses_period": yes(o.crosses_period)}


def level_text(level: Decimal) -> str:
    return format((level * 100).normalize(), "f")


def episode_row(e: Episode) -> dict[str, Any]:
    return {"symbol": e.symbol, "level": level_text(e.level), "high_date": e.high_date,
            "high_close": format(e.high_close, "f"), "low_date": e.low_date,
            "low_close": None if e.low_close is None else format(e.low_close, "f"),
            "drawdown_pct": None if e.drawdown_pct is None else format(e.drawdown_pct, "f"),
            "trading_days": e.trading_days, "grade": e.grade, "status": e.status, "confirm_date": e.confirm_date,
            "recovery_date": e.recovery_date, "recovery_note": e.recovery_note, "period": e.period,
            "before_start": yes(e.before_start), "crosses_boundary": yes(e.crosses_boundary),
            "counted": yes(e.counted)}


def _f(v: Decimal | None) -> str | None:
    return None if v is None else format(v, "f")


def window_row(w: WindowRow) -> dict[str, Any]:
    row: dict[str, Any] = {"symbol": w.symbol, "level": level_text(w.level), "high_date": w.high_date,
                           "date": w.date, "offset": w.offset, "offset_from_trough": w.offset_from_trough,
                           "drawdown_from_peak": _f(w.drawdown_from_peak), "decline_progress": _f(w.decline_progress),
                           "rebound_from_trough": _f(w.rebound_from_trough)}
    for version, key in VERSION_KEYS.items():
        total, lo, hi, stage = w.scores.get(version, (None, None, None, None))
        row.update({f"{key}_total": total, f"{key}_total_min": lo, f"{key}_total_max": hi, f"{key}_stage": stage})
    return row


def score_index(days: Sequence[DayResult]) -> dict[tuple[Any, str], tuple[int | None, int, int, str | None]]:
    return {(d.date, v): (r.total, r.total_range[0], r.total_range[1], r.stage or "范围跨越阶段")
            for d in days for v, r in d.results.items()}


def readme_text(meta: Mapping[str, Any]) -> str:
    return "\n".join([
        f"# 逐日历史回测 {meta['run_id']}", "",
        f"- 区间：{meta['start']} 至 {meta['end']}（{meta['days']} 个交易日）；版本：{'、'.join(meta['versions'])}",
        f"- 代码 commit：{meta['git_commit']}；数据集 manifest sha256：{meta['market_manifest_sha256']}",
        f"- 回测配置：config/backtest.yaml（版本 {meta['config']['config_version']}，"
        f"sha256 {meta['config']['sha256']}）",
        f"- 保留期：{'已解锁（' + meta['holdout_unlocked_at'] + '）' if meta['holdout_unlocked_at'] else '未解锁'}",
        "", "## 文件", "",
        "| 文件 | 内容 |", "|---|---|",
        "| `daily_scores.csv` | 每个基准日 × 版本一行：五维分数（待补为 待补[可能取值]）、总分或范围、阶段、待补维度、"
        "明确恶化证据链、SOP 9.4 预警（总分≥3：是/否/未知）、flags（类型|标的|日期|用途） |",
        f"| `daily_metrics.csv` | {DAILY_METRICS_NOTE} |",
        "| `outcomes.csv` | 结果标签（隔离）：基准日口径跌幅、峰谷回撤、is_event、is_near_event"
        "（Decimal，取整前判定），"
        "所属区间与是否跨入下一区间；结果窗口结束于保留期的不写出（未解锁时） |",
        "| `pullback_episodes.csv` | 回调事件（隔离）：多层级 ZigZag，同一段下跌可出现在多个层级；按高点归属区间；"
        "屏蔽依赖保留期数据的字段 |",
        "| `episode_windows.csv` | 回调窗口（隔离）：高点前20至低点后20个交易日两个版本的总分与阶段；不写保留期的行 |",
        "", "标签文件使用基准日之后的数据，评分代码不得读取。", ""])
