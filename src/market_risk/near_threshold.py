"""贴近门槛的读数（SPEC 第7节，阈值见 settings.yaml 的 near_threshold）。

只列出读数，不影响计分；用于判断分数的稳定性（SOP 9.5）。
"""

from __future__ import annotations

from decimal import Decimal

from market_risk.config import NearThreshold
from market_risk.models import MarketSnapshot, NearThresholdItem
from market_risk.scoring.common import bp, d2, exact, pct_change

BREADTH_LEVELS = (Decimal("40"), Decimal("50"))
VIX_LEVELS = (Decimal("18"), Decimal("20"), Decimal("25"))
G_LEVELS = (Decimal("20"), Decimal("30"))
OAS_LEVELS = (5, 20)


def _add(
    items: list[NearThresholdItem],
    label: str,
    value: Decimal | int,
    threshold: Decimal | int,
    gap: Decimal | int,
    unit: str,
    tolerance: float,
) -> None:
    if abs(float(gap)) <= tolerance + 1e-9:
        items.append(NearThresholdItem(label, float(value), float(threshold), float(gap), unit))


def near_threshold_items(snapshot: MarketSnapshot, cfg: NearThreshold) -> list[NearThresholdItem]:
    items: list[NearThresholdItem] = []

    # 价格：收盘价与 MA20/MA50 ±1%；MA5 与 MA50 ±0.5%
    for sym, e in snapshot.etfs.items():
        c, m5, m20, m50 = d2(e.close), exact(e.ma5), exact(e.ma20), exact(e.ma50)
        assert c is not None and m5 is not None and m20 is not None and m50 is not None
        for name, ma in (("MA20", m20), ("MA50", m50)):
            _add(items, f"{sym} 收盘价 vs {name}", c, ma, pct_change(c, ma), "%",
                 cfg.close_vs_ma_pct)
        _add(items, f"{sym} MA5 vs MA50", m5, m50, pct_change(m5, m50), "%", cfg.ma5_vs_ma50_pct)

    # 广度：F、W、L 与 40、50 ±2 个百分点
    if snapshot.breadth is not None:
        f, w = d2(snapshot.breadth.s5fi), d2(snapshot.breadth.s5tw)
        assert f is not None and w is not None
        for name, v in (("S5FI（F）", f), ("S5TW（W）", w), ("L=min(F,W)", min(f, w))):
            for level in BREADTH_LEVELS:
                _add(items, name, v, level, v - level, "个百分点", cfg.breadth_pts)

    # VIX：与 18、20、25 ±1；g 与 20%、30% ±3 个百分点
    vix, vix5 = d2(snapshot.vix), d2(snapshot.vix_t5)
    if vix is not None:
        for level in VIX_LEVELS:
            _add(items, "VIX", vix, level, vix - level, "点", cfg.vix_pts)
        if vix5 is not None:
            g = pct_change(vix, vix5)
            for level in G_LEVELS:
                _add(items, "VIX 变化率 g", g, level, g - level, "个百分点", cfg.vix_change_pts)

    # 信用：ΔOAS 与 5、20 ±3bp（两个版本的 O1/O6 可能不同，分别列出）
    pairs = (
        ("v2-M", snapshot.oas_o1_v2m, snapshot.oas_o6_v2m),
        ("v3-R1", snapshot.oas_o1, snapshot.oas_o6_v3r1),
    )
    for version, o1, o6 in pairs:
        o1d, o6d = d2(o1), d2(o6)
        if o1d is None or o6d is None:
            continue
        doas = bp(o1d, o6d)
        for level in OAS_LEVELS:
            _add(items, f"ΔOAS（{version}）", doas, level, doas - level, "bp", cfg.oas_bp)

    # 利率：y 与 H 相差 2bp 以内（含 y=H）
    y, h = d2(snapshot.y), d2(snapshot.h)
    if y is not None and h is not None:
        _add(items, "y vs H", y, h, bp(y, h), "bp", cfg.yield_bp)
    return items
