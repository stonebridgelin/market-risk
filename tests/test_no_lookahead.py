"""防未来信息测试（SPEC 0.6、9节阶段2验收）。

构造包含基准日之后数据的输入，确认截断后不参与任何计算；并确认断言能捕获越界。
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import SAMPLE_DATES, load_sample_raw

from market_risk import calendar as mcal
from market_risk.data.snapshot import (
    LookaheadError,
    assert_no_lookahead,
    build_snapshot,
    truncate,
)
from market_risk.models import BreadthReading

FUTURE_DAYS = 25


def _with_future(raw):
    """在每个序列后追加基准日之后的极端数值；并把基准日当天的 OAS 改成极端值。"""
    base = raw.base_date
    future = mcal.stock_trading_days(base + dt.timedelta(days=1), base + dt.timedelta(days=45))
    future = future[:FUTURE_DAYS]
    closes = {s: {**c, **dict.fromkeys(future, 1.0)} for s, c in raw.closes.items()}
    oas = {**raw.oas, **dict.fromkeys(future, 9.99)}
    oas[base] = 9.99  # 基准日当天的 OAS 观测不得使用（O1 在基准日之前）
    breadth = {
        **raw.breadth,
        future[0]: BreadthReading(future[0], 1.0, 1.0),
    }
    return dataclasses.replace(
        raw,
        closes=closes,
        vix_fred={**raw.vix_fred, **dict.fromkeys(future, 99.0)},
        vix_cboe={**(raw.vix_cboe or {}), **dict.fromkeys(future, 99.0)},
        treasury={**raw.treasury, **dict.fromkeys(future, 9.99)},
        oas=oas,
        oas_vintage={**(raw.oas_vintage or {}), **dict.fromkeys(future, 9.99)},
        breadth=breadth,
    )


@pytest.mark.parametrize("sample", SAMPLE_DATES)
def test_future_data_does_not_change_snapshot(sample):
    base = dt.date.fromisoformat(sample)
    breadth = {base: BreadthReading(base, 55.0, 60.0)}
    raw = load_sample_raw(sample, breadth)
    clean = build_snapshot(raw)
    polluted = build_snapshot(_with_future(raw))
    assert polluted.etfs == clean.etfs
    assert polluted.three_segment == clean.three_segment
    assert (polluted.vix, polluted.vix_t5) == (clean.vix, clean.vix_t5)
    assert (polluted.y, polluted.h, polluted.h_dates, polluted.y_t20) == (
        clean.y, clean.h, clean.h_dates, clean.y_t20
    )
    assert polluted.yields == clean.yields
    assert polluted.breadth == clean.breadth
    assert polluted.spy_window_max_close == clean.spy_window_max_close
    # OAS：基准日当天被改成 9.99，O1/O6 不受影响
    assert polluted.oas_o1 == clean.oas_o1 != 9.99
    assert polluted.oas_o1_v2m != 9.99
    assert polluted.refs.oas_o1 < base and polluted.refs.oas_o1_v2m < base
    assert polluted.refs == clean.refs


def test_truncate_keeps_base_date_inclusive():
    base = dt.date(2025, 11, 28)
    s = {base - dt.timedelta(days=1): 1.0, base: 2.0, base + dt.timedelta(days=1): 3.0}
    assert truncate(s, base) == {base - dt.timedelta(days=1): 1.0, base: 2.0}


def test_assert_catches_future_yield():
    snap = build_snapshot(load_sample_raw("2025-11-28"))
    bad = dataclasses.replace(snap, yields={**snap.yields, dt.date(2025, 12, 1): 4.09})
    with pytest.raises(LookaheadError, match="yields=2025-12-01"):
        assert_no_lookahead(bad)


def test_assert_catches_future_close_and_breadth_and_refs():
    snap = build_snapshot(load_sample_raw("2025-11-28"))
    spy = snap.etfs["SPY"]
    bad_spy = dataclasses.replace(spy, closes=(*spy.closes, (dt.date(2025, 12, 1), 680.0)))
    with pytest.raises(LookaheadError, match=r"SPY\.closes"):
        assert_no_lookahead(dataclasses.replace(snap, etfs={**snap.etfs, "SPY": bad_spy}))
    future_reading = BreadthReading(dt.date(2025, 12, 1), 50.0, 50.0)
    with pytest.raises(LookaheadError, match="breadth"):
        assert_no_lookahead(dataclasses.replace(snap, breadth_t5=future_reading))
    bad_refs = dataclasses.replace(snap.refs, oas_o1=dt.date(2025, 11, 28) + dt.timedelta(days=3))
    with pytest.raises(LookaheadError, match=r"refs\.oas_o1"):
        assert_no_lookahead(dataclasses.replace(snap, refs=bad_refs))


def test_assert_catches_future_three_segment_dates():
    snap = build_snapshot(load_sample_raw("2025-11-28"))
    res = snap.three_segment[True][0]
    bad_trace = dataclasses.replace(res.traces[0], d2_dates=(dt.date(2025, 12, 2),))
    bad_res = dataclasses.replace(res, traces=(bad_trace, *res.traces[1:]))
    bad = dataclasses.replace(
        snap, three_segment={True: (bad_res, *snap.three_segment[True][1:]), False: ()}
    )
    with pytest.raises(LookaheadError, match=r"three_segment\.d2"):
        assert_no_lookahead(bad)


def test_outcome_window_dates_are_allowed():
    """结果窗口只计算日期、不含行情，不视为越界。"""
    snap = build_snapshot(load_sample_raw("2025-11-28"))
    assert snap.refs.outcome_window_end > snap.refs.base_date
    assert_no_lookahead(snap)


def test_snapshot_module_does_not_import_network_or_outcomes():
    """snapshot.py 是纯计算：不导入网络模块，不引用结果标签。"""
    import inspect

    from market_risk.data import snapshot

    source = inspect.getsource(snapshot)
    for forbidden in ("requests", "yfinance", "outcomes", "open(", "read_text"):
        assert forbidden not in source, forbidden


def test_scoring_never_imports_two_sided_price_audit():
    """双侧残差审计不能进入评分依赖链；指数核查与除息记录也不能作为评分输入。"""
    import ast

    from market_risk.config import PROJECT_ROOT

    root = PROJECT_ROOT / 'src' / 'market_risk'
    files = [*root.joinpath('scoring').glob('*.py'), root / 'indicators.py', root / 'pipeline.py',
             root / 'data' / 'snapshot.py', root / 'data' / 'market.py']
    for path in files:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert 'price_review' not in (node.module or ''), path
                assert all('price_review' not in name.name for name in node.names), path
            elif isinstance(node, ast.Import):
                assert all('price_review' not in name.name for name in node.names), path


# 评分路径上的模块：导入它们时，不得（直接或间接）加载双侧审计、影响检验等使用未来数据的模块。
SCORING_PATH_MODULES = (
    "market_risk.scoring.v2m", "market_risk.scoring.v3r1", "market_risk.indicators", "market_risk.calendar",
    "market_risk.pipeline", "market_risk.data.snapshot", "market_risk.data.market",
    "market_risk.prepare", "market_risk.metrics", "market_risk.backtest.engine",
)
# 使用未来数据的模块：双侧审计、影响检验，以及结果标签与回调事件标签（阶段6）
AUDIT_MODULES = ("market_risk.data.price_review", "market_risk.data.price_review_inputs", "market_risk.price_impact",
                 "market_risk.outcomes", "market_risk.backtest.labels", "market_risk.backtest.zigzag",
                 "market_risk.backtest.report", "market_risk.research.analysis", "market_risk.research.io",
                 "market_risk.research.pullback", "market_risk.research.groups", "market_risk.research.features",
                 "market_risk.research.statistics", "market_risk.research.quality",
                 "market_risk.research.development_audit", "market_risk.research.zz_v121")


def test_scoring_path_does_not_load_audit_modules_transitively():
    """在干净的子进程中导入评分路径模块，确认 sys.modules 中没有审计模块（传递依赖检查）。"""
    import json
    import subprocess
    import sys

    code = ("import importlib, json, sys\n"
            f"for m in {list(SCORING_PATH_MODULES)!r}: importlib.import_module(m)\n"
            f"print(json.dumps([m for m in {list(AUDIT_MODULES)!r} if m in sys.modules] + "
            "[m for m in sys.modules if m.startswith('market_risk.wavewarn')]))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []


def test_wavewarn_live_state_does_not_load_labels_or_loss_transitively():
    """实时通道、特征与状态机不传递导入事后标签或损失。"""
    import json
    import subprocess
    import sys

    code = ("import importlib, json, sys\n"
            "for m in ('market_risk.wavewarn.features', 'market_risk.wavewarn.channels', "
            "'market_risk.wavewarn.state_machine', 'market_risk.wavewarn.diagnostics'): "
            "importlib.import_module(m)\n"
            "print(json.dumps([m for m in sys.modules if m.startswith('market_risk.wavewarn.labels_zz') "
            "or m.startswith('market_risk.wavewarn.loss') or m.startswith('market_risk.wavewarn.ledgers')]))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []


def test_wavewarn_and_original_research_do_not_import_each_other_transitively():
    """两个研究口径各自形成独立依赖图，避免隐式混用标签或 VIX3M 来源。"""
    import json
    import subprocess
    import sys

    for imported, forbidden in (("market_risk.wavewarn.development", "market_risk.research"),
                                ("market_risk.research.analysis", "market_risk.wavewarn"),
                                ("market_risk.research.zz_v121", "market_risk.wavewarn")):
        code = ("import importlib, json, sys\n"
                f"importlib.import_module({imported!r})\n"
                f"print(json.dumps([m for m in sys.modules if m.startswith({forbidden!r})]))\n")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert json.loads(out.stdout) == []
