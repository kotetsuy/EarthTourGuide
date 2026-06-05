#!/usr/bin/env python3
"""
Phase 0 confirmation: prove the in-app search flyTo is a *smooth trajectory*,
not a cut. Loads Earth at Tokyo, runs one in-app search to a far target, and
saves a timestamped frame every ~150ms during the flight so we can scrub it.
"""
import asyncio
import base64
import hashlib
import time
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).parent / "spike_out" / "flight"
OUT.mkdir(parents=True, exist_ok=True)

START = "https://earth.google.com/web/@35.6586,139.7454,1500a,4000d,35y,0h,45t,0r"
QUERY = "Sydney Opera House"


async def drive_search(page, query):
    """Open the top search box and submit a query. Robust against shadow DOM."""
    js = r"""
    (q) => {
      const find = (root) => {
        for (const el of root.querySelectorAll('*')) {
          if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el;
          if (el.shadowRoot) { const r = find(el.shadowRoot); if (r) return r; }
        }
        return null;
      };
      const inp = find(document);
      if (!inp) return false;
      inp.focus(); inp.value = '';
      return true;
    }
    """
    await page.keyboard.press("Slash")
    await page.wait_for_timeout(500)
    if not await page.evaluate(js, query):
        # fall back: '/' shortcut already focused it
        pass
    await page.keyboard.type(query, delay=40)
    await page.wait_for_timeout(1500)   # let autocomplete settle
    await page.keyboard.press("Enter")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, channel="chrome",
            args=["--no-first-run", "--window-size=1366,768"])
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Page.enable")

        seq = []          # (t, bytes)
        last_sha = [None]
        t0 = [None]

        async def on_frame(params):
            try:
                await cdp.send("Page.screencastFrameAck",
                               {"sessionId": params["sessionId"]})
            except Exception:
                pass
            if t0[0] is None:
                return
            data = base64.b64decode(params["data"])
            sha = hashlib.sha1(data).hexdigest()
            if sha != last_sha[0]:
                seq.append((time.monotonic() - t0[0], data))
            last_sha[0] = sha

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast",
                       {"format": "jpeg", "quality": 70,
                        "maxWidth": 1280, "maxHeight": 720, "everyNthFrame": 1})

        print("Loading Earth at Tokyo ...")
        await page.goto(START, wait_until="domcontentloaded")
        await page.wait_for_timeout(13000)   # boot + settle

        print(f"Search flyTo -> {QUERY}")
        t0[0] = time.monotonic()             # start recording window NOW
        await drive_search(page, QUERY)
        await page.wait_for_timeout(9000)     # capture the whole flight

        await cdp.send("Page.stopScreencast")
        await page.screenshot(path=str(OUT / "final.png"))

        # Save a frame roughly every 0.4s of the flight window for scrubbing.
        print(f"captured {len(seq)} distinct frames over "
              f"{seq[-1][0]:.1f}s" if seq else "no frames")
        next_t = 0.0
        saved = 0
        for t, data in seq:
            if t >= next_t:
                (OUT / f"t{t:05.2f}.jpg").write_bytes(data)
                saved += 1
                next_t += 0.4
        print(f"saved {saved} sampled frames to {OUT}")
        # measure inter-frame cadence during the active second 1-4s
        active = [t for (t, _) in seq if 1.0 <= t <= 4.0]
        if len(active) > 2:
            gaps = [active[i+1]-active[i] for i in range(len(active)-1)]
            avg = sum(gaps)/len(gaps)
            print(f"during flight 1-4s: {len(active)} frames, "
                  f"~{1/avg:.0f} fps avg, max gap {max(gaps)*1000:.0f}ms")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
