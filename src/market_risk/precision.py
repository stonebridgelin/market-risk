"""原始数值的十进制读取与价格公布精度。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

PRICE_CENT = Decimal("0.01")


def decimal_value(value: str | int | float | Decimal) -> Decimal:
    """从十进制文本构造数值，不对已有 Decimal 再做转换。"""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def published_price(value: str | int | float | Decimal) -> Decimal:
    """收盘价按公布的两位小数读取，四舍五入。"""
    return decimal_value(value).quantize(PRICE_CENT, rounding=ROUND_HALF_UP)
