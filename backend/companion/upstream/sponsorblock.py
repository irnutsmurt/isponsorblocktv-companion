"""SponsorBlock lookups used to attribute observed skips to segments and categories.

``merge_segments`` reproduces upstream ``api_helpers.ApiHelper.process_segments`` so the merged
segment list matches what iSponsorBlockTV actually skipped; a logged seek target is matched to the
merged segment whose end is the same position.
"""

from __future__ import annotations

import asyncio
import copy
import time
from hashlib import sha256

import aiohttp

from ..logging import get_logger

log = get_logger("upstream.sponsorblock")
USER_AGENT = "iSponsorBlockTV-Companion/0.1"


def merge_segments(raw_segments: list[dict], minimum_skip_length: float) -> list[dict]:
    """Return merged segments [{start, end, uuids:[...], categories:[...]}] like upstream."""
    segs = copy.deepcopy(raw_segments)
    segs.sort(key=lambda x: x["segment"][1])
    for i in segs:
        for j in segs:
            if j["segment"][0] <= i["segment"][1] <= j["segment"][1]:
                i["segment"][1] = j["segment"][1]
    segs.sort(key=lambda x: x["segment"][0])
    for i in reversed(segs):
        for j in reversed(segs):
            if j["segment"][0] <= i["segment"][0] <= j["segment"][1]:
                i["segment"][0] = j["segment"][0]
    merged: list[dict] = []
    for i in segs:
        start, end = i["segment"]
        item = {"start": start, "end": end, "uuids": [i["UUID"]], "categories": [i.get("category", "unknown")]}
        if merged and item["start"] - merged[-1]["end"] < 1:
            prev = merged.pop()
            item["start"] = prev["start"]
            item["uuids"].extend(prev["uuids"])
            for c in prev["categories"]:
                if c not in item["categories"]:
                    item["categories"].append(c)
        if item["end"] - item["start"] > minimum_skip_length:
            merged.append(item)
    return merged


def match_seek(merged: list[dict], seek_to: float, tolerance: float = 0.75) -> tuple[dict | None, str]:
    """Find the merged segment whose end matches the logged seek position."""
    if not merged:
        return None, "none"
    best = min(merged, key=lambda s: abs(s["end"] - seek_to))
    diff = abs(best["end"] - seek_to)
    if diff <= 0.01:
        return best, "exact"
    if diff <= tolerance:
        return best, "nearest"
    return None, "none"


class SponsorBlockClient:
    def __init__(self, session: aiohttp.ClientSession, api_url: str = "https://sponsor.ajay.app/api/") -> None:
        self.session = session
        self.api_url = api_url if api_url.endswith("/") else api_url + "/"
        self._last = 0.0
        self.min_interval = 0.5
        self.rate_limited_until = 0.0

    async def fetch_segments(self, video_id: str, categories: list[str]) -> list[dict] | None:
        """Raw segments for ``video_id`` (privacy-preserving hashed lookup, as upstream). None = not found."""
        now = time.monotonic()
        if now < self.rate_limited_until:
            raise RuntimeError("sponsorblock rate limited")
        wait = self.min_interval - (now - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()
        hashed = sha256(video_id.encode("utf-8")).hexdigest()[:4]
        params = [("category", c) for c in categories] + [("actionType", "skip"), ("service", "youtube")]
        url = f"{self.api_url}skipSegments/{hashed}"
        async with self.session.get(url, params=params, headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 404:
                return None
            if resp.status == 429:
                self.rate_limited_until = time.monotonic() + 60
                log.warning("sponsorblock.rate_limited", "SponsorBlock rate limit hit; pausing lookups for 60s")
                raise RuntimeError("sponsorblock rate limited")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        for entry in data or []:
            if entry.get("videoID") == video_id:
                return [
                    {"segment": s["segment"], "UUID": s["UUID"], "category": s.get("category"),
                     "locked": s.get("locked", 0), "votes": s.get("votes", 0), "videoDuration": s.get("videoDuration")}
                    for s in entry.get("segments", [])
                ]
        return None
