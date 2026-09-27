"""Windows 输出编码：GBK 控制台或管道下所有命令都能输出"−""≤"等字符。"""

from __future__ import annotations

import io
import os
import subprocess
import sys

from typer.testing import CliRunner

from market_risk.cli import app, configure_output


def test_configure_output_switches_gbk_stream_to_utf8():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk")
    configure_output((stream,))
    stream.write("结论−≤σ")
    stream.flush()
    assert raw.getvalue().decode("utf-8") == "结论−≤σ"


def test_configure_output_leaves_utf8_and_unsupported_streams():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    configure_output((stream,))
    assert stream.errors == "strict"
    configure_output((object(),))          # 没有 reconfigure：跳过，不报错


def test_cli_command_prints_minus_sign_under_gbk_pipe():
    """子进程以 GBK 作为管道编码运行真实的 CLI 入口，输出含"−"的内容不报错。"""
    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    code = ("import sys; import typer; import market_risk.cli as c\n"
            "@c.app.command('echo-test')\n"
            "def _t() -> None:\n    typer.echo('差值 −0.41 ≤ 0.02')\n"
            "sys.argv = ['market-risk', 'echo-test']\n"
            "c.app()\n")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert out.stdout.decode("utf-8").strip() == "差值 −0.41 ≤ 0.02"


def test_cli_runner_still_works():
    result = CliRunner().invoke(app, ["samples", "--year", "2025"])
    assert result.exit_code == 0 and "2025-" in result.output
