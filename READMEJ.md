# EarthTourGuide（日本語）

Google Earth 上を巡りながら、ずんだもん（VRM）が音声で解説する
「ワールドツアーガイド」デモ。展示会での実演を想定し、**安定性と見栄え**を最優先する。

ベースは [kotetsuy/AIassistant](https://github.com/kotetsuy/AIassistant)
（Voice → STT → LLM → TTS → VRM リップシンクをフルローカルで動かすテンプレート）。
本リポジトリはそのパイプラインを流用し、背景を **Google Earth のライブ映像** に
差し替え、ツアー進行ロジックを追加する。

## 体験イメージ

- 画面に地球が映り、その手前にずんだもん（VRM）が立つ
- ずんだもんが地名を紹介しながら、地球がその場所へ滑らかに飛ぶ（flyTo）
- ユーザーが🎤で質問すると、その場所について音声で答える（既存の音声対話を流用）

## アーキテクチャ

```
earth-controller (Playwright + system Chrome + CDP)
  └─ earth.google.com を操作（検索ボックス経由で滑らかな flyTo）
        ↓ CDP Page.startScreencast（JPEG フレーム連続取得）
   earth-bridge (port 8002, WebSocket ハブ)
        ↓ WS /stream でフレーム配信
   three-vrm (port 8000) ← 背景テクスチャとして描画
        └─ その手前に Koteko/ずんだもん VRM をオーバーレイ

音声対話（流用）:
   Browser 🎤 → ttllm(8001) → WhisperX(STT) + llama-server(8080, Qwen3.6)
              → 文境界で分割 → VOICEVOX(50021) → WS で音声+visemes を push

ツアー進行:
   ツアー定義(JSON) に沿って次の地点へ flyTo → LLM が解説生成 → ずんだもんが喋る
```

> **フェーズ0で実証済み**: 本家 earth.google.com で滑らかな flyTo は
> **検索ボックス経由でのみ**可能（URL 直書きは再読込＝瞬間移動）。CDP screencast は
> 1280×720 JPEG で約22fps。詳細は `earth-controller/SPIKE_FINDINGS.md`。

## ディレクトリ構成

| パス | 役割 | 形態 |
| --- | --- | --- |
| `earth-controller/` | Playwright + CDP screencast、Earth 操作・flyTo | 新規 |
| `earth-bridge/` | フレーム → WebSocket 中継ハブ (port 8002) | 新規 |
| `tour/tours/` | ツアー定義 JSON（地名・座標・解説プロンプト） | 新規 |
| `three-vrm/` | aiohttp + VRM ビューア (port 8000)。背景ライブ化の改造が入る | AIassistant からコピー |
| `ttllm/` | FastAPI bridge (WhisperX + llama.cpp) (port 8001) | symlink |
| `voicevox/` | VOICEVOX Engine 関連 (port 50021) | symlink |
| `whisperX-rocm/` | ROCm 版 WhisperX | symlink |
| `qwen3.6/` | Qwen3.6 GGUF モデル | symlink |
| `llama.cpp/` | 推論バイナリ | symlink |

流用資産（`ttllm` / `voicevox` / `whisperX-rocm` / `qwen3.6` / `llama.cpp`）は
`../AIassistant/` への **symlink**。パイプライン側を AIassistant で直すと自動反映される。
`three-vrm` のみ背景ライブ化の改造が入るため **コピーして差分管理**。

## ポート一覧

| サービス | Port |
| --- | --- |
| VOICEVOX Engine | 50021 |
| llama-server (Qwen3.6) | 8080 |
| ttllm | 8001 |
| three-vrm | 8000 |
| **earth-bridge** | **8002** |

## セットアップ

```bash
# Earth 操作用の venv（Playwright は system Chrome を使うので chromium DL は不要）
cd earth-controller
uv venv && uv pip install playwright aiohttp
# earth-bridge は同じ venv を流用する（専用 venv を作っても可）
```

ベース側（ttllm / voicevox / llama-server / モデル）のセットアップは
AIassistant の README を参照（symlink で共有）。

## 起動 / 停止

```bash
./start_all.sh        # 7サービスを tmux セッション "earthtour" で起動
./stop_all.sh         # 全停止（VOICEVOX も停止）
./stop_all.sh --keep-voicevox   # VOICEVOX は残す

tmux attach -t earthtour    # ログを見る
```

起動後:
- VRM 画面: <http://localhost:8000/zundamon.html>（Chrome で自動オープン）
- フレーム確認: <http://localhost:8002/preview>
- flyTo を手で叩く:
  ```bash
  curl -X POST http://localhost:8002/control \
    -H 'Content-Type: application/json' \
    -d '{"cmd":"flyto","place":"Eiffel Tower"}'
  ```

## 既知の制約（ベース由来）

- WhisperX は ROCm 7.x で **60秒超の録音で GPU memory fault**。VAD で 55秒カット。
- VOICEVOX は **CPU 推論**（GPU は LLM/STT で専有）。長文応答は TTS がボトルネック。
- Chrome の AudioContext は初回クリック（user-gesture）が必須。
- Qwen3 の thinking は ttllm 経由では常に OFF。
- パスは `$USER` / `expanduser("~/...")` で統一。ハードコードを増やさない。
- earth-controller はヘッド付き Chrome を使うため **DISPLAY が必要**
  （本機では GNOME Remote Desktop の `:10.0`）。

## 開発フェーズの進捗

- [x] **フェーズ0**: feasibility spike（flyTo 可否・screencast 検証）→ `earth-controller/SPIKE_FINDINGS.md`
- [x] **フェーズ1**: リポジトリ雛形（構成・symlink・start/stop・README）
- [ ] **フェーズ2**: 背景ライブ化（earth-bridge → three-vrm `zundamon.html` の背景を WS フレーム化）
- [ ] **フェーズ3**: ツアー進行 + ナレーション統合（自動巡回＋🎤割り込み質問）

## ライセンス

Apache-2.0（ベースの AIassistant に合わせる）。
