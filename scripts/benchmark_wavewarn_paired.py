"""平稳自助法性能验收：仅用构造差序列，不读取真实研究样本。"""

from __future__ import annotations

import time
from decimal import Decimal

from market_risk.wavewarn.paired_test import paired_stationary_test


def main() -> None:
    """按五组固定设定分别计时；保持 Decimal 求和及既定随机抽样顺序。"""
    n, resamples = 1500, 10_000
    settings = (("主区块", 20, 20260929), ("短区块", 10, 20260910),
                ("长区块", 40, 20260940), ("复现主区块A", 20, 20260929),
                ("复现主区块B", 20, 20260929))
    differences = tuple(Decimal((index * 7) % 11 - 5) / Decimal(10_000)
                        for index in range(n))
    total_start = time.perf_counter()
    for name, block, seed in settings:
        started = time.perf_counter()
        result = paired_stationary_test(differences, block, resamples, seed)
        elapsed = time.perf_counter() - started
        print(f"{name}: n={n}, B={resamples}, b={block}, seed={seed}, "
              f"耗时={elapsed:.3f}秒, 总差={result.total_difference}")
    print(f"五组合计={time.perf_counter() - total_start:.3f}秒")


if __name__ == "__main__":
    main()
