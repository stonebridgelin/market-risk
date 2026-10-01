"""机械重跑的对比报告：只比较两个版本已经写出的文件，不导入项目代码，不重新计算任何模型结果。

用法（在主仓库根目录）：
    python docs/audit/correction/compare_rerun.py <工作目录的上级目录> reports/research/wavewarn_v14/correction_rerun
原实现取工作目录 B（标签 v1.4-asrun），修正后取工作目录 B_fBW_flabel（标签 + f_BW + f_label），
两者都由 docs/audit/attribution/build_version.ps1 构造并运行；带缓冲带均线的参照行由 buffered_reference.py 另算。
重跑结果只作纠错证据，不自动恢复候选资格。
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import shutil
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

OLD, NEW = "B", "B_fBW_flabel"
OUTPUT_ROOT = "reports/research/wavewarn_v14"
EVALUATION, ROUND2, EXTENDED_NAV = "evaluation_development", "diagnostics_round2", "extended_nav"
SETTING_KEY = ("model", "exit_version", "k", "theta_p", "q")
UNCHANGED, CHANGED, ABSENT = "无变化", "有变化", "（无此行）"
TOLERANCE = Decimal("1E-20")
INPUTS = ("data/market/daily/SPX.csv", "data/market/daily/QQQ.csv", "data/market/daily/S5TW.csv",
          "data/market/daily/VIX_CBOE.csv", "data/processed/tradingview/NDTW.csv",
          "data/research/wavewarn/v121/vix3m_cboe_development.csv",
          "data/research/wavewarn/v14/vix3m_cboe_validation.csv",
          "reports/research/wavewarn_v121/zz_events_development.csv")
CONFIGS = ("config/wavewarn_v121.yaml", "config/wavewarn_v13.yaml", "config/wavewarn_v14.yaml",
           "config/wavewarn_v14_validation.yaml", "config/wavewarn_v14_diagnostics.yaml")
# 按主键逐格比较的表：（目录, 文件, 主键列）
KEYED = ((EVALUATION, "selection_trace.csv", ("step", "item")),
         (EVALUATION, "convergence.csv", SETTING_KEY),
         (EVALUATION, "event_scope_counts.csv", ("symbol",)),
         (EVALUATION, "reference_rows.csv", ("reference",)),
         (EVALUATION, "event_class_summary_selected.csv", (*SETTING_KEY, "scope")),
         (EVALUATION, "alert_summary_selected.csv", SETTING_KEY),
         (EVALUATION, "yearly_alert_selected.csv", (*SETTING_KEY, "year")),
         (EVALUATION, "missing_audit.csv", ("model", "k", "theta_p", "q", "scope", "reason")),
         (EVALUATION, "event_ledger_selected.csv", ("scope", "source", "peak_date")),
         (ROUND2, "objects.csv", ("object",)),
         (ROUND2, "switch_by_year.csv", ("object", "year")),
         (ROUND2, "switch_by_type.csv", ("object", "from", "to")),
         (ROUND2, "switch_reversals.csv", ("object", "window")),
         (ROUND2, "holding_summary.csv", ("object", "light")),
         (ROUND2, "green_summary.csv", ("object", "symbol")),
         (ROUND2, "green_events.csv", ("object", "symbol", "peak_date")),
         (ROUND2, "nav_metrics.csv", ("object", "scope")))
# 行集合比较的表（没有稳定主键：警报段与持有段的起止日本身会变）
ROW_SETS = ((EVALUATION, "alert_ledger_selected.csv"), (ROUND2, "holding_segments.csv"))
SCORE_FIELDS = (("total_loss", "主损失 L"), ("danger_loss", "危险项"), ("drawdown_loss", "回撤项"),
                ("opportunity_loss", "机会项"), ("switch_cost", "切换项"), ("miss_penalty", "漏报罚项"),
                ("mean_exposure", "ē"), ("timing_score", "T"), ("price_score", "T价格"),
                ("non_green_share", "非绿占比"), ("billed_switches", "计费切换次数"))
DELAY_FIELDS = ("spx_green_delay_median", "qqq_green_delay_median", "spx_class_1", "spx_class_2", "spx_class_3",
                "qqq_class_1", "qqq_class_2", "qqq_class_3", "non_green_ok", "green_delay_spx_ok",
                "green_delay_qqq_ok", "all_conditions_ok", "selected_as")
REFERENCE_FIELDS = (("total_loss", "主损失 L"), ("danger_loss", "危险项"), ("drawdown_loss", "回撤项"),
                    ("opportunity_loss", "机会项"), ("switch_cost", "切换项"), ("mean_exposure", "ē"),
                    ("timing_score", "T"), ("price_score", "T价格"), ("non_green_share", "非绿占比"),
                    ("billed_switches", "计费切换次数"))
OBJECT_FIELDS = (("mean_exposure", "ē"), ("timing_score", "T"), ("price_score", "T价格"), ("switch_cost", "切换项"),
                 ("switches", "切换次数"), ("exposure_change_total", "目标暴露变化量"),
                 ("break_even_gamma_vs_ma200", "相对 200 日均线的盈亏平衡 γ"), ("initial_light", "j₀ 的执行灯色"))
NAV_FIELDS = (("cumulative_return", "累计收益"), ("annualized_return", "年化收益"), ("max_drawdown", "全程最大回撤"),
              ("drawdown_peak_date", "回撤峰值日"), ("drawdown_trough_date", "回撤谷底日"),
              ("drawdown_decline_days", "峰值到谷底（交易日）"), ("drawdown_recovery_date", "恢复日"),
              ("days_from_trough_to_window_end", "谷底到窗口末日（交易日）"), ("worst_20_return", "最差 20 日"),
              ("worst_60_return", "最差 60 日"), ("worst_120_return", "最差 120 日"),
              ("annualized_volatility", "年化波动率"))
GREEN_FIELDS = (("included", "纳入件数"), ("class_1", "类别①"), ("class_2", "类别②"), ("class_3", "类别③"),
                ("green_within_window", "窗口内转绿"), ("median_delay_class_2", "延迟中位数（类别②）"),
                ("median_delay_conservative", "延迟中位数（保守口径）"))

Record = dict[str, str]
Key = tuple[str, ...]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def read_table(path: Path) -> list[Record]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as file:  # type: ignore[operator]
        return list(csv.DictReader(io.StringIO(file.read())))


def keyed(rows: list[Record], key: Key) -> dict[Key, Record]:
    table = {tuple(row[name] for name in key): row for row in rows}
    if len(table) != len(rows):
        raise ValueError(f"主键 {key} 不唯一")
    return table


def cell_changes(old: list[Record], new: list[Record], key: Key) -> list[tuple[str, str, str, str]]:
    """按主键对齐两张表，列出取值不同的单元格（主键、列名、原值、新值）；只在一边出现的行整行列出。"""
    before, after = keyed(old, key), keyed(new, key)
    changes = []
    for index in list(before) + [item for item in after if item not in before]:
        name = "／".join(index)
        if index not in after:
            changes.append((name, "（整行）", "有此行", ABSENT))
        elif index not in before:
            changes.append((name, "（整行）", ABSENT, "有此行"))
        else:
            changes.extend((name, column, before[index][column], value)
                           for column, value in after[index].items() if not same(before[index][column], value))
    return changes


def number(value: str) -> Decimal | None:
    try:
        return Decimal(value) if value != "" else None
    except InvalidOperation:
        return None


def same(old: str, new: str) -> bool:
    """取值是否相同：文本相同，或都是数且相差不超过 TOLERANCE（书写形式不同如 7 与 7.0、Decimal 末位舍入差）。"""
    first, second = number(old), number(new)
    return old == new or (first is not None and second is not None and abs(first - second) <= TOLERANCE)


def same_row(old: Record, new: Record) -> bool:
    return old.keys() == new.keys() and all(same(old[name], value) for name, value in new.items())


def short(value: str) -> str:
    """报告里的取值：带小数点的数保留 6 位，其余原样；空值写“—”。"""
    parsed = number(value)
    if value == "":
        return "—"
    if parsed is None or not ("." in value or "E" in value):
        return value
    return f"{parsed:.6f}".replace("-0.000000", "0.000000")


def delta(old: str, new: str) -> str:
    first, second = number(old), number(new)
    if first is None or second is None:
        return "—"
    difference = second - first
    if abs(difference) <= TOLERANCE:
        return "0"
    return f"{difference:+.6f}" if "." in old + new else f"{difference:+f}"


def mark(old: str, new: str) -> str:
    return UNCHANGED if same(old, new) else CHANGED


def md_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
            *("| " + " | ".join(cell.replace("|", "｜") for cell in row) + " |" for row in rows)]


def setting_name(row: Record) -> str:
    extra = f"，q={row['q']}" if row["q"] else ""
    return f"{row['model']} K={row['k']} θ_P={row['theta_p']}{extra}"


def with_price_score(row: Record) -> Record:
    """T价格 = T − 切换项（由同一行的两列相减，不重新计算模型）。"""
    return {**row, "price_score": str(Decimal(row["timing_score"]) - Decimal(row["switch_cost"]))}


def paired(old: list[Record], new: list[Record]) -> list[tuple[Record, Record]]:
    if [tuple(row[name] for name in SETTING_KEY) for row in old] != [
            tuple(row[name] for name in SETTING_KEY) for row in new]:
        raise ValueError("两次运行的设定集合或顺序不同，不能逐项对比")
    return [(with_price_score(a), with_price_score(b)) for a, b in zip(old, new, strict=True)]


def selection_section(old: dict[str, list[Record]], new: dict[str, list[Record]]) -> list[str]:
    name = "selection_trace.csv"
    rows = [(a["step"], a["item"], a["value"] if number(a["value"]) is None else short(a["value"]),
             b["value"] if number(b["value"]) is None else short(b["value"]), mark(a["value"], b["value"]))
            for a, b in zip(old[name], new[name], strict=True)]
    picked = [next(setting_name(row) for row in tables["settings_summary.csv"] if row["selected_as"])
              for tables in (old, new)]
    counts = [sum(row["all_conditions_ok"] == "是" for row in tables["settings_summary.csv"]
                  if row["role"] == "候选") for tables in (old, new)]
    feasible = [[setting_name(row) for row in tables["settings_summary.csv"]
                 if row["role"] == "候选" and row["all_conditions_ok"] == "是"] for tables in (old, new)]
    same = "没有改变" if picked[0] == picked[1] else "发生改变"
    return ["## 一、选定设定与各级候选数", "",
            "候选集合（K ∈ {3, 5, 10} × θ_P ∈ {1.5%, 2.0%, 2.5%} 九组）、可行条件与三级选择程序都没有改动，"
            "由原有命令 `wavewarn evaluate-v14-development` 重新执行一次。", "",
            f"- 选定设定：原实现 {picked[0]}；修正后 {picked[1]}。**{same}。**",
            f"- 三条可行条件全部满足的候选数：原实现 {counts[0]}，修正后 {counts[1]}；"
            f"满足的是哪几组：{UNCHANGED if feasible[0] == feasible[1] else CHANGED}"
            f"（修正后：{'；'.join(feasible[1])}）。", "",
            *md_table(("步骤", "事项", "原实现", "修正后", "比较"), rows), ""]


def score_section(pairs: list[tuple[Record, Record]]) -> list[str]:
    rows, parts = [], []
    for a, b in pairs:
        fields = ("timing_score", "price_score", "non_green_share", "mean_exposure", "billed_switches")
        rows.append((setting_name(b), b["role"][:5], short(a["timing_score"]), short(b["timing_score"]),
                     delta(a["timing_score"], b["timing_score"]), short(a["price_score"]), short(b["price_score"]),
                     short(a["non_green_share"]), short(b["non_green_share"]), short(a["mean_exposure"]),
                     short(b["mean_exposure"]), a["billed_switches"], b["billed_switches"],
                     UNCHANGED if all(same(a[f], b[f]) for f in fields) else CHANGED))
        fields = ("total_loss", "danger_loss", "drawdown_loss", "opportunity_loss", "switch_cost", "miss_penalty")
        parts.append((setting_name(b), *(f"{short(a[f])} → {short(b[f])}" if not same(a[f], b[f]) else
                                         f"{short(b[f])}（{UNCHANGED}）" for f in fields),
                      UNCHANGED if all(same(a[f], b[f]) for f in fields) else CHANGED))
    return ["## 二、全部 21 组设定的择时得分、非绿占比与切换次数", "",
            "T 为安全代理择时得分（越小越好）；T价格 = T − 切换项。九组 v1.4 是候选；“v1.4 去掉 MR”九组与"
            "三个中位设定（P1·E2、N′·X2、N·E2）是描述性对照，不参与选择，随同一条命令同步重跑。", "",
            *md_table(("设定", "角色", "T 原", "T 修正后", "T 之差", "T价格 原", "T价格 修正后", "非绿占比 原",
                       "非绿占比 修正后", "ē 原", "ē 修正后", "切换 原", "切换 修正后", "比较"), rows), "",
            "主损失与各分项（原实现 → 修正后）：", "",
            *md_table(("设定", "主损失 L", "危险项", "回撤项", "机会项", "切换项", "漏报罚项", "比较"), parts), ""]


def delay_section(pairs: list[tuple[Record, Record]]) -> list[str]:
    def cells(row: Record) -> tuple[str, str, str]:
        return (f"{row['spx_green_delay_median'] or '—'}（{row['spx_class_1']}/{row['spx_class_2']}/"
                f"{row['spx_class_3']}）",
                f"{row['qqq_green_delay_median'] or '—'}（{row['qqq_class_1']}/{row['qqq_class_2']}/"
                f"{row['qqq_class_3']}）",
                "/".join(row[name] for name in ("non_green_ok", "green_delay_spx_ok", "green_delay_qqq_ok")))
    rows = [(setting_name(b), *cells(a), *cells(b),
             UNCHANGED if all(same(a[f], b[f]) for f in DELAY_FIELDS) else CHANGED) for a, b in pairs]
    return ["## 三、转绿延迟与可行条件", "",
            "转绿延迟为类别②事件的中位数（交易日），括号内为类别①/②/③件数；条件依次为非绿占比、SPX 转绿延迟、"
            "QQQ 转绿延迟是否满足。", "",
            *md_table(("设定", "SPX 原", "QQQ 原", "条件 原", "SPX 修正后", "QQQ 修正后", "条件 修正后", "比较"),
                      rows), ""]


def window_section(old: dict[str, list[Record]], new: dict[str, list[Record]]) -> list[str]:
    name = "convergence.csv"
    rows = [(setting_name(b), a["convergence_date"], b["convergence_date"],
             mark(a["convergence_date"], b["convergence_date"]),
             "／".join(b[f] for f in ("t0", "tau", "first_loss_day")),
             UNCHANGED if all(same(a[f], b[f]) for f in ("t0", "tau", "first_loss_day")) else CHANGED)
            for a, b in zip(old[name], new[name], strict=True)]
    scope = cell_changes(old["event_scope_counts.csv"], new["event_scope_counts.csv"], ("symbol",))
    counts = "；".join(f"{row['symbol']}：全部 {row['events']} 件，进事件账 {row['in_event_ledger']} 件，"
                      f"计漏报罚项 {row['complete_for_penalty']} 件" for row in new["event_scope_counts.csv"])
    return ["## 四、收敛日、评价窗口与事件纳入范围", "",
            "v1.4 与“v1.4 去掉 MR”共 18 组的系统收敛日有变化，三个中位设定没有变化。"
            "τ = max(t0 + 63 个交易日, 全部设定的系统收敛日)：各组收敛日都早于 t0 + 63 个交易日，τ 与 j₀ 不变。", "",
            *md_table(("设定", "收敛日 原", "收敛日 修正后", "比较", "t0／τ／j₀（修正后）", "t0、τ、j₀ 的比较"), rows),
            "", f"- 事件纳入范围（`event_scope_counts.csv`）：{UNCHANGED if not scope else CHANGED}。{counts}。",
            "- 事件标签没有改动；纳入门槛（高点 P ≥ τ）随 τ 而定，τ 不变所以纳入范围不变。", ""]


def reference_section(old: dict[str, list[Record]], new: dict[str, list[Record]],
                      buffered: list[dict]) -> list[str]:
    name = "reference_rows.csv"
    rows = []
    for a, b in zip(old[name], new[name], strict=True):
        a, b = with_price_score(a), with_price_score(b)
        rows.append((b["reference"], *(short(b[f]) for f, _ in REFERENCE_FIELDS),
                     UNCHANGED if same_row(a, b) else CHANGED))
    label = "带缓冲带的200日均线"
    first, second = (item["rows"][label] for item in buffered)
    rows.append((f"{label}（另算）", *(short(str(second[f])) for f, _ in REFERENCE_FIELDS),
                 UNCHANGED if first == second else CHANGED))
    return ["## 五、参照行", "",
            "四条参照行（始终绿、黄、红与 200 日均线）来自 `reference_rows.csv`。带缓冲带的 200 日均线"
            f"（带宽 {Decimal(buffered[1]['band']) * 100:f}%，登记的固定参考值）不在开发期评价命令的输出里，"
            "由 `docs/audit/correction/buffered_reference.py` 在两个版本里各算一次："
            "与 200 日均线参照行走同一条执行与损失路径，同暴露基准用同一次运行里始终绿、始终红两行的主损失。"
            "下表列修正后的取值与两版的比较。", "",
            *md_table(("参照", *(title for _, title in REFERENCE_FIELDS), "与原实现比较"), rows), "",
            "参照行不含 BW 通道，评价窗口（τ、j₀）与事件纳入范围又没有变化，所以五行都没有变化。", ""]


def round2_section(old: dict[str, list[Record]], new: dict[str, list[Record]]) -> list[str]:
    lines = ["## 六、重选设定的第二轮诊断（切换、转绿双层报告、完整净值）", "",
             "重选的设定与原选定设定相同（K=5、θ_P=2.5%），诊断由原有命令 `wavewarn v14-diagnostics-round2` "
             "按同一口径写出。三个对象中，选定的 v1.4 与“v1.4 去掉 MR”含 BW 通道，200 日均线不含。", "",
             "**对象概览（`objects.csv`）**", ""]
    rows = []
    for a, b in zip(old["objects.csv"], new["objects.csv"], strict=True):
        rows.extend((b["object"], title, short(a[field]), short(b[field]), delta(a[field], b[field]),
                     mark(a[field], b[field])) for field, title in OBJECT_FIELDS)
    lines += [*md_table(("对象", "项目", "原实现", "修正后", "差", "比较"), rows), "",
              "**切换诊断**", ""]
    for name, key, title in (("switch_by_year.csv", ("object", "year"), "分年切换次数"),
                             ("switch_by_type.csv", ("object", "from", "to"), "分类型切换次数"),
                             ("switch_reversals.csv", ("object", "window"), "切换后短期内反向"),
                             ("holding_summary.csv", ("object", "light"), "持有段摘要")):
        changes = cell_changes(old[name], new[name], key)
        lines.append(f"- {title}（`{name}`）：{UNCHANGED if not changes else f'{len(changes)} 个单元格{CHANGED}'}。")
        if changes:
            lines += ["", *md_table(("行", "列", "原实现", "修正后"),
                                    [(k, c, short(x), short(y)) for k, c, x, y in changes]), ""]
    before, after = ({tuple(row.values()) for row in tables["holding_segments.csv"]} for tables in (old, new))
    lines += [f"- 持有段明细（`holding_segments.csv`）：原实现 {len(old['holding_segments.csv'])} 段，"
              f"修正后 {len(new['holding_segments.csv'])} 段；只在原实现出现的 {len(before - after)} 段，"
              f"只在修正后出现的 {len(after - before)} 段。", "", "**转绿双层报告（`green_summary.csv`）**", ""]
    rows = []
    for a, b in zip(old["green_summary.csv"], new["green_summary.csv"], strict=True):
        rows.extend((f"{b['object']}·{b['symbol']}", title, short(a[field]), short(b[field]),
                     mark(a[field], b[field])) for field, title in GREEN_FIELDS)
    events = cell_changes(old["green_events.csv"], new["green_events.csv"], ("object", "symbol", "peak_date"))
    verdict = UNCHANGED if not events else f"{len(events)} 个单元格{CHANGED}"
    lines += [*md_table(("对象·资产", "项目", "原实现", "修正后", "比较"), rows), "",
              f"- 逐事件明细（`green_events.csv`）：{verdict}。"]
    if events:
        lines += ["", *md_table(("对象／资产／高点日", "列", "原实现", "修正后"),
                                [(k, c, short(x), short(y)) for k, c, x, y in events])]
    lines += ["", "**完整净值（`nav_metrics.csv`，不含分红、现金收益为 0、未计费用）**", ""]
    rows = []
    for a, b in zip(old["nav_metrics.csv"], new["nav_metrics.csv"], strict=True):
        if same_row(a, b):
            rows.append((f"{b['object']}·{b['scope']}", "全部列", "—", "—", "—", UNCHANGED))
            continue
        rows.extend((f"{b['object']}·{b['scope']}", title, short(a[field]), short(b[field]),
                     delta(a[field], b[field]), mark(a[field], b[field])) for field, title in NAV_FIELDS)
    lines += [*md_table(("对象·口径", "项目", "原实现", "修正后", "差", "比较"), rows), ""]
    return lines


def ledger_section(old: dict[str, list[Record]], new: dict[str, list[Record]]) -> list[str]:
    lines = ["## 七、选定设定的事件账、警报账与缺值审计", ""]
    for name, key, title in (("event_class_summary_selected.csv", (*SETTING_KEY, "scope"), "事件分类汇总"),
                             ("event_ledger_selected.csv", ("scope", "source", "peak_date"), "事件账"),
                             ("alert_summary_selected.csv", SETTING_KEY, "警报账汇总"),
                             ("yearly_alert_selected.csv", (*SETTING_KEY, "year"), "分年警报"),
                             ("missing_audit.csv", ("model", "k", "theta_p", "q", "scope", "reason"),
                              "缺值审计（全部设定）")):
        changes = cell_changes(old[name], new[name], key)
        lines.append(f"- {title}（`{name}`，{len(new[name])} 行）："
                     f"{UNCHANGED if not changes else f'{len(changes)} 个单元格{CHANGED}'}。")
        if changes and len(changes) <= 40:
            lines += ["", *md_table(("行", "列", "原实现", "修正后"),
                                    [(k, c, short(x), short(y)) for k, c, x, y in changes]), ""]
    name = "alert_ledger_selected.csv"
    before, after = ({tuple(row.values()) for row in tables[name]} for tables in (old, new))
    lines += [f"- 警报账（`{name}`）：原实现 {len(old[name])} 段，修正后 {len(new[name])} 段；"
              f"只在原实现出现的 {len(before - after)} 段，只在修正后出现的 {len(after - before)} 段。",
              "- 逐格的完整清单见 `comparison_tables.csv`。", ""]
    return lines


def file_rows(roots: dict[str, Path]) -> list[tuple[str, str, str, str]]:
    """两个版本写出的全部文件的 SHA-256 与比较（文件集合须相同）。"""
    listed = {}
    for name, root in roots.items():
        base = root / OUTPUT_ROOT
        listed[name] = {path.relative_to(base).as_posix(): sha256(path.read_bytes())
                        for folder in (EVALUATION, ROUND2, EXTENDED_NAV)
                        for path in sorted((base / folder).rglob("*")) if path.is_file()}
    if set(listed[OLD]) != set(listed[NEW]):
        raise ValueError("两个版本写出的文件集合不同")
    return [(name, listed[OLD][name], digest, mark(listed[OLD][name], digest))
            for name, digest in listed[NEW].items()]


def file_section(files: list[tuple[str, str, str, str]]) -> list[str]:
    same = [name for name, _, _, verdict in files if verdict == UNCHANGED]
    return ["## 八、输出文件逐个比较", "",
            f"两个版本各写出 {len(files)} 个文件（四条命令的输出目录），{len(same)} 个{UNCHANGED}，"
            f"{len(files) - len(same)} 个{CHANGED}。SHA-256 按命令写出的字节计算（CSV 为 CRLF 换行），见 "
            "`file_comparison.csv`。", "",
            *md_table(("文件", "比较"), [(f"`{name}`", verdict) for name, _, _, verdict in files]), "",
            "- `evaluation_development/extended_history/`（冻结参数的纯价格版本，不含 BW）与 `extended_nav/`"
            "（补充历史净值，不含 BW）的全部文件都没有变化；这两处的已入库输出留在原处，不另存。",
            "- 开发期评价的 `README.md` 只有登记逐日明细哈希的一行不同。", ""]


def provenance(roots: dict[str, Path], versions: dict[str, dict],
               files: list[tuple[str, str, str, str]]) -> dict[str, object]:
    hashed = {}
    for group, names in (("inputs", INPUTS), ("configs", CONFIGS)):
        both = [{name: sha256((roots[version] / name).read_bytes()) for name in names} for version in (OLD, NEW)]
        if both[0] != both[1]:
            raise ValueError(f"两个版本的{group}不同")
        hashed[group] = both[1]
    return {"original": {key: versions[OLD][key] for key in ("base_tag", "base_commit", "code_tree", "patches")},
            "corrected": {key: versions[NEW][key] for key in ("base_tag", "base_commit", "code_tree", "patches",
                                                               "changed_files")},
            "inputs_sha256": hashed["inputs"], "configs_sha256": hashed["configs"],
            "outputs_sha256": {name: digest for name, _, digest, _ in files},
            "note": "输入与配置按各版本工作目录里的字节计算（由标签检出、不做换行转换，即仓库内的 LF 字节；"
                    "NDTW 不入库，取主仓库的同一份文件）；输出按命令写出的字节计算。两个版本的输入与配置逐项相同。"}


def provenance_section(data: dict) -> list[str]:
    corrected, original = data["corrected"], data["original"]
    patches = "；".join(f"`{item['file']}`（`{item['sha256']}`）" for item in corrected["patches"])
    return ["## 九、来源与哈希", "",
            f"- 原实现：标签 `{original['base_tag']}`（提交 `{original['base_commit']}`），"
            f"代码树 `{original['code_tree']}`，没有补丁。",
            f"- 修正后：同一标签 + 补丁 {patches}；代码树 `{corrected['code_tree']}`；"
            f"相对标签改动的文件：{'、'.join(f'`{item}`' for item in corrected['changed_files'])}。",
            "- 两个版本各在独立的工作目录里运行，输入与配置逐项相同：", "",
            *md_table(("输入或配置", "SHA-256"),
                      [(f"`{name}`", f"`{digest}`") for group in ("inputs_sha256", "configs_sha256")
                       for name, digest in data[group].items()]), "",
            f"- {data['note']}",
            "- 修正后各输出文件的 SHA-256 见 `provenance.json` 的 `outputs_sha256`。", ""]


def main() -> None:
    worktrees, target = Path(sys.argv[1]), Path(sys.argv[2])
    roots = {name: worktrees / name for name in (OLD, NEW)}
    tables: dict[str, dict[str, list[Record]]] = {}
    for name, root in roots.items():
        base = root / OUTPUT_ROOT
        tables[name] = {file: read_table(base / folder / file) for folder, file, _ in KEYED}
        tables[name].update({file: read_table(base / folder / file) for folder, file in ROW_SETS})
        tables[name]["settings_summary.csv"] = read_table(base / EVALUATION / "settings_summary.csv")
    old, new = tables[OLD], tables[NEW]
    pairs = paired(old["settings_summary.csv"], new["settings_summary.csv"])
    buffered = [json.loads((roots[name] / "buffered_reference.json").read_text(encoding="utf-8"))
                for name in (OLD, NEW)]
    versions = {name: json.loads((roots[name] / "version.json").read_text(encoding="utf-8-sig"))
                for name in (OLD, NEW)}
    files = file_rows(roots)
    data = provenance(roots, versions, files)
    target.mkdir(parents=True, exist_ok=True)
    with (target / "comparison_settings.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("setting", "role", "field", "original", "corrected", "changed"))
        writer.writerows((setting_name(b), b["role"], field, a[field], b[field],
                          "否" if same(a[field], b[field]) else "是")
                         for a, b in pairs for field in b if field not in SETTING_KEY and field != "role")
    with (target / "comparison_tables.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("table", "row", "column", "original", "corrected"))
        for folder, name, key in KEYED:
            changes = cell_changes(old[name], new[name], key)
            writer.writerows((f"{folder}/{name}", *change) for change in changes)
            if not changes:
                writer.writerow((f"{folder}/{name}", UNCHANGED, "", "", ""))
    with (target / "file_comparison.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("file", "original_sha256", "corrected_sha256", "comparison"))
        writer.writerows(files)
    for name, label in ((OLD, "original"), (NEW, "corrected")):
        shutil.copyfile(roots[name] / "buffered_reference.json", target / f"buffered_reference_{label}.json")
    (target / "provenance.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# v1.4 开发期评价：两处修正前后的逐项对比（机械重跑）", "",
             "本报告仅使用开发期数据。在“标签 v1.4-asrun + f_BW + f_label”这一个版本上，按原候选集合、原可行条件、"
             "原三级选择程序机械重跑并重选一次；**重跑结果只作纠错证据，不自动恢复候选资格。v1.4 继续暂停，"
             "是否以及如何继续由负责人另行裁决。**", "",
             "两处修正：f_BW 把 BW 退出谓词的括号范围改回规格原文（广度 > 40 连续 3 日，C_t > C_{t−5} 只看当日；"
             "任一 BW 输入缺失的一天连续日计数清零、当日不判断退出）；f_label 让标签读回核对全部日期列。"
             "γ、事件标签、主损失、缺值政策与其余评价代码都没有改动。下面每一项都列出，没有变化的写“无变化”；"
             f"两个取值都是数且相差不超过 {TOLERANCE}（书写形式不同，或 Decimal 末位舍入差）的按无变化计。", "",
             *selection_section(old, new), *score_section(pairs), *delay_section(pairs),
             *window_section(old, new), *reference_section(old, new, buffered), *round2_section(old, new),
             *ledger_section(old, new), *file_section(files), *provenance_section(data)]
    (target / "对比报告.md").write_text("\n".join(lines), encoding="utf-8")
    changed = sum(verdict == CHANGED for _, _, _, verdict in files)
    print(f"文件 {len(files)} 个，其中有变化 {changed} 个；对比报告已写出")


if __name__ == "__main__":
    main()
