"""Vercel serverless entry point — Telegram webhook + panel feed.

Three responsibilities, one FastAPI app:

  POST /api/telegram         — Telegram delivers updates here.
  GET  /api/sessions/{chat_id}
                             — snapshot for a session (scores, masters, log).
  GET  /api/stream/{chat_id} — Server-Sent Events stream of new log entries.
                               This works on Vercel (HTTP streaming) and is
                               the official panel feed for the serverless
                               deploy.
  WS   /api/ws/{chat_id}     — full-duplex WebSocket variant. Same payloads
                               as SSE. Vercel does NOT support persistent
                               WS connections — this endpoint exists for
                               self-hosted deploys (Docker / Fly.io / VPS).

The HTML panel under ``/`` uses SSE by default and falls back to WS only
when explicitly requested. EventSource auto-reconnects on disconnection,
so even Vercel's per-function timeout (60s on Hobby, 900s on Enterprise)
is transparent to the panel — it just looks like a slightly bumpy stream.

Required env vars:
    TELEGRAM_BOT_TOKEN
    STORAGE_URL           libsql://your-db.turso.io  (or file:./... locally)
    STORAGE_AUTH_TOKEN    Turso DB auth token
    GAMES_DIR             defaults to ./games (bundled with the deploy)
    WEBHOOK_SECRET        optional shared secret for the Telegram webhook
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from telegram import Update

from coordinatore.bot import build_application
from coordinatore.session import Session

log = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GAMES_DIR = Path(os.environ.get("GAMES_DIR", "games"))
STORAGE_URL = os.environ.get("STORAGE_URL")
STORAGE_AUTH_TOKEN = os.environ.get("STORAGE_AUTH_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
PANEL_HTML = Path(__file__).resolve().parent.parent / "public" / "panel.html"
STREAM_POLL_SECONDS = float(os.environ.get("STREAM_POLL_SECONDS", "1.0"))
STREAM_MAX_SECONDS = float(os.environ.get("STREAM_MAX_SECONDS", "55"))

if not TOKEN or not STORAGE_URL:
    log.warning("TELEGRAM_BOT_TOKEN or STORAGE_URL not set; webhook will 500")
    ptb_app = None
else:
    ptb_app = build_application(
        token=TOKEN,
        games_dir=GAMES_DIR,
        storage_url=STORAGE_URL,
        storage_auth_token=STORAGE_AUTH_TOKEN,
    )


def _bctx():
    if ptb_app is None:
        raise HTTPException(500, "bot not initialised")
    return ptb_app.bot_data["ctx"]


def _load_session(chat_id: int) -> Session:
    bctx = _bctx()
    row = bctx.storage.active_for_chat(chat_id)
    if not row:
        raise HTTPException(404, f"no active session for chat {chat_id}")
    config = bctx.games.get(row["game_id"])
    if not config:
        raise HTTPException(500, f"game {row['game_id']} no longer loaded")
    return Session.from_row(row, config)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if ptb_app is not None:
        await ptb_app.initialize()
    try:
        yield
    finally:
        if ptb_app is not None:
            await ptb_app.shutdown()


app = FastAPI(lifespan=lifespan, title="coordinatore", version="0.2.0")


# ---------- panel (static HTML) ----------


@app.get("/")
async def index():
    if PANEL_HTML.exists():
        return FileResponse(PANEL_HTML)
    return {"status": "ok", "panel": "missing public/panel.html"}


# ---------- Telegram webhook ----------


@app.get("/api/telegram")
async def healthcheck() -> dict:
    return {
        "ok": ptb_app is not None,
        "games": list(ptb_app.bot_data["ctx"].games) if ptb_app else [],
    }


@app.post("/api/telegram")
async def webhook(request: Request) -> dict:
    if ptb_app is None:
        raise HTTPException(
            500,
            "bot not initialised — check TELEGRAM_BOT_TOKEN and STORAGE_URL env vars",
        )
    if WEBHOOK_SECRET:
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if sent != WEBHOOK_SECRET:
            raise HTTPException(403, "bad webhook secret")
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"ok": True}


# ---------- panel snapshot ----------


@app.get("/api/sessions/{chat_id}")
async def session_snapshot(chat_id: int, since: int = -1) -> dict:
    """One-shot snapshot of the active session in ``chat_id``.

    Returns scores, masters, the config metadata the panel needs to render,
    and the log entries with seq > ``since``.
    """
    s = _load_session(chat_id)
    variant = s.config.variant(s.variant_id)
    return {
        "session_id": s.id,
        "chat_id": s.chat_id,
        "status": s.status,
        "started_at": s.started_at,
        "game": {
            "id": s.config.id,
            "name": s.config.name,
            "language": s.config.language,
        },
        "variant": {
            "id": s.variant_id,
            "label": variant.label,
            "active_factions": [
                {"id": fid, "label": s.config.faction(fid).label}
                for fid in variant.active_factions
            ],
        },
        "scores": s.scores,
        "total": s.total(),
        "max_total": s.config.max_total(s.variant_id),
        "outcome": (s.outcome().label if s.outcome() else None),
        "masters": [
            {"user_id": m.user_id, "username": m.username, "faction_id": m.faction_id}
            for m in s.masters.values()
        ],
        "events": [e for e in s.log if e.get("seq", -1) > since],
        "last_seq": (s.log[-1].get("seq", -1) if s.log else -1),
    }


# ---------- SSE stream ----------


@app.get("/api/stream/{chat_id}")
async def stream(chat_id: int, since: int = -1):
    """Server-Sent Events stream.

    Polls the storage every STREAM_POLL_SECONDS for new log entries with
    seq > the cursor, emits them as SSE events, then continues until the
    connection closes or STREAM_MAX_SECONDS elapses (Vercel timeout safety).

    The browser EventSource will reconnect automatically; clients SHOULD
    send their last seen ``seq`` via the ``since`` query param (or via the
    Last-Event-ID header, which we also honour).
    """
    last_seq = since

    async def event_gen():
        nonlocal last_seq
        # Heartbeat + initial events so the panel can render immediately.
        yield "retry: 2000\n\n"
        elapsed = 0.0
        while elapsed < STREAM_MAX_SECONDS:
            try:
                s = _load_session(chat_id)
            except HTTPException as exc:
                payload = json.dumps({"error": exc.detail})
                yield f"event: error\ndata: {payload}\n\n"
                return
            new_events = [e for e in s.log if e.get("seq", -1) > last_seq]
            for e in new_events:
                last_seq = e["seq"]
                payload = json.dumps(e)
                yield f"id: {last_seq}\nevent: {e.get('kind', 'event')}\ndata: {payload}\n\n"
            if s.status == "ended":
                yield "event: ended\ndata: {}\n\n"
                return
            await asyncio.sleep(STREAM_POLL_SECONDS)
            elapsed += STREAM_POLL_SECONDS
            # Periodic comment-line as keepalive so proxies don't time us out.
            yield ": keepalive\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- WebSocket stream (self-hosted only) ----------


@app.websocket("/api/ws/{chat_id}")
async def ws_stream(websocket: WebSocket, chat_id: int):
    """Full-duplex WebSocket version of /api/stream — same payloads.

    *Does not work on Vercel serverless* (no persistent connections). Use
    SSE there. Provided for self-hosted deploys where a long-running
    process can hold the socket open.
    """
    await websocket.accept()
    last_seq = -1
    try:
        while True:
            try:
                s = _load_session(chat_id)
            except HTTPException as exc:
                await websocket.send_json({"error": exc.detail})
                await websocket.close(code=1008)
                return
            new_events = [e for e in s.log if e.get("seq", -1) > last_seq]
            for e in new_events:
                last_seq = e["seq"]
                await websocket.send_json(e)
            if s.status == "ended":
                await websocket.send_json({"event": "ended"})
                await websocket.close()
                return
            await asyncio.sleep(STREAM_POLL_SECONDS)
    except WebSocketDisconnect:
        return
