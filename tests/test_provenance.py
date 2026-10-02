"""数据留痕第一步（T3）的测试：只写入临时目录，不接触真实的 data/、db/。"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from sqlalchemy import select

from market_risk import provenance as rules
from market_risk import services
from market_risk.config import PROJECT_ROOT, load_settings
from market_risk.provenance_config import load_provenance_config, parse_provenance_config
from market_risk.storage import db, schema
from market_risk.storage import provenance_store as store
from market_risk.storage.paths import StoragePaths

CONFIG = load_provenance_config(PROJECT_ROOT / "config")
CODE = rules.CodeVersion("0123456789abcdef0123456789abcdef01234567", False)
NEW_YORK = ZoneInfo("America/New_York")
DAY = dt.date(2026, 9, 25)                                    # 指标对应的交易日（构造的录入，不读任何市场数据）
OBTAINED = dt.datetime(2026, 9, 25, 17, 0, tzinfo=NEW_YORK)   # 当日美东 17:00 取得
ENTERED = dt.datetime(2026, 9, 25, 22, 0, tzinfo=dt.UTC)      # 美东 18:00 录入
NO_SNAPSHOT = "手工看屏录入，没有文件"


@pytest.fixture
def ctx(tmp_path: Path) -> services.Context:
    paths = StoragePaths(tmp_path)
    return services.Context(load_settings(), paths, db.default_url(paths))


def _add(ctx: services.Context, value: str = "54.67", **overrides: object) -> rules.ProvenanceRecord:
    arguments: dict[str, object] = {
        "indicator": "S5FI", "trade_date": DAY, "raw_value": value, "source": "TradingView INDEX:S5FI",
        "method": "手工录入", "entered_by": "stone", "first_obtained_at": OBTAINED, "now": ENTERED,
        "config": CONFIG, "code": CODE}
    arguments.update(overrides)
    if arguments.get("snapshot_file") is None:
        arguments.setdefault("snapshot_missing_reason", NO_SNAPSHOT)
    return services.provenance_add(ctx, **arguments)  # type: ignore[arg-type]


def _table(ctx: services.Context, table: object) -> list[dict[str, object]]:
    engine = db.make_engine(ctx.db_url)
    try:
        with engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(select(table))]  # type: ignore[call-overload]
    finally:
        engine.dispose()


def test_revision_never_overwrites_the_original_record(ctx: services.Context) -> None:
    """先录入 54.67，再做一次“人工修正”为 54.76：文件只追加一行，原记录那一行逐字节不变；

    新记录指向原记录，带原值、修正值与证据；查询时原记录显示“被 PR-000002 修订”。数据库里两条都在。
    """
    first = _add(ctx)
    before = ctx.paths.provenance_records_csv.read_bytes()
    second = services.provenance_revise(ctx, first.record_id, "人工修正", "54.76", "TradingView INDEX:S5FI", "手工录入",
                                        "stone", "截图读数为 54.76，原录入把后两位写反", OBTAINED,
                                        snapshot_missing_reason=NO_SNAPSHOT, now=ENTERED, config=CONFIG, code=CODE)
    after = ctx.paths.provenance_records_csv.read_bytes()
    assert after.startswith(before) and after.count(b"\n") == before.count(b"\n") + 1
    assert (second.record_id, second.revises_record_id, second.revision_kind) == ("PR-000002", "PR-000001", "人工修正")
    assert (second.correction_original_value, second.correction_corrected_value) == ("54.67", "54.76")
    assert second.correction_evidence == "截图读数为 54.76，原录入把后两位写反"
    records = store.read_records(ctx.paths)
    assert records[0] == first and records[0].raw_value == "54.67"
    views = services.provenance_list(ctx)
    assert views[0].revised_by == ("PR-000002",) and views[1].revised_by == ()
    rows = {row["record_id"]: row for row in _table(ctx, schema.provenance_records)}
    assert rows["PR-000001"]["raw_value"] == "54.67" and rows["PR-000002"]["revises_record_id"] == "PR-000001"
    # 人工修正必须写证据；修订类型只有两种；被修订的记录必须存在。
    with pytest.raises(services.ServiceError, match="证据"):
        services.provenance_revise(ctx, first.record_id, "人工修正", "54.70", "x", "手工录入", "stone", "", OBTAINED,
                                   snapshot_missing_reason=NO_SNAPSHOT, now=ENTERED, config=CONFIG, code=CODE)
    with pytest.raises(services.ServiceError, match="修订类型"):
        services.provenance_revise(ctx, first.record_id, "覆盖", "54.70", "x", "手工录入", "stone", "", OBTAINED,
                                   snapshot_missing_reason=NO_SNAPSHOT, now=ENTERED, config=CONFIG, code=CODE)
    with pytest.raises(services.ServiceError, match="找不到"):
        services.provenance_revise(ctx, "PR-000009", "来源修订", "54.70", "x", "手工录入", "stone", "", OBTAINED,
                                   snapshot_missing_reason=NO_SNAPSHOT, now=ENTERED, config=CONFIG, code=CODE)
    assert len(store.read_records(ctx.paths)) == 2                      # 被拒绝的修订没有留下任何行
    # 来源修订不带人工修正的三个字段。
    third = services.provenance_revise(ctx, second.record_id, "来源修订", "54.80", "TradingView INDEX:S5FI", "接口",
                                       "stone", "", OBTAINED, snapshot_missing_reason=NO_SNAPSHOT, now=ENTERED,
                                       config=CONFIG, code=CODE)
    assert (third.revision_kind, third.correction_original_value, third.correction_corrected_value) == (
        "来源修订", None, None)


def test_source_published_time_stays_empty_when_it_cannot_be_verified(ctx: services.Context) -> None:
    """来源发布时间没有给出（无法核实）时为空：不用首次取得时间、录入时间或交易日代替。给出时原样保存。"""
    record = _add(ctx)
    assert record.source_published_at is None and record.first_obtained_at_et is not None
    row = _table(ctx, schema.provenance_records)[0]
    assert row["source_published_at"] is None and row["first_obtained_at_et"] == "2026-09-25T17:00:00-04:00"
    published = dt.datetime(2026, 9, 25, 16, 15, tzinfo=NEW_YORK)
    other = _add(ctx, source_published_at=published)
    assert other.source_published_at == published
    with pytest.raises(services.ServiceError, match="来源发布时间必须带时区"):
        _add(ctx, source_published_at=dt.datetime(2026, 9, 25, 16, 15))


def test_historical_backfill_never_pretends_the_value_was_obtained_back_then(ctx: services.Context) -> None:
    """历史补录：2015-06-01 的读数在 2026 年录入，首次取得时间无法证明 → 留空并标“历史补录”，迟到无法判断（空）。

    不标“历史补录”又不给首次取得时间的录入被拒绝，程序不会用交易日或录入时间去填。
    能证明取得时间的补录可以同时带时间与标记；首次取得时间早于交易日、或晚于录入时间的都被拒绝。
    """
    old = dt.date(2015, 6, 1)
    record = _add(ctx, trade_date=old, first_obtained_at=None, historical_backfill=True)
    assert record.first_obtained_at_et is None and record.historical_backfill and record.is_late is None
    row = _table(ctx, schema.provenance_records)[0]
    assert row["first_obtained_at_et"] is None and row["historical_backfill"] is True and row["is_late"] is None
    assert "2015-06-01T" not in ctx.paths.provenance_records_csv.read_text(encoding="utf-8")
    with pytest.raises(services.ServiceError, match="历史补录"):
        _add(ctx, trade_date=old, first_obtained_at=None)
    proven = _add(ctx, trade_date=old, first_obtained_at=dt.datetime(2026, 9, 20, 10, 0, tzinfo=NEW_YORK),
                  historical_backfill=True)
    assert proven.historical_backfill and proven.is_late is True
    with pytest.raises(services.ServiceError, match="早于指标对应的交易日"):
        _add(ctx, first_obtained_at=dt.datetime(2026, 9, 24, 23, 0, tzinfo=NEW_YORK))
    with pytest.raises(services.ServiceError, match="晚于录入时间"):
        _add(ctx, first_obtained_at=dt.datetime(2026, 9, 25, 19, 0, tzinfo=NEW_YORK))
    with pytest.raises(services.ServiceError, match="必须带时区"):
        _add(ctx, first_obtained_at=dt.datetime(2026, 9, 25, 17, 0))


def test_late_boundary_is_strictly_after_1830_eastern() -> None:
    """相对指标对应交易日的美东 18:30:00：恰为 18:30:00 不算迟到，18:30:01 算，18:29:59 不算。

    时间以别的时区给出时先换成美东：2026-09-25（夏令时，UTC−4）的 18:30 美东 = 22:30 UTC；
    2026-12-18（冬令时，UTC−5）的 18:30 美东 = 23:30 UTC。次日取得一律迟到。首次取得时间为空时无法判断。
    """
    def late(moment: dt.datetime, day: dt.date = DAY) -> bool | None:
        return rules.is_late(moment, day, CONFIG)

    assert late(dt.datetime(2026, 9, 25, 18, 30, 0, tzinfo=NEW_YORK)) is False
    assert late(dt.datetime(2026, 9, 25, 18, 30, 1, tzinfo=NEW_YORK)) is True
    assert late(dt.datetime(2026, 9, 25, 18, 29, 59, tzinfo=NEW_YORK)) is False
    assert late(dt.datetime(2026, 9, 25, 22, 30, 0, tzinfo=dt.UTC)) is False
    assert late(dt.datetime(2026, 9, 25, 22, 30, 1, tzinfo=dt.UTC)) is True
    winter = dt.date(2026, 12, 18)
    assert late(dt.datetime(2026, 12, 18, 23, 30, 0, tzinfo=dt.UTC), winter) is False
    assert late(dt.datetime(2026, 12, 18, 23, 30, 1, tzinfo=dt.UTC), winter) is True
    assert late(dt.datetime(2026, 12, 18, 22, 30, 1, tzinfo=dt.UTC), winter) is False      # 冬令时这是美东 17:30
    assert late(dt.datetime(2026, 9, 26, 9, 0, tzinfo=NEW_YORK)) is True
    assert rules.is_late(None, DAY, CONFIG) is None
    with pytest.raises(rules.ProvenanceError, match="必须带时区"):
        late(dt.datetime(2026, 9, 25, 18, 30))


def test_raw_and_normalized_values_are_kept_side_by_side(ctx: services.Context) -> None:
    """原始值按录入文本原样保存（“54.670”保留末尾的 0，小数位数记 3），规范化值是同一个数的 Decimal，不另做舍入。

    取值范围 0 至 100（含两端）：0 与 100 可以录入，100.01、带符号或带单位的写法被拒绝；
    小数位数超过 8 位（规范化值无法无损保存）也被拒绝。
    """
    record = _add(ctx, "54.670")
    assert (record.raw_value, record.raw_unit, record.raw_precision) == ("54.670", "%", 3)
    assert record.normalized_value == Decimal("54.67") and str(record.normalized_value) == "54.670"
    assert record.raw_basis == "标普500成分股站上50日均线比例，日线收盘值"
    row = _table(ctx, schema.provenance_records)[0]
    assert row["raw_value"] == "54.670" and row["normalized_value"] == Decimal("54.67")
    assert store.read_records(ctx.paths)[0] == record                    # 文件读回与写入的记录相同
    spec = CONFIG.indicators["S5FI"]
    assert rules.normalize("0", spec, 8) == (Decimal("0"), 0) and rules.normalize("100", spec, 8)[0] == 100
    for bad in ("100.01", "-1", "54.67%", "5e1", "", " 54.67", "1.123456789"):
        with pytest.raises(rules.ProvenanceError):
            rules.normalize(bad, spec, 8)
    with pytest.raises(services.ServiceError, match="取值须在 0 至 100 之间"):
        _add(ctx, "100.01")
    with pytest.raises(services.ServiceError, match="不支持的指标"):
        _add(ctx, indicator="MMFI")
    with pytest.raises(services.ServiceError, match="取得方式"):
        _add(ctx, method="估算")
    assert len(store.read_records(ctx.paths)) == 1                        # 被拒绝的录入没有留下任何行
    # 第一批覆盖的三项指标都能录入。
    assert [_add(ctx, "40.00", indicator=name).indicator for name in ("S5TW", "NDTW")] == ["S5TW", "NDTW"]


def test_snapshot_hash_is_recorded_and_checked(ctx: services.Context, tmp_path: Path) -> None:
    """原始快照复制到 snapshots/<指标>/<交易日>/，记录相对路径与 SHA-256；核对时逐条重算。

    快照文件被改动一个字节 → 报“SHA-256 与记录不符”；文件被删掉 → 报“不存在”。
    """
    source = tmp_path / "upload" / "s5fi.png"
    source.parent.mkdir()
    source.write_bytes(b"snapshot-bytes")
    record = _add(ctx, snapshot_file=source)
    expected = store.file_sha256(source)
    assert record.snapshot_sha256 == expected and len(expected) == 64
    assert record.snapshot_path == f"data/manual/provenance/snapshots/S5FI/2026-09-25/{expected[:12]}_s5fi.png"
    stored = ctx.paths.root / record.snapshot_path
    assert stored.read_bytes() == b"snapshot-bytes" and services.provenance_verify_snapshots(ctx) == []
    plain = _add(ctx)                                                     # 没有快照的记录不参加核对
    assert plain.snapshot_path is None and plain.snapshot_sha256 is None
    stored.write_bytes(b"snapshot-bytez")
    assert services.provenance_verify_snapshots(ctx) == [
        f"PR-000001：快照文件的 SHA-256 与记录不符（{record.snapshot_path}）"]
    stored.unlink()
    assert services.provenance_verify_snapshots(ctx) == [f"PR-000001：快照文件不存在（{record.snapshot_path}）"]
    with pytest.raises(services.ServiceError, match="不存在"):
        _add(ctx, snapshot_file=tmp_path / "upload" / "missing.png")


def test_confirmation_is_a_separate_step_and_marks_self_confirmation(ctx: services.Context) -> None:
    """录入与确认分两步：刚录入的记录是“未确认”。确认人与录入人相同 → “自确认”；不同 → “已确认”。

    确认记录另存一个只追加的文件，不改记录那一行；同一条记录不能确认两次。
    """
    first, second = _add(ctx), _add(ctx, "55.00")
    assert [view.status for view in services.provenance_list(ctx)] == ["未确认", "未确认"]
    before = ctx.paths.provenance_records_csv.read_bytes()
    at = dt.datetime(2026, 9, 26, 1, 0, tzinfo=dt.UTC)
    own = services.provenance_confirm(ctx, first.record_id, "stone", at, CONFIG)
    other = services.provenance_confirm(ctx, second.record_id, "reviewer", at, CONFIG)
    assert (own.self_confirmed, other.self_confirmed) == (True, False)
    assert [view.status for view in services.provenance_list(ctx)] == ["自确认", "已确认"]
    assert ctx.paths.provenance_records_csv.read_bytes() == before
    rows = {row["record_id"]: row for row in _table(ctx, schema.provenance_confirmations)}
    assert rows["PR-000001"]["self_confirmed"] is True and rows["PR-000002"]["confirmed_by"] == "reviewer"
    with pytest.raises(services.ServiceError, match="已经确认过"):
        services.provenance_confirm(ctx, first.record_id, "reviewer", at, CONFIG)
    with pytest.raises(services.ServiceError, match="找不到记录"):
        services.provenance_confirm(ctx, "PR-000099", "stone", at, CONFIG)
    with pytest.raises(services.ServiceError, match="早于录入时间"):
        services.provenance_confirm(ctx, _add(ctx, "56.00").record_id, "stone", ENTERED - dt.timedelta(hours=1),
                                    CONFIG)


def test_database_is_rebuilt_from_the_append_only_files(ctx: services.Context) -> None:
    """数据库的三张表由文件重建：删掉数据库再重建，内容相同；预留的信号输入关联表本批为空。

    录入时记下代码提交号与“工作区是否有未提交改动”；数据版本在没有数据集清单时为空。
    """
    record = _add(ctx, code=rules.CodeVersion("f" * 40, True))
    services.provenance_confirm(ctx, record.record_id, "stone", ENTERED, CONFIG)
    assert (record.code_version, record.code_dirty, record.data_version) == ("f" * 40, True, None)
    first = db.dump(ctx.db_url)
    db.rebuild(ctx.paths, ctx.db_url)
    assert db.dump(ctx.db_url) == first
    assert len(first["provenance_records"]) == 1 and len(first["provenance_confirmations"]) == 1
    assert first["signal_input_links"] == []
    assert _table(ctx, schema.provenance_records)[0]["code_dirty"] is True
    # 查询按指标与日期筛选。
    _add(ctx, "40.00", indicator="NDTW", trade_date=dt.date(2026, 9, 24),
         first_obtained_at=dt.datetime(2026, 9, 24, 17, 0, tzinfo=NEW_YORK))
    assert [view.record.indicator for view in services.provenance_list(ctx, "NDTW")] == ["NDTW"]
    assert [view.record.record_id for view in services.provenance_list(ctx, None, DAY, DAY)] == ["PR-000001"]
    # 文件里的引用指向不存在的记录时，重建拒绝。
    with ctx.paths.provenance_confirmations_csv.open("a", encoding="utf-8", newline="") as file:
        file.write("PR-000077,stone,2026-09-26T00:00:00+00:00,是\n")
    with pytest.raises(rules.ProvenanceError, match="不存在的记录"):
        db.rebuild(ctx.paths, ctx.db_url)


def test_record_ids_are_sequential_and_config_matches_the_ruling() -> None:
    assert rules.next_record_id([], 0) == "PR-000001"
    assert rules.next_record_id(["PR-000001", "PR-000007"], 0) == "PR-000008"
    with pytest.raises(rules.ProvenanceError):
        rules.next_record_id(["X-1"], 0)
    assert set(CONFIG.indicators) == {"S5FI", "S5TW", "NDTW"} and CONFIG.late_cutoff == dt.time(18, 30)
    with (PROJECT_ROOT / "config" / "provenance.yaml").open(encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    with pytest.raises(ValueError, match="18:30:00"):
        parse_provenance_config({**raw, "late_cutoff": "18:00:00"})
    with pytest.raises(ValueError, match="小数位数"):
        parse_provenance_config({**raw, "max_precision": 12})


def test_provenance_is_not_wired_into_scoring_or_research() -> None:
    """本批不接入研究计算：评分、波段预警、研究与数据构建的模块都不导入数据留痕；留痕的规则模块不导入读写模块。"""
    source = PROJECT_ROOT / "src" / "market_risk"
    names = ("market_risk.provenance", "market_risk.provenance_config", "market_risk.storage.provenance_store")
    offenders = []
    for folder in ("scoring", "wavewarn", "research", "data"):
        for path in sorted((source / folder).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                           [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                imported = [f"{node.module}.{alias.name}" for alias in node.names] if isinstance(
                    node, ast.ImportFrom) and node.module else []
                if any(name in names for name in (*modules, *imported)):
                    offenders.append(path.name)
    assert offenders == []
    tree = ast.parse((source / "provenance.py").read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any(name.startswith("market_risk") for name in imports)


def _request(value: str = "50.00") -> rules.EntryRequest:
    return rules.EntryRequest("S5FI", DAY, value, "TradingView INDEX:S5FI", "手工录入", OBTAINED, False, None, "stone",
                              None, NO_SNAPSHOT, None, None)


def test_concurrent_allocation_never_hands_out_the_same_record_id(tmp_path: Path) -> None:
    """8 个线程同时各录入 5 条：分配编号、追加记录与更新计数都在排他锁内，

    40 条记录的编号互不相同，恰为 PR-000001 至 PR-000040，没有缺号；计数文件为 40；文件里正好 40 行。
    """
    from concurrent.futures import ThreadPoolExecutor

    paths = StoragePaths(tmp_path)

    def build(record_id: str, records: object) -> rules.ProvenanceRecord:
        return rules.build_record(record_id, _request(), None, None, ENTERED, CODE, CONFIG)

    def worker(_: int) -> list[str]:
        return [store.append_new_record(paths, build, 30.0).record_id for _ in range(5)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        issued = [record_id for batch in pool.map(worker, range(8)) for record_id in batch]
    assert sorted(issued) == [f"PR-{number:06d}" for number in range(1, 41)]
    assert [record.record_id for record in store.read_records(paths)] == sorted(issued)
    assert store.issued_number(paths) == 40 and not paths.provenance_lock_file.exists()
    # 锁被占着时，等待超时即报错，不会硬闯进去。
    with store.exclusive_lock(paths, 1.0), pytest.raises(rules.ProvenanceError, match="排他锁超时"):
        store.append_new_record(paths, build, 0.05)
    assert store.issued_number(paths) == 40


def test_record_ids_are_never_reused_and_grow_beyond_six_digits(ctx: services.Context) -> None:
    """录入三条后把记录文件的最后一行删掉（模拟误删）：下一条的编号是 PR-000004，不是被删掉的 PR-000003。

    被拒绝的录入不占用编号。计数到 999999 之后，下一条是 PR-1000000（位数自动增长），再下一条 PR-1000001。
    """
    for value in ("50.00", "51.00", "52.00"):
        _add(ctx, value)
    file = ctx.paths.provenance_records_csv
    lines = file.read_text(encoding="utf-8").splitlines(keepends=True)
    file.write_text("".join(lines[:-1]), encoding="utf-8", newline="")
    assert [record.record_id for record in store.read_records(ctx.paths)] == ["PR-000001", "PR-000002"]
    with pytest.raises(services.ServiceError, match="取值须在"):
        _add(ctx, "101")
    assert _add(ctx, "53.00").record_id == "PR-000004"
    assert rules.next_record_id(["PR-000001", "PR-000002"], 3) == "PR-000004"
    assert rules.next_record_id(["PR-000005"], 3) == "PR-000006"
    assert rules.next_record_id([], 999999) == "PR-1000000" and rules.record_number("PR-1000000") == 1000000
    ctx.paths.provenance_counter_file.write_text("999999\n", encoding="utf-8")
    big = _add(ctx, "54.00")
    assert big.record_id == "PR-1000000" and _add(ctx, "55.00").record_id == "PR-1000001"
    assert store.read_records(ctx.paths)[-2] == big
    rows = {row["record_id"] for row in _table(ctx, schema.provenance_records)}
    assert {"PR-1000000", "PR-1000001"} <= rows and "PR-000003" not in rows
    with pytest.raises(rules.ProvenanceError, match="不合法"):
        rules.record_number("PR-12345")


def test_times_are_saved_with_their_utc_offset_and_unknown_times_stay_empty(ctx: services.Context) -> None:
    """所有时间字段按带时区偏移的 ISO 8601 保存：首次取得时间换成美东（夏令时 −04:00、冬令时 −05:00），

    录入时间与确认时间为 +00:00，来源发布时间保留给出的偏移。未知的时间留空，不用录入时间代替。
    北京时间 2026-09-26 05:00（+08:00）即美东 2026-09-25 17:00（−04:00）。
    """
    beijing = dt.timezone(dt.timedelta(hours=8))
    record = _add(ctx, first_obtained_at=dt.datetime(2026, 9, 26, 5, 0, tzinfo=beijing),
                  source_published_at=dt.datetime(2026, 9, 26, 4, 30, tzinfo=beijing))
    services.provenance_confirm(ctx, record.record_id, "stone", dt.datetime(2026, 9, 26, 9, 0, tzinfo=beijing),
                                CONFIG)
    row = _table(ctx, schema.provenance_records)[0]
    assert row["first_obtained_at_et"] == "2026-09-25T17:00:00-04:00"
    assert row["entered_at_utc"] == "2026-09-25T22:00:00+00:00"
    assert row["source_published_at"] == "2026-09-26T04:30:00+08:00"
    assert _table(ctx, schema.provenance_confirmations)[0]["confirmed_at_utc"] == "2026-09-26T01:00:00+00:00"
    winter = _add(ctx, trade_date=dt.date(2026, 12, 18),
                  first_obtained_at=dt.datetime(2026, 12, 18, 22, 0, tzinfo=dt.UTC),
                  now=dt.datetime(2026, 12, 19, 0, 0, tzinfo=dt.UTC))
    assert winter.first_obtained_at_et is not None
    assert winter.first_obtained_at_et.isoformat() == "2026-12-18T17:00:00-05:00"
    unknown = _add(ctx, trade_date=dt.date(2015, 6, 1), first_obtained_at=None, historical_backfill=True)
    line = ctx.paths.provenance_records_csv.read_text(encoding="utf-8").splitlines()[-1].split(",")
    position = store.RECORD_FIELDS.index("first_obtained_at_et")
    assert unknown.first_obtained_at_et is None and line[position] == "" and line[position + 1] != ""
    assert line[store.RECORD_FIELDS.index("source_published_at")] == ""


def test_late_boundary_on_daylight_saving_switch_days() -> None:
    """夏令时切换日的迟到判断，按 America/New_York 的规则：

    2026-03-08 凌晨起为夏令时（UTC−4）：当日美东 18:30:00 = 22:30:00 UTC；前一天（冬令时，UTC−5）为 23:30:00 UTC。
    2026-11-01 凌晨起回到冬令时（UTC−5）：当日美东 18:30:00 = 23:30:00 UTC；前一天（夏令时）为 22:30:00 UTC。
    各自恰为截止时刻不算迟到，晚 1 秒算迟到；按“固定 UTC−5”去算会把 3 月 8 日 22:30:01 UTC 误判成未迟到。
    """
    cases = {dt.date(2026, 3, 7): (23, 30), dt.date(2026, 3, 8): (22, 30),
             dt.date(2026, 10, 31): (22, 30), dt.date(2026, 11, 1): (23, 30)}
    for day, (hour, minute) in cases.items():
        cutoff = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.UTC)
        assert cutoff.astimezone(NEW_YORK).time() == dt.time(18, 30)
        assert rules.is_late(cutoff, day, CONFIG) is False
        assert rules.is_late(cutoff + dt.timedelta(seconds=1), day, CONFIG) is True
        assert rules.is_late(cutoff - dt.timedelta(seconds=1), day, CONFIG) is False
    assert rules.is_late(dt.datetime(2026, 3, 8, 22, 30, 1, tzinfo=dt.UTC), dt.date(2026, 3, 8), CONFIG) is True


def test_dataset_version_only_for_indicators_in_the_dataset_and_source_file_hash(ctx: services.Context) -> None:
    """数据版本：S5FI 在数据集清单的序列里 → 记清单的 SHA-256；NDTW 不在数据集里 → 数据版本留空。

    NDTW 有已入库的来源文件时，保存该文件自身的 SHA-256（只记路径与哈希，不复制）。来源文件须在存储根目录之内。
    来源文件事后被改动，核对能查出来。
    """
    manifest = ctx.paths.market_manifest
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"series": {"S5FI": {}, "S5TW": {}, "SPX": {}}}', encoding="utf-8")
    raw = ctx.paths.tv_raw_root / "2026-09-26" / "INDEX_NDTW, 1D.csv"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"time,close\n2026-09-25,40.00\n")
    s5fi = _add(ctx)
    ndtw = _add(ctx, "40.00", indicator="NDTW", source="TradingView INDEX:NDTW 导出文件", source_file=raw)
    assert s5fi.data_version == store.file_sha256(manifest) and s5fi.source_file_sha256 is None
    assert ndtw.data_version is None
    assert ndtw.source_file_path == "data/manual/tradingview/raw/2026-09-26/INDEX_NDTW, 1D.csv"
    assert ndtw.source_file_sha256 == store.file_sha256(raw)
    rows = {row["record_id"]: row for row in _table(ctx, schema.provenance_records)}
    assert rows[ndtw.record_id]["data_version"] is None
    assert rows[ndtw.record_id]["source_file_sha256"] == store.file_sha256(raw)
    assert services.provenance_verify_snapshots(ctx) == []
    outside = ctx.paths.root.parent / f"{ctx.paths.root.name}_outside.csv"
    outside.write_bytes(b"x")
    with pytest.raises(services.ServiceError, match="存储根目录之内"):
        _add(ctx, "40.00", indicator="NDTW", source_file=outside)
    raw.write_bytes(b"time,close\n2026-09-25,41.00\n")
    assert services.provenance_verify_snapshots(ctx) == [
        f"{ndtw.record_id}：来源文件的 SHA-256 与记录不符（{ndtw.source_file_path}）"]


def test_missing_snapshot_needs_a_reason_and_the_query_shows_it(ctx: services.Context, tmp_path: Path) -> None:
    """原始快照为空时必须写明缺失原因；查询结果显示“无快照”及原因。有快照时不应再写原因，查询显示“有”。"""
    with pytest.raises(services.ServiceError, match="必须写明缺失原因"):
        _add(ctx, snapshot_missing_reason="")
    with pytest.raises(services.ServiceError, match="必须写明缺失原因"):
        _add(ctx, snapshot_missing_reason="   ")
    plain = _add(ctx)
    source = tmp_path / "shot.png"
    source.write_bytes(b"png")
    with pytest.raises(services.ServiceError, match="不应再写缺失原因"):
        _add(ctx, snapshot_file=source, snapshot_missing_reason="没有")
    shot = _add(ctx, snapshot_file=source)
    assert (plain.snapshot_missing_reason, shot.snapshot_missing_reason) == (NO_SNAPSHOT, None)
    views = services.provenance_list(ctx)
    assert [view.snapshot for view in views] == [f"无快照：{NO_SNAPSHOT}", "有"]
    rows = {row["record_id"]: row for row in _table(ctx, schema.provenance_records)}
    assert rows[plain.record_id]["snapshot_missing_reason"] == NO_SNAPSHOT
    assert rows[shot.record_id]["snapshot_missing_reason"] is None and len(store.read_records(ctx.paths)) == 2
