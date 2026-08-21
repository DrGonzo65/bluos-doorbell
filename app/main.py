"""FastAPI service: UniFi Protect webhook in, BluOS doorbell chime out."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .bluos import BluOSError, BluOSPlayer
from .config import Config, load_config
from .discovery import PlayerRegistry
from .orchestrator import DoorbellOrchestrator

log = logging.getLogger("doorbell")

CHIME_DIR = Path(os.environ.get("DOORBELL_CHIME_DIR", "/chimes"))

#: Stamped into the image by CI. Lets you confirm which build is actually
#: running after an update, rather than trusting that the pull took effect.
BUILD = {
    "git_sha": os.environ.get("DOORBELL_GIT_SHA", "dev"),
    "built_at": os.environ.get("DOORBELL_BUILD_TIME", "unknown"),
}

state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: Config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    client = httpx.AsyncClient(timeout=config.behaviour.http_timeout_seconds)
    registry = PlayerRegistry(config, client)
    state["config"] = config
    state["client"] = client
    state["registry"] = registry
    state["orchestrator"] = DoorbellOrchestrator(config, client, registry)

    chime_path = CHIME_DIR / config.chime.file
    if not chime_path.exists():
        log.warning("chime file %s not found — the chime will fail until it exists",
                    chime_path)

    log.info("BluOS doorbell service ready (build %s, %s)",
             BUILD["git_sha"][:12], BUILD["built_at"])
    if config.discovery.auto:
        log.info("  discovery: on — finding players automatically")
    else:
        log.info("  discovery: off — using the %d configured zone(s)",
                 len(config.enabled_zones()))
    log.info("  chime url: %s", config.chime_url())
    log.info("  webhook:   %s/doorbell", config.resolved_base_url())
    if not config.webhook.token:
        log.warning("  no webhook token set — any host that can reach this port "
                    "can ring the doorbell")

    # Find the players before we start answering rings.
    try:
        await registry.start()
    except Exception as exc:  # noqa: BLE001 - never block startup on discovery
        log.warning("discovery failed to start: %s", exc)

    zones = _orchestrator().target_zones()
    if zones:
        log.info("  zones:     %s", ", ".join(z.name for z in zones))
    else:
        log.warning("  NO PLAYERS FOUND YET — discovery keeps trying in the "
                    "background. Check GET /discover, or list them under "
                    "zones: in config.yaml.")

    try:
        yield
    finally:
        await registry.stop()
        await client.aclose()


app = FastAPI(title="BluOS Doorbell", version="1.0.0", lifespan=lifespan)
router = APIRouter()

if CHIME_DIR.exists():
    app.mount("/chimes", StaticFiles(directory=str(CHIME_DIR)), name="chimes")


def _config() -> Config:
    return state["config"]


def _orchestrator() -> DoorbellOrchestrator:
    return state["orchestrator"]


def _check_token(token: str | None, header_token: str | None) -> None:
    expected = _config().webhook.token
    if not expected:
        return
    if token != expected and header_token != expected:
        raise HTTPException(status_code=401, detail="bad or missing token")


def _device_allowed(payload: dict[str, Any]) -> bool:
    """Filter Protect payloads down to the doorbell(s) we care about."""
    allowed = _config().webhook.allowed_devices
    if not allowed:
        return True

    # Protect's Alarm Manager payload shape varies by version, so look in the
    # obvious places rather than assuming one schema.
    haystack: list[str] = []
    for key in ("device", "deviceId", "deviceName", "name", "id", "source"):
        value = payload.get(key)
        if isinstance(value, str):
            haystack.append(value)
        elif isinstance(value, dict):
            haystack.extend(str(v) for v in value.values() if isinstance(v, (str, int)))

    triggers = payload.get("triggers")
    if isinstance(triggers, list):
        for trigger in triggers:
            if isinstance(trigger, dict):
                haystack.extend(
                    str(trigger.get(k)) for k in ("device", "deviceId", "key", "eventId")
                    if trigger.get(k) is not None
                )

    joined = " ".join(haystack).lower()
    return any(a.lower() in joined for a in allowed)


@router.api_route("/doorbell", methods=["GET", "POST"])
async def doorbell(
    request: Request,
    token: str | None = Query(default=None),
    x_doorbell_token: str | None = Header(default=None),
):
    """Webhook target for UniFi Protect Alarm Manager (or anything else)."""
    _check_token(token, x_doorbell_token)

    payload: dict[str, Any] = {}
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                payload = body
        except Exception:  # noqa: BLE001 - Protect sometimes posts an empty body
            payload = {}

    if payload and not _device_allowed(payload):
        log.info("ignoring webhook from non-allowlisted device: %s",
                 payload.get("deviceName") or payload.get("device") or "unknown")
        return JSONResponse({"status": "ignored", "reason": "device not allowlisted"})

    source = payload.get("deviceName") or payload.get("alarm") or request.client.host \
        if request.client else "webhook"
    result = await _orchestrator().ring(source=str(source))
    return JSONResponse(result.as_dict())


@router.get("/health")
async def health():
    orch = _orchestrator()
    zones = orch.target_zones()
    registry = state.get("registry")
    return {
        "status": "ok" if zones else "no-players",
        "build": BUILD,
        "zones": len(zones),
        "zone_names": [z.name for z in zones],
        "discovery": registry.status() if registry else None,
        "chime_url": _config().chime_url(),
        "last_result": orch.last_result.as_dict() if orch.last_result else None,
    }


@router.get("/inspect")
async def inspect():
    """Dump what every configured player currently reports.

    This is the diagnostic view: it shows each player's OWN volume alongside
    the playback state, and how the groups resolve. Use it to sanity-check
    topology and to see what a restore would target.
    """
    config = _config()
    client = state["client"]
    out: list[dict[str, Any]] = []

    for zone in _orchestrator().target_zones():
        player = BluOSPlayer(zone.host, zone.port, name=zone.name, client=client,
                             timeout=config.behaviour.http_timeout_seconds)
        entry: dict[str, Any] = {"name": zone.name, "address": zone.address,
                                 "enabled": zone.enabled}
        try:
            sync = await player.sync_status()
            status = await player.status()
            volume = await player.volume()
            entry.update(
                reachable=True,
                player_name=sync.name,
                group=sync.group,
                role="secondary" if sync.is_secondary
                else ("primary" if sync.is_primary_of_group else "standalone"),
                master=sync.master,
                slaves=sync.slaves,
                own_volume=volume.level,
                muted=volume.mute,
                fixed_volume=volume.is_fixed,
                state=status.state,
                service=status.service,
                source_kind=status.source_kind,
                stream_url=status.stream_url,
                queue_song=status.song,
                queue_pid=status.pid,
                elapsed_secs=status.secs,
                can_seek=status.can_seek,
                restore_plan=_describe_restore(status),
            )
        except BluOSError as exc:
            entry.update(reachable=False, error=str(exc))
        out.append(entry)

    targets, skipped = await _orchestrator().resolve_groups()
    registry = state.get("registry")
    return {
        "discovery": registry.status() if registry else None,
        "players": out,
        "chime_targets": [
            {
                "primary": primary.name,
                "members": [m.name for m in members],
                "requested_by": [z.name for z in zones],
            }
            for primary, members, zones in targets
        ],
        "skipped": skipped,
    }


def _describe_restore(status) -> str:
    kind = status.source_kind
    if not status.is_active and status.state != "pause":
        return "nothing (player is stopped)"
    if kind == "input":
        return f"/Play?inputTypeIndex={status.input_id}"
    if kind == "stream":
        return f"/Play?url={status.stream_url}"
    if kind == "queue":
        seek = f"&seek={status.secs}" if (status.can_seek and status.totlen) else ""
        return f"/Play?id={status.song}{seek}"
    return "unknown — check /inspect output against the API docs"


@router.get("/discover")
async def discover(
    rescan: bool = Query(default=False,
                         description="force a fresh scan instead of the cache"),
    subnet: str | None = Query(default=None,
                               description="first three octets, e.g. 192.168.1"),
    token: str | None = Query(default=None),
    x_doorbell_token: str | None = Header(default=None),
):
    """What the service currently knows about players on the network.

    Discovery runs on its own — this endpoint is for looking at the result,
    and for forcing a rescan with ?rescan=1 if you just plugged something in
    and don't want to wait for the next refresh.
    """
    _check_token(token, x_doorbell_token)

    from tools.discover import discover_sweep, primary_ip

    registry = state.get("registry")
    ip = primary_ip()
    resolved = subnet or _config().discovery.subnet or ".".join(ip.split(".")[:3])

    if registry and (rescan or not registry.players):
        await registry.refresh(force_sweep=True)

    if registry and registry.config.discovery.auto:
        zones = _orchestrator().target_zones()
        players = [{"name": z.name, "host": z.host} for z in zones]
        status = registry.status()
    else:
        # Discovery is switched off; scan on demand so the endpoint still helps.
        found = await discover_sweep(resolved)
        players = [{"name": n, "host": h, "detail": d} for h, n, d in found]
        status = None

    yaml_block = ("zones:\n" + "".join(
        f"  - name: {p['name']}\n    host: {p['host']}\n" for p in players
    )) if players else "zones: []"

    hint = None
    if not players:
        hint = (f"Nothing answered on {resolved}.0/24. Check the subnet is right "
                f"(this host is {ip}) and retry with /discover?rescan=1&subnet=x.y.z")
    elif _config().discovery.auto:
        hint = ("These are in use already — with discovery on you don't need to "
                "paste anything into config.yaml. The zones block below is only "
                "useful if you want to pin or tune them.")

    return {
        "auto_discovery": _config().discovery.auto,
        "subnet": f"{resolved}.0/24",
        "this_host": ip,
        "count": len(players),
        "players": players,
        "discovery": status,
        "hint": hint,
        "yaml": yaml_block,
    }


@router.post("/test/chime")
async def test_chime(token: str | None = Query(default=None),
                     x_doorbell_token: str | None = Header(default=None)):
    """Fire the full sequence manually, bypassing the debounce window."""
    _check_token(token, x_doorbell_token)
    orch = _orchestrator()
    orch._last_ring = -1e9  # noqa: SLF001 - deliberate test escape hatch
    result = await orch.ring(source="manual test")
    return result.as_dict()


app.include_router(router)
