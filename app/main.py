"""FastAPI service: UniFi Protect webhook in, BluOS doorbell chime out."""

from __future__ import annotations

import asyncio
import hmac
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
    if config.webhook.token in DEFAULT_TOKENS:
        log.warning("=" * 68)
        if not config.webhook.token:
            log.warning("  NO WEBHOOK TOKEN SET.")
            log.warning("  Every endpoint is unauthenticated: anything that can")
            log.warning("  reach this port can ring the doorbell and read which")
            log.warning("  players you have and what they are playing.")
        else:
            log.warning("  WEBHOOK TOKEN IS STILL THE DEFAULT (%r).",
                        config.webhook.token)
            log.warning("  It ships in the public image, so it protects nothing.")
        log.warning("  Set webhook.token in config.yaml to something random:")
        log.warning("      python3 -c \"import secrets; print(secrets.token_urlsafe(24))\"")
        log.warning("  then restart this container.")
        log.warning("=" * 68)

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


#: Tokens shipped in the starter config. Present but useless — anyone who has
#: seen the repo knows them, so treat them as no token at all.
DEFAULT_TOKENS = {"", "change-me", "changeme", "CHANGEME"}


def _token_ok(token: str | None, header_token: str | None) -> bool:
    """True when the caller presented the configured token.

    Constant-time comparison so the endpoint can't be used as an oracle to
    guess the token one character at a time.
    """
    expected = _config().webhook.token
    if not expected:
        return False
    for candidate in (token, header_token):
        if candidate is not None and hmac.compare_digest(candidate, expected):
            return True
    return False


def _check_token(token: str | None, header_token: str | None) -> None:
    """Raise 401 unless the caller is authorised.

    An unset token disables the check entirely — that is a deliberate escape
    hatch for a trusted VLAN, and the service warns loudly about it at startup.
    """
    if not _config().webhook.token:
        return
    if not _token_ok(token, header_token):
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
async def health(
    token: str | None = Query(default=None),
    x_doorbell_token: str | None = Header(default=None),
):
    """Liveness. Stays reachable without a token because the Unraid WebUI link
    and the container healthcheck both hit it — but anonymous callers get only
    a status and a build stamp. Player names, addresses, what is playing and
    the discovery detail require the token."""
    orch = _orchestrator()
    zones = orch.target_zones()

    basic = {
        "status": "ok" if zones else "no-players",
        "build": BUILD,
        "zones": len(zones),
    }

    if not _token_ok(token, x_doorbell_token):
        basic["detail"] = "supply ?token= for player and discovery detail"
        return basic

    registry = state.get("registry")
    return {
        **basic,
        "zone_names": [z.name for z in zones],
        "discovery": registry.status() if registry else None,
        "chime_url": _config().chime_url(),
        "last_result": orch.last_result.as_dict() if orch.last_result else None,
    }


@router.get("/inspect")
async def inspect(
    token: str | None = Query(default=None),
    x_doorbell_token: str | None = Header(default=None),
):
    """Dump what every known player currently reports.

    The diagnostic view: each player's OWN volume alongside its playback
    state, and how the groups resolve. Token required — this exposes player
    names, LAN addresses and the URL of whatever is currently streaming.
    """
    _check_token(token, x_doorbell_token)
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
                               description="CIDR to sweep, e.g. 192.168.1.0/24"),
    token: str | None = Query(default=None),
    x_doorbell_token: str | None = Header(default=None),
):
    """What the service currently knows about players on the network.

    Discovery runs on its own — this endpoint is for looking at the result,
    and for forcing a rescan with ?rescan=1 if you just plugged something in
    and don't want to wait for the next refresh.
    """
    _check_token(token, x_doorbell_token)

    from tools.discover import discover_sweep
    from tools.netutil import SubnetError, primary_ip, resolve

    registry = state.get("registry")
    ip = primary_ip()
    try:
        network, source = resolve(subnet or _config().discovery.subnet)
    except SubnetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if registry and (rescan or not registry.players):
        await registry.refresh(force_sweep=True)

    if registry and registry.config.discovery.auto:
        zones = _orchestrator().target_zones()
        players = [{"name": z.name, "host": z.host} for z in zones]
        status = registry.status()
    else:
        # Discovery is switched off; scan on demand so the endpoint still helps.
        found = await discover_sweep(network)
        players = [{"name": n, "host": h, "detail": d} for h, n, d in found]
        status = None

    yaml_block = ("zones:\n" + "".join(
        f"  - name: {p['name']}\n    host: {p['host']}\n" for p in players
    )) if players else "zones: []"

    hint = None
    if not players:
        hint = (f"Nothing answered on {network}. Check that's the right network "
                f"(this host is {ip}), then set discovery.subnet to the CIDR your "
                f"players are on, or try /discover?rescan=1&subnet=<cidr>")
    elif _config().discovery.auto:
        hint = ("These are in use already — with discovery on you don't need to "
                "paste anything into config.yaml. The zones block below is only "
                "useful if you want to pin or tune them.")

    return {
        "auto_discovery": _config().discovery.auto,
        "subnet": str(network),
        "subnet_source": source,
        "this_host": ip,
        "count": len(players),
        "players": players,
        "discovery": status,
        "hint": hint,
        "yaml": yaml_block,
    }


@router.api_route("/test/chime", methods=["GET", "POST"])
async def test_chime(
    zone: list[str] | None = Query(
        default=None,
        description="Room(s) to test, by name or IP. Repeat for several; "
                    "omit to test every room."),
    token: str | None = Query(default=None),
    x_doorbell_token: str | None = Header(default=None),
):
    """Fire the full sequence by hand, bypassing the debounce window.

    GET works too, so a test is a link you can open in a browser:
        /test/chime?token=...&zone=Kitchen
    """
    _check_token(token, x_doorbell_token)
    orch = _orchestrator()

    only = None
    if zone:
        known = orch.target_zones()
        only, unknown = [], []
        for wanted in zone:
            needle = wanted.strip().lower()
            match = next((z for z in known
                          if z.name.strip().lower() == needle or z.host == wanted.strip()),
                         None)
            if match is None:
                unknown.append(wanted)
            elif match not in only:
                only.append(match)
        if unknown:
            raise HTTPException(status_code=404, detail={
                "error": f"no such room: {', '.join(unknown)}",
                "rooms": sorted(z.name for z in known),
            })

    orch._last_ring = -1e9  # noqa: SLF001 - deliberate test escape hatch
    result = await orch.ring(source="manual test", only=only)
    out = result.as_dict()
    if only:
        out["tested"] = [z.name for z in only]
    return out


app.include_router(router)
