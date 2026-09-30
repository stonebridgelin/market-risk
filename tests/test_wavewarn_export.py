"""研究 ZZ 标签输出按配置门槛复算，并与既有标签路径隔离。"""

import csv
import datetime as dt
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.config import WavewarnConfig
from market_risk.wavewarn.export import write_development_labels
from market_risk.wavewarn.inputs import DevelopmentInputs


def test_development_labels_use_separate_configured_decline_and_rebound_thresholds(tmp_path: Path) -> None:
    days = tuple(dt.date(2010, 1, 1) + dt.timedelta(days=index) for index in range(4))
    spx = dict(zip(days, (Decimal(100), Decimal(96), Decimal(90), Decimal("94.5")), strict=True))
    qqq = {day: Decimal(100) for day in days}
    inputs = DevelopmentInputs(days, {"SPX": spx, "QQQ": qqq})
    config = WavewarnConfig({"development_end": "2016-12-30",
                             "zz": {"spx_decline": "0.04", "spx_rebound": "0.05",
                                    "qqq_decline": "0.05", "qqq_rebound": "0.065"}})
    assert write_development_labels(inputs, tmp_path, config) == (1, 1)
    with (tmp_path / "zz_events_development.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    # 100×0.96=96 于第1天触及；90×1.05=94.5 于第3天结束。
    assert (rows[0]["t0_date"], rows[0]["end_date"]) == (days[1].isoformat(), days[3].isoformat())
    stricter = WavewarnConfig({"development_end": "2016-12-30",
                               "zz": {"spx_decline": "0.11", "spx_rebound": "0.05",
                                      "qqq_decline": "0.05", "qqq_rebound": "0.065"}})
    assert write_development_labels(inputs, tmp_path / "stricter", stricter) == (0, 0)
