#!/bin/zsh
# 啟動本機 AI 控制台（只聽 127.0.0.1）。用法：./start.sh  然後開 http://127.0.0.1:8765
cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8765}"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
