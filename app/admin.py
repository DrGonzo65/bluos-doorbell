"""Browser administration, durable preferences, and measured MP3 uploads."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from mutagen.mp3 import MP3
from pydantic import ValidationError

from .config import Config, settings_path

router = APIRouter()
WEB = Path(__file__).with_name("web")
MAX_UPLOAD = 12 * 1024 * 1024


def runtime():
    from . import main
    return main


def authorize(request: Request):
    main = runtime()
    main._check_token(None, request.headers.get("X-Doorbell-Token"))
    if request.method != "GET" and request.headers.get("X-Doorbell-Admin") != "1":
        raise HTTPException(403, "Use the settings interface to make changes")
    return main


def revision(config: Config) -> str:
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


def audio_path(filename: str) -> Path:
    root = runtime().CHIME_DIR.resolve()
    path = root / filename
    if (not filename or Path(filename).name != filename or "\\" in filename
            or path.is_symlink() or path.resolve().parent != root):
        raise HTTPException(422, "Choose an MP3 from the chime library")
    return path


def measure(path: Path) -> float:
    try:
        info = MP3(path).info
        seconds = float(info.length)
        if info.layer != 3 or not math.isfinite(seconds) or not 0 < seconds <= 60:
            raise ValueError("length")
        return round(seconds, 3)
    except Exception as exc:
        raise HTTPException(422, "Use a valid MP3 between 0 and 60 seconds long") from exc


def library() -> list[dict]:
    files = []
    for path in sorted(runtime().CHIME_DIR.glob("*.mp3")):
        if path.is_symlink() or not path.is_file():
            continue
        item = {"file": path.name, "bytes": path.stat().st_size}
        try:
            item["duration_seconds"] = measure(path)
        except HTTPException:
            item["error"] = "Cannot read this MP3 (maximum duration: 60 seconds)"
        files.append(item)
    return files


def atomic_save(config: Config) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config.model_dump(mode="json"), handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def snapshot() -> dict:
    main = runtime()
    cfg = main._config()
    public = cfg.model_dump(mode="json")
    public["webhook"].pop("token")
    # Addresses belong to the registry, not to editable settings.
    public.pop("zones")
    registry = main.state.get("registry")
    rooms = []
    seen = set()
    if registry:
        for player in sorted(registry.players.values(), key=lambda p: p.name.lower()):
            zone = registry.room_zone(player)
            seen.add(player.key)
            rooms.append({"id": player.key, "name": player.name, "host": player.host,
                          "detail": player.detail, "available": True,
                          "last_seen_seconds": round(player.age_seconds),
                          **{k: getattr(zone, k) for k in
                             ("enabled", "chime_volume", "chime_when_idle", "chime_when_muted")}})
    for key, pref in cfg.room_preferences.items():
        if key not in seen and not any(key == "name:" + r["name"].strip().casefold() for r in rooms):
            rooms.append({"id": key, **pref.model_dump(), "available": False, "host": ""})
    return {"settings": public, "rooms": rooms, "audio": library(),
            "revision": revision(cfg), "base_url": cfg.resolved_base_url(),
            "token_warning": cfg.webhook.token in main.DEFAULT_TOKENS,
            "discovery": registry.status() if registry else None,
            "active": main._orchestrator()._active,
            "last_result": main._orchestrator().last_result.as_dict()
            if main._orchestrator().last_result else None,
            "restart_required": (cfg.listen_host, cfg.listen_port) !=
            main.state.get("bound_address", (cfg.listen_host, cfg.listen_port)),
            "build": main.BUILD}


@router.get("/", include_in_schema=False)
async def interface():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/ui/{asset}", include_in_schema=False)
async def asset(asset: str):
    if asset not in ("app.js", "style.css"):
        raise HTTPException(404)
    return FileResponse(WEB / asset, headers={"Cache-Control": "no-cache"})


@router.get("/api/settings")
async def read_settings(request: Request):
    authorize(request)
    return snapshot()


@router.put("/api/settings")
async def save_settings(request: Request):
    main = authorize(request)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 128 * 1024:
            raise HTTPException(413, "Settings are too large")
    try:
        body = json.loads(raw)
        proposed = body["settings"]
        if not isinstance(proposed, dict):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "Expected a settings object") from None
    orch = main._orchestrator()
    async with orch._gate:
        if orch._active:
            raise HTTPException(409, "A chime is playing. Save again when it finishes.")
        # Recheck after acquiring the gate: another administrator may have changed the token.
        authorize(request)
        old = main._config()
        if body.get("revision") != revision(old):
            raise HTTPException(409, "Settings changed in another window. Reload before saving.")
        try:
            merged = old.model_dump(mode="json")
            merged.update(proposed)
            merged["zones"] = []
            merged["discovery"]["auto"] = True
            merged["discovery"]["exclude"] = []
            merged["webhook"]["token"] = body.get("new_token", old.webhook.token)
            candidate = Config.model_validate(merged)
            # Always derive timings server-side, even if a client submits a duration.
            for chime in candidate.all_chimes():
                duration = measure(audio_path(chime.file))
                if chime.doorbell == "default":
                    candidate.chime.duration_seconds = duration
                else:
                    candidate.doorbells[chime.doorbell].duration_seconds = duration
        except ValidationError as exc:
            errors = [".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()]
            raise HTTPException(422, "; ".join(errors)) from None
        except (TypeError, KeyError):
            raise HTTPException(422, "Incomplete settings") from None
        try:
            atomic_save(candidate)
        except OSError:
            logging.getLogger(__name__).exception("could not save settings")
            raise HTTPException(500, "Could not save. Check that the config folder is writable.") from None
        registry = main.state.get("registry")
        restart_discovery = registry and old.discovery != candidate.discovery
        if restart_discovery:
            await registry.stop()
        main.state["config"] = candidate
        orch.config = candidate
        if registry:
            registry.config = candidate
            if restart_discovery:
                await registry.start()
        logging.getLogger().setLevel(candidate.log_level)
    return snapshot()


@router.post("/api/discovery/refresh")
async def rescan(request: Request):
    main = authorize(request)
    registry = main.state.get("registry")
    if registry:
        await registry.refresh(force_sweep=True)
    return snapshot()


@router.post("/api/chimes")
async def upload(request: Request):
    main = authorize(request)
    filename = request.query_params.get("filename", "")
    if not filename.lower().endswith(".mp3"):
        raise HTTPException(422, "Choose an MP3 file")
    root = main.CHIME_DIR
    root.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".upload-", dir=root)
    try:
        size = 0
        with os.fdopen(fd, "wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "MP3 uploads are limited to 12 MB")
                handle.write(chunk)
        duration = await asyncio.to_thread(measure, Path(temp))
        # Immutable, unique names prevent an upload from replacing a playing chime.
        stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(filename).stem).strip("-")[:60] or "chime"
        name = f"{stem}-{uuid.uuid4().hex[:10]}.mp3"
        os.replace(temp, root / name)
        return {"file": name, "duration_seconds": duration, "bytes": size}
    finally:
        Path(temp).unlink(missing_ok=True)


@router.delete("/api/chimes/{filename}")
async def delete_audio(filename: str, request: Request):
    main = authorize(request)
    async with main._orchestrator()._gate:
        authorize(request)
        if main._orchestrator()._active:
            raise HTTPException(409, "Wait until the chime finishes")
        if any(c.file == filename for c in main._config().all_chimes()):
            raise HTTPException(409, "This sound is assigned to a doorbell. Choose another sound and save first.")
        path = audio_path(filename)
        if path.suffix != ".mp3" or not path.is_file():
            raise HTTPException(404, "Sound not found")
        path.unlink()
    return {"status": "deleted"}
