#!/usr/bin/env bash
# EarthTourGuide パイプライン一括起動スクリプト。
# AIassistant の start_all.sh を踏襲し、Earth 2サービスを追加したもの。
#
# 起動順:
#   1. VOICEVOX (docker)              :50021
#   2. llama-server (qwen3.6)         :8080
#   3. ttllm (WhisperX ↔ llama)       :8001  → /warmup 叩く
#   4. earth-bridge (フレーム中継)    :8002
#   5. earth-controller (Earth 操作 + screencast)   ← bridge にフレーム供給
#   6. three-vrm (VRM + ライブ背景)   :8000
#   7. Chrome で zundamon.html を開く
#
# 各サービスは tmux セッション "earthtour" の別ウィンドウで走る。
#   tmux attach -t earthtour     (ログ)
#   tmux kill-session -t earthtour (全停止) もしくは ./stop_all.sh
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_SERVER="${PULSE_SERVER:-unix:${XDG_RUNTIME_DIR}/pulse/native}"
# ヘッド付き Chrome（earth-controller / 表示用）に必要なディスプレイ
export DISPLAY="${DISPLAY:-:10.0}"

SESSION="earthtour"
ROOT="/home/$USER/EarthTourGuide"

LLAMA_BIN="/home/$USER/llama.cpp/build/bin/llama-server"
QWEN_MODEL="/home/$USER/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
LLAMA_HOST="127.0.0.1"
LLAMA_PORT="8080"
LLAMA_CTX="8192"
LLAMA_NGL="99"

VOICEVOX_CONTAINER="voicevox_engine"
VOICEVOX_IMAGE="voicevox/voicevox_engine:cpu-ubuntu20.04-latest"

TTLLM_DIR="${ROOT}/ttllm"
THREE_VRM_DIR="${ROOT}/three-vrm"
EARTH_BRIDGE_DIR="${ROOT}/earth-bridge"
EARTH_CONTROLLER_DIR="${ROOT}/earth-controller"

BROWSER_URL="http://localhost:8000/zundamon.html"

# gfx1151 (Ryzen AI Max+ 395) 向け ROCm env。
export HSA_OVERRIDE_GFX_VERSION="${HSA_OVERRIDE_GFX_VERSION:-11.5.1}"
export ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
export AMDGPU_TARGETS="${AMDGPU_TARGETS:-gfx1151}"
export LD_LIBRARY_PATH="/usr/local/lib:/opt/rocm/lib:/opt/rocm/lib/llvm/lib:${LD_LIBRARY_PATH:-}"

# ---- helpers ------------------------------------------------------------
log()  { printf '\033[1;34m[launch]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[launch]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[launch]\033[0m %s\n' "$*" >&2; exit 1; }

wait_http() {
    local name="$1" url="$2" timeout="${3:-120}" start now
    start=$(date +%s)
    log "waiting for ${name} (${url}) ..."
    while true; do
        if curl -sf -o /dev/null -m 2 "$url"; then
            log "  ${name} is up"; return 0
        fi
        now=$(date +%s)
        (( now - start > timeout )) && die "${name} did not come up within ${timeout}s"
        sleep 2
    done
}

new_window() {
    local name="$1" cmd="$2"
    tmux new-window -t "$SESSION" -n "$name"
    tmux send-keys -t "${SESSION}:${name}" "$cmd" C-m
}

# ---- preflight ----------------------------------------------------------
command -v tmux          >/dev/null || die "tmux がありません"
command -v docker        >/dev/null || die "docker がありません"
command -v curl          >/dev/null || die "curl がありません"
command -v google-chrome >/dev/null || warn "google-chrome が見つかりません (Chrome 起動はスキップ)"

[[ -x "$LLAMA_BIN"                ]] || die "llama-server が見つかりません: $LLAMA_BIN"
[[ -f "$QWEN_MODEL"               ]] || die "Qwen モデルが見つかりません: $QWEN_MODEL"
[[ -x "$TTLLM_DIR/run.sh"         ]] || die "ttllm/run.sh がありません"
[[ -d "$THREE_VRM_DIR"            ]] || die "three-vrm ディレクトリがありません"
[[ -x "$EARTH_BRIDGE_DIR/run.sh"  ]] || die "earth-bridge/run.sh がありません"
[[ -x "$EARTH_CONTROLLER_DIR/run.sh" ]] || die "earth-controller/run.sh がありません"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    log "既存の tmux セッション ${SESSION} を終了します"
    tmux kill-session -t "$SESSION"
fi

# ---- 1. VOICEVOX (docker) ----------------------------------------------
log "VOICEVOX コンテナ (${VOICEVOX_CONTAINER}) を起動します"
if docker ps --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
    log "  すでに running"
elif docker ps -a --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
    docker start "$VOICEVOX_CONTAINER" >/dev/null
else
    log "  コンテナが無いので新規作成します"
    docker run -d --name "$VOICEVOX_CONTAINER" --restart unless-stopped \
        -p 50021:50021 "$VOICEVOX_IMAGE" >/dev/null
fi

tmux new-session -d -s "$SESSION" -n voicevox \
    "docker logs -f --tail 50 ${VOICEVOX_CONTAINER}"
wait_http "VOICEVOX" "http://localhost:50021/version" 60

# ---- 2. llama-server ----------------------------------------------------
LLAMA_CMD="HSA_OVERRIDE_GFX_VERSION=${HSA_OVERRIDE_GFX_VERSION} \
ROCM_PATH=${ROCM_PATH} HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES} \
LD_LIBRARY_PATH=${LD_LIBRARY_PATH} \
${LLAMA_BIN} -m ${QWEN_MODEL} --host ${LLAMA_HOST} --port ${LLAMA_PORT} -ngl ${LLAMA_NGL} -c ${LLAMA_CTX} -fit off"
new_window "llama" "$LLAMA_CMD"
wait_http "llama-server" "http://${LLAMA_HOST}:${LLAMA_PORT}/health" 600

# ---- 3. ttllm -----------------------------------------------------------
new_window "ttllm" "cd ${TTLLM_DIR} && ./run.sh"
wait_http "ttllm" "http://localhost:8001/health" 60

log "WhisperX を warmup ..."
if curl -sf -X POST -m 300 http://localhost:8001/warmup -o /dev/null; then
    log "  warmup 完了"
else
    warn "  warmup に失敗 (初回転写が遅くなる可能性)"
fi

# ---- 4. earth-bridge (port 8002) ---------------------------------------
new_window "earth-bridge" "cd ${EARTH_BRIDGE_DIR} && ./run.sh"
wait_http "earth-bridge" "http://localhost:8002/health" 30

# ---- 5. earth-controller (Earth 操作 + screencast) ---------------------
# ヘッド付き Chrome を DISPLAY 上に出す。bridge にフレームを供給。
new_window "earth-controller" "cd ${EARTH_CONTROLLER_DIR} && DISPLAY=${DISPLAY} ./run.sh"
# controller の起動完了は controller_connected で確認（Earth ブートに時間がかかる）
log "earth-controller の bridge 接続を待ちます ..."
for i in $(seq 1 60); do
    if curl -sf -m 2 http://localhost:8002/health | grep -q '"controller_connected": true'; then
        log "  earth-controller connected"; break
    fi
    sleep 2
    (( i == 60 )) && warn "  controller が bridge に接続しません（手動確認してください）"
done

# ---- 6. three-vrm -------------------------------------------------------
new_window "three-vrm" "cd ${THREE_VRM_DIR} && python3 server.py"
wait_http "three-vrm" "http://localhost:8000/status" 30

# ---- 7. Chrome ----------------------------------------------------------
if command -v google-chrome >/dev/null; then
    log "Chrome で ${BROWSER_URL} を開きます"
    google-chrome --new-window "$BROWSER_URL" >/dev/null 2>&1 &
    disown
else
    warn "Chrome 起動はスキップ。手動で ${BROWSER_URL} を開いてください"
fi

cat <<EOF

=========================================================================
 EarthTourGuide パイプラインが起動しました。

   VOICEVOX        : http://localhost:50021/docs
   llama-server    : http://localhost:${LLAMA_PORT}/health
   ttllm           : http://localhost:8001/docs
   earth-bridge    : http://localhost:8002/health
                     フレーム確認: http://localhost:8002/preview
   three-vrm       : ${BROWSER_URL}   ← Chrome で自動オープン
                     右下の 🎤 ボタンで PTT

 制御例 (flyTo):
   curl -X POST http://localhost:8002/control \\
     -H 'Content-Type: application/json' \\
     -d '{"cmd":"flyto","place":"Eiffel Tower"}'

 tmux:
   tmux attach -t ${SESSION}        (ログを見る)
   ./stop_all.sh                    (全部止める)
=========================================================================
EOF
