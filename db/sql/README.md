# db/sql/：由程序导出的 SQL 文件

本目录的 SQL 文件由 `uv run market-risk db export-sql` 自动生成，**不得手工修改**。表结构或 `config/` 下的 YAML 变化后未重新导出时，测试（`tests/test_dbsql.py`）会失败。

| 文件 | 内容 |
|---|---|
| `01_schema_sqlite.sql` | 当前 Alembic 最新版本的表结构（SQLite），含 `alembic_version` |
| `01_schema_mysql.sql` | 同上（MySQL；建库时使用 utf8mb4 字符集） |
| `02_base_data.sql` | 参考表的 INSERT 语句：`symbols`（config/symbols.yaml）、`market_holidays`（config/holidays.yaml）、`data_decisions`（config/data_decisions.yaml）；两种数据库都可执行 |

表结构的唯一源头是 Alembic 迁移（`migrations/versions/`）；参考表内容的源头是 YAML。

## 具体数据不导出 SQL

数据库只存分析结果与索引，全部可由文件重建：

| 数据 | 位置 |
|---|---|
| 市场数据（逐日序列，不入库） | `data/market/` |
| 人工录入：广度读数、结果标签、复核记录、TradingView 原始导出 | `data/manual/` |
| 资料与资料索引 | `data/materials/` |
| 运行结果（分数、指标、正式记录） | `results/` |
| 截图时代的历史记录 | `data/legacy/` |

## 重建数据库

```bash
uv run market-risk rebuild-db
```

先清空数据库并执行 Alembic `upgrade head`，再由上述文件写入全部内容（含参考表）。连接地址来自 `.env` 的 `DATABASE_URL`，其次 `config/settings.yaml` 的 `database.url`，默认 `sqlite:///db/market_risk.sqlite`。

也可以直接用本目录的 SQL 建一个只含表结构与参考表的空库，例如 SQLite：

```bash
sqlite3 new.sqlite ".read db/sql/01_schema_sqlite.sql" ".read db/sql/02_base_data.sql"
```

## MySQL 兼容性验证

```bash
uv run --extra mysql market-risk db verify-mysql
```

连接地址从 `.env` 的 `MYSQL_VERIFY_URL` 读取（专用、可清空的空库；未设置时跳过）。文件、日志和输出中不包含任何账户、密码或本机路径。
