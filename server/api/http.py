"""REST + WebSocket + static frontend."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


class EngineUpdate(BaseModel):
    enabled: bool | None = None
    params: dict | None = None


class Annotations(BaseModel):
    speech_regions: list[dict]


def build_app(state) -> FastAPI:
    """state: object with .engine_manager, .store, .hub, .call_manager"""
    app = FastAPI(title="VAD Comparison Server")

    @app.get("/api/engines")
    def get_engines():
        return state.engine_manager.snapshot()

    @app.put("/api/engines/{name}")
    def put_engine(name: str, update: EngineUpdate):
        try:
            state.engine_manager.configure(name, update.enabled, update.params)
        except KeyError:
            raise HTTPException(404, f"no such engine: {name}")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return state.engine_manager.snapshot()

    @app.get("/api/sessions")
    def get_sessions():
        return state.store.list_sessions()

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        try:
            session = state.store.read_session(session_id)
        except (KeyError, ValueError, FileNotFoundError):
            raise HTTPException(404, f"no such session: {session_id}")
        session["annotations"] = state.store.read_annotations(session_id)
        return session

    @app.get("/api/sessions/{session_id}/audio.wav")
    def get_audio(session_id: str):
        try:
            path = state.store.audio_path(session_id)
        except (KeyError, ValueError):
            raise HTTPException(404, f"no such session: {session_id}")
        if not path.exists():
            raise HTTPException(404, "no audio recorded")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/sessions/{session_id}/annotations")
    def get_annotations(session_id: str):
        try:
            return state.store.read_annotations(session_id)
        except (KeyError, ValueError):
            raise HTTPException(404, f"no such session: {session_id}")

    @app.put("/api/sessions/{session_id}/annotations")
    def put_annotations(session_id: str, payload: Annotations):
        try:
            state.store.write_annotations(session_id, payload.model_dump())
        except (KeyError, ValueError):
            raise HTTPException(404, f"no such session: {session_id}")
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        queue = await state.hub.attach(ws)
        try:
            while True:
                message = await queue.get()
                await ws.send_json(message)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            state.hub.detach(ws)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
