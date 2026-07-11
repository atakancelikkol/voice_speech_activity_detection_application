"""Server configuration (defaults overridable via CLI flags in main.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    sip_port: int = 5060
    http_port: int = 8080
    rtp_port_min: int = 40000
    rtp_port_max: int = 40019
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "sessions")
    # local softphone client HTTP API, used by the one-button record proxy
    client_url: str = "http://127.0.0.1:8081"
    # start the softphone client as a child process so the user only needs
    # the main UI (:8080); its own page (:8081) stays an implementation detail
    spawn_client: bool = True
