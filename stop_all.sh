#!/usr/bin/env bash
# EarthTourGuide パイプライン停止スクリプト。
#
#   ./stop_all.sh                 → tmux セッション + VOICEVOX コンテナを停止
#   ./stop_all.sh --keep-voicevox → VOICEVOX は動かしたまま残す
#
# Chrome は閉じない（ユーザの操作を奪わない）。Earth 操作用の headed Chrome は
# earth-controller プロセスを止めれば一緒に閉じる。
set -euo pipefail

SESSION="earthtour"
VOICEVOX_CONTAINER="voicevox_engine"
KEEP_VOICEVOX=0

for arg in "$@"; do
    case "$arg" in
        --keep-voicevox) KEEP_VOICEVOX=1 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[1;34m[stop]\033[0m %s\n' "$*"; }

# ---- 1. tmux セッション ----------------------------------------------
if tmux has-session -t "$SESSION" 2>/dev/null; then
    log "tmux セッション ${SESSION} を終了します"
    tmux kill-session -t "$SESSION"
else
    log "tmux セッション ${SESSION} は起動していません"
fi

# ---- 2. 取りこぼしプロセス -------------------------------------------
PATTERNS=(
    "llama.cpp/build/bin/llama-server"
    "EarthTourGuide/ttllm/server:app"
    "EarthTourGuide/three-vrm/server.py"
    "EarthTourGuide/earth-bridge/bridge.py"
    "EarthTourGuide/earth-controller/controller.py"
    "EarthTourGuide/tour/tour_service.py"
)
for pat in "${PATTERNS[@]}"; do
    pids=$(pgrep -f "$pat" || true)
    if [[ -n "${pids}" ]]; then
        log "残存プロセスを停止: $pat (pid=${pids//$'\n'/,})"
        # shellcheck disable=SC2086
        kill ${pids} 2>/dev/null || true
        sleep 1
        pids=$(pgrep -f "$pat" || true)
        # shellcheck disable=SC2086
        [[ -n "${pids}" ]] && kill -9 ${pids} 2>/dev/null || true
    fi
done

# ---- 3. VOICEVOX docker ----------------------------------------------
if (( KEEP_VOICEVOX == 0 )); then
    if docker ps --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
        log "VOICEVOX コンテナ (${VOICEVOX_CONTAINER}) を停止します"
        docker stop "$VOICEVOX_CONTAINER" >/dev/null
    else
        log "VOICEVOX コンテナは既に停止しています"
    fi
else
    log "VOICEVOX は残します (--keep-voicevox)"
fi

log "停止完了"
