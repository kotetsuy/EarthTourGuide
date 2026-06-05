#!/usr/bin/env python3
"""
earth-bridge (port 8002): earth-controller が送る Earth のライブフレームを受け取り、
three-vrm（ブラウザ背景）や preview に WebSocket でファンアウトする中継ハブ。
あわせて flyTo 等の制御コマンドを controller に転送する HTTP 入口も持つ。

  earth-controller ──ws /ingest──►  [ bridge:8002 ]  ──ws /stream──►  three-vrm / preview
  tour ロジック   ──POST /control─►        └────── ws /ingest 経由で controller へ ──┘

エンドポイント:
  GET  /health           ヘルスチェック（start_all.sh が待つ）
  WS   /ingest           controller 専用入口（binary=JPEGフレーム, text=status）
  WS   /stream           視聴者（three-vrm / preview）がフレームを受け取る
  POST /control {cmd,..} 制御コマンド（controller に転送）。例: {"cmd":"flyto","place":"Eiffel Tower"}
  GET  /preview          フレーム確認用の簡易ビューア（フェーズ2開発用）

注: フレームは binary でそのまま転送（base64 にしない）。最新フレームのみ保持し、
新規視聴者には即座に最後のフレームを送る（黒画面待ちを避ける）。
"""
import asyncio
import weakref

from aiohttp import web, WSMsgType

PORT = 8002


class Hub:
    def __init__(self):
        self.viewers: weakref.WeakSet = weakref.WeakSet()  # /stream の WS
        self.ingest: web.WebSocketResponse | None = None    # controller の WS
        self.last_frame: bytes | None = None

    async def broadcast(self, data: bytes):
        self.last_frame = data
        dead = []
        for ws in list(self.viewers):
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.viewers.discard(ws)

    async def send_control(self, payload: dict) -> bool:
        if self.ingest is None or self.ingest.closed:
            return False
        await self.ingest.send_json(payload)
        return True


hub = Hub()


async def health(_request):
    return web.json_response({
        "status": "ok",
        "controller_connected": hub.ingest is not None and not hub.ingest.closed,
        "viewers": len(hub.viewers),
        "have_frame": hub.last_frame is not None,
    })


async def ws_ingest(request):
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    hub.ingest = ws
    print("[bridge] controller connected (/ingest)")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await hub.broadcast(msg.data)
            elif msg.type == WSMsgType.TEXT:
                pass  # status JSON 等（将来用）
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        if hub.ingest is ws:
            hub.ingest = None
        print("[bridge] controller disconnected")
    return ws


async def ws_stream(request):
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    hub.viewers.add(ws)
    print(f"[bridge] viewer connected (/stream) total={len(hub.viewers)}")
    # 接続直後に最後のフレームを送って黒画面を避ける
    if hub.last_frame is not None:
        try:
            await ws.send_bytes(hub.last_frame)
        except Exception:
            pass
    try:
        async for _msg in ws:
            pass  # 視聴者からの受信は今は無視
    finally:
        hub.viewers.discard(ws)
    return ws


async def control(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    ok = await hub.send_control(payload)
    if not ok:
        return web.json_response(
            {"error": "controller not connected"}, status=503)
    return web.json_response({"status": "sent", "cmd": payload})


PREVIEW_HTML = """<!doctype html><meta charset=utf-8>
<title>earth-bridge preview</title>
<body style="margin:0;background:#111">
<img id=v style="width:100vw;height:100vh;object-fit:contain">
<script>
const img = document.getElementById('v');
const ws = new WebSocket((location.protocol==='https:'?'wss':'ws')+
  '://'+location.host+'/stream');
ws.binaryType='blob';
let url=null;
ws.onmessage = (e)=>{ if(url) URL.revokeObjectURL(url);
  url=URL.createObjectURL(e.data); img.src=url; };
</script></body>"""


async def preview(_request):
    return web.Response(text=PREVIEW_HTML, content_type="text/html")


def make_app():
    app = web.Application()
    app.add_routes([
        web.get("/health", health),
        web.get("/ingest", ws_ingest),
        web.get("/stream", ws_stream),
        web.post("/control", control),
        web.get("/preview", preview),
    ])
    return app


if __name__ == "__main__":
    print(f"earth-bridge: http://localhost:{PORT}  "
          f"(preview: http://localhost:{PORT}/preview)")
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
