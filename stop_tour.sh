#!/usr/bin/env bash
# 実行中のツアーを停止するスクリプト。
# 前提: ./start_all.sh で tour サービス(:8003)が起動済みであること。
#
# 使い方:
#   ./stop_tour.sh
#
# 開始(無限ループ): ./start_tour_loop.sh
set -euo pipefail

TOUR_HOST="${TOUR_HOST:-localhost}"
TOUR_PORT="${TOUR_PORT:-8003}"
BASE="http://${TOUR_HOST}:${TOUR_PORT}"

log()  { printf '\033[1;34m[tour]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[tour]\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null || die "curl がありません"

# tour サービスが生きているか確認
curl -sf -m3 -o /dev/null "${BASE}/health" \
    || die "tour サービス(${BASE})に繋がりません。先に ./start_all.sh を実行してください"

log "ツアーを停止します"
resp=$(curl -sf -m5 -X POST "${BASE}/tour/stop") \
    || die "停止に失敗しました"

echo "$resp"
log "停止しました"
