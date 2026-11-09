"""YouTube calls the companion makes itself: pairing, screen status, and key-free metadata.

Pairing mirrors upstream ``api_helpers.ApiHelper.pair_with_code`` exactly; nothing here opens a
lounge session (``bc/bind``), so no competing remote-control session is ever created.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from ..logging import get_logger
from ..logging.redaction import register_secret

log = get_logger("upstream.youtube")

import os

# Overridable ONLY for automated tests (points pairing at a local fake). Never set in production.
LOUNGE_API = os.getenv("YOUTUBE_LOUNGE_API_BASE", "https://www.youtube.com/api/lounge").rstrip("/")
OEMBED = "https://www.youtube.com/oembed"
DATA_API = "https://www.googleapis.com/youtube/v3/"
USER_AGENT = "iSponsorBlockTV-Companion/0.1"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class PairingError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PairedScreen:
    screen_id: str
    name: str | None


def normalize_pairing_code(code: str) -> str:
    return re.sub(r"[\s\-]", "", code or "")


def validate_pairing_code(code: str) -> str | None:
    c = normalize_pairing_code(code)
    if not c.isdigit():
        return "The TV code contains only digits."
    if len(c) != 12:
        return "The TV code is 12 digits long (shown as three groups of four)."
    return None


class YouTubeClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._oembed_last = 0.0
        self._oembed_min_interval = 1.0  # polite: at most one oEmbed request per second

    async def pair_with_code(self, code: str) -> PairedScreen:
        err = validate_pairing_code(code)
        if err:
            raise PairingError(err, "invalid_format")
        normalized = normalize_pairing_code(code)
        try:
            async with self.session.post(f"{LOUNGE_API}/pairing/get_screen", data={"pairing_code": normalized},
                                         headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (400, 404):
                    raise PairingError("YouTube did not recognise this code. Codes expire quickly: open "
                                       "Settings → Link with TV code on the TV again and enter the new code.", "rejected")
                if resp.status != 200:
                    raise PairingError(f"YouTube returned an unexpected response ({resp.status}). Try again in a moment.", "upstream_error")
                try:
                    payload = await resp.json(content_type=None)
                except Exception as exc:
                    raise PairingError("YouTube returned an unreadable response.", "upstream_error") from exc
        except asyncio.TimeoutError as exc:
            raise PairingError("YouTube did not answer within 15 seconds. Check the companion's internet access and try again.", "timeout") from exc
        except aiohttp.ClientError as exc:
            raise PairingError(f"Could not reach YouTube: {exc.__class__.__name__}.", "network") from exc
        screen = (payload or {}).get("screen") or {}
        screen_id = screen.get("screenId")
        if not screen_id:
            raise PairingError("YouTube accepted the code but returned no screen. Restart the YouTube app on the TV and try a fresh code.", "rejected")
        register_secret(screen_id, "SCREEN_ID")
        token = screen.get("loungeToken")
        if token:
            register_secret(token, "LOUNGE_TOKEN")
        log.info("pairing.success", "Paired with a TV via code", screen_name=screen.get("name"))
        return PairedScreen(screen_id=screen_id, name=screen.get("name"))

    async def lounge_token(self, screen_id: str) -> str | None:
        """Resolve a screen id to a lounge token (same call upstream makes every 24h)."""
        async with self.session.post(f"{LOUNGE_API}/pairing/get_lounge_token_batch", data={"screen_ids": screen_id},
                                     headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json(content_type=None)
        screens = (payload or {}).get("screens") or []
        if not screens:
            return None
        token = screens[0].get("loungeToken")
        if token:
            register_secret(token, "LOUNGE_TOKEN")
        return token

    async def screen_availability(self, screen_id: str) -> str:
        """Returns 'online', 'offline', 'unknown_screen' or raises on network errors."""
        token = await self.lounge_token(screen_id)
        if not token:
            return "unknown_screen"
        async with self.session.post(f"{LOUNGE_API}/pairing/get_screen_availability", data={"lounge_token": token},
                                     headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return "error"
            payload = await resp.json(content_type=None)
        screens = (payload or {}).get("screens") or []
        if not screens:
            return "unknown_screen"
        return "online" if screens[0].get("status") == "online" else "offline"

    # ---- metadata (optional enrichment) -------------------------------------
    async def oembed(self, video_id: str) -> dict | None:
        """Title, channel name and channel URL without an API key. Returns None when not found."""
        if not VIDEO_ID_RE.match(video_id):
            return None
        wait = self._oembed_min_interval - (time.monotonic() - self._oembed_last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._oembed_last = time.monotonic()
        url = f"{OEMBED}?url={quote('https://www.youtube.com/watch?v=' + video_id, safe='')}&format=json"
        async with self.session.get(url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status in (400, 401, 403, 404):
                return None
            if resp.status == 429:
                raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=429, message="rate limited")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return {
            "title": data.get("title"),
            "channel_name": data.get("author_name"),
            "channel_url": data.get("author_url"),
        }

    async def data_api_video(self, video_id: str, api_key: str) -> dict | None:
        params = {"id": video_id, "key": api_key, "part": "snippet,contentDetails"}
        async with self.session.get(DATA_API + "videos", params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 403:
                raise PermissionError("YouTube Data API key rejected or quota exceeded")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        items = data.get("items") or []
        if not items:
            return None
        sn = items[0].get("snippet", {})
        return {
            "title": sn.get("title"),
            "channel_id": sn.get("channelId"),
            "channel_name": sn.get("channelTitle"),
            "channel_url": f"https://www.youtube.com/channel/{sn.get('channelId')}" if sn.get("channelId") else None,
            "duration_iso": items[0].get("contentDetails", {}).get("duration"),
        }

    async def search_channels(self, query: str, api_key: str) -> list[dict]:
        params = {"q": query, "key": api_key, "part": "snippet", "type": "channel", "maxResults": "5"}
        async with self.session.get(DATA_API + "search", params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 403:
                raise PermissionError("YouTube Data API key rejected or quota exceeded")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return [{"id": i["snippet"]["channelId"], "name": i["snippet"]["channelTitle"]} for i in data.get("items", [])]
