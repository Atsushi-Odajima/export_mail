#!/bin/sh
# 開発用：Python で直接起動（macOS / Linux）
cd "$(dirname "$0")/.." && . .venv/bin/activate && exec python run_app.py "$@"
