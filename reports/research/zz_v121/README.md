# ZZ v1.2.1 B、DV 分侧研究复算说明

本目录是独立的开发期研究输出，使用 ZZ SPX 4%/5%、QQQ 5%/6.5% 的标签口径。不得与 `reports/research/` 原口径前瞻报告混写，也不得据此进行参数选择或声称实盘有效。

在仓库根目录运行：

```powershell
uv run market-risk research zz-v121-sides
```

输入为 `reports/research/wavewarn_v121/zz_events_development.csv`、`zz_unknown_development.csv`，正式回测的开发期分数，以及逐序列流式截断至 2016-12-30 的市场与 TradingView 数据。输出包括危险时段清单、SPX/QQQ 各侧候选日分组、逐时段与逐簇观测值、缺值与单值阶段明细、逐时段剔除 AUC、分侧统计和报告。所有 CSV 的日期不得越过 2016-12-30。

`B_DV分侧去留报告.md` 的“保留／移除”是规格第四节预登记判据的开发期机械结果，尚待负责人审核；配置中的 N 通道开关仍为空。
