# 机械重跑的对比（2026-10-01）

本目录仅使用开发期数据。**重跑结果只作纠错证据，不自动恢复候选资格；v1.4 继续暂停。**

## 做了什么

只在“标签 `v1.4-asrun` + `patches/f_BW.patch` + `patches/f_label.patch`”这一个版本上（代码树 `cb5625030c5124969a88c056b58f60a339df646a`），用原有命令按原候选集合（九组）、原可行条件、原三级选择程序机械重跑并重选一次；收敛日、τ、j₀、参照行、事件纳入范围、主损失与择时得分、选择追踪都由命令完整重算，N′·X2、N·E2（与 P1·E2）中位设定随同一条命令同步重跑。对重选的设定按第二轮诊断的同一口径输出切换诊断、转绿双层报告与完整净值。

原实现一侧取标签 `v1.4-asrun` 本身（代码树 `5239b960d5af979db279c0565ed35306370c832b`）在另一个独立工作目录里的重新运行；它写出的开发期评价与标签里已入库的原始输出相同（见 `../attribution/归因报告.md`）。

## 文件

| 文件 | 内容 |
| --- | --- |
| `对比报告.md` | 逐项对比，没有变化的也写“无变化”：选定设定与各级候选数；21 组设定的 T、T价格、ē、非绿占比、切换次数、主损失与分项；转绿延迟与可行条件；收敛日、t0、τ、j₀、事件纳入范围；参照行（含另算的带缓冲带均线）；重选设定的第二轮诊断；事件账与警报账；输出文件逐个比较；来源与哈希 |
| `comparison_settings.csv` | 21 组设定 × `settings_summary.csv` 全部列的原值、新值与是否变化（完整精度） |
| `comparison_tables.csv` | 其余各表按主键逐格比较的全部差异；没有差异的表记一行“无变化” |
| `file_comparison.csv` | 两个版本写出的 38 个文件的 SHA-256 与比较 |
| `buffered_reference_original.json`、`buffered_reference_corrected.json` | 带缓冲带的 200 日均线参照行（另算），原实现与修正后各一份 |
| `provenance.json` | 两个版本的标签、提交、代码树哈希与补丁清单；输入哈希、配置哈希；修正后各输出文件的哈希 |

修正后的输出本身在 `../evaluation_development/`（说明见其中的 `纠错重跑说明.md`）与 `../diagnostics_round2/`；原输出在这两个目录各自的 `superseded/2026-10-01_修正前_v1.4-asrun/`。`../evaluation_development/extended_history/` 与 `../extended_nav/` 不含 BW，重新运行后全部文件与原来逐字节相同，留在原处。`../validation_rehearsal_development/`（开发期演练）只加了标注，没有重跑。

## 结论摘要

- 重选结果：K=5、θ_P=2.5%，与原选定设定相同；三条可行条件全部满足的候选仍是同样的 5 组，最终级别仍是第 1 级。
- t0、τ、j₀（2009-10-01、2009-12-31、2009-12-31）与事件纳入范围：无变化。v1.4 与“v1.4 去掉 MR”18 组的系统收敛日由 2009-10-21 或 2009-10-22 变为 2009-10-20；三个中位设定的收敛日无变化。
- 四条参照行与另算的带缓冲带均线参照行：无变化。
- 选定设定：T −0.232542 → −0.245690，T价格 −1.202542 → −1.240690，ē 0.546822 → 0.552781，非绿占比 0.478434 → 0.473326，计费切换 194 → 199 次；两资产的转绿延迟中位数（8、8）与三类件数无变化。
- 九组候选的 T 有升有降（之差在 −0.013148 至 +0.010699 之间），T价格 九组都下降，切换次数九组都增加（3 至 8 次）。
- 中位设定：P1·E2 无变化；N′·X2、N·E2 的 T 各变化 −0.007220，切换各增加 2 次。

以上只是修正前后的差异记录，不是对 v1.4 表现的评价。

## 复算

```
powershell -File docs\audit\attribution\build_version.ps1 -Name B
powershell -File docs\audit\attribution\build_version.ps1 -Name B_fBW_flabel -Patches f_BW.patch,f_label.patch
```

再在两个工作目录里各运行一次带缓冲带均线的参照行（`PYTHONPATH` 指向该工作目录的 `src`，当前目录为该工作目录）：

```
python <主仓库>\docs\audit\correction\buffered_reference.py <工作目录> <工作目录>\buffered_reference.json
```

最后在主仓库根目录生成对比：

```
uv run python docs/audit/correction/compare_rerun.py C:\Users\stone\PycharmProjects\market-risk-wt reports/research/wavewarn_v14/correction_rerun
```

`compare_rerun.py` 只读两个版本已经写出的文件，不导入项目代码。
