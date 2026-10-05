"""阶段四开发期输出（development_output）的构造文件读写测试（M2 第一部分指令第一节第 5 小节第 5 条）。

全部在 tmp_path 下：格式规则（UTF-8 无 BOM、LF、JSON 键序、CSV 空值、gzip mtime=0）；清单不含自身、排序、读回复算；
四个失败分支各自产生的文件集合；独占写入与改名在目标已存在时失败且不覆盖；发布后失败时正式目录逐字节不变。
失败分支直接调用入口模块的分支函数（不调用 main，不安装审计钩子；Audit 只作为普通对象记录尾段内容）。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.wavewarn_v20 import development_output as output
from market_risk.wavewarn_v20 import development_run as entry
from market_risk.wavewarn_v20.development_output import OutputError, PreflightError


def put(path: Path, data: bytes) -> Path:
    """把构造的字节写进临时目录。"""
    path.write_bytes(data)
    return path


def get(path: Path) -> bytes:
    """读取被测函数写出的文件（只读）。"""
    return path.read_bytes()


def names(directory: Path) -> set[str]:
    return {item.name for item in directory.iterdir()}


# ---------------------------------------------------------------------------
# 格式规则
# ---------------------------------------------------------------------------


def test_json_is_utf8_without_bom_with_lf_and_keeps_key_order() -> None:
    data = output.json_bytes({"乙": Decimal("1.50"), "甲": dt.date(2001, 1, 2), "列": [None, True, 0.5]})
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and data.endswith(b"\n")
    text = data.decode("utf-8")
    assert list(json.loads(text)) == ["乙", "甲", "列"] and '"乙": "1.50"' in text and '"甲": "2001-01-02"' in text
    with pytest.raises(OutputError):
        output.json_bytes({"x": float("nan")})
    with pytest.raises(OutputError):
        output.json_bytes({"x": object()})


def test_csv_empty_field_means_none_and_rows_end_with_lf() -> None:
    data = output.csv_bytes(["a", "b", "c", "d"], [[None, True, 0.1, dt.date(2001, 1, 2)], ["x,y", False, 2, "z"]])
    assert data == b'a,b,c,d\n,true,0.1,2001-01-02\n"x,y",false,2,z\n'
    with pytest.raises(OutputError):
        output.csv_bytes(["a"], [[""]])
    with pytest.raises(OutputError):
        output.csv_bytes(["a"], [[float("inf")]])
    with pytest.raises(OutputError):
        output.csv_bytes(["a", "b"], [[1]])


def test_gzip_has_zero_mtime_and_no_name_and_is_reproducible() -> None:
    first, second = output.gzip_bytes(b"a,b\n1,2\n"), output.gzip_bytes(b"a,b\n1,2\n")
    assert first == second and first[4:8] == b"\x00\x00\x00\x00" and first[3] & 0x08 == 0
    assert gzip.decompress(first) == b"a,b\n1,2\n"


# ---------------------------------------------------------------------------
# 清单：不含自身、按相对路径排序、读回复算
# ---------------------------------------------------------------------------


def test_manifest_is_sorted_and_verified_after_reading_back(tmp_path: Path) -> None:
    directory = tmp_path / "package"
    output.make_directory(directory)
    files = {"b.json": output.json_bytes({"k": 1}), "a.csv": output.csv_bytes(["x"], [[1]]),
             "c.csv.gz": output.gzip_bytes(b"x\n1\n")}
    assert output.write_files(directory, files) == ["b.json", "a.csv", "c.csv.gz"]
    output.verify_written(directory, files, (directory,))
    listing = output.manifest_bytes(files)
    assert [line.split(" ", 2)[2] for line in listing.decode().splitlines()] == ["a.csv", "b.json", "c.csv.gz"]
    output.write_new(directory / output.MANIFEST, listing)
    listed = output.verify_manifest(directory, output.MANIFEST, (directory,))
    assert set(listed) == set(files) and output.MANIFEST not in listed
    with pytest.raises(OutputError):
        output.read_back(directory / "a.csv", (tmp_path / "other",))
    with pytest.raises(OutputError):
        output.parse_manifest(b"abc 1 a.csv\n")


def test_manifest_recomputation_detects_a_changed_file(tmp_path: Path) -> None:
    directory = tmp_path / "package"
    output.make_directory(directory)
    files = {"a.csv": b"x\n1\n"}
    output.write_files(directory, files)
    output.write_new(directory / output.MANIFEST, output.manifest_bytes({"a.csv": b"x\n2\n"}))
    with pytest.raises(OutputError, match="清单复算不符"):
        output.verify_manifest(directory, output.MANIFEST, (directory,))
    with pytest.raises(OutputError, match="读回字节"):
        output.verify_written(directory, {"a.csv": b"x\n2\n"}, (directory,))


# ---------------------------------------------------------------------------
# 独占写入与改名：目标已存在时失败且不覆盖
# ---------------------------------------------------------------------------


def test_exclusive_write_fails_and_keeps_the_existing_file(tmp_path: Path) -> None:
    target = put(tmp_path / "f.json", b"old\n")
    with pytest.raises(FileExistsError):
        output.write_new(target, b"new\n")
    assert get(target) == b"old\n"
    calls: list[str] = []
    output.write_new(tmp_path / "g.txt", lambda: b"later\n", lambda: calls.append("closed"))
    assert get(tmp_path / "g.txt") == b"later\n" and calls == ["closed"]


def test_make_directory_fails_when_it_exists(tmp_path: Path) -> None:
    output.make_directory(tmp_path / "a" / "b")
    with pytest.raises(FileExistsError):
        output.make_directory(tmp_path / "a" / "b")


def test_rename_fails_when_the_target_exists_and_changes_nothing(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    output.make_directory(source)
    output.make_directory(target)
    output.write_new(source / "x", b"s\n")
    output.write_new(target / "x", b"t\n")
    with pytest.raises(OutputError, match="改名目标已存在"):
        output.rename_directory(source, target)
    assert get(source / "x") == b"s\n" and get(target / "x") == b"t\n"
    output.rename_directory(source, tmp_path / "moved")
    assert not output.path_exists(source) and get(tmp_path / "moved" / "x") == b"s\n"


def test_preflight_reader_accepts_only_registered_paths_and_only_before_closing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output, "_preflight_closed", False)
    put(tmp_path / "pyproject.toml", b"# p\n")
    assert output.read_preflight_file(tmp_path, "pyproject.toml") == b"# p\n"
    with pytest.raises(PreflightError, match="不在预检文件集合之内"):
        output.read_preflight_file(tmp_path, "other.toml")
    output.close_preflight()
    with pytest.raises(PreflightError, match="预检已结束"):
        output.read_preflight_file(tmp_path, "pyproject.toml")


@pytest.mark.parametrize("relative", ["other.toml", "../pyproject.toml", "config/../../pyproject.toml",
                                      "data/x.csv", "data", "src/market_risk/../../data/x.csv"])
def test_preflight_reader_rejects_paths_outside_the_set_without_opening_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    """M2 指令第一节第 5 小节第 6 条：集合外路径、“..”越界、指向 data 的路径均被拒绝，且不打开文件。"""
    monkeypatch.setattr(output, "_preflight_closed", False)
    opened: list[Path] = []
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(self) or b"")
    with pytest.raises(PreflightError, match="不在预检文件集合之内"):
        output.read_preflight_file(tmp_path, relative)
    assert opened == []


# ---------------------------------------------------------------------------
# 四个失败分支各自产生的文件集合（直接调用分支函数；不安装钩子）
# ---------------------------------------------------------------------------


def places_in(root: Path) -> output.Locations:
    return output.locations(root, "20260101T000000Z", "abcdef1")


def failure() -> entry.RunFailure:
    return entry.RunFailure("计算失败", "选择", ValueError("构造"), {"k": 1})


def tail_records(places: output.Locations) -> list[dict]:
    return [json.loads(line) for line in get(places.tail).decode("utf-8").splitlines()]


def test_failure_before_manifest_file_set(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    places = places_in(tmp_path)
    output.make_directory(places.staging)
    written = {"run_record.json": output.json_bytes({"a": 1})}
    writes = entry.WriteRecord()                     # 补充七：写入经完成状态记录（与入口同一方式）
    for name, data in written.items():
        writes.write(output, places.staging / name, data)
    code = entry._fail(failure(), "trace", entry.Audit(), entry.Information(), places, written, writes, None, None,
                       output.readable_roots(places), output, {})
    assert code == 1 and not output.path_exists(places.staging)
    assert json.loads(get(places.failed / "failure.json"))["incomplete_files"] == []
    assert names(places.failed) == {"run_record.json", "failure.json", "information_state_final.json",
                                    "audit_log.jsonl", "FAILURE_MANIFEST.sha256"}
    listed = output.verify_manifest(places.failed, output.FAILURE_MANIFEST, (places.failed,))
    assert set(listed) == names(places.failed) - {"FAILURE_MANIFEST.sha256"}
    assert names(places.research) == {places.failed.name, places.tail.name}
    assert tail_records(places)[0]["manifest_file"] == "FAILURE_MANIFEST.sha256"
    assert tail_records(places)[-1]["outcome"] == "失败"
    assert json.loads(capsys.readouterr().out)["exit_code"] == 1


def frozen_package(places: output.Locations, writes: entry.WriteRecord) -> dict[str, bytes]:
    output.make_directory(places.staging)
    files = {"run_record.json": output.json_bytes({"a": 1}), "report.md": b"# r\n",
             "audit_log.jsonl": b'{"record": "coverage", "last_seq": 0, "violations_in_segment": 0}\n'}
    for name, data in files.items():
        writes.write(output, places.staging / name, data)
    writes.write(output, places.staging / output.MANIFEST, output.manifest_bytes(files))
    return files


def test_failure_after_manifest_file_set(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    places = places_in(tmp_path)
    writes = entry.WriteRecord()
    files = frozen_package(places, writes)
    manifest_sha = output.sha256(get(places.staging / output.MANIFEST))
    code = entry._fail_after_manifest(failure(), "trace", entry.Audit(), entry.Information(), places, writes, 0,
                                      manifest_sha, None, output.readable_roots(places), output, {})
    assert code == 1
    assert json.loads(get(places.failed / "failure.json"))["incomplete_files"] == []
    assert names(places.failed) == {*files, "MANIFEST.sha256", "failure.json", "information_state_final.json",
                                    "FAILURE_MANIFEST.sha256"}
    document = json.loads(get(places.failed / "failure.json"))
    assert document["manifest_sha256"] == manifest_sha and "不覆盖本失败目录的最终内容" in document["manifest_scope"]
    listed = output.verify_manifest(places.failed, output.FAILURE_MANIFEST, (places.failed,))
    assert "MANIFEST.sha256" in listed
    assert tail_records(places)[0]["frozen_manifest_sha256"] == manifest_sha
    capsys.readouterr()


def test_failure_after_publishing_leaves_the_formal_directory_byte_identical(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    places = places_in(tmp_path)
    writes = entry.WriteRecord()
    frozen_package(places, writes)
    output.rename_directory(places.staging, places.formal)
    writes.moved(places.staging, places.formal)
    before = {name: get(places.formal / name) for name in names(places.formal)}
    manifest_sha = output.sha256(before[output.MANIFEST])
    code = entry._fail_published(failure(), entry.Audit(), entry.Information(), places, writes, 0, manifest_sha, None,
                                 output.readable_roots(places), output, {})
    assert code == 1
    assert {name: get(places.formal / name) for name in names(places.formal)} == before
    assert names(places.research) == {places.formal.name, places.publish_failure.name, places.tail.name}
    note = json.loads(get(places.publish_failure))
    assert note["statement"] == output.NOT_A_SUCCESS and note["manifest_sha256"] == manifest_sha
    assert tail_records(places)[-1]["failure_note"] == places.publish_failure.name
    capsys.readouterr()


def test_unwritable_evidence_keeps_the_staging_name_and_exits_three(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    places = places_in(tmp_path)
    output.make_directory(places.staging)
    output.write_new(places.staging / "failure.json", b"{}\n")            # 已存在：独占写入失败，不覆盖
    code = entry._fail(failure(), "trace", entry.Audit(), entry.Information(), places, {}, entry.WriteRecord(), None,
                       None, output.readable_roots(places), output, {})
    assert code == 3 and output.path_exists(places.staging) and not output.path_exists(places.failed)
    assert get(places.staging / "failure.json") == b"{}\n"
    assert names(places.staging) == {"failure.json"}
    assert "失败证据未完整取得" in capsys.readouterr().err
    assert tail_records(places)[-1]["outcome"] == "失败"
