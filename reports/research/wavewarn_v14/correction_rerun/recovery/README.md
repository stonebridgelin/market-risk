# 归因版本的恢复材料（2026-10-01）

阶段二的单项与合并归因用了四个独立的 git 工作目录（B、B + f_BW、B + f_label、B + f_BW + f_label）。G2 收尾时这四个工作目录已删除；本目录保存重建它们所需的全部材料。只涉及开发期数据（截至 2016-12-30）。

## 保存了什么

| 文件 | 内容 |
| --- | --- |
| `versions.json` | 四个版本的基准标签与提交、补丁清单与补丁哈希、代码树哈希、相对标签改动的文件、固定设定汇总（`fixed_setting.json`）的输出哈希；运行环境（Python 版本、`uv.lock` 的哈希、汇总脚本的哈希） |
| `patches/f_BW.patch`、`patches/f_label.patch` | 两个补丁文件，与仓库根目录 `patches/` 下的逐字节相同 |
| `config/` | 五个配置文件，取自标签 `v1.4-asrun` 的仓库内字节 |
| `inputs_manifest.json` | 输入来源与版本清单：每个输入的路径、SHA-256、来源；配置的哈希 |
| `rebuild.ps1` | 重建并核对一个版本的脚本 |
| `verification_result.json` | 删除工作目录之前做的一次恢复验证的结果 |

输入只存清单，不存文件：除 NDTW 外的输入都在标签 `v1.4-asrun` 的代码树里。`data/processed/tradingview/NDTW.csv` 按项目设置不入库，清单另记它的原始导出文件 `data/manual/tradingview/raw/2026-09-26/INDEX_NDTW, 1D.csv` 的路径与哈希（原始导出已入库）。

四个版本各自写出的全部文件的哈希在 `../../attribution/versions/<版本>/output_manifest.json`。

## 如何重建

在主仓库根目录运行（`-WorktreeRoot` 取一个仓库之外的空目录）：

```
powershell -File reports\research\wavewarn_v14\correction_rerun\recovery\rebuild.ps1 -Name B_fBW -WorktreeRoot <临时目录>
```

脚本依次做：

1. 从标签 `v1.4-asrun` 检出独立工作目录（不做换行转换），套用本目录保存的补丁（先核对补丁哈希）。
2. 计算代码树哈希，与 `versions.json` 的记录比较。
3. 用本目录保存的配置覆盖工作目录里的配置（先核对哈希）。
4. 逐项核对输入的哈希。NDTW 取主仓库的文件；缺失或哈希不符时，用现有处理流程（`market-risk tv validate`，按清单重读已入库的原始导出并重建清洗结果）重新生成，再核对哈希。
5. 用该版本的源码运行 `docs/audit/attribution/fixed_setting.py`，重算固定设定（K=5、θ_P=2.5%）的汇总。
6. 把 `fixed_setting.json` 的哈希与记录比较。

任何一项不符即报错停下，不删除任何东西。要得到该版本的全部输出（开发期评价、补充历史、第二轮诊断、补充历史净值），改用 `docs/audit/attribution/build_version.ps1`（做法见 `../../attribution/README.md`），再与 `output_manifest.json` 比较。

用完后清理：`git worktree remove --force <临时目录>\<版本名>`。

## 删除之前的验证

2026-10-01，在一个新的临时工作目录里按上面的命令重建了 B + f_BW：代码树哈希 `c88614a503c73f33f94da3e08b54ecbe4747716d` 与固定设定汇总的哈希 `2A1969DDE3CA4A5529B56EEEC3F2996B68B05AA0906ECA96AE4D934B03CF158E` 都与记录一致（`verification_result.json`）。验证通过后才删除了原来的四个工作目录与这个临时工作目录。

这次验证没有走到“由原始导出重新生成 NDTW”的分支（主仓库那份的哈希与清单一致）。

**补充验证（2026-10-01，合并到 main 之前）。** 按负责人的要求另做了一次：把已入库的原始导出 `data/manual/tradingview/raw/2026-09-26/INDEX_NDTW, 1D.csv` 与导入清单复制到仓库之外的临时目录，用现有处理流程（`market_risk.data.tradingview.rebuild`，即 `market-risk tv validate` 调用的同一个函数，存储根目录指向该临时目录）重新生成清洗结果。重新生成的 `NDTW.csv` 的 SHA-256 为 `691CEBFC92F368E2C5CC8A80E1A79BB0820EE84641BC13FDF4B1A6C7335DB4C3`，与清单一致。比对后删除了临时目录；仓库中的文件没有改动。`rebuild.ps1` 里“哈希不符时在主仓库里运行 `tv validate`”这一分支本身仍然没有被触发过。

## 限制

- 固定设定汇总由 Python 文本模式写出，在 Windows 上换行为 CRLF；记录的哈希对应这种字节。换平台重算时内容相同而哈希不同。
- 重算用的是主仓库当前的虚拟环境；`versions.json` 记录了当时的 Python 版本与 `uv.lock` 的哈希，依赖版本变化后数值末位可能不同。
