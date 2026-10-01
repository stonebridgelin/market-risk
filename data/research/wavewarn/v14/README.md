# v1.4 验证期所需的 VIX3M

`vix3m_cboe_validation.csv` 来自 [Cboe 官方 VIX3M 历史 CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv) 的 `CLOSE` 列，保留官方原始小数文本。2026-10-01 下载（准确时间见 `vix3m_cboe_validation.meta.json` 的 `fetched_at_utc`），按日期升序流式读取，遇到 2022-12-30 之后的首行立即停止；只保存 2009-09-18 至 2022-12-30 的 3345 行，不读取、保存或输出 2023 年起的数值。

`vix3m_cboe_validation.meta.json` 记录来源网址、取得时间、截断范围、行数与本副本的 SHA-256（`970fcdf54427e0b561c05bc0d59f50754888e48e12f4961183f41ca49a39e488`）。该哈希是**截断副本**的哈希，不是 Cboe 全量历史文件的哈希。

下载时已核对：本副本中 2016-12-30 及以前的 1835 行与已入库的开发期副本 `data/research/wavewarn/v121/vix3m_cboe_development.csv` 逐行相同（不同则程序报错、不写文件）。

生成命令：`uv run market-risk wavewarn fetch-vix3m-validation`。目标文件已存在时拒绝覆盖。

**使用限制：** 本文件中 2017 年起的行只在锁定之后、由那一次正式的验证期运行使用。验证期之前的任何运行（包括开发期演练与测试）都通过 `inputs.load_inputs_until` 以 2016-12-30 为截止日读取，在第一条晚于截止日的行之前停止。本文件只供 `market_risk.wavewarn` 研究使用，与原口径前瞻报告的 TradingView VIX3M 不得混用。
