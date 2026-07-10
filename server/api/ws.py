"""Live WebSocket hub: fan-out of call/audio/score/segment messages.

Message kinds pushed to clients:
  call_state  {kind, state: idle|active|finished, session_id}
  audio_peaks {kind, session_id, t0_ms, dt_ms, peaks: [[min,max],...]}
  scores      {kind, session_id, engine, points: [[t_ms, score],...]}
  segment     {kind, session_id, engine, index, start_ms, end_ms, final}
  event       {kind, session_id, engine, event, at_ms}
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import WebSocket


class Hub:
    def __init__(self) -> None:
        self._queues: dict[WebSocket, asyncio.Queue] = {}
        self.last_call_state: dict[str, Any] | None = None

    async def attach(self, ws: WebSocket) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._queues[ws] = queue
        if self.last_call_state:
            queue.put_nowait(self.last_call_state)
        return queue

    def detach(self, ws: WebSocket) -> None:
        self._queues.pop(ws, None)

    def publish(self, message: dict[str, Any]) -> None:
        if message.get("kind") == "call_state":
            self.last_call_state = message
        for queue in self._queues.values():
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)  # a slow client just misses updates
