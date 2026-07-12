"""Softphone client: real SIP+RTP to the VAD server.

    uv run vad-client --wav tests/fixtures/speech.wav --no-ui   # one-shot file call
    uv run vad-client                                            # mic mode + local web UI
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from pathlib import Path

from pydantic import BaseModel

from client.rtp_sender import RtpSender, open_rtp_socket
from client.sip_uac import SipUac
from client.wav_source import stream_wav
from server.sip.sdp import build_sdp, parse_sdp

log = logging.getLogger("client")


class StartRequest(BaseModel):
    mode: str  # "mic" | "wav"
    wav_path: str | None = None


class CallController:
    def __init__(self, args):
        self.args = args
        self.state = "idle"
        self.error: str | None = None
        self.level = 0.0
        self._uac: SipUac | None = None
        self._rtp_transport = None
        self._capture = None
        self._stream_task: asyncio.Task | None = None

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "level": round(self.level, 4)}

    def _set_level(self, level: float) -> None:
        self.level = level

    async def start_call(self, mode: str, wav_path: str | None = None) -> None:
        if self.state != "idle":
            raise RuntimeError(f"call already {self.state}")
        if mode == "wav":
            if not wav_path or not Path(wav_path).exists():
                raise FileNotFoundError(f"WAV file not found: {wav_path}")
        self.error = None
        self.state = "calling"
        try:
            self._rtp_transport = await open_rtp_socket(self.args.bind_ip, self.args.rtp_port)
            self._uac = SipUac(self.args.server_host, self.args.server_sip_port, self.args.bind_ip)
            await self._uac.start()
            offer = build_sdp(self.args.bind_ip, self.args.rtp_port, session_name="vad-client")
            ok = await self._uac.invite(offer)
            answer = parse_sdp(ok.body.decode())
            sender = RtpSender(self._rtp_transport, (answer.ip, answer.audio_port))
            log.info("call active: sending RTP to %s:%d", answer.ip, answer.audio_port)
            self.state = "active"
            if mode == "wav":
                self._stream_task = asyncio.get_running_loop().create_task(self._run_wav(wav_path, sender))
            else:
                loop = asyncio.get_running_loop()
                self._capture = self._make_capture(sender, loop)
                # opening the input device can block on the macOS microphone
                # permission prompt — keep the loop responsive and fail loud
                try:
                    await asyncio.wait_for(loop.run_in_executor(None, self._capture.start), timeout=8.0)
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        "microphone did not open in 8s — grant microphone access to your "
                        "terminal in System Settings > Privacy & Security > Microphone"
                    )
        except Exception as exc:
            self.error = str(exc)
            log.error("call failed: %s", exc)
            await self._teardown()
            raise

    def _make_capture(self, sender: RtpSender, loop: asyncio.AbstractEventLoop):
        from client.capture import MicCapture  # lazy: needs sounddevice

        return MicCapture(
            on_frame=lambda frame: loop.call_soon_threadsafe(sender.send, frame),
            on_level=lambda level: loop.call_soon_threadsafe(self._set_level, level),
        )

    async def _run_wav(self, wav_path: str, sender: RtpSender) -> None:
        try:
            await stream_wav(wav_path, sender.send, self._set_level)
            log.info("file finished (%d packets sent), hanging up", sender.packets_sent)
        except asyncio.CancelledError:
            raise
        finally:
            self._stream_task = None
            if self.state == "active":
                await self.stop_call()

    async def stop_call(self) -> None:
        if self.state == "idle":
            return
        self.state = "ending"
        if self._stream_task is not None:
            task, self._stream_task = self._stream_task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        if self._uac is not None:
            with contextlib.suppress(Exception):
                await self._uac.bye()
        await self._teardown()
        log.info("call ended")

    async def _teardown(self) -> None:
        # a failed start (e.g. mic open error) lands here without going through
        # stop_call, so release the capture too or it keeps holding the device
        if self._capture is not None:
            with contextlib.suppress(Exception):
                self._capture.stop()
            self._capture = None
        if self._uac is not None:
            self._uac.close()
            self._uac = None
        if self._rtp_transport is not None:
            self._rtp_transport.close()
            self._rtp_transport = None
        self.level = 0.0
        self.state = "idle"


async def one_shot(args) -> None:
    controller = CallController(args)
    done = asyncio.Event()

    async def run():
        await controller.start_call("wav", args.wav)
        while controller.state != "idle":
            await asyncio.sleep(0.1)
        done.set()

    await run()
    await done.wait()


def _placeholder_page(main_url: str) -> str:
    # This process only exists to place SIP calls for the main app; it has no
    # UI of its own. Anyone who lands here is redirected to the real app so
    # the two-port setup never causes confusion.
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>VAD softphone (internal)</title>
<meta http-equiv="refresh" content="3;url={main_url}">
<style>body{{font-family:system-ui,sans-serif;background:#14161a;color:#e8e8e8;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
a{{color:#4dabf7}}.box{{max-width:460px;padding:24px}}</style></head>
<body><div class="box">
<h1>Nothing to see here</h1>
<p>This is the <b>softphone client's internal service</b> — the piece that places
the real SIP call for the app. It has no controls of its own.</p>
<p>Open the app here instead:<br><a href="{main_url}">{main_url}</a></p>
<p style="color:#9aa;font-size:13px">(redirecting automatically…)</p>
</div></body></html>"""


def build_ui_app(controller: CallController, main_url: str = "http://127.0.0.1:8080"):
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="VAD Softphone (internal)")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _placeholder_page(main_url)

    @app.post("/call/start")
    async def call_start(start: StartRequest):
        try:
            await controller.start_call(start.mode, start.wav_path)
        except Exception as exc:
            raise HTTPException(422, str(exc))
        return controller.status()

    @app.post("/call/stop")
    async def call_stop():
        await controller.stop_call()
        return controller.status()

    @app.get("/status")
    def status():
        return controller.status()

    @app.websocket("/ws")
    async def ws(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(controller.status())
                await asyncio.sleep(0.1)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="VAD softphone client")
    parser.add_argument("--server-host", default="127.0.0.1")
    parser.add_argument("--server-sip-port", type=int, default=5060)
    parser.add_argument("--bind-ip", default="127.0.0.1")
    parser.add_argument("--rtp-port", type=int, default=40100)
    parser.add_argument("--ui-port", type=int, default=8081)
    parser.add_argument("--main-url", default="http://127.0.0.1:8080", help="where to redirect stray visitors")
    parser.add_argument("--wav", help="stream this WAV file instead of the microphone")
    parser.add_argument("--no-ui", action="store_true", help="one-shot call (requires --wav), then exit")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = parse_args(argv)
    if args.no_ui:
        if not args.wav:
            raise SystemExit("--no-ui requires --wav")
        asyncio.run(one_shot(args))
        return
    import uvicorn

    controller = CallController(args)
    if args.wav:
        log.info("WAV mode default: %s", args.wav)
    app = build_ui_app(controller, main_url=args.main_url)
    log.info("softphone internal service on http://127.0.0.1:%d (use the app at %s)", args.ui_port, args.main_url)
    uvicorn.run(app, host="127.0.0.1", port=args.ui_port, log_level="warning")


if __name__ == "__main__":
    main()
