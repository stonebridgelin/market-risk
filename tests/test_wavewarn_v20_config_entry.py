"""v2.0 配置读取入口（阶段三实施指令第一节第 3 部分、第二节第 5 部分）：构造文件读写测试。

在 pytest 的临时目录下按仓库布局构造 config/wavewarn_v20.yaml 与 config/data_decisions.yaml；
不读取真实项目配置。登记值是构造的，不是登记第零节的真实数值。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk import config as project_config
from market_risk.wavewarn_v20 import config_v20
from market_risk.wavewarn_v20.config_v20 import V20ConfigError, load_v20_config

D = dt.date
DATASET = """\
dataset:
  SPX:
    raw_sha256: {a}
    normalized_sha256: {b}
    data_rows: 40
    first_date: 2001-01-02
    last_date: 2001-02-28
  QQQ:
    raw_sha256: {c}
    normalized_sha256: {c}
    data_rows: 20
    first_date: 2001-01-31
    last_date: 2001-02-28
""".format(a="a" * 64, b="b" * 64, c="c" * 64)
DECISIONS = """\
- date: 2001-01-22
  symbol: QQQ
  decision: correct
  reason: 构造的修正条目
  decided_on: 2001-06-01
  corrected_value: 43.31
  evidence_source: 构造的证据
- date: 2001-01-23
  symbol: SPX
  decision: exclude
  reason: 构造
  decided_on: 2001-06-02
- date: 2001-01-24
  symbol: SPY
  decision: keep
  reason: 构造
  decided_on: 2001-06-03
- date: 2001-01-25
  symbol: SPX
  decision: invalid
  reason: 构造
  decided_on: 2001-06-04
"""


def write(root: Path, name: str, text: str) -> Path:
    """把构造的配置文本写进临时目录里的 config/ 之下。"""
    path = root / "config" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def layout(root: Path, dataset: str | None = DATASET, decisions: str | None = DECISIONS) -> Path:
    if dataset is not None:
        write(root, "wavewarn_v20.yaml", dataset)
    if decisions is not None:
        write(root, "data_decisions.yaml", decisions)
    return root


def test_loads_registered_values_and_all_decisions(tmp_path: Path) -> None:
    config = load_v20_config(layout(tmp_path))
    assert set(config.registered) == {"SPX", "QQQ"}
    spx = config.registered["SPX"]
    assert (spx.asset, spx.raw_sha256, spx.normalized_sha256) == ("SPX", "a" * 64, "b" * 64)
    assert (spx.data_rows, spx.first_date, spx.last_date) == (40, D(2001, 1, 2), D(2001, 2, 28))
    assert config.registered["QQQ"].first_date == D(2001, 1, 31)
    # 保留全部裁定类型，不在这里筛选。
    assert [(item.symbol, item.date, item.decision) for item in config.decisions] == [
        ("QQQ", D(2001, 1, 22), "correct"), ("SPX", D(2001, 1, 23), "exclude"), ("SPY", D(2001, 1, 24), "keep"),
        ("SPX", D(2001, 1, 25), "invalid")]
    assert config.decisions[0].corrected_value == Decimal("43.31") and config.decisions[0].decided_on == D(2001, 6, 1)
    assert config.decisions[1].corrected_value is None
    with pytest.raises(TypeError):
        config.registered["SPX"] = spx                                    # type: ignore[index]


@pytest.mark.parametrize(("dataset", "reason"), [
    (None, "配置文件不存在"),                                    # 缺失
    ("- 1\n- 2\n", "顶层应为映射"),                              # 顶层不是映射
    ("dataset: [unclosed\n", "wavewarn_v20.yaml"),               # YAML 语法错误
])
def test_dataset_file_problems_are_wrapped_with_the_path(tmp_path: Path, dataset: str | None, reason: str) -> None:
    with pytest.raises(V20ConfigError) as caught:
        load_v20_config(layout(tmp_path, dataset=dataset))
    assert "wavewarn_v20.yaml" in str(caught.value) and reason in str(caught.value)


def test_dataset_file_content_is_validated(tmp_path: Path) -> None:
    for broken in (DATASET.replace("    data_rows: 40\n", ""), DATASET.replace("a" * 64, "A" * 64),
                   DATASET.replace("data_rows: 40", "data_rows: 0"), DATASET.replace("  QQQ:", "  NDX:"),
                   DATASET.replace("first_date: 2001-01-02", "first_date: someday")):
        with pytest.raises(V20ConfigError):
            load_v20_config(layout(tmp_path, dataset=broken))


def test_missing_decisions_file_is_an_error_not_an_empty_table(tmp_path: Path) -> None:
    with pytest.raises(V20ConfigError, match="裁定文件不存在"):
        load_v20_config(layout(tmp_path, decisions=None))


@pytest.mark.parametrize("text", ["", "[]\n", "# 只有注释\n"])
def test_empty_decisions_table_is_an_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(V20ConfigError, match="裁定解析结果为空"):
        load_v20_config(layout(tmp_path, decisions=text))


def test_decisions_file_deleted_between_the_two_existence_checks(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """存在判断通过、随后读取函数返回空元组（模拟两次判断之间文件被删除）：报错，不返回空裁定。"""
    monkeypatch.setattr(project_config, "load_data_decisions", lambda path: ())
    with pytest.raises(V20ConfigError, match="裁定解析结果为空"):
        load_v20_config(layout(tmp_path))


@pytest.mark.parametrize("broken", [
    DECISIONS.replace("  corrected_value: 43.31\n", ""),                         # correct 条目缺修正值
    DECISIONS.replace("corrected_value: 43.31", "corrected_value: -1"),          # 修正值非正
    DECISIONS.replace("date: 2001-01-25\n  symbol: SPX", "date: 2001-01-23\n  symbol: SPX"),   # 同一日期与标的重复
    DECISIONS.replace("decision: keep", "decision: maybe"),                      # 未知的裁定类型
    "symbol: SPX\n",                                                             # 顶层不是列表
    "- [unclosed\n",                                                             # YAML 语法错误
    "- symbol: SPX\n",                                                           # 条目缺字段
])
def test_invalid_decision_entries_are_wrapped(tmp_path: Path, broken: str) -> None:
    with pytest.raises(V20ConfigError) as caught:
        load_v20_config(layout(tmp_path, decisions=broken))
    assert "data_decisions.yaml" in str(caught.value)


def test_only_the_two_registered_paths_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Path]] = []
    real_yaml, real_decisions = project_config._read_yaml, project_config.load_data_decisions

    def read_yaml(path: Path):
        seen.append(("_read_yaml", path))
        return real_yaml(path)

    def load_decisions(path: Path):
        seen.append(("load_data_decisions", path))
        return real_decisions(path)

    monkeypatch.setattr(project_config, "_read_yaml", read_yaml)
    monkeypatch.setattr(project_config, "load_data_decisions", load_decisions)
    load_v20_config(layout(tmp_path))
    assert seen == [("_read_yaml", tmp_path / "config" / "wavewarn_v20.yaml"),
                    ("load_data_decisions", tmp_path / "config" / "data_decisions.yaml")]
    assert (config_v20.DATASET_FILE, config_v20.DECISIONS_FILE) == (("config", "wavewarn_v20.yaml"),
                                                                    ("config", "data_decisions.yaml"))


def test_table_without_any_correct_entry_is_fine(tmp_path: Path) -> None:
    """裁定表有条目、但没有任何 correct 条目：正常返回。空检查只针对全部条目，不针对筛选结果。"""
    only_others = "- date: 2001-01-23\n  symbol: SPX\n  decision: exclude\n  reason: 构造\n  decided_on: 2001-06-02\n"
    config = load_v20_config(layout(tmp_path, decisions=only_others))
    assert [item.decision for item in config.decisions] == ["exclude"]
