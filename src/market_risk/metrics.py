"""扁平的数值指标（metrics.json / 数据库 metrics 表 / 回测 daily_metrics.csv）。不读取结果标签。

计算口径（2026-09-27，阶段6）：
- 原始数值（收盘价、F、W、VIX、收益率、OAS）按公布精度读取为 Decimal；
- 派生值（均线差、百分比差、g、Δy、ΔOAS 等）用 Decimal 精确计算，输出时统一保留 6 位小数，ROUND_HALF_UP；
- 两个版本口径不同的指标（OAS 的 O1/O6 日期、数值与 ΔOAS）分别成列（后缀 _v2m、_v3r1）；
- 单日运行的 metrics.json 与回测的 daily_metrics.csv 使用同一套计算（metric_values）。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from market_risk.precision import decimal_value, published_price

SCORED = ("SPY", "QQQ", "RSP")
MA_KEYS = ("ma5", "ma20", "ma50", "ma200")
Q6 = Decimal("0.000001")
SPY_NEAR_HIGH = Decimal("0.98")          # v2-M 广度 (b)：20日最高收盘价 × 0.98
Metric = Decimal | str | bool | None


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    return decimal_value(v)


def _p2(v: Any) -> Decimal | None:
    """原始数值按公布的两位小数读取。"""
    x = _d(v)
    return None if x is None else published_price(x)


def q6(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Q6, ROUND_HALF_UP)


def _sub(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None else a - b


def _pct(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or not b else (a / b - 1) * 100


def metric_values(snap: Mapping[str, Any], three_segment: Mapping[str, bool] | None = None) -> dict[str, Metric]:
    """从 snapshot.json 结构提取全部指标（精确 Decimal，未取整）。

    three_segment：v2-M 三环节（d1 为 T−20 至 T−2）在各 ETF 上是否完成；snapshot.json 不含三环节，由调用方传入。
    """
    m: dict[str, Metric] = {}
    for sym, e in snap.get("etfs", {}).items():
        close = _p2(e.get("close"))
        m[f"{sym}.close"] = close
        for k in ("ma5", "ma10", "ma20", "ma30", "ma50", "ma200"):
            m[f"{sym}.{k}"] = _d(e.get(k))
        for k in MA_KEYS:
            ma = _d(e.get(k))
            m[f"{sym}.close_minus_{k}"] = _sub(close, ma)
            m[f"{sym}.close_minus_{k}_pct"] = _pct(close, ma)
        m[f"{sym}.ma5_minus_ma50"] = _sub(_d(e.get("ma5")), _d(e.get("ma50")))
        m[f"{sym}.ma5_minus_ma50_pct"] = _pct(_d(e.get("ma5")), _d(e.get("ma50")))
    for sym, reason in snap.get("missing_etfs", ()):
        m[f"{sym}.missing"] = reason
    hi20 = _p2(snap.get("spy_window_max_close"))
    m["spy_window_max_close"] = hi20
    m["spy_window_max_close_x098"] = None if hi20 is None else hi20 * SPY_NEAR_HIGH
    for sym, completed in (three_segment or {}).items():
        m[f"{sym}.three_segment_completed_v2m"] = completed
    b, b5 = snap.get("breadth"), snap.get("breadth_t5")
    f, w = (_p2(b["s5fi"]), _p2(b["s5tw"])) if b else (None, None)
    f5, w5 = (_p2(b5["s5fi"]), _p2(b5["s5tw"])) if b5 else (None, None)
    m["F"], m["W"], m["F5"], m["W5"] = f, w, f5, w5
    m["L"] = min(f, w) if f is not None and w is not None else None
    m["F_minus_F5"], m["W_minus_W5"] = _sub(f, f5), _sub(w, w5)
    v, v5 = _p2(snap.get("vix")), _p2(snap.get("vix_t5"))
    m["VIX"], m["VIX_T5"] = v, v5
    m["g_pct"] = _pct(v, v5)
    y, h, y20 = _p2(snap.get("y")), _p2(snap.get("h")), _p2(snap.get("y_t20"))
    m["y"], m["H"], m["y_t20"] = y, h, y20
    m["H_date"] = snap.get("h_date")
    m["dy_bp"] = None if _sub(y, y20) is None else _sub(y, y20) * 100      # type: ignore[operator]
    m["y_minus_H_bp"] = None if _sub(y, h) is None else _sub(y, h) * 100  # type: ignore[operator]
    refs = snap.get("refs", {})
    for ver, o1k, o6k, o1d, o6d in (("v3r1", "oas_o1", "oas_o6_v3r1", "oas_o1", "oas_o6_v3r1"),
                                    ("v2m", "oas_o1_v2m", "oas_o6_v2m", "oas_o1_v2m", "oas_o6_v2m")):
        o1, o6 = _p2(snap.get(o1k)), _p2(snap.get(o6k))
        m[f"oas_o1_date_{ver}"], m[f"oas_o6_date_{ver}"] = refs.get(o1d), refs.get(o6d)
        m[f"oas_o1_{ver}"], m[f"oas_o6_{ver}"] = o1, o6
        m[f"doas_{ver}_bp"] = None if _sub(o1, o6) is None else _sub(o1, o6) * 100  # type: ignore[operator]
    m["hyg_lqd"] = _d(snap.get("hyg_lqd"))
    return m


def metrics_from_snapshot_json(snap: dict[str, Any], three_segment: Mapping[str, bool] | None = None
                               ) -> dict[str, float | str | bool | None]:
    """metrics.json / 数据库 metrics 表：数值保留 6 位小数（ROUND_HALF_UP）后以浮点写出；日期、说明为文本。"""
    return {k: float(q6(v)) if isinstance(v, Decimal) else v  # type: ignore[arg-type]
            for k, v in metric_values(snap, three_segment).items()}


def format_metric(v: Metric) -> str:
    """daily_metrics.csv 的写法：数值保留 6 位小数（ROUND_HALF_UP）；布尔为 是/否；缺失为空。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, Decimal):
        return format(q6(v), "f")
    return str(v)


DAILY_METRICS_NOTE = ("daily_metrics.csv：每个基准日一行，内容为评分实际使用的原始数值与派生值（metrics.metric_values，"
                      "与单日运行的 metrics.json 同一套计算）。原始数值按公布的两位小数读取，"
                      "派生值用 Decimal 精确计算；"
                      "写出时所有数值统一保留 6 位小数，舍入方式 ROUND_HALF_UP；布尔为 是/否；缺失为空。"
                      "两个版本口径不同的指标以 _v2m、_v3r1 后缀分列。不含结果标签与回调事件标签。")
