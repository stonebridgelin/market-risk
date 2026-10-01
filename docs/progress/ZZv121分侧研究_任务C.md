# 任务 C：ZZ v1.2.1 B、DV 分侧研究

本批只用开发期，执行规格第四节预登记判据；输出是“预登记规则的开发期结果，待负责人审核”，不是历史预警效果、实盘有效性或参数选择。未运行验证期检验，未读取或输出保留期研究数据。N 的四侧通道开关仍留空。

## 实际改动

| 文件 | 目的 |
| --- | --- |
| `src/market_risk/research/zz_v121.py` | 读取独立 ZZ 标签及尾段未定清单，分 SPX/QQQ 侧复用原研究的分组、观测值、AUC 与一致性函数，应用预登记判据 |
| `src/market_risk/research/features.py`、`groups.py` | 允许 SPX/QQQ 分别选高位候选日；尾段危险归属未定日不作为样本或对照 |
| `src/market_risk/services.py`、`cli.py` | 暴露 `research zz-v121-sides` 命令 |
| `tests/test_research_zz_v121.py`、`tests/test_no_lookahead.py` | 构造测试、未来信息与研究包传递依赖隔离 |
| `reports/research/zz_v121/`、本文件 | 独立明细、报告和复算说明；未覆盖原口径报告 |

## 数据版本与复算

本批从基准代码提交 `ad3da07` 开始；本文件与任务 C 代码同一次提交，提交号以 `git log -1` 查看。正式回测为 `run_20260928T112013Z_2cc8969`，`data/market/manifest.json` SHA-256 为 `258184232AE1F93CFC57E28CC010E50121A31EC8F973C512F9858D42947C8649`。B2 同步修订后的 v1.2.1 规格副本 SHA-256 为 `E731D302C6E46FC3EBAE4EEEFF3CAFBE16263229E649CF8F7207AB8B118B370D`；外置平稳自助参考表 SHA-256 为 `A38A852DCDA19C23A01AD10AF60BF00F8F92927801C16B05AE5DFB02F4C72FFE`（本任务不运行该配对检验）。

复算命令：`uv run market-risk research zz-v121-sides`。本次实测输出 44 个 ZZ 危险时段；任一资产尾段未定的交易日为 12 天。主要文件 SHA-256（重跑后核验）：危险时段清单 `807E63B632CF3BC90E737A8E6220C3511EDF4E3FD5797B034F3E90DC2AAEB175`，分侧统计 `B582F9DA90C99C3B9F8B94D7BABB130D0D58A6FB686D750A8005461E48F9A7C0`，报告 `16159DFEAF9E30952ACEBE5A3AE2BFA89452328AE239ADA72B2732E5A9513C24`。

## 开发期机械结果与缺值

SPX 侧高位候选 853 日，样本 90 日／22 时段，对照 386 日／18 簇；QQQ 侧候选 826 日，样本 97 日／24 时段，对照 331 日／19 簇。两侧各有 12 个尾段未定候选日被排除。预登记判据机械输出：SPX B 移除、SPX DV 保留；QQQ B 保留、QQQ DV 保留。AUC 分别为 0.431818、0.356061、0.401316、0.339912；全部数值依据、区间、三项一致性和单值阶段剔除敏感性见独立报告及 CSV。SPX B 剔除单值阶段后方向反转，已如实列出，不据此更改预登记判据。

逐特征观测日：SPX ΔW20/ΔW10 各 476 日，QQQ ΔW20/ΔW10 各 428 日，均无缺值；涉及单值 K 线阶段分别为 111/102/109/100 日。这些是质量标记，不当作缺值。逐日原因、逐观测值有效日数已写入缺值审计和观测值 CSV。

## 检查与待办

`uv run market-risk research zz-v121-sides` 已完成；最终 `uv run pytest -q`：649 passed、2 deselected（63.20秒）；`uv run ruff check .`：通过；`uv run market-risk validate`：264/264 一致。固定样本 validate 涉及 2025 年的既有回归，按负责人确认的例外执行，仅报告总数，未将样本值用于研究。任务 C 的开发期去留结果需负责人审核后才能填写 N 通道开关；此审核不阻碍本研究代码与报告提交。本地提交后请负责人推送。
