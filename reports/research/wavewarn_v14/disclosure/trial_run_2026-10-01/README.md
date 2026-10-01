# 试跑材料存档（2026-10-01）

**补充指令到达前的未核对试跑，仅作信息披露证据，不是正式性能证据，不得引用其中的数值。**

## 经过

2026-10-01，一致性审计只发现 BW 退出谓词一处会改变输出的偏差。按当时指令的条件分支，执行者（Claude）开始了“BW 修正与机械重跑”：在工作区里改了 BW 退出谓词，并在仓库之外的会话临时目录里试跑了一次 v1.4 开发期评价（只读开发期输入，截至 2016-12-30）。随后收到负责人的补充指令“审计后停下，不进入第三步”，工作区的改动全部撤回，没有提交；临时目录里的试跑输出没有删除。本目录把这些材料原样存档。

正式结果以从标签 `v1.4-asrun` 出发、只打登记的补丁构造的版本重新运行为准（见《暂停与纠错登记》补充裁决第 13 条）。

## 执行者当时看到了什么

- 试跑脚本的控制台回显一行：t0、τ、j₀、选定设定的键、所在级别、三级候选数。
- `selection_trace.csv` 全文（含选定设定的 T）。
- `event_scope_counts.csv` 全文。
- `settings_summary.csv`、`convergence.csv`、`reference_rows.csv` 的表头与第一行数据（第一行是 v1.4 K=3、θ_P=1.5% 一组与“始终绿”参照行）。

除此之外没有打开其他文件（报告正文、逐日明细、事件账、警报账、其余各组的汇总行都没有看）。

## 试跑所用的修正与本批正式修正的差别

试跑时的写法是“广度条件连续 3 日”与“当日价格条件”直接取合取，没有按《暂停与纠错登记》补充裁决第 7 条处理缺值（BW 所需任一输入缺失的一天使连续日计数清零）。开发期自 t0 起六个输入都没有缺值，两种写法在开发期输入上给出相同的谓词；但试跑代码不是本批登记的补丁 `patches/f_BW.patch`，不能互相替代。

## 保留了什么、没有保留什么

保留（全部来自会话临时目录，内容未改）：

| 文件 | 说明 | 字节数 | SHA-256 |
| --- | --- | --- | --- |
| `code/step3_tracked.patch` | 撤回前工作区相对 `0efc8c1` 的全部改动（`git diff`）：BW 谓词修正、审计案例去掉预期失败标记、对比命令的入口 | 7469 | `E30C1DA8471077955D6A4F189822C84DE1EAC92002A78E56516CBBBECC0FA3D8` |
| `code/bw_comparison.py.txt` | 当时写了一半的对比工具（未入库、未运行过） | 10954 | `949D1AA2BE228CE57A2DDBE42F0E2E83ED94148D597D998C55A930B1C39CBC7C` |
| `code/bw_comparison_run.py.txt` | 同上，读写部分（未入库、未运行过） | 8688 | `7D2D90FD90F363E446A7FC10796E5540F432E3A9E09DBEB0BD3EEAD3432BDCEB` |
| `code/try_bwfix.py.txt` | 试跑脚本（调用项目的开发期评价写出函数，输出到临时目录） | 660 | `B591FACE58DC5DB8607F6F134B694AD920890A8EF9A58622D3A2F1FEBD443EE3` |
| `output/settings_summary.csv` | 试跑输出：21 组设定的汇总 | 16171 | `D957CA7C25A73E77A371D14A1104EF87CA324157431CE409D05D3871EA6467E9` |
| `output/selection_trace.csv` | 试跑输出：选择追踪 | 441 | `DE796134C366EF5C34AEEF91742CEE6F2471D33FEA70BC8794B6C7D7822EA1CB` |
| `output/reference_rows.csv` | 试跑输出：四条参照行 | 969 | `8F33F7A78778BDA504165D602B24FBF096A3A47381F14D5DECCD5234323CF84E` |
| `output/convergence.csv` | 试跑输出：收敛日与 t0、τ、j₀ | 1453 | `73C2A692DAF7973B746E509FBEA1674405293B8C5C3B65771AF5F907BB3CBF7F` |
| `output/event_scope_counts.csv` | 试跑输出：事件纳入件数 | 209 | `FB936317B8B1376E2C9024E9D156E2E71257F69281AC67F2832C08C83B5EA2A8` |
| `output/missing_audit.csv` | 试跑输出：缺值审计 | 4427 | `6AB8E746199AE5936936EF39C1DB8034E3FC418239647803BC7AFCB6E8B33BBB` |
| `output/daily_selected.csv.gz` | 试跑输出：选定设定与参照行的逐日明细（存档时用 `gzip -n` 压缩；压缩前 3,953,642 字节，SHA-256 `BA82944E649BA4B5E6938280B13D1DDD317BF68B5004F11A9F9516B43A213902`） | 820535 | `7488317FE7F476AD1209A373FC72BE038F30EE1A87658662C0BD7F986CFB8CDF` |
| `output/event_ledger_selected.csv` | 试跑输出：事件账 | 50590 | `23D162F66A3B46E6D41F3030E29082160915114618C64C3E43575BEFA3C15BF8` |
| `output/event_class_summary_selected.csv` | 试跑输出：事件账分类汇总 | 337 | `0CC68979D60C08C736FAD5B545B4187BD0E3E8381AFD5516C3EFEBD7509AFBD4` |
| `output/alert_ledger_selected.csv` | 试跑输出：警报账 | 10238 | `F1594CEF30A36F44B2E807B7DED486946A7FFB37F41EF3615E0B41734F4CC20C` |
| `output/alert_summary_selected.csv` | 试跑输出：警报账汇总 | 276 | `410A4199FB3AE3E7353026DD58BF5E9592613E6C8C16509D0DBE665254E80BA8` |
| `output/yearly_alert_selected.csv` | 试跑输出：逐年警报 | 1126 | `96605207471532495FAA5869FECD3521DDE72B707CBE7BB0B1065FD0CCBEFAAD` |
| `output/开发期评价报告.md` | 试跑输出：报告 | 12756 | `57141B5B81405606752081B71E05302464B81FB120AE54F611C02A03B1EEF626` |
| `output/README.md` | 试跑输出：输出说明 | 2266 | `4BB6CA17A94F2676F1AA82AE430B3A55492D156BEDD47BB7E50CC038B27525FC` |

三个代码文件存档时加了 `.txt` 后缀（内容不变），以免被当作项目源码检查。

没有保留：

- 试跑时的控制台回显没有另存为文件，只留在会话记录里；其内容就是上面“看到了什么”的第一条。
- 当时在临时目录里跑的两段探查脚本（四层统计、BW 分歧天数）属于审计，其结果已写入审计报告，不在本存档内。
- 对比工具从未运行，没有任何对比输出。
