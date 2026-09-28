# 独立复核工具

`audit_indep.py` 只读取 `data/market/` 原始 CSV 和 `results/MARKET/risk_scoring/backtests/official.json` 指向的正式回测文件，以独立实现重算分数、中间值、回调事件及结果标签，不导入 `market_risk` 的任何模块。它用于发现正式计算路径与独立实现之间的不一致，不替代人工核对的回归期望值。

在项目根目录运行：

```powershell
uv run python docs/audit/独立复核/audit_indep.py . docs/audit/独立复核
```

输出为同目录的 `score_compare.txt`、`sample_compare.md`、`zigzag_compare.md`、`label_compare.txt`。每次修改评分、指标、结果标签或 ZigZag 相关代码之后都必须重跑，并检查上述文件的差异。正式回测指针应先指向待复核的运行目录。
