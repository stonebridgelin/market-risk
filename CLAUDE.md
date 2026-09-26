# CLAUDE.md：长期约定

1. 回复、代码注释、文档一律使用中文；变量名、函数名使用英文。
2. 评分规则以 `docs/SOP.md` 第7节为准，SOP 未写清之处按 `docs/SPEC.md` 第5.6节口径；两者都没有覆盖的，停下来问用户。
   规则版本冻结：修改规则必须新建版本文件（如 `scoring/v4.py`），不得改动 `scoring/v2m.py`、`scoring/v3r1.py` 的判定逻辑。
3. 任何参与计算的数据都必须截断到基准日（含）；新增数据源时必须补充防未来信息测试（`tests/test_no_lookahead.py`）。
4. 价格一律使用不复权 `Close`（yfinance `auto_adjust=False`），不得使用 `Adj Close`。
5. 缺失数据记"待补"，不插值、不用其他数据替代（唯一例外：VIX 的 Cboe 官方备用源，见 SPEC 5.6 第7条）。
6. 修改代码后运行 `uv run pytest` 与 `uv run ruff check`，全部通过才能提交。
7. 回归样本的期望值来自人工核对，不得为了通过测试而修改期望值；出现差异时报告给用户。
8. 核心逻辑写成输入输出清晰的纯函数，不依赖全局状态（为以后接入 LangChain Agent 预留）。
9. 存储设计以 `docs/STORAGE.md` 为准：所有路径一律由 `src/market_risk/storage/paths.py` 生成，不在其他地方拼接路径字符串；
   每次运行保留独立目录，不覆盖；测试只能写入临时目录，不得写入真实的 `results/`、`data/`、`db/`、`reports/`。
10. 结果标签隔离：`scoring/`、`report.py` 中生成 prompt 的部分、`data/snapshot.py` 不得读取 `outcomes`（标签文件与标签表）。
11. 本机 git 不在 PATH 中，路径为 `C:\Execute\Git\bin`；在 PowerShell 中先执行 `$env:PATH = "C:\Execute\Git\bin;$env:PATH"`。
