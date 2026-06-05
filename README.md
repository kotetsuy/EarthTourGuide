# EarthTourGuide

A "world tour guide" demo: **Zundamon** (a VRM avatar) narrates by voice while
**Google Earth** flies smoothly across the globe behind her. Built for live
exhibition use, so **stability and visual polish come first**.

日本語の詳しい説明は **[READMEJ.md](READMEJ.md)** を参照。

Based on [kotetsuy/AIassistant](https://github.com/kotetsuy/AIassistant) — a
fully-local Voice → STT → LLM → TTS → VRM lip-sync template. This repo reuses
that pipeline, swaps the background for a **live Google Earth feed**, and adds
tour-progression logic.

## Experience

- The globe fills the screen; Zundamon stands in front of it.
- She introduces a place and Earth flies there smoothly (`flyTo`).
- Press 🎤 to ask a question; she answers about that place (reusing the existing
  voice dialogue pipeline).

## Architecture

```
earth-controller (Playwright + system Chrome + CDP)
  └─ drives earth.google.com (smooth flyTo via the in-app search box)
        ↓ CDP Page.startScreencast (continuous JPEG frames)
   earth-bridge (port 8002, WebSocket hub)
        ↓ WS /stream fans frames out
   three-vrm (port 8000) ← draws them as the background texture
        └─ Koteko/Zundamon VRM overlaid in front

Voice dialogue (reused):
   Browser 🎤 → ttllm(8001) → WhisperX (STT) + llama-server(8080, Qwen3.6)
              → split at sentence boundaries → VOICEVOX(50021) → WS push
```

> **Verified in Phase 0:** on the real earth.google.com, a smooth `flyTo` is
> only possible **via the in-app search box** (a raw URL reloads = teleport).
> CDP screencast runs ~22 fps at 1280×720 JPEG. See
> `earth-controller/SPIKE_FINDINGS.md`.

## Layout

| Path | Role | Form |
| --- | --- | --- |
| `earth-controller/` | Playwright + CDP screencast, Earth control / flyTo | new |
| `earth-bridge/` | frame → WebSocket relay hub (port 8002) | new |
| `tour/tours/` | tour definitions (places, coords, narration prompts) | new |
| `three-vrm/` | aiohttp + VRM viewer (port 8000); gains live-background changes | copied from AIassistant |
| `ttllm/`, `voicevox/`, `whisperX-rocm/`, `qwen3.6/`, `llama.cpp/` | reused pipeline | symlinks → `../AIassistant/` |

## Quick start

```bash
cd earth-controller && uv venv && uv pip install playwright aiohttp   # uses system Chrome
cd .. && ./start_all.sh        # starts 7 services in tmux session "earthtour"
./stop_all.sh                  # stop everything
```

- VRM view: <http://localhost:8000/zundamon.html>
- Frame preview: <http://localhost:8002/preview>
- Trigger a flyTo:
  ```bash
  curl -X POST http://localhost:8002/control \
    -H 'Content-Type: application/json' -d '{"cmd":"flyto","place":"Eiffel Tower"}'
  ```

Note: `earth-controller` launches a **headed** Chrome, so a `DISPLAY` is
required (on this machine, GNOME Remote Desktop's `:10.0`).

## Phase status

- [x] **Phase 0** — feasibility spike (flyTo + screencast) → `earth-controller/SPIKE_FINDINGS.md`
- [x] **Phase 1** — repo skeleton (layout, symlinks, start/stop, README)
- [ ] **Phase 2** — live background (earth-bridge → three-vrm `zundamon.html`)
- [ ] **Phase 3** — tour progression + narration (auto tour + 🎤 interrupts)

## License

Apache-2.0 (matching the AIassistant base).
