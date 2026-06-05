#!/usr/bin/env python3
"""
earth-controller: Playwright で earth.google.com を操作し、CDP スクリーンキャストの
JPEG フレームを earth-bridge (ws://localhost:8002/ingest) に送り続ける。
bridge から届く制御コマンド (flyto など) を受けて Earth のカメラを動かす。

  Browser earth.google.com  ──CDP screencast──►  EarthDriver
                            ◄──── flyTo 等 ────┘
  EarthDriver  ──WS frames/status──►  earth-bridge(8002)  ──►  three-vrm 背景
               ◄──── control JSON ───┘

EarthDriver 部分（flyTo / screencast）はフェーズ0スパイクで実証済み。
bridge との配線・コマンド体系はフェーズ2/3で詰める（下の TODO 参照）。

実行:  ./run.sh        (= .venv/bin/python controller.py)
"""
import argparse
import asyncio
import base64
import json
import os

import aiohttp
from playwright.async_api import async_playwright

BRIDGE_WS = os.getenv("EARTH_BRIDGE_WS", "ws://localhost:8002/ingest")

# Earth 起動時の初期位置（@lat,lng,alt(a),dist(d),heading(y),0h,tilt(t),0r）
HOME_URL = (
    "https://earth.google.com/web/"
    "@35.6586,139.7454,1500a,9000d,35y,0h,45t,0r"
)


def earth_url(lat: float, lng: float, alt: int = 1500,
              dist: int = 6000, tilt: int = 45) -> str:
    return (f"https://earth.google.com/web/"
            f"@{lat},{lng},{alt}a,{dist}d,35y,0h,{tilt}t,0r")


class EarthDriver:
    """earth.google.com をヘッド付き Chrome で操作するドライバ。

    フェーズ0で実証済みの要点:
      * 滑らかな flyTo は「検索ボックス」経由でのみ可能（URLは瞬間移動=再読込）。
      * CDP Page.startScreencast は各フレームの ack 必須（怠ると停止）。
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self.browser = None
        self.page = None
        self.cdp = None
        self.on_frame = None          # async callable(bytes) -> None
        self._last_sha = None

    async def start(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.headless, channel="chrome",
            args=["--no-first-run", "--window-size=1366,768"])
        ctx = await self.browser.new_context(
            viewport={"width": 1366, "height": 768})
        self.page = await ctx.new_page()
        self.cdp = await ctx.new_cdp_session(self.page)
        await self.cdp.send("Page.enable")
        # 初期位置は URL で一発設定（ここは瞬間移動で問題ない）
        await self.page.goto(HOME_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(13000)   # WASM ブート + 整定

    async def start_screencast(self, quality: int = 70,
                               max_w: int = 1280, max_h: int = 720):
        self.cdp.on("Page.screencastFrame", self._handle_frame)
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": quality,
            "maxWidth": max_w, "maxHeight": max_h, "everyNthFrame": 1})

    async def _handle_frame(self, params):
        # ack を最優先（怠ると stream が止まる）
        try:
            await self.cdp.send("Page.screencastFrameAck",
                                {"sessionId": params["sessionId"]})
        except Exception:
            pass
        if self.on_frame is None:
            return
        data = base64.b64decode(params["data"])
        try:
            await self.on_frame(data)
        except Exception:
            pass

    async def fly_to(self, place: str):
        """検索ボックス経由で place へ滑らかに flyTo（実証済みの手順）。"""
        page = self.page
        # '/' で検索にフォーカス → shadow DOM を貫いて input を掴む
        await page.keyboard.press("Slash")
        await page.wait_for_timeout(500)
        await page.evaluate(
            """() => { const find = (r) => { for (const el of
               r.querySelectorAll('*')) { if (el.tagName==='INPUT'||
               el.tagName==='TEXTAREA') return el; if (el.shadowRoot){
               const x=find(el.shadowRoot); if(x) return x; } } return null; };
               const i=find(document); if(i){ i.focus(); i.value=''; } }""")
        await page.keyboard.type(place, delay=40)
        await page.wait_for_timeout(1500)     # オートコンプリート整定
        await page.keyboard.press("Enter")

    async def dismiss_panels(self):
        """到着後の情報パネル/候補ドロップダウンを閉じる（背景を綺麗に）。"""
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

    async def close(self):
        try:
            await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()


async def run(args):
    driver = EarthDriver(headless=args.headless)
    session: aiohttp.ClientSession | None = None
    ws: aiohttp.ClientWebSocketResponse | None = None

    async def push_frame(data: bytes):
        if ws is not None and not ws.closed:
            try:
                await ws.send_bytes(data)
            except Exception:
                pass

    driver.on_frame = push_frame
    await driver.start()
    await driver.start_screencast()
    print(f"[controller] Earth ready; connecting to bridge {BRIDGE_WS}")

    # bridge への接続はベストエフォート（落ちていてもローカル動作は継続）
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(BRIDGE_WS, max_msg_size=0)
        print("[controller] bridge connected")
    except Exception as e:
        print(f"[controller] bridge 未接続 ({e}); フレーム配信なしで継続")

    # bridge からの制御コマンドを処理するループ
    if ws is not None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    cmd = json.loads(msg.data)
                except Exception:
                    continue
                kind = cmd.get("cmd")
                if kind == "flyto":
                    place = cmd.get("place") or cmd.get("query")
                    if place:
                        print(f"[controller] flyto -> {place}")
                        await driver.fly_to(place)
                elif kind == "dismiss":
                    await driver.dismiss_panels()
                elif kind == "ping":
                    await ws.send_str('{"event":"pong"}')
            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.ERROR):
                break
    else:
        # bridge 無しでも screencast は回り続ける（手動確認用）
        while True:
            await asyncio.sleep(3600)

    await driver.close()
    if session:
        await session.close()


def parse_args():
    ap = argparse.ArgumentParser(description="earth-controller")
    ap.add_argument("--headless", action="store_true",
                    help="ヘッドレス起動（WebGL がソフト描画になり遅い可能性）")
    return ap.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass
