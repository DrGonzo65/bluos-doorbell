"""Testing one room at a time via /test/chime?zone=...

Run: python -m tests.test_single_room
"""

from __future__ import annotations

import sys
from unittest.mock import patch
from app.bluos import BluOSPlayer, BluOSError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as appmain  # noqa: E402
from app.config import Config  # noqa: E402
from app.orchestrator import DoorbellOrchestrator  # noqa: E402
from tests.mock_player import MockPlayer, group, serve  # noqa: E402

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
failures: list[str] = []
TOKEN = "t0k3n"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def chimes(p: MockPlayer) -> int:
    return sum(1 for path, q in p.calls if path == "/Play" and "url" in q
               and "doorbell" in q["url"][0])


def touched(p: MockPlayer) -> bool:
    """Any state-changing call at all — volume write or transport."""
    return bool(p.volume_writes) or any(
        path in ("/Play", "/Pause", "/Stop") for path, _ in p.calls)


def reset(*players: MockPlayer) -> None:
    for p in players:
        p.calls.clear()
        p.volume_writes.clear()


# Three independent rooms plus a grouped pair and an idle bedroom.
KITCHEN = MockPlayer(name="Kitchen", port=25001, volume=50, song=2, secs=40)
OFFICE = MockPlayer(name="Office", port=25002, volume=35, song=5, secs=10)
PATIO = MockPlayer(name="Patio", port=25003, volume=60, song=1, secs=99)
LIVING = MockPlayer(name="Living Room", port=25004, volume=44, song=3, secs=5)
DINING = MockPlayer(name="Dining", port=25005, volume=22)
BEDROOM = MockPlayer(name="Bedroom", port=25006, volume=18, state="stop")
group(LIVING, DINING)
ALL = [KITCHEN, OFFICE, PATIO, LIVING, DINING, BEDROOM]
for _p in ALL:
    serve(_p)

CONFIG = Config.model_validate({
    "service_base_url": "http://127.0.0.1:9999",
    "discovery": {"auto": False},
    "webhook": {"token": TOKEN},
    "chime": {"duration_seconds": 0.05, "tail_seconds": 0.02},
    "behaviour": {"debounce_seconds": 0, "fade_ms": 0},
    "zones": [
        {"name": "Kitchen", "host": "127.0.0.1", "port": 25001},
        {"name": "Office", "host": "127.0.0.1", "port": 25002},
        {"name": "Patio", "host": "127.0.0.1", "port": 25003},
        {"name": "Living Room", "host": "127.0.0.1", "port": 25004},
        {"name": "Dining", "host": "127.0.0.1", "port": 25005},
        {"name": "Bedroom", "host": "127.0.0.1", "port": 25006,
         "chime_when_idle": False},
    ],
})


def client() -> TestClient:
    http = httpx.AsyncClient(timeout=3.0)
    appmain.state["config"] = CONFIG
    appmain.state["client"] = http
    appmain.state["registry"] = None
    appmain.state["orchestrator"] = DoorbellOrchestrator(CONFIG, http)
    return TestClient(appmain.app)


def test_one_room_only():
    print("\n1. Testing one room leaves every other room alone")
    reset(*ALL)
    r = client().get("/test/chime", params={"token": TOKEN, "zone": "Kitchen"})
    body = r.json()
    check("GET works (clickable from a browser)", r.status_code == 200, str(r.status_code))
    check("status chimed", body.get("status") == "chimed", str(body))
    check("response says which room was tested", body.get("tested") == ["Kitchen"],
          str(body.get("tested")))
    check("Kitchen chimed exactly once", chimes(KITCHEN) == 1, str(chimes(KITCHEN)))
    check("Kitchen volume restored", KITCHEN.volume == 50, str(KITCHEN.volume))
    for other in (OFFICE, PATIO, LIVING, DINING, BEDROOM):
        check(f"{other.name} untouched — no chime, no volume change",
              not touched(other),
              f"writes={other.volume_writes} calls={[c[0] for c in other.calls]}")


def test_matching():
    print("\n2. Room names are case-insensitive; several can be tested at once")
    reset(*ALL)
    r = client().post("/test/chime", params={"token": TOKEN, "zone": "office"})
    check("case-insensitive name", r.status_code == 200 and chimes(OFFICE) == 1,
          str(r.json()))

    reset(*ALL)
    r = client().post("/test/chime", params=[("token", TOKEN), ("zone", "Patio"),
                                             ("zone", "Kitchen")])
    body = r.json()
    check("several rooms in one test",
          chimes(PATIO) == 1 and chimes(KITCHEN) == 1 and not touched(OFFICE),
          str(body))
    check("both reported as tested", sorted(body.get("tested", [])) == ["Kitchen", "Patio"],
          str(body.get("tested")))


def test_unknown_room():
    print("\n3. A typo gets a helpful 404, and nothing plays")
    reset(*ALL)
    r = client().get("/test/chime", params={"token": TOKEN, "zone": "Kitchn"})
    detail = r.json().get("detail", {})
    check("404", r.status_code == 404, str(r.status_code))
    check("names the room it couldn't find", "Kitchn" in str(detail.get("error")), str(detail))
    check("lists the rooms that do exist", "Kitchen" in detail.get("rooms", []), str(detail))
    check("nothing played", not any(touched(p) for p in ALL))


def test_grouped_room():
    print("\n4. Testing a grouped room says the rest of the group heard it")
    reset(*ALL)
    body = client().get("/test/chime", params={"token": TOKEN, "zone": "Dining"}).json()
    check("chimed", body.get("status") == "chimed", str(body))
    check("chime routed through the group primary, once",
          chimes(LIVING) == 1 and chimes(DINING) == 0,
          f"living={chimes(LIVING)} dining={chimes(DINING)}")
    notes = " ".join(body.get("notes", []))
    check("note explains Living Room heard it too",
          "Living Room" in notes and "grouped" in notes, str(body.get("notes")))
    check("both volumes restored to their own levels",
          LIVING.volume == 44 and DINING.volume == 22,
          f"living={LIVING.volume} dining={DINING.volume}")
    check("rooms outside the group untouched", not touched(KITCHEN) and not touched(OFFICE))


def test_skip_reason_visible():
    print("\n5. If a room is skipped, the response says why")
    reset(*ALL)
    body = client().get("/test/chime", params={"token": TOKEN, "zone": "Bedroom"}).json()
    check("nothing chimed", chimes(BEDROOM) == 0, str(body))
    skipped = " ".join(body.get("skipped", []))
    check("skip reason names the room and the setting",
          "Bedroom" in skipped and "chime_when_idle" in skipped, str(body))


def test_full_ring_has_no_group_noise():
    print("\n6. A normal all-rooms ring doesn't emit grouping notes")
    reset(*ALL)
    body = client().get("/test/chime", params={"token": TOKEN}).json()
    check("no zone param -> every playing room chimes",
          all(chimes(p) == 1 for p in (KITCHEN, OFFICE, PATIO, LIVING)), str(body))
    check("no 'heard it too' notes when the whole group was targeted",
          body.get("notes") == [], str(body.get("notes")))
    check("no 'tested' key on an all-rooms ring", "tested" not in body)


def test_still_needs_token():
    print("\n7. Still requires the token")
    reset(*ALL)
    r = client().get("/test/chime", params={"zone": "Kitchen"})
    check("401 without token", r.status_code == 401, str(r.status_code))
    check("nothing played", not any(touched(p) for p in ALL))


def test_playback_failure_visible():
    print("\n8. Failed playback is reported and volume is restored")
    reset(*ALL)
    async def fail_play(self, url):
        raise BluOSError(f"{self.name}: /Play failed: unavailable")
    with patch.object(BluOSPlayer, "play_url", fail_play):
        body = client().post("/test/chime", params={"token": TOKEN, "zone": "Kitchen"}).json()
    check("failed command is not reported as chimed", body.get("status") == "error", str(body))
    check("playback error reaches browser", any("/Play failed" in e for e in body.get("errors", [])), str(body))
    check("volume restored after playback error", KITCHEN.volume == 50, str(KITCHEN.volume))


def test_default_volume_changes():
    print("\n9. Saved default volume applies on the next chime unless overridden")
    original_default = CONFIG.chime.default_volume
    original_override = CONFIG.zones[0].chime_volume
    try:
        CONFIG.zones[0].chime_volume = None
        for level in (20, 45, 0):
            reset(*ALL)
            CONFIG.chime.default_volume = level
            body = client().post("/test/chime", params={"token": TOKEN, "zone": "Kitchen"}).json()
            check(f"default {level} applied before restoring volume",
                  body.get("status") == "chimed" and KITCHEN.volume_writes == [(level, "0"), (50, "0")],
                  str(KITCHEN.volume_writes))
        reset(*ALL)
        CONFIG.zones[0].chime_volume = 25
        CONFIG.chime.default_volume = 40
        client().post("/test/chime", params={"token": TOKEN, "zone": "Kitchen"})
        check("room override takes precedence over default",
              KITCHEN.volume_writes == [(25, "0"), (50, "0")], str(KITCHEN.volume_writes))
    finally:
        CONFIG.chime.default_volume = original_default
        CONFIG.zones[0].chime_volume = original_override


def main() -> int:
    test_one_room_only()
    test_matching()
    test_unknown_room()
    test_grouped_room()
    test_skip_reason_visible()
    test_full_ring_has_no_group_noise()
    test_still_needs_token()
    test_playback_failure_visible()
    test_default_volume_changes()

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
