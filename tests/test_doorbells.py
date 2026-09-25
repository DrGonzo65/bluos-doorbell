"""Several doorbells, each with its own sound, ringing the same rooms.

Run: python -m tests.test_doorbells
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import main as appmain  # noqa: E402
from app.config import Config  # noqa: E402
from app.orchestrator import DoorbellOrchestrator  # noqa: E402
from tests.mock_player import MockPlayer, group, serve  # noqa: E402

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
failures: list[str] = []
TOKEN = "d00rbell"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


# Two separate groups: Kitchen on its own, Living Room + Dining together.
KITCHEN = MockPlayer(name="Kitchen", port=27001, volume=50, song=2, secs=40)
LIVING = MockPlayer(name="Living Room", port=27002, volume=44, song=3, secs=5)
DINING = MockPlayer(name="Dining", port=27003, volume=22)
group(LIVING, DINING)
ALL = [KITCHEN, LIVING, DINING]
PRIMARIES = [KITCHEN, LIVING]
for _p in ALL:
    serve(_p)


def make_config(**overrides) -> Config:
    raw = {
        "service_base_url": "http://127.0.0.1:9999",
        "discovery": {"auto": False},
        "webhook": {"token": TOKEN},
        "chime": {"file": "doorbell.mp3", "duration_seconds": 0.3, "tail_seconds": 0.02},
        "doorbells": {"back": {"file": "back-door.mp3", "duration_seconds": 0.3}},
        "behaviour": {"debounce_seconds": 0, "fade_ms": 0},
        "zones": [
            {"name": "Kitchen", "host": "127.0.0.1", "port": 27001},
            {"name": "Living Room", "host": "127.0.0.1", "port": 27002},
            {"name": "Dining", "host": "127.0.0.1", "port": 27003},
        ],
    }
    for key, value in overrides.items():
        raw[key] = value
    return Config.model_validate(raw)


def played(p: MockPlayer) -> list[str]:
    """Chime files this player was told to play, in order."""
    out = []
    for path, q in p.calls:
        if path == "/Play" and "url" in q and "/chimes/" in q["url"][0]:
            out.append(q["url"][0].rsplit("/", 1)[-1])
    return out


def reset() -> None:
    for p in ALL:
        p.calls.clear()
        p.volume_writes.clear()
    KITCHEN.volume, LIVING.volume, DINING.volume = 50, 44, 22
    for p in PRIMARIES:
        p.state, p.stream_url = "play", None
    KITCHEN.song, LIVING.song = 2, 3


def client(cfg: Config) -> TestClient:
    http = httpx.AsyncClient(timeout=3.0)
    appmain.state["config"] = cfg
    appmain.state["client"] = http
    appmain.state["registry"] = None
    appmain.state["orchestrator"] = DoorbellOrchestrator(cfg, http)
    return TestClient(appmain.app)


# --------------------------------------------------------------------------
def test_config():
    print("\n1. Config")
    cfg = make_config()
    check("default doorbell resolves from chime:",
          cfg.chime_for(None).file == "doorbell.mp3"
          and cfg.chime_for("default").file == "doorbell.mp3")
    back = cfg.chime_for("back")
    check("named doorbell resolves to its own file", back and back.file == "back-door.mp3")
    check("names are case-insensitive", cfg.chime_for("BACK") == back)
    check("tail_seconds inherited from chime: when not set",
          back and back.tail_seconds == 0.02, str(back))
    check("unknown doorbell -> None", cfg.chime_for("side") is None)
    check("all_chimes lists default first",
          [c.doorbell for c in cfg.all_chimes()] == ["default", "back"])

    def rejected(doorbells) -> str | None:
        try:
            make_config(doorbells=doorbells)
        except ValidationError as exc:
            return str(exc)
        return None

    err = rejected({"default": {"file": "x.mp3", "duration_seconds": 1}})
    check("'default' is reserved", err is not None and "reserved" in err, str(err)[:120])
    err = rejected({"back door": {"file": "x.mp3", "duration_seconds": 1}})
    check("a name that can't go in a URL is rejected", err is not None and "URL" in err,
          str(err)[:120])
    check("names differing only in case are duplicates",
          rejected({"Back": {"file": "a.mp3", "duration_seconds": 1},
                    "back": {"file": "b.mp3", "duration_seconds": 1}}) is not None)
    err = rejected({"back": {"file": "back-door.mp3"}})
    check("duration_seconds is required per doorbell",
          err is not None and "duration_seconds" in err, str(err)[:120])


def test_routing():
    print("\n2. Each webhook plays its own sound, in the same rooms")
    c = client(make_config())

    reset()
    body = c.post("/doorbell", params={"token": TOKEN}).json()
    check("/doorbell rings the default doorbell", body.get("doorbells") == ["default"],
          str(body))
    check("...playing doorbell.mp3 in every group",
          all(played(p) == ["doorbell.mp3"] for p in PRIMARIES),
          str({p.name: played(p) for p in PRIMARIES}))

    reset()
    body = c.post("/doorbell/back", params={"token": TOKEN}).json()
    check("/doorbell/back rings the back doorbell", body.get("doorbells") == ["back"],
          str(body))
    check("...playing back-door.mp3 in the SAME groups",
          all(played(p) == ["back-door.mp3"] for p in PRIMARIES),
          str({p.name: played(p) for p in PRIMARIES}))
    check("volumes restored afterwards",
          (KITCHEN.volume, LIVING.volume, DINING.volume) == (50, 44, 22),
          str((KITCHEN.volume, LIVING.volume, DINING.volume)))

    reset()
    r = c.get("/doorbell/back", params={"token": TOKEN})
    check("GET works on a named doorbell too", r.status_code == 200 and
          played(KITCHEN) == ["back-door.mp3"])

    reset()
    r = c.post("/doorbell/back")
    check("named doorbell still needs the token", r.status_code == 401)
    check("...and nothing played", not any(played(p) for p in ALL))


def test_unknown_doorbell():
    print("\n3. A mistyped doorbell name")
    c = client(make_config())

    reset()
    r = c.post("/doorbell/bak", params={"token": TOKEN})
    body = r.json()
    check("webhook still rings — a wrong sound beats silence",
          r.status_code == 200 and body.get("status") == "chimed", str(body))
    check("...with the default chime", played(KITCHEN) == ["doorbell.mp3"],
          str(played(KITCHEN)))
    check("...and the response says what happened",
          any("unknown doorbell" in n for n in body.get("notes", [])), str(body))

    reset()
    r = c.get("/test/chime", params={"token": TOKEN, "doorbell": "bak"})
    detail = r.json().get("detail", {})
    check("test endpoint 404s instead, listing real doorbells",
          r.status_code == 404 and detail.get("doorbells") == ["default", "back"],
          str(r.json()))
    check("...and plays nothing", not any(played(p) for p in ALL))


def test_second_doorbell_queued():
    print("\n4. Back pressed while front is still chiming")
    reset()

    async def run():
        async with httpx.AsyncClient(timeout=3.0) as http:
            cfg = make_config()
            o = DoorbellOrchestrator(cfg, http)
            front = asyncio.create_task(o.ring("front", chime=cfg.chime_for(None)))
            await asyncio.sleep(0.15)          # front is mid-chime
            back = await o.ring("back", chime=cfg.chime_for("back"))
            return back, await front

    back, front = asyncio.run(run())
    check("back was queued, not dropped", back.status == "extended", back.status)
    check("one sequence played both, front then back",
          front.doorbells == ["default", "back"], str(front.doorbells))
    for p in PRIMARIES:
        check(f"{p.name}: heard front then back",
              played(p) == ["doorbell.mp3", "back-door.mp3"], str(played(p)))
    check("volumes restored once, to the true originals",
          (KITCHEN.volume, LIVING.volume, DINING.volume) == (50, 44, 22),
          str((KITCHEN.volume, LIVING.volume, DINING.volume)))
    captures = sum(1 for path, _ in KITCHEN.calls if path == "/Status")
    check("a single capture (no second snapshot of ducked volumes)", captures == 1,
          f"{captures} /Status reads")


def test_repeat_reaches_every_group():
    print("\n5. Regression: a repeat press plays in EVERY group")
    reset()

    async def run():
        async with httpx.AsyncClient(timeout=3.0) as http:
            cfg = make_config()
            o = DoorbellOrchestrator(cfg, http)
            first = asyncio.create_task(o.ring("press 1"))
            await asyncio.sleep(0.15)
            await o.ring("press 2")
            return await first

    result = asyncio.run(run())
    check("same doorbell twice -> played twice", result.doorbells == ["default", "default"],
          str(result.doorbells))
    for p in PRIMARIES:
        check(f"{p.name} (separate group) heard both",
              played(p) == ["doorbell.mp3", "doorbell.mp3"], str(played(p)))


def test_debounce_is_per_doorbell():
    print("\n6. Debounce is per doorbell")
    reset()

    async def run():
        async with httpx.AsyncClient(timeout=3.0) as http:
            cfg = make_config(behaviour={"debounce_seconds": 30, "fade_ms": 0})
            o = DoorbellOrchestrator(cfg, http)
            r1 = await o.ring("front", chime=cfg.chime_for(None))
            r2 = await o.ring("back", chime=cfg.chime_for("back"))     # right after
            r3 = await o.ring("front again", chime=cfg.chime_for(None))
            return r1, r2, r3

    r1, r2, r3 = asyncio.run(run())
    check("front rings", r1.status == "chimed", r1.status)
    check("back rings immediately after — NOT swallowed by front's debounce",
          r2.status == "chimed", r2.status)
    check("front again inside its own window is debounced", r3.status == "debounced",
          r3.status)


def test_masher():
    print("\n7. Someone leaning on the button")
    reset()

    async def run():
        async with httpx.AsyncClient(timeout=3.0) as http:
            cfg = make_config()
            o = DoorbellOrchestrator(cfg, http)
            first = asyncio.create_task(o.ring("press 1"))
            await asyncio.sleep(0.12)
            extra = [await o.ring(f"press {i}") for i in range(2, 9)]
            return extra, await first

    extra, result = asyncio.run(run())
    check("seven extra presses -> one extra chime, not seven",
          result.doorbells == ["default", "default"], str(result.doorbells))
    check("the duplicates say they were already queued",
          sum("already queued" in " ".join(r.notes) for r in extra) == 6,
          str([r.notes for r in extra]))


def test_press_before_chime_starts():
    print("\n8. A press before the first chime has started isn't lost")
    reset()

    async def run():
        async with httpx.AsyncClient(timeout=3.0) as http:
            cfg = make_config()
            o = DoorbellOrchestrator(cfg, http)
            front = asyncio.create_task(o.ring("front", chime=cfg.chime_for(None)))
            await asyncio.sleep(0)             # front has started capturing, no sound yet
            back = await o.ring("back", chime=cfg.chime_for("back"))
            return await front, back

    front, back = asyncio.run(run())
    check("front chimed", front.status == "chimed", front.status)
    check("back waited and then ran its own sequence",
          back.status == "chimed" and back.doorbells == ["back"], str(back.as_dict()))
    check("Kitchen heard both, in order",
          played(KITCHEN) == ["doorbell.mp3", "back-door.mp3"], str(played(KITCHEN)))
    check("volumes still correct after two full sequences",
          (KITCHEN.volume, LIVING.volume, DINING.volume) == (50, 44, 22),
          str((KITCHEN.volume, LIVING.volume, DINING.volume)))


def test_single_room_with_doorbell():
    print("\n9. Room test with a specific doorbell's sound")
    reset()
    body = client(make_config()).get("/test/chime", params={
        "token": TOKEN, "zone": "Kitchen", "doorbell": "back"}).json()
    check("plays the back sound", body.get("doorbells") == ["back"], str(body))
    check("only in the Kitchen", played(KITCHEN) == ["back-door.mp3"]
          and not played(LIVING), str({p.name: played(p) for p in PRIMARIES}))


def test_health_lists_doorbells():
    print("\n10. /health (with token) lists doorbells and whether each file exists")
    body = client(make_config()).get("/health", params={"token": TOKEN}).json()
    bells = {d["name"]: d for d in body.get("doorbells", [])}
    check("both listed", set(bells) == {"default", "back"}, str(bells))
    check("file presence reported", all("file_present" in d for d in bells.values()))


def main() -> int:
    test_config()
    test_routing()
    test_unknown_doorbell()
    test_second_doorbell_queued()
    test_repeat_reaches_every_group()
    test_debounce_is_per_doorbell()
    test_masher()
    test_press_before_chime_starts()
    test_single_room_with_doorbell()
    test_health_lists_doorbells()

    print("\n" + "=" * 60)
    if failures:
        print(f"\033[31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\033[32mAll checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
