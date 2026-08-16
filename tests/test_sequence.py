"""End-to-end tests of the doorbell choreography against mock players.

Run: python -m tests.test_sequence
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (BehaviourConfig, ChimeConfig, Config,  # noqa: E402
                        WebhookConfig, ZoneConfig)
from app.orchestrator import DoorbellOrchestrator  # noqa: E402
from tests.mock_player import MockPlayer, group, serve  # noqa: E402

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def make_config(zones: list[ZoneConfig], **behaviour) -> Config:
    return Config(
        service_base_url="http://127.0.0.1:9999",
        zones=zones,
        chime=ChimeConfig(file="doorbell.mp3", duration_seconds=0.05,
                          tail_seconds=0.02, default_volume=30),
        behaviour=BehaviourConfig(debounce_seconds=0.0, fade_ms=0,
                                  http_timeout_seconds=3.0, **behaviour),
        webhook=WebhookConfig(),
    )


async def run(config: Config):
    async with httpx.AsyncClient(timeout=3.0) as client:
        orch = DoorbellOrchestrator(config, client)
        return await orch.ring(source="test"), orch


# --------------------------------------------------------------------------
async def test_standalone_queue():
    print("\n1. Standalone player on a play queue")
    p = MockPlayer(name="Office", port=21001, volume=62, song=7, secs=96)
    serve(p)
    config = make_config([ZoneConfig(name="Office", host="127.0.0.1", port=21001,
                                     chime_volume=25)])
    result, _ = await run(config)

    check("sequence completed", result.status == "chimed", str(result.as_dict()))
    check("volume restored to the ORIGINAL 62", p.volume == 62, f"got {p.volume}")
    check("ducked to the chime volume 25 first",
          25 in [v for v, _ in p.volume_writes], str(p.volume_writes))
    check("every volume write used tell_slaves=0",
          all(ts == "0" for _, ts in p.volume_writes), str(p.volume_writes))

    plays = [params for path, params in p.calls if path == "/Play"]
    chimed = any("url" in q and "doorbell.mp3" in q["url"][0] for q in plays)
    check("chime was played via /Play?url=", chimed)
    resumed = [q for q in plays if "id" in q]
    check("queue resumed at the captured track", bool(resumed)
          and resumed[-1]["id"][0] == "7", str(resumed))
    check("queue resumed at the captured position (seek=96)",
          bool(resumed) and resumed[-1].get("seek", [""])[0] == "96", str(resumed))


# --------------------------------------------------------------------------
async def test_grouped_pair():
    print("\n2. Grouped pair — target is the SECONDARY")
    primary = MockPlayer(name="Kitchen", port=21002, volume=55, song=3, secs=12)
    secondary = MockPlayer(name="Dining", port=21003, volume=28)
    group(primary, secondary)
    serve(primary)
    serve(secondary)

    config = make_config([
        ZoneConfig(name="Dining", host="127.0.0.1", port=21003, chime_volume=30),
    ])
    result, _ = await run(config)

    check("sequence completed", result.status == "chimed", str(result.as_dict()))
    check("Kitchen (primary) volume restored to 55", primary.volume == 55,
          f"got {primary.volume}")
    check("Dining (secondary) volume restored to its OWN 28 — not the primary's 55",
          secondary.volume == 28, f"got {secondary.volume}")

    chimes = [q for path, q in primary.calls
              if path == "/Play" and "url" in q and "doorbell" in q["url"][0]]
    check("exactly one chime sent, to the primary", len(chimes) == 1,
          f"got {len(chimes)}")
    sec_chimes = [q for path, q in secondary.calls
                  if path == "/Play" and "url" in q and "doorbell" in q["url"][0]]
    check("no chime sent directly to the secondary", not sec_chimes)
    check("no volume write used tell_slaves=1",
          all(ts == "0" for _, ts in primary.volume_writes + secondary.volume_writes),
          str(primary.volume_writes + secondary.volume_writes))


# --------------------------------------------------------------------------
async def test_double_press():
    print("\n3. Double press mid-chime does not corrupt the saved volume")
    p = MockPlayer(name="Den", port=21004, volume=71, song=2, secs=5)
    serve(p)
    config = make_config([ZoneConfig(name="Den", host="127.0.0.1", port=21004,
                                     chime_volume=20)])
    config.chime.duration_seconds = 0.35

    async with httpx.AsyncClient(timeout=3.0) as client:
        orch = DoorbellOrchestrator(config, client)
        first = asyncio.create_task(orch.ring(source="press 1"))
        await asyncio.sleep(0.12)
        second = await orch.ring(source="press 2")
        r1 = await first

    check("second press was folded into the running sequence",
          second.status in ("extended", "debounced"), second.status)
    check("first press completed", r1.status == "chimed", str(r1.as_dict()))
    check("volume restored to the ORIGINAL 71, not the ducked 20",
          p.volume == 71, f"got {p.volume}")


# --------------------------------------------------------------------------
async def test_stream_and_idle():
    print("\n4. Internet radio restore, and an idle zone that opts out")
    radio = MockPlayer(name="Patio", port=21005, volume=40, state="stream",
                       stream_url="http://radio.example/stream.mp3", song=None,
                       service="TuneIn", can_seek=False, totlen=None)
    idle = MockPlayer(name="Bedroom", port=21006, volume=33, state="stop")
    serve(radio)
    serve(idle)

    config = make_config([
        ZoneConfig(name="Patio", host="127.0.0.1", port=21005),
        ZoneConfig(name="Bedroom", host="127.0.0.1", port=21006,
                   chime_when_idle=False),
    ])
    result, _ = await run(config)

    check("Patio volume restored to 40", radio.volume == 40, f"got {radio.volume}")
    check("radio stream re-issued after the chime",
          radio.stream_url == "http://radio.example/stream.mp3",
          f"got {radio.stream_url}")
    check("idle Bedroom was skipped entirely",
          not any(path == "/Play" for path, _ in idle.calls),
          str([c[0] for c in idle.calls]))
    check("idle Bedroom volume untouched", idle.volume == 33, f"got {idle.volume}")
    check("Patio appears in the chimed groups", "Patio" in " ".join(result.groups))


# --------------------------------------------------------------------------
async def test_fixed_volume_player():
    print("\n5. Fixed-volume player (line-out at unity) is left alone")
    p = MockPlayer(name="Rack", port=21007, volume=50, fixed_volume=True, song=1)
    serve(p)
    config = make_config([ZoneConfig(name="Rack", host="127.0.0.1", port=21007)])
    await run(config)

    check("no volume writes attempted", not p.volume_writes, str(p.volume_writes))
    check("chime still played",
          any(path == "/Play" and "url" in q for path, q in p.calls))


# --------------------------------------------------------------------------
async def test_unreachable_player():
    print("\n6. One dead player does not stop the others")
    alive = MockPlayer(name="Hall", port=21008, volume=44, song=5, secs=30)
    serve(alive)
    config = make_config([
        ZoneConfig(name="Hall", host="127.0.0.1", port=21008),
        ZoneConfig(name="Ghost", host="127.0.0.1", port=21099),  # nothing listening
    ])
    result, _ = await run(config)

    check("still chimed", result.status == "chimed", str(result.as_dict()))
    check("dead player reported as skipped", any("Ghost" in s for s in result.skipped),
          str(result.skipped))
    check("live player restored to 44", alive.volume == 44, f"got {alive.volume}")


async def main() -> int:
    await test_standalone_queue()
    await test_grouped_pair()
    await test_double_press()
    await test_stream_and_idle()
    await test_fixed_volume_player()
    await test_unreachable_player()

    print("\n" + "=" * 60)
    if failures:
        print(f"\033[31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\033[32mAll checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
