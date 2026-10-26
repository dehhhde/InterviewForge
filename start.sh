#!/usr/bin/env bash
# InterviewForge 启动脚本（Git Bash / POSIX sh）。
# 为什么要这么一层：本机没有 make/uv，裸 `python` 会以退出码 49 失败，而 PYTHONPATH 与
# `-X utf8` 少一个就起不来——GBK 控制台下 alembic/config 读中文路径会抛 UnicodeDecodeError。
# 把这些固定死，别再让每次手敲都复述一遍踩坑清单。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PY=".venv/Scripts/python.exe"
if [[ ! -x "$PY" ]]; then
  echo "找不到 $PY。先建虚拟环境并装依赖：" >&2
  echo "  py -3.14 -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt" >&2
  exit 1
fi

# 开发库随仓库走；表不存在时页面能起、接口会 500，所以这里只提示不自动迁移——
# `alembic upgrade head` 会写 data/interview_forge.db，属于有后果的动作，交给敲命令的人。
if [[ ! -f data/interview_forge.db ]]; then
  echo "提示：data/interview_forge.db 不存在，先跑 PYTHONPATH=. $PY -X utf8 -m alembic upgrade head" >&2
fi

# 后端只读 frontend/dist；源码目录改了不会自动出现在页面上。
if [[ ! -f frontend/dist/index.html ]]; then
  echo "提示：frontend/dist 还没构建。构建控制台：cd frontend && npm install && npm run build" >&2
  echo "      只想调前端时用 cd frontend && npm run dev（3000 端口，/api 已代理到 8000）。" >&2
fi

PORT="${1:-8000}"
HOST="${HOST:-127.0.0.1}"

if [[ "${RELOAD:-0}" == "1" ]]; then
  # --reload 只盯 .py；前端改动要么 `npm run build`，要么另开 `npm run dev`
  exec env PYTHONPATH=. "$PY" -X utf8 -m uvicorn app.main:app --reload --host "$HOST" --port "$PORT"
fi

echo "InterviewForge → http://$HOST:$PORT/ （控制台），/docs 是接口文档"
exec env PYTHONPATH=. "$PY" -X utf8 -m uvicorn app.main:app --host "$HOST" --port "$PORT"
