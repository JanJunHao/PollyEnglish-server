"""Polly 内容采集 sourcing agent（阶段 2 — Q3）。

agent = LLM 驱动、负责「动脑判断」的程序；它把现有确定性连接器（fetch_* 脚本）
和 ingest.py 当工具调用，不替换它们。详见同目录 README.md。
"""
