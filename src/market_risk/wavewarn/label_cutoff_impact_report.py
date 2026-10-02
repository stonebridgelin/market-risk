"""旧补充历史的标签影响量化：表格行与报告正文（纯计算，只生成行与文本）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.label_cutoff_impact import (
    COMPONENT_NAMES,
    PRICE_COMPONENTS,
    Comparison,
    LabelDifference,
    ObjectImpact,
    comparisons,
    ranking,
)

Row = tuple[object, ...]
REPORT_NAME = "标签影响量化报告.md"
OPENING = ("这是冻结路径上的局部纠错核算，不是重新选参；不修改任何已入库的历史输出。"
           "信号、执行暴露、日期轴与损失函数都不变，只把逐资产区间的归类换成按窗口截止日生成的标签。")
DIFFERENCE_HEADER = ("version", "symbol", "interval_start", "interval_end", "development_label_class",
                     "cutoff_label_class")
OBJECT_HEADER = ("version", "object", "role", "item", "development_labels", "cutoff_labels", "change")
LOCAL_HEADER = ("version", "object", "symbol", "component", "local_adjustment")
RECONCILIATION_HEADER = ("version", "object", "danger_gap", "drawdown_gap", "opportunity_gap", "total_loss_gap",
                         "score_gap", "tolerance", "passed")
COMPARISON_HEADER = ("version", "setting", "reference", "item", "development_labels", "cutoff_labels", "change",
                     "direction_changed")
RANKING_HEADER = ("version", "item", "rank", "development_labels", "cutoff_labels", "same")
ITEMS = (*PRICE_COMPONENTS, "switch_cost", "full_exposure_cost", "total_loss", "score")
ITEM_NAMES = {**COMPONENT_NAMES, "score": "T", "mean_exposure": "ē", "benchmark_loss": "同暴露基准"}


@dataclass(frozen=True)
class VersionImpact:
    """一个旧补充历史版本的核算结果。"""

    name: str                                  # 如“v1.3 补充历史（P0 稳健性）”
    window: str                                # 窗口的文字说明
    differences: tuple[LabelDifference, ...]
    settings: tuple[ObjectImpact, ...]
    references: tuple[ObjectImpact, ...]
    gaps: Mapping[str, tuple[Decimal, ...]]    # 对象 → 五项核对之差
    stored: Mapping[str, str]                  # 原标签下的重算与已入库输出的比对：文件 → 结果
    others: Mapping[str, str]                  # 其余用到标签的输出：项目 → “无变化”或变化的说明
    conclusions: tuple[str, ...]               # 原报告中的文字结论是否改变

    @property
    def objects(self) -> tuple[ObjectImpact, ...]:
        return (*self.references, *self.settings)


def _value(item: ObjectImpact, name: str, new: bool) -> Decimal:
    totals = item.new if new else item.old
    if name in ("total_loss", "score", "mean_exposure", "benchmark_loss"):
        return getattr(totals, name)
    return totals.parts[name]


def difference_rows(versions: Sequence[VersionImpact]) -> tuple[Row, ...]:
    return tuple((version.name, item.symbol, item.start, item.end, item.old_class, item.new_class)
                 for version in versions for item in version.differences)


def object_rows(versions: Sequence[VersionImpact]) -> tuple[Row, ...]:
    rows: list[Row] = []
    for version in versions:
        for item in version.objects:
            for name in (*ITEMS, "mean_exposure", "benchmark_loss"):
                old, new = _value(item, name, False), _value(item, name, True)
                rows.append((version.name, item.name, item.role, ITEM_NAMES[name], old, new, new - old))
    return tuple(rows)


def local_rows(versions: Sequence[VersionImpact]) -> tuple[Row, ...]:
    return tuple((version.name, item.name, symbol, COMPONENT_NAMES[name], value)
                 for version in versions for item in version.objects
                 for symbol, parts in sorted(item.local.items()) for name, value in parts.items())


def reconciliation_rows(versions: Sequence[VersionImpact], tolerance: Decimal) -> tuple[Row, ...]:
    return tuple((version.name, name, *gaps, tolerance, "是" if all(abs(gap) <= tolerance for gap in gaps) else "否")
                 for version in versions for name, gaps in version.gaps.items())


def comparison_rows(versions: Sequence[VersionImpact]) -> tuple[Row, ...]:
    return tuple((version.name, item.setting, item.reference, ITEM_NAMES[item.field], item.old, item.new,
                  item.new - item.old, "是" if item.direction_changed else "否")
                 for version in versions for item in comparisons(version.settings, version.references))


def ranking_rows(versions: Sequence[VersionImpact]) -> tuple[Row, ...]:
    rows: list[Row] = []
    for version in versions:
        for field in ("total_loss", "score"):
            old, new = ranking(version.settings, field, False), ranking(version.settings, field, True)
            rows.extend((version.name, ITEM_NAMES[field], number, before, after, "是" if before == after else "否")
                        for number, (before, after) in enumerate(zip(old, new, strict=True), start=1))
    return tuple(rows)


def num(value: Decimal) -> str:
    text = f"{value:.6f}"
    return "0.000000" if text == "-0.000000" else text


def signed(value: Decimal) -> str:
    return "0" if value == 0 else f"{value:+.6f}"


def md_table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
            *("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows), ""]


def _asset_part(item: ObjectImpact, symbol: str) -> Decimal:
    return sum(item.local[symbol].values(), Decimal(0))


def changed_directions(version: VersionImpact) -> tuple[Comparison, ...]:
    return tuple(item for item in comparisons(version.settings, version.references) if item.direction_changed)


def version_lines(version: VersionImpact, tolerance: Decimal) -> list[str]:
    symbols = sorted({item.symbol for item in version.differences})
    counts = "，".join(f"{symbol} {sum(item.symbol == symbol for item in version.differences)} 个"
                      for symbol in symbols)
    lines = [f"## {version.name}", "", version.window, "",
             "### 归类不同的资产区间", "",
             f"在整个窗口内逐日比对两套标签对每个资产区间的归类，共有 {len(version.differences)} 个资产区间不同"
             f"（{counts or '无'}）：", "",
             *md_table(("资产", "区间起点日", "区间终点日", "开发期标签下的归类", "截止日标签下的归类"),
                       [(item.symbol, item.start, item.end, item.old_class, item.new_class)
                        for item in version.differences]),
             "按登记规则，未定区间的三项价格损失记零，日期保留在平均暴露的分母中；所以各对象的区间数、平均暴露与"
             "切换项都不变，变的只是这些区间上的价格损失。", "",
             "### 各对象的变化", "",
             "各列为“开发期标签下的值 → 截止日标签下的值（差）”。主损失的差按资产拆成 SPX 部分与 QQQ 部分"
             "（权重已乘入），两者相加即主损失的差。", "",
             *md_table(("对象", "危险项", "回撤项", "机会项", "主损失 L", "其中 SPX 部分", "其中 QQQ 部分", "T"),
                       [(item.name,
                         *(f"{num(_value(item, name, False))} → {num(_value(item, name, True))}"
                           f"（{signed(item.change(name))}）" for name in (*PRICE_COMPONENTS, "total_loss")),
                         signed(_asset_part(item, "SPX")), signed(_asset_part(item, "QQQ")),
                         f"{num(item.old.score)} → {num(item.new.score)}（{signed(item.change('score'))}）")
                        for item in version.objects]),
             "切换项、漏报罚项、区间数与平均暴露在两套标签下都相同（程序核对）。两份旧报告都没有 T价格，这里不报。", ""]
    flips = changed_directions(version)
    rows = [(item.setting, item.reference, ITEM_NAMES[item.field], num(item.old), num(item.new),
             signed(item.new - item.old), "有改变" if item.direction_changed else "不变")
            for item in comparisons(version.settings, version.references) if item.reference == "200日均线"]
    lines += ["### 与参照行的比较差值", "",
              "差值 = 候选设定 − 参照行。下表列与 200 日均线的比较；与始终绿、黄、红的比较在 `comparisons.csv`。", "",
              *md_table(("设定", "参照", "项目", "开发期标签", "截止日标签", "差值的变化", "比较方向"), rows),
              f"全部比较（九组设定 × 四条参照行 × 主损失与 T）中，比较方向改变的有 {len(flips)} 项"
              + ("。" if not flips else "：" + "；".join(
                  f"{item.setting} 对 {item.reference} 的 {ITEM_NAMES[item.field]}" for item in flips) + "。"), ""]
    lines += ["### 排序", ""]
    for field in ("total_loss", "score"):
        old, new = ranking(version.settings, field, False), ranking(version.settings, field, True)
        lines.append(f"- 九组设定按 {ITEM_NAMES[field]} 从小到大的排序：{'没有改变' if old == new else '有改变'}。"
                     + ("" if old == new else f"原排序：{' < '.join(old)}；截止日标签下：{' < '.join(new)}。"))
    lines += ["", "### 原报告中的文字结论", "", *(f"- {text}" for text in version.conclusions), "",
              "### 其余用到这批标签的输出", "", *(f"- {name}：{text}" for name, text in version.others.items()), "",
              "### 核对", "",
              "- 原标签下的重算与已入库输出：" + "；".join(f"`{name}` {text}" for name, text in version.stored.items())
              + "。",
              f"- 逐区间的局部调整之和与汇总的变化（三个价格分项、主损失、T；绝对误差不超过 {tolerance}）："
              f"{len(version.gaps)} 个对象全部一致，最大的差为 "
              f"{max(abs(gap) for gaps in version.gaps.values() for gap in gaps):.1E}。", ""]
    return lines


def report_lines(versions: Sequence[VersionImpact], tolerance: Decimal, notes: Sequence[str]) -> list[str]:
    return ["# 旧补充历史的标签影响量化", "", OPENING, "",
            "背景：此前 v1.3、v1.4 补充历史的损失计算使用了开发期标签（截止 2016-12-30）；窗口末日是 2009-09-30，"
            "应当使用按该截止日生成的标签。下面逐个对象核算两套标签下的差别。", "",
            *(line for version in versions for line in version_lines(version, tolerance)), *notes]
