# EarthTourGuide — セットアップ & 実行手順（日本語）

Google Earth 上を巡りながら、VRM アバター（Koteko／ずんだもん）が音声で解説する
「ワールドツアーガイド」デモ。展示会での実演を想定し、**安定性と見栄え**を最優先する。

- このドキュメントは **git clone から起動まで**の手順書です。
- 仕組み・設計の詳細は **[TECHNICALJ.md](TECHNICALJ.md)**（English: [TECHNICAL.md](TECHNICAL.md)）を参照。
- English setup guide: **[README.md](README.md)**。

---

## 1. 前提条件

| 項目 | 要件 |
| --- | --- |
| OS | Ubuntu 24.04 |
| GPU / ROCm | AMD gfx1151（Ryzen AI Max+ 395 等）/ ROCm 7.x |
| Python | 3.12 |
| 必須コマンド | `git` `tmux` `docker` `curl` `google-chrome` `uv` |
| ディスプレイ | ヘッド付き Chrome を出す `DISPLAY`（本機では GNOME Remote Desktop の `:10.0`） |

> **重要:** 本リポジトリは音声パイプライン（STT/LLM/TTS/VRM）を
> [kotetsuy/AIassistant](https://github.com/kotetsuy/AIassistant) から
> **相対 symlink（`../AIassistant/...`）で流用**します。先に AIassistant を
> **兄弟ディレクトリとして配置・セットアップ**しておく必要があります。

---

## 2. ベース（AIassistant）の準備

```bash
cd ~
git clone https://github.com/kotetsuy/AIassistant.git
cd AIassistant
# AIassistant の README に従って以下を用意:
#   - llama.cpp をビルド (~/llama.cpp/build/bin/llama-server)
#   - Qwen3.6 GGUF モデル (qwen3.6/)
#   - ttllm の依存、VOICEVOX(docker)、whisperX-rocm、VRM モデル(vroid/koteko.vrm)
```

AIassistant 単体で `./start_all.sh` が通る状態になっていれば OK です。

---

## 3. EarthTourGuide の取得

AIassistant と**同じ親ディレクトリ**に clone します（symlink が `../AIassistant` を指すため）。

```bash
cd ~                       # AIassistant と同じ階層
git clone https://github.com/kotetsuy/EarthTourGuide.git
cd EarthTourGuide

# symlink が解決できるか確認（全て [OK] になること）
for d in ttllm voicevox whisperX-rocm qwen3.6 llama.cpp; do
  [ -e "$d/" ] && echo "OK  $d -> $(readlink $d)" || echo "BROKEN $d"
done
```

---

## 4. Earth 用 venv の作成

Earth を操作する `earth-controller` と中継 `earth-bridge` / `tour` は
Playwright + aiohttp を使います（Playwright は **system の Google Chrome** を使うので
chromium のダウンロードは不要）。

```bash
cd earth-controller
uv venv
uv pip install playwright aiohttp
cd ..
# earth-bridge と tour は earth-controller/.venv を自動で流用します
# （専用 venv を作っても可: 各ディレクトリで uv venv && uv pip install aiohttp）
```

---

## 5. 起動

```bash
export DISPLAY=:10.0        # ヘッド付き Chrome 用（環境に合わせて）
./start_all.sh
```

`start_all.sh` は tmux セッション `earthtour` に以下 7 サービスを順に起動し、
各ヘルスチェックを待ってから次へ進みます。

1. VOICEVOX (docker, 50021) → 2. llama-server (8080) → 3. ttllm (8001)
→ 4. earth-bridge (8002) → 5. earth-controller（headed Chrome で Earth 操作）
→ 6. three-vrm (8000) → 7. tour (8003)、最後に Chrome で VRM 画面を自動オープン。

起動後の確認:
- VRM 画面: <http://localhost:8000/zundamon.html>（自動オープン）
- Earth ライブフレーム確認: <http://localhost:8002/preview>
- ログ: `tmux attach -t earthtour`

> 初回は **VRM 画面を一度クリック**してください（Chrome の AudioContext は
> user-gesture が必須のため、クリックするまで音声が鳴りません）。

---

## 6. 使い方

### ツアー（自動巡回）

```bash
# tour/tours/<id>.json を読んで自動巡回を開始
curl -X POST http://localhost:8003/tour/start \
  -H 'Content-Type: application/json' -d '{"id":"world"}'

curl -X POST http://localhost:8003/tour/stop     # 停止
curl -X POST http://localhost:8003/tour/pause    # 一時停止
curl -X POST http://localhost:8003/tour/resume   # 再開
curl -X POST http://localhost:8003/tour/next     # 次の地点へスキップ
curl     http://localhost:8003/tour/status       # 進行状態
curl     http://localhost:8003/tour/list         # ツアー一覧
```

各地点で「その場所へ flyTo → アバターが解説をナレーション」を自動で行います。

### 🎤 で割り込み質問

VRM 画面右下の 🎤 ボタンを押して話しかけると、その内容に音声で答えます。
**録音を始めるとツアーは自動で一時停止**し、応答が終わると自動で再開します。

### 単発で地点へ飛ばす（デバッグ）

```bash
curl -X POST http://localhost:8002/control \
  -H 'Content-Type: application/json' -d '{"cmd":"flyto","place":"Eiffel Tower"}'
```

### 自分のツアーを追加

`tour/tours/<id>.json` を作成（`world.json` を雛形に）。`id` がそのまま
`/tour/start` の `id` になります。各 stop の `query`（検索語）と `prompt`（解説指示）を編集。

---

## 7. 停止

```bash
./stop_all.sh                  # 全停止（VOICEVOX コンテナも停止）
./stop_all.sh --keep-voicevox  # VOICEVOX は残す
```

---

## 8. トラブルシュート

| 症状 | 対処 |
| --- | --- |
| Earth が映らない / 背景が出ない | `DISPLAY` が正しいか、`http://localhost:8002/health` が `controller_connected:true, have_frame:true` か確認 |
| 音が鳴らない | VRM 画面を一度クリック（user-gesture）。VOICEVOX(50021) の起動も確認 |
| symlink が BROKEN | AIassistant が `../AIassistant` に在るか、セットアップ済みか確認 |
| ツアーが始まらない | `curl http://localhost:8003/tour/status` と `tmux attach -t earthtour` の `tour` ウィンドウのログを確認 |
| 長い録音で落ちる | WhisperX は ROCm で 60 秒超の録音が不安定（VAD で 55 秒カット） |

詳細な制約・設計は **[TECHNICALJ.md](TECHNICALJ.md)** を参照。

---

## ライセンス

Apache-2.0（ベースの AIassistant に合わせる）。
