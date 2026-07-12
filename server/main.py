"""VAD comparison server: SIP UAS + RTP + engines + web frontend, one loop.

    uv run vad-server [--host 127.0.0.1] [--sip-port 5060] [--http-port 8080]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from pathlib import Path

import uvicorn

from server.api.http import build_app
from server.api.ws import Hub
from server.calls import CallManager
from server.client_supervisor import ClientSupervisor
from server.config import ServerConfig
from server.engines_state import EngineManager
from server.enhance.manager import EnhancerManager
from server.sessions.store import SessionStore
from server.sip.uas import start_uas
from server.softphone import SoftphoneProxy

log = logging.getLogger("server")


class ServerState:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.engine_manager = EngineManager()
        self.enhancer_manager = EnhancerManager()
        self.store = SessionStore(config.data_dir)
        self.hub = Hub()
        self.call_manager = CallManager(config, self.store, self.engine_manager, self.hub)
        self.softphone = SoftphoneProxy(config.client_url)


def parse_args(argv=None) -> ServerConfig:
    parser = argparse.ArgumentParser(description="VAD comparison server")
    defaults = ServerConfig()
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--sip-port", type=int, default=defaults.sip_port)
    parser.add_argument("--http-port", type=int, default=defaults.http_port)
    parser.add_argument("--rtp-port-min", type=int, default=defaults.rtp_port_min)
    parser.add_argument("--rtp-port-max", type=int, default=defaults.rtp_port_max)
    parser.add_argument("--data-dir", type=Path, default=defaults.data_dir)
    parser.add_argument("--client-url", default=defaults.client_url)
    parser.add_argument(
        "--no-client",
        action="store_true",
        help="do not start the softphone client automatically (start it yourself with `make run-client`)",
    )
    args = parser.parse_args(argv)
    return ServerConfig(
        host=args.host,
        sip_port=args.sip_port,
        http_port=args.http_port,
        rtp_port_min=args.rtp_port_min,
        rtp_port_max=args.rtp_port_max,
        data_dir=args.data_dir,
        client_url=args.client_url,
        spawn_client=not args.no_client,
    )


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = parse_args(argv)
    state = ServerState(config)

    for entry in state.engine_manager.snapshot():
        status = "enabled" if entry["enabled"] else f"UNAVAILABLE ({entry['reason']})"
        log.info("engine %-14s %s", entry["name"], status)

    app = build_app(state)
    main_url = f"http://{config.host}:{config.http_port}"
    supervisor = (
        ClientSupervisor(config.client_url, config.sip_port, config.host, main_url)
        if config.spawn_client
        else None
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        sip_transport = await start_uas(config, state.call_manager)
        # spawn the client in the background so it doesn't delay the HTTP
        # listener coming up (the poller in the UI waits for it either way)
        spawn_task = (
            asyncio.create_task(asyncio.to_thread(supervisor.start)) if supervisor is not None else None
        )
        try:
            yield
        finally:
            if spawn_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await spawn_task
            state.call_manager.end_all()
            sip_transport.close()
            if supervisor is not None:
                supervisor.stop()

    app.router.lifespan_context = lifespan
    log.info("open the app at http://%s:%d", config.host, config.http_port)
    uvicorn.run(app, host=config.host, port=config.http_port, log_level="warning")


if __name__ == "__main__":
    main()
