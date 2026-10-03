"""配置读取的边界模块（阶段三实施指令第一节第 3 部分、第二节第 5 部分）。

只读取两份配置文件：
- config/wavewarn_v20.yaml：登记第零节绑定的数据集元数据（经项目已有的 YAML 读取函数，一处）；
- config/data_decisions.yaml：已裁定日期表（先判断文件存在一处，再经项目已有的读取函数一处）。
缺失、解析失败、裁定解析结果为空，一律报 V20ConfigError，并注明路径与原因。

“裁定解析结果为空即报错”是当前绑定数据集的完整性要求（该数据集含已批准的修正条目），不是所有未来数据集的通用规则；
检查的是读取函数返回的全部条目，在任何筛选之前进行。这里保留全部裁定类型，筛选由行情读取完成。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from market_risk import config as project_config
from market_risk.wavewarn_v20.dataset_v20 import DecisionEntry, RegisteredFile

DATASET_FILE = ("config", "wavewarn_v20.yaml")
DECISIONS_FILE = ("config", "data_decisions.yaml")
ASSETS = ("SPX", "QQQ")


class V20ConfigError(ValueError):
    """v2.0 的配置缺失、无法解析或内容不合要求。"""


@dataclass(frozen=True)
class V20Config:
    registered: Mapping[str, RegisteredFile]       # 各资产登记的文件元数据
    decisions: tuple[DecisionEntry, ...]           # 全部裁定条目（各种裁定类型）


def _date(value: Any, name: str) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as error:
        raise V20ConfigError(f"{name} 不是日期：{value!r}") from error


def _sha256(value: Any, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise V20ConfigError(f"{name} 须是 64 位小写十六进制：{value!r}")
    return text


def _registered(asset: str, raw: Any, path: Path) -> RegisteredFile:
    try:
        rows = raw["data_rows"]
        if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
            raise V20ConfigError(f"{asset}.data_rows 须是正整数：{rows!r}")
        return RegisteredFile(asset, _sha256(raw["raw_sha256"], f"{asset}.raw_sha256"),
                              _sha256(raw["normalized_sha256"], f"{asset}.normalized_sha256"), rows,
                              _date(raw["first_date"], f"{asset}.first_date"),
                              _date(raw["last_date"], f"{asset}.last_date"))
    except (KeyError, TypeError) as error:
        raise V20ConfigError(f"{path} 中 {asset} 的登记值缺少字段或格式错误：{error}") from error


def load_v20_config(root: Path) -> V20Config:
    """读取登记的数据集元数据与全部裁定条目。root 为仓库根目录（测试里是按仓库布局构造的临时目录）。"""
    dataset_path = root.joinpath(*DATASET_FILE)
    try:
        raw = project_config._read_yaml(dataset_path)
    except (project_config.ConfigError, yaml.YAMLError) as error:
        raise V20ConfigError(f"无法读取 {dataset_path}：{error}") from error
    dataset = raw.get("dataset")
    if not isinstance(dataset, dict) or set(dataset) != set(ASSETS):
        raise V20ConfigError(f"{dataset_path} 的 dataset 须恰好登记 {ASSETS}")
    registered = {asset: _registered(asset, dataset[asset], dataset_path) for asset in ASSETS}

    decisions_path = root.joinpath(*DECISIONS_FILE)
    if not decisions_path.exists():
        raise V20ConfigError(f"裁定文件不存在：{decisions_path}")
    try:
        loaded = project_config.load_data_decisions(decisions_path)
    except (project_config.ConfigError, KeyError, ValueError, yaml.YAMLError) as error:
        raise V20ConfigError(f"无法读取 {decisions_path}：{error}") from error
    if not loaded:
        raise V20ConfigError(f"{decisions_path} 的裁定解析结果为空")
    decisions = tuple(DecisionEntry(item.symbol, item.date, item.decision, item.corrected_value, item.decided_on)
                      for item in loaded)
    return V20Config(MappingProxyType(registered), decisions)
