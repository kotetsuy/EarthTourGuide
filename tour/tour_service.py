#!/usr/bin/env python3
"""
tour サービス (port 8003): EarthTourGuide のツアー進行ロジック専用サービス。

tour/tours/*.json を読み、各地点について
  1. earth-bridge へ flyTo 指示 (POST /control {"cmd":"flyto","place":...})
  2. 到着後に情報パネルを閉じる ({"cmd":"dismiss"})
  3. ttllm /chat で解説文を生成
  4. VOICEVOX /audio_query で発話長を見積もり
  5. three-vrm /speak で VRM に喋らせる
  6. 発話長 + dwell だけ滞在して次へ
を繰り返す。pause/resume を持ち、🎤 割り込み質問のときはブラウザから
/tour/pause が呼ばれて進行が止まる（質問が終わると /tour/resume で再開）。

エンドポイント:
  GET  /health
  GET  /tour/list                 利用可能なツアー一覧
  GET  /tour/status               実行状態（running/paused/現在地点/index）
  POST /tour/start  {"id":"world"} 自動巡回を開始
  POST /tour/stop                 停止
  POST /tour/pause                一時停止（🎤 割り込み時）
  POST /tour/resume               再開
  POST /tour/next                 次の地点へスキップ
"""
import asyncio
import glob
import json
import os
from pathlib import Path

import aiohttp
from aiohttp import web

PORT = 8003
TOURS_DIR = Path(__file__).parent / "tours"

EARTH_BRIDGE_URL = os.getenv("EARTH_BRIDGE_URL", "http://localhost:8002")
TTLLM_URL = os.getenv("TTLLM_URL", "http://localhost:8001")
THREE_VRM_URL = os.getenv("THREE_VRM_URL", "http://localhost:8000")
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://localhost:50021")

# 各種デフォルト（ツアー JSON の defaults で上書き可）
DEFAULTS = {
    "fly_seconds": 10.0,     # flyTo アニメーションの待ち時間
    "dwell_seconds": 6.0,    # 解説後に滞在する時間
    "speaker_id": 3,         # VOICEVOX 話者（3=ずんだもん ノーマル）
    "speech_buffer": 0.8,    # 発話見積りに足す余裕
    "max_tokens": 220,
    "temperature": 0.7,
    "system": None,          # 解説の system プロンプト（None なら ttllm 既定）
}


def load_tours() -> dict:
    tours = {}
    for path in sorted(glob.glob(str(TOURS_DIR / "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            tid = data.get("id") or Path(path).stem
            tours[tid] = data
        except Exception as e:
            print(f"[tour] failed to load {path}: {e}")
    return tours


class TourRunner:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self._resume = asyncio.Event()
        self._resume.set()                  # 既定は「動作中（非ポーズ）」
        self._stop = False
        # 状態
        self.tour_id: str | None = None
        self.tour_title: str | None = None
        self.index = -1
        self.stop_name: str | None = None
        self.phase = "idle"                 # idle|flying|narrating|dwelling|done
        self._skip = False
        self.loop = False                   # True なら最後の地点の後に先頭へ戻る

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def status(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "tour_id": self.tour_id,
            "tour_title": self.tour_title,
            "index": self.index,
            "stop": self.stop_name,
            "phase": self.phase,
            "loop": self.loop,
        }

    async def start(self, tour: dict, loop: bool = False):
        await self.stop()
        self._stop = False
        self._skip = False
        self._resume.set()
        self.loop = loop
        self.tour_id = tour.get("id")
        self.tour_title = tour.get("title")
        self.index = -1
        self.phase = "starting"
        self.task = asyncio.create_task(self._run(tour))

    async def stop(self):
        self._stop = True
        self._resume.set()                  # ポーズ中でも抜けられるように
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self.phase = "idle"
        self.stop_name = None

    def pause(self):
        self._resume.clear()

    def resume(self):
        self._resume.set()

    def skip(self):
        self._skip = True
        self._resume.set()

    async def _sleep(self, secs: float):
        """停止/ポーズ/スキップに反応しながら secs 秒待つ。ポーズ中は時間が止まる。"""
        remaining = secs
        while remaining > 0:
            if self._stop or self._skip:
                return
            if not self._resume.is_set():
                await self._resume.wait()
                continue
            step = min(0.2, remaining)
            await asyncio.sleep(step)
            remaining -= step

    async def _gate(self):
        """ポーズ中ならここで待機（各ステップの先頭で呼ぶ）。"""
        if not self._resume.is_set():
            await self._resume.wait()

    async def _run(self, tour: dict):
        cfg = {**DEFAULTS, **(tour.get("defaults") or {})}
        stops = tour.get("stops") or []
        async with aiohttp.ClientSession() as s:
            # 開始時に字幕をクリア（前回の音声入力ダイアログ等を残さない）。
            await self._post(s, f"{THREE_VRM_URL}/clear", {})
            while True:
                await self._run_once(s, stops, cfg)
                if self._stop or not self.loop:
                    break
        self.phase = "done"
        self.stop_name = None

    async def _run_once(self, s, stops, cfg):
        for i, stop in enumerate(stops):
            if self._stop:
                break
            self._skip = False
            self.index = i
            self.stop_name = stop.get("name") or stop.get("query")
            await self._gate()

            # 1) flyTo
            self.phase = "flying"
            place = stop.get("query") or stop.get("name")
            await self._post(s, f"{EARTH_BRIDGE_URL}/control",
                             {"cmd": "flyto", "place": place})
            await self._sleep(stop.get("fly_seconds", cfg["fly_seconds"]))
            if self._stop:
                break
            # 2) 到着後パネルを閉じて背景を綺麗に
            await self._post(s, f"{EARTH_BRIDGE_URL}/control",
                             {"cmd": "dismiss"})

            if self._skip:
                continue
            await self._gate()

            # 3) 解説文生成
            self.phase = "narrating"
            reply = await self._narrate(s, stop, cfg)

            # 4) 発話 + 5) 滞在
            if reply:
                secs = await self._estimate_speech(
                    s, reply, cfg["speaker_id"])
                await self._post(s, f"{THREE_VRM_URL}/speak",
                                 {"text": reply,
                                  "speaker_id": cfg["speaker_id"]})
                await self._sleep((secs or 0) + cfg["speech_buffer"])

            self.phase = "dwelling"
            await self._sleep(stop.get("dwell_seconds", cfg["dwell_seconds"]))

    async def _narrate(self, s, stop, cfg) -> str | None:
        prompt = stop.get("prompt") or (
            f"{stop.get('name')} について観光ガイドとして2〜3文で解説して。")
        payload = {
            "text": prompt,
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        }
        if cfg.get("system"):
            payload["system"] = cfg["system"]
        data = await self._post(s, f"{TTLLM_URL}/chat", payload, want_json=True)
        if isinstance(data, dict):
            return (data.get("reply") or "").strip() or None
        return None

    async def _estimate_speech(self, s, text, speaker_id) -> float | None:
        """VOICEVOX audio_query から発話秒数を概算（合成はしない）。"""
        try:
            async with s.post(f"{VOICEVOX_URL}/audio_query",
                              params={"text": text, "speaker": speaker_id}) as r:
                if r.status != 200:
                    return self._fallback_secs(text)
                q = await r.json()
        except Exception:
            return self._fallback_secs(text)
        total = (q.get("prePhonemeLength") or 0.0) + (q.get("postPhonemeLength") or 0.0)
        for ap in q.get("accent_phrases", []):
            for m in ap.get("moras", []):
                total += (m.get("consonant_length") or 0.0) + (m.get("vowel_length") or 0.0)
            pm = ap.get("pause_mora")
            if pm:
                total += (pm.get("vowel_length") or 0.0)
        speed = q.get("speedScale") or 1.0
        return total / speed if speed else total

    @staticmethod
    def _fallback_secs(text: str) -> float:
        return max(2.5, len(text) * 0.18)

    async def _post(self, s, url, payload, want_json=False):
        try:
            async with s.post(url, json=payload) as r:
                if want_json:
                    if r.status == 200:
                        return await r.json()
                    print(f"[tour] {url} -> {r.status}: {await r.text()}")
                    return None
                if r.status >= 400:
                    print(f"[tour] {url} -> {r.status}: {await r.text()}")
        except Exception as e:
            print(f"[tour] POST {url} failed: {e}")
        return None


runner = TourRunner()


# ---- HTTP handlers ------------------------------------------------------
async def health(_req):
    return web.json_response({"status": "ok", **runner.status()})


async def tour_list(_req):
    tours = load_tours()
    return web.json_response({"tours": [
        {"id": t.get("id"), "title": t.get("title"),
         "stops": len(t.get("stops") or [])}
        for t in tours.values()]})


async def tour_status(_req):
    return web.json_response(runner.status())


async def tour_start(req):
    try:
        body = await req.json()
    except Exception:
        body = {}
    tid = body.get("id") or "world"
    tours = load_tours()
    if tid not in tours:
        return web.json_response(
            {"error": f"unknown tour '{tid}'",
             "available": list(tours)}, status=404)
    # loop は body 優先、無ければツアー JSON の "loop"、それも無ければ False。
    loop = body.get("loop")
    if loop is None:
        loop = bool(tours[tid].get("loop", False))
    await runner.start(tours[tid], loop=bool(loop))
    return web.json_response({"status": "started", **runner.status()})


async def tour_stop(_req):
    await runner.stop()
    return web.json_response({"status": "stopped", **runner.status()})


async def tour_pause(_req):
    runner.pause()
    return web.json_response({"status": "paused", **runner.status()})


async def tour_resume(_req):
    runner.resume()
    return web.json_response({"status": "resumed", **runner.status()})


async def tour_next(_req):
    runner.skip()
    return web.json_response({"status": "skipping", **runner.status()})


def make_app():
    app = web.Application()
    app.add_routes([
        web.get("/health", health),
        web.get("/tour/list", tour_list),
        web.get("/tour/status", tour_status),
        web.post("/tour/start", tour_start),
        web.post("/tour/stop", tour_stop),
        web.post("/tour/pause", tour_pause),
        web.post("/tour/resume", tour_resume),
        web.post("/tour/next", tour_next),
    ])
    return app


if __name__ == "__main__":
    print(f"tour service: http://localhost:{PORT}  "
          f"(POST /tour/start {{\"id\":\"world\"}})")
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
