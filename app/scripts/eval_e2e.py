"""指针：端到端评测脚本在 `scripts/eval_e2e.py`〔阶段 9 · §12.4〕

放那儿是因为它和 `run_e2e_live.py`、`probe_*.py` 同属"手动跑的活体脚本"（要联网、花 token、
不进 `pytest`），而 `app/scripts/` 装的是随包分发的命令（`seed`、`hello_llm`）。
跑法：

    .venv/Scripts/python.exe -X utf8 scripts/eval_e2e.py [简历目录] [JD 文件]
"""
