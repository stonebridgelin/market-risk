# 波段预警 v2.0 独立回算脚本（甲层 N1、乙层 N2）

编写：独立回算脚本会话（与原开发会话分开）。规格：《阶段四M2第二部分实施指令_修订六》第四节、第三节 N1—N4 四行、第六节第 1 小节、第七节（产品经理摘录，材料包 03）；设计稿修订二第八节第 2—3 小节、第九节、第十一节（材料包 02）；登记规格 v2.0（材料包 01）；结果目录格式说明与样本表头（材料包 04、05）。负责人裁决：R1 甲、R3、R8 甲（2026-10-05）。第二次交付另依据《答复单（定稿）：独立回算脚本会话 README 第七节 19 项与第二次交付》（SHA-256 `225177bc8d92fbc3a7e5859cce5d20fcae94c60c8f170883869b22c2d7ba0e73`；第三次交付起以负责人批准的《答复单（定稿修订一）》`fd72965b3a211b50b4e9d57c6a00e486af8cf0b7262f901d96a6b06de1009668` 为准，批准原文 `f9df40aeb3908131e4bc7193d0d29f2c4e3ffffa4ab4643d1b0628f6d2b9b1f2`，落实指令《第三次交付指令》`708bb5dc9b3804199a961f41896b839fc52a1ad7233365830142d4487a00716f`）与《指令修订六勘误及权限补充单（定稿）》（SHA-256 `6de1da0ead937fda9ffb4a9fe2932aaf8c86e4e761c361cb59fa269ad26420a3`），负责人转发即批准。

本会话**没有**看到项目源码（`src/market_risk/`、第一部分测试、独立复核工具），也没有读取任何真实或构造行情、任何演习产物。材料 03 中的 D23—D44 行号只作“规则来源”标注。会话的实际信息暴露范围另见交付目录 `记录\访问材料.txt`。

| 文件 | 作用 |
| --- | --- |
| `recompute_a.py`（N1） | 甲层：由结果目录的逐日输出重算汇总，与项目汇总逐项比对 |
| `recompute_b.py`（N2） | 乙层：由 `input_snapshot` 的价格独立生成 U、组合收益与净值，与项目逐区间比对；恒定仓位、结构 B 已知前缀、市场环境与暴露替换算术 |
| `README.md`（N3） | 本文件 |
| `tests/test_v20_recompute.py`（N4） | 两层的独立构造验收（纯函数小样例、程序化生成的完整 27 组目录 + CLI、故障注入与写入中断、AST 检查） |

## 一、用法、输出与退出码

```
python recompute_a.py --result-dir <evaluation_development 目录> --out <新报告.json>
python recompute_b.py --result-dir <evaluation_development 目录> --out <新报告.json>
```

- 只用 Python 标准库（3.12 验证）；不导入 `market_risk`、`numpy`、`pandas`、`pandas_market_calendars`、`yaml`。N1、N2 直接导入白名单：`argparse`、`csv`、`gzip`、`json`、`math`、`hashlib`、`pathlib`、`dataclasses`、`datetime`、`decimal`、`bisect`、`typing`、`sys`、`os`、`tempfile`（N4 以 AST 检查）。N4 直接导入：`ast`、`csv`、`gzip`、`importlib.util`、`json`、`math`、`subprocess`、`pathlib`、`pytest`，以及 `sys`（只取 `sys.executable`，勘误补充单 K2）与 `hashlib`（N4 直接导入 `hashlib`，经负责人裁决 P3 加入 N4 白名单，用途限定为构造夹具 `MANIFEST.sha256` 行的 SHA-256）；N4 的 AST 检查同时核对其自身导入属于 N4 白名单、`hashlib` 只在 `sha256_hex` 中使用。
- 只读结果目录内第四节第 2 小节允许的文件（见第二节）；`--out` 已存在即退出码 2 且不写；报告先写同目录临时文件再改名，改名失败或写入中断时删除临时文件、退出码 3，并在标准错误列明“已写出 k/N 字节、临时文件是否删除、目标文件未生成”。文件写入只出现在 `write_report` 函数内（N4 以 AST 检查）。
- 报告 JSON：`layer`、`inputs`（每个输入文件的字节数与 SHA-256）、结构（`structure`）、`usage_restriction`（JSON 字符串数组，照录全部元素与原顺序）、`cutoff`、对象集合、逐项比对 `items`（`item`、`object`、`position`、`script`、`project`、`difference`、`status`）、`summary`（项数、不一致数、计算失败数、未核对或照录数）、`first_mismatch`（首个不一致或计算失败）、`exit_code`、`exit_code_meanings`、`error`（输入错误或计算失败时的种类、说明、定位）。甲层另含 `ledger_counts`（段账计数，只写入报告不比对）；乙层另含 `known`、`days`、`interval_u`、`environments`。
- `status` 取值：`一致`、`一致（两者均为空）`、`不一致：…`、`计算失败：…`、`未核对：…`、`照录：…`、`保留：主参照失败原因未独立核验`（只用于 `selection.json` 出口为“计算失败”的主参照分支，见第七节第 7 项；不计入一致、不计入不一致，在 `summary.reserved` 单列）。`未核对`、`照录` 不计入不一致；按答复单，第二次交付后只剩两处：`state_statistics[*].start_note` 文字照录（答复单第 6 项），以及首个信号日为二级时“依赖窗口外状态，未独立核对”的标注（第四节第 3 小节与矩阵 `test_a_state_statistics_first_day_dependency` 要求）。
- 退出码：0 全部一致；1 存在不一致；2 参数或输入错误（含表头不符、行数或日期不符、表外取值、结构不在已定义集合内、结构判定字段非法）；3 计算失败、非有限值或报告写入失败。

## 二、两层的输入（三类划分）

| 类 | 甲层（N1） | 乙层（N2） |
| --- | --- | --- |
| 独立重建用的输入 | `daily_targets`、`daily_nav`、`daily_signals`（只用于四项状态统计）、`r2_judgements`、`segment_ledgers`、`daily_policy`（只用于政策差额与结构 B 前缀核对） | `input_snapshot`；`daily_targets`（`date,object,core,leverage,position`）；仅结构 B 下 `daily_policy`（`date,object,core,leverage`；`wealth` 只作比对值） |
| 元数据输入 | `window.json`（`first_day`、`last_day`、`n`、`j0`、`e_index`；`e_index` 只照录）；`run_record.json`（只取 `cutoff`、`usage_restriction`（字符串数组，照录）与 `window_coverage`）。**`window_coverage` 为修正批第一道门新增读取（项目发起人批准，2026-10-06）**：只读 `condition`、`first_day`、`satisfied`、`status` 四键（须恰为四键，缺失或键多、少为退出码 2）；允许用途只作比对对象——（a）`condition` 逐字等于 `axis[j0] <= 2000-03-24`；（b）`first_day` 等于 `window.json` 的 `first_day`；（c）`status` 等于“已判定”；（d）`satisfied` 为 JSON 布尔且等于以 `window.json` 的 `first_day` 与冻结常量 2000-03-24 独立判定的结果（不晚于为真）；（e）`usage_restriction` 含开发窗口覆盖限制文字（逐字）当且仅当独立判定为不满足（实现以独立判定结果为准，与 (d) 一致时即项目 `satisfied` 为假）；禁止用途：不作任何计算输入或结构判定依据，不据其改变、跳过或放宽其他核验；核验范围：只对完整结果目录，既有输入要求不满足时照既有规则停止；`descriptive.json` 四个结构判定字段 `averages[*].name/evaluable`、`constants[*].object/computed`；脚本内冻结登记常量（第六节） | `window.json`（`first_day`、`last_day`、`n`、`j0`）；`run_record.json`（只取 `cutoff` 与 `usage_restriction`（字符串数组，照录））；`MANIFEST.sha256`（**本次补充批准**，勘误补充单 K4：只取文件名为 `input_snapshot.csv.gz` 的唯一一行，与快照实际 SHA-256、字节数核对；不读清单所指其他文件，不作计算输入）；同左四个结构判定字段；冻结登记常量 |
| 比对对象（不作计算输入） | `selection.json`、`candidates_summary.csv`、`reference_summary.csv`、`segments.csv`、`reconciliation.json`、`descriptive.json`（`state_statistics`、`policy_minus_signal`、`averages[*].switches/signal/policy`（不可评价者须为 null）、`averages[*].signal.log_wealth`、`constants[*].nav`（未计算者须为 null）的 `log_wealth/max_drawdown`） | `daily_nav`（信号模拟、一直持有、恒定仓位）、结构 B 下 `daily_policy.wealth`（前 known 行）、`candidates_summary.signal_log_wealth`、`reference_summary.signal_log_wealth`、`descriptive.constants[*].core/leverage/exposure/nav.log_wealth`、`descriptive.environment_summary`、`environments.csv`、`exposure_substitution.csv` |
| 禁止读取 | `input_snapshot`、`r2_events`、`diagnostics`、`report.md`、`audit_log.jsonl`、`information_state.json`、`run_environment.json`、`MANIFEST.sha256` | `daily_signals`、`r2_*`、`segment_ledgers`、`diagnostics`、`selection.json`、`information_state.json`、`audit_log.jsonl`；结构 A 下 `daily_policy`（脚本先由快照与 `daily_nav` 判定结构，只有结构 B 才读取该文件） |

两层的表头一律与第四节第 2 小节、材料 04/05 的表头原文逐字核对，不符即退出码 2。

甲层 R2 比对另按第七节第 23 项（项目负责人 2026-10-06 裁决 F-1）处理 R2 事件不可得的资产：既有全部结构检查照旧先执行；随后对每个资产 a（SPX、QQQ），进入特殊分支的唯一条件是 `candidates_summary.csv` 27 行中 a 的五列（`<a>_r2_computable`、`<a>_r2_denominator`、`<a>_r2_achieved`、`<a>_r2_new_only`、`<a>_r2_meets`）全部为空，且同时满足（i）`r2_judgements` 中 a 无任何行，（ii）`run_record.json` 的 `usage_restriction` 数组含前缀诊断限制文字“前缀诊断运行（严格读取未通过：缺少必需价格）”（逐字），（iii）`selection.json` 27 条 `records[*].r2[a]` 均为 null。满足时这 27 × 5 = 135 项状态写为“照录：R2 事件不可得（项目表示；原因未独立核对）”，计入 `summary.unchecked_or_recorded`，不计一致、不一致或计算失败，不表示独立核验通过；N1 对 a 的 computable 视为假（脚本由空判定行算得不可计算、meets 为空），出口判定规则、其他字段核验、汇总逻辑与出口优先级不变；报告 `r2_unavailable_assets` 列出进入特殊分支的资产。其他一切情况（任一行 a 任一列非空，或五列全空但任一前提不满足）按既有规则逐字段比对；合法零分母 false/0/0/0/空照旧一致。本规则不新增读取材料或读取权限（所用字段均为既已获准读取），N2 不变。接受的限制：满足三项前提时，N1 不能区分“事件不可得”与“零判定行”，也不核对不可得的原因，由 M4 工具比对承接。

## 三、对象集合记号、合法组合表与计数公式

| 记号 | 定义 | 来源 |
| --- | --- | --- |
| C | 27 个候选键（登记顺序，`order` 0—26） | 冻结登记 |
| R、H | `主参照`、`一直持有` | 冻结登记（第四节第 2 小节） |
| E | 可评价的均线对照 ⊆ {`200 日均线一级版`, `带缓冲带的 200 日均线二级版（101%/99%）`}，按 `descriptive.averages[*].evaluable` 为真取；`averages` 须恰两项、`name` 恰为两名各一次；count(E) ∈ {0,1,2} | 结构判定字段 |
| K | 已计算的恒定仓位 ⊆ {`恒定仓位：主参照`, `恒定仓位：<selected>`}：`computed` 为真**且**身份与条件核对通过者。甲层：`constants[0].object` 须为 `主参照`；出口为“选定”时 `constants[1].object` 须等于 `selection.selected`，否则须为 `选定候选` 且 `computed` 为假；结构 B 下 `computed` 为真即不一致。身份或条件不符记不一致且不计入 K（因此其逐日行按“不在合法组合表内”处理）。乙层不读 `selection.json`。**身份核验分工（答复单第 14 项订正第三节第 5 条）**：甲层依 `selection.json` 的出口核对恒定仓位对象是实际选定候选还是占位对象 `选定候选`；乙层只核对对象属于允许集合（`选定候选`、`主参照`、候选键）与结构条件，乙层的“候选键合法”不表示已独立证明该候选为选定候选 | 结构判定字段 |
| P | 评价窗口价格齐全：结构 A 为真、结构 B 为假 | 甲层由 `r1_computable` 与 `daily_nav` 判定；乙层由快照判定 |
| O_d | 有逐日信号/目标/政策行的对象 = C ∪ {R} ∪ E；count(O_d) = 28 + count(E) | — |

| `path` | 合法对象 | 存在条件 | 每对象行数 |
| --- | --- | --- | --- |
| `信号模拟` | O_d | P 为真 | n + 1 |
| `执行政策研究模拟` | O_d | P 为真 | n + 1 |
| `一直持有` | 只有 H | P 为真 | n + 1 |
| `恒定仓位` | K | `computed` 为真（蕴含 P 为真） | n + 1 |

`daily_nav` 路径总数 = P × (2·count(O_d) + 1) + count(K)；`daily_signals`、`daily_targets` 对象数 = count(O_d)，各 n + 1 行；`reconciliation.rows` 单一路径行数 = P × (2·count(O_d) + 1) + count(K)，相对主参照 = P × 27；`reference_summary.csv` 恰 4 行。表外组合记“不一致：对象或路径不在合法组合表内”；合法组合缺行为退出码 2。

**结构判定（甲层）**：A ⇔ `candidates_summary.r1_computable` 27 行全为真且 `daily_nav` 有 `一直持有` 路径；B ⇔ 全为假且 `daily_nav` 无数据行；其他组合退出码 2“结构不在已定义集合内”，报告列出各候选 `r1_computable` 与 `daily_nav` 路径集合。`usage_restriction` 只照录，不参与判定。

**结构判定（乙层）**：由快照独立求 `known` = 评价窗口 `days[0..n]` 中首个任一资产缺价的下标（无缺价为“无”）。无缺价而项目无净值行 → “不一致：结构与快照不符”（退出码 1）；有缺价而 `daily_nav` 有任何数据行 → 退出码 2“结构不在已定义集合内”（矩阵 `test_b_structure_b_rejects_nav_rows`）。`known` 不从项目净值行数反推。

## 四、算法依据（逐条标“登记来源 / 实现来源”）

“登记来源”指由登记规格（材料 01）直接推出；“实现来源”指只来自材料 03 对源码的规格化文字，其与登记是否一致由独立复核者另行核对，**不由回算证明**。

### 甲层（N1）

| 项 | 规则 | 来源 |
| --- | --- | --- |
| 执行日 | `days` 取主参照 `daily_targets` 的日期，须无重复、共 `n + 1` 个、首末等于 `window.first_day/last_day`；其余对象与路径的日期集合须恰为 `days`；信号日 = [s0] + `days[0..n−1]`，s0 须早于 `days[0]` | 实现来源（第四节第 2 小节“缺行识别依据”）；甲层不能证明 `days` 是交易日历上的连续交易日 |
| 净值链 | `W_0 = 1`，`W_i = W_{i−1}·(1 + return_to_date_i)`；首行 `wealth` 须恰为 1.0、首行 `return_to_date` 须为空 | 登记来源（第四节第 4 小节起点记 1） |
| 对账（单一路径） | Σ`log1p(r)`（`math.fsum`）、独立 `ln W_n`、项目 `ln wealth_末` 两两之差 ≤ 1e-10；`difference = abs(fsum(log1p(r)) − ln wealth_末)` 与项目值之差 ≤ 1e-12；`tolerance` 须为 1e-10；`passed` 一致 | 1e-10 为登记来源（第五节第 1 小节）；difference 定义为实现来源 |
| 对账（相对主参照） | 执行日序列相同；`abs(fsum(log1p(r_c) − log1p(r_ref)) − (ln W_c,末 − ln W_ref,末))` ≤ 1e-10，且与项目值之差 ≤ 1e-12（W 取项目末行） | 实现来源 |
| 最大回撤 | `max_t (1 − W_t ÷ max_{s≤t} W_s)`，含起点；序列须非空、为正的有限数 | 实现来源 |
| R1 | `mdd_signal ≤ 0.5 × mdd_hold`；任一净值不可计算则不可计算；两者均 0 → 满足；hold 为 0 而 signal > 0 → 不满足 | 不等式与比例为登记来源（第五节第 1 小节）；零回撤处理为实现来源 |
| R2 | 分母 = 事件数 − 左截断（含输入不足）；达标 = 持续覆盖达标 + 新提示达标；`5·达标 ≥ 3·分母`（整数比较）；分母 0 → 不可计算、`0/0/0/空`；类别全集 7 个；R2 事件不可得的资产按第七节第 23 项（裁决 F-1）照录 | 分子分母、60% 与“输入不足计入分母”为登记来源（第五节、第八节第 5 小节 G3b）；整数比较与类别文字为实现来源 |
| 切换与幅度 | `position` 与前一执行日不同计一次，首行不计；`e = core + 2·leverage`；幅度 Σ\|Δe\|（`fsum`），方向和 ΣΔe | 计数口径为登记来源（第一节第 7 小节、第四节第 4 小节）；幅度取绝对值为实现来源 |
| 选择 | 出口优先级 计算失败 > 缺值无法评价 > 无合格候选 > 选定；可行 ⇔ R1 且两资产 R2；M 为可行集 ln W 最大值；并列 `ln W ≥ M − 1e-10`；选 (切换次数, order) 最小 | 登记来源（第五节第 1、2 小节）；`failed` 取项目 `records[*].failed`（甲层不能独立判定计算失败） |
| 四项状态统计 | 直接进入二级 = 前一信号日正常且当日二级；经一级再入二级 = 前一信号日一级且当日二级（自第 2 个信号日起精确重算）；首个信号日为二级时，按 `descriptive.state_statistics[*].direct_level2_days/via_level1_days` 是否含该日判定项目所取前一日状态并标注“依赖窗口外状态，未独立核对”；一级/二级执行区间数 = `targets[0..n−1]` 中连续相同 position 的一级、二级段数，段起点 0 记左截断、终点 n 记右截断 | 直接进入二级的定义为登记来源（第一节第 4 小节）；段的统计口径为实现来源 |
| 分段回撤比 | 分段四项（第五节常量）；`i1 = max(bisect_right(days, start), 1)`、`i2 = bisect_right(days, end) − 1`；`i2 < i1` → 无收益区间；否则 a = i1 − 1、b = i2；信号或一直持有不可计算 → 不可计算；`start ≥ days[0]` 且 `end ≤ days[n]` → 完整覆盖，否则部分覆盖；比值 = MDD(signal[a..b]) ÷ MDD(hold[a..b])，分母 0 → 比值空、`undefined` 真 | 分段边界为登记来源（第五节“R1 的报告边界”）；映射与比值为实现来源 |
| 政策差额 | `ln(政策末净值) − ln(信号末净值)`（取 `daily_nav` 两路径末行；政策末行须与 `daily_policy.wealth` 末行相等）；结构 B 为空 | 实现来源 |
| 段账计数 | 每对象每资产各类别件数与 `pre_window` 为真件数，类别全集 6 个，只写入报告 | 实现来源 |
| 第二次交付：输出契约逐项核验 | 按答复单第 3—8、14 项：一直持有行 `switches` 0、`exposure_magnitude` 0.0；`segments.csv` 四种覆盖分支下各列取值（“无收益区间”与“不可计算”时 `undefined` 为 False）；`truncated` 元素 `[position, days[s], days[e], 左截断, 右截断]`，只收截断的一级、二级段，先一级后二级、各按段序（起止日期对应第四节第 3 小节的“段起点下标、段终点下标”）；`state_statistics` 对象恰为 27 候选 + 主参照（登记顺序，主参照最后）、必需项齐全，否则退出码 2；`policy_minus_signal` 键恰为 27 候选 + 主参照 + 两条均线对照，每项含 `difference`、`note`（`note` 不读），否则退出码 2；`selection.json` 按出口逐项核验 `feasible`、`maximum`、`tied`、`selected`（列表元素须为候选键文本、顺序亦比对，非选定出口时为空列表与 null），并按项目 `records` 核对非选定出口成立；不可评价均线对照的 `switches`、`signal`、`policy` 须为 JSON null；未计算恒定仓位的 `nav` 须为 null | 实现来源（答复单所引源码未提供，按答复单文字实现） |

### 乙层（N2）

| 项 | 规则 | 来源 |
| --- | --- | --- |
| 日期轴与执行日 | 快照 `date` 严格升序（否则退出码 2）；`days` = `first_day..last_day` 在轴上的闭区间，长 `n + 1`；`days[0]` 在轴上的下标须等于 `window.j0`；`last_day` 晚于 `cutoff` 为输入错误 | 实现来源；`cutoff` 检查依据“数据截断到基准日”的原则（推导） |
| 价格 | 两位小数文本 → `Decimal` → `float`，不舍入；空为缺价 | 实现来源 |
| U | `r = float(C_i)/float(C_{i−1}) − 1.0`；`U = 0.5 * (r_SPX + r_QQQ)`（先相加再乘 0.5，先 SPX 后 QQQ）；任一端缺价自该区间起无法计算；价格为 0 除零 → 计算失败 | 两资产等权为登记来源（第六节第 2 小节）；运算分组为实现来源 |
| 暴露 | 区间 i 用 `days[i−1]` 行的 `core`、`leverage`；一直持有恒为 (0.6, 0.4)；恒定仓位 `w̄_c = fsum(core_0..n−1)/n`、`w̄_l = fsum(leverage_0..n−1)/n`、`ē = w̄_c + 2·w̄_l` | 权重为登记来源（第一节第 6 小节）；按前 n 行平均为实现来源 |
| 组合收益 | `R = core·U + leverage·λ·U`（此顺序），λ = 2；`leverage > 0` 时 `1 + λU` 不大于 0 即计算失败（退出码 3，定位区间） | 杠杆检查为登记来源（第六节第 2 小节）；运算顺序为实现来源 |
| 净值 | `W_0 = 1`，`W_i = W_{i−1}·(1 + R_i)`；`ln W_末 = ln W_n` | 登记来源 |
| 结构 B 已知前缀 | `daily_policy` 各对象 `wealth` 非空行数须等于 `known`；`known > 0` 时用第 i 行（`days[i−1]`）的 `core`、`leverage` 与 `U_i`（i = 1…known−1）重建 W_0…W_{known−1} 并比对；`known = 0` 不建立任何已知净值；不重建止损、冷却、重入 | 实现来源（第四节第 6 小节） |
| 市场环境（R8） | 资产 SPX；d = 区间起点执行日 `days[i−1]`；熊市优先（`P ≤ d < Tr`）；其余按 d 所在年 y 的 `r_y = close(YEAR_END[y]) ÷ close(YEAR_END[y−1]) − 1`（`Decimal`，精度 28 的显式上下文）：`≥ 0.10` 上涨年、`≤ −0.10` 下跌年、其余平淡年；y 或 y−1 不在常量表、年末不在轴上、晚于截止日或缺价 → 完整年度分类不可得；落在快照轴范围内的年末常量须在轴上且为该年轴上最后一日，否则退出码 2；各环境对数收益之和 = 该环境各区间 `fsum(log1p(R_i))`（乙层自己的 R） | 环境分类存在于登记第三节（“口径同 T2”）；资产、阈值、熊市日期、年度端点与 Decimal 计算为实现来源 |
| 第二次交付：输出契约逐项核验 | 按答复单第 11—14 项：`MANIFEST.sha256` 快照行核对（缺失、重复、格式非法、不符为退出码 2）；`environments.csv` 熊市与不可得区间 `year_return` 为空，其余为 r_y 的 Decimal 精确文本；`environment_summary` 键恰为五个环境名、`log_return_sums` 键恰为 27 候选 + 主参照 + 一直持有（否则退出码 2），`intervals` 两种结构都比对，结构 A 比对各和（空分类为 0.0，可计算对象给 null 为不一致），结构 B 各值须为 null；`descriptive.constants[*]` 按 `object` 与结构分三种情形（答复单第 14 项订正（定稿，`996b7111…5189`，2026-10-06），取代原“未计算者四项为 null”的规则）：（a）`object` 为 `选定候选`：`computed` 须为假，`core`、`leverage`、`exposure`、`nav` 均为 null；（b）`object` 为 `主参照` 或候选键且快照判定为结构 B：`computed` 须为假，`nav` 为 null，`core`、`leverage`、`exposure` 为数值并与乙层由该对象 `daily_targets` 前 n 行自算的平均比对（1e-12 绝对）；（c）`object` 为 `主参照` 或候选键且为结构 A：`computed` 须为真，三项平均与 `nav.log_wealth` 比对，`computed` 为假 → 不一致；其余组合按既有规则判不一致或退出码 2；`exposure_substitution.csv` 未计算行 `intervals` 为 0、其余数值与区间为空；两处 `note` 不读取 | 勘误补充单 K4；实现来源（答复单所引源码未提供，按答复单文字实现） |
| 暴露替换（R8） | 解析 `interval_days`（`起/止;…`）；每区间核对：起止为相邻执行日、起点该候选 `daily_targets.position` 为一级、起点满足熊市条件；`registered_log` 用一级 (0.6, 0)、`substituted_log` 用二级 (0.3, 0)、`difference = substituted − registered` | 登记第一节导言（熊市中 0.6 倍的代价报告）；区间筛选与算术为实现来源。**只能证明算术与上列必要条件，不能证明筛选完整性**（MR 激活、PR 不激活不在逐日输出中） |

## 五、容差与数值

| 比较 | 容差 | 依据 |
| --- | --- | --- |
| 逐区间收益（乙层） | `abs ≤ 1e-12`（绝对） | 负责人裁决 R3 |
| 逐日净值（两层） | `abs(a − b) ≤ 1e-12 × max(1, abs(a), abs(b))`（混合） | R3 |
| 对数净值汇总、对账、政策差额、环境对数收益之和、暴露替换三值、`selection.maximum` | `abs ≤ 1e-10`（绝对） | R3；登记第五节 |
| 恒定仓位平均权重 `w̄_c`、`w̄_l`、`ē` | `abs ≤ 1e-12`（绝对） | R3 |
| 对账行 `difference` 与项目值 | `abs ≤ 1e-12`（绝对） | 第四节第 3 小节 |
| 其余 float64 字段：最大回撤、R1 回撤、分段回撤比、调仓幅度、方向和 | `abs ≤ 1e-12`（绝对） | 答复单第 1 项确认（第四节第 4 小节“float64 字段既有容差 1e-12”） |
| 年度收益（Decimal 文本）、整数、日期、分类文字、布尔、哈希、`selection.tolerance` | 精确相等 | R3 |

每次容差比较前先校验两端：脚本值非有限 → “计算失败：脚本值非有限”；项目值非有限 → “不一致：项目值非有限”；一方缺值 → “不一致：一方缺值”；容差本身须为有限正数。计算中出现 `nan`、`inf` 或非正净值 → “计算失败：非有限值或非正净值”，退出码 3。

## 六、常量来源表

| 常量 | 值 | 来源 |
| --- | --- | --- |
| 27 组候选 | K ∈ {3,5,10}、θ_P ∈ {0.015, 0.02, 0.025}、h ∈ {1,3,5}；顺序先 K、再 θ_P、再 h；键 `K={k},θ_P={theta},h={h}`，θ 文本 `0.015`、`0.02`、`0.025` | 登记第五节；键格式与 θ 文本为实现来源（材料 03 第四节第 2 小节、材料 04 第三节） |
| 对象名 | `主参照`、`一直持有`、`200 日均线一级版`、`带缓冲带的 200 日均线二级版（101%/99%）`、`恒定仓位：<对象>`、`选定候选` | 材料 03、04 |
| 权重与 λ | 正常 (0.6, 0.4)、一级 (0.6, 0.0)、二级 (0.3, 0.0)；λ = 2 | 登记第一节第 6 小节、第六节第 2 小节 |
| R1、R2、并列 | 0.5；3/5；1e-10 | 登记第五节 |
| 分段 | `1999—2009` (1998-12-31, 2009-12-31]；`2010—2016` (2009-12-31, 2016-12-30]；`熊市一` (2000-03-24, 2002-10-09]；`熊市二` (2007-10-09, 2009-03-09] | 分段名称为登记来源（第五节）；日期取自材料 03 第四节第 3 小节（D27） |
| 熊市（环境分类） | [2000-03-24, 2002-10-09)、[2007-10-09, 2009-03-09)（`P ≤ d < Tr`） | 材料 03 第四节第 9 小节 |
| 环境阈值 | 资产 SPX；上涨 `≥ 0.10`、下跌 `≤ −0.10`（Decimal） | 材料 03 第四节第 9 小节（D27） |
| `YEAR_END_NYSE[1989..2016]` | 1989-12-29、1990-12-31、1991-12-31、1992-12-31、1993-12-31、1994-12-30、1995-12-29、1996-12-31、1997-12-31、1998-12-31、1999-12-31、2000-12-29、2001-12-31、2002-12-31、2003-12-31、2004-12-31、2005-12-30、2006-12-29、2007-12-31、2008-12-31、2009-12-31、2010-12-31、2011-12-30、2012-12-31、2013-12-31、2014-12-31、2015-12-31、2016-12-30 | 回算脚本会话按 NYSE 交易日历规则人工列出：12 月 31 日为周一至周五时即为当年最后交易日（NYSE 在元旦逢周六时不提前在周五休市，故 1993、1999、2004、2010 年的 12-31 周五照常交易）；12 月 31 日为周六或周日时取之前的周五。1989—2016 各年 12 月下旬无其他全日休市影响年末。星期由本会话以 Python `datetime` 逐项核对（见 `记录\步骤记录.txt`）；未调用任何日历库，未读取行情。与登记分段端点 1998-12-31、2009-12-31、2016-12-30 一致。该表与项目 `stock_trading_days` 的一致性由 N4b 联检证明，表本身的来源由独立复核者核对 |

## 七、已答复事项与依据（答复单第二节；编号同首次交付 README 第七节）

| # | 事项 | 答复与本次实现 |
| --- | --- | --- |
| 1 | 其余 float64 字段的容差 | 确认 1e-12 绝对（最大回撤、R1 回撤、分段回撤比、调仓幅度、方向和），超出即不一致 |
| 2 | 甲层比对 `descriptive.averages[*].signal.log_wealth` | 确认，只比较，1e-10 绝对 |
| 3 | `reference_summary` 一直持有行 | `switches` 期望整数 0（精确），`exposure_magnitude` 期望 0.0（1e-12）；不再照录 |
| 4 | `segments.csv` 各分支 | 按四种覆盖文字分支核验全部列；“无收益区间”与“不可计算”时 `undefined` 为 False；表外覆盖文字为退出码 2；不再照录 |
| 5 | `state_statistics[*].truncated` | 元素 `[position, start, end, left_truncated, right_truncated]`，逐条精确（条数与内容）；起止日期按第四节第 3 小节“段起点下标 s、段终点下标 e”取 `days[s]`、`days[e]` |
| 6 | 对象覆盖范围 | 对象集合为已绑定的输出接口契约（`development_output.py` 第 554—564 行）：`state_statistics` 恰为 27 候选 + 主参照（登记顺序）、必需项齐全；`policy_minus_signal` 键恰为 30 个、每项含 `difference` 与 `note`；多、少或缺项为退出码 2；只有 `start_note` 文字照录 |
| 7 | `selection.json` | 按出口逐项核验；元素为候选键文本且顺序亦比对（顺序号为表外取值，退出码 2）；非选定出口时 `selected`、`maximum` 为 null，`feasible`、`tied` 为空列表。缺值无法评价、无合格候选按项目 `records` 核对出口成立。**计算失败**（答复单定稿修订一）：出口条件为“主参照失败或任一候选 `failed` 为真”；候选分支（有候选 `failed` 为真）按 `records[*].failed` 核对出口成立；主参照分支（项目出口为“计算失败”而 `records` 无一 `failed` 为真）：已获准字段中没有“主参照失败”的直接字段，`outcome` 一项不判不一致，记“保留：主参照失败原因未独立核验”，并把 `reference_summary.csv` 主参照行 `signal_log_wealth`、`signal_max_drawdown` 是否为空作为旁证写入报告（“主参照失败旁证”项）。负责人补充执行限定（照录）：“‘计算失败’出口也须核对 selected=null、feasible=[]、maximum=null、tied=[]。‘主参照失败原因未独立核验’不豁免结果字段及其他结构检查，不得将该保留项计为完整核验通过。”实现：保留项只作用于 `outcome` 一项；`selected`、`feasible`、`maximum`、`tied` 仍按空值精确核对，其他结构与字段检查照常；报告不写“出口完整核验通过”，保留项在 `summary.reserved` 单列、不计入一致。登记（实现来源，供复核者核对）：开发期入口调用 `select` 时 `reference_failed` 固定传入假值，主参照失败在开发期入口于更早阶段停止处理；本脚本不据此省略上述处理 |
| 8 | 不可评价均线对照 | `descriptive.averages` 的 `switches`、`signal`、`policy` 须为 JSON null；`reference_summary` 该行四列为空、`switches` 0、`exposure_magnitude` 0.0；null、空字符串、0 互不相等 |
| 9 | O_d 以外对象出现在逐日表 | 确认“不一致：对象不在 O_d 内” |
| 10 | E 中对象缺行 | 退出码 2（勘误补充单 K3） |
| 11 | 快照哈希 | 删除递归找哈希；读 `MANIFEST.sha256` 的 `input_snapshot.csv.gz` 唯一一行核对（勘误补充单 K4）；`run_record.json` 只取 `cutoff`、`usage_restriction` |
| 12 | `environments.csv` 的 `year_return` | 熊市与不可得为空；其余为 Decimal 精确文本 |
| 13 | `environment_summary` | 逐字段核验（见第四节）；键多、少为退出码 2 |
| 14 | 未计算项与 `note` | `descriptive.constants[*]` 按 `object` 与结构分三种情形（答复单第 14 项订正（定稿，`996b7111…5189`，2026-10-06），取代原“未计算者四项为 null”的规则）：（a）`object` 为 `选定候选`：`computed` 须为假，`core`、`leverage`、`exposure`、`nav` 均为 null；（b）`object` 为 `主参照` 或候选键且快照判定为结构 B：`computed` 须为假，`nav` 为 null，`core`、`leverage`、`exposure` 为数值并与乙层由该对象 `daily_targets` 前 n 行自算的平均比对（1e-12 绝对）；（c）`object` 为 `主参照` 或候选键且为结构 A：`computed` 须为真，三项平均与 `nav.log_wealth` 比对，`computed` 为假 → 不一致；其余组合按既有规则判不一致或退出码 2。未计算替换行 `intervals` 0 且其余为空，精确核对（不变）；两处 `note` 不读取、不比对。身份核验分工：甲层依 `selection.json` 出口核对恒定仓位对象是实际选定候选还是 `选定候选`；乙层只核对允许集合与结构条件，不表示已独立证明该候选为选定候选 |
| 15 | 乙层 `daily_nav` 缺应有路径 | 确认“不一致：一方缺值” |
| 16 | `last_day` 晚于 `cutoff` | 确认退出码 2 |
| 17 | 矩阵“乙·市场环境”行措辞 | 首次交付的读法正确，实现与 N4 覆盖予以接受；矩阵按勘误补充单 K1 改为“1997 年末不在轴上 → 起点 1998-12-31 的区间‘完整年度分类不可得’”。原因说明保留：按第四节第 9 小节 d 为区间起点，轴 1998-12-31 至 2001-01-02 上没有起点在 2001 年的区间，且 2001 年全年在熊市一内。**纪律偏差登记**：首次交付时本会话发现该处矩阵期望与规则不符，按转达稿第四节应停下列问题，但实际按规则实现并继续交付、在报告中列出；负责人登记为纪律偏差、不追究 |
| 18 | 矩阵“只读目录” | 确认：Windows 上以“`--out` 的父路径是普通文件”构造不可写目标，另以注入异常模拟写入中途失败 |
| 19 | N4 导入 `sys` | 勘误补充单 K2：N4 白名单加入 `sys`（仅限 `sys.executable`） |
| 20 | 恒定仓位对象命名（第四次交付指令，`78357066…96af`） | 同一正式目录中恒定仓位路径在三处使用不同对象名：`daily_nav.csv.gz` 的 `object` 为 `恒定仓位：<对象>`（全角冒号“：”，无空格）、`path` 为 `恒定仓位`（`development_output.py` 第 403 行）；`reconciliation.json` 单一路径对账行的 `object` 为原对象名（`主参照` 或候选键）、`path` 为 `恒定仓位`（`development_compare.py` 第 569—571 行）；`descriptive.json` `constants[*].object` 为原对象名（`ConstantExposure.object`）。其他路径（信号模拟、执行政策研究模拟、一直持有、均线对照）两表对象名相同；对账行以（`object`，`path`）二元组唯一定位。逐处对应：(1) N1 合法组合表与 K：K 成员名 = “恒定仓位：” + `constants[*].object`（`computed` 为真且身份核对通过），`daily_nav` 恒定仓位行的对象名须与之精确相等（全角冒号），否则“不一致：对象或路径不在合法组合表内”，K 成员的必需行因此缺失时按既有缺行规则退出码 2；(2) N1 单一路径对账：恒定仓位路径的期望对账行键为（原对象名，`恒定仓位`），原对象名由 K 成员名去掉前缀“恒定仓位：”得到（`recon_key`）；对账行写成带前缀的名称（全角或半角冒号）即一缺一多；(3) N1 `descriptive.constants[*].nav` 比对：以“恒定仓位：” + `object` 取 `daily_nav` 净值链；(4) N2：`daily_nav` 恒定仓位行与 `descriptive.constants` 同样以“恒定仓位：” + `object` 对应，N2 不读 `reconciliation.json`，未改动；(5) `exposure_substitution.csv` 的对象为候选键，不涉及恒定仓位命名。首次至第三次交付中 N1 与 N4 生成器都把恒定仓位对账行写成或期望为`恒定仓位：<对象>`，两者自洽而与契约不符，属第八节所述“同一理解错误同时出现在生成规则与脚本中”的情形，由原开发会话以构造正式目录试跑发现（4 项一缺一多），第四次交付改正 N1 与生成器 |
| 21 | `usage_restriction` 改为数组（修正批第一道门接口 E1） | N1、N2 相同：值须为 JSON 数组且每个元素为字符串，照录全部元素、保持原顺序，`[]` 为无用途限制；值不是数组（字符串、null、数字、布尔、对象等）或任一元素不是字符串 → 输入错误，退出码 2，不转换、不忽略。缺键：沿用既有处理（`jget` 抛输入错误“缺少字段 usage_restriction”，退出码 2）。登记文字：前缀诊断限制“前缀诊断运行（严格读取未通过：缺少必需价格）”在前、开发窗口覆盖限制“开发窗口未覆盖登记区间（axis[j₀] 晚于 2000-03-24）”在后。未登记文字、顺序不同、元素重复：项目负责人 2026-10-06 裁决 R-1：两脚本照录、不作判定；项目侧数组的文字与顺序由项目构造验收保证，独立层对此不作核对（已接受的限制）；N1 第 22 项 (e) 只看覆盖限制文字的有无与逐字相等 |
| 22 | 开发窗口覆盖判定（修正批第一道门接口 E2 与第四节） | N1 新增只读 `run_record.json` 的 `window_coverage`（四键），只作比对对象，允许用途、禁止用途与核验范围见第二节“元数据输入”行；冻结常量 2000-03-24（`COVERAGE_DATE`）、条件文字 `axis[j0] <= 2000-03-24`、状态“已判定”、覆盖限制文字写在 N1 常量中。`satisfied` 为字符串或数字（非 JSON 布尔）判不一致；N2 不读 `window_coverage`。另确认：N1、N2 均不读取 `descriptive.json` 的 `policy_events`（事件种类文字），也不对 `descriptive.json` 顶层键集做精确核对 |
| 23 | R2 事件不可得（修正批第 3 步） | R2 事件不可得（项目负责人 2026-10-06 裁决 F-1（经项目发起人批准））：既有全部结构检查照旧先执行；随后对每个资产 a（SPX、QQQ），进入特殊分支的唯一条件是 `candidates_summary.csv` 27 行中 a 的五列（`<a>_r2_computable`、`<a>_r2_denominator`、`<a>_r2_achieved`、`<a>_r2_new_only`、`<a>_r2_meets`）全部为空，且同时满足（i）`r2_judgements` 中 a 无任何行，（ii）`run_record.json` 的 `usage_restriction` 数组含前缀诊断限制文字“前缀诊断运行（严格读取未通过：缺少必需价格）”（逐字），（iii）`selection.json` 27 条 `records[*].r2[a]` 均为 null。满足时这 27 × 5 = 135 项状态写为“照录：R2 事件不可得（项目表示；原因未独立核对）”，计入 `summary.unchecked_or_recorded`，不计一致、不一致或计算失败，不表示独立核验通过；N1 对 a 的 computable 视为假（脚本由空判定行算得不可计算、meets 为空），出口判定规则、其他字段核验、汇总逻辑与出口优先级不变；报告 `r2_unavailable_assets` 列出进入特殊分支的资产。其他一切情况（任一行 a 任一列非空，或五列全空但任一前提不满足）按既有规则逐字段比对；合法零分母 false/0/0/0/空照旧一致。本规则不新增读取材料或读取权限（所用字段均为既已获准读取），N2 不变。接受的限制：满足三项前提时，N1 不能区分“事件不可得”与“零判定行”，也不核对不可得的原因，由 M4 工具比对承接 |
| 24 | 非有限暴露与未预期异常（修正批第 4 步） | 依据：项目负责人 2026-10-06 裁决 F-2（经项目发起人批准）。（1）N1：读取 `daily_targets` 的 `core`、`leverage` 用于暴露计算前逐值校验有限性，并对暴露 e = core + λ·leverage 与切换处 Δe 校验有限性；任一非有限即记“计算失败：非有限值”（位置写表、对象、日期、字段：`daily_targets.csv.gz 对象=… 日期=… 字段=core/leverage/e/Δe`），该对象的切换次数与调仓幅度不再计算（记为空，依赖它们的既有比对按既有规则记“一方缺值”），退出码按既有优先级为 3，报告照常写出；其他逻辑与 F-1 特殊分支不变。此前 core 为 inf 且相邻两次切换时 Δe 为 +inf 与 −inf，`math.fsum` 抛 ValueError，N1 无报告、进程退出码 1。（2）N2 观察（修改前，以同一构造输入运行现行 N2）：结构 A 下 core = inf 或 leverage = nan → 组合收益非有限，退出码 3，写出报告，error 为“计算失败：非有限值”、位置为该对象与区间（符合规格）；结构 B 下 N2 不用该候选的计划目标作任何计算（不计算组合收益，恒定仓位平均只取主参照），无非有限计算，退出码 0（符合规格：规格针对“任一计算出现”）。结论：N2 不作第 1 条同类修改。（3）未预期异常兜底（N1、N2）：主入口（核对阶段 `build_report` 与读入阶段 `main`）捕获此前未被捕获的异常，写出报告（error.kind 为“未预期异常”，message 为异常类型与信息），退出码 3；只作用于既有处理之外的异常：输入或参数错误仍为 2，存在不一致仍为 1，计算失败仍为 3，报告写入失败仍按既有规则（标准错误列明已写出部分，退出码 3）。代码以 `# noqa: BLE001` 标明按裁决须捕获全部异常 |
| 25 | 仓库 lint 兼容与并列组切换次数为空（修正批第 5 步） | 依据：项目负责人 2026-10-06 裁决 F-4（经项目发起人批准）。（1）删除 N1、N2 兜底处四个“# noqa: BLE001”（仓库 ruff 配置未启用 BLE，该注释触发 RUF100），保留其上一行引用裁决 F-2 第 3 条的理由注释，兜底逻辑不变；仓库配置：line-length 120、target-version py312、select E/F/W/I/UP/B/ANN/RUF、ignore ANN401/RUF001/RUF002/RUF003、`tests/**` 忽略 ANN。（2）N1 重算选择：并列组只有一名则照常选定；并列组至少两名且任一成员切换次数为空（含全部为空）时停止排序、不比较、不抛异常，重算的 selected 记为空，并无论项目值如何都追加一项“计算失败：并列组切换次数不可计算”（位置 selection.json selected，列出切换次数为空的成员）；selected 仍以既有 compare_exact 比对（项目值非空为“不一致：一方缺值”，双方均空保留既有结果）；outcome、feasible、maximum、tied 照常比对；退出码按既有优先级为 3，error 为 null，items 完整保留；正常有效输入的选择规则不变。（3）读取方排查：N1 读取 SwitchSummary 的 count、magnitude、change 与 CandidateRecord.switches 的全部位置——compare_candidate_core、reference_expectation（经 check_reference_summary 比对）、script_records、record_items、average_entry_items——均交 compare_exact 或 compare_float，空值按既有规则记“一方缺值”、不抛异常；select_candidate 的排序按第（2）条处理；CandidateRecord.switches 与 SwitchSummary 三项的类型声明允许空值，SelectionResult 增加 uncomputable_tied（默认空）。未发现其他会抛异常的位置 |

N4 的 `hashlib`：N4 直接导入 `hashlib`，经负责人裁决 P3（2026-10-06，甲案）加入 N4 白名单，用途限定为构造夹具 `MANIFEST.sha256` 行的 SHA-256。第二至第四次交付曾经按路径加载的 N2 模块属性取用 `hashlib` 并登记于 `TEST_EXCEPTIONS`；第五次交付按裁决改为直接导入，删除该绕行写法与对应 `TEST_EXCEPTIONS` 条目。

## 八、限制与共同错误风险（照第四节第 7 小节写入）

- 甲层以项目逐日输出为输入，逐日输出错而汇总自洽时发现不了；乙层以项目快照与计划目标为输入，快照或计划目标错时发现不了；两层都不能证明状态与事件正确（由工具比对 M4 负责）。
- 甲层不能证明收益、状态、事件由价格正确生成，不能发现窗口本身的错误；甲层不宣称完成乙层复核。乙层不能证明状态机与事件判定，不推定缺价之后的目标、止损、冷却与重入，不核对快照与原始行情文件的关系。
- 首个信号日的前一日状态不在逐日输出中，相关转换只标注“依赖窗口外状态，未独立核对”。
- 暴露替换只证明算术与必要条件，不证明区间筛选的完整性。
- **共同理解风险**：第四节的公式与规则由产品经理从 D23—D30 源码整理成规格化文字，本会话据此实现；若源码与登记不符而规格化文字照抄了源码，回算发现不了。标“实现来源”的各项与登记是否一致，由独立复核者单独核对。会话隔离只降低“照抄实现”的风险，不是算法独立性的证明。
- N4 的构造目录生成函数与 N1、N2 由同一会话编写，存在同一理解错误同时出现在生成规则与脚本中的风险；N4 以人工期望值表（`*_HAND` 常量与 `test_generator_matches_hand_table`）约束关键值，`test_ab_on_constructed_formal_directory`（N4b，原开发会话）是“项目与脚本相互一致”的检查，不含人工期望值。
- 第十一节覆盖表中“未复核项”：共同起点与收敛（不在两层内，M4）；均线规则本身；执行政策模拟本身（止损、冷却、重入，M4）；R2 事件与类别本身（M4）。
- 第二次交付中按答复单“依据：源码行号”实现的输出契约条目（第四节两处“第二次交付”行），所引源码未提供，本会话按答复单文字实现；与源码是否一致由 N4b 联检与独立复核者核对。

独立回算脚本按提供的规格材料实现；会话启动与访问范围存在已披露的隔离偏离。暴露范围依据访问记录和执行者报告登记，尚未由独立复核者核验原始材料。不以工作树、分支或会话位置证明算法独立性。

## 九、冻结哈希登记表（项目负责人 2026-10-06 裁决 F-3、F-4：由独立回算脚本会话在第十次交付中填写；只列 N1、N2）
下表为修正批冻结值（第十次交付）。提交 d5917ac 时的历史冻结值：recompute_a.py 95478 字节 281e1728baf30c22d380d94927d73154a1f9765ae51c09a2f23ea8930c909f31；recompute_b.py 61212 字节 65f542b05f850be683624d31194d8bec8e960dd46b71f7e2e220bcc1d7819abc。

| 文件 | 字节数 | SHA-256 |
| --- | --- | --- |
| `docs/audit/独立回算/v20/recompute_a.py` | 105711 | `de70cd44d4aa642477c75d4756a395467821a3962f6c3ccc912feee0bc4c78a3` |
| `docs/audit/独立回算/v20/recompute_b.py` | 64614 | `8a670b89da685119d3b06b151b0fc6de7b1ece8bfc90a72b9042d9205f94e1b7` |
