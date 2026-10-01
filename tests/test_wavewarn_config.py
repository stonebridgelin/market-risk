"""v1.2.1 已登记业务参数与分侧开关的配置路径。"""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import pytest

from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.loss import configured_loss_settings


def test_confirmed_business_parameters_and_side_switches() -> None:
    config = load_wavewarn_config(Path(__file__).resolve().parents[1] / "config/wavewarn_v121.yaml")
    # SPX 下跌4%与反弹5%各自从配置读取，不把两道门槛混成同一数值。
    assert config.zz_thresholds()["SPX"] == (Decimal("0.04"), Decimal("0.05"))
    assert config.zz_thresholds()["QQQ"] == (Decimal("0.05"), Decimal("0.065"))
    assert config.fixed_parameters().anchor == 63
    assert config.candidate_sets().k == (3, 5, 10)
    assert config.paired_parameters().seed_main == 20260929
    assert (config.paired_parameters().short_block_length,
            config.paired_parameters().long_block_length,
            config.paired_parameters().event_padding) == (10, 40, 20)
    business = config.require_business_parameters()
    # 负责人登记 0.5、E2、2:1 与四侧去留；配置只负责接线，不证明效果。
    assert (business.eta, business.e_version, business.kappa_d, business.beta) == (
        Decimal("0.5"), "E2", Decimal(2), Decimal(1))
    assert configured_loss_settings(config).parameters.gamma == Decimal("0.005")
    selection = config.require_channel_selection()
    assert (selection.b_spx, selection.dv_spx, selection.b_qqq, selection.dv_qqq) == (
        False, True, True, True)


def test_constructed_business_and_channel_values_validate_without_setting_real_config(tmp_path: Path) -> None:
    file = tmp_path / "wavewarn.yaml"
    file.write_text("""version: v1.2.1
nl_unknown_policy: three_valued
business: {eta: '0.5', e_version: E2, kappa_d: '2', beta: '1'}
channel_selection: {b_spx: true, dv_spx: false, b_qqq: false, dv_qqq: true}
""", encoding="utf-8")
    config = load_wavewarn_config(file)
    # 构造值2≥1>0且0≤0.5≤1；四侧开关仅在临时配置显式填写，不落盘。
    assert config.require_business_parameters().e_version == "E2"
    selection = config.require_channel_selection()
    assert (selection.b_spx, selection.dv_spx, selection.b_qqq, selection.dv_qqq) == (
        True, False, False, True)
    missing = copy.deepcopy(config.raw)
    missing["channel_selection"]["b_spx"] = None
    with pytest.raises(ValueError, match="须显式填写四侧开关"):
        type(config)(missing).require_channel_selection()
    # 临时构造配置补齐固定字段，仅核查参数接线；不代表实际选择。
    real = load_wavewarn_config(Path(__file__).resolve().parents[1] / "config/wavewarn_v121.yaml")
    complete = copy.deepcopy(real.raw)
    complete["business"] = {"eta": "0.5", "e_version": "E2", "kappa_d": "2", "beta": "1"}
    settings = configured_loss_settings(type(real)(complete))
    params = settings.parameters
    assert (params.eta, params.noise_floor, params.gamma) == (
        Decimal("0.5"), Decimal("0.02"), Decimal("0.005"))
    assert (settings.mu, settings.weights) == (
        Decimal(0), {"SPX": Decimal("0.5"), "QQQ": Decimal("0.5")})


def test_fixed_config_rejects_changed_window_and_candidate_order() -> None:
    original = load_wavewarn_config(Path(__file__).resolve().parents[1] / "config/wavewarn_v121.yaml")
    altered_window = copy.deepcopy(original.raw)
    altered_window["windows"]["anchor"] = 64
    with pytest.raises(ValueError, match=r"windows\.anchor"):
        type(original)(altered_window).fixed_parameters()
    altered_candidates = copy.deepcopy(original.raw)
    altered_candidates["candidates"]["k"] = [5, 3, 10]
    with pytest.raises(ValueError, match="登记顺序"):
        type(original)(altered_candidates).candidate_sets()
    altered_end = copy.deepcopy(original.raw)
    altered_end["development_end"] = "2022-12-30"
    # 截止日若被改成验证期，固定校验必须先报错，不能读取后默默沿用代码常量。
    with pytest.raises(ValueError, match="development_end"):
        type(original)(altered_end).fixed_parameters()
    altered_pairing = copy.deepcopy(original.raw)
    altered_pairing["paired_test"]["half_split"] = "2021-01-01"
    with pytest.raises(ValueError, match="half_split"):
        type(original)(altered_pairing).paired_parameters()
