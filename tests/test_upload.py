"""The main UI's 'WAV file…' button uploads a file (via the OS file dialog)
to /api/softphone/upload, which saves it where the client can read it and
places the call. These check the save + reject-non-wav + start-call path."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server.api.http import build_app

FIXTURE = Path(__file__).parent / "fixtures" / "speech.wav"


class FakeSoftphone:
    def __init__(self):
        self.started = None

    async def start(self, mode, wav_path):
        self.started = (mode, wav_path)
        return 200, {"state": "active", "error": None, "level": 0.0}

    async def status(self):
        return None

    async def stop(self):
        return 200, {"state": "idle"}


@pytest.fixture
def client(tmp_path):
    softphone = FakeSoftphone()
    state = SimpleNamespace(
        config=SimpleNamespace(data_dir=tmp_path / "sessions"),
        softphone=softphone,
    )
    return TestClient(build_app(state)), softphone, tmp_path


def test_upload_saves_wav_and_places_call(client):
    tc, softphone, tmp_path = client
    payload = FIXTURE.read_bytes()
    resp = tc.post("/api/softphone/upload", files={"file": ("my recording.wav", payload, "audio/wav")})
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"

    mode, path = softphone.started
    assert mode == "wav"
    saved = Path(path)
    assert saved.exists() and saved.read_bytes() == payload
    assert saved.parent == tmp_path / "uploads"
    assert saved.name.endswith("my recording.wav")  # original name preserved, prefixed


def test_upload_rejects_non_wav(client):
    tc, softphone, _ = client
    resp = tc.post("/api/softphone/upload", files={"file": ("song.mp3", b"not a wav", "audio/mpeg")})
    assert resp.status_code == 422
    assert "wav" in resp.json()["detail"].lower()
    assert softphone.started is None  # no call placed


def test_upload_strips_path_from_filename(client):
    tc, softphone, tmp_path = client
    payload = FIXTURE.read_bytes()
    # a malicious filename must not escape the uploads dir
    tc.post("/api/softphone/upload", files={"file": ("../../etc/evil.wav", payload, "audio/wav")})
    saved = Path(softphone.started[1])
    assert saved.parent == tmp_path / "uploads"
    assert "evil.wav" in saved.name and ".." not in saved.name
