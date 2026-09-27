"""人工价格修正的来源追溯、重复构建、历史修订与回归隔离。"""

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from market_risk.config import ConfigError, DataDecision, load_data_decisions
from market_risk.data import market
from market_risk.data.raw_io import load_raw_inputs, save_raw_inputs
from market_risk.storage.paths import StoragePaths

DAY = dt.date(2020, 1, 2)


def correction(value='101'):
    return DataDecision(DAY, 'SPY', 'correct', '人工核对', dt.date(2026, 9, 27), Decimal(value), '测试证据')


def series(value=100):
    return market.NewSeries('SPY', 'etf', {DAY: {'value': value, 'close': value, 'open': 99,
                                               'high': 102, 'low': 98, 'volume': 1000, 'source': 'yahoo'}}, 'yahoo')


def test_correction_is_idempotent_and_compares_originals(tmp_path):
    paths = StoragePaths(tmp_path)
    market.build_dataset(paths, [series()])
    first = market.build_dataset(paths, [series()], decisions=(correction(),))
    assert not first.revisions
    row = market.read_series_file(paths.market_daily_file('SPY'))[1][DAY]
    assert row == {**series().rows[DAY], 'value': 101, 'close': 101, 'source': 'correct:yahoo'}
    audit = first.series['SPY']['corrections'][0]
    assert audit['original_value'] == 100 and audit['corrected_value'] == '101'
    assert audit['evidence_source'] == '测试证据'
    before = paths.market_manifest.read_bytes()
    repeated = market.build_dataset(paths, [series()], decisions=(correction(),))
    assert not repeated.revisions and not repeated.changed
    assert paths.market_manifest.read_bytes() == before
    # 来源后来修订为102：旧值必须是来源原值100，不能是人工修正101。
    rejected = market.build_dataset(paths, [series(102)], decisions=(correction(),))
    assert {(r.old, r.new) for r in rejected.revisions} == {(100, 102)}
    assert rejected.series['SPY']['corrections'][0]['original_value'] == 100
    accepted = market.build_dataset(paths, [series(102)], accept_revisions=True, decisions=(correction(),))
    assert accepted.series['SPY']['corrections'][0]['original_value'] == 102
    assert market.read_series_file(paths.market_daily_file('SPY'))[1][DAY]['close'] == 101
    # 撤销裁定恢复已接受来源原值。
    market.build_dataset(paths, [series(102)])
    assert market.read_series_file(paths.market_daily_file('SPY'))[1][DAY]['close'] == 102


def test_source_disappears_keeps_original_and_correction(tmp_path):
    paths = StoragePaths(tmp_path)
    market.build_dataset(paths, [series()], decisions=(correction(),))
    result = market.build_dataset(paths, [dataclasses.replace(series(), rows={})], decisions=(correction(),))
    assert result.series['SPY']['corrections'][0]['original_value'] == 100
    assert not result.revisions


def test_corrections_apply_without_new_cache_input(tmp_path):
    from market_risk.models import SourceInfo

    paths = StoragePaths(tmp_path)
    info = SourceInfo('yahoo', 'SPY', '测试来源', '2020-01-03T00:00:00+00:00', 1, DAY, DAY,
                      False, 'data/cache/yahoo/测试.csv')
    market.build_dataset(paths, [dataclasses.replace(series(), inputs=[info])])
    result = market.build_dataset(paths, [], decisions=(correction(),))
    assert result.changed == ['SPY']
    assert result.series['SPY']['downloaded_at_utc'] == info.downloaded_at_utc
    assert result.series['SPY']['inputs'] == [info.cache_file]
    assert market.read_series_file(paths.market_daily_file('SPY'))[1][DAY]['value'] == 101
    market.build_dataset(paths, [])
    assert market.read_series_file(paths.market_daily_file('SPY'))[1][DAY]['value'] == 100


def test_unregistered_symbol_is_not_silently_ignored(tmp_path):
    with pytest.raises(market.MarketDataError, match='标的不存在'):
        market.build_dataset(StoragePaths(tmp_path), [], decisions=(correction(),))


def test_invalid_correction_fails_before_any_write(tmp_path):
    paths = StoragePaths(tmp_path)
    bad = dataclasses.replace(correction(), date=DAY + dt.timedelta(days=1))
    with pytest.raises(ValueError):
        market.build_dataset(paths, [series()], decisions=(bad,))
    assert not paths.market_manifest.exists()
    assert not paths.market_daily_file('SPY').exists()


@pytest.mark.parametrize('value', ['-1', '0', '1.00001', '1000000000000', 'NaN', 'Infinity'])
def test_correction_rejects_invalid_price(value):
    with pytest.raises(ConfigError):
        correction(value)


def test_duplicate_corrections_fail(tmp_path):
    with pytest.raises(ValueError, match='重复'):
        market.build_dataset(StoragePaths(tmp_path), [series()], decisions=(correction(), correction()))


@pytest.mark.parametrize('extra', ['', 'corrected_value: .nan', 'corrected_value: 101'])
def test_yaml_requires_finite_value_and_evidence(tmp_path, extra):
    path = tmp_path / 'decisions.yaml'
    path.write_text('- date: 2020-01-02\n  symbol: SPY\n  decision: correct\n  reason: 测试\n'
                    '  decided_on: 2026-09-27\n' + (f'  {extra}\n' if extra else ''), encoding='utf-8')
    with pytest.raises(ConfigError):
        load_data_decisions(path)


def test_raw_inputs_preserve_correction(tmp_path):
    from conftest import load_sample_raw

    raw = dataclasses.replace(load_sample_raw('2025-11-28'), decisions=(correction(),))
    save_raw_inputs(raw, tmp_path)
    assert load_raw_inputs(tmp_path).decisions == raw.decisions


def test_future_correction_does_not_change_past_score(tmp_path):
    from test_market import SETTINGS, _sample, dataset_from_raw

    from market_risk.data.snapshot import build_snapshot
    from market_risk.pipeline import score_snapshot

    raw, breadth = _sample()
    paths = StoragePaths(tmp_path)
    inputs = dataset_from_raw(raw, breadth)
    future = dt.date(2025, 12, 1)
    spy = next(s for s in inputs if s.name == 'SPY')
    spy.rows[future] = {**next(iter(spy.rows.values())), 'value': 500, 'close': 500}
    market.build_dataset(paths, inputs)
    clean = build_snapshot(market.load_raw_inputs(paths, SETTINGS, raw.base_date, revision_check=False))
    market.build_dataset(paths, inputs, decisions=(dataclasses.replace(correction('999999'), date=future),))
    after = build_snapshot(market.load_raw_inputs(paths, SETTINGS, raw.base_date, revision_check=False))
    assert score_snapshot(clean, SETTINGS) == score_snapshot(after, SETTINGS)


def test_correct_reference_roundtrip_and_rebuild(tmp_path, monkeypatch):
    from market_risk.config import PROJECT_ROOT
    from market_risk.storage import db, schema

    cfg = tmp_path / 'config'
    cfg.mkdir()
    for name in ('symbols.yaml', 'holidays.yaml'):
        (cfg / name).write_bytes((PROJECT_ROOT / 'config' / name).read_bytes())
    (cfg / 'data_decisions.yaml').write_text(
        '- {date: 2020-01-02, symbol: SPY, decision: correct, corrected_value: 101.1234, '
        'evidence_source: 人工证据, reason: 已批准, decided_on: 2026-09-27}\n', encoding='utf-8')
    reader = db.reference_rows
    monkeypatch.setattr(db, 'reference_rows', lambda config_dir=None: reader(cfg))
    paths = StoragePaths(tmp_path)
    for _ in range(2):
        url = db.rebuild(paths, db.default_url(paths))
        engine = db.make_engine(url)
        try:
            with engine.connect() as conn:
                row = conn.execute(schema.data_decisions.select()).one()
                assert row.corrected_value == Decimal('101.1234')
                assert row.evidence_source == '人工证据' and row.decision == 'correct'
        finally:
            engine.dispose()


def test_data_build_reports_invalid_correction_as_service_error(tmp_path, monkeypatch):
    """裁定表有误时，data build 显示原因（ServiceError），不抛异常栈。"""
    from market_risk import services
    from market_risk.config import load_settings
    from market_risk.storage import db

    ctx = services.Context(load_settings(), StoragePaths(tmp_path), db.default_url(StoragePaths(tmp_path)))
    bad = dataclasses.replace(correction(), date=DAY + dt.timedelta(days=1))
    monkeypatch.setattr(services, 'load_data_decisions', lambda: (bad,))
    with pytest.raises(services.ServiceError, match=r'data_decisions\.yaml'):
        services.data_build(ctx, end=DAY, collect=lambda e: ([series()], {}))
    monkeypatch.setattr(services, 'load_data_decisions', lambda: (correction(), correction()))
    with pytest.raises(services.ServiceError, match='重复'):
        services.data_build(ctx, end=DAY, collect=lambda e: ([series()], {}))
