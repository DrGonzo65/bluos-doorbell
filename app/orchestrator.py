"""The doorbell choreography: capture -> duck -> chime -> restore.

The whole point of this service is doing the save/restore correctly, which the
stock drivers get wrong in two specific ways:

1. Volume is captured per-player from that player's own ``GET /Volume`` and
   restored with ``tell_slaves=0``. Reading volume from ``/Status`` on a
   grouped secondary returns the *primary's* volume (API doc section 2.2),
   which is how every zone in a group ends up flattened to one level.

2. Transport commands sent to a grouped secondary are proxied to the primary
   (section 8). So we resolve the group topology first and send exactly one
   chime per group primary, instead of one per member.

A single in-flight gate makes a double-press incapable of capturing the ducked
volume as the "previous" volume.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .bluos import BluOSError, BluOSPlayer, PlayerState, VolumeState
from .config import Chime, Config, ZoneConfig

log = logging.getLogger(__name__)


@dataclass
class MemberSnapshot:
    """A single player's volume, saved so we can put it back exactly."""

    player: BluOSPlayer
    volume: VolumeState
    chime_volume: int

    @property
    def adjustable(self) -> bool:
        return not self.volume.is_fixed


@dataclass
class GroupSnapshot:
    """Everything captured for one group before we touch anything."""

    primary: BluOSPlayer
    members: list[MemberSnapshot]
    status: PlayerState
    zones: list[ZoneConfig]
    captured_at: float = field(default_factory=time.monotonic)

    @property
    def label(self) -> str:
        return " + ".join(z.name for z in self.zones) or self.primary.name

    @property
    def is_stale(self) -> bool:
        return False  # replaced by ttl check in the orchestrator


@dataclass
class RingResult:
    status: str                       # "chimed" | "debounced" | "extended" | "error"
    #: Doorbells whose chime played, in order — more than one when a second
    #: doorbell was pressed while the first was still chiming.
    doorbells: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Things worth knowing that aren't errors — e.g. a room you targeted is
    #: grouped, so other rooms heard the chime too.
    notes: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "doorbells": self.doorbells,
            "groups": self.groups,
            "skipped": self.skipped,
            "errors": self.errors,
            "notes": self.notes,
            "duration_seconds": round(self.duration_seconds, 2),
        }


class DoorbellOrchestrator:
    def __init__(self, config: Config, client: httpx.AsyncClient, registry=None):
        self.config = config
        self.client = client
        #: Supplies the live zone list. Without one we fall back to the
        #: configured zones, which keeps the tests and manual mode simple.
        self.registry = registry
        self._gate = asyncio.Lock()
        self._active = False
        #: True only while chimes are actually playing. Before the first chime
        #: starts, or once restoring has begun, a new press can't join the
        #: running sequence — it waits for it to finish and starts its own.
        self._accepting = False
        #: Chimes waiting to play after the current one, in press order.
        self._queue: list[Chime] = []
        #: Doorbells played by the running sequence.
        self._played: list[str] = []
        #: The zone subset of the running sequence, if it's a room test.
        self._running_only: list[ZoneConfig] | None = None
        #: Set whenever no sequence is running.
        self._idle = asyncio.Event()
        self._idle.set()
        #: Per-doorbell, so the back door isn't ignored because the front door
        #: rang a few seconds earlier.
        self._last_ring: dict[str, float] = {}
        self.last_result: RingResult | None = None

    #: Most chimes that can be waiting at once. Two doorbells plus a repeat of
    #: each is already more than anyone needs to hear.
    MAX_QUEUED = 4

    def reset_debounce(self) -> None:
        """Forget recent rings — lets a manual test fire immediately."""
        self._last_ring.clear()

    # -- public entry point ---------------------------------------------------

    def target_zones(self) -> list[ZoneConfig]:
        if self.registry is not None:
            return self.registry.zones()
        return self.config.enabled_zones()

    async def ring(self, source: str = "manual",
                   only: list[ZoneConfig] | None = None,
                   chime: Chime | None = None) -> RingResult:
        """Run the chime sequence.

        ``chime`` picks the doorbell's sound; it defaults to the one under
        `chime:` in config. ``only`` restricts the rooms — used by the
        single-room test. A zone that is a group secondary still chimes via
        its primary, so the whole group hears it; that's how BluOS routes
        audio.
        """
        chime = chime or self.config.chime_for(None)

        if not (only or self.target_zones()):
            if self.config.discovery.auto:
                log.warning("ring from %s ignored — no players discovered yet. "
                            "Check /discover, or list them under zones: in "
                            "config.yaml.", source)
                return RingResult(status="error", doorbells=[chime.doorbell],
                                  errors=["no players discovered yet"])
            log.warning("ring from %s ignored — no zones configured. Edit "
                        "config.yaml and restart.", source)
            return RingResult(status="error", doorbells=[chime.doorbell],
                              errors=["no zones configured — edit config.yaml"])

        while True:
            async with self._gate:
                if self._active:
                    # Join the running sequence only while it's actually
                    # chiming, and only if neither side is a room test — a
                    # visitor at the door must not ring in just the room
                    # being tested, nor a test ring the whole house.
                    if self._accepting and only is None and self._running_only is None:
                        return self._enqueue(chime, source)
                else:
                    since = time.monotonic() - self._last_ring.get(chime.doorbell, float("-inf"))
                    if since < self.config.behaviour.debounce_seconds:
                        log.info("%s ring from %s debounced (%.1fs since last)",
                                 chime.doorbell, source, since)
                        return RingResult(status="debounced", doorbells=[chime.doorbell])
                    self._active = True
                    self._accepting = False
                    self._queue.clear()
                    self._played = [chime.doorbell]
                    self._running_only = only
                    self._idle.clear()
                    break
            # Too early or too late to join — capture hasn't finished or
            # restore has begun. Wait it out, then start a fresh sequence,
            # which re-captures the (by then restored) volumes.
            log.info("%s ring from %s waiting for the current sequence to finish",
                     chime.doorbell, source)
            await self._idle.wait()

        started = time.monotonic()
        try:
            result = await self._run_sequence(only, chime)
        except Exception as exc:  # noqa: BLE001 - never let a ring kill the service
            log.exception("doorbell sequence failed")
            result = RingResult(status="error", doorbells=list(self._played),
                                errors=[str(exc)])
        finally:
            async with self._gate:
                ended = time.monotonic()
                for name in self._played:
                    self._last_ring[name] = ended
                if self._queue:
                    log.warning("dropping %d queued chime(s) that never played: %s",
                                len(self._queue),
                                ", ".join(c.doorbell for c in self._queue))
                self._queue.clear()
                self._active = False
                self._accepting = False
                self._running_only = None
                self._idle.set()

        result.duration_seconds = time.monotonic() - started
        self.last_result = result
        return result

    def _enqueue(self, chime: Chime, source: str) -> RingResult:
        """Add a chime to the running sequence. Caller holds the gate."""
        if self._queue and self._queue[-1].doorbell == chime.doorbell:
            log.info("%s ring from %s — already queued", chime.doorbell, source)
            return RingResult(status="extended", doorbells=[chime.doorbell],
                              notes=[f"{chime.doorbell} is already queued"])
        if len(self._queue) >= self.MAX_QUEUED:
            log.warning("%s ring from %s — queue full, ignored", chime.doorbell, source)
            return RingResult(status="extended", doorbells=[chime.doorbell],
                              notes=["queue full — press ignored"])
        self._queue.append(chime)
        log.info("%s ring from %s — queued to play after the chime in progress",
                 chime.doorbell, source)
        return RingResult(status="extended", doorbells=[chime.doorbell],
                          notes=[f"{chime.doorbell} queued to play after the "
                                 f"chime in progress"])

    # -- topology -------------------------------------------------------------

    def _player(self, host: str, port: int = 11000, name: str | None = None) -> BluOSPlayer:
        return BluOSPlayer(
            host, port, name=name, client=self.client,
            timeout=self.config.behaviour.http_timeout_seconds,
        )

    def _zone_by_address(self, address: str) -> ZoneConfig | None:
        return next((z for z in self.target_zones() if z.address == address), None)

    def _player_from_address(self, address: str) -> BluOSPlayer:
        host, _, port = address.partition(":")
        zone = self._zone_by_address(address)
        return self._player(host, int(port or 11000), name=zone.name if zone else host)

    async def resolve_groups(self, zones: list[ZoneConfig] | None = None
                             ) -> tuple[list[tuple[BluOSPlayer, list[BluOSPlayer], list[ZoneConfig]]], list[str]]:
        """Map configured zones onto the group primaries that must be addressed.

        Returns (targets, skipped) where each target is
        (primary, all_members_including_primary, zones_that_asked_for_it).
        """
        zones = zones if zones is not None else self.target_zones()
        skipped: list[str] = []

        async def sync(zone: ZoneConfig):
            player = self._player(zone.host, zone.port, zone.name)
            try:
                return zone, player, await player.sync_status()
            except BluOSError as exc:
                log.warning("skipping %s: %s", zone.name, exc)
                skipped.append(f"{zone.name} (unreachable)")
                return zone, player, None

        results = await asyncio.gather(*(sync(z) for z in zones))

        # primary address -> (primary player, member addresses, zones)
        grouped: dict[str, tuple[BluOSPlayer, list[str], list[ZoneConfig]]] = {}

        for zone, player, sync_state in results:
            if sync_state is None:
                continue

            if sync_state.is_secondary:
                primary_addr = sync_state.master
                assert primary_addr is not None
                owns_primary = self._zone_by_address(primary_addr) is not None
                if not owns_primary and self.config.behaviour.group_policy == "skip":
                    log.info(
                        "%s is grouped under %s which is not a target — skipping "
                        "(group_policy=skip)", zone.name, primary_addr,
                    )
                    skipped.append(f"{zone.name} (secondary of untargeted group)")
                    continue
                primary = self._player_from_address(primary_addr)
                log.info("%s is a group secondary — chiming via primary %s",
                         zone.name, primary.name)
            else:
                primary_addr = player.address
                primary = player

            entry = grouped.get(primary_addr)
            if entry is None:
                members = [primary_addr, *sync_state.slaves] if not sync_state.is_secondary \
                    else [primary_addr]
                grouped[primary_addr] = (primary, members, [zone])
            else:
                entry[2].append(zone)
                for slave in sync_state.slaves:
                    if slave not in entry[1]:
                        entry[1].append(slave)

        # A group primary we reached indirectly may have slaves we haven't
        # enumerated. Ask it directly so we can save every member's volume.
        async def expand(primary_addr: str, primary: BluOSPlayer, members: list[str]):
            try:
                state = await primary.sync_status()
            except BluOSError:
                return members
            for slave in state.slaves:
                if slave not in members:
                    members.append(slave)
            return members

        targets = []
        for primary_addr, (primary, members, zones_for) in grouped.items():
            members = await expand(primary_addr, primary, members)
            member_players = [self._player_from_address(a) for a in members]
            targets.append((primary, member_players, zones_for))

        return targets, skipped

    # -- the sequence ---------------------------------------------------------

    async def _run_sequence(self, only: list[ZoneConfig] | None = None,
                            chime: Chime | None = None) -> RingResult:
        chime = chime or self.config.chime_for(None)
        targets, skipped = await self.resolve_groups(only)
        if not targets:
            return RingResult(status="error", skipped=skipped,
                              errors=["no reachable zones"])

        snapshots = await asyncio.gather(
            *(self._capture(primary, members, zones) for primary, members, zones in targets),
            return_exceptions=True,
        )

        live: list[GroupSnapshot] = []
        errors: list[str] = []
        for snap in snapshots:
            if isinstance(snap, BaseException):
                errors.append(str(snap))
            elif isinstance(snap, str):
                skipped.append(snap)
            else:
                live.append(snap)

        if not live:
            return RingResult(status="error", skipped=skipped,
                              errors=errors or ["nothing to chime"])

        # Anything past this point must restore, even if it throws.
        try:
            await asyncio.gather(*(self._duck(s) for s in live), return_exceptions=True)

            # Open the queue only now that sound is actually playing.
            async with self._gate:
                self._accepting = True
            await self._play_all(live, chime)

            # Chimes queued while that played — another doorbell, or the same
            # one pressed again. Played in order, in every group, before any
            # restore. Checking and closing the queue under the gate means a
            # press can't slip in between "queue is empty" and "stop taking".
            while True:
                async with self._gate:
                    if not self._queue:
                        self._accepting = False
                        break
                    nxt = self._queue.pop(0)
                    self._played.append(nxt.doorbell)
                log.info("playing queued %s chime", nxt.doorbell)
                await self._play_all(live, nxt)
        finally:
            async with self._gate:
                self._accepting = False
            restores = await asyncio.gather(
                *(self._restore(s) for s in live), return_exceptions=True
            )
            for r in restores:
                if isinstance(r, BaseException):
                    errors.append(str(r))

        # If you targeted one room and it's grouped, the others heard it too —
        # say so, so a single-room test doesn't look like it misfired.
        notes: list[str] = []
        for snap in live:
            asked = {z.address for z in snap.zones}
            others = [m.player.name for m in snap.members if m.player.address not in asked]
            if others:
                notes.append(f"{snap.label} is grouped with {', '.join(others)}, "
                             f"so {'it' if len(others) == 1 else 'they'} heard the chime too")

        return RingResult(
            status="chimed",
            doorbells=list(self._played),
            groups=[s.label for s in live],
            skipped=skipped,
            errors=errors,
            notes=notes,
        )

    async def _capture(self, primary: BluOSPlayer, members: list[BluOSPlayer],
                       zones: list[ZoneConfig]) -> GroupSnapshot | str:
        """Snapshot a group, or return a human-readable reason for skipping it."""
        """Read playback state once, and each member's OWN volume."""
        status = await primary.status()

        # Honour per-zone preferences using the zones that asked for this group.
        if not status.is_active and not any(z.chime_when_idle for z in zones):
            log.info("%s is idle and no zone wants an idle chime — skipping",
                     primary.name)
            return (f"{' + '.join(z.name for z in zones) or primary.name} "
                    f"(idle, and chime_when_idle is off)")

        async def member_volume(m: BluOSPlayer) -> MemberSnapshot | None:
            try:
                vol = await m.volume()
            except BluOSError as exc:
                log.warning("could not read volume from %s: %s", m.name, exc)
                return None
            zone = self._zone_by_address(m.address)
            chime_volume = (
                zone.chime_volume if zone and zone.chime_volume is not None
                else self.config.chime.default_volume
            )
            return MemberSnapshot(player=m, volume=vol, chime_volume=chime_volume)

        gathered = await asyncio.gather(*(member_volume(m) for m in members))
        member_snaps = [m for m in gathered if m is not None]

        if member_snaps and all(m.volume.mute for m in member_snaps) \
                and not any(z.chime_when_muted for z in zones):
            log.info("%s is muted — skipping", primary.name)
            return (f"{' + '.join(z.name for z in zones) or primary.name} "
                    f"(muted, and chime_when_muted is off)")

        log.info(
            "captured %s: state=%s source=%s members=%s",
            primary.name, status.state, status.source_kind,
            {m.player.name: m.volume.level for m in member_snaps},
        )
        return GroupSnapshot(primary=primary, members=member_snaps,
                             status=status, zones=zones)

    async def _duck(self, snap: GroupSnapshot) -> None:
        await asyncio.gather(
            *(
                self._ramp(m.player, m.volume.level, m.chime_volume)
                for m in snap.members if m.adjustable
            ),
            return_exceptions=True,
        )

    async def _play_all(self, live: list[GroupSnapshot], chime: Chime) -> None:
        """Play one chime on every group at once, then wait for it to finish.

        Repeats and queued chimes go through here too, at the sequence level —
        previously each group looped on a shared flag, so the first group to
        check it cleared it and a repeat only played in one group.
        """
        url = self.config.chime_url(chime.file)

        async def one(snap: GroupSnapshot) -> None:
            try:
                await snap.primary.play_url(url)
            except BluOSError as exc:
                log.error("%s chime failed on %s: %s", chime.doorbell,
                          snap.primary.name, exc)

        await asyncio.gather(*(one(s) for s in live))
        await asyncio.sleep(chime.total_seconds)

    async def _restore(self, snap: GroupSnapshot) -> None:
        age = time.monotonic() - snap.captured_at
        ttl = self.config.behaviour.state_ttl_seconds
        if age > ttl:
            log.error("snapshot for %s is %.0fs old (ttl %.0fs) — restoring volume "
                      "only, not the source", snap.label, age, ttl)
        else:
            await self._restore_source(snap)

        # Volume last, so the source fades back in rather than slamming.
        await asyncio.gather(
            *(
                self._ramp(m.player, m.chime_volume, m.volume.level)
                for m in snap.members if m.adjustable
            ),
            return_exceptions=True,
        )

        # Put mute back if the player was muted before (we never unmute, but a
        # muted player that we ducked should end up where it started).
        log.info("restored %s", snap.label)

    async def _restore_source(self, snap: GroupSnapshot) -> None:
        status = snap.status
        primary = snap.primary

        if not status.is_active and status.state != "pause":
            # It was stopped or idle — the chime leaves it stopped, which is
            # where it started. Nothing to restore.
            await self._safe(primary.stop())
            return

        kind = status.source_kind
        try:
            if kind == "input" and status.input_id:
                await primary.play_input(status.input_id)
            elif kind == "stream" and status.stream_url:
                await primary.play_url(status.stream_url)
            elif kind == "queue" and status.song is not None:
                seek = status.secs if (status.can_seek and status.totlen) else None
                await primary.resume_queue(status.song, seek)
            else:
                log.warning("%s: unknown source kind, falling back to /Play",
                            primary.name)
                await primary.play()
        except BluOSError as exc:
            log.error("could not restore source on %s: %s", primary.name, exc)
            return

        if status.state == "pause" and self.config.behaviour.restore_pause_state:
            await asyncio.sleep(0.5)
            await self._safe(primary.pause())

    # -- helpers --------------------------------------------------------------

    async def _ramp(self, player: BluOSPlayer, start: int, end: int) -> None:
        """Move volume from start to end, optionally in steps for a soft fade."""
        if start == end:
            return

        behaviour = self.config.behaviour
        steps = max(1, behaviour.fade_steps) if behaviour.fade_ms > 0 else 1
        if steps == 1:
            await self._safe(player.set_volume(end))
            return

        delay = (behaviour.fade_ms / 1000.0) / steps
        last = start
        for i in range(1, steps + 1):
            level = round(start + (end - start) * i / steps)
            if level != last:  # don't spam a player with no-op writes
                await self._safe(player.set_volume(level))
                last = level
            if i < steps:
                await asyncio.sleep(delay)
        if last != end:
            await self._safe(player.set_volume(end))

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except BluOSError as exc:
            log.warning("player command failed: %s", exc)
