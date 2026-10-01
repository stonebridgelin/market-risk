# v1.4 第二轮开发期诊断

本报告仅使用开发期数据；诊断不改变模型规则、参数、选定设定与γ。

只作描述，不参与任何判定。v1.4 已暂停作为当前产品候选（见 `docs/research/波段预警_v1.4_暂停与纠错登记.md`）。**本目录的结果只作纠错证据，不自动恢复候选资格。**

## 本目录（2026-10-01 纠错重跑）

在“标签 `v1.4-asrun` + `patches/f_BW.patch` + `patches/f_label.patch`”这一个版本上（代码树 `cb5625030c5124969a88c056b58f60a339df646a`），用原有命令 `wavewarn v14-diagnostics-round2` 按同一口径写出的切换诊断、转绿双层报告与完整净值。机械重跑重选出的设定与原选定设定相同（K=5、θ_P=2.5%），诊断对象仍是：选定的 v1.4、同参数的“v1.4 去掉 MR”、200 日均线。

**报告里一句过期的说明。** `诊断报告.md` 开头第四条写着“选定的 v1.4 与‘v1.4 去掉 MR’两个对象……其结果基于偏离规格的实现……尚未在修正实现下核验”。这句话是报告程序里的固定文字，写于修正之前；本批指令不允许改动两处修正以外的评价代码，所以没有改它。**对本目录的这一版输出，这句话不成立**：本目录就是在修正后的实现下写出的。是否改这句固定文字，等负责人决定。

与修正前相比哪些变了、哪些没变，见 `../correction_rerun/对比报告.md` 第六节（200 日均线、满仓、现金各行没有变化）。

文件：`诊断报告.md`、`objects.csv`、`switch_by_year.csv`、`switch_by_type.csv`、`switch_reversals.csv`、`holding_segments.csv`、`holding_summary.csv`、`green_summary.csv`、`green_events.csv`、`nav_metrics.csv`、`nav_daily.csv.gz`（gzip -n）、`output_hashes.json`（各文件的 SHA-256，由命令写出）。输入哈希与配置哈希见 `../correction_rerun/provenance.json`。

## superseded/

- `superseded/2026-10-01_修正前_v1.4-asrun/`：原实现（标签 `v1.4-asrun`）的输出，原样移入。被取代的原因：选定的 v1.4 与“v1.4 去掉 MR”两个对象基于偏离规格的 BW 退出谓词。其中的 `README.md` 是当时的原文，“本目录（当前有效）”指移入之前的位置。
- `superseded/2026-10-01_补全前/`：净值诊断补全之前的输出（同样基于原实现）。

## 复算

```
uv run market-risk wavewarn v14-diagnostics-round2
```

命令在输出目录已存在时拒绝覆盖；复算前先把本目录整体改名。`.md`、`.csv` 为 LF 换行，重新检出后哈希可能改变，以重新生成的文件为准。
