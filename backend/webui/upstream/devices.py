"""Device registry: stable identity (hashed screen id) across renames, config edits and removal."""

from __future__ import annotations

from ..db import Database
from ..logging import get_logger
from ..logging.redaction import register_secret
from ..util import now_iso, row_to_dict, screen_hint, screen_key

log = get_logger("upstream.devices")


class DeviceRegistry:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._by_key: dict[str, dict] = {}
        self._loaded = False

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT * FROM devices")
        self._by_key = {r["screen_key"]: row_to_dict(r) for r in rows}
        self._loaded = True

    def all(self) -> list[dict]:
        return list(self._by_key.values())

    def by_id(self, device_id: int) -> dict | None:
        for d in self._by_key.values():
            if d["id"] == device_id:
                return d
        return None

    def by_key(self, key: str) -> dict | None:
        return self._by_key.get(key)

    async def sync_from_config(self, config: dict | None) -> dict:
        """Upsert configured devices; mark the rest as not configured. Returns a summary."""
        if not self._loaded:
            await self.load()
        now = now_iso()
        configured_keys: set[str] = set()
        added = renamed = removed = 0
        for dev in (config or {}).get("devices", []) if isinstance((config or {}).get("devices"), list) else []:
            if not isinstance(dev, dict):
                continue
            sid = dev.get("screen_id")
            if not isinstance(sid, str) or not sid:
                continue
            register_secret(sid, "SCREEN_ID")
            key = screen_key(sid)
            configured_keys.add(key)
            name = dev.get("name") if isinstance(dev.get("name"), str) and dev.get("name").strip() else None
            existing = self._by_key.get(key)
            if existing is None:
                display = name or f"TV …{screen_hint(sid)}"
                did = await self.db.execute_returning_id(
                    "INSERT INTO devices (screen_key, screen_hint, screen_id_length, config_name, display_name, configured, first_seen_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (key, screen_hint(sid), len(sid), name, display, now, now),
                )
                await self.db.execute("INSERT OR IGNORE INTO device_state (device_id, state, state_since, state_evidence) VALUES (?, 'configured', ?, 'present in config.json')", (did, now))
                self._by_key[key] = row_to_dict(await self.db.fetchone("SELECT * FROM devices WHERE id = ?", (did,)))
                added += 1
                log.info("device.added", "Device appeared in config.json", device_id=did, name=display, legacy_format=len(sid) == 26)
            else:
                changed = False
                if existing.get("config_name") != name:
                    existing["config_name"] = name
                    changed = True
                    renamed += 1
                display = name or existing.get("screen_name") or existing.get("display_name")
                if display != existing.get("display_name") or not existing.get("configured") or existing.get("removed_at"):
                    changed = True
                if changed:
                    await self.db.execute(
                        "UPDATE devices SET config_name = ?, display_name = ?, configured = 1, removed_at = NULL, screen_id_length = ?, screen_hint = ? WHERE id = ?",
                        (name, display, len(sid), screen_hint(sid), existing["id"]),
                    )
                    existing.update({"display_name": display, "configured": 1, "removed_at": None, "screen_id_length": len(sid), "screen_hint": screen_hint(sid)})
        for key, dev in self._by_key.items():
            if key not in configured_keys and dev.get("configured"):
                await self.db.execute("UPDATE devices SET configured = 0, removed_at = ? WHERE id = ?", (now, dev["id"]))
                dev["configured"] = 0
                dev["removed_at"] = now
                removed += 1
                log.info("device.removed", "Device no longer in config.json; history preserved", device_id=dev["id"], name=dev["display_name"])
        return {"configured": len(configured_keys), "added": added, "renamed": renamed, "removed": removed}

    async def from_log(self, key: str, screen_id: str | None) -> dict:
        """A device logger appeared that is not in config (config changed externally or stale run)."""
        dev = self._by_key.get(key)
        if dev:
            return dev
        now = now_iso()
        hint = screen_hint(screen_id) if screen_id else "????"
        did = await self.db.execute_returning_id(
            "INSERT INTO devices (screen_key, screen_hint, screen_id_length, display_name, configured, first_seen_at, created_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (key, hint, len(screen_id) if screen_id else None, f"Unlisted TV …{hint}", now, now),
        )
        await self.db.execute("INSERT OR IGNORE INTO device_state (device_id, state, state_since, state_evidence) VALUES (?, 'unknown', ?, 'seen in logs but not in config.json')", (did, now))
        dev = row_to_dict(await self.db.fetchone("SELECT * FROM devices WHERE id = ?", (did,)))
        self._by_key[key] = dev
        log.warning("device.unlisted", "Device seen in upstream logs but not in config.json", device_id=did)
        return dev

    async def set_screen_name(self, device_id: int, screen_name: str) -> None:
        dev = self.by_id(device_id)
        if dev is None or dev.get("screen_name") == screen_name:
            return
        display = dev.get("config_name") or screen_name
        await self.db.execute("UPDATE devices SET screen_name = ?, display_name = ? WHERE id = ?", (screen_name, display, device_id))
        dev["screen_name"] = screen_name
        dev["display_name"] = display

    async def touch(self, device_id: int, at: str) -> None:
        await self.db.execute("UPDATE devices SET last_seen_at = ? WHERE id = ? AND (last_seen_at IS NULL OR last_seen_at < ?)", (at, device_id, at))
        dev = self.by_id(device_id)
        if dev and (not dev.get("last_seen_at") or dev["last_seen_at"] < at):
            dev["last_seen_at"] = at
