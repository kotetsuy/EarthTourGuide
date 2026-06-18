#!/usr/bin/env bash
# world.json（など）のツアーを無限ループで開始するスクリプト。
# 前提: ./start_all.sh で tour サービス(:8003)が起動済みであること。
#
# 使い方:
#   ./start_tour_loop.sh           # world ツアーを無限ループで開始
#   ./start_tour_loop.sh kyoto     # 別ツアー id を指定して無限ループ開始
#
# 停止: curl -X POST http://localhost:8003/tour/stop
set -euo pipefail

TOUR_HOST="${TOUR_HOST:-localhost}"
TOUR_PORT="${TOUR_PORT:-8003}"
TOUR_ID="${1:-world}"
BASE="http://${TOUR_HOST}:${TOUR_PORT}"

log()  { printf '\033[1;34m[tour]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[tour]\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null || die "curl がありません"

# tour サービスが生きているか確認
curl -sf -m3 -o /dev/null "${BASE}/health" \
    || die "tour サービス(${BASE})に繋がりません。先に ./start_all.sh を実行してください"

log "ツアー '${TOUR_ID}' を無限ループで開始します"
resp=$(curl -sf -m5 -X POST "${BASE}/tour/start" \
    -H 'Content-Type: application/json' \
    -d "{\"id\":\"${TOUR_ID}\",\"loop\":true}") \
    || die "開始に失敗しました（id='${TOUR_ID}' は存在しますか? ${BASE}/tour/list で確認）"

echo "$resp"
log "停止するには: curl -X POST ${BASE}/tour/stop"
