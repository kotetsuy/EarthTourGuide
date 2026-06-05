#!/usr/bin/env python3
"""
Phase 0 feasibility spike for EarthTourGuide.

Goal: determine whether earth.google.com (the real WASM app) can be driven by
Playwright to produce a *smooth* flyTo animation (not a teleport), and whether
CDP Page.startScreencast can capture frames at a usable rate/latency.

We test two navigation strategies:
  (A) URL navigation  -> page.goto("https://earth.google.com/web/@lat,lng,...")
  (B) in-app search   -> open the search UI and type a place name + Enter

For each strategy we keep a CDP screencast running and count how many *distinct*
frames arrive in the seconds after we trigger navigation. A smooth flyTo produces
many distinct intermediate frames; a teleport produces ~0-1 transition frames.

Run:
  cd earth-controller && ./.venv/bin/python spike.py
Frames + screenshots land in ./spike_out/
"""
import asyncio
import base64
import hashlib
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).parent / "spike_out"
OUT.mkdir(exist_ok=True)

# (name, lat, lng, alt_meters)
WAYPOINTS = [
    ("tokyo_tower", 35.6586, 139.7454, "tokyo tower"),
    ("eiffel_tower", 48.8584, 2.2945, "eiffel tower"),
    ("grand_canyon", 36.1069, -112.1129, "grand canyon"),
]


def earth_url(lat, lng, alt=1500, dist=4000, tilt=45):
    # @lat,lng,altitude(a),distance(d),heading(y),0h,tilt(t),0r
    return (
        f"https://earth.google.com/web/@{lat},{lng},{alt}a,{dist}d,35y,0h,{tilt}t,0r"
    )


class Screencast:
    """Collects CDP screencast frames and records distinct-frame timeline."""

    def __init__(self, cdp):
        self.cdp = cdp
        self.frames = []          # list of (t, sha) for distinct frames
        self.last_sha = None
        self.count = 0
        self.save_every = None    # set to int to dump every Nth distinct frame
        self._t0 = time.monotonic()

    async def start(self, fmt="jpeg", quality=60, max_w=1280, max_h=720):
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send(
            "Page.startScreencast",
            {"format": fmt, "quality": quality, "maxWidth": max_w,
             "maxHeight": max_h, "everyNthFrame": 1},
        )

    async def _on_frame(self, params):
        # MUST ack or the stream stalls
        try:
            await self.cdp.send("Page.screencastFrameAck",
                                 {"sessionId": params["sessionId"]})
        except Exception:
            pass
        data = base64.b64decode(params["data"])
        sha = hashlib.sha1(data).hexdigest()
        self.count += 1
        if sha != self.last_sha:
            t = time.monotonic() - self._t0
            self.frames.append((t, sha))
            if self.save_every and len(self.frames) % self.save_every == 0:
                (OUT / f"frame_{len(self.frames):04d}.jpg").write_bytes(data)
        self.last_sha = sha

    def distinct_since(self, t_start):
        return [f for f in self.frames if f[0] >= t_start]

    async def stop(self):
        try:
            await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass


async def dismiss_popups(page):
    """Best-effort: close cookie/consent/intro dialogs."""
    for label in ["Accept all", "I agree", "Got it", "No thanks", "Dismiss",
                  "すべて承諾", "同意する", "OK"]:
        try:
            btn = page.get_by_role("button", name=label)
            if await btn.count() > 0:
                await btn.first.click(timeout=1500)
                print(f"  dismissed popup: {label}")
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def probe_dom(page):
    """Walk shadow DOM and report candidate search inputs / buttons."""
    js = r"""
    () => {
      const out = [];
      const visit = (root) => {
        const els = root.querySelectorAll('*');
        for (const el of els) {
          const tag = el.tagName.toLowerCase();
          const aria = el.getAttribute && el.getAttribute('aria-label');
          if (tag === 'input' || tag === 'textarea' ||
              (aria && /search|検索/i.test(aria))) {
            out.push({tag, aria, id: el.id || null,
                      ph: el.getAttribute && el.getAttribute('placeholder')});
          }
          if (el.shadowRoot) visit(el.shadowRoot);
        }
      };
      visit(document);
      return out;
    }
    """
    try:
        res = await page.evaluate(js)
        print("  DOM probe (search candidates):")
        for r in res[:20]:
            print("   ", r)
        return res
    except Exception as e:
        print("  DOM probe failed:", e)
        return []


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--use-gl=angle", "--ignore-gpu-blocklist",
                  "--enable-unsafe-webgpu", "--no-first-run",
                  "--window-size=1366,768"],
        )
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Page.enable")

        sc = Screencast(cdp)
        await sc.start()
        sc.save_every = 8  # dump a sampling of frames to eyeball

        print(f"[1] Loading Earth at {WAYPOINTS[0][0]} via URL ...")
        await page.goto(earth_url(WAYPOINTS[0][1], WAYPOINTS[0][2]),
                        wait_until="domcontentloaded")
        # Earth WASM needs time to boot + render the globe
        await page.wait_for_timeout(12000)
        await dismiss_popups(page)
        await page.wait_for_timeout(4000)
        await page.screenshot(path=str(OUT / "00_loaded.png"))
        print(f"  total screencast frames so far: {sc.count}, "
              f"distinct: {len(sc.frames)}")

        await probe_dom(page)

        # ---- Strategy A: URL navigation to next waypoint ----
        print("\n[2] Strategy A: URL navigation -> Eiffel Tower")
        tA = time.monotonic() - sc._t0
        await page.goto(earth_url(WAYPOINTS[1][1], WAYPOINTS[1][2]),
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(10000)
        distinctA = sc.distinct_since(tA)
        await page.screenshot(path=str(OUT / "01_urlnav_eiffel.png"))
        print(f"  distinct frames during 10s after URL nav: {len(distinctA)}")
        print("  (high count over a sustained window => animated; "
              "URL nav usually reloads the app => teleport)")

        # ---- Strategy B: in-app search ----
        print("\n[3] Strategy B: in-app search -> Grand Canyon")
        ok = await try_search(page, "grand canyon")
        if ok:
            tB = time.monotonic() - sc._t0
            await page.wait_for_timeout(10000)
            distinctB = sc.distinct_since(tB)
            await page.screenshot(path=str(OUT / "02_search_grandcanyon.png"))
            print(f"  distinct frames during 10s after search: {len(distinctB)}")
        else:
            print("  search UI not driven automatically; needs manual selector work")

        await sc.stop()
        print(f"\nDone. total frames={sc.count} distinct={len(sc.frames)}")
        print(f"Artifacts in {OUT}")
        # Keep window up briefly for visual confirmation
        await page.wait_for_timeout(3000)
        await browser.close()


async def try_search(page, query):
    """Attempt to open Earth's search and submit a query. Returns True if typed."""
    # Earth web: a search button in the left rail toggles a search input.
    # We try keyboard shortcut '/' first (focuses search in many Google apps),
    # then fall back to clicking an aria-labelled search control.
    try:
        await page.keyboard.press("Slash")
        await page.wait_for_timeout(800)
    except Exception:
        pass
    # Try to find a focusable search input via shadow-dom-piercing JS click
    js_focus = r"""
    () => {
      const find = (root) => {
        for (const el of root.querySelectorAll('*')) {
          const aria = el.getAttribute && el.getAttribute('aria-label');
          if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) return el;
          if (aria && /search|検索/i.test(aria)) { el.click(); }
          if (el.shadowRoot) { const r = find(el.shadowRoot); if (r) return r; }
        }
        return null;
      };
      const inp = find(document);
      if (inp) { inp.focus(); return true; }
      return false;
    }
    """
    try:
        focused = await page.evaluate(js_focus)
    except Exception as e:
        print("    focus js failed:", e)
        focused = False
    if not focused:
        return False
    await page.keyboard.type(query, delay=50)
    await page.wait_for_timeout(1200)
    await page.keyboard.press("Enter")
    print(f"    typed '{query}' + Enter")
    return True


if __name__ == "__main__":
    asyncio.run(main())

# =============================================================================
# CONCLUSION (Phase 0 spike, 2026-06-05)  -- see SPIKE_FINDINGS.md for detail
# -----------------------------------------------------------------------------
# VERDICT: Smooth flyTo IS achievable. Recommended method = in-app search box.
#
# * In-app SEARCH (top-left box, reached via '/' shortcut + keyboard typing,
#   wait ~1.5s for autocomplete, then Enter) triggers Earth's NATIVE camera
#   flight: a continuous ~6s arc (zoom out -> cross globe -> zoom in).
#   Verified frame-by-frame Tokyo->Sydney in spike_confirm.py. THIS IS THE WAY.
#
# * URL navigation (page.goto to @lat,lng,...) lands at the right place but
#   RELOADS the WASM app => hard teleport, no animation. Use ONLY for the
#   very first/initial position, never for in-tour transitions.
#
# * CDP Page.startScreencast works well for the live-background plan:
#   ~22 fps @ 1280x720 JPEG q70, worst-case inter-frame gap ~0.5s during heavy
#   tile streaming. Must ack every frame (Page.screencastFrameAck) or it stalls.
#
# * NOTE the distinct-frame *count* is NOT a good "is it animating?" signal:
#   Earth renders continuously (atmosphere/tiles) even when idle, so all phases
#   show high counts. Judge motion by scrubbing actual frames, not by counts.
#
# CAVEAT for Phase 2 (background): search leaves an autocomplete dropdown + a
# right-side info panel over the globe. For a clean demo background we'll want
# to hide Earth's UI chrome / dismiss those panels after arrival (Escape/close),
# or crop the screencast region.
# =============================================================================
