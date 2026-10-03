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


def content(items: list[Row], newline: str = "\n", header: str = "date,value,source") -> bytes:
    lines = [header, *(f"{day},{price},{source}" for day, price, source in items)]
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
    source = inspect.getsource(data_v20.parse_rows)
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
    return f"{DAYS[index]},{price},yahoo"


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


HEADER_LINE = "date,value,source"
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
    (f"{DAYS[12]},100.00\x0b,yahoo", "价格含空白或不可打印字符"),          # Decimal 本会去掉首尾空白
    (f"{DAYS[12]},100.00 ,yahoo", "价格含空白或不可打印字符"),
    (f"{DAYS[12]}\x0c,100.00,yahoo", "日期字段不是 ISO 日期"),
    (f"{DAYS[12]},100.00,yahoo\x0b", "来源标识不在允许清单内"),
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
    middle = [*BEFORE_CUTOFF[:13], f"{bad},100.00,yahoo", *BEFORE_CUTOFF[14:]]
    with pytest.raises(DataEntryError, match="日期字段不是 ISO 日期"):
        read_lines(tmp_path, middle)
    first = [HEADER_LINE, f"{bad},100.00,yahoo", *BEFORE_CUTOFF[2:]]               # 元数据取首日
    with pytest.raises(DataEntryError, match="日期字段不是 ISO 日期"):
        file_metadata(lines_file(first))
    last = [*BEFORE_CUTOFF, f"{bad},100.00,yahoo"]                                  # 元数据取末日
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
