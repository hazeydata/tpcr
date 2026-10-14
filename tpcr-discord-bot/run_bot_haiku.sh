#!/bin/bash
cd "$(dirname "$0")"
export USE_OLLAMA=0
export PYTHONUNBUFFERED=1
exec /home/wilma/theme-park-crowd-report/.venv/bin/python bot.py
