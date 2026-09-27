"""统一的路径生成（docs/STORAGE.md 第2、8节）。

本模块只计算路径，不读写文件、不创建目录。其他模块不得自行拼接存储路径。
路径由 subject（MARKET 或股票代码）、framework、base_date 决定。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MARKET = "MARKET"
RISK_SCORING = "risk_scoring"
CACHE_SOURCES = ("yahoo", "fred", "treasury", "cboe", "tiingo")

_SUBJECT_RE = re.compile(r"^[A-Z0-9]{1,10}([.\-][A-Z0-9]{1,5})?$")  # 如 NVDA、BRK.B
_TV_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,39}$")  # 如 S5FI、ES1_、US10Y
_FRAMEWORK_RE =re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9]+([_.\-][A-Za-z0-9]+)*$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_RUN_ID_RE = re.compile(r"^run_\d{8}T\d{6}Z_[0-9a-f]{7}(_\d+)?$")


class PathError(ValueError):
    """路径参数不合法。"""


def normalize_subject(subject: str) -> str:
    """对象名：股票代码一律大写，大盘统一为 MARKET。"""
    s = subject.strip().upper()
    if not _SUBJECT_RE.match(s):
        raise PathError(f"对象名不合法：{subject!r}")
    return s


def _check_framework(framework: str) -> str:
    if not _FRAMEWORK_RE.match(framework):
        raise PathError(f"框架名不合法（小写字母、数字、下划线）：{framework!r}")
    return framework


def _check_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.match(run_id):
        raise PathError(f"运行编号不合法：{run_id!r}")
    return run_id


def make_run_id(created_at: dt.datetime, git_commit: str) -> str:
    """运行编号：run_<UTC 时间 YYYYMMDDTHHMMSSZ>_<commit 前7位>。created_at 必须带时区。"""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise PathError("运行时间必须带时区")
    commit = git_commit.strip().lower()
    if not _COMMIT_RE.match(commit):
        raise PathError(f"git commit 不合法：{git_commit!r}")
    utc = created_at.astimezone(dt.UTC)
    return f"run_{utc:%Y%m%dT%H%M%SZ}_{commit[:7]}"


def unique_run_id(run_id: str, existing: Iterable[str]) -> str:
    """同一秒内重复运行时依次加后缀 _2、_3……"""
    _check_run_id(run_id)
    taken = set(existing)
    if run_id not in taken:
        return run_id
    n = 2
    while f"{run_id}_{n}" in taken:
        n += 1
    return f"{run_id}_{n}"


@dataclass(frozen=True)
class StoragePaths:
    """以 root 为根的全部存储路径。"""

    root: Path

    # ---- data/cache ----
    def cache_dir(self, source: str) -> Path:
        if source not in CACHE_SOURCES:
            raise PathError(f"未知的缓存来源：{source!r}，应为 {CACHE_SOURCES}")
        return self.root / "data" / "cache" / source

    def cache_file(
        self, source: str, key: str, start: dt.date, end: dt.date, suffix: str = ".csv"
    ) -> Path:
        """缓存文件：<来源>/<代码>_<起>_<止><后缀>；文件名包含数据源、代码、日期范围。"""
        if not _CACHE_KEY_RE.match(key):
            raise PathError(f"缓存代码不合法：{key!r}")
        return self.cache_dir(source) / f"{key}_{start.isoformat()}_{end.isoformat()}{suffix}"

    @staticmethod
    def cache_meta_file(data_file: Path) -> Path:
        """缓存文件旁的元数据（来源 URL、下载时间、行数）。"""
        return data_file.with_name(data_file.name + ".meta.json")

    # ---- data/manual、data/legacy ----
    @property
    def manual_dir(self) -> Path:
        return self.root / "data" / "manual"

    @property
    def breadth_csv(self) -> Path:
        return self.manual_dir / "breadth.csv"

    @property
    def outcomes_csv(self) -> Path:
        return self.manual_dir / "outcomes.csv"

    @property
    def reviews_csv(self) -> Path:
        """复核记录（ChatGPT / Claude / 本人 / 截图与程序对照），数据库 reviews 表由此重建。"""
        return self.manual_dir / "reviews.csv"

    @property
    def materials_index_csv(self) -> Path:
        """资料索引，数据库 materials 表由此重建。"""
        return self.root / "data" / "materials" / "index.csv"

    @property
    def results_root(self) -> Path:
        return self.root / "results"

    @property
    def legacy_excel(self) -> Path:
        return self.root / "data" / "legacy" / "backtest_record_legacy.xlsx"

    # ---- TradingView 导出数据（docs/TRADINGVIEW.md 第2节）----
    @property
    def tv_raw_root(self) -> Path:
        """原始导出文件根目录（只读，提交 git）。"""
        return self.manual_dir / "tradingview" / "raw"

    def tv_raw_dir(self, export_date: dt.date) -> Path:
        return self.tv_raw_root / export_date.isoformat()

    @property
    def tv_manifest(self) -> Path:
        return self.manual_dir / "tradingview" / "manifest.csv"

    @property
    def tv_processed_dir(self) -> Path:
        """清洗后的数据（不提交 git，可由原始文件重建）。"""
        return self.root / "data" / "processed" / "tradingview"

    def tv_processed_file(self, symbol: str) -> Path:
        if not _TV_SYMBOL_RE.match(symbol):
            raise PathError(f"TradingView 标的名不合法：{symbol!r}")
        return self.tv_processed_dir / f"{symbol}.csv"

    # ---- data/materials ----
    def materials_dir(self, subject: str, base_date: dt.date) -> Path:
        return (
            self._subject_dir(self.root / "data" / "materials", subject)
            / f"{base_date.year}"
            / base_date.isoformat()
        )

    # ---- results ----
    def results_date_dir(self, subject: str, framework: str, base_date: dt.date) -> Path:
        return (
            self._subject_dir(self.root / "results", subject)
            / _check_framework(framework)
            / f"{base_date.year}"
            / base_date.isoformat()
        )

    def official_pointer(self, subject: str, framework: str, base_date: dt.date) -> Path:
        return self.results_date_dir(subject, framework, base_date) / "official.json"

    def run_dir(self, subject: str, framework: str, base_date: dt.date, run_id: str) -> Path:
        return self.results_date_dir(subject, framework, base_date) / _check_run_id(run_id)

    def run_inputs_dir(
        self, subject: str, framework: str, base_date: dt.date, run_id: str
    ) -> Path:
        return self.run_dir(subject, framework, base_date, run_id) / "inputs"

    # ---- db、reports ----
    @property
    def db_path(self) -> Path:
        return self.root / "db" / "market_risk.sqlite"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def backtest_history_xlsx(self) -> Path:
        return self.reports_dir / "backtest_history.xlsx"

    @property
    def backtest_stats_md(self) -> Path:
        return self.reports_dir / "backtest_stats.md"

    @property
    def tv_crosscheck_md(self) -> Path:
        return self.reports_dir / "tradingview_crosscheck.md"

    @property
    def holiday_audit_md(self) -> Path:
        return self.reports_dir / "holiday_calendar_audit.md"

    @property
    def tv_quality_md(self) -> Path:
        return self.reports_dir / "tradingview_data_quality.md"

    def tv_compare_md(self, symbol: str) -> Path:
        if not _TV_SYMBOL_RE.match(symbol):
            raise PathError(f"TradingView 标的名不合法：{symbol!r}")
        return self.reports_dir / f"tradingview_compare_{symbol}.md"

    def daily_report_csv(self, year: int, month: int) -> Path:
        if not 1 <= month <= 12:
            raise PathError(f"月份不合法：{month}")
        return self.reports_dir / "daily" / f"{year:04d}-{month:02d}.csv"

    # ---- 内部 ----
    @staticmethod
    def _subject_dir(base: Path, subject: str) -> Path:
        s = normalize_subject(subject)
        return base / MARKET if s == MARKET else base / "stocks" / s


# 运行目录内的固定文件名（STORAGE 第2节）
RUN_FILES = {
    "meta": "meta.json",
    "dates": "dates.json",
    "snapshot": "snapshot.json",
    "scores": "scores.json",
    "three_segment": "three_segment.csv",
    "prompt": "prompt.md",
    "summary": "summary.md",
    "metrics": "metrics.json",       # 扁平的数值指标（数据库 metrics 表）
    "legacy": "legacy.json",         # import-legacy：Excel 中该样本的原始单元格
}
INPUT_FILES = {
    "daily_data": "daily_data.csv",
    "fred_observations": "fred_observations.csv",
    "treasury_yields": "treasury_yields.csv",
    "breadth": "breadth.csv",
}
