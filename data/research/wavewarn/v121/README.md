# v1.2.1 研究专用 VIX3M

`vix3m_cboe_development.csv` 来自 [Cboe 官方 VIX3M 历史 CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv) 的 `CLOSE` 列，保留官方原始小数文本。2026-09-29 下载时按日期升序流式读取，遇到开发期末 2016-12-30 之后的首行立即停止；仅保存 2009-09-18 至 2016-12-30 的 1835 行，不读取、保存或输出验证期与保留期数值。

`vix3m_cboe_development.meta.json` 记录来源网址、取得时间、截断范围、行数与截断 CSV 的 SHA-256。该哈希是**开发期截断副本**的哈希，不是 Cboe 全量历史文件的哈希。重新获取若发现已存开发期数值改变，代码会报错，需核对历史修订后另行处理。

本文件只供 `market_risk.wavewarn` 研究使用；原口径前瞻报告的 VIX3M 特征仍用其原有 TradingView 序列，两个口径的数据不得混用。
