# 单项与合并归因

本目录仅使用开发期数据。结果只作纠错证据，不自动恢复候选资格；v1.4 继续暂停。

## 做法

- B 为标签 `v1.4-asrun`（提交 `a0a8ec0`，BW 谓词与标签读回都是原实现）。
- 四个版本各用一个独立的 git 工作目录，从 B 检出，只打对应的补丁，不使用主仓库连续提交后的累计状态：B、B + f_BW、B + f_label、B + f_BW + f_label。构造与运行的脚本是 `docs/audit/attribution/build_version.ps1`。
- 每个版本用自己的源码依次运行 `evaluate-v14-development`、`v14-extended-history`、`v14-diagnostics-round2`、`v14-extended-nav`，再运行 `docs/audit/attribution/fixed_setting.py` 汇总固定设定（K=5、θ_P=2.5%，取自配置里的锁定设定，不重选）的各项指标。
- `docs/audit/attribution/compare_versions.py` 只比较各版本已经写出的文件（不导入项目代码、不重新计算），生成本目录的 `归因报告.md`、`attribution.csv` 与 `invariance_result.json`。

## 文件

| 文件 | 内容 |
| --- | --- |
| `invariance_whitelist.md` | f_label 不变性验收的比对范围与白名单。**写于运行之前**：随提交 `5db52cb` 入库，早于任何版本的运行；SHA-256 `7FB3A761EB0750619CB6E8BADE9FA0AE84C8093F9B204D86256E42740EB95753`（LF 换行的字节） |
| `归因报告.md` | 版本、补丁与代码树哈希；不变性验收结果；各项指标的单项影响、合并影响与非加和影响 |
| `attribution.csv` | 上表的完整精度 |
| `invariance_result.json` | 三项比对的差异清单（全部为空即一致） |
| `versions/<版本>/fixed_setting.json` | 该版本固定设定的各项指标与逐日执行灯色 |
| `versions/<版本>/version.json` | 补丁清单与哈希、代码树哈希、相对 B 改动的文件、NDTW 输入的哈希 |
| `versions/<版本>/output_manifest.json` | 该版本写出的全部 39 个文件的 SHA-256 |

## 补丁与代码树

| 版本 | 补丁 | 代码树哈希 |
| --- | --- | --- |
| B | 无 | `5239b960d5af979db279c0565ed35306370c832b` |
| B + f_BW | `patches/f_BW.patch` | `c88614a503c73f33f94da3e08b54ecbe4747716d` |
| B + f_label | `patches/f_label.patch` | `11bd2e64a66ea73b31a343e930afee5be9291be6` |
| B + f_BW + f_label | `patches/f_BW.patch`、`patches/f_label.patch` | `cb5625030c5124969a88c056b58f60a339df646a` |

补丁的 SHA-256：`f_BW.patch` 为 `0A9479418504AEF3776EF5D770D93D139403A90B3AFFEA3FE0C7B65186A8CFE9`，`f_label.patch` 为 `5DF3D7BC4AF8214CBD9587561EB16917996390D54AB6E2136FCA2D874F5471FA`。代码树哈希是套用补丁之后、运行之前整个工作目录的 git 树（`git write-tree`，不做换行转换）；B 的代码树就是标签的树。

输入：除 `data/processed/tradingview/NDTW.csv` 外都在标签的代码树里。NDTW 由 TradingView 原始导出整理而成，按项目设置不入库；四个版本用的是主仓库里的同一份文件（SHA-256 `691CEBFC92F368E2C5CC8A80E1A79BB0820EE84641BC13FDF4B1A6C7335DB4C3`）。

## 结论摘要

- **f_label 的不变性验收：一致。** B + f_label 与 B 写出的 39 个文件，文件集合与逐文件 SHA-256 完全相同；白名单对文件内容没有任何排除项。B + f_BW + f_label 与 B + f_BW 同样完全相同。
- B 重新写出的开发期评价与标签里已入库的原始输出相同（换行统一后比较；入库 README 多一行压缩后手工追加的哈希说明）。
- f_BW 的单项影响（固定设定）：执行灯色不同的区间 19 个；T 由 −0.232542 变为 −0.245690，T价格 由 −1.202542 变为 −1.240690；计费切换 194 → 199 次；ē 0.546822 → 0.552781；净值累计收益 18.55% → 20.35%，全程最大回撤 26.06% → 26.42%；两资产的转绿延迟中位数与三类件数不变。
- f_label 的单项影响：全部为 0。合并影响等于 f_BW 的单项影响，非加和影响全部为 0。非加和影响只说明效果不能简单相加，不识别两两交互，不是因果证明。

## 复算

```
powershell -File docs\audit\attribution\build_version.ps1 -Name B
powershell -File docs\audit\attribution\build_version.ps1 -Name B_fBW -Patches f_BW.patch
powershell -File docs\audit\attribution\build_version.ps1 -Name B_flabel -Patches f_label.patch
powershell -File docs\audit\attribution\build_version.ps1 -Name B_fBW_flabel -Patches f_BW.patch,f_label.patch
uv run python docs/audit/attribution/compare_versions.py C:\Users\stone\PycharmProjects\market-risk-wt reports/research/wavewarn_v14/attribution
```

每个版本约 100 秒。工作目录在仓库之外（`C:\Users\stone\PycharmProjects\market-risk-wt\`），已存在时脚本拒绝覆盖；用完后可用 `git worktree remove` 清理。
