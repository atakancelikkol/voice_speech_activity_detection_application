"""REST + WebSocket + static frontend."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import shutil
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.rtp import g711
from server.vad.runner import SOURCE_RATE

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


class EngineUpdate(BaseModel):
    enabled: bool | None = None
    params: dict | None = None


class Annotations(BaseModel):
    speech_regions: list[dict]


class SoftphoneStart(BaseModel):
    mode: str = "mic"
    wav_path: str | None = None


class Reanalyze(BaseModel):
    engines: list[str] | None = None  # None = all currently enabled engines


def build_app(state) -> FastAPI:
    """state: object with .engine_manager, .store, .hub, .call_manager"""
    app = FastAPI(title="VAD Comparison Server")

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # frontend files change with the code; force ETag revalidation so
        # browsers never run a stale app.js against a new API
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-cache"
        return response

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

    @app.get("/api/enhancers")
    def get_enhancers():
        return state.enhancer_manager.snapshot()

    @app.put("/api/enhancers/{name}")
    def put_enhancer(name: str, update: EngineUpdate):
        try:
            state.enhancer_manager.configure(name, update.enabled, update.params)
        except KeyError:
            raise HTTPException(404, f"no such enhancer: {name}")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return state.enhancer_manager.snapshot()

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

    @app.get("/api/sessions/{session_id}/enhanced.wav")
    def get_enhanced_audio(session_id: str):
        # The raw recording run through the currently active enhancer: this is
        # the audio UniMRCP would stream to the recognizer (STT), so the UI can
        # PLAY it to hear the enhancer's effect. It is decoupled from the VAD
        # engines (they always score the raw audio), so it never changes any
        # engine's segments. Falls back to the raw audio when no enhancer is
        # active.
        from fastapi import Response

        from server.audio.wav_io import load_wav, wav_bytes
        from server.enhance.base import enhance_pcm

        try:
            path = state.store.audio_path(session_id)
        except (KeyError, ValueError):
            raise HTTPException(404, f"no such session: {session_id}")
        if not path.exists():
            raise HTTPException(404, "no audio recorded")
        pcm = load_wav(path, 8000)
        name = state.enhancer_manager.active_name()
        if name is not None:
            enhancer = state.enhancer_manager.instantiate_active(8000)
            try:
                pcm = enhance_pcm(enhancer, pcm)
            finally:
                enhancer.close()
        return Response(wav_bytes(pcm, 8000), media_type="audio/wav")

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

    @app.post("/api/sessions/{session_id}/reanalyze")
    async def reanalyze_session(session_id: str, payload: Reanalyze):
        from server.analysis import reanalyze_session as run_reanalyze

        try:
            # offline, CPU-bound; keep the event loop responsive
            session = await asyncio.to_thread(
                run_reanalyze,
                state.store,
                state.engine_manager,
                session_id,
                payload.engines,
            )
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, f"no such session: {session_id}")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        session["annotations"] = state.store.read_annotations(session_id)
        return session

    @app.get("/api/softphone")
    async def softphone_status():
        status = await state.softphone.status()
        return {"running": status is not None, "status": status}

    @app.post("/api/softphone/start")
    async def softphone_start(start: SoftphoneStart):
        code, body = await state.softphone.start(start.mode, start.wav_path)
        if code != 200:
            raise HTTPException(code, body.get("detail", "softphone error"))
        return body

    @app.post("/api/softphone/stop")
    async def softphone_stop():
        code, body = await state.softphone.stop()
        if code != 200:
            raise HTTPException(code, body.get("detail", "softphone error"))
        return body

    @app.post("/api/softphone/upload")
    async def softphone_upload(file: UploadFile = File(...)):
        # a WAV picked from the browser's file dialog: save it where the
        # (same-machine) softphone client can read it, then place the call
        name = Path(file.filename or "upload.wav").name
        if not name.lower().endswith(".wav"):
            raise HTTPException(422, "only .wav files are supported")
        upload_dir = state.config.data_dir.parent / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / f"{secrets.token_hex(4)}_{name}"

        def _save():
            with dest.open("wb") as out:
                shutil.copyfileobj(file.file, out)

        await asyncio.to_thread(_save)
        code, body = await state.softphone.start("wav", str(dest))
        if code != 200:
            raise HTTPException(code, body.get("detail", "softphone error"))
        return body

    @app.post("/api/record/upload")
    async def record_upload(
        file: UploadFile = File(...),
        rate: int = Query(SOURCE_RATE),
        imprint: str | None = Query(None),
    ):
        """Headless analysis: run every enabled engine over an uploaded recording
        and persist it as a session — no softphone/SIP, so it works in a
        container. Reuses the recording pipeline, so the result is identical in
        shape to a live mic recording.

        Accepts a .wav container, or headless signed 16-bit little-endian mono
        PCM (.raw/.pcm/.sw/.s16/.lpcm/.l16) — the exact LPCM/8000/1 UniMRCP
        decodes G.711 into, so you can feed the bytes the production VAD sees.
        `rate` is the sample rate of raw PCM (default 8 kHz); it is resampled to
        the pipeline's 8 kHz. WAV files carry their own rate and ignore `rate`.
        `imprint` ('alaw'/'ulaw') round-trips clean audio through that G.711 codec
        so it carries the wire companding the production VAD sees (Turkey=alaw)."""
        name = Path(file.filename or "upload.wav").name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        raw_exts = {"raw", "pcm", "sw", "s16", "lpcm", "l16"}
        if ext != "wav" and ext not in raw_exts:
            raise HTTPException(422, "supported: .wav, or raw s16le mono PCM (.raw/.pcm/.sw/.s16/.lpcm/.l16)")
        data = await file.read()

        def _load():
            from server.audio.wav_io import load_raw_pcm, load_wav

            if ext != "wav":
                return load_raw_pcm(data, rate, SOURCE_RATE)
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                tmp.write(data)
                tmp.flush()
                return load_wav(tmp.name, SOURCE_RATE)

        try:
            pcm = await asyncio.to_thread(_load)
        except Exception as exc:
            raise HTTPException(422, f"could not read audio: {exc}")
        if imprint:
            try:
                pcm = g711.imprint(pcm, imprint)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
        # pipeline callbacks touch the asyncio hub, so drive it on the loop
        pipeline = state.call_manager.create_recording_pipeline(f"upload-{secrets.token_hex(4)}")
        chunk = SOURCE_RATE * 20 // 1000
        for i in range(0, len(pcm), chunk):
            pipeline.on_audio(pcm[i : i + chunk])
        session_id = state.call_manager.finalize_recording_pipeline(pipeline)
        return {"session_id": session_id}

    @app.websocket("/api/record")
    async def record_ws(ws: WebSocket):
        """Browser-microphone ingest: the page streams 8 kHz mono int16 PCM
        frames and we feed them into a recording pipeline exactly like RTP audio
        (no SIP, no softphone client — works headless in a container). The live
        timeline updates over the main /ws hub via call_state 'active'; on close
        we finalize and persist the session."""
        await ws.accept()
        # ?imprint=alaw|ulaw round-trips each mic frame through that G.711 codec
        # so a clean browser mic carries the wire companding the production VAD
        # sees; unknown values are ignored (feed raw). G.711 is memoryless per
        # sample, so per-frame imprint equals whole-stream.
        law = ws.query_params.get("imprint") or None
        if law is not None:
            try:
                g711.imprint(np.zeros(1, dtype=np.int16), law)
            except ValueError:
                law = None
        pipeline = state.call_manager.create_recording_pipeline(f"browser-{secrets.token_hex(4)}")
        with contextlib.suppress(Exception):
            await ws.send_json({"kind": "recording_started", "session_id": pipeline.session_id})
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data:
                    pcm = np.frombuffer(data, dtype=np.int16)
                    if len(pcm):
                        if law is not None:
                            pcm = g711.imprint(pcm, law)
                        pipeline.on_audio(pcm)
                elif message.get("text") == "stop":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            session_id = state.call_manager.finalize_recording_pipeline(pipeline)
            with contextlib.suppress(Exception):
                await ws.send_json({"kind": "recording_finished", "session_id": session_id})
            with contextlib.suppress(Exception):
                await ws.close()

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

    @app.get("/favicon.ico")
    def favicon():
        # no icon shipped; answer cleanly so the browser stops logging a 404
        return Response(status_code=204)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
