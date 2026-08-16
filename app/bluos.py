"""Thin async client for the BluOS Custom Integration API (v1.7).

Every BluOS player exposes a plain HTTP API on port 11000 that returns XML.
This module wraps the handful of endpoints the doorbell service needs and
normalises the XML into dictionaries.

Reference: BluOS Custom Integration API v1.7
https://bluos.io/wp-content/uploads/2025/06/BluOS-Custom-Integration-API_v1.7.pdf
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

DEFAULT_PORT = 11000


def _xml_to_dict(text: str) -> dict[str, Any]:
    """Flatten a BluOS XML response into {tag: text} plus root attributes.

    BluOS responses are shallow: a root element with attributes and a flat
    list of child elements. Repeated tags (e.g. <slave>) collect into a list.
    """
    root = ET.fromstring(text)
    out: dict[str, Any] = {f"@{k}": v for k, v in root.attrib.items()}
    out["@root"] = root.tag

    for child in root:
        # Keep child attributes too — <slave id="..." port="..."/> carries
        # everything in attributes and has no text.
        value: Any
        if child.attrib and not (child.text or "").strip():
            value = dict(child.attrib)
        else:
            value = (child.text or "").strip()
            if child.attrib:
                value = {"_text": value, **child.attrib}

        if child.tag in out:
            existing = out[child.tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[child.tag] = [existing, value]
        else:
            out[child.tag] = value

    return out


def _as_int(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, dict):
        value = value.get("_text", "")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class PlayerState:
    """Everything needed to put a player back the way we found it."""

    state: str = "stop"          # play | pause | stop | stream | connecting
    service: str | None = None
    stream_url: str | None = None
    input_id: str | None = None
    song: int | None = None       # index in the play queue
    pid: int | None = None        # play queue id — changes if the queue changes
    secs: int = 0                 # elapsed seconds in the current track
    totlen: int | None = None
    can_seek: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.state in ("play", "stream")

    @property
    def source_kind(self) -> str:
        """How this player needs to be restarted after an interruption."""
        if self.input_id:
            return "input"
        if self.stream_url:
            return "stream"
        if self.song is not None:
            return "queue"
        return "unknown"


@dataclass
class VolumeState:
    level: int = -1               # 0-100, or -1 for fixed-volume players
    db: float | None = None
    mute: bool = False

    @property
    def is_fixed(self) -> bool:
        return self.level < 0


@dataclass
class SyncState:
    name: str = ""
    group: str | None = None
    master: str | None = None        # "ip:port" when this player is a secondary
    slaves: list[str] = field(default_factory=list)
    volume: int = -1
    mac: str | None = None

    @property
    def is_secondary(self) -> bool:
        return bool(self.master)

    @property
    def is_primary_of_group(self) -> bool:
        return bool(self.slaves)


class BluOSError(RuntimeError):
    pass


class BluOSPlayer:
    """One BluOS player, addressed by host:port."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, *, name: str | None = None,
                 client: httpx.AsyncClient | None = None, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.name = name or host
        self._client = client
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BluOSPlayer {self.name} {self.address}>"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            return _xml_to_dict(resp.text)
        except httpx.HTTPError as exc:
            raise BluOSError(f"{self.name}: {path} failed: {exc}") from exc
        except ET.ParseError as exc:
            raise BluOSError(f"{self.name}: {path} returned unparseable XML: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

    # -- state ----------------------------------------------------------------

    async def status(self) -> PlayerState:
        """GET /Status — playback state. NOTE: on a grouped secondary this is a
        copy of the primary's status, so never read per-player volume from here."""
        data = await self._get("/Status")
        return PlayerState(
            state=str(data.get("state", "stop")).strip() or "stop",
            service=data.get("service") or None,
            stream_url=data.get("streamUrl") or None,
            input_id=data.get("inputId") or None,
            song=_as_int(data.get("song")),
            pid=_as_int(data.get("pid")),
            secs=_as_int(data.get("secs"), 0) or 0,
            totlen=_as_int(data.get("totlen")),
            can_seek=_as_int(data.get("canSeek"), 0) == 1,
            raw=data,
        )

    async def volume(self) -> VolumeState:
        """GET /Volume — this player's OWN volume, unlike /Status when grouped."""
        data = await self._get("/Volume")
        db_raw = data.get("db")
        try:
            db = float(db_raw) if db_raw not in (None, "") else None
        except (TypeError, ValueError):
            db = None
        return VolumeState(
            level=_as_int(data.get("volume"), -1) if data.get("volume") is not None
            else _as_int(data.get("@volume"), -1),
            db=db,
            mute=_as_int(data.get("mute"), 0) == 1,
        )

    async def sync_status(self) -> SyncState:
        """GET /SyncStatus — identity and grouping."""
        data = await self._get("/SyncStatus")

        def _addr(entry: Any) -> str | None:
            if isinstance(entry, dict):
                ip = entry.get("id") or entry.get("ip") or entry.get("_text")
                port = entry.get("port", DEFAULT_PORT)
                return f"{ip}:{port}" if ip else None
            if isinstance(entry, str) and entry:
                return entry
            return None

        master_raw = data.get("master")
        slaves_raw = data.get("slave")
        if slaves_raw is None:
            slaves_raw = []
        elif not isinstance(slaves_raw, list):
            slaves_raw = [slaves_raw]

        # Attributes live on the root element for /SyncStatus.
        name = data.get("@name") or data.get("name") or self.name
        return SyncState(
            name=str(name),
            group=data.get("@group") or data.get("group") or None,
            master=_addr(master_raw),
            slaves=[a for a in (_addr(s) for s in slaves_raw) if a],
            volume=_as_int(data.get("@volume"), -1) if data.get("@volume") is not None
            else _as_int(data.get("volume"), -1),
            mac=data.get("@mac") or data.get("mac"),
        )

    # -- control --------------------------------------------------------------

    async def set_volume(self, level: int, *, tell_slaves: bool = False) -> None:
        """Set absolute volume 0-100 on THIS player only by default.

        tell_slaves=1 writes the same absolute level to every group member,
        which is what flattens a group's relative balance. We keep it off.
        """
        level = max(0, min(100, int(level)))
        await self._get("/Volume", {"level": level, "tell_slaves": 1 if tell_slaves else 0})

    async def play_url(self, url: str) -> None:
        """GET /Play?url= — interrupt whatever is playing with an HTTP audio stream."""
        # BluOS wants the URL percent-encoded as a single parameter value.
        await self._get(f"/Play?url={quote(url, safe='')}")

    async def play(self) -> None:
        await self._get("/Play")

    async def pause(self) -> None:
        await self._get("/Pause")

    async def stop(self) -> None:
        await self._get("/Stop")

    async def resume_queue(self, song: int, seek: int | None = None) -> None:
        params: dict[str, Any] = {"id": song}
        if seek is not None:
            params["seek"] = max(0, seek)
        await self._get("/Play", params)

    async def play_input(self, input_id: str) -> None:
        # BluOS 4.2.0+ uses inputTypeIndex (e.g. "spdif-1"); older firmware uses
        # InputId. We send whichever shape the captured value looks like.
        if "-" in input_id and not input_id.startswith("input"):
            await self._get("/Play", {"inputTypeIndex": input_id})
        else:
            await self._get("/Play", {"InputId": input_id})

    async def doorbell(self) -> dict[str, Any]:
        """GET /Doorbell?play=1 — native chime. Only present on NAD CI 580 and
        Bluesound Professional B400S; Node players will error."""
        return await self._get("/Doorbell", {"play": 1})
