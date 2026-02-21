"""
FastAPI server — serves the dashboard and pushes live events over WebSocket.

Architecture:
  - Bot threads call event_bus.publish() from background threads
  - EventBus uses loop.call_soon_threadsafe to enqueue into async subscriber queues
  - Each connected WebSocket gets its own asyncio.Queue
  - On connect: replay event history, then stream new events
"""

import asyncio
import os
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from bot.events import EventBus

_STATIC = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Polymarket Arb Dashboard")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")

_bus: EventBus | None = None
_mirror_bot = None          # MirrorBot instance, set from main_dashboard


def set_event_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus


def set_mirror_bot(bot) -> None:
    global _mirror_bot
    _mirror_bot = bot


@app.on_event("startup")
async def _startup() -> None:
    """Hand the running asyncio loop to the EventBus so it can bridge threads."""
    if _bus:
        _bus.set_loop(asyncio.get_running_loop())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC, "index.html"))


# ── Mirror REST API ───────────────────────────────────────────────────────────

class AddressPayload(BaseModel):
    address: str
    nickname: str
    poll_interval: Optional[float] = None


class AddressUpdate(BaseModel):
    nickname: Optional[str] = None
    enabled: Optional[bool] = None


@app.get("/api/mirror/snapshot")
async def mirror_snapshot():
    if _mirror_bot is None:
        raise HTTPException(503, "Mirror bot not initialized")
    return JSONResponse(_mirror_bot.snapshot())


@app.get("/api/mirror/addresses")
async def list_addresses():
    if _mirror_bot is None:
        raise HTTPException(503, "Mirror bot not initialized")
    return JSONResponse({"addresses": _mirror_bot.get_addresses()})


@app.post("/api/mirror/addresses", status_code=201)
async def add_address(body: AddressPayload):
    if _mirror_bot is None:
        raise HTTPException(503, "Mirror bot not initialized")
    result = _mirror_bot.add_address(body.address, body.nickname, body.poll_interval)
    return JSONResponse(result)


@app.delete("/api/mirror/addresses/{address}")
async def remove_address(address: str):
    if _mirror_bot is None:
        raise HTTPException(503, "Mirror bot not initialized")
    ok = _mirror_bot.remove_address(address)
    if not ok:
        raise HTTPException(404, "Address not found")
    return JSONResponse({"ok": True})


@app.patch("/api/mirror/addresses/{address}")
async def update_address(address: str, body: AddressUpdate):
    if _mirror_bot is None:
        raise HTTPException(503, "Mirror bot not initialized")
    ok = _mirror_bot.update_address(address, body.nickname, body.enabled)
    if not ok:
        raise HTTPException(404, "Address not found")
    return JSONResponse({"ok": True})


@app.post("/api/mirror/reset")
async def mirror_reset():
    if _mirror_bot is None:
        raise HTTPException(503, "Mirror bot not initialized")
    _mirror_bot.reset()
    return JSONResponse({"ok": True, "ts": time.time()})


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    if _bus is None:
        await ws.close(code=1011, reason="Event bus not initialized")
        return

    queue = _bus.subscribe()
    try:
        # Replay history so reconnecting clients see all past events
        for event in _bus.get_history():
            await ws.send_json(event)

        # Stream live events
        while True:
            event = await queue.get()
            await ws.send_json(event)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _bus.unsubscribe(queue)
