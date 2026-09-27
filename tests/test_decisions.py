"""已裁定日期表（config/data_decisions.yaml）与债市休市日 OAS 统一规则（SPEC 5.6 第10、12条）。"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import synthetic_raw

from market_risk.config import ConfigError, DataDecision, load_data_decisions
from market_risk.data.raw_io import load_raw_inputs, save_raw_inputs
from market_risk.data.snapshot import build_snapshot
from market_risk.scoring import v2m, v3r1

D = dt.date
MLK = D(2015, 1, 19)


def raw_2015(**kw):
    """基准日 2015-01-21：01-19 债市休市（财政部无数据），OAS 在该日数值 5.39 与前一观测 5.42 不同。"""
    raw = synthetic_raw(D(2015, 1, 21), treasury_overrides={MLK: None},
                        oas_overrides={D(2015, 1, 16): 5.42, MLK: 5.39})
    return dataclasses.replace(raw, **kw)


def decision(kind: str) -> DataDecision:
    return DataDecision(MLK, "BAMLH0A0HYM2", kind, "测试理由", D(2026, 9, 27))


def test_undecided_general_holiday_excluded_by_rule():
    """未裁定的一般债市休市日（非月末）：按规则排除，两个版本都不计入，不再提示需人工判断。"""
    snap = build_snapshot(raw_2015())
    assert not any("需人工判断" in n for n in snap.data_notes)
    assert any("OAS 2015-01-19（债市休市日）" in n and "v2-M 与 v3-R1 都不计入" in n for n in snap.data_notes)
    assert snap.refs.oas_o1_v2m == D(2015, 1, 20)
    assert snap.refs.oas_o6_v2m == D(2015, 1, 12)   # 01-19 不计为观测


def test_exclude_decision_removes_observation_for_both_versions():
    base = build_snapshot(raw_2015())
    snap = build_snapshot(raw_2015(decisions=(decision("exclude"),)))
    assert not any("需人工判断" in n for n in snap.data_notes)
    assert any("OAS 2015-01-19 按已裁定日期表排除" in n and "测试理由" in n for n in snap.data_notes)
    # 2015-01-19 的裁定与统一规则一致：两者都不把 01-19 计为观测
    assert base.refs.oas_o6_v2m == snap.refs.oas_o6_v2m == D(2015, 1, 12)
    # v3-R1 按债市营业日计数，01-19 本来就不计入
    assert snap.refs.oas_o6_v3r1 == base.refs.oas_o6_v3r1
    assert v2m.score_credit(snap).score is not None and v3r1.score_credit(snap).score is not None


def test_keep_decision_suppresses_prompt():
    snap = build_snapshot(raw_2015(decisions=(decision("keep"),)))
    assert not any("需人工判断" in n for n in snap.data_notes)
    assert any("按已裁定日期表保留" in n for n in snap.data_notes)
    assert snap.refs.oas_o6_v2m == D(2015, 1, 13)   # keep：按裁定计入 v2-M


def test_decision_for_other_symbol_is_ignored():
    other = dataclasses.replace(decision("exclude"), symbol="BAMLC0A0CM")
    snap = build_snapshot(raw_2015(decisions=(other,)))
    assert any("OAS 2015-01-19（债市休市日）" in n and "按规则排除" in n for n in snap.data_notes)


def test_config_contains_mlk_2015_and_validates(tmp_path):
    decisions = load_data_decisions()
    assert any(d.date == MLK and d.symbol == "BAMLH0A0HYM2" and d.decision == "exclude" for d in decisions)
    assert load_data_decisions(tmp_path / "missing.yaml") == ()
    bad = tmp_path / "d.yaml"
    bad.write_text("- {date: 2015-01-19, symbol: X, decision: drop, decided_on: 2026-09-27}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="decision"):
        load_data_decisions(bad)
    bad.write_text("- {date: 2015-01-19, symbol: X, decision: keep, decided_on: 2026-09-27}\n" * 2,
                   encoding="utf-8")
    with pytest.raises(ConfigError, match="重复"):
        load_data_decisions(bad)
    bad.write_text("- {date: 2015-01-19}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_data_decisions(bad)


def test_raw_inputs_roundtrip_keeps_decisions(tmp_path):
    raw = raw_2015(decisions=(decision("exclude"),), oas_symbol="BAMLH0A0HYM2")
    save_raw_inputs(raw, tmp_path / "r")
    again = load_raw_inputs(tmp_path / "r")
    assert again.decisions == raw.decisions and again.oas_symbol == "BAMLH0A0HYM2"


# ---- 统一规则的例外：债市休市日恰为自然月末（SPEC 5.6 第10条）----

MEMORIAL_2021 = D(2021, 5, 31)


def raw_2021_month_end():
    """基准日 2021-06-01：05-31 阵亡将士纪念日（债市休市）恰为自然月末，OAS 3.34 与 05-28 的 3.29 不同。"""
    return synthetic_raw(D(2021, 6, 1), treasury_overrides={MEMORIAL_2021: None},
                         oas_overrides={D(2021, 5, 28): 3.29, MEMORIAL_2021: 3.34})


def test_month_end_bond_holiday_counted_by_v2m():
    snap = build_snapshot(raw_2021_month_end())
    assert (snap.refs.oas_o1_v2m, snap.oas_o1_v2m) == (MEMORIAL_2021, 3.34)
    assert snap.refs.oas_o6_v2m == D(2021, 5, 24)
    assert snap.refs.o1_v2m_lag_stock_days == 1
    assert any("恰为自然月末" in n and "v2-M 计入，v3-R1 不计入" in n for n in snap.data_notes)
    assert not any("需人工判断" in n for n in snap.data_notes)


def test_month_end_bond_holiday_not_counted_by_v3r1():
    snap = build_snapshot(raw_2021_month_end())
    assert (snap.refs.oas_o1, snap.oas_o1) == (D(2021, 5, 28), 3.29)
    assert snap.refs.oas_o6_v3r1 == D(2021, 5, 21)
    assert MEMORIAL_2021 not in snap.refs.oas_o1_to_o6_sequence
