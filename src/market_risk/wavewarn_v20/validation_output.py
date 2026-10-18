"""阶段五验证期与保留期运行的输出方案（V1 设计稿定稿第七节；V2 指令第四节第 7 条）。边界侧。

文件操作：输出、清单、发布与失败证据一律经 development_output 的基本函数（make_directory、write_new、read_back、
rename_directory、path_exists；本模块只导入这些公共函数，不另写）；本模块新增的文件操作只有一处，即预检只读入口
PreflightReader.read（只接受 PREFLIGHT_FILES 中的相对路径，只在预检步骤中使用）。
基准文件（M3 正式目录或验证期正式目录中的清单、逐日信号与检验结果）经 read_back 读取，允许对象限于该文件本身。

位置：命名规则与开发期相同，只把目录名换为 evaluation_validation 或 evaluation_holdout（正式目录、暂存目录、
失败目录、包外审计尾段、发布失败说明）。路径由 storage/paths.py 的研究目录生成。

格式与开发期相同（development_output 的 json_bytes、csv_bytes、gzip_bytes、cell）。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from market_risk.storage.paths import StoragePaths
from market_risk.wavewarn_v20 import development_compare as compare
from market_risk.wavewarn_v20.confirmatory import BootstrapRow, ConfirmatoryResult
from market_risk.wavewarn_v20.development_output import (
    AUDIT_LOG,
    FAILURE,
    FAILURE_MANIFEST,
    HISTORICAL_NOTE,
    INFORMATION_STATE,
    INFORMATION_STATE_FINAL,
    MANIFEST,
    NOT_A_SUCCESS,
    PREFIX_USAGE,
    RUN_ENVIRONMENT,
    Locations,
    OutputError,
    PreflightError,
    cell,
    json_bytes,
    make_directory,
    manifest_bytes,
    parse_manifest,
    path_exists,
    read_back,
    readable_roots,
    rename_directory,
    serialize_daily,
    serialize_r2,
    serialize_snapshot,
    sha256,
    verify_manifest,
    verify_written,
    window_document,
    write_new,
)
from market_risk.wavewarn_v20.development_output import PREFLIGHT_FILES as DEVELOPMENT_PREFLIGHT_FILES
from market_risk.wavewarn_v20.nav import PartialPolicyResult, PolicyResult
from market_risk.wavewarn_v20.registered_v20_validation import (
    LOCK_RECORD_SHA256,
    LOCK_SUPPLEMENT_SHA256,
    M3_FORMAL_NAME,
    MANIFEST_NAME,
    SIGNALS_HEADER,
    SIGNALS_NAME,
    STANDARD_R3_SHA256,
    V1_DESIGN_SHA256,
    PeriodSpec,
)
from market_risk.wavewarn_v20.research_run import REFERENCE, WindowResult, candidate_key
from market_risk.wavewarn_v20.snapshot import Snapshot

__all__ = (
    "AUDIT_LOG", "FAILURE", "FAILURE_MANIFEST", "INFORMATION_STATE", "INFORMATION_STATE_FINAL", "MANIFEST",
    "NOT_A_SUCCESS", "PREFIX_USAGE", "RUN_ENVIRONMENT", "Locations", "OutputError", "PreflightError", "make_directory",
    "manifest_bytes", "parse_manifest", "path_exists", "read_back", "readable_roots", "rename_directory", "sha256",
    "verify_manifest", "verify_written", "write_new",
)

OUTPUT_SCHEME = "v20-stage5-output-1"                     # 输出方案版本
SIGNALS_FILE = SIGNALS_NAME                               # daily_signals.csv.gz（重叠比对的基准与本期输出同名）
CONFIRMATORY = "confirmatory.json"
OVERLAP = "overlap_check.json"
DATE_ASSERTIONS = "date_assertions.json"
DESCRIPTIVE = "descriptive.json"
REPORT = "report.md"
SENSITIVITY_INCOMPLETE = "敏感性证据不完整"
INVALID = "计算无效"
# 预检只读入口的固定集合：开发期的 35 个文件，加阶段五的 4 个新模块（POSIX 写法）。
PREFLIGHT_FILES: tuple[str, ...] = (
    *DEVELOPMENT_PREFLIGHT_FILES,
    *(f"src/market_risk/wavewarn_v20/{name}.py" for name in (
        "registered_v20_validation", "validation_diagnostics", "validation_output", "validation_run")),
)
RISK_TEXT = {0: "正常", 1: "一级", 2: "二级"}             # 与开发期输出 daily_signals 的风险列写法相同


def diagnostics_name(period: PeriodSpec) -> str:
    """必报诊断的文件名：验证期 diagnostics_validation.json，保留期 diagnostics_holdout.json。"""
    return f"diagnostics_{period.mode}.json"


# ---------------------------------------------------------------------------
# 位置（命名规则与开发期相同，只替换目录名）
# ---------------------------------------------------------------------------


def locations(root: Path, formal_name: str, utc: str, short_commit: str) -> Locations:
    research = StoragePaths(root).v20_research_dir
    stamp = f"{utc}_{short_commit}"
    return Locations(research, research / formal_name, research / f".staging_{formal_name}_{stamp}",
                     research / f"{formal_name}_failed_{stamp}", research / f"{formal_name}_audit_tail_{stamp}.jsonl",
                     research / f"{formal_name}_publish_failure_{stamp}.json", stamp)


def development_baseline(root: Path) -> tuple[Path, Path]:
    """验证期重叠比对的基准：M3 正式目录中的清单与逐日信号。"""
    formal = StoragePaths(root).v20_research_dir / M3_FORMAL_NAME
    return formal / MANIFEST_NAME, formal / SIGNALS_NAME


def read_reference(path: Path) -> bytes:
    """读取一个基准文件的全部字节（经 read_back，允许对象只限该文件本身）。"""
    return read_back(path, (path,))


class PreflightReader:
    """预检只读入口：只接受 PREFLIGHT_FILES 中的相对路径；规范化后须位于 root 之下且不位于 root/data 之下；
    只读、返回字节；close 之后再读即失败（只在预检步骤中使用）。"""

    def __init__(self, files: Sequence[str] = PREFLIGHT_FILES) -> None:
        self.files = tuple(files)
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def read(self, root: Path, relative: str) -> bytes:
        if self.closed:
            raise PreflightError("预检已结束，不得再调用预检只读入口")
        if relative not in self.files:
            raise PreflightError(f"不在预检文件集合之内：{relative!r}")
        path = root.joinpath(*relative.split("/"))
        base, target = _normalized(root), _normalized(path)
        data_dir = _normalized(root.joinpath("data"))
        if not _within(target, base) or _within(target, data_dir):
            raise PreflightError(f"预检路径越界：{relative!r}")
        content = path.read_bytes()
        return content


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _within(target: str, base: str) -> bool:
    return target == base or target.startswith(base.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------------------
# 逐日信号的格式化与基准解析（重叠比对用；规则与开发期输出 daily_signals 相同）
# ---------------------------------------------------------------------------


def risk_text(value: object) -> str:
    """风险列：{0: 正常, 1: 一级, 2: 二级}[int(risk)]（与开发期输出相同的映射，在此重写）。"""
    return RISK_TEXT[int(value)]                                  # type: ignore[call-overload]


def signal_cells(record: object) -> tuple[str, str, str, str, str]:
    """一条信号记录的五个已发布字段（risk、level、c1、c2、all_valid）按开发期输出规则格式化后的字符串。"""
    return (risk_text(record.risk), cell(record.level), cell(record.c1), cell(record.c2),   # type: ignore[attr-defined]
            cell(record.all_valid))                                                       # type: ignore[attr-defined]


def parse_signals(data: bytes) -> list[tuple[str, ...]]:
    """解析 daily_signals.csv.gz（gzip、UTF-8、csv 模块）；表头须恰为 date,object,risk,level,c1,c2,all_valid。
    返回数据行（原始字符串）。"""
    text = gzip.decompress(data).decode("utf-8")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or tuple(rows[0]) != SIGNALS_HEADER:
        raise OutputError(f"基准文件表头不是 {','.join(SIGNALS_HEADER)}")
    body = [tuple(row) for row in rows[1:]]
    if any(len(row) != len(SIGNALS_HEADER) for row in body):
        raise OutputError("基准文件中有字段数不为 7 的行")
    return body


# ---------------------------------------------------------------------------
# 确认性检验、诊断与描述性对照的文档
# ---------------------------------------------------------------------------


def _row_document(row: BootstrapRow) -> dict:
    return {"block": row.block, "seed": row.seed, "valid": row.valid, "p_value": row.p_value, "low": row.low,
            "high": row.high, "note": row.note}


def confirmatory_document(result: ConfirmatoryResult, period: PeriodSpec) -> dict:
    """确认性检验结果。主检验计算无效（登记第七节第 5 小节前五项）时只写原因，不写类别与任何优劣；
    敏感性单行无效（第六项）只标该行，报告“敏感性证据不完整”，不计通过、不计 p ≥ 0.10 的警示。"""
    parameters = period.confirmatory
    if not result.valid:
        return {"period": period.period, "valid": False, "outcome": INVALID, "reason": result.reason, "n": result.n,
                "category": None, "conclusion": INVALID, "statement": "计算无效：不写任何优劣结论，不计算必报诊断",
                "main": None, "sensitivities": [], "sensitivity_incomplete": None, "invalid_sensitivities": []}
    invalid_rows = [row for row in result.sensitivities if not row.valid]
    return {
        "period": period.period, "valid": True, "outcome": "有效", "reason": "", "n": result.n,
        "delta": result.delta, "delta_min": result.delta_min, "annual_growth": result.annual_growth,
        "main": None if result.main is None else _row_document(result.main),
        "sensitivities": [_row_document(row) for row in result.sensitivities],
        "sensitivity_incomplete": bool(invalid_rows),
        "invalid_sensitivities": [{"block": row.block, "seed": row.seed, "note": row.note} for row in invalid_rows],
        "sensitivity_note": ("无效的敏感性行不计为通过，也不计作 p ≥ 0.10 的警示；不改变类别，不触发“证据不稳定”"
                             if invalid_rows else None),
        "improvement_passed": result.improvement_passed, "magnitude_reached": result.magnitude_reached,
        "category": result.category, "unstable": result.unstable, "warnings": list(result.warnings),
        "halves": None if result.halves is None else {"first": result.halves[0], "second": result.halves[1],
                                                      "split": parameters.split},
        "zeroed": [{"asset": item.asset, "peak": item.peak, "delta": item.delta} for item in result.zeroed],
        "conclusion": result.conclusion, "own_result": result.own_result,
        "alpha": parameters.alpha, "warning_p": parameters.warning_p, "resamples": parameters.resamples,
    }


def diagnostics_document(computed: dict | None, reason: str | None) -> dict:
    if computed is None:
        return {"computed": False, "reason": reason or "计算无效时不计算本节"}
    return {"computed": True, **computed}


def _policy_summary(policy: object) -> dict:
    if isinstance(policy, PolicyResult):
        return {"complete": True, **compare.nav_summary(policy.nav)}
    if isinstance(policy, PartialPolicyResult):
        return {"complete": False, "undetermined": None if policy.undetermined is None else
                {"day": policy.undetermined.day, "reason": policy.undetermined.reason},
                "missing": [[asset, day] for asset, day in policy.missing]}
    return {"complete": None}


def descriptive_document(result: WindowResult, averages: Sequence[compare.AverageComparison],
                         constants: Sequence[compare.ConstantExposure]) -> dict:
    """描述性对照与执行政策研究模拟（只作描述，不参与确认性检验）。"""
    objects = []
    for candidate, outcome in result.candidates.items():
        objects.append((candidate_key(candidate), outcome.outcome))
    if result.reference is not None:
        objects.append((REFERENCE, result.reference.outcome))
    return {
        "objects": [{"object": name, "signal": compare.nav_summary(outcome.signal_nav),
                     "policy": _policy_summary(outcome.policy),
                     "switches": compare.switch_summary(outcome.switches),
                     "policy_minus_signal": compare.policy_minus_signal(outcome.signal_nav, outcome.policy)}
                    for name, outcome in objects],
        "hold": compare.nav_summary(None if result.common is None else result.common.hold),
        "averages": [{"name": item.name, "convergence_day": item.convergence_day, "evaluable": item.evaluable,
                      "note": item.note,
                      "signal": compare.nav_summary(item.signal_nav) if item.evaluable else None,
                      "policy": _policy_summary(item.policy) if item.evaluable else None,
                      "switches": compare.switch_summary(item.switches) if item.evaluable else None}
                     for item in averages],
        "constants": [{"object": item.object, "computed": item.computed, "note": item.note, "core": item.core,
                       "leverage": item.leverage, "exposure": item.exposure,
                       "nav": compare.nav_summary(item.nav) if item.computed else None} for item in constants],
        "note": "描述性对照与执行政策研究模拟只作描述，不参与确认性检验，不构成可实盘规则",
    }


# ---------------------------------------------------------------------------
# 报告（固定文字照《验证期通过标准》修订三原文）
# ---------------------------------------------------------------------------

# 修订三“时点（照实登记，B62）”一段（标准写明“验证期报告须照写本段”）。
TIMING_TEXT = (
    "修订三写于看到 M3 开发期结果之后、验证期正式确认性运行之前；此前已登记的元数据读取（2026-10-04 元数据核对读取过"
    "整份行情文件字节）与三起授权外事件（bt0、2026-10-07 检索、2026-10-17 仓库检索；第 5 项裁决、L-13）照旧，"
    "不据此声称从未接触验证期或保留期数据。第〇节第 7 条“读取任何验证期数据之前”一语为修订二·修订一的已批准原文，"
    "本稿不改；其含义同样按本段理解，即验证期正式确认性运行之前。起因是 V1 设计审查发现标准文字与 f = j₀ − 1 不一致，"
    "不是任何结果。它不改变第一、二节，不改变类别、稳定性警示、后续动作与判定线，也不动用第〇节第 5 条的修改例外。"
    "验证期报告须照写本段。")
# 第〇节第 7 条（修订二的时点；标准写明“验证期报告须照写本条”）。
SECTION0_ITEM7 = (
    "第五节写于看到 M3 开发期结果之后、读取任何验证期数据之前。",
    "设立它的直接原因是开发期结果：Gemini 审阅指出 R2 达标中持续覆盖占比偏高（B-02，项目负责人已用真实 M3 文件核实"
    "方向，A）。",
    "第五节不改动第二节，不改变类别、既有稳定性警示与后续动作，只增加报告内容；判定线只用登记的 60%，"
    "不取任何开发期数值作阈值。",
    "第〇节第 5 条所说“验证期运行之前另行登记”的修改例外，不得因 M3 或任何后续结果启动（与 10-14 批准原文一致）。"
    "第五节是新增的报告要求，不是对第二节的修改。",
    "验证期报告须照写本条。",
)
# 第三节第 5 项说明（验证期已见资料 L-13；标准写明“验证期报告须照写此项”）。
SECTION3_ITEM5 = (
    "登记第七稿日期为 2026-10-02（A），早于 bt0，这只支持时序说明。",
    "后续实施与判断（候选选择、锁定、检验实现）是否受 bt0 影响，**未获证明**；缺口保留，按既有裁决处理。",
    "验证期报告须照写此项，由发起人裁定其影响。",
)
WORDING_LIMIT = ("措辞上限：即使是 A 类，也只能写“在冻结后的历史检验中，满足风险与提示条件，收益改善检验通过，"
                 "且改善点估计达到登记幅度”；不得写成已统计证明改善至少达到 1%，本报告不构成任何实盘依据。")
OVERLAP_SCOPE = ("重叠状态比对的证明范围：正式比对只证明冻结候选与主参照的五个已发布字段"
                 "（risk、level、c1、c2、all_valid）在基准日期上逐日相同；完整状态（五个通道、S、c₁、c₂）"
                 "跨截止日逐日相同由构造测试 T-OVL 证明，本次运行的比对不证明通道内部状态。")


def data_scope_text(period: PeriodSpec) -> str:
    """C0′ 读取口径（V1 第三节第 2 条；数据哈希口径补记）：三层分开写。"""
    cutoff = period.cutoff.isoformat()
    return (f"数据读取口径：行情文件读入整份字节（含 {cutoff} 之后的行），核对原始字节与换行规范化"
            f"两种 SHA-256（数据哈希口径补记）；价格只解析 {cutoff}（含）以内的行；计算只用该日以内的"
            f"价格。{cutoff} 之后的行被读入字节、参与哈希，不解析价格、不参与任何计算，"
            "不得表述为“未读取”。")


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def render_report(period: PeriodSpec, window: Mapping | None, dates: Mapping, overlap: Mapping,
                  confirmatory: Mapping, diagnostics: Mapping, usage_restriction: Sequence[str],
                  holdout_gate: Mapping | None = None) -> bytes:
    """报告：用途限制逐项一行；固定说明；摘要首屏（结论与诊断警示）；依据；窗口与日期断言；重叠比对；确认性检验；
    必报诊断；说明。计算无效时不写类别与任何优劣。"""
    warnings = list(diagnostics.get("warnings", [])) if diagnostics.get("computed") else []
    lines = [f"# 波段预警 v2.0 {period.period}运行报告", *usage_restriction, "", HISTORICAL_NOTE, "", "## 摘要", ""]
    if window is not None:
        lines.append(f"- 期间：{period.period}；j₀ = {_fmt(window.get('first_day'))}；f = "
                     f"{_fmt(period.first_signal_day)}；E = {_fmt(window.get('last_day'))}；n = {window.get('n')}")
    if confirmatory.get("valid"):
        lines.append(f"- 结论：{confirmatory['conclusion']}")
        lines += [f"- {line}" for line in warnings]
    else:
        lines.append(f"- 结论：{INVALID}（{confirmatory.get('reason')}）；不写任何优劣结论，必报诊断不计算")
    lines += ["", "## 依据", "",
              f"- 《波段预警 v2.0 锁定记录》定稿：SHA-256 `{LOCK_RECORD_SHA256}`",
              f"- 《波段预警 v2.0 锁定补充记录（一）》：SHA-256 `{LOCK_SUPPLEMENT_SHA256}`（j₀、f 的唯一来源）",
              f"- 《验证期通过标准与后续决策登记》修订三：SHA-256 `{STANDARD_R3_SHA256}`",
              f"- 《阶段五 V1 设计稿》定稿：SHA-256 `{V1_DESIGN_SHA256}`",
              f"- {data_scope_text(period)}",
              "", "## 时点与已见资料（照写）", "", f"- {TIMING_TEXT}", "", "第〇节第 7 条：",
              *(f"- {line}" for line in SECTION0_ITEM7), "", "第三节第 5 项说明：",
              *(f"- {line}" for line in SECTION3_ITEM5),
              "", "## 窗口与日期断言", ""]
    for item in dates.get("checks", []):
        lines.append(f"- {item['item']}：预期 {_fmt(item['expected'])}，实际 {_fmt(item['actual'])}，"
                     f"{'相同' if item['passed'] else '不同'}")
    d15 = dates.get("d15")
    if d15:
        lines.append(f"- D15：j₀ ≥ t0 + 63（{d15['j0']} ≥ {d15['t0']} + {d15['offset']}）为 {d15['warmup_ok']}；"
                     f"j₀ ≥ κ + 1（{d15['j0']} ≥ {d15['kappa_all']} + 1）为 {d15['convergence_ok']}")
    lines += ["", "## 重叠状态比对", "", f"- 基准：{overlap.get('baseline')}；结果：{overlap.get('outcome')}",
              f"- {OVERLAP_SCOPE}", "", "## 确认性检验", ""]
    if confirmatory.get("valid"):
        main = confirmatory["main"]
        lines += [f"- n = {confirmatory['n']}；Δ = {confirmatory['delta']!r}；Δ_min = {confirmatory['delta_min']!r}；"
                  f"年化相对净值增长率 {confirmatory['annual_growth']!r}",
                  f"- 主设定 b = {main['block']}（种子 {main['seed']}）：p = {main['p_value']!r}",
                  *(f"- 敏感性 b = {row['block']}（种子 {row['seed']}）："
                    + (f"p = {row['p_value']!r}" if row["valid"] else f"无效（{row['note']}）")
                    for row in confirmatory["sensitivities"])]
        if confirmatory["sensitivity_incomplete"]:
            blocks = "、".join(f"b = {row['block']}" for row in confirmatory["invalid_sensitivities"])
            lines.append(f"- {SENSITIVITY_INCOMPLETE}（无效行：{blocks}）：不计为通过，也不计作 p ≥ 0.10 的警示")
        lines += [f"- 收益改善检验通过：{confirmatory['improvement_passed']}；改善点估计达到登记幅度："
                  f"{confirmatory['magnitude_reached']}",
                  f"- 稳定性警示：{'、'.join(confirmatory['warnings']) or '无'}",
                  f"- 类别：{confirmatory['category']}；结论：{confirmatory['conclusion']}"]
        lines += warnings
        if confirmatory.get("own_result"):
            lines.append(f"- {confirmatory['own_result']}")
    else:
        lines.append(f"- {INVALID}：{confirmatory.get('reason')}；不写类别与任何优劣结论。")
    lines += ["", "## 必报诊断", ""]
    if diagnostics.get("computed"):
        for item in diagnostics["d1_new_prompt_share"]:
            lines.append(f"- D-1 {item['asset']}：新提示达标 {item['new_achieved']}，持续覆盖达标 "
                         f"{item['covered_achieved']}，R2 达标 {item['r2_achieved']}，分母 {item['denominator']}；"
                         f"新提示占比 ≥ 60%：{item['new_share_at_least_60']}")
        lines += [f"- {line}" for line in diagnostics["statements"]]
        occupancy = diagnostics["d2_abnormal_occupancy"]
        lines.append(f"- D-2：窗口信号日 {occupancy['window_days']}，一级 {occupancy['level1_days']}，二级 "
                     f"{occupancy['level2_days']}，状态无法确定 {occupancy['undetermined_days']}（计入分母）")
        for item in diagnostics["d3_long_hang"]:
            lines.append(f"- D-3 {item['asset']}：提示段 {item['segments_count']} 个，e ≥ 2 的段 "
                         f"{item['e_at_least_2']} 个，c 最大值 {item['c_max']}；“剔除单段”判断："
                         f"{item['removal_check']['result']}")
        lines += [f"- {line}" for line in diagnostics["warnings"]]
        lines.append(f"- {diagnostics['reclassification_note']}")
    else:
        lines.append(f"- 未计算：{diagnostics.get('reason')}")
    lines += ["", "## 说明", "", f"- {WORDING_LIMIT}",
              "- 描述性对照与执行政策研究模拟只作描述，不参与确认性检验，不构成可实盘规则。",
              "- 诊断警示与稳定性警示分开列示，不计入“证据不稳定”，不改变类别、资格与后续动作。"]
    if holdout_gate is not None:
        lines.append(f"- 保留期启用凭据：验证期清单 SHA-256 `{holdout_gate['validation_manifest_sha256']}`（类别 "
                     f"{holdout_gate['validation_category']}）；授权原文正文 SHA-256 "
                     f"`{holdout_gate['authorization_sha256']}`（入口只校验格式并照录，程序未验证授权）。")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# 稳定计算文件
# ---------------------------------------------------------------------------


def stable_files(snapshot: Snapshot, result: WindowResult, period: PeriodSpec, run_record: Mapping,
                 dates: Mapping, overlap: Mapping, confirmatory: Mapping, diagnostics: Mapping, descriptive: Mapping,
                 usage_restriction: Sequence[str], holdout_gate: Mapping | None = None,
                 note: Callable[[str], None] | None = None) -> dict[str, bytes]:
    """全部稳定计算文件（information_state.json 除外，它由调用方最后写出），按写出顺序排列。"""

    def mark(name: str) -> None:
        if note is not None:
            note(name)

    window = window_document(snapshot, result)
    files: dict[str, bytes] = {"run_record.json": json_bytes(run_record)}
    files["input_snapshot.csv.gz"] = serialize_snapshot(snapshot)
    files["window.json"] = json_bytes(window)
    files[DATE_ASSERTIONS] = json_bytes(dates)
    files[OVERLAP] = json_bytes(overlap)
    files[CONFIRMATORY] = json_bytes(confirmatory)
    files[diagnostics_name(period)] = json_bytes(diagnostics)
    files[DESCRIPTIVE] = json_bytes(descriptive)
    mark("汇总文件")
    files.update(serialize_daily(result, None))
    files.update(serialize_r2(result))
    mark("逐日明细")
    files[REPORT] = render_report(period, window.get("window"), dates, overlap, confirmatory, diagnostics,
                                  usage_restriction, holdout_gate)
    return files


def read_json(data: bytes) -> object:
    return json.loads(data.decode("utf-8"))
