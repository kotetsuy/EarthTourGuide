# EarthTourGuide — Setup & Run Guide

A "world tour guide" demo: a VRM avatar (Koteko / Zundamon) narrates by voice
while **Google Earth** flies smoothly across the globe behind her. Built for
live exhibition use, so **stability and visual polish come first**.

- This document is the **git-clone-to-launch** guide.
- For how it works and design rationale, see **[TECHNICAL.md](TECHNICAL.md)**.
- 日本語: セットアップ手順は **[READMEJ.md](READMEJ.md)**、技術解説は **[TECHNICALJ.md](TECHNICALJ.md)**。

---

## 1. Prerequisites

| Item | Requirement |
| --- | --- |
| OS | Ubuntu 24.04 |
| GPU / ROCm | AMD gfx1151 (e.g. Ryzen AI Max+ 395) / ROCm 7.x |
| Python | 3.12 |
| Commands | `git` `tmux` `docker` `curl` `google-chrome` `uv` |
| Display | a `DISPLAY` for headed Chrome (here: GNOME Remote Desktop `:10.0`) |

> **Important:** this repo reuses the voice pipeline (STT/LLM/TTS/VRM) from
> [kotetsuy/AIassistant](https://github.com/kotetsuy/AIassistant) via **relative
> symlinks (`../AIassistant/...`)**. You must place and set up **AIassistant as a
> sibling directory first**.

---

## 2. Prepare the base (AIassistant)

```bash
cd ~
git clone https://github.com/kotetsuy/AIassistant.git
cd AIassistant
# Follow AIassistant's README to provide:
#   - a built llama.cpp (~/llama.cpp/build/bin/llama-server)
#   - the Qwen3.6 GGUF model (qwen3.6/)
#   - ttllm deps, VOICEVOX (docker), whisperX-rocm, VRM model (vroid/koteko.vrm)
```

When `./start_all.sh` works inside AIassistant on its own, you're ready.

---

## 3. Get EarthTourGuide

Clone it into the **same parent directory** as AIassistant (the symlinks point
to `../AIassistant`).

```bash
cd ~                       # same level as AIassistant
git clone https://github.com/kotetsuy/EarthTourGuide.git
cd EarthTourGuide

# verify the symlinks resolve (all should print OK)
for d in ttllm voicevox whisperX-rocm qwen3.6 llama.cpp; do
  [ -e "$d/" ] && echo "OK  $d -> $(readlink $d)" || echo "BROKEN $d"
done
```

---

## 4. Create the Earth venv

`earth-controller`, `earth-bridge` and `tour` use Playwright + aiohttp.
Playwright drives the **system Google Chrome**, so no chromium download.

```bash
cd earth-controller
uv venv
uv pip install playwright aiohttp
cd ..
# earth-bridge and tour reuse earth-controller/.venv automatically
# (or give each its own: uv venv && uv pip install aiohttp)
```

---

## 5. Launch

```bash
export DISPLAY=:10.0        # for headed Chrome (adjust to your env)
./start_all.sh
```

`start_all.sh` starts 7 services in the tmux session `earthtour`, waiting on each
health check before the next:

1. VOICEVOX (docker, 50021) → 2. llama-server (8080) → 3. ttllm (8001)
→ 4. earth-bridge (8002) → 5. earth-controller (headed Chrome driving Earth)
→ 6. three-vrm (8000) → 7. tour (8003); then Chrome auto-opens the VRM page.

After launch:
- VRM page: <http://localhost:8000/zundamon.html> (auto-opened)
- Live Earth frame preview: <http://localhost:8002/preview>
- Logs: `tmux attach -t earthtour`

> **Click the VRM page once** on first use — Chrome's AudioContext requires a
> user gesture, so there is no sound until you click.

---

## 6. Usage

### Auto tour

```bash
# start the auto tour reading tour/tours/<id>.json (one lap)
curl -X POST http://localhost:8003/tour/start \
  -H 'Content-Type: application/json' -d '{"id":"world"}'

# start looping forever (after the last stop it wraps back to the first)
curl -X POST http://localhost:8003/tour/start \
  -H 'Content-Type: application/json' -d '{"id":"world","loop":true}'

curl -X POST http://localhost:8003/tour/stop     # stop
curl -X POST http://localhost:8003/tour/pause    # pause
curl -X POST http://localhost:8003/tour/resume   # resume
curl -X POST http://localhost:8003/tour/next     # skip to next stop
curl     http://localhost:8003/tour/status       # progress (also returns loop)
curl     http://localhost:8003/tour/list         # list tours
```

Wrapper scripts are also available:

```bash
./start_tour_loop.sh          # start the world tour looping forever
./start_tour_loop.sh kyoto    # loop a different tour id
./stop_tour.sh                # stop the tour
```

Per stop it flies to the place and the avatar narrates it automatically.
To always loop, add `"loop": true` at the top level of `tour/tours/<id>.json`
instead of passing `"loop":true` on every start.

### Interrupt with 🎤 / drive Earth by voice

Press the 🎤 button (bottom-right of the VRM page) and speak; the avatar answers
by voice. **Starting a recording auto-pauses the tour**, which resumes when the
answer finishes.

You can also **command a destination by voice** — say e.g. *"Take me to Tokyo
Tower"* / 「東京タワーを案内して」 and **Google Earth flies there and the
background switches**, no `curl` needed. When the transcript carries a "go/guide"
intent, an LLM extracts the place name and a flyTo is sent to earth-bridge. The
flyTo runs in parallel with narration, so the avatar still describes the place.

### One-off flyTo (debug)

```bash
curl -X POST http://localhost:8002/control \
  -H 'Content-Type: application/json' -d '{"cmd":"flyto","place":"Eiffel Tower"}'
```

### Add your own tour

Create `tour/tours/<id>.json` (use `world.json` as a template). The `id` is what
you pass to `/tour/start`. Edit each stop's `query` (search term) and `prompt`.

---

## 7. Stop

```bash
./stop_all.sh                  # stop everything (incl. VOICEVOX container)
./stop_all.sh --keep-voicevox  # keep VOICEVOX running
```

---

## 8. Troubleshooting

| Symptom | Check |
| --- | --- |
| No Earth / no background | `DISPLAY` correct? `http://localhost:8002/health` shows `controller_connected:true, have_frame:true`? |
| No sound | Click the VRM page once (user gesture); confirm VOICEVOX (50021) is up |
| Symlink BROKEN | Is AIassistant at `../AIassistant` and set up? |
| Tour won't start | `curl http://localhost:8003/tour/status` and the `tour` window in `tmux attach -t earthtour` |
| Crash on long recording | WhisperX is unstable past 60 s on ROCm (VAD caps at 55 s) |

For constraints and design details, see **[TECHNICAL.md](TECHNICAL.md)**.

---

## License

Apache-2.0 (matching the AIassistant base).
