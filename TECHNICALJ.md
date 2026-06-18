# EarthTourGuide — 技術解説（日本語）

セットアップ・実行手順は **[READMEJ.md](READMEJ.md)** を参照。
本書は**仕組みと設計判断**を説明します。English: [TECHNICAL.md](TECHNICAL.md)。

---

## 1. 全体アーキテクチャ

```
earth-controller (Playwright + system Chrome + CDP)
  └─ earth.google.com を操作（検索ボックス経由で滑らかな flyTo）
        │  CDP Page.startScreencast（JPEG フレーム連続取得）
        ▼
   earth-bridge (port 8002, WebSocket ハブ)
        │  WS /stream でフレーム配信
        ▼
   three-vrm (port 8000)  ← フレームを scene.background テクスチャに描画
        └─ その手前に VRM アバターをオーバーレイ（既存のリップシンク/idle motion）

ツアー進行（司令塔）:
   tour (port 8003)
     stop ごとに:
       1. POST earth-bridge /control {cmd:flyto, place}   ← 地点へ飛ぶ
       2. POST earth-bridge /control {cmd:dismiss}         ← 情報パネルを閉じる
       3. POST ttllm /chat {text:prompt, system}           ← 解説文を生成
       4. POST voicevox /audio_query                       ← 発話長を見積り
       5. POST three-vrm /speak {text, speaker_id}         ← 合成して VRM へ配信
       6. 発話長 + dwell だけ滞在 → 次の stop へ

音声対話（AIassistant から流用、🎤 割り込み）:
   Browser 🎤 → three-vrm /voice_chat_speak_stream
            → ttllm(8001): WhisperX(STT) + llama-server(8080, Qwen3.6)
            → 文境界で分割 → VOICEVOX(50021) → WS で audio+visemes を push
            └─ transcript に移動・案内の意図があれば:
               ttllm /chat で行き先を抽出 → earth-bridge /control flyto
               （ナレーション生成と並行。§5.1 参照）
```

### サービス一覧

| サービス | Port | 役割 | 形態 |
| --- | --- | --- | --- |
| VOICEVOX Engine | 50021 | TTS（CPU 推論） | symlink |
| llama-server | 8080 | Qwen3.6 推論（MTP） | symlink(bin) |
| ttllm | 8001 | WhisperX(STT) + llama bridge（FastAPI） | symlink |
| three-vrm | 8000 | VRM ビューア + 発話配信（aiohttp） | **コピー** |
| earth-bridge | 8002 | Earth フレーム中継ハブ（aiohttp WS） | 新規 |
| earth-controller | — | Earth 操作 + CDP screencast（Playwright） | 新規 |
| tour | 8003 | ツアー進行の司令塔（aiohttp） | 新規 |

---

## 2. リポジトリ構成と symlink / コピー方針

```
EarthTourGuide/
├─ earth-controller/   新規: Playwright + CDP screencast、Earth 操作
├─ earth-bridge/       新規: フレーム → WebSocket 中継 (8002)
├─ tour/               新規: 進行サービス (8003) + tours/*.json
├─ three-vrm/          コピー: 背景ライブ化の改造が入る
├─ ttllm/              symlink → ../AIassistant/ttllm
├─ voicevox/           symlink → ../AIassistant/voicevox
├─ whisperX-rocm/      symlink → ../AIassistant/whisperX-rocm
├─ qwen3.6/            symlink → ../AIassistant/qwen3.6
└─ llama.cpp/          symlink → ../AIassistant/llama.cpp
```

- **無改造で流用する資産**（ttllm / voicevox / whisperX-rocm / qwen3.6 / llama.cpp）は
  `../AIassistant/` への**相対 symlink**。AIassistant 側でパイプラインを直すと自動反映され、
  二重メンテを避けられる。
- **three-vrm のみコピー**。背景を「静止画ローテーション」から「Earth ライブ映像」に
  差し替える改造が入るため、symlink ではなくコピーして差分管理する。
- `three-vrm/server.py` は VRM モデルを `~/AIassistant/vroid`、背景画像を `~/AIzunda/images`
  から読む（コピー後もそのまま動作）。

---

## 3. Earth 操作（earth-controller）

### なぜ「検索ボックス経由」なのか（フェーズ0 の結論）

本家 `earth.google.com` は **WebAssembly アプリで公開 JS API が無い**。
フェーズ0スパイク（`earth-controller/SPIKE_FINDINGS.md`）で 2 方式を検証した結果:

- **URL 直書き**（`page.goto("…/@lat,lng,…")`）= WASM アプリが**再読込**され
  **瞬間移動**。アニメーション無し。→ 初期位置の設定だけに使う。
- **検索ボックス経由**（`/` でフォーカス → 地名入力 → 1.5s 待ち → Enter）=
  Earth の**ネイティブなカメラ飛行**が走る。Tokyo→Sydney をフレーム単位で確認し、
  「ズームアウト → 地球を横断 → ズームイン」の連続した約 6 秒のアークを実証。
  **これを flyTo の正式手段に採用**。

検索ボックスは shadow DOM の奥にあるため、`controller.py` の `fly_to()` は
`document` を再帰的に走査して最初の `input/textarea` を掴み、`/` ショートカットで
フォーカスしてからキー入力する。到着後は `dismiss()`（Escape）で情報パネルを閉じる。

### CDP screencast

`Page.startScreencast`（JPEG, 1280×720, quality 70）でフレームを連続取得。
実測 **約 22fps**、タイル読込中の最悪フレーム間隔 ~0.5s。
**各フレームは必ず `Page.screencastFrameAck` する**（怠ると stream が止まる）。
取得した JPEG バイト列はそのまま earth-bridge の `/ingest`（WS）へ送る。

`controller.py` は earth-bridge から届く制御 JSON も処理する:
`{"cmd":"flyto","place":...}` / `{"cmd":"dismiss"}` / `{"cmd":"ping"}`。

---

## 4. フレーム中継（earth-bridge, port 8002）

aiohttp の WebSocket ハブ。最新フレームのみ保持し、新規視聴者には即座に
最後のフレームを送る（黒画面待ちを避ける）。

| エンドポイント | 用途 |
| --- | --- |
| `WS /ingest` | controller 専用入口（binary=JPEG, text=status） |
| `WS /stream` | 視聴者（three-vrm / preview）がフレームを受け取る |
| `POST /control` | 制御コマンドを controller に転送（例 flyto） |
| `GET /preview` | フレーム確認用の簡易ビューア |
| `GET /health` | controller 接続状態 / 視聴者数 / フレーム有無 |

フレームは **base64 化せず binary のまま**転送して帯域とCPUを節約する。

---

## 5. ライブ背景（three-vrm / zundamon.html）

既存の three-vrm は `scene.background` を「5 分ごとに静止画テクスチャへ差し替える」
仕組みを持っていた。これを **earth-bridge の `/stream` から来る JPEG フレームで
毎フレーム更新する**よう拡張した（背景ソースの差し替えという自然な拡張）。

要点:
- `ws://<host>:8002/stream` に接続し、受信 Blob を `createImageBitmap(...,
  {imageOrientation:"flipY"})` でデコード、`THREE.Texture` の `image` を差し替えて
  `needsUpdate=true`。`texture.flipY=false` と合わせて **three.js の ImageBitmap
  上下反転/警告を回避**。古い `ImageBitmap` は `.close()` で解放しメモリリークを防ぐ。
- **フォールバック**: WS 未接続/切断時は従来の静止画ローテーションを自動再開。
  接続は 2 秒間隔で自動リトライ。
- 既存のリップシンク・idle motion・🎤 動線には手を付けず、背景ソースのみ差し替えた。

### 5.1 音声での行き先指示（🎤 → flyTo）

`voice_chat_speak_stream_handler` は ttllm からの SSE `transcript` イベントを監視する。
まず安価な正規表現 `_GUIDE_INTENT`（案内 / 行って / 連れて …）で移動・案内の意図を
粗く判定し、該当時のみ ttllm `/chat` を抽出専用システムプロンプトで呼び、**行き先の
地名だけ**（または `NONE`）を返させる。地名が返れば、投げっぱなしタスクで earth-bridge
`/control {cmd:flyto, place}` を送り、`FLY_DISMISS_DELAY` 秒後に `{cmd:dismiss}` を送る。

- 抽出は **バックグラウンドタスク**（`_spawn`）で走らせ、ナレーションのトークン
  ストリームや HTTP 応答をブロックしない。
- 抽出用 `/chat` とナレーションのストリームは llama-server に**同時に**届くため、
  `start_all.sh` は `--parallel 2`（`LLAMA_PARALLEL`）で起動する。これは既存の
  `-c 8192` を 2×4096 スロットに分割するだけで（**追加 VRAM なし**）、短い flyTo 抽出が
  長いナレーションの後ろにキュー待ちせず追い越せる＝アバターが喋っている間にカメラが
  飛び始める。
- ブラウザは `flyto` WS イベントで `🛫 <place> へ移動中…` のヒントを表示する。

### 5.2 字幕表示（音声同期スクロール / 一括クリア）

`zundamon.html` には 2 種類の字幕がある: ボット返答 `#subtitle`（白）と
ユーザー発話 `#user-subtitle`（薄青）。

- **3 行に制限**: `#subtitle` は `max-height`（≒3 行）+ `overflow:hidden`。
  長い解説でも画面を覆わない。
- **音声同期スクロール**: `startSubtitleScroll(startAt, duration)` が
  `requestAnimationFrame` で AudioContext 時刻を監視し、再生区間
  `[startAt, startAt+duration]` の進捗に応じて `scrollTop` を 0→最大へ動かす
  （3 行窓を喋っている箇所に追従＝カラオケ風）。`turn_start` / `clear` で停止。
- **置き換え vs 追記**: ストリーミング音声対話は `turn_start` で `botReplyBuf` を
  リセットし、文チャンクを**追記**する。一方 `/speak` は 1 発話まるごとを 1 メッセージで
  送るため、speak メッセージに `replace:true` を付け、クライアントは直前の発話
  （例: 音声入力で指示した行き先）に追記せず**置き換える**。これが無いとツアーの
  ナレーションが前の発話の後ろに連結し、3 行窓の外に隠れて「更新されない」ように見える。
- **一括クリア**: three-vrm に `POST /clear` を新設し、全 WS へ `{"type":"clear"}` を
  配信する。受信側は両字幕を消す。tour は `_run` 冒頭でこれを呼び、**ツアー開始時に
  前回の音声入力ダイアログ（🗣 …）を消す**。

---

## 6. ツアー進行（tour, port 8003）

ツアー定義 `tour/tours/*.json` を読み、各 stop を直列に処理する**司令塔**。
他サービス（earth-bridge / ttllm / three-vrm / voicevox）を HTTP で叩くだけで、
状態は tour サービス内に閉じる。

### ツアー定義 JSON

```jsonc
{
  "id": "world",
  "title": "...",
  "loop": false,            // 任意: true で常時ループ（start の loop が優先）
  "defaults": {
    "fly_seconds": 10,      // flyTo アニメーションの待ち時間
    "dwell_seconds": 6,     // 解説後の滞在時間
    "speaker_id": 3,        // VOICEVOX 話者
    "max_tokens": 220,
    "system": "...ガイド人格の system プロンプト..."
  },
  "stops": [
    { "name": "東京タワー", "query": "Tokyo Tower",
      "lat": 35.66, "lng": 139.75,
      "prompt": "東京タワーを2〜3文で紹介して。" }
  ]
}
```

`query` が flyTo の検索語、`prompt` が ttllm への解説指示。stop 単位で
`fly_seconds`/`dwell_seconds` を上書きできる。

> **口調の注意**: `defaults.system` を渡すと ttllm 既定の人格プロンプト
> （`server.py` の `SYSTEM_PROMPT` = コテコ／一人称コテコ／アルヨ調）を**上書き**する。
> 同梱 `world.json` の `system` はコテコ人格＋ツアーガイド役割を両立させてあり、
> `prompt` 側にも「ずんだもん口調」等の口調指定は入れていない（口調は `system` が一括で担当）。
> 別人格にしたい場合は `system` をそのツアー JSON で差し替える。

### 1 stop の処理シーケンス

1. `POST earth-bridge /control {cmd:flyto, place:query}` → `fly_seconds` 待つ
2. `POST earth-bridge /control {cmd:dismiss}`（情報パネルを閉じる）
3. `POST ttllm /chat {text:prompt, system, max_tokens}` → 解説文 `reply`
4. `POST voicevox /audio_query` で **発話秒数を概算**（合成はしない。
   `accent_phrases` の mora 長 + pause + pre/post を `speedScale` で割る）
5. `POST three-vrm /speak {text:reply, speaker_id}` → VOICEVOX 合成 + WS 配信
6. 「発話秒数 + buffer + `dwell_seconds`」だけ滞在 → 次の stop へ

発話長を VOICEVOX から見積もるのは、`/speak` が音声長を返さないため
（次の地点へ早すぎ/遅すぎないタイミングで進めるための工夫）。取得失敗時は
文字数ベースの概算（`len*0.18s`）にフォールバック。

### 状態機械と pause/resume

`TourRunner` が `asyncio.Task` で巡回ループを保持する。

- `_resume`（`asyncio.Event`）= 動作中フラグ。`pause()` で clear、`resume()` で set。
- `_sleep()` は**ポーズ中は時間を進めない**実装（resume で残り時間から続行）。
- `next()` は現在 stop を中断して次へスキップ。`stop()` はタスクをキャンセル。
- **無限ループ**: 巡回本体を `_run_once()`（1 周）に切り出し、`_run()` が
  `while True: _run_once(); if _stop or not loop: break` で回す。`loop` は
  `start(tour, loop)` で受け取り（`start` の `loop` 優先 → ツアー JSON の `loop` →
  既定 `false`）、最後の stop の後に index 0 へ巻き戻る。`status()` に `loop` を含める。
  `_run` 冒頭で `POST three-vrm /clear` を一度だけ呼び、前回の字幕を消す（§5.2）。

| エンドポイント | 動作 |
| --- | --- |
| `POST /tour/start {id, loop?}` | 巡回開始（既存ツアーはキャンセルして作り直し）。`loop:true` で無限ループ |
| `POST /tour/stop` | 停止（ループ中も次の区切りで抜ける） |
| `POST /tour/pause` `/resume` | 一時停止 / 再開 |
| `POST /tour/next` | 次の地点へスキップ |
| `GET /tour/status` `/list` | 進行状態（`loop` を含む）/ ツアー一覧 |

開始／停止のラッパースクリプト `start_tour_loop.sh [id]`（無限ループ起動）と
`stop_tour.sh`（停止）をリポジトリ直下に同梱する（tour サービスの health を確認してから
`/tour/start`・`/tour/stop` を叩くだけの薄いラッパ）。

### 🎤 割り込みとの両立

`zundamon.html` の `setMicState()` にフックを入れ、**録音開始（recording）で
`POST /tour/pause`、応答完了（idle 復帰）で `POST /tour/resume`** を自動送信する。
これによりツアー再生中でも 🎤 質問が自然に割り込める。

- tour は別ポート(8003)なので**クロスオリジン**だが、ボディ無し `POST` は
  **単純リクエスト**でプリフライト不要、副作用（pause/resume）はサーバに到達する。
  レスポンスは読まない（`.catch()` で握り潰す）ので CORS ヘッダは不要。
- ツアー未実行でも pause/resume はサーバ側 no-op。

---

## 7. 検証状況

- **フェーズ0**: URL=瞬間移動 / 検索=滑らか flyTo を実機で結論。screencast ~22fps。
- **フェーズ2**: controller→bridge→/stream→zundamon.html をエンドツーエンドで確認。
  背景にライブ Earth、手前に VRM 起立。`/control` の flyTo（東京→パリ）で背景が
  リアルタイム更新されることも確認。
- **フェーズ3**: 依存4サービスをモックに差し替え、tour の**呼び出し順序**
  （flyto→dismiss→chat→audio_query→speak を地点ごとに反復）・**発話長反映**・
  **pause/resume の凍結と完走**・stop/unknown-id(404) を決定的に検証。

> 完全実機通し（Qwen3.6 35B + VOICEVOX 込みの一括起動）は GPU を専有するため未実施。

---

## 8. 既知の制約・未対応（演出仕上げ）

- **Earth の UI 映り込み**: 背景にツールバー/検索ボックス/地名マーカーが映る。
  クリーンにするには (a) controller で earth.google.com に CSS 注入して UI 非表示、
  または (b) zundamon.html 側でライブテクスチャを上下クロップ（UV offset/repeat）。**未対応**。
- **音声ダッキング**: 🎤 割り込み時、ツアー進行は止まるが**再生中のナレーション音声は
  鳴り切る**（即停止は未実装）。
- **アスペクト比**: 背景はウィンドウに引き伸ばし（cover 補正は未実装）。

### ベース由来の制約

- WhisperX は ROCm 7.x で **60 秒超の録音が不安定**（VAD で 55 秒カット）。
- VOICEVOX は **CPU 推論**（GPU は LLM/STT で専有）。長文応答は TTS がボトルネック。
- Chrome の AudioContext は初回クリック（user-gesture）必須。
- Qwen3 の thinking は ttllm 経由では常に OFF。
- earth-controller は **headed Chrome** を使うため `DISPLAY` が必須。
- パスは `$USER` / `expanduser("~/...")` で統一。ハードコードを増やさない。

---

## 9. 開発フェーズ履歴

- [x] **フェーズ0**: feasibility spike（flyTo 可否・screencast 検証）
- [x] **フェーズ1**: リポジトリ雛形（構成・symlink・start/stop・README）
- [x] **フェーズ2**: 背景ライブ化（earth-bridge → three-vrm 背景の WS フレーム化）
- [x] **フェーズ3**: ツアー進行 + ナレーション統合（tour サービス・🎤 自動 pause）
- [x] **演出仕上げ**: 字幕の 3 行制限＋音声同期スクロール / `/clear` による字幕一括クリア /
  ツアー無限ループ（`loop`）と起動・停止スクリプト / `world.json` の口調をコテコ・アルヨ調に統一
