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
| OS | Ubuntu 26.04 (resolute) |
| GPU / ROCm | AMD gfx1151（Ryzen AI Max+ 395 等）/ ROCm 7.14.0 (`/opt/rocm`) |
| Python | system 3.14 / 各 venv は 3.12（`.python-version` で固定） |
| 必須コマンド | `git` `tmux` `docker` `curl` `google-chrome` `uv` |
| ディスプレイ | ヘッド付き Chrome を出す `DISPLAY`（本機はローカルの `:0`） |

> ROCm 7.14 は Ubuntu 26.04 なら apt でネイティブ導入できます
> （`repo.amd.com/rocm/packages-multi-arch/ubuntu2604` の `amdrocm-core-sdk7.14-gfx1151`）。
> カーネル同梱 amdgpu が gfx1151 対応済みなので DKMS / `amdgpu-install` は不要です。

> **`HSA_OVERRIDE_GFX_VERSION` は設定しないこと。** gfx1151 版 PyTorch ホイール・
> CTranslate2-ROCm・llama.cpp はすべて gfx1151 ネイティブビルドなので、arch を override
> すると壊れます。`start_all.sh` はシェル profile から export されている場合に備えて
> 明示的に `unset` します。

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

> **ROCm 7.14 環境での要点**（詳細は AIassistant の READMEJ）:
> - PyTorch は **gfx1151 専用インデックス** (`repo.amd.com/rocm/whl/gfx1151/`) の
>   `torch==2.8.0+rocm7.12.0` / `torchaudio==2.8.0a0+rocm7.12.0` を使う。汎用 `whl-multi-arch`
>   版は実行時に `hipErrorInvalidImage` で全 GPU 操作が落ちる。**torchaudio は 2.9 未満**
>   （pyannote が `torchaudio.info` / `AudioMetaData` を使う）。
> - WhisperX venv は `~/whisperx/whisperX-rocm/.venv`（`ttllm/run.sh` の既定。`WHISPERX_VENV`
>   で上書き可）。旧 `~/AIzunda/whisperX-rocm` は OS 更新（system python 3.14）で壊れたため使わない。
> - `ttllm/server.py` は先頭で `import torch` する（ctranslate2 より先に読まないと
>   `undefined symbol: _ZN9rocRoller...` で落ちる）。

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

`three-vrm` も aiohttp が必要ですが、Ubuntu 26.04 の system python3 (3.14) には入っていません。
`start_all.sh` は `~/whisperx/whisperX-rocm/.venv` → `earth-controller/.venv` → `python3` の順に
**aiohttp を持つ python** を探して three-vrm を起動します。どこにも無ければ起動前に停止するので、
その場合は venv に入れてください:

```bash
VIRTUAL_ENV=~/whisperx/whisperX-rocm/.venv uv pip install aiohttp
```

---

## 5. 起動

```bash
export DISPLAY=:0           # ヘッド付き Chrome 用（環境に合わせて）
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
# tour/tours/<id>.json を読んで自動巡回を開始（1 周）
curl -X POST http://localhost:8003/tour/start \
  -H 'Content-Type: application/json' -d '{"id":"world"}'

# 無限ループで開始（最後の地点まで行ったら先頭へ戻る）
curl -X POST http://localhost:8003/tour/start \
  -H 'Content-Type: application/json' -d '{"id":"world","loop":true}'

curl -X POST http://localhost:8003/tour/stop     # 停止
curl -X POST http://localhost:8003/tour/pause    # 一時停止
curl -X POST http://localhost:8003/tour/resume   # 再開
curl -X POST http://localhost:8003/tour/next     # 次の地点へスキップ
curl     http://localhost:8003/tour/status       # 進行状態（loop の有無も返る）
curl     http://localhost:8003/tour/list         # ツアー一覧
```

ラッパースクリプトでも開始／停止できます:

```bash
./start_tour_loop.sh          # world ツアーを無限ループで開始
./start_tour_loop.sh kyoto    # 別の id を指定して無限ループ開始
./stop_tour.sh                # ツアー停止
```

各地点で「その場所へ flyTo → アバターが解説をナレーション」を自動で行います。
常時ループにしたい場合は、毎回 `"loop":true` を付ける代わりに
`tour/tours/<id>.json` のトップレベルに `"loop": true` を足しておけば既定でループします。

### 🎤 で割り込み質問・音声で行き先を指示

VRM 画面右下の 🎤 ボタンを押して話しかけると、その内容に音声で答えます。
**録音を始めるとツアーは自動で一時停止**し、応答が終わると自動で再開します。

さらに、**「東京タワーを案内して」「エッフェル塔に行って」**のように移動・案内を
指示すると、その場で **Google Earth がその場所へ flyTo し、背景が切り替わります**
（curl を打たなくてもマイクだけで操作できます）。仕組みは、聞き取った発話に移動の
意図があるときだけ LLM が行き先（地名）を抽出し、earth-bridge に flyTo を送ります。
flyTo はナレーション生成と並行で走るので、アバターは今まで通りその場所を解説します。

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
| three-vrm が `ModuleNotFoundError: aiohttp` | system python3 (3.14) で起動している。aiohttp 入りの venv python を使う |
| STT で `undefined symbol: _ZN9rocRoller...` | torch が ctranslate2 より後に import されている。`ttllm/server.py` 冒頭の `import torch` を確認 |
| torch で `hipErrorInvalidImage` / `kpack_load_code_object failed` | 汎用 multi-arch の torch が入っている。gfx1151 専用インデックス版に入れ替える |
| `module 'torchaudio' has no attribute 'AudioMetaData'` | torchaudio が 2.9 以上。2.8.x (`2.8.0a0+rocm7.12.0`) に下げる |
| GPU が使われない / HIP エラー | `HSA_OVERRIDE_GFX_VERSION` が export されていないか確認（gfx1151 では設定してはいけない） |

詳細な制約・設計は **[TECHNICALJ.md](TECHNICALJ.md)** を参照。

---

## ライセンス

Apache-2.0（ベースの AIassistant に合わせる）。
