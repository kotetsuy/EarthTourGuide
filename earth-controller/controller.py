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
import re

import aiohttp
from playwright.async_api import async_playwright

BRIDGE_WS = os.getenv("EARTH_BRIDGE_WS", "ws://localhost:8002/ingest")

# Earth 起動時の初期位置（@lat,lng,alt(a),dist(d),heading(y),0h,tilt(t),0r）
HOME_LAT, HOME_LNG = 35.6586, 139.7454
HOME_URL = (
    "https://earth.google.com/web/"
    f"@{HOME_LAT},{HOME_LNG},1500a,9000d,35y,0h,45t,0r"
)

# ブート完了判定の許容値。初期位置に着けていれば lat/lng は誤差 1 度以内、
# カメラ距離は 9000d 指定なので 100km も離れていない。
HOME_TOL_DEG = 1.0
HOME_MAX_DIST_M = 100_000.0

# URL 内のカメラ状態 "@lat,lng,alt a,dist d" を読むための正規表現。
_CAM_RE = re.compile(
    r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),"
    r"(-?\d+(?:\.\d+)?)a,(-?\d+(?:\.\d+)?)d")

# shadow DOM を辿って「実際にフォーカスされている要素」を取る。
# Earth は初回検索後に不可視の input を DOM に複数残すため、
# 「最初に見つかった input」を掴む方式（旧実装）は 2 回目以降に
# 不可視要素へ打鍵してしまい無反応になる。'/' で検索を開いた直後の
# activeElement だけが信用できる。
_ACTIVE_INPUT_DECL = """
    const __activeInput = () => {
        let e = document.activeElement;
        while (e && e.shadowRoot && e.shadowRoot.activeElement) {
            e = e.shadowRoot.activeElement;
        }
        if (e && (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA')) return e;
        return null;
    };
"""


def _input_js(body: str) -> str:
    """__activeInput() を使う body を page.evaluate 可能なアロー関数に包む。"""
    return "() => {" + _ACTIVE_INPUT_DECL + body + "}"


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
        await self.goto_home()

    # ---- ブート待ち ------------------------------------------------------
    # 以前はここが wait_for_timeout(13000) の固定待ちだった。start_all.sh から
    # 起動すると llama の 22GB ロードと WhisperX warmup で負荷が高く、13 秒では
    # Earth の WASM ブートが終わらないことがある。その状態で screencast と
    # コマンド受付を始めてしまうと、初期位置が反映されないまま地球全体が映り、
    # 以後の flyTo も検索ボックスに届かず無反応になる（無言のまま止まる）。
    # 実際に初期位置へ着いたことを確認してから先に進める。

    def camera(self):
        """現在の URL からカメラ (lat, lng, dist_m) を読む。Earth はカメラ移動に
        追従して URL を書き換えるので、これが外から見える唯一の位置情報。"""
        m = _CAM_RE.search(self.page.url or "")
        if not m:
            return None
        return float(m.group(1)), float(m.group(2)), float(m.group(4))

    def app_initialized(self) -> bool:
        """Earth の WASM アプリが起動して URL を書き換えたか。

        Earth は初期化を終えると自分で URL に "/data=..." を付け足す。ブート前は
        こちらが渡した URL のままなので、これが起動完了の外から見える印になる。

        検索ボックスの有無は判定に使えない。Earth の検索 input は '/' を押すまで
        DOM に生成されず、確認のために押すと開いた状態が残って直後の flyTo の
        打鍵が入らなくなる（副作用のない判定であることが重要）。
        """
        return "/data=" in (self.page.url or "")

    async def wait_app_initialized(self, timeout: float = 30.0) -> bool:
        """WASM アプリの起動完了（URL への /data= 付与）を待つ。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self.app_initialized():
                return True
            await self.page.wait_for_timeout(1000)
        return False

    async def wait_ready(self, timeout: float = 45.0) -> bool:
        """初期位置に着き、検索ボックスも使える状態になるまで待つ。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            cam = self.camera()
            if (cam
                    and abs(cam[0] - HOME_LAT) < HOME_TOL_DEG
                    and abs(cam[1] - HOME_LNG) < HOME_TOL_DEG
                    and cam[2] < HOME_MAX_DIST_M
                    and self.app_initialized()):
                await self.page.wait_for_timeout(1500)   # 描画の整定ぶん
                return True
            await self.page.wait_for_timeout(1000)
        return False

    async def goto_home(self, attempts: int = 3,
                        per_attempt: float = 45.0) -> bool:
        """初期位置へ移動し、着いたことを確認する。駄目なら読み直して再試行。"""
        for n in range(1, attempts + 1):
            await self.page.goto(HOME_URL, wait_until="domcontentloaded")
            if await self.wait_ready(per_attempt):
                print(f"[controller] Earth ブート完了 (試行 {n}/{attempts})")
                return True
            cam = self.camera()
            print(f"[controller] Earth が初期位置に着きません "
                  f"(試行 {n}/{attempts}, camera={cam}); 再読込します")
        print("[controller] 警告: Earth を初期位置に落ち着かせられませんでした。"
              "flyTo が効かない可能性があります")
        return False

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

    # flyTo 到着判定: 検索フライトの着地はカメラ距離が数 km 以下になる。
    # 検索結果リスト表示（飛ばない失敗モード）は地球全体ビューの ~17,000km。
    ARRIVAL_MAX_DIST_M = 1_000_000.0

    async def fly_to(self, place: str, attempts: int = 3):
        """検索ボックス経由で place へ滑らかに flyTo。

        かつては '/' → input を探して focus → type → Enter を投げっぱなしに
        していたが、Earth は初回検索後に不可視の input を DOM に残すため、
        2 回目以降は不可視要素に打鍵してしまい「前回の検索語のまま無反応」に
        なることがある。また Enter が候補への飛行ではなく検索結果リスト表示に
        化けることもある（地球全体ビューで固まる）。そこで
          1. '/' で開いた直後の activeElement にだけ入力する
          2. 入力値を読み戻して確認してから Enter
          3. Enter 後にカメラが実際に降下したかで成否を判定する
        を各試行で行い、全滅したら URL 検索（テレポート）で確実に到着させる
        （滑らかさは失うが、展示が無言で止まるよりよい）。
        """
        page = self.page
        for n in range(1, attempts + 1):
            # 地図プロジェクト画面などに迷い込んでいたら地球へ戻す
            await self.ensure_globe()
            before = self.camera()

            # 残っているパネル／候補を畳んでから検索を開く
            await self.dismiss_panels()
            await page.wait_for_timeout(300)
            await page.keyboard.press("Slash")
            await page.wait_for_timeout(600)

            if not await page.evaluate(_input_js("return !!__activeInput();")):
                print(f"[controller] 検索ボックスにフォーカスできません "
                      f"(試行 {n}/{attempts})")
                continue

            # 前回の検索語を実キーイベントで全消去する。i.value='' は DOM 値を
            # 消すだけで Earth の検索 UI 内部状態に残り、追記されてしまう
            # （例: 「東京タワー」+「エッフェル塔」→「東京タワーエッフェル塔」）。
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(200)

            if n == 1:
                await page.keyboard.type(place, delay=60)
            else:
                # 再試行: insert_text は 1 つの入力イベントで丸ごと挿入する
                # ため、オートコンプリート再描画との競合で文字が欠落しない。
                await page.keyboard.insert_text(place)
            await page.wait_for_timeout(1500)     # オートコンプリート整定

            typed = await page.evaluate(
                _input_js("const i = __activeInput();"
                          " return i ? i.value : null;"))
            if (typed or "").strip() != place:
                print(f"[controller] 検索欄に入力できていません "
                      f"(試行 {n}/{attempts}, 実際の値={typed!r})")
                continue

            await page.keyboard.press("Enter")
            if await self._wait_arrival(before):
                return True
            print(f"[controller] Enter 後にカメラが降下しません "
                  f"(試行 {n}/{attempts}, camera={self.camera()})")

        # 最終手段: URL 検索でテレポート。WASM アプリごと読み直すので
        # アニメーション無しのカット移動になるが、確実に到着はする。
        print(f"[controller] flyTo({place}) が検索ボックス経由で成立せず、"
              "URL 検索（テレポート）に切り替えます")
        try:
            from urllib.parse import quote_plus
            await page.goto(
                f"https://earth.google.com/web/search/{quote_plus(place)}",
                wait_until="domcontentloaded")
            await self.wait_app_initialized(30.0)
            # テレポートでも地球に着けたかは確認する（プロジェクト画面に
            # 飛ばされた場合はここで気づけるようにしておく）
            if await self._wait_arrival(None, timeout=20.0):
                return True
            print(f"[controller] 警告: flyTo({place}) はテレポートでも"
                  "地球ビューに着けませんでした")
            return False
        except Exception as e:
            print(f"[controller] 警告: flyTo({place}) 失敗: {e}")
            return False

    async def _wait_arrival(self, before, timeout: float = 20.0) -> bool:
        """検索フライトの着地を待つ。

        「カメラ距離が閾値以下」だけでは不十分。Earth が地球ビューを離れて
        「地図プロジェクト」管理画面に遷移していると URL に前回の座標が残った
        ままになり、飛んでいないのに到着と誤判定してしまう。飛行前の座標から
        実際に**変化した**ことも条件にする。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            cam = self.camera()
            if (cam and cam != before
                    and cam[2] < self.ARRIVAL_MAX_DIST_M):
                return True
            await self.page.wait_for_timeout(1000)
        return False

    async def ensure_globe(self) -> bool:
        """地球ビューに居ることを保証する。

        Earth はプロモ表示や操作の巡り合わせで、地球ではなく「地図プロジェクト」
        管理画面に遷移してしまうことがある。この画面にも検索ボックスがあるため、
        気づかずに検索すると「入力は通るがカメラは動かない」状態に陥る。
        カメラ状態が URL から読めない＝地球ビューに居ないとみなして復帰させる。
        """
        if self.camera() is not None:
            return True
        print("[controller] 地球ビューを離れています。初期位置へ復帰します")
        return await self.goto_home(attempts=2)

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
