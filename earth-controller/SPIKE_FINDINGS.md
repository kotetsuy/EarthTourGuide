# Phase 0 Feasibility Spike — Findings

**Date:** 2026-06-05
**Question (from HANDOFF §4):** Can Playwright drive the real `earth.google.com`
WASM app to produce a *smooth* flyTo (not a teleport), and is CDP
`Page.startScreencast` good enough to use as a live background?

**Verdict: YES — proceed with the planned "real Earth + Playwright + CDP
screencast" architecture. No need to fall back to Google Maps 3D Tiles.**

## Setup
- `google-chrome-stable` driven headed via Playwright `channel="chrome"` on
  `DISPLAY=:10.0` (GNOME Remote Desktop X session).
- venv: `earth-controller/.venv` (Playwright 1.60.0). No chromium download —
  uses the system Chrome.
- Scripts: `spike.py` (3-strategy probe), `spike_confirm.py` (frame-by-frame
  flight capture). Artifacts in `earth-controller/spike_out/`.

## Results

### 1. In-app search box → SMOOTH flyTo ✅  (recommended method)
Reaching the top-left search box (press `/` to focus, type the place, wait
~1.5 s for autocomplete to settle, press `Enter`) triggers Earth's **native
camera flight**. Captured Tokyo → Sydney Opera House frame by frame:

| t (s) | view |
|-------|------|
| 0.5 | Tokyo Tower, zoomed in (start) |
| 4.4 | zoomed out over greater Tokyo |
| 6.4 | whole of Australia, marker on Sydney coast |
| ~7  | zoomed into Sydney Opera House (arrival) |

A continuous ~6 s "zoom out → cross globe → zoom in" arc. Because search keeps
the WASM app alive (no reload), Earth animates the camera itself.

### 2. URL navigation (`page.goto @lat,lng,…`) → TELEPORT ❌
Lands at the correct place but **reloads the whole WASM app**, so it cuts
instantly with no animation. Use only to set the *initial* position before the
tour starts; never for in-tour transitions.

### 3. CDP `Page.startScreencast` → good enough ✅
- ~22 fps average at 1280×720, JPEG quality 70.
- Worst-case inter-frame gap ~0.5 s during heavy tile streaming; otherwise
  smooth. Acceptable for an exhibition background.
- **Must** `Page.screencastFrameAck` every frame or the stream stalls.

### Gotchas / notes for later phases
- Distinct-frame *count* is a bad "is it moving?" signal — Earth renders
  continuously (atmosphere, streaming tiles) even when idle. Judge motion by
  scrubbing real frames.
- After a search, Earth leaves an autocomplete dropdown + a right-side info
  panel over the globe. For a clean Phase-2 background we'll want to hide
  Earth's UI chrome / dismiss those panels (Escape / close button) or crop the
  captured region.
- Headed mode needs a real display; the env's `DISPLAY=:10.0` works. For an
  unattended/exhibition box we may want a dedicated X session or xvfb (but xvfb
  has no GPU → WebGL software render may be slow; prefer a real GPU display).

## Recommendation for `earth-controller`
Drive transitions exclusively through the in-app search box; expose a
`fly_to(place_name)` that focuses search, types, waits for autocomplete, hits
Enter, and (optionally) dismisses the info panel after arrival. Keep one
persistent CDP screencast feeding `earth-bridge`.
