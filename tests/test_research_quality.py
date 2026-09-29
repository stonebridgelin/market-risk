"""开发期前瞻特征缺值审计：所有期望由构造依赖手推。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.research.development_audit import _read_dev_csv, _reason_detail, _replace_block
from market_risk.research.features import FeatureEngine
from market_risk.research.io import ResearchInputs, _values
from market_risk.research.quality import feature_quality


def _engine() -> FeatureEngine:
    first = dt.date(2010, 1, 4)
    today = dt.date(2010, 1, 5)
    inputs = ResearchInputs("constructed", {}, {}, (), (), (), 0,
                            {"S5TW": {first: Decimal("50")},
                             "R2FI": {dt.date(2013, 10, 14): Decimal("45")}},
                            {"PCCE": {first: Decimal("1")}}, (first, today))
    days = (first, today)
    return FeatureEngine(inputs, days, {day: i for i, day in enumerate(days)}, (), {})


def test_missing_reasons_keep_all_causes_and_exclusive_primary_by_hand() -> None:
    engine = _engine()
    first, today = engine.days
    # R2FI 尚未开始，十日窗口也不足；按已确认优先级，以“序列未开始”为互斥主因。
    early = feature_quality(engine, "R2FI_change_10", first, None)
    assert early.all_reasons == ("序列未开始", "窗口不足")
    assert early.primary_reason == "序列未开始"
    # 2010-01-04 的 S5TW 有读数且处在截至2010-10-01的单值阶段，只标质量风险。
    available = feature_quality(engine, "S5TW", first, Decimal("50"))
    assert available.missing is False
    assert available.single_value_stage is True
    assert available.primary_reason is None
    # 次日 S5TW 缺价，序列已开始；缺口与单值阶段同时记录，互斥主因为“缺口”。
    gap = feature_quality(engine, "S5TW", today, None)
    assert gap.all_reasons == ("缺口", "单值阶段")
    assert gap.primary_reason == "缺口"
    # PCCE 有首日读数，但十日均值仅有2个交易日可回看，归为窗口不足。
    short = feature_quality(engine, "PCCE_ma10", today, None)
    assert short.all_reasons == ("窗口不足",)
    assert short.primary_reason == "窗口不足"


def test_development_loader_stops_before_later_rows_and_rejects_later_observations(tmp_path: Path) -> None:
    series = tmp_path / "series.csv"
    series.write_text("date,value\n2016-12-30,2\n2017-01-03,BAD\n", encoding="utf-8")
    # 截止开发期的读取在2017首行立即停止，不解析其非法数值。
    assert _values(series, end=dt.date(2016, 12, 30)) == {dt.date(2016, 12, 30): Decimal("2")}
    observed = tmp_path / "observed.csv"
    observed.write_text("dates\n2016-12-30|2017-01-03\n", encoding="utf-8")
    with pytest.raises(ValueError, match="拒绝读取验证期观测"):
        _read_dev_csv(observed, "dates")


def test_development_report_block_replacement_is_idempotent() -> None:
    original = "## 第3节\n旧开发期内容\n\n## 第4节 数据质量与局限\n旧质量内容\n"
    once = _replace_block(original, "## 第4节 数据质量与局限", "### 新增\n数值1\n")
    twice = _replace_block(once, "## 第4节 数据质量与局限", "### 新增\n数值1\n")
    assert once == twice
    assert "旧开发期内容" in twice and "旧质量内容" in twice


def test_pending_score_detail_distinguishes_bond_holiday_by_hand() -> None:
    engine = _engine()
    today = engine.days[-1]
    engine.inputs.scores[today, "v2-M"] = {"rates": "待补[0, 1, 2]"}
    # 同一个待补字段按日期是否属于债市休市集合添加细节；主因仍由缺值审计归为“缺口”。
    assert _reason_detail(engine, "v2m_rates", today, frozenset({today})) == "评分维度待补；债市休市日"
    assert _reason_detail(engine, "v2m_rates", today, frozenset()) == "评分维度待补"
