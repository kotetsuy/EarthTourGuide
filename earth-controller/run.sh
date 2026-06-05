#!/usr/bin/env bash
# earth-controller 起動ラッパ。Playwright + system Chrome で earth.google.com を操作し、
# CDP スクリーンキャストのフレームを earth-bridge(8002) に流す。
#
# 前提: .venv に playwright と aiohttp が入っていること（下記 install で用意）。
# ヘッド付き起動なので DISPLAY が必要（リモートデスクトップ等）。
set -euo pipefail
cd "$(dirname "$0")"

# ヘッド付き Chrome 用のディスプレイ。未設定なら GNOME Remote Desktop の :10.0 を既定に。
export DISPLAY="${DISPLAY:-:10.0}"

if [[ ! -x .venv/bin/python ]]; then
    echo "[earth-controller] .venv がありません。'uv venv && uv pip install playwright aiohttp' を実行してください" >&2
    exit 1
fi

exec ./.venv/bin/python controller.py "$@"
