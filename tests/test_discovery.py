"""Tests for auto-discovery and the zone-override merge.

Run: python -m tests.test_discovery
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import Config  # noqa: E402
from app.discovery import PlayerRegistry  # noqa: E402
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


def registry_with(config: Config, discovered: dict[str, str]) -> PlayerRegistry:
    """A registry pre-populated as if discovery had already run."""
    reg = PlayerRegistry(config, httpx.AsyncClient())
    for host, name in discovered.items():
        reg.note(host, name, "PowerNode", "lsdp")
    return reg


DISCOVERED = {
    "192.168.1.51": "Kitchen",
    "192.168.1.52": "Living Room",
    "192.168.1.54": "Primary Bedroom",
}


def test_zero_config_uses_everything():
    print("\n1. Empty config chimes every discovered player")
    reg = registry_with(Config(), DISCOVERED)
    zones = reg.zones()
    check("all three discovered players become zones", len(zones) == 3, str(zones))
    check("names come from the players",
          {z.name for z in zones} == {"Kitchen", "Living Room", "Primary Bedroom"},
          str([z.name for z in zones]))
    check("defaults applied", all(z.chime_when_idle and z.enabled for z in zones))


def test_override_by_name():
    print("\n2. An override tunes one player without listing the rest")
    cfg = Config.model_validate({
        "zones": [{"name": "Primary Bedroom", "chime_when_idle": False,
                   "chime_volume": 20}],
    })
    zones = {z.name: z for z in registry_with(cfg, DISCOVERED).zones()}

    check("still three zones — the override didn't replace the list",
          len(zones) == 3, str(list(zones)))
    bedroom = zones.get("Primary Bedroom")
    check("override applied to the named player",
          bedroom is not None and bedroom.chime_when_idle is False
          and bedroom.chime_volume == 20,
          str(bedroom))
    kitchen = zones.get("Kitchen")
    check("other players keep defaults",
          kitchen is not None and kitchen.chime_when_idle is True
          and kitchen.chime_volume is None, str(kitchen))
    check("override kept the discovered host",
          bedroom is not None and bedroom.host == "192.168.1.54", str(bedroom))


def test_override_by_host_and_disable():
    print("\n3. Matching by IP, and excluding a player")
    cfg = Config.model_validate({
        "zones": [
            {"host": "192.168.1.51", "chime_volume": 45},
            {"name": "Living Room", "enabled": False},
        ],
    })
    zones = {z.name: z for z in registry_with(cfg, DISCOVERED).zones()}

    check("matched by host", zones.get("Kitchen") is not None
          and zones["Kitchen"].chime_volume == 45, str(zones.get("Kitchen")))
    check("enabled:false removes the zone", "Living Room" not in zones,
          str(list(zones)))
    check("the rest survive", len(zones) == 2, str(list(zones)))


def test_exclude_list():
    print("\n4. discovery.exclude keeps a player out entirely")
    cfg = Config.model_validate({"discovery": {"exclude": ["Kitchen",
                                                           "192.168.1.52"]}})
    reg = registry_with(cfg, DISCOVERED)
    names = {z.name for z in reg.zones()}
    check("excluded by name", "Kitchen" not in names, str(names))
    check("excluded by IP", "Living Room" not in names, str(names))
    check("others remain", names == {"Primary Bedroom"}, str(names))
    check("excluded players are never even recorded",
          "192.168.1.51" not in reg.players, str(list(reg.players)))


def test_manual_zone_survives():
    print("\n5. A hand-pinned player discovery can't see is still used")
    cfg = Config.model_validate({
        "zones": [{"name": "Back Deck", "host": "192.168.1.60"}],
    })
    zones = {z.name: z for z in registry_with(cfg, DISCOVERED).zones()}
    check("manual zone present", "Back Deck" in zones, str(list(zones)))
    check("discovered ones still there", len(zones) == 4, str(list(zones)))
    check("no duplicate for a manual zone that IS discovered",
          len({z.host for z in registry_with(
              Config.model_validate({"zones": [{"name": "K", "host": "192.168.1.51"}]}),
              DISCOVERED).zones()}) == 3)


def test_auto_off_uses_config_only():
    print("\n6. discovery.auto=false ignores everything discovered")
    cfg = Config.model_validate({
        "discovery": {"auto": False},
        "zones": [{"name": "Only This", "host": "192.168.1.99"}],
    })
    zones = registry_with(cfg, DISCOVERED).zones()
    check("only the configured zone is used",
          [z.name for z in zones] == ["Only This"], str([z.name for z in zones]))


def test_ring_without_players():
    print("\n7. A ring before anything is discovered fails clearly")
    async def run():
        cfg = Config()
        async with httpx.AsyncClient() as client:
            reg = PlayerRegistry(cfg, client)
            orch = DoorbellOrchestrator(cfg, client, reg)
            return await orch.ring(source="test")
    result = asyncio.run(run())
    check("reports an error rather than crashing", result.status == "error",
          str(result.as_dict()))
    check("message names the real problem",
          any("discovered" in e for e in result.errors), str(result.errors))


def test_end_to_end_auto():
    print("\n8. Full chime sequence driven purely by discovery")
    primary = MockPlayer(name="Kitchen", port=24001, volume=55, song=3, secs=12)
    secondary = MockPlayer(name="Dining", port=24002, volume=28)
    group(primary, secondary)
    serve(primary)
    serve(secondary)

    async def run():
        cfg = Config.model_validate({
            "service_base_url": "http://127.0.0.1:9999",
            "chime": {"duration_seconds": 0.05, "tail_seconds": 0.02},
            "behaviour": {"debounce_seconds": 0, "fade_ms": 0},
        })
        async with httpx.AsyncClient(timeout=3.0) as client:
            reg = PlayerRegistry(cfg, client)
            # Exactly what a real scan would record: host, name, port.
            reg.note("127.0.0.1", "Kitchen", "PowerNode", "lsdp", port=24001)
            orch = DoorbellOrchestrator(cfg, client, reg)
            return orch.target_zones(), await orch.ring(source="test")

    zones, result = asyncio.run(run())
    check("config had no zones; discovery supplied one",
          len(zones) == 1 and zones[0].name == "Kitchen"
          and zones[0].port == 24001, str(zones))
    check("chimed", result.status == "chimed", str(result.as_dict()))
    check("chime went to the group primary once",
          len([1 for path, q in primary.calls
               if path == "/Play" and "url" in q]) == 1,
          str([c[0] for c in primary.calls]))
    check("primary volume restored", primary.volume == 55, str(primary.volume))
    check("secondary restored to its OWN level, discovered via the group",
          secondary.volume == 28, str(secondary.volume))


def main() -> int:
    test_zero_config_uses_everything()
    test_override_by_name()
    test_override_by_host_and_disable()
    test_exclude_list()
    test_manual_zone_survives()
    test_auto_off_uses_config_only()
    test_ring_without_players()
    test_end_to_end_auto()

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
