"""v2.0 行情读取入口（阶段三实施指令第二节、第六节第 2 部分）：构造文件读写测试。

构造的行情文件、登记值与裁定条目全部在 pytest 的临时目录或内存里；不读取真实研究数据或真实项目配置。
登记值由测试直接对构造的字节计算（hashlib 与简单计数），不用被测函数的输出。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.calendar import stock_trading_days
from market_risk.wavewarn_v20 import data_v20
from market_risk.wavewarn_v20.data_v20 import (
    DataEntryError,
    assemble_snapshot,
    file_metadata,
    read_until,
    verify_registered,
)
from market_risk.wavewarn_v20.dataset_v20 import AssetSeries, DecisionEntry, RegisteredFile
from market_risk.wavewarn_v20.snapshot import Snapshot

D = dt.date
DAYS = stock_trading_days(D(2001, 1, 2), D(2001, 4, 30))        # 真实的 NYSE 交易日轴，只用来给构造文件排日期
CUTOFF = DAYS[30]
ACQUIRED = dt.datetime(2001, 5, 1, 12, 0, tzinfo=dt.UTC)
Row = tuple[object, str, str]


def put(tmp_path: Path, name: str, data: bytes) -> Path:
    """把构造的字节写进 pytest 临时目录。"""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def rows(count: int, start: int = 0, price: str = "100.00", source: str = "yahoo") -> list[Row]:
    return [(day, price, source) for day in DAYS[start:start + count]]


# 负责人裁决 D16（2026-10-04）：八列表头，在本测试文件中独立写出，不引用 data_v20.HEADER。
HEADER_TEXT = "date,value,open,high,low,close,volume,source"


def line8(day: object, price: str, source: str) -> str:
    """八列数据行：下标 1 为价格、下标 7 为来源，下标 2—6 为固定占位（非数值）；价格为空时只置空第 2 字段。"""
    return f"{day},{price},IGN_OPEN,IGN_HIGH,IGN_LOW,IGN_CLOSE,IGN_VOLUME,{source}"


def content(items: list[Row], newline: str = "\n", header: str = HEADER_TEXT) -> bytes:
    lines = [header, *(line8(day, price, source) for day, price, source in items)]
    return (newline.join(lines) + newline).encode("utf-8")


def registered(asset: str, raw: bytes, **changes) -> RegisteredFile:
    """由构造的字节直接算出登记值（不经过被测函数）。"""
    lines = [line for line in raw.replace(b"\r\n", b"\n").split(b"\n") if line]
    fields = dict(asset=asset, raw_sha256=hashlib.sha256(raw).hexdigest(),
                  normalized_sha256=hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(), data_rows=len(lines) - 1,
                  first_date=D.fromisoformat(lines[1].split(b",")[0].decode()),
                  last_date=D.fromisoformat(lines[-1].split(b",")[0].decode()))
    fields.update(changes)
    return RegisteredFile(**fields)


def read(tmp_path: Path, items: list[Row], cutoff: dt.date = CUTOFF, decisions: tuple[DecisionEntry, ...] = (),
         asset: str = "SPX", name: str = "series.csv") -> AssetSeries:
    raw = content(items)
    return read_until(put(tmp_path, name, raw), asset, cutoff, registered(asset, raw), decisions)


class Spy:
    """记录被替换函数的调用，并照常调用原函数。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
        self.calls: dict[str, list] = {name: [] for name in names}
        for name in names:
            original = getattr(data_v20, name)

            def wrapper(*args, _name=name, _original=original, **kwargs):
                self.calls[_name].append(args)
                return _original(*args, **kwargs)

            monkeypatch.setattr(data_v20, name, wrapper)


PARSERS = ("parse_source", "parse_price", "check_trading_axis", "check_corrections", "assemble_snapshot")


def correct(day: dt.date, value: str, symbol: str = "SPX", decision: str = "correct") -> DecisionEntry:
    return DecisionEntry(symbol, day, decision, Decimal(value) if decision == "correct" else None, D(2001, 6, 1))


# ---------------------------------------------------------------------------
# 1—4：截止日之后不解析；元数据不解析行情；哈希口径；登记值不符
# ---------------------------------------------------------------------------


def test_rows_after_the_cutoff_are_never_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 截止日之后：非法价格、非法来源、连日期都不是的行、乱序日期与空价格。最后一行的日期须合法（元数据要读末日）。
    after = [(DAYS[31], "abc", "yahoo"), (DAYS[33], "101.00", "tiingo"), ("not-a-date", "x", "y"),
             (DAYS[32], "", "???")]
    spy = Spy(monkeypatch, "parse_source", "parse_price")
    series = read(tmp_path, [*rows(31), *after])
    assert list(series.closes) == DAYS[:31] and series.cutoff == CUTOFF
    assert len(spy.calls["parse_price"]) == len(spy.calls["parse_source"]) == 31       # 只作用于截止日以内的行
    assert {args[0] for args in spy.calls["parse_price"]} == {"100.00"}
    assert {args[0] for args in spy.calls["parse_source"]} == {"yahoo"}


def test_metadata_does_not_parse_prices_or_sources() -> None:
    raw = content([(day, "不是数字", "任意来源") for day in DAYS[:10]])
    metadata = file_metadata(raw)
    assert (metadata.data_rows, metadata.total_rows) == (10, 11)
    assert (metadata.first_date, metadata.last_date) == (DAYS[0], DAYS[9])
    assert metadata.raw_sha256 == metadata.normalized_sha256 == hashlib.sha256(raw).hexdigest()


def test_hash_conventions_for_crlf_and_lf() -> None:
    """同一内容分别写成 CRLF 与 LF：原始字节哈希不同；换行规范化哈希相同，且都等于 LF 文件的原始字节哈希。"""
    lf, crlf = content(rows(12)), content(rows(12), newline="\r\n")
    first, second = file_metadata(lf), file_metadata(crlf)
    assert first.raw_sha256 != second.raw_sha256
    assert first.normalized_sha256 == second.normalized_sha256 == first.raw_sha256 == hashlib.sha256(lf).hexdigest()
    assert (first.data_rows, first.first_date, first.last_date) == (second.data_rows, second.first_date,
                                                                    second.last_date) == (12, DAYS[0], DAYS[11])


@pytest.mark.parametrize(("changes", "named"), [
    ({"raw_sha256": "0" * 64}, ["原始字节 SHA-256"]),
    ({"normalized_sha256": "0" * 64}, ["换行规范化 SHA-256"]),
    ({"data_rows": 39}, ["数据行数"]),
    ({"first_date": DAYS[1]}, ["首日"]),
    ({"last_date": DAYS[38]}, ["末日"]),
    ({"raw_sha256": "0" * 64, "data_rows": 1, "last_date": DAYS[0]}, ["原始字节 SHA-256", "数据行数", "末日"]),
])
def test_registered_value_mismatch_stops_before_any_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            changes: dict, named: list[str]) -> None:
    """允许为文件级元数据核对读取全部原始字节，但不得进入来源解析、价格解析、快照构建或后续计算。"""
    raw = content(rows(40))
    wrong = registered("SPX", raw, **changes)
    with pytest.raises(DataEntryError) as caught:
        verify_registered(file_metadata(raw), wrong)
    assert all(name in str(caught.value) for name in named)                      # 列出全部不符项
    every = ("原始字节 SHA-256", "换行规范化 SHA-256", "数据行数", "首日", "末日")
    assert sum(name in str(caught.value) for name in every) == len(named)
    spy = Spy(monkeypatch, *PARSERS)
    with pytest.raises(DataEntryError, match="与登记值不符"):
        read_until(put(tmp_path, "series.csv", raw), "SPX", CUTOFF, wrong, ())
    assert all(calls == [] for calls in spy.calls.values())                      # 四类函数都没有被调用
    verify_registered(file_metadata(raw), registered("SPX", raw))                # 相符时不抛异常


# ---------------------------------------------------------------------------
# 5、6：整行缺失与其他输入检查
# ---------------------------------------------------------------------------


def without(items: list[Row], *positions: int) -> list[Row]:
    return [item for index, item in enumerate(items) if index not in positions]


def test_missing_whole_rows_are_reported(tmp_path: Path) -> None:
    full = rows(40)
    with pytest.raises(DataEntryError) as caught:
        read(tmp_path, without(full, 10))                                          # 缺一日
    assert f"缺少交易日 {DAYS[10]}" in str(caught.value)
    with pytest.raises(DataEntryError) as caught:
        read(tmp_path, without(full, 5, 6, 7))                                     # 缺连续多日
    assert all(str(DAYS[index]) in str(caught.value) for index in (5, 6, 7))
    with pytest.raises(DataEntryError) as caught:
        read(tmp_path, without(full, 30))                                          # 缺截止日当天
    assert str(CUTOFF) in str(caught.value)
    weekend = D(2001, 1, 6)                                                        # 周六，不是交易日
    extra = sorted([*full, (weekend, "100.00", "yahoo")], key=lambda item: item[0])
    with pytest.raises(DataEntryError, match=f"多出非交易日 {weekend}"):
        read(tmp_path, extra)
    assert len(read(tmp_path, without(full, 35, 36)).closes) == 31                 # 截止日之后的缺失不影响结果


def test_other_input_checks(tmp_path: Path) -> None:
    full = rows(40)
    duplicated = [*full[:6], full[5], *full[6:]]
    with pytest.raises(DataEntryError, match="重复或乱序"):
        read(tmp_path, duplicated)
    swapped = [*full[:3], full[4], full[3], *full[5:]]
    with pytest.raises(DataEntryError, match="重复或乱序"):
        read(tmp_path, swapped)
    empty_price = [*full[:8], (DAYS[8], "", "yahoo"), *full[9:]]
    with pytest.raises(DataEntryError, match="价格为空"):
        read(tmp_path, empty_price)
    bad_source = [*full[:8], (DAYS[8], "100.00", "tiingo"), *full[9:]]
    with pytest.raises(DataEntryError, match="来源标识不在允许清单内"):
        read(tmp_path, bad_source)
    with pytest.raises(DataEntryError, match="不是 NYSE 交易日"):
        read(tmp_path, full, cutoff=D(2001, 1, 6))                                 # 截止日为周六
    with pytest.raises(DataEntryError, match="不是 NYSE 交易日"):
        read(tmp_path, full, cutoff=D(2001, 1, 15))                                # 马丁·路德·金纪念日
    raw = content(full, header="date,close,source")
    with pytest.raises(DataEntryError, match="表头"):
        read_until(put(tmp_path, "bad_header.csv", raw), "SPX", CUTOFF, RegisteredFile(
            "SPX", hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw).hexdigest(), 40, DAYS[0], DAYS[39]), ())
    with pytest.raises(DataEntryError, match="登记值属于"):
        read_until(put(tmp_path, "other.csv", content(full)), "QQQ", CUTOFF, registered("SPX", content(full)), ())


def test_prices_are_read_with_two_decimals_half_up(tmp_path: Path) -> None:
    items = [(DAYS[0], "100.005", "yahoo"), (DAYS[1], "100.0049", "yahoo"), (DAYS[2], "43.305", "yahoo"),
             *rows(28, start=3)]
    series = read(tmp_path, items)
    assert [series.closes[day] for day in DAYS[:3]] == [Decimal("100.01"), Decimal("100.00"), Decimal("43.31")]
    assert series.raw_sha256 == hashlib.sha256(content(items)).hexdigest()         # 来源哈希取原始字节 SHA-256
    assert (series.asset, series.first_date, series.cutoff) == ("SPX", DAYS[0], CUTOFF)
    with pytest.raises(TypeError):
        series.closes[DAYS[0]] = Decimal("1")                                      # type: ignore[index]


# ---------------------------------------------------------------------------
# 7、13：修正条目的双向核对；筛选结果为空
# ---------------------------------------------------------------------------


def with_row(position: int, price: str, source: str, count: int = 40) -> list[Row]:
    items = rows(count)
    items[position] = (DAYS[position], price, source)
    return items


def test_approved_correction_is_read_normally(tmp_path: Path) -> None:
    series = read(tmp_path, with_row(12, "43.31", "correct:yahoo"), decisions=(correct(DAYS[12], "43.31"),))
    assert series.closes[DAYS[12]] == Decimal("43.31")
    # 文件值与修正值在四位小数上不同、两位小数上相同：通过。
    assert read(tmp_path, with_row(12, "43.3051", "correct:yahoo"),
                decisions=(correct(DAYS[12], "43.3149"),)).closes[DAYS[12]] == Decimal("43.31")


def test_correction_mismatches_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(DataEntryError, match="与修正值"):                           # 两位小数不同
        read(tmp_path, with_row(12, "43.31", "correct:yahoo"), decisions=(correct(DAYS[12], "43.32"),))
    with pytest.raises(DataEntryError, match="没有对应的已批准修正条目"):            # 没有条目的 correct:yahoo 行
        read(tmp_path, with_row(12, "43.31", "correct:yahoo"))
    with pytest.raises(DataEntryError, match="该行来源是 yahoo"):                # 已批准日期的行来源不是 correct:yahoo
        read(tmp_path, with_row(12, "43.31", "yahoo"), decisions=(correct(DAYS[12], "43.31"),))


def test_only_matching_correct_entries_apply(tmp_path: Path) -> None:
    others = (correct(DAYS[12], "43.31", symbol="SPY"),                             # 其他资产的 correct 条目
              correct(DAYS[13], "0", decision="exclude"), correct(DAYS[14], "0", decision="keep"),
              correct(DAYS[15], "0", decision="invalid"),                           # 当前资产的其他裁定类型
              correct(DAYS[35], "43.31"))                                           # 截止日之后的条目
    series = read(tmp_path, rows(40), decisions=others)                             # 全部是普通 yahoo 行：正常读入
    assert len(series.closes) == 31
    # 其他资产的条目不能给 SPX 的 correct:yahoo 行作依据。
    with pytest.raises(DataEntryError, match="没有对应的已批准修正条目"):
        read(tmp_path, with_row(12, "43.31", "correct:yahoo"), decisions=others)


def test_empty_filter_result_is_handled_normally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """筛选结果为空可以正常处理（空检查只针对裁定表的全部条目，由配置读取负责）。"""
    # SPX 没有 correct 条目：裁定表里只有其他资产的 correct 条目与 SPX 的其他类型条目。
    table = (correct(DAYS[5], "51.06", symbol="QQQ"), correct(DAYS[6], "0", decision="exclude"))
    assert len(read(tmp_path, rows(40), decisions=table).closes) == 31
    # 截止日早于 QQQ 的修正日期：截止日以内没有适用条目；截止日之后那一行即使来源为 correct:yahoo，也不解析、不核对。
    spy = Spy(monkeypatch, "parse_source", "parse_price")
    series = read(tmp_path, with_row(35, "43.31", "correct:yahoo"),
                  decisions=(correct(DAYS[35], "99.99", symbol="QQQ"),), asset="QQQ")
    assert len(series.closes) == 31 and DAYS[35] not in series.closes
    assert all(args[0] != "correct:yahoo" for args in spy.calls["parse_source"])


# ---------------------------------------------------------------------------
# 8、9、10：截止日不变性；旧登记值必须失败；绑定同一份字节
# ---------------------------------------------------------------------------


def test_cutoff_invariance_across_file_versions(tmp_path: Path) -> None:
    first = rows(35)
    second = [*first[:31], *((day, "777.77", "yahoo") for day in DAYS[31:35]), *rows(10, start=35, price="88.88")]
    one, two = content(first), content(second)                   # 文件二：改了截止日之后的已有行，并追加若干行
    a = read_until(put(tmp_path, "one.csv", one), "SPX", CUTOFF, registered("SPX", one), ())
    b = read_until(put(tmp_path, "two.csv", two), "SPX", CUTOFF, registered("SPX", two), ())
    assert dict(a.closes) == dict(b.closes) and list(a.closes) == DAYS[:31]        # 截止日以内逐日相同
    assert a.raw_sha256 != b.raw_sha256                                            # 来源哈希确实不同


def test_old_registered_values_must_fail_on_a_changed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one = content(rows(35))
    two = content([*rows(35), *rows(10, start=35, price="88.88")])
    spy = Spy(monkeypatch, *PARSERS)
    with pytest.raises(DataEntryError, match="与登记值不符"):
        read_until(put(tmp_path, "two.csv", two), "SPX", CUTOFF, registered("SPX", one), ())
    assert all(calls == [] for calls in spy.calls.values())


def test_hash_and_parsing_are_bound_to_the_same_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = content(rows(35))
    changed = content(rows(35, price="55.55"))
    path = put(tmp_path, "series.csv", original)
    real_read = data_v20.read_file_bytes
    calls: list[Path] = []

    def read_then_rewrite(target: Path) -> bytes:
        calls.append(target)
        data = real_read(target)
        put(tmp_path, "series.csv", changed)             # 读入之后立刻改写磁盘文件
        return data

    monkeypatch.setattr(data_v20, "read_file_bytes", read_then_rewrite)
    series = read_until(path, "SPX", CUTOFF, registered("SPX", original), ())
    assert calls == [path]                                                         # 一次读取恰为一次打开
    assert set(series.closes.values()) == {Decimal("100.00")}                      # 结果是改写之前的内容
    assert series.raw_sha256 == hashlib.sha256(original).hexdigest()
    assert real_read(path) == changed                                              # 磁盘上确实已被改写
    # 读入之前文件已被改写、仍用旧登记值：抛异常，后续解析都没有被调用。
    monkeypatch.setattr(data_v20, "read_file_bytes", real_read)
    spy = Spy(monkeypatch, *PARSERS)
    with pytest.raises(DataEntryError, match="与登记值不符"):
        read_until(path, "SPX", CUTOFF, registered("SPX", original), ())
    assert all(calls == [] for calls in spy.calls.values())


# ---------------------------------------------------------------------------
# 11：双资产组装
# ---------------------------------------------------------------------------


def test_assemble_two_assets(tmp_path: Path) -> None:
    spx = read(tmp_path, rows(40), name="spx.csv")
    qqq_rows = rows(20, start=20, price="50.00")                                   # QQQ 较晚上市：登记首日为 DAYS[20]
    qqq = read(tmp_path, qqq_rows, asset="QQQ", name="qqq.csv")
    assert qqq.first_date == DAYS[20]
    built = assemble_snapshot({"SPX": spx, "QQQ": qqq}, CUTOFF, ACQUIRED)
    assert type(built.snapshot) is Snapshot                                        # 阶段一的原类型
    assert list(built.snapshot.days) == DAYS[:31]                                  # 共同轴从较早的登记首日开始
    assert dict(built.first_days) == {"SPX": DAYS[0], "QQQ": DAYS[20]}
    assert built.pre_listing_days("QQQ") == tuple(DAYS[:20]) and built.pre_listing_days("SPX") == ()
    assert built.missing_days("QQQ") == () and built.missing_days("SPX") == ()     # 上市前的日期不算缺价
    assert built.snapshot.close("QQQ", DAYS[5]) is None and built.snapshot.close("QQQ", DAYS[20]) == Decimal("50.00")
    assert dict(built.snapshot.source_hashes) == {"SPX": spx.raw_sha256, "QQQ": qqq.raw_sha256}
    assert built.snapshot.acquired_at == ACQUIRED and built.snapshot.day == CUTOFF


def test_assemble_reports_real_gaps_but_not_pre_listing_days() -> None:
    closes = {day: Decimal("50.00") for day in DAYS[20:31] if day != DAYS[25]}     # 上市后缺一日（直接构造的读取结果）
    qqq = AssetSeries("QQQ", DAYS[20], CUTOFF, closes, "b" * 64)
    spx = AssetSeries("SPX", DAYS[0], CUTOFF, {day: Decimal("100.00") for day in DAYS[:31]}, "a" * 64)
    built = assemble_snapshot({"SPX": spx, "QQQ": qqq}, CUTOFF, ACQUIRED)
    assert built.missing_days("QQQ") == (DAYS[25],)
    assert not set(built.missing_days("QQQ")) & set(DAYS[:20])


def test_assemble_rejects_inconsistent_inputs() -> None:
    spx = AssetSeries("SPX", DAYS[0], CUTOFF, {day: Decimal("100.00") for day in DAYS[:31]}, "a" * 64)
    earlier = AssetSeries("QQQ", DAYS[20], DAYS[29], {day: Decimal("50.00") for day in DAYS[20:30]}, "b" * 64)
    with pytest.raises(DataEntryError, match="截止日"):
        assemble_snapshot({"SPX": spx, "QQQ": earlier}, CUTOFF, ACQUIRED)          # 两资产截止日不同
    with pytest.raises(DataEntryError, match="键 QQQ 下放的是 SPX"):
        assemble_snapshot({"SPX": spx, "QQQ": spx}, CUTOFF, ACQUIRED)
    with pytest.raises(DataEntryError):
        assemble_snapshot({}, CUTOFF, ACQUIRED)


# ---------------------------------------------------------------------------
# 12：静态断言
# ---------------------------------------------------------------------------


def test_price_parsing_only_happens_after_the_cutoff_check() -> None:
    source = inspect.getsource(data_v20._scan_rows)
    stop = source.index("if day > cutoff:\n            break")
    assert source.index("parse_source(") > stop and source.index("parse_price(") > stop
    assert source.index("_date_field(line)") < stop and source.index(".decode(") > stop     # 截止日判断之前只取日期字段
    whole = inspect.getsource(data_v20)
    assert whole.count("parse_price(") == 2 and whole.count("parse_source(") == 2           # 各一处定义、一处调用


def test_data_entry_module_does_not_import_provenance_config_or_models() -> None:
    whole = inspect.getsource(data_v20)
    imports = [line for line in whole.splitlines() if line.startswith(("import ", "from "))]
    assert not any("provenance_v20" in line or "config_v20" in line for line in imports)
    assert not any(line.startswith(("from market_risk.config", "import market_risk.config",
                                    "from market_risk.models", "import market_risk.models",
                                    "from market_risk import")) for line in imports)
    parameters = [parameter for name in ("read_until", "parse_rows", "assemble_snapshot", "read_file_bytes")
                  for parameter in inspect.signature(getattr(data_v20, name)).parameters]
    # 没有任何启用第二来源补齐的开关或参数。
    assert sorted(set(parameters)) == ["acquired_at", "asset", "cutoff", "day", "decisions", "path", "raw",
                                       "registered", "series"]
    # 本模块唯一的文件打开语句是二进制只读的那一处。
    opens = [line.strip() for line in whole.splitlines() if ".open" + "(" in line]
    assert opens == ['with path.open' + '("rb") as file:']


# ---------------------------------------------------------------------------
# 补充裁决：截止日检查前移、空行与物理行、表头、字段中的异常字符
# ---------------------------------------------------------------------------


def lines_file(lines: list[str], newline: str = "\n", end: bool = True) -> bytes:
    """把物理行原样拼成文件字节；end 为真时文件以换行符结尾。"""
    return (newline.join(lines) + (newline if end else "")).encode("utf-8")


def record(index: int, price: str = "100.00") -> str:
    return line8(DAYS[index], price, "yahoo")


def ruled_registered(asset: str, raw: bytes) -> RegisteredFile:
    """按补充裁决第 0 条的条文在测试内另写的登记值计算（不调用被测函数）：
    按 \\n 切行，末尾换行之后的空串不算一行，每行只去掉一个行尾 \\r；只含空格、制表符或为空的行不计。
    """
    parts = raw.split(b"\n")
    if raw.endswith(b"\n"):
        parts = parts[:-1]
    lines = [part[:-1] if part.endswith(b"\r") else part for part in parts]
    data = [line for line in lines[1:] if line.strip(b" \t") != b""]
    digest = hashlib.sha256(raw).hexdigest()
    return RegisteredFile(asset, digest, hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(), len(data),
                          D.fromisoformat(data[0].split(b",")[0].decode()),
                          D.fromisoformat(data[-1].split(b",")[0].decode()))


def read_lines(tmp_path: Path, lines: list[str], newline: str = "\n", end: bool = True) -> AssetSeries:
    raw = lines_file(lines, newline, end)
    return read_until(put(tmp_path, "series.csv", raw), "SPX", CUTOFF, ruled_registered("SPX", raw), ())


HEADER_LINE = HEADER_TEXT
BEFORE_CUTOFF = [HEADER_LINE, *(record(index) for index in range(31))]          # 截止日记录为最后一行


def test_cutoff_checks_happen_before_the_file_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = content(rows(40))
    path = put(tmp_path, "series.csv", raw)
    opened: list[Path] = []
    monkeypatch.setattr(data_v20, "read_file_bytes", lambda target: opened.append(target) or raw)
    with pytest.raises(DataEntryError, match="不是 NYSE 交易日"):
        read_until(path, "SPX", D(2001, 1, 6), registered("SPX", raw), ())               # 截止日为周六
    late_start = registered("SPX", raw, first_date=DAYS[20])
    with pytest.raises(DataEntryError, match="早于 SPX 的登记数据覆盖首日"):
        read_until(path, "SPX", DAYS[19], late_start, ())                                  # 截止日早于登记首日
    assert opened == []                                                                    # 两种情形都没有打开文件


@pytest.mark.parametrize("blank", ["", " ", "\t", " \t "])
def test_blank_line_between_records_within_the_cutoff_is_rejected(tmp_path: Path, blank: str) -> None:
    middle = [*BEFORE_CUTOFF[:12], blank, *BEFORE_CUTOFF[12:]]                             # 两条截止日以内的记录之间
    with pytest.raises(DataEntryError, match="数据记录之间出现空行"):
        read_lines(tmp_path, middle)
    before_cutoff = [*BEFORE_CUTOFF[:-1], blank, BEFORE_CUTOFF[-1]]                        # 空行之后紧接截止日记录
    with pytest.raises(DataEntryError, match="数据记录之间出现空行"):
        read_lines(tmp_path, before_cutoff)
    after_header = [HEADER_LINE, blank, *BEFORE_CUTOFF[1:]]                                # 表头与第一条记录之间
    with pytest.raises(DataEntryError, match="数据记录之间出现空行"):
        read_lines(tmp_path, after_header)


FUTURE = [record(index, "88.88") for index in range(31, 36)]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("after", [
    [],                                                       # 基准：截止日记录之后直接结束
    [""],                                                     # 一个尾部空行
    ["", " ", "\t", ""],                                      # 多个尾部空行
    FUTURE,                                                   # 追加未来的记录
    ["", *FUTURE],                                            # 截止日记录与未来记录之间插入空行
    [FUTURE[0], "", "  ", FUTURE[1], "", *FUTURE[2:]],        # 未来记录之间插入空行
    [FUTURE[0], "\t", "", "", FUTURE[1], *FUTURE[2:], "", ""],  # 改变未来空行的数量
])
def test_results_depend_only_on_content_up_to_the_cutoff_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                after: list[str], newline: str) -> None:
    """各变体与基准截止日以内的内容完全相同，各自绑定正确的登记值，结果逐日相同；
    处理完截止日记录后没有再处理任何行，也没有调用任何解析。"""
    base = read_lines(tmp_path, BEFORE_CUTOFF, newline)
    spy = Spy(monkeypatch, "_date_field", "parse_source", "parse_price")
    variant = read_lines(tmp_path, [*BEFORE_CUTOFF, *after], newline)
    assert dict(variant.closes) == dict(base.closes) and list(variant.closes) == DAYS[:31]
    # 日期字段：元数据取首末两次，逐行处理恰为截止日以内的 31 条记录；截止日之后的行一行都没有处理。
    assert len(spy.calls["_date_field"]) == 2 + 31
    assert len(spy.calls["parse_source"]) == len(spy.calls["parse_price"]) == 31


def test_trailing_newline_is_not_a_blank_record(tmp_path: Path) -> None:
    with_end, without_end = lines_file(BEFORE_CUTOFF), lines_file(BEFORE_CUTOFF, end=False)
    assert file_metadata(with_end).data_rows == file_metadata(without_end).data_rows == 31
    assert file_metadata(with_end).total_rows == 32
    assert dict(read_lines(tmp_path, BEFORE_CUTOFF, end=False).closes) == dict(read_lines(tmp_path,
                                                                                          BEFORE_CUTOFF).closes)
    crlf = lines_file(BEFORE_CUTOFF, "\r\n")
    assert file_metadata(crlf).data_rows == 31                                             # CRLF 的末尾换行同样不算


@pytest.mark.parametrize("odd", ["\x0b", "\x0c", "\r\r"])
def test_vertical_tab_form_feed_and_double_cr_lines_are_not_blank(tmp_path: Path, odd: str) -> None:
    """\\x0b、\\x0c 行，以及 \\r\\r 行（只去掉一个行尾 \\r 后残留一个 \\r）都算非空：计入数据行数，
    出现在截止日以内时在日期解析处报错。行数一致不证明格式正确。"""
    lines = [*BEFORE_CUTOFF[:12], odd, *BEFORE_CUTOFF[12:]]
    raw = lines_file(lines)
    assert file_metadata(raw).data_rows == 32 and ruled_registered("SPX", raw).data_rows == 32
    with pytest.raises(DataEntryError, match="日期字段不是 ISO 日期"):
        read_lines(tmp_path, lines)


@pytest.mark.parametrize(("line", "message"), [
    (line8(DAYS[12], "100.00\x0b", "yahoo"), "价格含空白或不可打印字符"),          # Decimal 本会去掉首尾空白
    (line8(DAYS[12], "100.00 ", "yahoo"), "价格含空白或不可打印字符"),
    (line8(f"{DAYS[12]}\x0c", "100.00", "yahoo"), "日期字段不是 ISO 日期"),
    (line8(DAYS[12], "100.00", "yahoo\x0b"), "来源标识不在允许清单内"),
])
def test_fields_with_odd_characters_are_rejected(tmp_path: Path, line: str, message: str) -> None:
    lines = [*BEFORE_CUTOFF[:13], line, *BEFORE_CUTOFF[14:]]
    with pytest.raises(DataEntryError, match=message):
        read_lines(tmp_path, lines)


BAD_DATES = ["20010119",            # date.fromisoformat 本会接受的紧凑写法
             "2001-W03-5",          # 同上：ISO 周日期
             "2001-1-19",           # 位数不对
             "２００１-01-19",      # 全角数字（非 ASCII）
             "2001-01-19T00",       # 带时间
             "2001-02-30"]          # 格式正确但日期不存在


@pytest.mark.parametrize("bad", BAD_DATES)
def test_dates_must_be_existing_ascii_yyyy_mm_dd(tmp_path: Path, bad: str) -> None:
    """日期词法规则（补充裁决第一部分第 3 条）：逐行解析与文件级元数据的首末日期使用同一套规则。"""
    middle = [*BEFORE_CUTOFF[:13], line8(bad, "100.00", "yahoo"), *BEFORE_CUTOFF[14:]]
    with pytest.raises(DataEntryError, match="日期字段不是 ISO 日期"):
        read_lines(tmp_path, middle)
    first = [HEADER_LINE, line8(bad, "100.00", "yahoo"), *BEFORE_CUTOFF[2:]]       # 元数据取首日
    with pytest.raises(DataEntryError, match="日期字段不是 ISO 日期"):
        file_metadata(lines_file(first))
    last = [*BEFORE_CUTOFF, line8(bad, "100.00", "yahoo")]                          # 元数据取末日
    with pytest.raises(DataEntryError, match="日期字段不是 ISO 日期"):
        file_metadata(lines_file(last))


def test_valid_dates_still_parse(tmp_path: Path) -> None:
    assert data_v20._date_field(b"2000-02-29,1,yahoo") == D(2000, 2, 29)            # 闰日存在
    with pytest.raises(DataEntryError, match="日期不存在"):
        data_v20._date_field(b"2001-02-29,1,yahoo")
    assert list(read_lines(tmp_path, BEFORE_CUTOFF).closes) == DAYS[:31]


@pytest.mark.parametrize(("price", "message"), [
    ("1e2", "词法规则"), ("1E2", "词法规则"),                     # 科学计数法：本次新增的词法规则
    ("+1.50", "词法规则"), ("-1.50", "词法规则"),                 # 正负号：本次新增的词法规则
    (".50", "词法规则"), ("1.", "词法规则"), ("1,000", "词法规则"),
    ("NaN", "词法规则"), ("Infinity", "词法规则"),
    ("１.50", "词法规则"),                                        # 全角数字
    (" 1.50", "价格含空白或不可打印字符"),                         # 空白：落实已有的严格契约
    ("1.50\x0c", "价格含空白或不可打印字符"),
    ("0", "规范化后不满足正价格要求"), ("0.00", "规范化后不满足正价格要求"),        # 严格大于零
    ("0.004", "规范化后不满足正价格要求"),                          # 词法合法，按两位小数规范化后为 0.00
])
def test_prices_follow_the_lexical_rule(price: str, message: str) -> None:
    with pytest.raises(DataEntryError, match=message):
        data_v20.parse_price(price)


@pytest.mark.parametrize(("price", "expected"), [("1", "1.00"), ("43.3149", "43.31"), ("100.005", "100.01"),
                                                  ("007.5", "7.50")])
def test_prices_without_a_fixed_number_of_decimals_are_accepted(price: str, expected: str) -> None:
    """不增加“必须两位小数”等未登记的限制：整数与多位小数都接受，按两位小数 ROUND_HALF_UP 读入。"""
    assert data_v20.parse_price(price) == Decimal(expected)


@pytest.mark.parametrize("prefix", [[""], [" "], ["# 注释"], [record(0)]])
def test_header_must_be_the_first_physical_line(tmp_path: Path, prefix: list[str]) -> None:
    raw = lines_file([*prefix, *BEFORE_CUTOFF])
    with pytest.raises(DataEntryError, match="第一行必须恰为表头"):
        file_metadata(raw)
    with pytest.raises(DataEntryError, match="第一行必须恰为表头"):
        data_v20.parse_rows(raw, CUTOFF)
    fake = RegisteredFile("SPX", "0" * 64, "0" * 64, 31, DAYS[0], DAYS[30])
    with pytest.raises(DataEntryError, match="第一行必须恰为表头"):
        read_until(put(tmp_path, "series.csv", raw), "SPX", CUTOFF, fake, ())


def test_header_with_a_byte_order_mark_is_rejected() -> None:
    raw = b"\xef\xbb\xbf" + lines_file(BEFORE_CUTOFF)
    with pytest.raises(DataEntryError, match="第一行必须恰为表头"):
        file_metadata(raw)
    with pytest.raises(DataEntryError, match="第一行必须恰为表头"):
        data_v20.parse_rows(raw, CUTOFF)
    assert file_metadata(lines_file(BEFORE_CUTOFF, "\r\n")).data_rows == 31                 # 表头行尾的一个 \r 可去掉


def test_missing_rows_and_empty_prices_share_the_missing_price_reason(tmp_path: Path) -> None:
    """补修 Q5：整行缺失由 data_v20 发现，空价格由 labels_r2 发现，两者传出相同的原因类别“缺少必需价格”；
    其余输入问题为“输入校验失败”。按子类与 reason 属性判别，不解析报错文字。"""
    from market_risk.wavewarn_v20 import labels_r2

    full = rows(40)
    with pytest.raises(DataEntryError) as missing_row:
        read(tmp_path, without(full, 10))                                          # 整行缺失
    with pytest.raises(DataEntryError) as empty_price:
        read(tmp_path, [*full[:8], (DAYS[8], "", "yahoo"), *full[9:]])             # 行情文件中的空价格
    with pytest.raises(labels_r2.LabelError) as label_empty:
        labels_r2.r2_events("SPX", [(DAYS[0], Decimal("100")), (DAYS[1], None)], DAYS[1],
                            labels_r2.R2Thresholds(Decimal("0.95"), Decimal("1.05"), Decimal("0.97")))
    assert type(missing_row.value) is data_v20.MissingPriceEntryError
    assert type(empty_price.value) is data_v20.MissingPriceEntryError
    assert type(label_empty.value) is labels_r2.MissingPriceError
    assert missing_row.value.reason == empty_price.value.reason == label_empty.value.reason == "缺少必需价格"
    weekend = D(2001, 1, 6)
    with pytest.raises(DataEntryError) as extra_day:                                # 多出非交易日：输入校验失败
        read(tmp_path, sorted([*full, (weekend, "100.00", "yahoo")], key=lambda item: item[0]))
    with pytest.raises(DataEntryError) as bad_source:
        read(tmp_path, [*full[:8], (DAYS[8], "100.00", "tiingo"), *full[9:]])
    for caught in (extra_day, bad_source):
        assert type(caught.value) is data_v20.DataInputError and caught.value.reason == "输入校验失败"


# ---------------------------------------------------------------------------
# 负责人裁决 D16（2026-10-04）：八列口径
# ---------------------------------------------------------------------------


def replaced(index: int, line: str) -> list[str]:
    """BEFORE_CUTOFF 中第 index 条记录（截止日以内）换成给定的物理行。"""
    return [*BEFORE_CUTOFF[:index + 1], line, *BEFORE_CUTOFF[index + 2:]]


def test_d16_three_column_header_is_rejected(tmp_path: Path) -> None:
    lines = ["date,value,source", *BEFORE_CUTOFF[1:]]
    raw = lines_file(lines)
    with pytest.raises(data_v20.DataInputError, match="第一行必须恰为表头"):
        file_metadata(raw)
    with pytest.raises(data_v20.DataInputError, match="第一行必须恰为表头"):
        read_lines(tmp_path, lines)


def test_d16_seven_fields_within_the_cutoff_are_rejected(tmp_path: Path) -> None:
    line = f"{DAYS[12]},100.00,IGN_OPEN,IGN_HIGH,IGN_LOW,IGN_CLOSE,yahoo"
    with pytest.raises(data_v20.DataInputError, match="不是八个字段"):
        read_lines(tmp_path, replaced(12, line))


def test_d16_nine_fields_within_the_cutoff_are_rejected(tmp_path: Path) -> None:
    line = f"{DAYS[12]},100.00,IGN_OPEN,IGN_HIGH,IGN_LOW,IGN_CLOSE,IGN_VOLUME,EXTRA,yahoo"
    with pytest.raises(data_v20.DataInputError, match="不是八个字段"):
        read_lines(tmp_path, replaced(12, line))


def test_d16_trailing_comma_within_the_cutoff_is_rejected(tmp_path: Path) -> None:
    line = line8(DAYS[12], "100.00", "yahoo") + ","
    with pytest.raises(data_v20.DataInputError, match="不是八个字段"):
        read_lines(tmp_path, replaced(12, line))


def test_d16_empty_fields_two_to_six_are_accepted(tmp_path: Path) -> None:
    series = read_lines(tmp_path, replaced(12, f"{DAYS[12]},123.45,,,,,,yahoo"))
    assert series.closes[DAYS[12]] == Decimal("123.45")
    assert list(series.closes) == DAYS[:31]


def test_d16_close_field_is_not_parsed_or_compared(tmp_path: Path) -> None:
    line = f"{DAYS[12]},100.00,IGN_OPEN,IGN_HIGH,IGN_LOW,999.99,IGN_VOLUME,yahoo"
    assert read_lines(tmp_path, replaced(12, line)).closes[DAYS[12]] == Decimal("100.00")


def test_d16_empty_value_is_not_filled_from_close(tmp_path: Path) -> None:
    line = f"{DAYS[12]},,IGN_OPEN,IGN_HIGH,IGN_LOW,100.00,IGN_VOLUME,yahoo"
    with pytest.raises(data_v20.MissingPriceEntryError):
        read_lines(tmp_path, replaced(12, line))


def test_d16_field_count_is_not_checked_after_the_cutoff(tmp_path: Path) -> None:
    series = read_lines(tmp_path, [*BEFORE_CUTOFF, f"{DAYS[31]},88.88,yahoo"])
    assert series.cutoff == CUTOFF and list(series.closes) == DAYS[:31]


def test_d16_metadata_does_not_check_field_counts() -> None:
    metadata = file_metadata(lines_file(replaced(12, f"{DAYS[12]},100.00,yahoo")))
    assert metadata.data_rows == 31 and metadata.first_date == DAYS[0] and metadata.last_date == CUTOFF


def test_d16_header_with_a_byte_order_mark_is_rejected(tmp_path: Path) -> None:
    raw = b"\xef\xbb\xbf" + lines_file(BEFORE_CUTOFF)
    with pytest.raises(data_v20.DataInputError, match="第一行必须恰为表头"):
        file_metadata(raw)
    with pytest.raises(data_v20.DataInputError, match="第一行必须恰为表头"):
        read_until(put(tmp_path, "series.csv", raw), "SPX", CUTOFF, ruled_registered("SPX", raw), ())


def test_d16_source_comes_from_field_seven_before_the_price(tmp_path: Path) -> None:
    with pytest.raises(data_v20.DataInputError, match="来源标识不在允许清单内"):
        read_lines(tmp_path, replaced(12, line8(DAYS[12], "100.00", "tiingo")))
    with pytest.raises(data_v20.DataInputError, match="来源标识不在允许清单内"):
        read_lines(tmp_path, replaced(12, line8(DAYS[12], "", "tiingo")))       # 先来源后价格


# ---------------------------------------------------------------------------
# 阶段四 M2 第一部分（修订二；补充二至补充五）：读取拆分的行为不变证明、结构化缺失证据、前缀诊断读取
# 设计稿第三节第 1—3 小节；M2 指令第二节第 2 小节。以下只追加，不改上面的任何既有测试。
# ---------------------------------------------------------------------------

from market_risk.calendar import is_stock_trading_day  # noqa: E402

# 开工版本参照实现：逐字复制开工提交 7123272 中 data_v20 的 parse_rows、check_trading_axis、read_until 函数体，
# 只把模块内名称写成 data_v20.<名称>（这些被引用的函数在本批未改）。用于判定“与开工版本相同”。


def start_parse_rows(raw: bytes, cutoff: dt.date) -> tuple[tuple[dt.date, Decimal, str], ...]:
    lines = data_v20.physical_lines(raw)
    data_v20._check_header(lines)
    rows: list[tuple[dt.date, Decimal, str]] = []
    pending_blank = False
    for line in lines[1:]:
        if data_v20.is_blank(line):
            pending_blank = True
            continue
        day = data_v20._date_field(line)
        if day > cutoff:
            break
        if pending_blank:
            raise data_v20.DataInputError(f"数据记录之间出现空行：{day} 这一行之前")
        if rows and day <= rows[-1][0]:
            raise data_v20.DataInputError(f"日期重复或乱序：{rows[-1][0]} 之后是 {day}")
        try:
            fields = line.decode("utf-8").split(",")
        except UnicodeDecodeError as error:
            raise data_v20.DataInputError(f"{day} 这一行不是 UTF-8 文本") from error
        if len(fields) != 8:
            raise data_v20.DataInputError(f"{day} 这一行不是八个字段")
        source = data_v20.parse_source(fields[7])
        rows.append((day, data_v20.parse_price(fields[1]), source))
        if day == cutoff:
            break
    return tuple(rows)


def start_check_trading_axis(asset: str, first_day: dt.date, cutoff: dt.date, days) -> None:
    expected = stock_trading_days(first_day, cutoff)
    missing = sorted(set(expected) - set(days))
    extra = sorted(set(days) - set(expected))
    if missing or extra:
        parts = []
        if missing:
            parts.append("缺少交易日 " + "、".join(str(day) for day in missing))
        if extra:
            parts.append("多出非交易日 " + "、".join(str(day) for day in extra))
        message = f"{asset} 的日期与交易日轴不符：" + "；".join(parts)
        if extra:
            raise data_v20.DataInputError(message)
        raise data_v20.MissingPriceEntryError(message)


def start_read_until(path: Path, asset: str, cutoff: dt.date, registered: RegisteredFile, decisions) -> AssetSeries:
    if registered.asset != asset:
        raise data_v20.DataInputError(f"登记值属于 {registered.asset}，不是 {asset}")
    if not is_stock_trading_day(cutoff):
        raise data_v20.DataInputError(f"截止日 {cutoff} 不是 NYSE 交易日")
    if cutoff < registered.first_date:
        raise data_v20.DataInputError(f"截止日 {cutoff} 早于 {asset} 的登记数据覆盖首日 {registered.first_date}")
    raw = data_v20.read_file_bytes(path)
    data_v20.verify_registered(data_v20.file_metadata(raw), registered)
    rows = start_parse_rows(raw, cutoff)
    start_check_trading_axis(asset, registered.first_date, cutoff, [day for day, _, _ in rows])
    data_v20.check_corrections(asset, cutoff, rows, decisions)
    return AssetSeries(asset, registered.first_date, cutoff, {day: price for day, price, _ in rows},
                       hashlib.sha256(raw).hexdigest())


def outcome(function, *args) -> tuple:
    """返回值逐字段，或异常的（类型、reason、消息）。"""
    try:
        value = function(*args)
    except DataEntryError as error:
        return ("异常", type(error), error.reason, str(error))
    if isinstance(value, AssetSeries):
        return ("返回", value.asset, value.first_date, value.cutoff, dict(value.closes), value.raw_sha256)
    return ("返回", value)


def chain(path: Path, asset: str, cutoff: dt.date, registered: RegisteredFile, decisions) -> AssetSeries:
    """完整链（设计稿第三节第 1 小节证明第 2 条）：precheck_read → read_file_bytes → parse_until。"""
    data_v20.precheck_read(asset, cutoff, registered)
    raw = data_v20.read_file_bytes(path)
    return data_v20.parse_until(raw, asset, cutoff, registered, decisions)


# 等价用例（补充二第五节）：输入由本文件既有的构造函数复现；id 为对应的既有测试函数名（多个输入加序号后缀）。
# 每个工厂返回（原始字节，资产，截止日，登记值，裁定条目）。
FAKE = RegisteredFile("SPX", "0" * 64, "0" * 64, 31, DAYS[0], DAYS[30])
Case = tuple[bytes, str, dt.date, RegisteredFile, tuple[DecisionEntry, ...]]


def by_items(items: list[Row], cutoff: dt.date = CUTOFF, decisions: tuple[DecisionEntry, ...] = (),
             asset: str = "SPX", **changes) -> Case:
    """与既有的 read() 相同的构造方式。"""
    raw = content(items)
    return raw, asset, cutoff, registered(asset, raw, **changes), decisions


def by_lines(lines: list[str], newline: str = "\n", end: bool = True) -> Case:
    """与既有的 read_lines() 相同的构造方式。"""
    raw = lines_file(lines, newline, end)
    return raw, "SPX", CUTOFF, ruled_registered("SPX", raw), ()


def by_raw(raw: bytes, reg: RegisteredFile = FAKE) -> Case:
    return raw, "SPX", CUTOFF, reg, ()


def _registered_changes() -> list:
    return [{"raw_sha256": "0" * 64}, {"normalized_sha256": "0" * 64}, {"data_rows": 39}, {"first_date": DAYS[1]},
            {"last_date": DAYS[38]}, {"raw_sha256": "0" * 64, "data_rows": 1, "last_date": DAYS[0]}]


def _bad_header() -> Case:
    raw = content(rows(40), header="date,close,source")
    return raw, "SPX", CUTOFF, RegisteredFile("SPX", hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw).hexdigest(),
                                              40, DAYS[0], DAYS[39]), ()


def _other_asset() -> Case:
    return content(rows(40)), "QQQ", CUTOFF, registered("SPX", content(rows(40))), ()


def _late_start() -> Case:
    raw = content(rows(40))
    return raw, "SPX", DAYS[19], registered("SPX", raw, first_date=DAYS[20]), ()


# test_results_depend_only_on_content_up_to_the_cutoff_record 的两种换行 × 七种截止日之后的内容（既有参数化原样）。
AFTER_VARIANTS = [(newline, after) for newline in ("\n", "\r\n") for after in (
    [], [""], ["", " ", "\t", ""], FUTURE, ["", *FUTURE], [FUTURE[0], "", "  ", FUTURE[1], "", *FUTURE[2:]],
    [FUTURE[0], "\t", "", "", FUTURE[1], *FUTURE[2:], "", ""])]
PARSE_PRICE_ONLY = "只直接调用 parse_price，不构造行情文件、不调用 read_until"
ASSET_SERIES_ONLY = "直接构造 AssetSeries，不构造行情文件、不调用 read_until"

EQUIVALENCE: list[tuple[str, object]] = [
    ("test_rows_after_the_cutoff_are_never_parsed", lambda: by_items(
        [*rows(31), (DAYS[31], "abc", "yahoo"), (DAYS[33], "101.00", "tiingo"), ("not-a-date", "x", "y"),
         (DAYS[32], "", "???")])),
    ("test_metadata_does_not_parse_prices_or_sources",
     lambda: by_items([(day, "不是数字", "任意来源") for day in DAYS[:10]])),
    ("test_hash_conventions_for_crlf_and_lf-1", lambda: by_items(rows(12))),
    ("test_hash_conventions_for_crlf_and_lf-2", lambda: (lambda raw: (raw, "SPX", CUTOFF, registered("SPX", raw), ()))(
        content(rows(12), newline="\r\n"))),
    *((f"test_registered_value_mismatch_stops_before_any_parsing-{index + 1}",
       lambda changes=changes: by_items(rows(40), **changes)) for index, changes in enumerate(_registered_changes())),
    ("test_registered_value_mismatch_stops_before_any_parsing-7", lambda: by_items(rows(40))),
    ("test_missing_whole_rows_are_reported-1", lambda: by_items(without(rows(40), 10))),
    ("test_missing_whole_rows_are_reported-2", lambda: by_items(without(rows(40), 5, 6, 7))),
    ("test_missing_whole_rows_are_reported-3", lambda: by_items(without(rows(40), 30))),
    ("test_missing_whole_rows_are_reported-4", lambda: by_items(
        sorted([*rows(40), (D(2001, 1, 6), "100.00", "yahoo")], key=lambda item: item[0]))),
    ("test_missing_whole_rows_are_reported-5", lambda: by_items(without(rows(40), 35, 36))),
    ("test_other_input_checks-1", lambda: by_items([*rows(40)[:6], rows(40)[5], *rows(40)[6:]])),
    ("test_other_input_checks-2", lambda: by_items([*rows(40)[:3], rows(40)[4], rows(40)[3], *rows(40)[5:]])),
    ("test_other_input_checks-3", lambda: by_items([*rows(40)[:8], (DAYS[8], "", "yahoo"), *rows(40)[9:]])),
    ("test_other_input_checks-4", lambda: by_items([*rows(40)[:8], (DAYS[8], "100.00", "tiingo"), *rows(40)[9:]])),
    ("test_other_input_checks-5", lambda: by_items(rows(40), cutoff=D(2001, 1, 6))),
    ("test_other_input_checks-6", lambda: by_items(rows(40), cutoff=D(2001, 1, 15))),
    ("test_other_input_checks-7", _bad_header),
    ("test_other_input_checks-8", _other_asset),
    ("test_prices_are_read_with_two_decimals_half_up", lambda: by_items(
        [(DAYS[0], "100.005", "yahoo"), (DAYS[1], "100.0049", "yahoo"), (DAYS[2], "43.305", "yahoo"),
         *rows(28, start=3)])),
    ("test_approved_correction_is_read_normally-1", lambda: by_items(
        with_row(12, "43.31", "correct:yahoo"), decisions=(correct(DAYS[12], "43.31"),))),
    ("test_approved_correction_is_read_normally-2", lambda: by_items(
        with_row(12, "43.3051", "correct:yahoo"), decisions=(correct(DAYS[12], "43.3149"),))),
    ("test_correction_mismatches_are_rejected-1", lambda: by_items(
        with_row(12, "43.31", "correct:yahoo"), decisions=(correct(DAYS[12], "43.32"),))),
    ("test_correction_mismatches_are_rejected-2", lambda: by_items(with_row(12, "43.31", "correct:yahoo"))),
    ("test_correction_mismatches_are_rejected-3", lambda: by_items(
        with_row(12, "43.31", "yahoo"), decisions=(correct(DAYS[12], "43.31"),))),
    ("test_only_matching_correct_entries_apply-1", lambda: by_items(rows(40), decisions=(
        correct(DAYS[12], "43.31", symbol="SPY"), correct(DAYS[13], "0", decision="exclude"),
        correct(DAYS[14], "0", decision="keep"), correct(DAYS[15], "0", decision="invalid"),
        correct(DAYS[35], "43.31")))),
    ("test_only_matching_correct_entries_apply-2", lambda: by_items(with_row(12, "43.31", "correct:yahoo"), decisions=(
        correct(DAYS[12], "43.31", symbol="SPY"), correct(DAYS[13], "0", decision="exclude"),
        correct(DAYS[14], "0", decision="keep"), correct(DAYS[15], "0", decision="invalid"),
        correct(DAYS[35], "43.31")))),
    ("test_empty_filter_result_is_handled_normally-1", lambda: by_items(rows(40), decisions=(
        correct(DAYS[5], "51.06", symbol="QQQ"), correct(DAYS[6], "0", decision="exclude")))),
    ("test_empty_filter_result_is_handled_normally-2", lambda: by_items(
        with_row(35, "43.31", "correct:yahoo"), decisions=(correct(DAYS[35], "99.99", symbol="QQQ"),), asset="QQQ")),
    ("test_cutoff_invariance_across_file_versions-1", lambda: by_items(rows(35))),
    ("test_cutoff_invariance_across_file_versions-2", lambda: by_items(
        [*rows(35)[:31], *((day, "777.77", "yahoo") for day in DAYS[31:35]), *rows(10, start=35, price="88.88")])),
    ("test_old_registered_values_must_fail_on_a_changed_file", lambda: (
        content([*rows(35), *rows(10, start=35, price="88.88")]), "SPX", CUTOFF, registered("SPX", content(rows(35))),
        ())),
    ("test_hash_and_parsing_are_bound_to_the_same_bytes-1", lambda: by_items(rows(35))),
    ("test_hash_and_parsing_are_bound_to_the_same_bytes-2", lambda: (
        content(rows(35, price="55.55")), "SPX", CUTOFF, registered("SPX", content(rows(35))), ())),
    ("test_assemble_two_assets-1", lambda: by_items(rows(40))),
    ("test_assemble_two_assets-2", lambda: by_items(rows(20, start=20, price="50.00"), asset="QQQ")),
    ("test_cutoff_checks_happen_before_the_file_is_opened-1", lambda: by_items(rows(40), cutoff=D(2001, 1, 6))),
    ("test_cutoff_checks_happen_before_the_file_is_opened-2", _late_start),
    *((f"test_blank_line_between_records_within_the_cutoff_is_rejected-{index * 3 + place + 1}",
       lambda blank=blank, place=place: by_lines(
           [[*BEFORE_CUTOFF[:12], blank, *BEFORE_CUTOFF[12:]], [*BEFORE_CUTOFF[:-1], blank, BEFORE_CUTOFF[-1]],
            [HEADER_LINE, blank, *BEFORE_CUTOFF[1:]]][place]))
      for index, blank in enumerate(["", " ", "\t", " \t "]) for place in range(3)),
    *((f"test_results_depend_only_on_content_up_to_the_cutoff_record-{index + 1}",
       lambda newline=newline, after=after: by_lines([*BEFORE_CUTOFF, *after], newline))
      for index, (newline, after) in enumerate(AFTER_VARIANTS)),
    ("test_trailing_newline_is_not_a_blank_record-1", lambda: by_lines(BEFORE_CUTOFF)),
    ("test_trailing_newline_is_not_a_blank_record-2", lambda: by_lines(BEFORE_CUTOFF, end=False)),
    ("test_trailing_newline_is_not_a_blank_record-3", lambda: by_lines(BEFORE_CUTOFF, "\r\n")),
    *((f"test_vertical_tab_form_feed_and_double_cr_lines_are_not_blank-{index + 1}",
       lambda odd=odd: by_lines([*BEFORE_CUTOFF[:12], odd, *BEFORE_CUTOFF[12:]]))
      for index, odd in enumerate(["\x0b", "\x0c", "\r\r"])),
    *((f"test_fields_with_odd_characters_are_rejected-{index + 1}",
       lambda line=line: by_lines([*BEFORE_CUTOFF[:13], line, *BEFORE_CUTOFF[14:]]))
      for index, line in enumerate([line8(DAYS[12], "100.00\x0b", "yahoo"), line8(DAYS[12], "100.00 ", "yahoo"),
                                    line8(f"{DAYS[12]}\x0c", "100.00", "yahoo"),
                                    line8(DAYS[12], "100.00", "yahoo\x0b")])),
    *((f"test_dates_must_be_existing_ascii_yyyy_mm_dd-{index * 3 + place + 1}",
       lambda bad=bad, place=place: (by_lines([*BEFORE_CUTOFF[:13], line8(bad, "100.00", "yahoo"), *BEFORE_CUTOFF[14:]])
                                     if place == 0 else by_raw(lines_file(
                                         [HEADER_LINE, line8(bad, "100.00", "yahoo"), *BEFORE_CUTOFF[2:]] if place == 1
                                         else [*BEFORE_CUTOFF, line8(bad, "100.00", "yahoo")]))))
      for index, bad in enumerate(BAD_DATES) for place in range(3)),
    ("test_valid_dates_still_parse", lambda: by_lines(BEFORE_CUTOFF)),
    *((f"test_header_must_be_the_first_physical_line-{index + 1}",
       lambda prefix=prefix: by_raw(lines_file([*prefix, *BEFORE_CUTOFF])))
      for index, prefix in enumerate([[""], [" "], ["# 注释"], [record(0)]])),
    ("test_header_with_a_byte_order_mark_is_rejected-1", lambda: by_raw(b"\xef\xbb\xbf" + lines_file(BEFORE_CUTOFF))),
    ("test_header_with_a_byte_order_mark_is_rejected-2", lambda: by_lines(BEFORE_CUTOFF, "\r\n")),
    ("test_missing_rows_and_empty_prices_share_the_missing_price_reason-1",
     lambda: by_items(without(rows(40), 10))),
    ("test_missing_rows_and_empty_prices_share_the_missing_price_reason-2",
     lambda: by_items([*rows(40)[:8], (DAYS[8], "", "yahoo"), *rows(40)[9:]])),
    ("test_missing_rows_and_empty_prices_share_the_missing_price_reason-3", lambda: by_items(
        sorted([*rows(40), (D(2001, 1, 6), "100.00", "yahoo")], key=lambda item: item[0]))),
    ("test_missing_rows_and_empty_prices_share_the_missing_price_reason-4",
     lambda: by_items([*rows(40)[:8], (DAYS[8], "100.00", "tiingo"), *rows(40)[9:]])),
    ("test_d16_three_column_header_is_rejected", lambda: by_lines(["date,value,source", *BEFORE_CUTOFF[1:]])),
    ("test_d16_seven_fields_within_the_cutoff_are_rejected",
     lambda: by_lines(replaced(12, f"{DAYS[12]},100.00,IGN_OPEN,IGN_HIGH,IGN_LOW,IGN_CLOSE,yahoo"))),
    ("test_d16_nine_fields_within_the_cutoff_are_rejected",
     lambda: by_lines(replaced(12, f"{DAYS[12]},100.00,IGN_OPEN,IGN_HIGH,IGN_LOW,IGN_CLOSE,IGN_VOLUME,EXTRA,yahoo"))),
    ("test_d16_trailing_comma_within_the_cutoff_is_rejected",
     lambda: by_lines(replaced(12, line8(DAYS[12], "100.00", "yahoo") + ","))),
    ("test_d16_empty_fields_two_to_six_are_accepted",
     lambda: by_lines(replaced(12, f"{DAYS[12]},123.45,,,,,,yahoo"))),
    ("test_d16_close_field_is_not_parsed_or_compared",
     lambda: by_lines(replaced(12, f"{DAYS[12]},100.00,IGN_OPEN,IGN_HIGH,IGN_LOW,999.99,IGN_VOLUME,yahoo"))),
    ("test_d16_empty_value_is_not_filled_from_close",
     lambda: by_lines(replaced(12, f"{DAYS[12]},,IGN_OPEN,IGN_HIGH,IGN_LOW,100.00,IGN_VOLUME,yahoo"))),
    ("test_d16_field_count_is_not_checked_after_the_cutoff",
     lambda: by_lines([*BEFORE_CUTOFF, f"{DAYS[31]},88.88,yahoo"])),
    ("test_d16_metadata_does_not_check_field_counts", lambda: by_lines(replaced(12, f"{DAYS[12]},100.00,yahoo"))),
    ("test_d16_header_with_a_byte_order_mark_is_rejected", lambda: (lambda raw: (
        raw, "SPX", CUTOFF, ruled_registered("SPX", raw), ()))(b"\xef\xbb\xbf" + lines_file(BEFORE_CUTOFF))),
    ("test_d16_source_comes_from_field_seven_before_the_price-1",
     lambda: by_lines(replaced(12, line8(DAYS[12], "100.00", "tiingo")))),
    ("test_d16_source_comes_from_field_seven_before_the_price-2",
     lambda: by_lines(replaced(12, line8(DAYS[12], "", "tiingo")))),
    # 新增三类开读前非法输入（M2 指令第二节第 2 小节第 3 条；设计稿第三节第 1 小节证明第 2 条）。
    ("新增-开读前非法-资产与登记值不符", _other_asset),
    ("新增-开读前非法-截止日不是交易日", lambda: by_items(rows(40), cutoff=D(2001, 1, 13))),
    ("新增-开读前非法-截止日早于登记首日", _late_start),
]
PRE_READ_ILLEGAL = ("新增-开读前非法-资产与登记值不符", "新增-开读前非法-截止日不是交易日",
                    "新增-开读前非法-截止日早于登记首日")

# 完整性守卫（补充二第五节第 2 条）：开工提交 712327273c8ab038a34d34b93fea50aa2152bf05 时本文件全部既有测试函数名，
# 由 git show 7123272:tests/test_wavewarn_v20_data_entry.py 的 AST（模块顶层、以 test_ 开头的函数定义）取得。
START_TESTS = (
    "test_rows_after_the_cutoff_are_never_parsed", "test_metadata_does_not_parse_prices_or_sources",
    "test_hash_conventions_for_crlf_and_lf", "test_registered_value_mismatch_stops_before_any_parsing",
    "test_missing_whole_rows_are_reported", "test_other_input_checks", "test_prices_are_read_with_two_decimals_half_up",
    "test_approved_correction_is_read_normally", "test_correction_mismatches_are_rejected",
    "test_only_matching_correct_entries_apply", "test_empty_filter_result_is_handled_normally",
    "test_cutoff_invariance_across_file_versions", "test_old_registered_values_must_fail_on_a_changed_file",
    "test_hash_and_parsing_are_bound_to_the_same_bytes", "test_assemble_two_assets",
    "test_assemble_reports_real_gaps_but_not_pre_listing_days", "test_assemble_rejects_inconsistent_inputs",
    "test_price_parsing_only_happens_after_the_cutoff_check",
    "test_data_entry_module_does_not_import_provenance_config_or_models",
    "test_cutoff_checks_happen_before_the_file_is_opened",
    "test_blank_line_between_records_within_the_cutoff_is_rejected",
    "test_results_depend_only_on_content_up_to_the_cutoff_record", "test_trailing_newline_is_not_a_blank_record",
    "test_vertical_tab_form_feed_and_double_cr_lines_are_not_blank", "test_fields_with_odd_characters_are_rejected",
    "test_dates_must_be_existing_ascii_yyyy_mm_dd", "test_valid_dates_still_parse",
    "test_prices_follow_the_lexical_rule",
    "test_prices_without_a_fixed_number_of_decimals_are_accepted", "test_header_must_be_the_first_physical_line",
    "test_header_with_a_byte_order_mark_is_rejected",
    "test_missing_rows_and_empty_prices_share_the_missing_price_reason",
    "test_d16_three_column_header_is_rejected", "test_d16_seven_fields_within_the_cutoff_are_rejected",
    "test_d16_nine_fields_within_the_cutoff_are_rejected", "test_d16_trailing_comma_within_the_cutoff_is_rejected",
    "test_d16_empty_fields_two_to_six_are_accepted", "test_d16_close_field_is_not_parsed_or_compared",
    "test_d16_empty_value_is_not_filled_from_close", "test_d16_field_count_is_not_checked_after_the_cutoff",
    "test_d16_metadata_does_not_check_field_counts", "test_d16_header_with_a_byte_order_mark_is_rejected",
    "test_d16_source_comes_from_field_seven_before_the_price",
)
EXCLUDED = {
    "test_assemble_reports_real_gaps_but_not_pre_listing_days": ASSET_SERIES_ONLY,
    "test_assemble_rejects_inconsistent_inputs": ASSET_SERIES_ONLY,
    "test_price_parsing_only_happens_after_the_cutoff_check": "源码文字的静态断言，不构造行情输入",
    "test_data_entry_module_does_not_import_provenance_config_or_models":
        "导入、签名与打开语句的静态断言，不构造行情输入",
    "test_prices_follow_the_lexical_rule": PARSE_PRICE_ONLY,
    "test_prices_without_a_fixed_number_of_decimals_are_accepted": PARSE_PRICE_ONLY,
}


def test_m2_equivalence_cases_cover_every_start_test() -> None:
    """完整性守卫：开工版本的每个既有测试名，要么出现在等价用例 id 中，要么在排除表中；两边不重叠、不遗漏、不多出。"""
    names = [case for case, _ in EQUIVALENCE]
    assert len(names) == len(set(names))
    covered = {name.rsplit("-", 1)[0] if name.rsplit("-", 1)[-1].isdigit() else name
               for name in names if not name.startswith("新增-")}
    assert len(START_TESTS) == 43 and len(set(START_TESTS)) == 43
    assert covered | set(EXCLUDED) == set(START_TESTS) and not covered & set(EXCLUDED)
    assert {name for name in names if name.startswith("新增-")} == set(PRE_READ_ILLEGAL)


@pytest.mark.parametrize(("case", "factory"), EQUIVALENCE, ids=[case for case, _ in EQUIVALENCE])
def test_m2_read_until_equals_the_full_chain_and_the_start_version(tmp_path: Path, case: str, factory) -> None:
    """设计稿第三节第 1 小节证明第 2、4 条：read_until 与完整链（precheck_read → read_file_bytes → parse_until）
    返回值逐字段相等，或抛同类型、同 reason、同消息的异常；二者又与开工版本 read_until 相同。
    以原调用文字直接调用 parse_rows（不传 asset）的结果也与开工版本相同（补充二第三节、补充五第一节第 3 小节）。"""
    raw, asset, cutoff, reg, decisions = factory()
    path = put(tmp_path, "series.csv", raw)
    new = outcome(read_until, path, asset, cutoff, reg, decisions)
    assert outcome(chain, path, asset, cutoff, reg, decisions) == new
    assert outcome(start_read_until, path, asset, cutoff, reg, decisions) == new
    assert outcome(data_v20.parse_rows, raw, cutoff) == outcome(start_parse_rows, raw, cutoff)


@pytest.mark.parametrize("case", ["test_registered_value_mismatch_stops_before_any_parsing-7", *PRE_READ_ILLEGAL])
def test_m2_opening_behaviour_of_read_until_and_the_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           case: str) -> None:
    """设计稿第三节第 1 小节证明第 3 条（测试内同口径计数：记录 Path.open 的路径与模式）：合法输入时 read_until 与
    完整链都恰打开行情文件一次、只读（"rb"）；开读前非法输入时两者都不打开。"""
    raw, asset, cutoff, reg, decisions = dict(EQUIVALENCE)[case]()
    path = put(tmp_path, "series.csv", raw)
    opened: list[tuple[Path, tuple]] = []
    real_open = Path.open

    def recording_open(self: Path, *args, **kwargs):
        opened.append((self, args))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    expected = [] if case in PRE_READ_ILLEGAL else [(path, ("rb",))]
    for function in (read_until, chain):
        opened.clear()
        outcome(function, path, asset, cutoff, reg, decisions)
        assert opened == expected


def test_m2_read_until_signature_is_unchanged() -> None:
    """设计稿第三节第 1 小节证明第 4 条：read_until 的参数与开工版本相同；新增函数的参数如设计稿所列。"""
    assert list(inspect.signature(read_until).parameters) == ["path", "asset", "cutoff", "registered", "decisions"]
    assert list(inspect.signature(data_v20.precheck_read).parameters) == ["asset", "cutoff", "registered"]
    for name in ("parse_until", "diagnose_until"):
        assert list(inspect.signature(getattr(data_v20, name)).parameters) == [
            "raw", "asset", "cutoff", "registered", "decisions"]
    parameters = inspect.signature(data_v20.parse_rows).parameters
    assert list(parameters) == ["raw", "cutoff", "asset"] and parameters["asset"].kind is inspect.Parameter.KEYWORD_ONLY


def caught(function, *args) -> DataEntryError:
    with pytest.raises(DataEntryError) as info:
        function(*args)
    return info.value


def test_m2_empty_value_exception_keeps_type_reason_and_message_and_carries_evidence(tmp_path: Path) -> None:
    """补充四第一节第 3 小节第 1、2、6 条（补充五保留）：空 value 经 read_until 与 parse_until 抛出的异常，
    类型、reason、消息与 parse_price("") 抛出的逐项相同；asset 为所读资产，missing_value_days 恰为该行日期，
    其余为空元组。
    直接调用 parse_price("") 的行为与开工版本相同（类型、reason、消息；属性为默认值）。"""
    direct = caught(data_v20.parse_price, "")
    assert (type(direct), direct.reason, str(direct)) == (data_v20.MissingPriceEntryError, "缺少必需价格", "价格为空")
    assert (direct.asset, direct.missing_value_days, direct.missing_row_days, direct.extra_days) == (None, (), (), ())
    for asset in ("SPX", "QQQ"):
        raw, _, cutoff, reg, decisions = by_items([*rows(40)[:8], (DAYS[8], "", "yahoo"), *rows(40)[9:]], asset=asset)
        path = put(tmp_path, f"{asset}.csv", raw)
        for error in (caught(read_until, path, asset, cutoff, reg, decisions),
                      caught(data_v20.parse_until, raw, asset, cutoff, reg, decisions)):
            assert (type(error), error.reason, str(error)) == (type(direct), direct.reason, str(direct))
            assert error.asset == asset and error.missing_value_days == (DAYS[8],)
            assert error.missing_row_days == () and error.extra_days == ()
    plain = caught(data_v20.parse_rows, content([*rows(40)[:8], (DAYS[8], "", "yahoo"), *rows(40)[9:]]), CUTOFF)
    assert plain.asset is None and plain.missing_value_days == (DAYS[8],)          # 原调用方式不传 asset


def test_m2_axis_errors_carry_structured_days() -> None:
    """设计稿第三节第 2 小节：check_trading_axis 抛出的两种异常带 asset、missing_row_days、extra_days；
    消息与 reason 不变。"""
    days = [day for day in DAYS[:31] if day not in (DAYS[5], DAYS[6])]
    missing = caught(data_v20.check_trading_axis, "QQQ", DAYS[0], CUTOFF, days)
    assert type(missing) is data_v20.MissingPriceEntryError and missing.reason == "缺少必需价格"
    assert (missing.asset, missing.missing_row_days, missing.missing_value_days, missing.extra_days) == (
        "QQQ", (DAYS[5], DAYS[6]), (), ())
    weekend = D(2001, 1, 6)
    extra = caught(data_v20.check_trading_axis, "SPX", DAYS[0], CUTOFF, sorted([*days, weekend]))
    assert type(extra) is data_v20.DataInputError and extra.reason == "输入校验失败"
    assert (extra.asset, extra.missing_row_days, extra.extra_days) == ("SPX", (DAYS[5], DAYS[6]), (weekend,))
    with pytest.raises(AttributeError):
        missing.asset = "SPX"                                                          # type: ignore[misc]  # 只读


def strict_and_diagnosis(case: Case) -> tuple[tuple, tuple]:
    raw, asset, cutoff, reg, decisions = case
    return (outcome(data_v20.parse_until, raw, asset, cutoff, reg, decisions),
            outcome(data_v20.diagnose_until, raw, asset, cutoff, reg, decisions))


def test_m2_diagnosis_collects_missing_days_and_verifies_strict_evidence() -> None:
    """设计稿第三节第 3 小节；补充四第 3 小节第 5 条：诊断读取的序列不含缺价日；收集到的缺价日与严格读取异常的
    已知日期逐日核实为“是”（严格异常只给首个空 value；只缺日时给全部整行缺失日）。"""
    items = [*rows(40)[:8], (DAYS[8], "", "yahoo"), *rows(40)[9:20], (DAYS[20], "", "yahoo"), *rows(40)[21:]]
    raw, asset, cutoff, reg, decisions = by_items(without(items, 25))
    diagnosis = data_v20.diagnose_until(raw, asset, cutoff, reg, decisions)
    assert diagnosis.missing_value_days == (DAYS[8], DAYS[20]) and diagnosis.missing_row_days == (DAYS[25],)
    assert set(diagnosis.series.closes) == set(DAYS[:31]) - {DAYS[8], DAYS[20], DAYS[25]}
    assert diagnosis.series.raw_sha256 == hashlib.sha256(raw).hexdigest()
    strict = caught(data_v20.parse_until, raw, asset, cutoff, reg, decisions)
    assert [day in diagnosis.missing_value_days for day in strict.missing_value_days] == [True]
    # 只缺日：严格读取异常给出全部整行缺失日，逐日在诊断证据中。
    raw, asset, cutoff, reg, decisions = by_items(without(rows(40), 5, 6, 7))
    diagnosis = data_v20.diagnose_until(raw, asset, cutoff, reg, decisions)
    strict = caught(data_v20.parse_until, raw, asset, cutoff, reg, decisions)
    assert strict.missing_row_days == (DAYS[5], DAYS[6], DAYS[7]) == diagnosis.missing_row_days
    assert [day in diagnosis.missing_row_days for day in strict.missing_row_days] == [True, True, True]
    assert diagnosis.missing_value_days == ()
    # 无缺失：诊断读取与严格读取的序列逐字段相同。
    clean = by_items(rows(40))
    strict_result, diagnosis_result = strict_and_diagnosis(clean)
    assert diagnosis_result[0] == "返回"
    full = data_v20.diagnose_until(*clean)
    assert outcome(lambda: full.series) == strict_result and full.missing_value_days == full.missing_row_days == ()


@pytest.mark.parametrize("case", [
    "test_missing_whole_rows_are_reported-4",                          # 多出日
    "test_other_input_checks-1", "test_other_input_checks-2",          # 重复、乱序
    "test_other_input_checks-4",                                       # 来源非法
    "test_fields_with_odd_characters_are_rejected-2",                  # 价格含空白
    "test_dates_must_be_existing_ascii_yyyy_mm_dd-1",                  # 日期格式
    "test_correction_mismatches_are_rejected-1", "test_correction_mismatches_are_rejected-2",   # 修正条目核对
    "test_registered_value_mismatch_stops_before_any_parsing-1",       # 登记值不符
    "test_d16_seven_fields_within_the_cutoff_are_rejected",            # 字段数
])
def test_m2_diagnosis_raises_the_same_input_errors_as_strict_reading(case: str) -> None:
    """M2 指令第二节第 2 小节第 3 条：诊断读取对多出日、格式错误、修正条目等，
    抛与严格读取相同类型、reason、消息的异常。"""
    strict, diagnosis = strict_and_diagnosis(dict(EQUIVALENCE)[case]())
    assert strict[0] == "异常" and strict[1] is data_v20.DataInputError and diagnosis == strict


def test_m2_correction_on_a_day_without_a_row_is_an_input_error_in_both_readings() -> None:
    """修正条目所在日在文件中没有这一行（条目日为非交易日、截止日以内）：两种读取都抛相同的 DataInputError。"""
    case = by_items(rows(40), decisions=(correct(D(2001, 1, 6), "43.31"),))
    strict, diagnosis = strict_and_diagnosis(case)
    assert strict[:3] == ("异常", data_v20.DataInputError, "输入校验失败") and "没有这一行" in strict[3]
    assert diagnosis == strict
    # 修正条目所在日为空 value 行：严格读取先报缺价；诊断读取跳过该行后核对修正条目，报“没有这一行”。
    case = by_items(with_row(12, "", "correct:yahoo"), decisions=(correct(DAYS[12], "43.31"),))
    strict, diagnosis = strict_and_diagnosis(case)
    assert strict[1] is data_v20.MissingPriceEntryError
    assert diagnosis[:3] == ("异常", data_v20.DataInputError, "输入校验失败") and "没有这一行" in diagnosis[3]


def test_m2_empty_value_with_an_illegal_source_reports_the_source_in_both_readings(tmp_path: Path) -> None:
    """补充四第 3 小节第 3 条：空 value 行同时来源非法时，严格读取、诊断读取都抛与开工版本相同的来源错误。"""
    raw, asset, cutoff, reg, decisions = by_lines(replaced(12, line8(DAYS[12], "", "tiingo")))
    start = outcome(start_read_until, put(tmp_path, "s.csv", raw), asset, cutoff, reg, decisions)
    assert start[:3] == ("异常", data_v20.DataInputError, "输入校验失败") and "来源标识" in start[3]
    assert strict_and_diagnosis((raw, asset, cutoff, reg, decisions)) == (start, start)


def test_m2_whitespace_value_is_an_input_error_in_both_readings() -> None:
    """补充四第 3 小节第 4 条：含空白的 value 在两种读取下都抛 DataInputError（不当作空价格）。"""
    for value in (" ", "\t", "100.00 "):
        strict, diagnosis = strict_and_diagnosis(by_lines(replaced(12, line8(DAYS[12], value, "yahoo"))))
        assert strict[1] is data_v20.DataInputError and diagnosis == strict


def test_m2_order_checks_include_empty_value_rows() -> None:
    """补充五第一节第 3 小节：重复、乱序比较的前一条记录包括空价格行。
    - 空价格行在前（D1、D3 空价格、D2；D1、D2 空价格、D2）：严格读取在空价格行抛 MissingPriceEntryError（同一输入），
      诊断读取在其后一行抛“日期重复或乱序”，与把该空价格补成合法价格后严格读取在同一位置抛出的异常类型、reason、消息相同；
    - 空价格行在后（D1、D3、D2 空价格；D1、D2、D2 空价格）：两种读取抛完全相同的异常。"""
    d1, d2, d3 = DAYS[10], DAYS[11], DAYS[12]
    base = rows(40)
    for first, second in ((d3, d2), (d2, d2)):
        empty_first = [*base[:10], (d1, "100.00", "yahoo"), (first, "", "yahoo"), (second, "100.00", "yahoo"),
                       *base[13:]]
        filled = [*base[:10], (d1, "100.00", "yahoo"), (first, "100.00", "yahoo"), (second, "100.00", "yahoo"),
                  *base[13:]]
        strict, diagnosis = strict_and_diagnosis(by_items(empty_first))
        assert strict[1] is data_v20.MissingPriceEntryError
        reference, _ = strict_and_diagnosis(by_items(filled))
        assert reference[:3] == ("异常", data_v20.DataInputError, "输入校验失败") and "重复或乱序" in reference[3]
        assert diagnosis == reference
        empty_second = [*base[:10], (d1, "100.00", "yahoo"), (first, "100.00", "yahoo"), (second, "", "yahoo"),
                        *base[13:]]
        strict, diagnosis = strict_and_diagnosis(by_items(empty_second))
        assert strict == diagnosis == reference


def test_m2_diagnosis_stops_at_the_cutoff_even_when_its_value_is_empty() -> None:
    """补充五第一节第 3 小节：截止日当天为空价格行时，诊断读取不处理截止日之后的行；严格读取在同一输入上抛
    MissingPriceEntryError。截止日之后放一行日期非法的记录与一行合法记录（元数据要读末日）。"""
    lines = [*BEFORE_CUTOFF[:-1], line8(CUTOFF, "", "yahoo"), "not-a-date,x,y", record(31)]
    case = by_raw(lines_file(lines))
    raw, asset, cutoff, _, decisions = case
    reg = RegisteredFile("SPX", hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw).hexdigest(), 33, DAYS[0], DAYS[31])
    diagnosis = data_v20.diagnose_until(raw, asset, cutoff, reg, decisions)
    assert diagnosis.missing_value_days == (CUTOFF,) and diagnosis.missing_row_days == ()
    assert list(diagnosis.series.closes) == DAYS[:30]
    strict = caught(data_v20.parse_until, raw, asset, cutoff, reg, decisions)
    assert type(strict) is data_v20.MissingPriceEntryError and strict.missing_value_days == (CUTOFF,)
