"""扁平的数值指标（metrics.json / 数据库 metrics 表）。不读取结果标签。"""

from __future__ import annotations

from typing import Any


def metrics_from_snapshot_json(snap: dict[str, Any]) -> dict[str, float | None]:
    """从 snapshot.json 提取扁平的数值指标（收盘价、均线、差值、L、g、Δy、ΔOAS 等）。"""
    m: dict[str, float | None] = {}
    for sym, e in snap.get("etfs", {}).items():
        for k in ("close", "ma5", "ma10", "ma20", "ma30", "ma50", "ma200"):
            m[f"{sym}.{k}"] = e.get(k)
        if e.get("close") is not None and e.get("ma50"):
            m[f"{sym}.close_minus_ma20"] = round(e["close"] - e["ma20"], 6)
            m[f"{sym}.close_minus_ma50"] = round(e["close"] - e["ma50"], 6)
            m[f"{sym}.ma5_minus_ma50"] = round(e["ma5"] - e["ma50"], 6)
    m["spy_window_max_close"] = snap.get("spy_window_max_close")
    b, b5 = snap.get("breadth"), snap.get("breadth_t5")
    m["F"], m["W"] = (b["s5fi"], b["s5tw"]) if b else (None, None)
    m["F5"], m["W5"] = (b5["s5fi"], b5["s5tw"]) if b5 else (None, None)
    m["L"] = min(m["F"], m["W"]) if m["F"] is not None and m["W"] is not None else None
    m["VIX"], m["VIX_T5"] = snap.get("vix"), snap.get("vix_t5")
    m["g_pct"] = (m["VIX"] / m["VIX_T5"] - 1) * 100 if m["VIX"] and m["VIX_T5"] else None
    m["y"], m["H"], m["y_t20"] = snap.get("y"), snap.get("h"), snap.get("y_t20")
    m["dy_bp"] = round((m["y"] - m["y_t20"]) * 100, 6) if m["y"] is not None and m["y_t20"] is not None else None
    for ver, o1k, o6k in (("v3r1", "oas_o1", "oas_o6_v3r1"), ("v2m", "oas_o1_v2m", "oas_o6_v2m")):
        o1, o6 = snap.get(o1k), snap.get(o6k)
        m[f"oas_o1_{ver}"], m[f"oas_o6_{ver}"] = o1, o6
        m[f"doas_{ver}_bp"] = round((o1 - o6) * 100, 6) if o1 is not None and o6 is not None else None
    m["hyg_lqd"] = snap.get("hyg_lqd")
    return m
