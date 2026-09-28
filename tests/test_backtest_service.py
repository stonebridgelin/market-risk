"""回测服务：运行目录、正式回测指针与 .gitignore、rebuild-db、基础统计报告、zigzag-check 的隔离。只写临时目录。"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys

import pytest

from market_risk import services
from market_risk.config import PROJECT_ROOT, load_settings
from market_risk.storage import backtests, db
from market_risk.storage.paths import StoragePaths
from market_risk.storage.runs import GitInfo, _find_git

D = dt.date
CLEAN = GitInfo("b" * 40, False)


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    root = tmp_path_factory.mktemp("bt")
    shutil.copytree(PROJECT_ROOT / "data" / "market", root / "data" / "market")
    paths = StoragePaths(root)
    return services.Context(load_settings(), paths, db.default_url(paths))


@pytest.fixture(scope="module")
def run(ctx):
    return services.backtest_run(ctx, D(2018, 1, 2), D(2018, 4, 30), git=CLEAN,
                                 now=dt.datetime(2026, 9, 27, 12, tzinfo=dt.UTC))


def test_run_writes_all_files(ctx, run):
    names = {p.name for p in run.run_dir.iterdir()}
    assert names == {"meta.json", "README.md", "daily_scores.csv", "daily_metrics.csv", "outcomes.csv",
                     "pullback_episodes.csv", "episode_windows.csv"}
    meta = json.loads((run.run_dir / "meta.json").read_text("utf-8"))
    assert meta["git_commit"] == "b" * 40 and meta["market_manifest_sha256"] and meta["config"]["sha256"]
    assert meta["holdout_unlocked"] is False and meta["holdout_unlocked_at"] is None
    assert meta["periods"]["holdout_start"] == "2023-01-01" and meta["runtime_seconds"] >= 0
    scores = backtests.read_csv(run.run_dir / "daily_scores.csv")
    assert len(scores) == 2 * run.days and {r["version"] for r in scores} == {"v2-M", "v3-R1"}
    windows = backtests.read_csv(run.run_dir / "episode_windows.csv")
    assert "decline_progress" in windows[0] and "offset_from_trough" in windows[0]
    episodes = backtests.read_csv(run.run_dir / "pullback_episodes.csv")
    assert {e["level"] for e in episodes if e["symbol"] == "SPX"} == {"5", "10", "20"}
    assert {e["level"] for e in episodes if e["symbol"] == "QQQ"} == {"7", "10", "20"}
    # 保留期：高点在保留期的回调、结果窗口结束于保留期的标签、保留期的窗口行都不写出
    assert not any(e["period"] == "保留期" for e in episodes)
    assert all(r["window_end"] < "2023-01-01" for r in backtests.read_csv(run.run_dir / "outcomes.csv"))
    assert all(w["date"] < "2023-01-01" for w in windows)
    masked = [e for e in episodes if e["status"] == "跨入保留期，未解锁"]
    assert masked and all(not e["low_date"] and not e["drawdown_pct"] for e in masked)


def test_second_run_gets_new_directory(ctx, run):
    again = services.backtest_run(ctx, D(2018, 1, 3), D(2018, 1, 5), git=CLEAN,
                                  now=dt.datetime(2026, 9, 27, 12, tzinfo=dt.UTC))
    assert again.run_id != run.run_id and again.run_id.startswith(run.run_id)


def test_explicit_from_before_config_start_is_rejected(ctx):
    """L-14：显式起点不能静默改为配置起点。"""
    with pytest.raises(services.ServiceError, match="早于回测配置起点"):
        services.backtest_run(ctx, D(2008, 8, 8), D(2008, 8, 11), git=CLEAN)


def test_official_pointer_gitignore_and_rebuild(ctx, run):
    services.backtest_set_official(ctx, run.run_id)
    assert backtests.read_official(ctx.paths)["run_id"] == run.run_id
    text = ctx.paths.backtest_gitignore.read_text("utf-8")
    assert text.endswith(f"/*/\n!/{run.run_id}/\n")
    rows = db.dump(ctx.db_url)
    assert sum(1 for r in rows["backtest_daily_scores"] if r[0] == run.run_id) == 2 * run.days
    official = [r for r in rows["backtest_runs"] if r[0] == run.run_id]
    assert official


def test_gitignore_only_lets_official_run_through(ctx, run, tmp_path):
    """用 git check-ignore 验证：只有正式回测的运行目录被放行。"""
    git = _find_git()
    if git is None:
        pytest.skip("本机没有 git")
    repo = tmp_path / "repo"
    root = repo / "results" / "MARKET" / "risk_scoring" / "backtests"
    for name in (run.run_id, "run_20260101T000000Z_ccccccc"):
        (root / name).mkdir(parents=True)
        (root / name / "meta.json").write_text("{}", encoding="utf-8")
    (root / ".gitignore").write_text(backtests.gitignore_text(run.run_id), encoding="utf-8")
    (root / "official.json").write_text("{}", encoding="utf-8")
    subprocess.run([git, "init", "-q", str(repo)], check=True)

    def ignored(rel: str) -> bool:
        return subprocess.run([git, "check-ignore", "-q", rel], cwd=repo).returncode == 0

    base = "results/MARKET/risk_scoring/backtests"
    assert not ignored(f"{base}/{run.run_id}/meta.json")
    assert ignored(f"{base}/run_20260101T000000Z_ccccccc/meta.json")
    assert not ignored(f"{base}/official.json") and not ignored(f"{base}/.gitignore")


def test_report_uses_only_development_and_validation(ctx, run):
    report = services.backtest_report(ctx, run.run_id)
    assert report.path.exists() and "## 6. 标普500 5% 层级每年的回调次数" in report.text
    assert "保留期（2023-01-01 起）不参与任何统计" in report.text
    assert "2023 |" not in report.text


FORBIDDEN = ("market_risk.scoring", "market_risk.prepare", "market_risk.pipeline", "market_risk.data.snapshot",
             "market_risk.backtest.labels", "market_risk.backtest.engine", "market_risk.outcomes",
             "market_risk.metrics", "market_risk.storage.backtests")


def test_zigzag_check_does_not_touch_scores_or_labels(tmp_path):
    """zigzag-check 的代码路径不导入评分模块，不读取回测运行目录（只需要数据集中的收盘价）。"""
    root = tmp_path / "only_prices"
    daily = root / "data" / "market" / "daily"
    daily.mkdir(parents=True)
    shutil.copyfile(PROJECT_ROOT / "data" / "market" / "daily" / "SPX.csv", daily / "SPX.csv")
    code = (
        "import sys, json, datetime as dt\n"
        "from decimal import Decimal\n"
        "from pathlib import Path\n"
        "from market_risk.backtest.settings import load_backtest_config\n"
        "from market_risk.backtest.zigzag_check import price_swings\n"
        "from market_risk.storage.paths import StoragePaths\n"
        f"root = Path(r'{root}')\n"
        "grades = load_backtest_config().grades['SPX']\n"
        "rows = price_swings(StoragePaths(root), 'SPX', Decimal('0.05'), grades, dt.date(2025, 8, 1),"
        " dt.date(2026, 9, 25))\n"
        f"bad = [m for m in sys.modules if m.startswith({FORBIDDEN!r})]\n"
        "print(json.dumps({'n': len(rows), 'bad': bad}))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result["bad"] == [] and result["n"] >= 2
