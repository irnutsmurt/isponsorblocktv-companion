"""Optional YouTube screen-availability poll (status call, not a lounge session)."""

from __future__ import annotations

import asyncio
import random

import aiohttp

from ..db import Database
from ..events_bus import EventBus
from ..logging import get_logger
from ..util import now_iso
from .devices import DeviceRegistry
from .youtube import YouTubeClient

log = get_logger("availability")


class AvailabilityPoller:
    def __init__(self, db: Database, bus: EventBus, http: aiohttp.ClientSession, devices: DeviceRegistry, config_provider, interval_minutes: int) -> None:
        self.db = db
        self.bus = bus
        self.yt = YouTubeClient(http)
        self.devices = devices
        self.config_provider = config_provider
        self.interval = interval_minutes * 60
        self._task: asyncio.Task | None = None
        self.last_run: str | None = None

    async def start(self) -> None:
        if self.interval > 0:
            self._task = asyncio.create_task(self._loop(), name="availability")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.poll_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("availability.poll.failed", "Availability poll failed", error=str(exc)[:200])
            await asyncio.sleep(self.interval * (0.9 + random.random() * 0.2))

    async def poll_all(self) -> list[dict]:
        config = await self.config_provider() if self.config_provider else None
        results = []
        for dev in (config or {}).get("devices", []) if isinstance((config or {}).get("devices"), list) else []:
            if not isinstance(dev, dict) or not isinstance(dev.get("screen_id"), str) or not dev["screen_id"]:
                continue
            results.append(await self.poll_screen(dev["screen_id"]))
            await asyncio.sleep(1.0)
        self.last_run = now_iso()
        return results

    async def poll_screen(self, screen_id: str) -> dict:
        from ..util import screen_key
        key = screen_key(screen_id)
        device = self.devices.by_key(key)
        try:
            status = await asyncio.wait_for(self.yt.screen_availability(screen_id), timeout=30)
            error = None
        except Exception as exc:
            status = "error"
            error = f"{exc.__class__.__name__}: {str(exc)[:120]}"
        if device:
            await self.db.execute("UPDATE device_state SET yt_availability = ?, yt_availability_at = ?, yt_availability_error = ? WHERE device_id = ?",
                                  (status, now_iso(), error, device["id"]))
            self.bus.publish("availability", {"device_id": device["id"], "status": status, "at": now_iso()})
            log.debug("availability.result", "Screen availability polled", device_id=device["id"], status=status)
        return {"device_id": device["id"] if device else None, "status": status, "error": error}
