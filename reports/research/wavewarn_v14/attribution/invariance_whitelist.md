# f_label 不变性验收：运行元数据白名单

**写于运行之前**（2026-10-01；本文件的提交哈希与 SHA-256 记录在 `README.md`）。比对 B + f_label 与 B 的规范化研究结果时，只排除本白名单列出的内容；白名单以外的任何差异都视为不一致，停下报告。

## 比对的范围（规范化研究结果）

两个版本各自在独立工作目录里依次运行下列四条命令，再运行固定设定的汇总脚本，比对它们写出的**全部文件**，逐字节比较：

| 命令 | 输出目录（相对工作目录） |
| --- | --- |
| `wavewarn evaluate-v14-development` | `reports/research/wavewarn_v14/evaluation_development/`（不含 `extended_history/`） |
| `wavewarn v14-extended-history` | `reports/research/wavewarn_v14/evaluation_development/extended_history/`（唯一经过标签读回函数的命令） |
| `wavewarn v14-diagnostics-round2` | `reports/research/wavewarn_v14/diagnostics_round2/` |
| `wavewarn v14-extended-nav` | `reports/research/wavewarn_v14/extended_nav/` |
| `docs/audit/attribution/fixed_setting.py` | `fixed_setting.json`（固定设定 K=5、θ_P=2.5% 的各项指标与逐日执行灯色） |

文件集合本身也比对：两个版本写出的文件名必须完全相同。

## 白名单（比对时排除）

**文件内容：没有任何白名单项。** 上述命令写出的文件里不含时间戳、提交号、运行编号、耗时或绝对路径（写出代码里没有这些字段；逐日净值与逐日明细的压缩用 `gzip -n`，不含文件名与时间）。所以全部文件一律逐字节比较，不做任何字段级或行级的排除。

**不属于研究结果、不参加比对的内容**（逐项列出）：

1. 命令在控制台回显的文本：其中含输出目录的绝对路径（两个工作目录的路径不同），以及由文件内容派生的报告 SHA-256（内容相同则相同）。控制台回显不落盘、不参加比对。
2. 运行耗时：只在本批的操作记录里出现，不写入任何输出文件。
3. 各版本的元数据文件 `version.json`（由归因步骤的操作脚本写出，不是上述命令的输出）：字段 `name`、`worktree`（绝对路径）、`base_tag`、`base_commit`、`patches`（补丁文件名与 SHA-256）、`code_tree`（代码树哈希）、`python`（解释器路径）。这些字段按定义就随版本而不同。
4. Python 的字节码缓存目录 `__pycache__/` 与测试临时目录 `.pytest_tmp/`：不是命令输出。
5. 运行前为了让命令能够写出而从工作目录里删去的原有输出目录（即上表四个目录在标签 `v1.4-asrun` 中已入库的旧内容）：两个版本都删去，不参加比对。

## 判定

- 文件集合相同，且每个文件的 SHA-256 相同 → 一致。
- 任何一个文件不同、多出或缺少 → 不一致，停下报告，不进入机械重跑。
