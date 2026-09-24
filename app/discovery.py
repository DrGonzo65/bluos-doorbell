"""Keeps a live picture of which BluOS players exist on the network.

Three sources, cheapest first:

* a **passive listener** on udp/11430 — players announce themselves roughly
  every 57 seconds, so plugging in a new one gets noticed without polling;
* a **periodic LSDP query**, which asks everything to announce right now;
* a **subnet sweep** over HTTP, used when the other two find nothing and
  occasionally afterwards to catch anything whose broadcasts don't reach us.

The result is merged with the ``zones`` overrides from config.yaml, so the
normal configuration is an empty list and the service works out the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from dataclasses import dataclass, field

import httpx

from .config import Config, ZoneConfig
from tools import lsdp
from tools.discover import discover_sweep, probe
from tools.netutil import SubnetError, resolve

log = logging.getLogger("doorbell.discovery")


@dataclass
class DiscoveredPlayer:
    host: str
    name: str
    detail: str = ""
    source: str = "lsdp"          # lsdp | sweep | listener
    port: int = 11000
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.last_seen


class _LSDPProtocol(asyncio.DatagramProtocol):
    """Feeds every announcement it hears into the registry."""

    def __init__(self, registry: "PlayerRegistry"):
        self.registry = registry

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        for ann in lsdp.parse(data, source_ip=addr[0]):
            if ann.is_player:
                self.registry.note(ann.address, ann.name, ann.model, "listener")

    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        log.debug("lsdp socket error: %s", exc)


class PlayerRegistry:
    def __init__(self, config: Config, client: httpx.AsyncClient):
        self.config = config
        self.client = client
        self.players: dict[str, DiscoveredPlayer] = {}
        self.last_refresh: float | None = None
        self.last_error: str | None = None
        #: The CIDR last swept, for /health and /discover.
        self.network: str | None = None
        self._refreshes = 0
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if not self.config.discovery.auto:
            log.info("discovery disabled (discovery.auto=false) — using configured zones only")
            return

        if self.config.discovery.listen:
            await self._start_listener()

        # Discover once before serving, so the very first ring has targets.
        await self.refresh(force_sweep=True)
        self._tasks.append(asyncio.create_task(self._refresh_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._transport:
            self._transport.close()
            self._transport = None

    async def _start_listener(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind(("", lsdp.PORT))
        except OSError as exc:
            sock.close()
            log.warning("not listening for player announcements: %s", exc)
            log.warning("(needs host networking — discovery falls back to sweeping)")
            return

        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _LSDPProtocol(self), sock=sock
        )
        self._transport = transport
        log.info("listening for player announcements on udp/%d", lsdp.PORT)

    async def _refresh_loop(self) -> None:
        interval = max(30.0, self.config.discovery.refresh_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a refresh must never kill the loop
                log.warning("discovery refresh failed: %s", exc)

    # -- discovery ------------------------------------------------------------

    def note(self, host: str, name: str, detail: str, source: str,
             port: int = 11000) -> None:
        """Record a sighting. Called from the listener and the active scans."""
        if not host or self.config.discovery.is_excluded(name, host):
            return
        existing = self.players.get(host)
        if existing:
            existing.last_seen = time.monotonic()
            if name and name != host:
                existing.name = name
            if detail:
                existing.detail = detail
        else:
            self.players[host] = DiscoveredPlayer(host, name or host, detail,
                                                  source, port)
            log.info("discovered %s at %s (%s)", name or host, host, source)

    async def refresh(self, force_sweep: bool = False) -> None:
        async with self._lock:
            cfg = self.config.discovery
            before = set(self.players)

            # 1. Active LSDP query — cheap, and answers arrive on the listener
            #    socket too when it is running.
            found, error = await asyncio.to_thread(self._lsdp_query, 3.0)
            self.last_error = error
            for host, name, detail in found:
                self.note(host, name, detail, "lsdp")

            # 2. Re-probe what we already know, so renames and departures show up.
            await self._verify_known()

            # 3. Sweep when we have nothing, or every Nth refresh as a backstop
            #    for players whose broadcasts never reach us.
            due = cfg.full_sweep_every > 0 and self._refreshes % cfg.full_sweep_every == 0
            if force_sweep or due or not self.players:
                try:
                    network, source = resolve(cfg.subnet)
                except SubnetError as exc:
                    network = None
                    self.last_error = str(exc)
                    log.warning("not sweeping: %s", exc)
                if network is not None:
                    self.network = str(network)
                    log.info("sweeping %s (%s, %d addresses) for players",
                             network, source, network.num_addresses - 2
                             if network.prefixlen < 31 else network.num_addresses)
                    for host, name, detail in await discover_sweep(network):
                        self.note(host, name, detail, "sweep")

            self._refreshes += 1
            self.last_refresh = time.monotonic()

            new = set(self.players) - before
            if new:
                log.info("now watching %d player(s); new: %s",
                         len(self.players), ", ".join(sorted(new)))
            elif not self.players:
                log.warning("no players found — check the subnet, or list them "
                            "manually under zones: in config.yaml")

    def _lsdp_query(self, timeout: float) -> tuple[list[tuple[str, str, str]], str | None]:
        """Send a query and collect replies on a throwaway socket.

        Separate from the long-lived listener because that one may be bound by
        the event loop; replies land on whichever socket is listening.
        """
        from tools.discover import discover_lsdp
        try:
            network, _ = resolve(self.config.discovery.subnet)
        except SubnetError:
            network = None
        return discover_lsdp(timeout, network)

    async def _verify_known(self) -> None:
        """Confirm known players still answer, and pick up renames."""
        if not self.players:
            return

        async def check(player: DiscoveredPlayer) -> tuple[str, tuple | None]:
            return player.host, await probe(self.client, player.host, player.port)

        results = await asyncio.gather(*(check(p) for p in list(self.players.values())),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                continue
            host, hit = result
            if hit:
                _, name, detail = hit
                self.note(host, name, detail, "verify")
            else:
                player = self.players.get(host)
                # Don't drop on a single miss — a player can be briefly busy.
                if player and player.age_seconds > 15 * 60:
                    log.info("dropping %s at %s — unreachable for 15 minutes",
                             player.name, host)
                    self.players.pop(host, None)

    # -- the merged view the rest of the service uses --------------------------

    def zones(self) -> list[ZoneConfig]:
        """Discovered players with config overrides applied, plus manual zones."""
        cfg = self.config
        out: list[ZoneConfig] = []
        used_overrides: set[int] = set()

        if cfg.discovery.auto:
            for player in sorted(self.players.values(), key=lambda p: p.name.lower()):
                zone = ZoneConfig(name=player.name, host=player.host,
                                  port=player.port)

                for index, override in enumerate(cfg.zones):
                    if not override.matches(player.name, player.host):
                        continue
                    used_overrides.add(index)
                    # Only fields the user actually wrote should win.
                    for field_name in override.model_fields_set:
                        if field_name in ("name", "host"):
                            continue
                        setattr(zone, field_name, getattr(override, field_name))
                    if "name" in override.model_fields_set and override.name:
                        zone.name = override.name

                if zone.enabled:
                    out.append(zone)

        # Explicit zones that discovery didn't find (or discovery is off).
        for index, override in enumerate(cfg.zones):
            if index in used_overrides or override.is_override_only:
                continue
            if not override.host or not override.enabled:
                continue
            if any(z.address == override.address for z in out):
                continue
            out.append(override)

        return out

    def status(self) -> dict:
        return {
            "auto": self.config.discovery.auto,
            "listening": self._transport is not None,
            "known_players": len(self.players),
            "last_refresh_seconds_ago": (
                round(time.monotonic() - self.last_refresh, 1)
                if self.last_refresh else None
            ),
            "network": self.network,
            "last_error": self.last_error,
            "players": [
                {"name": p.name, "host": p.host, "port": p.port,
                 "detail": p.detail, "source": p.source,
                 "last_seen_seconds_ago": round(p.age_seconds, 1)}
                for p in sorted(self.players.values(), key=lambda p: p.name.lower())
            ],
        }
