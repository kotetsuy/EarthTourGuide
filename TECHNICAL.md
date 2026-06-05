# EarthTourGuide — Technical Overview

For setup and run steps, see **[README.md](README.md)**. This document explains
**how it works and why**. 日本語: [TECHNICALJ.md](TECHNICALJ.md)。

---

## 1. Architecture

```
earth-controller (Playwright + system Chrome + CDP)
  └─ drives earth.google.com (smooth flyTo via the in-app search box)
        │  CDP Page.startScreencast (continuous JPEG frames)
        ▼
   earth-bridge (port 8002, WebSocket hub)
        │  WS /stream fans frames out
        ▼
   three-vrm (port 8000)  ← draws frames as the scene.background texture
        └─ VRM avatar overlaid in front (existing lip-sync / idle motion)

Tour progression (orchestrator):
   tour (port 8003)
     per stop:
       1. POST earth-bridge /control {cmd:flyto, place}   # fly there
       2. POST earth-bridge /control {cmd:dismiss}         # close info panel
       3. POST ttllm /chat {text:prompt, system}           # generate narration
       4. POST voicevox /audio_query                       # estimate speech len
       5. POST three-vrm /speak {text, speaker_id}         # synth + push to VRM
       6. dwell for (speech + buffer + dwell) → next stop

Voice dialogue (reused from AIassistant, 🎤 interrupt):
   Browser 🎤 → three-vrm /voice_chat_speak_stream
            → ttllm(8001): WhisperX (STT) + llama-server(8080, Qwen3.6)
            → split at sentence boundaries → VOICEVOX(50021) → WS push audio+visemes
```

### Services

| Service | Port | Role | Form |
| --- | --- | --- | --- |
| VOICEVOX Engine | 50021 | TTS (CPU) | symlink |
| llama-server | 8080 | Qwen3.6 inference (MTP) | symlink (bin) |
| ttllm | 8001 | WhisperX (STT) + llama bridge (FastAPI) | symlink |
| three-vrm | 8000 | VRM viewer + speech delivery (aiohttp) | **copy** |
| earth-bridge | 8002 | Earth frame relay hub (aiohttp WS) | new |
| earth-controller | — | Earth control + CDP screencast (Playwright) | new |
| tour | 8003 | tour orchestrator (aiohttp) | new |

---

## 2. Layout, symlink vs copy

```
EarthTourGuide/
├─ earth-controller/   new: Playwright + CDP screencast, Earth control
├─ earth-bridge/       new: frame → WebSocket relay (8002)
├─ tour/               new: orchestrator (8003) + tours/*.json
├─ three-vrm/          copy: gains the live-background change
├─ ttllm/              symlink → ../AIassistant/ttllm
├─ voicevox/           symlink → ../AIassistant/voicevox
├─ whisperX-rocm/      symlink → ../AIassistant/whisperX-rocm
├─ qwen3.6/            symlink → ../AIassistant/qwen3.6
└─ llama.cpp/          symlink → ../AIassistant/llama.cpp
```

- **Reused-as-is assets** (ttllm / voicevox / whisperX-rocm / qwen3.6 /
  llama.cpp) are **relative symlinks** to `../AIassistant/`. Fixing the pipeline
  in AIassistant propagates automatically — no double maintenance.
- **three-vrm is copied** because it gets the background change (static image
  rotation → live Earth feed), so it's tracked as a diff, not a symlink.
- `three-vrm/server.py` reads the VRM model from `~/AIassistant/vroid` and
  background images from `~/AIzunda/images` (still works after copying).

---

## 3. Earth control (earth-controller)

### Why the in-app search box (Phase 0 conclusion)

The real `earth.google.com` is a **WebAssembly app with no public JS API**.
The Phase 0 spike (`earth-controller/SPIKE_FINDINGS.md`) compared two methods:

- **URL navigation** (`page.goto("…/@lat,lng,…")`) **reloads** the WASM app =
  a **teleport**, no animation. Used only to set the initial position.
- **In-app search** (press `/` to focus → type place → wait ~1.5 s → Enter)
  triggers Earth's **native camera flight**. Verified frame-by-frame
  Tokyo→Sydney: a continuous ~6 s "zoom out → cross globe → zoom in" arc.
  **Adopted as the canonical flyTo method.**

The search box lives deep in shadow DOM, so `controller.py`'s `fly_to()` walks
`document` recursively for the first `input/textarea`, focuses it via the `/`
shortcut, then types. After arrival `dismiss()` (Escape) closes the info panel.

### CDP screencast

`Page.startScreencast` (JPEG, 1280×720, quality 70) streams frames at **~22 fps**
(worst-case inter-frame gap ~0.5 s during tile streaming). **Every frame must be
acked** with `Page.screencastFrameAck` or the stream stalls. The raw JPEG bytes
are forwarded to earth-bridge's `/ingest` WS.

`controller.py` also handles control JSON from the bridge:
`{"cmd":"flyto","place":…}` / `{"cmd":"dismiss"}` / `{"cmd":"ping"}`.

---

## 4. Frame relay (earth-bridge, port 8002)

An aiohttp WebSocket hub. It keeps only the latest frame and immediately sends it
to a newly connected viewer (avoids a black screen on join).

| Endpoint | Purpose |
| --- | --- |
| `WS /ingest` | controller's input (binary=JPEG, text=status) |
| `WS /stream` | viewers (three-vrm / preview) receive frames |
| `POST /control` | forward a control command to the controller (e.g. flyto) |
| `GET /preview` | minimal frame viewer |
| `GET /health` | controller connection / viewer count / frame presence |

Frames are forwarded **as binary, not base64**, to save bandwidth and CPU.

---

## 5. Live background (three-vrm / zundamon.html)

three-vrm already rotated `scene.background` through static images every 5 min.
That was extended to **update the texture every frame from earth-bridge's
`/stream`** — a natural change of the background *source*.

Key points:
- Connects to `ws://<host>:8002/stream`, decodes each Blob with
  `createImageBitmap(..., {imageOrientation:"flipY"})`, swaps the `THREE.Texture`
  `image` and sets `needsUpdate=true`. With `texture.flipY=false` this **avoids
  three.js's ImageBitmap flip warning / upside-down background**. Old
  `ImageBitmap`s are released via `.close()` to prevent leaks.
- **Fallback:** if the WS is unavailable or drops, the static image rotation
  resumes; reconnect is retried every 2 s.
- Lip-sync, idle motion and the 🎤 flow are untouched — only the background
  source changed.

---

## 6. Tour progression (tour, port 8003)

An **orchestrator** that reads `tour/tours/*.json` and processes each stop
serially. It only makes HTTP calls to the other services (earth-bridge / ttllm /
three-vrm / voicevox); all state stays inside the tour service.

### Tour definition JSON

```jsonc
{
  "id": "world",
  "title": "...",
  "defaults": {
    "fly_seconds": 10,      // wait for the flyTo animation
    "dwell_seconds": 6,     // linger after narration
    "speaker_id": 3,        // VOICEVOX speaker
    "max_tokens": 220,
    "system": "...guide persona system prompt..."
  },
  "stops": [
    { "name": "Tokyo Tower", "query": "Tokyo Tower",
      "lat": 35.66, "lng": 139.75,
      "prompt": "Introduce Tokyo Tower in 2-3 sentences." }
  ]
}
```

`query` is the flyTo search term, `prompt` is the narration instruction to ttllm.
`fly_seconds`/`dwell_seconds` can be overridden per stop.

### One stop's sequence

1. `POST earth-bridge /control {cmd:flyto, place:query}` → wait `fly_seconds`
2. `POST earth-bridge /control {cmd:dismiss}` (close info panel)
3. `POST ttllm /chat {text:prompt, system, max_tokens}` → narration `reply`
4. `POST voicevox /audio_query` to **estimate speech seconds** (no synthesis;
   sum mora lengths + pause + pre/post, divided by `speedScale`)
5. `POST three-vrm /speak {text:reply, speaker_id}` → VOICEVOX synth + WS push
6. dwell for `speech + buffer + dwell_seconds` → next stop

Speech length is estimated from VOICEVOX because `/speak` doesn't return audio
duration; this keeps stop transitions neither too early nor too late. If the
query fails it falls back to a char-count estimate (`len*0.18s`).

### State machine and pause/resume

`TourRunner` holds the loop in an `asyncio.Task`.

- `_resume` (`asyncio.Event`) is the "running" flag: `pause()` clears it,
  `resume()` sets it.
- `_sleep()` **does not advance time while paused** (continues from the remaining
  time on resume).
- `next()` aborts the current stop and skips forward; `stop()` cancels the task.

| Endpoint | Action |
| --- | --- |
| `POST /tour/start {id}` | start (cancels & recreates any running tour) |
| `POST /tour/stop` | stop |
| `POST /tour/pause` `/resume` | pause / resume |
| `POST /tour/next` | skip to next stop |
| `GET /tour/status` `/list` | progress / tour list |

### Coexisting with the 🎤 interrupt

A hook in `zundamon.html`'s `setMicState()` auto-sends **`POST /tour/pause` when
recording starts and `POST /tour/resume` when it returns to idle** (answer done),
so a 🎤 question naturally interrupts a running tour.

- tour is on a different port (8003) → cross-origin, but a **bodyless `POST` is a
  simple request** (no preflight); the side effect reaches the server. The
  response is ignored (`.catch()`), so no CORS headers are needed.
- pause/resume are no-ops server-side when no tour is running.

---

## 7. Verification status

- **Phase 0:** confirmed on real hardware that URL=teleport and search=smooth
  flyTo; screencast ~22 fps.
- **Phase 2:** verified controller→bridge→/stream→zundamon.html end to end —
  live Earth background with the VRM in front, and a `/control` flyTo
  (Tokyo→Paris) updating the background in real time.
- **Phase 3:** with the four dependencies replaced by mocks, deterministically
  verified the tour's **call order** (flyto→dismiss→chat→audio_query→speak per
  stop), **speech-length timing**, **pause/resume freeze-and-finish**, and
  stop / unknown-id (404).

> A full live run (Qwen3.6 35B + VOICEVOX all up at once) wasn't done as it
> monopolizes the GPU.

---

## 8. Known limitations / not yet done (polish)

- **Earth UI bleed-through:** the background shows Earth's toolbar / search box /
  place markers. To clean it: (a) inject CSS into earth.google.com from the
  controller to hide the UI, or (b) crop the live texture top/bottom in
  zundamon.html (UV offset/repeat). **Not done.**
- **Audio ducking:** on a 🎤 interrupt the tour pauses, but the **already-playing
  narration audio plays out** (no immediate stop).
- **Aspect ratio:** the background is stretched to the window (no cover fit).

### Inherited from the base

- WhisperX is **unstable past 60 s** of audio on ROCm 7.x (VAD caps at 55 s).
- VOICEVOX runs on **CPU** (GPU is taken by LLM/STT); long replies are TTS-bound.
- Chrome's AudioContext needs a first click (user gesture).
- Qwen3 "thinking" is always OFF through ttllm.
- earth-controller needs a `DISPLAY` (**headed** Chrome).
- Paths use `$USER` / `expanduser("~/...")`; don't add hardcoded paths.

---

## 9. Phase history

- [x] **Phase 0** — feasibility spike (flyTo + screencast)
- [x] **Phase 1** — repo skeleton (layout, symlinks, start/stop, README)
- [x] **Phase 2** — live background (earth-bridge → three-vrm)
- [x] **Phase 3** — tour progression + narration (tour service, 🎤 auto-pause)
