"""数据留痕（T3 第一步）的规则（纯计算）：记录的字段、校验、规范化、迟到判断、修订与确认。

只建设录入留痕；不接入任何评分、信号或研究计算，不替换它们的既有输入。
不读写文件，不访问数据库；读写在 storage/provenance_store.py。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

RECORD_PREFIX = "PR-"
SOURCE_REVISION, MANUAL_CORRECTION = "来源修订", "人工修正"
UNCONFIRMED, CONFIRMED, SELF_CONFIRMED = "未确认", "已确认", "自确认"
_RAW_VALUE = re.compile(r"^\d+(\.\d+)?$")


class ProvenanceError(ValueError):
    """录入、修订或确认不符合留痕规则。"""


@dataclass(frozen=True)
class IndicatorSpec:
    code: str
    unit: str                      # 原始单位
    basis: str                     # 口径
    minimum: Decimal               # 规范化值的取值范围（含两端）
    maximum: Decimal


@dataclass(frozen=True)
class ProvenanceConfig:
    indicators: Mapping[str, IndicatorSpec]
    timezone: str                  # 迟到判断与“首次取得时间”所用的时区（美东）
    late_cutoff: dt.time           # 指标对应交易日当日的截止时刻
    max_precision: int             # 原始值小数位数的上限（超过即拒绝，不舍入）
    methods: tuple[str, ...]       # 取得方式
    revision_kinds: tuple[str, ...]
    lock_timeout_seconds: float    # 分配记录编号时等待排他锁的最长时间


@dataclass(frozen=True)
class Snapshot:
    """原始快照文件：相对存储根目录的路径与 SHA-256。"""

    path: str
    sha256: str


@dataclass(frozen=True)
class SourceFile:
    """已有的来源文件（如已入库的原始导出）：相对存储根目录的路径与该文件自身的 SHA-256。"""

    path: str
    sha256: str


@dataclass(frozen=True)
class EntryRequest:
    """一次录入的内容。时间都必须带时区；无法证明或无法核实的时间留空，不用别的时间代替。"""

    indicator: str
    trade_date: dt.date                        # 指标对应的交易日
    raw_value: str                             # 原始值，按录入文本原样保存
    source: str
    method: str                                # 接口 或 手工录入
    first_obtained_at: dt.datetime | None      # 首次取得时间；无法证明时为空
    historical_backfill: bool                  # 历史补录
    source_published_at: dt.datetime | None    # 来源发布时间；无法核实时为空
    entered_by: str
    snapshot: Snapshot | None
    snapshot_missing_reason: str               # 没有原始快照时必须写明原因；有快照时留空
    source_file: SourceFile | None             # 已有的来源文件；没有时为空
    data_version: str | None                   # 指标所在数据集清单的版本；指标不在数据集中时为空


@dataclass(frozen=True)
class RevisionRequest:
    """修订关系：新记录指向被修订的记录，原记录不覆盖。"""

    revises: str                               # 被修订的记录编号
    kind: str                                  # 来源修订 或 人工修正
    evidence: str                              # 人工修正的证据；来源修订可为空


@dataclass(frozen=True)
class CodeVersion:
    commit: str
    dirty: bool | None                         # 录入时工作区是否有未提交改动；取不到时为空


@dataclass(frozen=True)
class ProvenanceRecord:
    record_id: str
    indicator: str
    trade_date: dt.date
    raw_value: str
    raw_unit: str
    raw_basis: str
    raw_precision: int                         # 原始值的小数位数
    normalized_value: Decimal                  # 规范化值（百分数），与原始值并存
    source: str
    acquisition_method: str
    first_obtained_at_et: dt.datetime | None   # 美东时间
    entered_at_utc: dt.datetime
    entered_by: str
    is_late: bool | None                       # 首次取得时间为空时无法判断，为空
    source_published_at: dt.datetime | None
    snapshot_path: str | None
    snapshot_sha256: str | None
    data_version: str | None
    code_version: str
    code_dirty: bool | None
    revises_record_id: str | None
    revision_kind: str | None
    correction_original_value: str | None      # 人工修正：被修正记录的原始值
    correction_corrected_value: str | None     # 人工修正：修正值（即本记录的原始值）
    correction_evidence: str | None
    historical_backfill: bool
    snapshot_missing_reason: str | None        # 没有原始快照的原因；有快照时为空
    source_file_path: str | None               # 已有来源文件的路径
    source_file_sha256: str | None             # 该来源文件自身的 SHA-256


@dataclass(frozen=True)
class Confirmation:
    record_id: str
    confirmed_by: str
    confirmed_at_utc: dt.datetime
    self_confirmed: bool                       # 确认人与录入人相同


def record_number(record_id: str) -> int:
    """记录编号里的顺序号；格式为 PR- 加至少六位数字。"""
    if not re.fullmatch(rf"{RECORD_PREFIX}\d{{6,}}", record_id):
        raise ProvenanceError(f"记录编号不合法：{record_id!r}")
    return int(record_id[len(RECORD_PREFIX):])


def next_record_id(existing: Sequence[str], issued: int) -> str:
    """唯一记录编号：PR- 加顺序号，接在“已有编号的最大值”与“已发出的最大顺序号”两者较大者之后。

    issued 是持久保存的已发出的最大顺序号：即使记录文件里的行被删除，编号也不复用。
    顺序号不足六位时补零，超过六位时位数自动增长，不设数量上限。
    """
    numbers = [record_number(item) for item in existing]
    if len(set(numbers)) != len(numbers):
        raise ProvenanceError("已有的记录编号有重复")
    if issued < 0:
        raise ProvenanceError("已发出的最大顺序号不能为负")
    return f"{RECORD_PREFIX}{max(max(numbers, default=0), issued) + 1:06d}"


def normalize(raw_value: str, spec: IndicatorSpec, max_precision: int) -> tuple[Decimal, int]:
    """原始值 → （规范化值，原始值的小数位数）。规范化值不做额外舍入；超出取值范围即拒绝。"""
    if not _RAW_VALUE.fullmatch(raw_value):
        raise ProvenanceError(f"原始值须是不带符号与单位的十进制数：{raw_value!r}")
    precision = len(raw_value.partition(".")[2])
    if precision > max_precision:
        raise ProvenanceError(f"原始值有 {precision} 位小数，超过可无损保存的 {max_precision} 位，拒绝录入")
    value = Decimal(raw_value)
    if not spec.minimum <= value <= spec.maximum:
        raise ProvenanceError(f"{spec.code} 的取值须在 {spec.minimum} 至 {spec.maximum} 之间，收到 {raw_value}")
    return value, precision


def _aware(value: dt.datetime, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProvenanceError(f"{name}必须带时区")
    return value


def is_late(first_obtained_at: dt.datetime | None, trade_date: dt.date, config: ProvenanceConfig) -> bool | None:
    """是否迟到：首次取得时间严格晚于指标对应交易日的美东截止时刻（18:30:00）才算；恰为截止时刻不算。

    首次取得时间为空时无法判断，返回空。
    """
    if first_obtained_at is None:
        return None
    zone = ZoneInfo(config.timezone)
    cutoff = dt.datetime.combine(trade_date, config.late_cutoff, tzinfo=zone)
    return _aware(first_obtained_at, "首次取得时间").astimezone(zone) > cutoff


def _first_obtained(request: EntryRequest, entered_at: dt.datetime, config: ProvenanceConfig) -> dt.datetime | None:
    """首次取得时间换成美东时间；无法证明时留空并要求标“历史补录”，不得用录入时间或交易日代替。"""
    if request.first_obtained_at is None:
        if not request.historical_backfill:
            raise ProvenanceError("首次取得时间为空时必须标“历史补录”；能证明取得时间的，请写明带时区的时间")
        return None
    value = _aware(request.first_obtained_at, "首次取得时间").astimezone(ZoneInfo(config.timezone))
    if value.date() < request.trade_date:
        raise ProvenanceError("首次取得时间早于指标对应的交易日")
    if value > entered_at:
        raise ProvenanceError("首次取得时间晚于录入时间")
    return value


def build_record(record_id: str, request: EntryRequest, revision: RevisionRequest | None,
                 original: ProvenanceRecord | None, entered_at: dt.datetime, code: CodeVersion,
                 config: ProvenanceConfig) -> ProvenanceRecord:
    """校验并生成一条记录。revision 不为空时 original 是被修订的记录；原记录不改动。"""
    spec = config.indicators.get(request.indicator)
    if spec is None:
        raise ProvenanceError(f"本批不支持的指标：{request.indicator!r}（支持 {'、'.join(config.indicators)}）")
    if request.method not in config.methods:
        raise ProvenanceError(f"取得方式须是 {'、'.join(config.methods)} 之一")
    if not request.entered_by.strip() or not request.source.strip():
        raise ProvenanceError("录入人与来源不能为空")
    if request.snapshot is not None and not (request.snapshot.path and request.snapshot.sha256):
        raise ProvenanceError("原始快照须同时有路径与 SHA-256")
    reason = request.snapshot_missing_reason.strip()
    if request.snapshot is None and not reason:
        raise ProvenanceError("没有原始快照时必须写明缺失原因")
    if request.snapshot is not None and reason:
        raise ProvenanceError("已有原始快照，不应再写缺失原因")
    source = request.source_file
    if source is not None and not (source.path and source.sha256):
        raise ProvenanceError("来源文件须同时有路径与 SHA-256")
    entered = _aware(entered_at, "录入时间").astimezone(dt.UTC)
    value, precision = normalize(request.raw_value, spec, config.max_precision)
    first = _first_obtained(request, entered, config)
    published = None if request.source_published_at is None else _aware(request.source_published_at, "来源发布时间")
    kind = original_value = corrected = evidence = None
    if revision is not None:
        if original is None or original.record_id != revision.revises:
            raise ProvenanceError(f"找不到被修订的记录：{revision.revises}")
        if (original.indicator, original.trade_date) != (request.indicator, request.trade_date):
            raise ProvenanceError("修订记录的指标与交易日须与被修订的记录相同")
        if revision.kind not in config.revision_kinds:
            raise ProvenanceError(f"修订类型须是 {'、'.join(config.revision_kinds)} 之一")
        kind = revision.kind
        if kind == MANUAL_CORRECTION:
            if not revision.evidence.strip():
                raise ProvenanceError("人工修正必须写明证据")
            original_value, corrected, evidence = original.raw_value, request.raw_value, revision.evidence
        elif revision.evidence.strip():
            evidence = revision.evidence
    snapshot = request.snapshot
    return ProvenanceRecord(
        record_id, request.indicator, request.trade_date, request.raw_value, spec.unit, spec.basis, precision, value,
        request.source, request.method, first, entered, request.entered_by, is_late(first, request.trade_date, config),
        published, snapshot.path if snapshot else None, snapshot.sha256 if snapshot else None,
        request.data_version or None, code.commit, code.dirty,
        revision.revises if revision else None, kind, original_value, corrected, evidence,
        request.historical_backfill, reason or None, source.path if source else None,
        source.sha256 if source else None)


def confirm(record: ProvenanceRecord, existing: Sequence[Confirmation], confirmed_by: str,
            confirmed_at: dt.datetime) -> Confirmation:
    """确认一条记录；每条记录只确认一次。不强制确认人不同于录入人，两者相同时标“自确认”。"""
    if not confirmed_by.strip():
        raise ProvenanceError("确认人不能为空")
    if any(item.record_id == record.record_id for item in existing):
        raise ProvenanceError(f"{record.record_id} 已经确认过，确认记录不覆盖")
    at = _aware(confirmed_at, "确认时间").astimezone(dt.UTC)
    if at < record.entered_at_utc:
        raise ProvenanceError("确认时间早于录入时间")
    return Confirmation(record.record_id, confirmed_by, at, confirmed_by == record.entered_by)


@dataclass(frozen=True)
class RecordView:
    """查询结果的一行：记录、确认状态、是否已被更新的记录修订。"""

    record: ProvenanceRecord
    confirmation: Confirmation | None
    revised_by: tuple[str, ...]                # 修订了本记录的记录编号

    @property
    def status(self) -> str:
        if self.confirmation is None:
            return UNCONFIRMED
        return SELF_CONFIRMED if self.confirmation.self_confirmed else CONFIRMED

    @property
    def snapshot(self) -> str:
        """查询结果里的快照一栏：有快照写“有”，没有时写“无快照”及原因。"""
        if self.record.snapshot_path:
            return "有"
        return f"无快照：{self.record.snapshot_missing_reason}"


def record_views(records: Sequence[ProvenanceRecord], confirmations: Sequence[Confirmation]) -> tuple[RecordView, ...]:
    by_record = {item.record_id: item for item in confirmations}
    revised: dict[str, list[str]] = {}
    for record in records:
        if record.revises_record_id:
            revised.setdefault(record.revises_record_id, []).append(record.record_id)
    return tuple(RecordView(record, by_record.get(record.record_id), tuple(revised.get(record.record_id, ())))
                 for record in records)


def select_views(views: Sequence[RecordView], indicator: str | None, start: dt.date | None,
                 end: dt.date | None) -> tuple[RecordView, ...]:
    return tuple(view for view in views
                 if (indicator is None or view.record.indicator == indicator)
                 and (start is None or view.record.trade_date >= start)
                 and (end is None or view.record.trade_date <= end))
