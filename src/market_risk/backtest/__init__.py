"""阶段6 逐日历史回测：引擎（engine）、配置（settings）、回调识别（zigzag，标签）、结果标签（labels）、报告（report）。

隔离：zigzag、labels 使用基准日之后的数据，属于标签，评分路径（scoring、prepare、pipeline、snapshot、market、
metrics）不得导入它们（tests/test_no_lookahead.py 有传递依赖检查）。
"""
