"""Configuration loading and validation."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class ZoneConfig(BaseModel):
    """One BluOS player that should chime."""

    name: str
    host: str
    port: int = 11000

    #: Volume (0-100) the chime plays at in this zone. Falls back to the
    #: global default when unset.
    chime_volume: int | None = None

    #: Set false to keep a zone configured but temporarily silent.
    enabled: bool = True

    #: Chime even when this zone is idle/stopped. When false, a zone that
    #: isn't playing anything is left alone.
    chime_when_idle: bool = True

    #: Chime even when this zone is muted.
    chime_when_muted: bool = False

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class ChimeConfig(BaseModel):
    #: Filename inside the chimes directory, served over HTTP to the players.
    file: str = "doorbell.mp3"

    #: How long the chime runs, in seconds. The service waits this long before
    #: restoring. Measure your file and set this accurately — too short cuts
    #: the chime off, too long leaves a silent gap.
    duration_seconds: float = 3.0

    #: Extra settling time after the chime before restoring the source.
    tail_seconds: float = 0.8

    #: Default chime volume for zones that don't override it.
    default_volume: int = 30


class BehaviourConfig(BaseModel):
    #: Ignore repeat rings inside this window. Prevents a double-press from
    #: capturing the ducked volume as "previous" — the classic restore bug.
    debounce_seconds: float = 8.0

    #: Milliseconds to ramp volume down and back up. 0 disables ramping.
    fade_ms: int = 300
    fade_steps: int = 4

    #: If a captured state is older than this, discard it rather than restoring
    #: something stale after a crash or hang.
    state_ttl_seconds: float = 120.0

    #: What to do when a target zone is a secondary in a group whose primary is
    #: not itself a target.
    #:   "primary" — chime the group's primary anyway (whole group hears it)
    #:   "skip"    — leave that group alone
    group_policy: Literal["primary", "skip"] = "primary"

    #: Restore a paused player back to paused rather than leaving it playing.
    restore_pause_state: bool = True

    #: Per-request timeout when talking to a player.
    http_timeout_seconds: float = 5.0


class WebhookConfig(BaseModel):
    #: Shared secret. Requests must present it as ?token=... or an
    #: X-Doorbell-Token header. Empty disables the check (fine on a trusted
    #: VLAN, but set one if Protect can reach other networks).
    token: str = ""

    #: Only accept rings from these UniFi Protect device IDs / names. Empty
    #: means accept any ring the webhook delivers.
    allowed_devices: list[str] = Field(default_factory=list)


class Config(BaseModel):
    #: Base URL the PLAYERS use to fetch the chime — must be reachable from
    #: the players' VLAN. Auto-detected from the host's primary IP if unset.
    service_base_url: str = ""
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080

    zones: list[ZoneConfig]
    chime: ChimeConfig = Field(default_factory=ChimeConfig)
    behaviour: BehaviourConfig = Field(default_factory=BehaviourConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)

    log_level: str = "INFO"

    @field_validator("zones")
    @classmethod
    def _at_least_one_zone(cls, v: list[ZoneConfig]) -> list[ZoneConfig]:
        if not v:
            raise ValueError("at least one zone must be configured")
        return v

    def enabled_zones(self) -> list[ZoneConfig]:
        return [z for z in self.zones if z.enabled]

    def zone_by_address(self, address: str) -> ZoneConfig | None:
        return next((z for z in self.zones if z.address == address), None)

    def resolved_base_url(self) -> str:
        if self.service_base_url:
            return self.service_base_url.rstrip("/")
        return f"http://{_primary_ip()}:{self.listen_port}"

    def chime_url(self) -> str:
        return f"{self.resolved_base_url()}/chimes/{self.chime.file}"


def _primary_ip() -> str:
    """Best-effort local IP that other hosts on the LAN can reach.

    Opening a UDP socket to a public address doesn't send anything, but it
    makes the kernel pick the interface it would route through.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("DOORBELL_CONFIG", "/config/config.yaml"))
    if not path.exists():
        raise FileNotFoundError(
            f"config not found at {path} — copy config.example.yaml and edit it"
        )
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config.model_validate(raw)
