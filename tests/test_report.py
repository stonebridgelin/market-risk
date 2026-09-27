"""输出与运行目录测试（SPEC 第7节、STORAGE 第2、3节）。只写入临时目录。"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json

import pytest
from conftest import load_sample_raw
from typer.testing import CliRunner

from market_risk.config import load_holidays, load_settings
from market_risk.models import BreadthReading
from market_risk.pipeline import run_scoring
from market_risk.report import extract_sop_section, load_rules
from market_risk.storage import runs
from market_risk.storage.paths import MARKET, RISK_SCORING, StoragePaths

D = dt.date
BASE = D(2025, 11, 28)
NOW = dt.datetime(2026, 9, 27, 2, 15, 30, tzinfo=dt.UTC)
CLEAN = runs.GitInfo("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", False)
DIRTY = runs.GitInfo("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", True)
SETTINGS = load_settings()


@pytest.fixture()
def paths(tmp_path):
    return StoragePaths(tmp_path)


def raw4():
    return load_sample_raw("2025-11-28", {BASE: BreadthReading(BASE, 58.44, 76.73)})


def test_extract_sop_sections_verbatim():
    rules = load_rules()
    assert rules["7.1"].startswith("- 每个维度先判2分，再判0分")
    assert "**大盘明确恶化**" in rules["7.1"]
    assert rules["7.2"].startswith("**一、价格（0-2）**")
    assert "若 O1 滞后基准日超过1个股票交易日，最高只计1分。" in rules["7.2"]
    assert rules["7.3"].startswith("**一、价格（0-2）**")
    assert "7.4" not in rules["7.3"] and "四处实质修改" not in rules["7.3"]
    with pytest.raises(ValueError):
        extract_sop_section("## 1. x\n", "9.9")


def test_full_run_outputs(paths):
    out = run_scoring(raw4(), SETTINGS, paths, CLEAN, load_holidays(), now=NOW)
    assert out.run_id == "run_20260927T021530Z_a1b2c3d"
    assert out.run_dir == paths.run_dir(MARKET, RISK_SCORING, BASE, out.run_id)
    assert out.status == "complete"
    for name in ("meta.json", "dates.json", "snapshot.json", "scores.json", "three_segment.csv",
                 "prompt.md", "summary.md", "inputs/daily_data.csv", "inputs/fred_observations.csv",
                 "inputs/treasury_yields.csv", "inputs/breadth.csv", "inputs/raw/meta.json"):
        assert (out.run_dir / name).exists(), name

    meta = json.loads((out.run_dir / "meta.json").read_text("utf-8"))
    assert meta["created_at_utc"] == "2026-09-27T02:15:30+00:00"
    assert "created_at_local" in meta and meta["status"] == "complete"
    assert meta["git_commit"].startswith("a1b2c3d") and meta["git_dirty"] is False
    assert meta["data_source_type"] == "api" and meta["rule_versions"] == ["v2-M", "v3-R1"]
    assert meta["config"]["three_segment.d1_includes_t_minus_20"] is True
    assert {s["source"] for s in meta["sources"]} >= {"yahoo", "fred", "treasury", "cboe"}

    dates = json.loads((out.run_dir / "dates.json").read_text("utf-8"))
    assert dates["oas_o1"] == "2025-11-26" and dates["t_minus_5"] == "2025-11-20"
    scores = json.loads((out.run_dir / "scores.json").read_text("utf-8"))
    assert [r["version"] for r in scores["results"]] == ["v2-M", "v3-R1"]
    assert scores["results"][0]["total"] == 0 and "true" in scores["three_segment"]
    snap = json.loads((out.run_dir / "snapshot.json").read_text("utf-8"))
    assert "closes" not in snap["etfs"]["SPY"] and "three_segment" not in snap

    with (out.run_dir / "inputs" / "daily_data.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["date"] == "2025-09-25" and rows[-1]["date"] == "2025-11-28"   # T−45 至基准日
    assert rows[-1]["SPY_close"] == "683.39" and rows[-1]["oas"] == ""            # 基准日 OAS 不使用
    assert abs(float(rows[-1]["SPY_ma50"]) - 670.44) <= 0.01
    assert "HYG_close" in rows[0]
    with (out.run_dir / "three_segment.csv").open(encoding="utf-8") as f:
        ts = list(csv.DictReader(f))
    assert len(ts) == 3 * 19 + 3 * 18   # 两种口径的完整遍历

    # 首次运行、工作区干净：自动设为正式记录，reviewed=false
    assert out.official_set
    pointer = runs.read_official(paths, MARKET, RISK_SCORING, BASE)
    assert pointer["run_id"] == out.run_id and pointer["reviewed"] is False
    assert pointer["set_by"] == "auto"


def test_prompt_content(paths):
    out = run_scoring(raw4(), SETTINGS, paths, CLEAN, now=NOW)
    prompt = (out.run_dir / "prompt.md").read_text("utf-8")
    assert prompt.startswith("# 美股大盘风险评分历史回测：v2-M 与 v3-R1 并行")
    assert "2025-11-28（周五）" in prompt
    for section in ("## 一、回测隔离", "## 二、时点口径", "## 三、数据说明", "## 四、日期对照",
                    "## 五、数据核对行", "## 六、共同规则", "## 七、规则A：v2-M", "## 八、规则B：v3-R1",
                    "## 九、输出格式"):
        assert section in prompt, section
    assert "**不得联网**" in prompt and "只使用本 prompt 提供的数据独立复核" in prompt
    assert "| SPY | 683.39 | 673.17 | 672.90 | 670.44 | 616.06 |" in prompt
    assert "| SPY | 2025-11-17 | 665.67 | 667.80 | 2025-10-22 | 是 |" in prompt  # 遍历表
    assert "| 2025-09-25 |" in prompt and "| 2025-11-28 | 683.39 | 619.25 | 191.53 |" in prompt
    assert "S5FI 58.44%；S5TW 76.73%" in prompt
    assert "O1=2025-11-26" in prompt and "O6=2025-11-19" in prompt
    assert "2025-11-27 股债均休市" in prompt and "2025-11-11 债市休市、股市开市" in prompt
    assert "提前收盘日" in prompt
    rules = load_rules()
    assert rules["7.2"] in prompt and rules["7.3"] in prompt and rules["7.1"] in prompt
    assert "[ ]" not in prompt
    assert "outcome" not in prompt.lower()
    assert "风险事件标签" not in prompt.replace("不计算基准日之后的风险事件标签", "")


def test_daily_mode_prompt_and_summary(paths):
    raw = dataclasses.replace(raw4(), mode="daily")
    out = run_scoring(raw, SETTINGS, paths, CLEAN, now=NOW)
    prompt = (out.run_dir / "prompt.md").read_text("utf-8")
    assert prompt.startswith("# 美股大盘风险评分（每日前瞻）")
    assert "本日是否为本周最后一个交易日：是（计入检验样本）" in prompt
    assert "本日是否为本周最后一个交易日：是" in (out.run_dir / "summary.md").read_text("utf-8")


def test_summary_content(paths):
    out = run_scoring(raw4(), SETTINGS, paths, CLEAN, now=NOW)
    summary = (out.run_dir / "summary.md").read_text("utf-8")
    assert "| **总分（阶段）** | 0（早期信号） | 0（早期信号） |" in summary
    assert "| 大盘明确恶化 | 否 | 否 |" in summary
    assert "SPY MA5 vs MA50" in summary
    assert "已自动设为正式记录" in summary


def test_second_run_keeps_both_and_official_unchanged(paths):
    first = run_scoring(raw4(), SETTINGS, paths, CLEAN, now=NOW)
    second = run_scoring(raw4(), SETTINGS, paths, CLEAN, now=NOW)   # 同一秒
    assert second.run_id == first.run_id + "_2"
    assert runs.list_runs(paths, MARKET, RISK_SCORING, BASE) == [first.run_id, second.run_id]
    assert not second.official_set and "已有正式记录" in second.official_note
    assert runs.read_official(paths, MARKET, RISK_SCORING, BASE)["run_id"] == first.run_id


def test_dirty_worktree_not_auto_official(paths):
    out = run_scoring(raw4(), SETTINGS, paths, DIRTY, now=NOW)
    assert not out.official_set and "未提交的修改" in out.official_note
    meta = json.loads((out.run_dir / "meta.json").read_text("utf-8"))
    assert meta["git_dirty"] is True and meta["warnings"]


def test_pending_run_and_breadth_message(paths):
    raw = load_sample_raw("2025-10-31", {D(2025, 10, 31): BreadthReading(D(2025, 10, 31), 40.15, 38.56)})
    out = run_scoring(raw, SETTINGS, paths, CLEAN, now=NOW)
    assert out.status == "pending"
    assert "需要补录 2025-10-24 的 S5FI、S5TW 读数" in out.messages
    assert out.official_set   # pending 也可自动设为正式记录
    summary = (out.run_dir / "summary.md").read_text("utf-8")
    assert "待补（可能 1/2）" in summary


def test_failed_run_writes_meta(paths):
    raw = dataclasses.replace(raw4(), closes={k: v for k, v in raw4().closes.items() if k != "RSP"})
    with pytest.raises(Exception, match="缺少 RSP"):
        run_scoring(raw, SETTINGS, paths, CLEAN, now=NOW)
    (run_id,) = runs.list_runs(paths, MARKET, RISK_SCORING, BASE)
    meta = json.loads((paths.run_dir(MARKET, RISK_SCORING, BASE, run_id) / "meta.json").read_text("utf-8"))
    assert meta["status"] == "failed" and "缺少 RSP" in meta["error"]
    assert runs.read_official(paths, MARKET, RISK_SCORING, BASE) is None


def test_official_set_and_confirm(paths):
    first = run_scoring(raw4(), SETTINGS, paths, DIRTY, now=NOW)
    assert runs.read_official(paths, MARKET, RISK_SCORING, BASE) is None
    runs.set_official(paths, MARKET, RISK_SCORING, BASE, first.run_id, "manual", now=NOW)
    pointer = runs.confirm_official(paths, MARKET, RISK_SCORING, BASE, now=NOW)
    assert pointer["reviewed"] is True and pointer["set_by"] == "manual"
    with pytest.raises(FileNotFoundError):
        runs.set_official(paths, MARKET, RISK_SCORING, BASE, "run_20260101T000000Z_0000000", "manual")
    with pytest.raises(FileNotFoundError):
        runs.confirm_official(paths, MARKET, RISK_SCORING, D(2025, 10, 31))


def test_cli_validate_and_samples():
    from market_risk.cli import app

    r = CliRunner().invoke(app, ["validate"])
    assert r.exit_code == 0, r.output
    assert "不一致 0" in r.stdout and "SPY MA50" in r.stdout
    r = CliRunner().invoke(app, ["samples", "--year", "2020"])
    assert "2020-12-31（周四）（最后一个周五休市，取当月最后一个交易日）" in r.stdout
