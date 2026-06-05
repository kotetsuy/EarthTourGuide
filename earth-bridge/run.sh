#!/usr/bin/env bash
# earth-bridge 起動ラッパ（port 8002）。aiohttp のみ依存。
# 専用 venv が無ければ earth-controller/.venv を流用する。
set -euo pipefail
cd "$(dirname "$0")"

if [[ -x .venv/bin/python ]]; then
    PY=.venv/bin/python
elif [[ -x ../earth-controller/.venv/bin/python ]]; then
    PY=../earth-controller/.venv/bin/python
else
    echo "[earth-bridge] python venv が見つかりません。aiohttp 入りの venv を用意してください" >&2
    exit 1
fi

exec "$PY" bridge.py
