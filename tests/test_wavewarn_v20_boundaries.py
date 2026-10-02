"""v2.0 的包边界与所复用的交易日历：依赖方向测试、NYSE 历史休市日。不读任何数据文件。"""

from __future__ import annotations

import ast
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from market_risk.calendar import is_stock_trading_day, stock_trading_days

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "market_risk" / "wavewarn_v20"
# wavewarn_v20 下任何模块都不得导入的模块（及其子模块）。
FORBIDDEN = ("market_risk.wavewarn", "market_risk.scoring", "market_risk.outcomes", "market_risk.data.market",
             "market_risk.data.snapshot", "market_risk.research")
# 本批的模块都是纯计算：除本包与标准库外不导入任何东西。
PURE = ("snapshot", "inputs", "channels", "state_machine", "convergence", "reference", "execution", "nav")


def forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN)


def modules() -> list[str]:
    return sorted(f"market_risk.wavewarn_v20.{path.stem}" for path in PACKAGE.glob("*.py") if path.stem != "__init__")


def test_no_module_imports_forbidden_modules_statically() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        assert sorted(name for name in names if forbidden(name)) == [], path.name
        project = {name for name in names if name.startswith("market_risk")}
        if path.stem in PURE:
            assert all(name.startswith("market_risk.wavewarn_v20") for name in project), path.name


@pytest.mark.parametrize("module", modules())
def test_importing_a_module_loads_no_forbidden_module(module: str) -> None:
    """在全新的解释器里只导入这一个模块，检查实际被加载的全部模块（含间接导入）。"""
    code = ("import importlib, json, sys\n"
            f"importlib.import_module({module!r})\n"
            "print(json.dumps(sorted(name for name in sys.modules if name.startswith('market_risk'))))\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    loaded = json.loads(done.stdout)
    assert [name for name in loaded if forbidden(name)] == []
    assert all(name == "market_risk" or name.startswith("market_risk.wavewarn_v20") for name in loaded)


def test_forbidden_check_does_not_confuse_wavewarn_with_wavewarn_v20() -> None:
    assert forbidden("market_risk.wavewarn") and forbidden("market_risk.wavewarn.channels")
    assert not forbidden("market_risk.wavewarn_v20") and not forbidden("market_risk.wavewarn_v20.channels")
    assert forbidden("market_risk.data.market") and not forbidden("market_risk.data.market_build")


# ---------------------------------------------------------------------------
# 交易日历（复用 market_risk.calendar）：历史上的特殊休市日。期望值取自交易所的休市记录，经产品经理确认。
# ---------------------------------------------------------------------------

D = dt.date
CLOSED = [D(2001, 9, 11), D(2001, 9, 12), D(2001, 9, 13), D(2001, 9, 14),     # 九一一之后休市四天
          D(2004, 6, 11),                                                      # 里根总统国葬
          D(2007, 1, 2),                                                       # 福特总统国葬
          D(2012, 10, 29), D(2012, 10, 30),                                    # 飓风桑迪
          D(2018, 12, 5),                                                      # 老布什总统国葬
          D(2025, 1, 9)]                                                       # 卡特总统国葬


@pytest.mark.parametrize("day", CLOSED)
def test_special_closures_are_not_trading_days(day: dt.date) -> None:
    assert day.weekday() < 5                    # 都是工作日，所以休市不是因为周末
    assert not is_stock_trading_day(day)
    assert day not in stock_trading_days(day - dt.timedelta(days=7), day + dt.timedelta(days=7))


@pytest.mark.parametrize(("before", "after"), [
    (D(2001, 9, 10), D(2001, 9, 17)),
    (D(2004, 6, 10), D(2004, 6, 14)),
    (D(2012, 10, 26), D(2012, 10, 31)),
    (D(2018, 12, 4), D(2018, 12, 6)),
    (D(2025, 1, 8), D(2025, 1, 10)),
])
def test_working_days_around_special_closures_are_trading_days(before: dt.date, after: dt.date) -> None:
    assert is_stock_trading_day(before) and is_stock_trading_day(after)


def test_days_around_2007_01_02() -> None:
    # 2007-01-02 的前一个工作日是元旦（周一），不是交易日；前一个交易日是 2006-12-29。
    assert not is_stock_trading_day(D(2007, 1, 1))
    assert is_stock_trading_day(D(2006, 12, 29)) and is_stock_trading_day(D(2007, 1, 3))
    assert stock_trading_days(D(2006, 12, 29), D(2007, 1, 3)) == [D(2006, 12, 29), D(2007, 1, 3)]
