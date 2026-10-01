"""v1.4 第二轮开发期诊断的表格行与报告（纯计算，描述性，不参与任何判定）。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

from market_risk.wavewarn.config_v14 import Round2Config
from market_risk.wavewarn.diagnostics_round2 import (
    LIGHTS,
    MA200,
    PORTFOLIO,
    SELECTED,
    NavRow,
    ObjectDiagnostics,
    Round2Result,
)
from market_risk.wavewarn.evaluation import SYMBOLS
from market_risk.wavewarn.evaluation_v13_report import share, table
from market_risk.wavewarn.green_report import CLASS_1, CLASS_3, GreenEvent, GreenSummary
from market_risk.wavewarn.nav import Drawdown, wealth_path
from market_risk.wavewarn.switch_diagnostics import (
    Switch,
    TimingSplit,
    break_even_gamma,
    exposure_change,
    first_day_switches,
    holding_summary,
    reversal_count,
    switches_by_type,
    switches_by_year,
)

Row = tuple[object, ...]
OPENING = "本报告仅使用开发期数据；诊断不改变模型规则、参数、选定设定与γ。"
BW_NOTE = ("选定的 v1.4 与“v1.4 去掉 MR”两个对象含 BW 通道，其结果基于偏离规格的实现（BW 退出谓词，"
           "见 `docs/research/v1.4_一致性审计.md`），尚未在修正实现下核验；"
           "200 日均线、满仓、现金与恒定暴露各行不含 BW。")
REPORT_NAME = "诊断报告.md"
CLUSTER_START, CLUSTER_END = dt.date(2011, 7, 1), dt.date(2011, 12, 31)    # 补充登记 B.5 所指的同一动荡期


def _blank(value: object) -> object:
    return "" if value is None else value


def _yes(value: bool) -> str:
    return "是" if value else "否"


def plain(value: object) -> str:
    """报告里的计数类数值：去掉无意义的尾随零；空值显示为“—”。"""
    if value is None or value == "":
        return "—"
    if isinstance(value, Decimal):
        return "超限" if value.is_infinite() else f"{value.normalize():f}"
    return str(value)


def baseline_split(result: Round2Result) -> TimingSplit:
    return next(item.split for item in result.objects if item.name == MA200)


def years(result: Round2Result, config: Round2Config) -> Decimal:
    """评价窗口折合的年数：区间数 ÷ 年交易日数。"""
    return Decimal(len(result.days) - 1) / config.trading_days_per_year


OBJECT_HEADER = ("object", "intervals", "mean_exposure", "initial_light", "initial_exposure", "timing_score",
                 "price_score", "switch_cost", "switches", "break_even_gamma_vs_ma200", "exposure_change_total",
                 "exposure_change_per_year", "switches_excluding_first_day", "exposure_change_excluding_first_day")


def old_convention(switches: Sequence[Switch]) -> tuple[int, Decimal]:
    """旧口径（不计 j₀ 当日相对前一日的切换）下的切换次数与目标暴露变化量，只用来标出差异。"""
    first = first_day_switches(switches)
    return len(switches) - len(first), exposure_change(switches) - exposure_change(first)


def object_rows(result: Round2Result, config: Round2Config) -> tuple[Row, ...]:
    baseline = baseline_split(result)
    rows = []
    for item in result.objects:
        change = exposure_change(item.switches)
        gamma = None if item.name == MA200 else break_even_gamma(item.split, baseline)
        rows.append((item.name, item.timing.intervals, item.timing.mean_exposure, item.initial_light,
                     item.initial_exposure, item.split.score, item.split.price_score, item.split.switch_cost,
                     item.split.switches, _blank(gamma), change, change / years(result, config),
                     *old_convention(item.switches)))
    return tuple(rows)


YEAR_HEADER = ("object", "year", "switches")
TYPE_HEADER = ("object", "from", "to", "direction", "switches")
REVERSAL_HEADER = ("object", "window", "switches", "reversed_within_window", "share")
SEGMENT_HEADER = ("object", "light", "first_interval", "last_interval", "length", "truncated_by_window")
HOLDING_HEADER = ("object", "light", "segments", "median", "p25", "p75", "maximum", "truncated_segments")


def year_rows(result: Round2Result) -> tuple[Row, ...]:
    """按执行日所在年份；窗口内每个年份都列出，没有切换的年份记 0。"""
    all_years = sorted({day.year for day in result.days[1:-1]})
    rows = []
    for item in result.objects:
        counts = switches_by_year(item.switches)
        rows.extend((item.name, year, counts.get(year, 0)) for year in all_years)
    return tuple(rows)


def type_rows(result: Round2Result) -> tuple[Row, ...]:
    rank = {light: order for order, light in enumerate(LIGHTS)}
    return tuple((item.name, before, after, "暴露下降" if rank[after] > rank[before] else "暴露上升", count)
                 for item in result.objects for (before, after), count in switches_by_type(item.switches).items())


def reversal_rows(result: Round2Result, config: Round2Config) -> tuple[Row, ...]:
    rows = []
    for item in result.objects:
        for window in config.reversal_windows:
            count = reversal_count(item.switches, window)
            rows.append((item.name, window, len(item.switches), count,
                         Decimal(count) / len(item.switches) if item.switches else ""))
    return tuple(rows)


def segment_rows(result: Round2Result) -> tuple[Row, ...]:
    return tuple((item.name, segment.light, segment.start, segment.end, segment.length, _yes(segment.truncated))
                 for item in result.objects for segment in item.segments)


def holding_rows(result: Round2Result) -> tuple[Row, ...]:
    rows = []
    for item in result.objects:
        for light in LIGHTS:
            summary = holding_summary(item.segments, light)
            rows.append((item.name, light, summary.count, _blank(summary.median), _blank(summary.p25),
                         _blank(summary.p75), _blank(summary.maximum), summary.truncated))
    return tuple(rows)


GREEN_SUMMARY_HEADER = ("object", "symbol", "included", "class_1", "class_2", "class_3", "class_1_share",
                        "class_2_share", "class_3_share", "class_3_next_event", "class_3_window_end",
                        "class_3_next_event_share", "complete_window_events", "green_within_window",
                        "within_share_all_included", "green_within_window_complete",
                        "within_share_complete_window", "median_delay_class_2", "median_delay_conservative")
GREEN_EVENT_HEADER = ("object", "symbol", "peak_date", "trough_date", "class", "class_3_reason",
                      "complete_window", "first_green_execution", "delay", "not_green_through_window_end_days",
                      "greens_before_trough", "deepest_decline_after_early_green", "green_within_window")


def _shown(value: Decimal | None) -> object:
    if value is None:
        return ""
    return "超限" if value.is_infinite() else value


def green_summary_row(name: str, summary: GreenSummary) -> Row:
    return (name, summary.symbol, summary.included, summary.class_1, summary.class_2, summary.class_3,
            _blank(summary.share(summary.class_1)), _blank(summary.share(summary.class_2)),
            _blank(summary.share(summary.class_3)), summary.class_3_next_event, summary.class_3_window_end,
            _blank(summary.share(summary.class_3_next_event)), summary.complete_window, summary.within_window,
            _blank(summary.within_share_all), summary.within_window_complete,
            _blank(summary.within_share_complete), _shown(summary.median_class_2),
            _shown(summary.median_conservative))


def green_summary_rows(result: Round2Result) -> tuple[Row, ...]:
    return tuple(green_summary_row(item.name, item.green_summaries[symbol])
                 for item in result.objects for symbol in SYMBOLS)


def green_event_row(name: str, event: GreenEvent) -> Row:
    return (name, event.symbol, event.peak_date, event.trough_date, event.rebound_class, event.class_3_reason,
            _yes(event.complete_window), _blank(event.first_green), _blank(event.delay),
            _blank(event.unobserved_days), event.half_way_green_count, _blank(event.deepest_decline),
            _yes(event.within_window))


def green_event_rows(result: Round2Result) -> tuple[Row, ...]:
    return tuple(green_event_row(item.name, event)
                 for item in result.objects for symbol in SYMBOLS for event in item.green[symbol])


def nav_header(config: Round2Config) -> tuple[str, ...]:
    rolling = tuple(f"worst_{window}_{name}" for window in config.rolling_windows
                    for name in ("return", "start", "end"))
    return ("object", "scope", "intervals", "cumulative_return", "annualized_return", "max_drawdown",
            "drawdown_peak_date", "drawdown_trough_date", "drawdown_decline_days", "drawdown_recovery_date",
            "drawdown_recovery_days", "days_from_trough_to_window_end", *rolling, "annualized_volatility",
            "switches", "exposure_change")


def nav_row(row: NavRow) -> Row:
    metrics = row.metrics
    rolling = tuple(cell for item in metrics.rolling for cell in (_blank(item.value), _blank(item.start),
                                                                    _blank(item.end)))
    drawdown = metrics.drawdown
    return (row.name, row.scope, metrics.intervals, metrics.cumulative, metrics.annualized, drawdown.depth,
            drawdown.peak_date, drawdown.trough_date, drawdown.decline_days, _blank(drawdown.recovery_date),
            _blank(drawdown.recovery_days), drawdown.days_after_trough, *rolling, metrics.volatility, row.switches,
            row.exposure_change)


def recovery_text(drawdown: Drawdown, last_label: str) -> str:
    """谷底到恢复前高的交易日数；所给区间内未恢复时注明截至区间末日已过去多少个交易日。"""
    if not drawdown.depth:
        return "—"
    if drawdown.recovery_days is None:
        return f"截至{last_label}未恢复（已过 {drawdown.days_after_trough} 日）"
    return f"{drawdown.recovery_days}（{drawdown.recovery_date} 恢复）"


def nav_daily(result: Round2Result, places: Decimal) -> tuple[tuple[str, ...], tuple[Row, ...]]:
    """逐日净值（宽表）：每个对象、每个口径一列，W_0 = 1；数值四舍五入到 places。"""
    header = ("date", *(f"{row.name}|{row.scope}" for row in result.nav))
    paths = [wealth_path(row.returns) for row in result.nav]
    return header, tuple((day, *(path[index].quantize(places) for path in paths))
                         for index, day in enumerate(result.days))


def percent(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def scope_lines(result: Round2Result, config: Round2Config) -> list[str]:
    first, last = result.days[0], result.days[-1]
    rows = [(item.name, item.timing.intervals, item.timing.mean_exposure, item.initial_light,
             plain(item.initial_exposure)) for item in result.objects]
    return [
        "# v1.4 第二轮开发期诊断（描述性，不参与任何判定）", "", OPENING, "",
        f"- 评价窗口：j₀ = {first} 至最后一个 next_date ≤ {last} 的区间（最后一个区间为 {result.days[-2]} 收盘至 "
        f"{last} 收盘），共 {len(result.days) - 1} 个区间。输入只读到 {last}，没有使用其后的任何价格。",
        "- 对象：选定的 v1.4（K=5，θ_P=2.5%）、同参数的“v1.4 去掉 MR”、200 日均线；完整净值另加满仓、现金与两条"
        "同平均暴露基准。各对象的状态序列与损失直接取自开发期评价的同一条计算路径，没有改动任何信号。",
        "- 初始暴露继承此前的信号（模型自 t0 连续运行）。切换次数与目标暴露变化量按主损失的计费口径："
        "j₀ 当日执行灯色与前一日不同时计为一次切换（j₀ 前一日已有执行状态，这不是初始建仓），窗口末日的切换不计费；"
        "各对象的切换次数与主损失计费的次数一致（程序核对）。与旧口径（不计 j₀ 当日）的差异见第二节。",
        f"- {BW_NOTE}",
        f"- “年均”与年化按每年 {config.trading_days_per_year} 个交易日折算，"
        f"窗口折合 {years(result, config):.3f} 年。", "",
        "## 一、对象与初始暴露", "",
        *table(("对象", "区间数", "平均执行暴露 ē", "j₀ 的执行灯色", "j₀ 的初始暴露"), rows), ""]


def switch_count_lines(result: Round2Result, config: Round2Config) -> list[str]:
    """按年、按转换类型与短期反转。"""
    names = [item.name for item in result.objects]
    all_years = sorted({row[1] for row in year_rows(result)})
    by_year = {(row[0], row[1]): row[2] for row in year_rows(result)}
    year_table = [(year, *(by_year[name, year] for name in names)) for year in all_years]
    year_table.append(("合计", *(len(item.switches) for item in result.objects)))
    by_type = {(row[0], row[1], row[2]): row for row in type_rows(result)}
    kinds = list(dict.fromkeys((row[1], row[2], row[3]) for row in type_rows(result)))
    type_table = [(f"{before}→{after}", direction, *(by_type[name, before, after][4] for name in names))
                  for before, after, direction in kinds]
    reversal = [(row[0], row[1], row[2], row[3], share(row[4]) if row[4] != "" else "—")
                for row in reversal_rows(result, config)]
    return ["## 二、切换诊断", "", "### 按年切换次数（按执行日归年）", "",
            *table(("年份", *names), year_table), "",
            "### 按转换类型", "",
            "登记列出五类；红→绿只可能出现在没有黄灯的 200 日均线，另列一行。", "",
            *table(("转换", "方向", *names), type_table), "",
            "### 短期反转", "",
            "暴露上升（红→黄、黄→绿、红→绿）与暴露下降（绿→黄、黄→红、绿→红）互为反方向。"
            "对每次切换，判断随后 N 个交易日内是否至少出现一次反方向切换；每次起始切换在每个窗口最多计一次。"
            "两个窗口分别报告，不相加。"
            "临近窗口末日的切换后续观察不足 N 日，照实计数。", "",
            *table(("对象", "窗口（交易日）", "切换次数", "其中随后出现反方向切换", "占比"), reversal), ""]


def holding_lines(result: Round2Result, config: Round2Config) -> list[str]:
    """执行段持有时长与目标暴露变化量。"""
    holding = [(row[0], row[1], row[2], plain(row[3]), plain(row[4]), plain(row[5]), plain(row[6]), row[7])
               for row in holding_rows(result)]
    ends = [(item.name, f"{item.segments[0].light}，{item.segments[0].length} 个区间，"
             f"{'被窗口起点截断' if item.segments[0].truncated else '未截断'}",
             f"{item.segments[-1].light}，{item.segments[-1].length} 个区间，"
             f"{'被窗口末日截断' if item.segments[-1].truncated else '未截断'}") for item in result.objects]
    change = [(row[0], row[8], plain(row[10]), f"{row[11]:.2f}", row[12], plain(row[13]),
               "无差异" if (row[8], row[10]) == (row[12], row[13]) else "有差异")
              for row in object_rows(result, config)]
    return ["### 执行段持有时长", "",
            "每段连续相同执行灯色的长度（区间数，即持有的交易日数）；分位数为线性插值。首段与末段若被评价窗口截断，"
            "真实时长不短于表中数值；它们仍计入分布，并在“其中被截断”一列标出。", "",
            *table(("对象", "灯色", "段数", "中位数", "P25", "P75", "最大值", "其中被截断"), holding), "",
            *table(("对象", "首段", "末段"), ends), "",
            "### 目标暴露变化量 Σ|Δe|", "",
            "只累计窗口内每次切换的目标暴露变化（绿 1、黄 0.5、红 0），含 j₀ 当日相对前一日的变化。"
            "不含每日再平衡交易，不能直接乘费率得到全部交易费用。"
            "后三列为旧口径（不计 j₀ 当日的切换）下的数值及其与现口径的比较。", "",
            *table(("对象", "切换次数", "目标暴露变化量（全期）", "目标暴露变化量（年均）", "旧口径切换次数",
                    "旧口径目标暴露变化量", "与旧口径比较"), change), ""]


def split_lines(result: Round2Result, config: Round2Config) -> list[str]:
    """T 拆为 T价格 与切换项，并列盈亏平衡 γ。"""
    rows = [(row[0], row[5], row[6], row[7], row[8], row[9] if row[9] != "" else "—")
            for row in object_rows(result, config)]
    return ["### 安全代理择时得分 T 的拆分", "",
            "T = T价格 + 切换项，切换项 = γ × 切换次数（γ=0.005）。盈亏平衡 γ* 是使该对象与 200 日均线的 T 相等的每次"
            "切换罚分：γ 低于 γ* 时该对象的 T 更低（更好），高于 γ* 时相反。它是比较结果的翻转点，不是推荐参数，"
            "也不是实测交易成本。", "",
            *table(("对象", "T", "T价格", "切换项", "切换次数", "相对 200 日均线的盈亏平衡 γ*"), rows), "",
            "在已查看的开发期及既定安全代理损失下，未计切换罚分的得分优于均线；计入登记罚分后，选定设定落后。"
            "尚不能由此证明独立样本中的择时优势。", ""]


def cluster_sentence(result: Round2Result) -> str:
    """补充登记 B.5 的表述：选定设定在 2011 年下半年（按低点日期）的类别③事件数由数据算出。"""
    selected = next(item for item in result.objects if item.name == SELECTED)
    counts = {symbol: sum(event.rebound_class == CLASS_3 and CLUSTER_START <= event.trough_date <= CLUSTER_END
                          for event in selected.green[symbol]) for symbol in SYMBOLS}
    cells = "、".join(f"{symbol} {counts[symbol]} 件" for symbol in SYMBOLS)
    return (f"2011 年下半年的类别③事件（选定的 v1.4：{cells}）集中于同一动荡期、存在跨资产重叠与依赖，"
            f"并非 {sum(counts.values())} 个独立案例。")


def green_lines(result: Round2Result, config: Round2Config) -> list[str]:
    """转绿双层报告：汇总表（全部对象）与选定 v1.4 的逐事件表。"""
    window = config.green_window
    summary = [(row[0], row[1], row[2], f"{row[3]}（{share(row[6])}）", f"{row[4]}（{share(row[7])}）",
                f"{row[5]}（{share(row[8])}）", f"{row[9]} / {row[10]}", share(row[11]), row[12],
                f"{row[13]}（{share(row[14])}）", f"{row[15]}（{share(row[16])}）", plain(row[17]), plain(row[18]))
               for row in green_summary_rows(result)]
    lines = ["## 三、转绿双层报告", "",
             "纳入高点 P ≥ τ 的已确认事件（同开发期评价）。类别①“低点前已绿”单列：它不是转绿迟到，不进入延迟分布。"
             "类别③的原因：(a) 下一事件 T0 先到；(b) 评价窗口结束，尚未观察到转绿。"
             f"“完整观察窗口”指 Tr 之后第 {window} 个交易日的执行日不晚于窗口最后一日；"
             "观察不足的事件不等于已观察到的恢复失败。"
             f"“{window} 日内转绿”指类别②且延迟 ≤ {window}，给两个分母：全部纳入事件；只含有完整观察窗口的事件。", "",
             "类别②中位数是有条件的统计，不代表全部事件的恢复速度；保守口径把类别③按无穷计入（类别①不计入），"
             "只能解释为“在下一事件前未转绿”的保守综合口径，不代表真实等待时间无穷。"
             + cluster_sentence(result), "",
             *table(("对象", "资产", "纳入", "①（占比）", "②（占比）", "③（占比）", "③原因 a / b", "下一事件先到占比",
                     "有完整观察窗口", f"{window} 日内转绿（÷全部纳入）", f"{window} 日内转绿（÷完整窗口）",
                     "②延迟中位（有条件）", "保守口径中位"), summary), ""]
    selected = next(item for item in result.objects if item.name == SELECTED)
    for symbol in SYMBOLS:
        lines.extend([f"### 选定的 v1.4：{symbol} 逐事件", "", *event_table(selected, symbol, result), ""])
    lines.extend(["其余对象的逐事件表见 `green_events.csv`。", ""])
    return lines


def event_table(item: ObjectDiagnostics, symbol: str, result: Round2Result) -> list[str]:
    """逐事件：实际首次转绿执行日与延迟（类别③也列出）、低点前转绿次数及随后最深跌幅。"""
    rows = []
    for event in item.green[symbol]:
        if event.rebound_class == CLASS_1:
            first, delay = "低点前已绿", "—"
        elif event.first_green is None:
            first, delay = f"截至窗口末日 {result.days[-1]} 未转绿", f"已过 {event.unobserved_days} 日"
        else:
            first, delay = str(event.first_green), str(event.delay)
        rows.append((event.peak_date, event.trough_date, event.rebound_class[0], event.class_3_reason or "—",
                     _yes(event.complete_window), first, delay, event.half_way_green_count,
                     share(event.deepest_decline) if event.deepest_decline is not None else "—"))
    return table(("高点 P", "低点 Tr", "类别", "③的原因", "完整观察窗口", "实际首次转绿执行日", "延迟（交易日）",
                  "低点前转绿次数", "其后最深跌幅"), rows)


def nav_lines(result: Round2Result, config: Round2Config) -> list[str]:
    """完整净值：双资产与两个单资产各一张表。"""
    lines = ["## 四、完整净值", "",
             "全部 next_date ≤ 开发期末的区间都计入，不因标签不可判定而删除。"
             "单资产当日收益 R_{a,j} = e_j·(exp(r_{a,j}) − 1)；"
             "双资产每日收盘按“总暴露 e_j、两资产各半”再平衡，R_j = Σ_a (e_j/2)·(exp(r_{a,j}) − 1)；现金收益为 0；"
             "W_{j+1} = W_j·(1 + R_j)，W_0 = 1。", "",
             "回撤区分两种：全程净值曲线上的最大回撤（相对此前最高点）；"
             "在既定区间起点把净值重新记为 1 之后的区间内回撤。"
             "开发期只有“全窗口”这一个既定区间，净值在 j₀ 记为 1，两种口径在这里是同一个数，下表只列一次；"
             "恢复指谷底之后净值首次回到或超过峰值，窗口内没有恢复的注明截至窗口末日已过去的交易日数。", "",
             "口径：SPX 为价格指数，QQQ 为不含分红的 ETF 价格；不含分红、现金收益为 0、未计真实交易费用与再平衡交易；"
             "γ 是代理损失罚分，不扣入净值。年化收益 = W_N^(年交易日数 ÷ N) − 1；年化波动率 = 日收益的样本标准差"
             "（分母 N−1）× √年交易日数；最大回撤 = 1 − 谷值 ÷ 此前最高值；"
             "最差滚动 k 日收益 = min W_{t+k} ÷ W_t − 1。", ""]
    header = ("对象", "累计收益", "年化收益", "最大回撤（峰值日至谷底日）", "峰值到谷底（交易日）",
              "谷底到恢复前高（交易日）",
              *(f"最差滚动 {window} 日" for window in config.rolling_windows), "年化波动率", "切换次数",
              "目标暴露变化量")
    for scope in (PORTFOLIO, *SYMBOLS):
        rows = []
        for row in result.nav:
            if row.scope != scope:
                continue
            metrics = row.metrics
            drawdown = (f"{percent(metrics.drawdown.depth)}（{metrics.drawdown.peak_date} 至 "
                        f"{metrics.drawdown.trough_date}）" if metrics.drawdown.depth else "0.00%")
            rows.append((row.name, percent(metrics.cumulative), percent(metrics.annualized), drawdown,
                         metrics.drawdown.decline_days if metrics.drawdown.depth else "—",
                         recovery_text(metrics.drawdown, "窗口末日"),
                         *(percent(item.value) for item in metrics.rolling), percent(metrics.volatility),
                         row.switches, plain(row.exposure_change)))
        lines.extend([f"### {scope}", "", *table(header, rows), ""])
    lines.extend(["最差滚动收益的起止日、逐日净值见 `nav_metrics.csv` 与 `nav_daily.csv.gz`。", ""])
    return lines


def report_lines(result: Round2Result, config: Round2Config) -> list[str]:
    return [*scope_lines(result, config), *switch_count_lines(result, config), *holding_lines(result, config),
            *split_lines(result, config), *green_lines(result, config), *nav_lines(result, config)]


def csv_tables(result: Round2Result, config: Round2Config) -> tuple[tuple[str, Sequence[str], Sequence[Row]], ...]:
    """全部 CSV 表：文件名、表头与行。"""
    return (("objects.csv", OBJECT_HEADER, object_rows(result, config)),
            ("switch_by_year.csv", YEAR_HEADER, year_rows(result)),
            ("switch_by_type.csv", TYPE_HEADER, type_rows(result)),
            ("switch_reversals.csv", REVERSAL_HEADER, reversal_rows(result, config)),
            ("holding_segments.csv", SEGMENT_HEADER, segment_rows(result)),
            ("holding_summary.csv", HOLDING_HEADER, holding_rows(result)),
            ("green_summary.csv", GREEN_SUMMARY_HEADER, green_summary_rows(result)),
            ("green_events.csv", GREEN_EVENT_HEADER, green_event_rows(result)),
            ("nav_metrics.csv", nav_header(config), tuple(nav_row(row) for row in result.nav)))
