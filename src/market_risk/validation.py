"""回归比对：程序值与截图读数（SPEC 第9节阶段4）。

期望值来自人工核对（tests/fixtures/regression/expected.json），不得为通过测试而修改；
出现差异时列出交给用户判断，不修改代码迁就。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from market_risk.config import PROJECT_ROOT, Settings
from market_risk.data.raw_io import load_raw_inputs
from market_risk.data.snapshot import RawInputs, build_snapshot
from market_risk.models import BreadthReading, MarketSnapshot, ScoreResult
from market_risk.pipeline import score_snapshot
from market_risk.precision import decimal_value
from market_risk.storage.paths import StoragePaths

EXPECTED_PATH = PROJECT_ROOT / "tests" / "fixtures" / "regression" / "expected.json"
RAW_DIR = PROJECT_ROOT / "tests" / "fixtures" / "raw"
MA_NAMES = ("收盘", "MA5", "MA20", "MA50", "MA200")


@dataclass(frozen=True)
class Check:
    sample: str
    item: str
    screenshot: str
    program: str
    diff: str
    ok: bool


def load_expected(path: Path = EXPECTED_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sample_breadth(expected: dict[str, Any]) -> dict[dt.date, BreadthReading]:
    return {
        dt.date.fromisoformat(d): BreadthReading(dt.date.fromisoformat(d), f, w, "screenshot")
        for d, (f, w) in expected["breadth"].items()
    }


def sample_raw(sample: str, expected: dict[str, Any], raw_dir: Path = RAW_DIR) -> RawInputs:
    return load_raw_inputs(raw_dir / sample, sample_breadth(expected))


def _fmt(x: Any) -> str:
    if x is None:
        return "-"
    return f"{x:.2f}" if isinstance(x, float | Decimal) else str(x)


def _exact(sample: str, item: str, want: Any, got: Any) -> Check:
    if isinstance(want, float):
        quantum = Decimal("0.01")
        ok = got is not None and decimal_value(got).quantize(quantum, rounding=ROUND_HALF_UP) == (
            decimal_value(want).quantize(quantum, rounding=ROUND_HALF_UP))
        diff = "" if ok else ("不一致" if got is None else f"{decimal_value(got) - decimal_value(want):+.2f}")
    else:
        ok = want == got
        diff = "" if ok else "不一致"
    return Check(sample, item, _fmt(want), _fmt(got), diff, ok)


def compare_sample(
    sample: str, exp: dict[str, Any], snap: MarketSnapshot, results: tuple[ScoreResult, ...], tol: float
) -> list[Check]:
    checks: list[Check] = []
    for sym, values in exp["etfs"].items():
        e = snap.etfs[sym]
        got = (e.close, e.ma5, e.ma20, e.ma50, e.ma200)
        for name, want, g in zip(MA_NAMES, values, got, strict=True):
            d = decimal_value(g) - decimal_value(want)
            checks.append(Check(sample, f"{sym} {name}", f"{want:.2f}", f"{g:.2f}", f"{d:+.3f}",
                                abs(d) <= decimal_value(tol)))
    refs = snap.refs
    checks += [
        _exact(sample, "VIX", exp["vix"], snap.vix),
        _exact(sample, "VIX（T−5）", exp["vix_t5"], snap.vix_t5),
        _exact(sample, "10年期 y", exp["y"], snap.y),
        _exact(sample, "窗口最高 H", exp["h"], snap.h),
        _exact(sample, "H 日期", exp["h_date"], str(snap.h_date)),
        _exact(sample, "T−20 的 y", exp["y_t20"], snap.y_t20),
        _exact(sample, "OAS O1", exp["oas_o1"], snap.oas_o1),
        _exact(sample, "O1 日期", exp["oas_o1_date"], str(refs.oas_o1)),
        _exact(sample, "O6 日期", exp["oas_o6_date"], str(refs.oas_o6_v3r1)),
    ]
    if exp["oas_o6"] is not None:
        checks.append(_exact(sample, "OAS O6", exp["oas_o6"], snap.oas_o6_v3r1))
    else:  # 样本1：截图为目测约值，只要求 ΔOAS ≤ 5
        o1, o6 = snap.oas_o1, snap.oas_o6_v3r1
        doas = None if o1 is None or o6 is None else round((o1 - o6) * 100)
        checks.append(Check(sample, "OAS O6（只要求 ΔOAS≤5）", "约2.95", _fmt(o6),
                            f"ΔOAS={doas}bp", doas is not None and doas <= 5))
    step1 = exp.get("three_segment_step1")
    if step1:
        for res in snap.three_segment[True]:
            got_list = [t for t in res.traces if t.step1]
            want = step1[res.symbol]
            if want and isinstance(want[0], list):
                got_v = [[str(t.d1), t.d1_close, t.lc, str(t.lc_date)] for t in got_list]
                want_v = [[w[0], decimal_value(w[1]), decimal_value(w[2]), w[3]] for w in want]
            else:
                got_v = [str(t.d1) for t in got_list]
                want_v = list(want)
            checks.append(Check(sample, f"三环节第一步 {res.symbol}", str(want_v) if want_v else "无",
                                str(got_v) if got_v else "无", "", got_v == want_v))
            checks.append(Check(sample, f"三环节完成 {res.symbol}", "否", "是" if res.completed else "否",
                                "", not res.completed))
    for r in results:
        want = exp["scores"][r.version]
        got = [d.score for d in r.dimensions]
        checks.append(Check(sample, f"{r.version} 五项分数", str(want), str(got), "", got == want))
        checks.append(_exact(sample, f"{r.version} 阶段", exp["stage"], r.stage))
    return checks


def validate_all(settings: Settings, expected_path: Path = EXPECTED_PATH,
                 raw_dir: Path = RAW_DIR, market_paths: StoragePaths | None = None) -> list[Check]:
    """离线样本比对；给出 market_paths 时，另用 data/market/ 对同一样本重新计分并逐项比对（B1-5）。"""
    spec = load_expected(expected_path)
    tol = float(spec["tolerance_price"])
    checks: list[Check] = []
    for sample, exp in spec["samples"].items():
        snap = build_snapshot(sample_raw(sample, exp, raw_dir), settings.scored_symbols)
        results, _ = score_snapshot(snap, settings)
        checks += compare_sample(sample, exp, snap, results, tol)
        if market_paths is not None:
            checks += compare_market(sample, exp, settings, market_paths, snap, results, tol)
    return checks


def compare_market(sample: str, exp: dict[str, Any], settings: Settings, paths: StoragePaths,
                   offline: MarketSnapshot, offline_results: tuple[ScoreResult, ...], tol: float) -> list[Check]:
    """用 data/market/ 组装同一基准日的输入：与截图读数比对，并与离线样本的分数、阶段逐项比对。"""
    from market_risk.data.market import MarketDataError, load_raw_inputs

    label = f"{sample}（data/market）"
    try:
        raw = load_raw_inputs(paths, settings, dt.date.fromisoformat(sample), revision_check=False)
    except MarketDataError as exc:
        return [Check(label, "数据集", "覆盖基准日", str(exc), "", False)]
    snap = build_snapshot(raw, settings.scored_symbols)
    results, _ = score_snapshot(snap, settings)
    checks = compare_sample(label, exp, snap, results, tol)
    for a, b in zip(offline_results, results, strict=True):
        same = [d.score for d in a.dimensions] == [d.score for d in b.dimensions] and a.stage == b.stage \
            and a.total == b.total
        checks.append(Check(label, f"{b.version} 与离线样本", f"{[d.score for d in a.dimensions]} {a.stage}",
                            f"{[d.score for d in b.dimensions]} {b.stage}", "", same))
    return checks
