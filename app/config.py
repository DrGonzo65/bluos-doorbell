"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from tools.netutil import SubnetError, check_sweepable, parse_subnet


class ZoneConfig(BaseModel):
    """A player that should chime.

    With discovery on (the default) these entries are *overrides*: match a
    discovered player by name or host and change only the fields you set.
    An entry with a host that discovery never finds is still used as-is, so
    you can always pin something by hand.
    """

    name: str = ""
    host: str = ""
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

    @property
    def is_override_only(self) -> bool:
        """Names a player to tune but doesn't say where it is."""
        return not self.host and bool(self.name)

    def matches(self, name: str, host: str) -> bool:
        if self.host and self.host == host:
            return True
        return bool(self.name) and self.name.strip().lower() == name.strip().lower()


class DiscoveryConfig(BaseModel):
    """The service finds players itself; you only configure exceptions."""

    #: Discover players automatically. Turn off to use `zones` verbatim.
    auto: bool = True

    #: Network to sweep, as CIDR: "192.168.1.0/24", "10.0.4.0/22". Blank means
    #: this host's own subnet, using the interface's real netmask. Normalised
    #: on load, so "192.168.1.37/24" is stored as "192.168.1.0/24"; the old
    #: three-octet form ("192.168.1") is still read as a /24.
    subnet: str = ""

    @field_validator("subnet")
    @classmethod
    def _valid_cidr(cls, value: str) -> str:
        if not value or not value.strip():
            return ""
        try:
            net = parse_subnet(value)
            check_sweepable(net)
        except SubnetError as exc:
            raise ValueError(f"discovery.subnet: {exc}") from None
        return str(net)

    #: How often to re-run discovery. Players that move or get renamed are
    #: picked up within this window without a restart.
    refresh_seconds: float = 300.0

    #: Keep a socket open for the announcements players broadcast every ~57s,
    #: so a newly plugged-in player appears within about a minute.
    listen: bool = True

    #: A full subnet sweep is heavier than an LSDP query, so only do one every
    #: Nth refresh — or immediately whenever nothing is known yet.
    full_sweep_every: int = 12

    #: Names or IPs never to chime, matched case-insensitively.
    exclude: list[str] = Field(default_factory=list)

    def is_excluded(self, name: str, host: str) -> bool:
        for entry in self.exclude:
            needle = entry.strip().lower()
            if needle and (needle == host.lower() or needle == name.strip().lower()):
                return True
        return False


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


#: The doorbell `chime:` describes, and what a bare /doorbell rings.
DEFAULT_DOORBELL = "default"

_DOORBELL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class DoorbellConfig(BaseModel):
    """An additional doorbell with its own sound.

    It rings the same rooms at the same volumes as every other doorbell — only
    the sound differs. Its webhook is /doorbell/<name>.
    """

    #: Filename inside the chimes directory.
    file: str

    #: Length of THIS file, in seconds. Required rather than inherited from
    #: `chime:`, because a different sound is almost never the same length and
    #: a wrong value either clips it or leaves a silent gap.
    duration_seconds: float = Field(gt=0, le=60)

    #: Settling time after the chime. Defaults to chime.tail_seconds.
    tail_seconds: float | None = Field(default=None, ge=0, le=10)


@dataclass(frozen=True)
class Chime:
    """A doorbell's sound, fully resolved — what the orchestrator plays."""

    doorbell: str
    file: str
    duration_seconds: float
    tail_seconds: float

    @property
    def total_seconds(self) -> float:
        return self.duration_seconds + self.tail_seconds


class BehaviourConfig(BaseModel):
    #: Ignore repeat rings of the SAME doorbell inside this window. Counted per
    #: doorbell, so a second doorbell is never swallowed by the first. Presses
    #: during a chime are queued, not debounced; the in-flight gate is what
    #: stops a second capture from saving the ducked volume.
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
    listen_port: int = 8095

    #: Overrides layered on top of discovery — see ZoneConfig.
    zones: list[ZoneConfig] = Field(default_factory=list)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    #: The default doorbell's sound — what /doorbell rings.
    chime: ChimeConfig = Field(default_factory=ChimeConfig)
    #: Further doorbells, each with its own sound, keyed by the name used in
    #: its webhook URL: /doorbell/<name>.
    doorbells: dict[str, DoorbellConfig] = Field(default_factory=dict)
    behaviour: BehaviourConfig = Field(default_factory=BehaviourConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)

    log_level: str = "INFO"

    #: Zero zones is legal — with discovery on, that is the normal case. The
    #: service finds its own players and only crash-loops if you insist on
    #: manual zones and then list none.
    @property
    def is_configured(self) -> bool:
        return self.discovery.auto or bool(self.enabled_zones())

    def enabled_zones(self) -> list[ZoneConfig]:
        return [z for z in self.zones if z.enabled and z.host]

    def zone_by_address(self, address: str) -> ZoneConfig | None:
        return next((z for z in self.zones if z.address == address), None)

    def resolved_base_url(self) -> str:
        if self.service_base_url:
            return self.service_base_url.rstrip("/")
        return f"http://{_primary_ip()}:{self.listen_port}"

    @field_validator("doorbells", mode="before")
    @classmethod
    def _doorbell_names(cls, value):
        if not value:
            return {}
        if not isinstance(value, dict):
            raise ValueError("doorbells must be a mapping of name -> settings")
        out: dict = {}
        for raw, settings in value.items():
            name = str(raw).strip().lower()
            if name == DEFAULT_DOORBELL:
                raise ValueError(
                    f"doorbells: {raw!r} is reserved — the default doorbell is "
                    f"configured under chime:")
            if not _DOORBELL_NAME.match(name):
                raise ValueError(
                    f"doorbells: {raw!r} can't be used in a URL — use letters, "
                    f"digits, '-' or '_' (e.g. back-door)")
            if name in out:
                raise ValueError(f"doorbells: {raw!r} is listed twice")
            out[name] = settings
        return out

    def chime_for(self, doorbell: str | None) -> Chime | None:
        """The sound for a doorbell, or None if no doorbell has that name."""
        name = (doorbell or DEFAULT_DOORBELL).strip().lower()
        if name == DEFAULT_DOORBELL:
            return Chime(DEFAULT_DOORBELL, self.chime.file,
                         self.chime.duration_seconds, self.chime.tail_seconds)
        bell = self.doorbells.get(name)
        if bell is None:
            return None
        tail = bell.tail_seconds if bell.tail_seconds is not None else self.chime.tail_seconds
        return Chime(name, bell.file, bell.duration_seconds, tail)

    def all_chimes(self) -> list[Chime]:
        chimes = [self.chime_for(DEFAULT_DOORBELL)]
        chimes += [self.chime_for(name) for name in sorted(self.doorbells)]
        return [c for c in chimes if c is not None]

    def chime_url(self, file: str | None = None) -> str:
        return f"{self.resolved_base_url()}/chimes/{file or self.chime.file}"


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
