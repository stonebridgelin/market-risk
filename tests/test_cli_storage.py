"""阶段4.5 命令行测试：存储根目录改为临时目录。"""

from __future__ import annotations

import dataclasses
import shutil

import pytest
from test_storage import EXCEL, program_runs
from typer.testing import CliRunner

import market_risk.cli as cli
from market_risk.config import load_settings
from market_risk.storage import runs
from market_risk.storage.paths import MARKET, RISK_SCORING, StoragePaths


@pytest.fixture()
def env(tmp_path, monkeypatch):
    settings = dataclasses.replace(load_settings(), storage_root=tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(runs, "git_info", lambda repo: runs.GitInfo("c" * 40, False))
    return StoragePaths(tmp_path), CliRunner()


def test_official_and_import_legacy_and_stats(env):
    paths, runner = env
    import datetime as dt

    prog = program_runs(paths)
    r = runner.invoke(cli.app, ["official", "set", "--date", "2025-11-28", "--run", prog["2025-11-28"]])
    assert r.exit_code == 0 and "reviewed=false" in r.stdout
    r = runner.invoke(cli.app, ["official", "confirm", "--date", "2025-11-28"])
    assert r.exit_code == 0 and "reviewed=true" in r.stdout
    assert runner.invoke(cli.app, ["official", "set", "--date", "2025-11-28", "--run", "run_x"]).exit_code != 0
    assert runner.invoke(cli.app, ["official", "confirm", "--date", "2025-12-26"]).exit_code == 1

    paths.legacy_excel.parent.mkdir(parents=True)
    shutil.copyfile(EXCEL, paths.legacy_excel)
    r = runner.invoke(cli.app, ["import-legacy"])
    assert r.exit_code == 0, r.output
    assert "截图记录与程序记录的维度分数全部一致" in r.stdout and "全部与 Excel 缓存值一致" in r.stdout
    pointer = runs.read_official(paths, MARKET, RISK_SCORING, dt.date(2025, 11, 28))
    assert pointer["set_by"] == "import-legacy"

    r = runner.invoke(cli.app, ["outcome", "add", "--date", "2025-10-31", "--spx", "-5.5", "--qqq", "-8"])
    assert r.exit_code == 0 and "风险事件=是" in r.stdout
    r = runner.invoke(cli.app, ["rebuild-db"])
    assert r.exit_code == 0 and "runs 8" in r.stdout and "officials 4" in r.stdout and "outcomes 1" in r.stdout
    r = runner.invoke(cli.app, ["stats"])
    assert r.exit_code == 0 and "不确定" in r.stdout and paths.backtest_history_xlsx.exists()


def test_import_legacy_stops_on_score_difference(env):
    import datetime as dt
    import json

    paths, runner = env
    prog = program_runs(paths)
    p = paths.run_dir(MARKET, RISK_SCORING, dt.date(2025, 8, 29), prog["2025-08-29"]) / "scores.json"
    s = json.loads(p.read_text("utf-8"))
    s["results"][0]["credit"]["score"] = 1
    p.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    paths.legacy_excel.parent.mkdir(parents=True)
    shutil.copyfile(EXCEL, paths.legacy_excel)
    r = runner.invoke(cli.app, ["import-legacy"])
    assert r.exit_code == 3 and "【需用户判断】" in r.stdout and "v2-M 信用：截图 0，程序 1" in r.stdout


def test_material_breadth_and_outcome_commands(env, tmp_path):
    _, runner = env
    f = tmp_path / "gpt.md"
    f.write_text("回复", encoding="utf-8")
    r = runner.invoke(cli.app, ["material", "add", "--date", "2025-11-28", "--type", "chatgpt_response",
                                "--file", str(f), "--source", "ChatGPT"])
    assert r.exit_code == 0
    r = runner.invoke(cli.app, ["material", "list", "--from", "2025-11-01"])
    assert "chatgpt_response" in r.stdout
    assert "没有资料" in runner.invoke(cli.app, ["material", "list", "--from", "2026-01-01"]).stdout
    assert runner.invoke(cli.app, ["material", "add", "--date", "2025-11-28", "--type", "bad",
                                   "--file", str(f)]).exit_code == 1

    add = ["breadth", "add", "--date", "2025-12-01", "--s5fi", "60", "--s5tw", "70"]
    assert runner.invoke(cli.app, add).exit_code == 0
    r = runner.invoke(cli.app, ["breadth", "add", "--date", "2025-12-01", "--s5fi", "60", "--s5tw", "70"])
    assert "无变更" in r.stdout
    r = runner.invoke(cli.app, ["breadth", "add", "--date", "2025-12-01", "--s5fi", "61", "--s5tw", "70"])
    assert r.exit_code == 1 and "--overwrite" in r.output
    r = runner.invoke(cli.app, ["breadth", "add", "--date", "2025-12-01", "--s5fi", "61", "--s5tw", "70",
                                "--overwrite"], input="n\n")
    assert r.exit_code == 1
    r = runner.invoke(cli.app, ["breadth", "add", "--date", "2025-12-01", "--s5fi", "61", "--s5tw", "70",
                                "--overwrite", "--yes"])
    assert r.exit_code == 0 and "已写入" in r.stdout

    r = runner.invoke(cli.app, ["outcome", "add", "--date", "2099-01-02", "--spx", "-1", "--qqq", "-1"])
    assert r.exit_code == 1 and "尚未结束" in r.output
