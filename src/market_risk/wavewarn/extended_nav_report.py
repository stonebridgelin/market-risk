"""补充历史真实净值的表格行与报告（纯计算，描述性，不参与任何判定）。"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from market_risk.wavewarn.config_v14 import Round2Config
from market_risk.wavewarn.diagnostics_round2 import PORTFOLIO, NavRow
from market_risk.wavewarn.diagnostics_round2_report import nav_header, nav_row, percent, plain
from market_risk.wavewarn.evaluation_v13_report import share, table
from market_risk.wavewarn.extended_nav import SCOPES, ExtendedNavResult, SignalObject, reversal_share
from market_risk.wavewarn.nav import wealth_path

Row = tuple[object, ...]
OPENING = "本报告仅使用2009-09-30以前的纯价格数据；诊断不改变模型规则、参数、选定设定与γ。"
REPORT_NAME = "补充历史净值报告.md"


def _blank(value: object) -> object:
    return "" if value is None else value


def reversal_cells(item: SignalObject | None, config: Round2Config) -> tuple[object, ...]:
    """各观察窗口内出现反方向切换的切换占比；恒定暴露的对象没有切换，留空。"""
    return tuple(_blank(reversal_share(item.switches, window)) if item is not None else ""
                 for window in config.reversal_windows)


def metric_header(config: Round2Config) -> tuple[str, ...]:
    return (*nav_header(config), "mean_exposure",
            *(f"reversed_within_{window}_share" for window in config.reversal_windows), "switch_on_first_day")


def metric_rows(rows: Sequence[NavRow], signals: Sequence[SignalObject], config: Round2Config) -> tuple[Row, ...]:
    """净值指标后附平均执行暴露与反向切换比例。"""
    by_name = {item.name: item for item in signals}
    result = []
    for row in rows:
        item = by_name.get(row.name)
        result.append((*nav_row(row), item.mean_exposure if item is not None else "",
                       *reversal_cells(item, config),
                       ("是" if item.switch_at_start else "否") if item is not None else ""))
    return tuple(result)


PERIOD_HEADER = ("period_start", "period_end", "object", "scope", "intervals", "period_return", "max_drawdown",
                 "drawdown_peak_date", "drawdown_trough_date")


def period_table(result: ExtendedNavResult) -> tuple[Row, ...]:
    return tuple((row.start, row.end, row.name, row.scope, row.intervals, row.period_return, row.drawdown.depth,
                  row.drawdown.peak_date, row.drawdown.trough_date) for row in result.periods)


def nav_daily(result: ExtendedNavResult, places: Decimal) -> tuple[tuple[str, ...], tuple[Row, ...]]:
    """逐日净值（宽表）：各对象、各口径一列，W_0 = 1；九组设定只列双资产。"""
    rows = [*result.nav, *(row for row in result.grid_nav if row.scope == PORTFOLIO)]
    paths = [wealth_path(row.returns) for row in rows]
    header = ("date", *(f"{row.name}|{row.scope}" for row in rows))
    return header, tuple((day, *(path[index].quantize(places) for path in paths))
                         for index, day in enumerate(result.days))


def _drawdown(row: NavRow) -> str:
    drawdown = row.metrics.drawdown
    if not drawdown.depth:
        return "0.00%"
    return f"{percent(drawdown.depth)}（{drawdown.peak_date} 至 {drawdown.trough_date}）"


def _share(value: object) -> str:
    return share(value) if isinstance(value, Decimal) else "—"


def scope_lines(result: ExtendedNavResult, config: Round2Config) -> list[str]:
    prepared = result.prepared
    signals = [(item.name, item.mean_exposure, item.lights[0], len(item.switches),
                "有" if item.switch_at_start else "无",
                *(_share(cell) for cell in reversal_cells(item, config))) for item in result.objects]
    first_day = [item.name.split("版 ")[1] for item in result.grid if item.switch_at_start]
    return [
        "# v1.4 补充历史的真实净值（描述性，不参与任何判定）", "", OPENING, "",
        f"- 窗口与 v1.4 补充历史相同：t0′ = {prepared.t0}，τ′ = {prepared.tau}，j₀′ = {result.days[0]}，"
        f"窗口末日 {result.days[-1]}，共 {len(result.days) - 1} 个区间（t0′、τ′、j₀′ 的规则不变）。"
        "只读取 SPX 与 QQQ 的收盘价，读取在窗口末日之后的第一行之前停止；"
        "SPX 的 200 日均线用到窗口起点之前的 SPX 价格。",
        "- 对象：v1.4 纯价格版（MR + P + PR，解除规则 F）的选定设定 K=5、θ_P=2.5%，另附九组汇总；200 日均线；"
        "带缓冲带的 200 日均线（t0′ 当日 SPX 收盘价不低于其 200 日均线为绿，否则为红；之后高于均线的 101% 转绿、"
        "低于 99% 转红，区间内保持前状态；1% 为固定参考值）；一直持有；同平均暴露基准（分别取纯价格版选定设定与 "
        "200 日均线的 ē）。",
        "- 各对象的执行灯色由状态序列按“次日收盘执行”直接得到，没有读取任何标签，也没有计算损失"
        "（ē 与原补充历史报告一致，有测试核对）；"
        "选定设定是开发期选择程序的结果，这里不重新选择。初始暴露继承此前的信号，初始建仓与窗口末日的切换不计。",
        f"- 净值口径与第二轮诊断相同：单资产 R_{{a,j}} = e_j·(exp(r_{{a,j}}) − 1)；双资产每日收盘按“总暴露 e_j、"
        "两资产各半”再平衡，R_j = Σ_a (e_j/2)·(exp(r_{a,j}) − 1)；现金收益为 0；不含分红与费用。"
        "SPX 为价格指数，QQQ 为不含分红的 ETF 价格；γ 是代理损失罚分，不扣入净值。"
        f"年化按每年 {config.trading_days_per_year} 个交易日、波动率用样本标准差。", "",
        "## 一、对象", "",
        *table(("对象", "平均执行暴露 ē", "j₀′ 的执行灯色", "切换次数", "j₀′ 当日有无切换",
                *(f"{window} 日内反向切换占比" for window in config.reversal_windows)), signals), "",
        "切换次数按诊断口径：只计窗口内相邻两日执行灯色不同的切换，初始建仓与窗口末日的切换不计。"
        "j₀′ 当日收盘若恰有一次切换（执行灯色与前一交易日不同），按初始建仓处理、不计入；"
        "主损失的计费口径会计入这一次，所以这类对象在补充历史报告（`extended_history/`）里的计费切换次数比这里多 1。"
        + (f"九组中属于这种情形的：{'、'.join(first_day)}。" if first_day else "九组中没有这种情形。"), ""]


def nav_table(rows: Sequence[NavRow], names: Sequence[str], signals: Sequence[SignalObject],
              config: Round2Config) -> list[str]:
    """一个口径的净值表。"""
    by_name = {item.name: item for item in signals}
    header = ("对象", "累计收益", "年化收益", "最大回撤（起止日）",
              *(f"最差滚动 {window} 日" for window in config.rolling_windows), "年化波动率", "切换次数",
              "目标暴露变化量", *(f"{window} 日内反向" for window in config.reversal_windows))
    body = []
    for row, name in zip(rows, names, strict=True):
        metrics = row.metrics
        body.append((name, percent(metrics.cumulative), percent(metrics.annualized), _drawdown(row),
                     *(percent(item.value) for item in metrics.rolling), percent(metrics.volatility), row.switches,
                     plain(row.exposure_change),
                     *(_share(cell) for cell in reversal_cells(by_name.get(row.name), config))))
    return table(header, body)


def nav_lines(result: ExtendedNavResult, config: Round2Config) -> list[str]:
    lines = ["## 二、完整净值", "",
             "最大回撤 = 1 − 谷值 ÷ 此前最高值；最差滚动 k 日收益 = min W_{t+k} ÷ W_t − 1；"
             "目标暴露变化量只累计窗口内每次切换的目标暴露变化，不含每日再平衡交易与初始建仓。", ""]
    for scope in SCOPES:
        rows = [row for row in result.nav if row.scope == scope]
        lines.extend([f"### {scope}", "",
                      *nav_table(rows, [row.name for row in rows], result.objects, config), ""])
    return lines


def grid_lines(result: ExtendedNavResult, config: Round2Config) -> list[str]:
    rows = [row for row in result.grid_nav if row.scope == PORTFOLIO]
    names = [f"K={item.name.split('K=')[1]}" for item in result.grid]
    pairs = zip(names, result.grid, strict=True)
    exposures = "；".join(f"{name} 的 ē = {item.mean_exposure:.4f}" for name, item in pairs)
    return ["## 三、纯价格版九组汇总（双资产）", "",
            "九组按登记顺序（先 K 后 θ_P）全部列出，只作描述，不在这段历史上重新选择。", "",
            *nav_table(rows, names, result.grid, config), "", f"平均执行暴露：{exposures}。", ""]


def period_lines(result: ExtendedNavResult) -> list[str]:
    lines = ["## 四、两次熊市的期间收益与最大回撤", "",
             "期间按 SPX 收盘价的高点到低点划定，两个资产共用；取区间起点在 [起, 止) 内的区间"
             "（最后一个区间止于期末当日收盘），净值在期间起点重新记为 1。", ""]
    for start, end in dict.fromkeys((row.start, row.end) for row in result.periods):
        lines.extend([f"### {start} 至 {end}", ""])
        for scope in SCOPES:
            rows = [(row.name, row.intervals, percent(row.period_return),
                     f"{percent(row.drawdown.depth)}（{row.drawdown.peak_date} 至 {row.drawdown.trough_date}）"
                     if row.drawdown.depth else "0.00%")
                    for row in result.periods if (row.start, row.end, row.scope) == (start, end, scope)]
            lines.extend([f"**{scope}**", "", *table(("对象", "区间数", "期间收益", "期间最大回撤（起止日）"), rows),
                          ""])
    return lines


def report_lines(result: ExtendedNavResult, config: Round2Config) -> list[str]:
    return [*scope_lines(result, config), *nav_lines(result, config), *grid_lines(result, config),
            *period_lines(result),
            "这段历史只能运行纯价格版（没有广度与期限结构数据），数字只作描述，不能称为历史预警效果，"
            "也不改变任何选择。逐日净值见 `nav_daily.csv.gz`，各文件的 SHA-256 见 `output_hashes.json`。", ""]


def csv_tables(result: ExtendedNavResult, config: Round2Config
               ) -> tuple[tuple[str, Sequence[str], Sequence[Row]], ...]:
    return (("nav_metrics.csv", metric_header(config), metric_rows(result.nav, result.objects, config)),
            ("price_only_grid_nav.csv", metric_header(config), metric_rows(result.grid_nav, result.grid, config)),
            ("bear_markets.csv", PERIOD_HEADER, period_table(result)))
