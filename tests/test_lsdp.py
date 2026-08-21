"""Round-trip tests for the LSDP parser.

Run: python -m tests.test_lsdp
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import lsdp  # noqa: E402

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def test_query_matches_spec():
    print("\n1. Query packet matches the v1.7 wire format")
    q = lsdp.build_query()
    check("header is len=6, 'LSDP', version 1",
          q[:6] == bytes([6]) + b"LSDP" + bytes([1]), q[:6].hex())
    check("message is len=5, 'Q', count=1, class 0xFFFF",
          q[6:] == bytes([5, ord("Q"), 1, 0xFF, 0xFF]), q[6:].hex())
    check("targeted query encodes each class",
          lsdp.build_query((1, 3))[6:] == bytes([7, ord("Q"), 2, 0, 1, 0, 3]),
          lsdp.build_query((1, 3))[6:].hex())


def test_announce_round_trip():
    print("\n2. Announce packets parse back to what we encoded")
    packet = lsdp.build_announce(
        "192.168.1.51",
        bytes.fromhex("001122334455"),
        {lsdp.CLASS_PLAYER: {"name": "Kitchen", "model": "PowerNode"}},
    )
    found = lsdp.parse(packet)
    check("one announcement parsed", len(found) == 1, str(found))
    if found:
        a = found[0]
        check("address recovered", a.address == "192.168.1.51", a.address)
        check("node id recovered", a.node_id == "001122334455", a.node_id)
        check("name from TXT", a.name == "Kitchen", a.name)
        check("model from TXT", a.model == "PowerNode", a.model)
        check("recognised as a player", a.is_player)


def test_multi_class_and_non_players():
    print("\n3. Class filtering")
    hub = lsdp.build_announce("192.168.1.9", b"\xaa\xbb",
                              {lsdp.CLASS_HUB: {"name": "Hub"}})
    a = lsdp.parse(hub)[0]
    check("hub is not treated as a player", not a.is_player)

    secondary = lsdp.build_announce("192.168.1.52", b"\xcc\xdd",
                                    {lsdp.CLASS_SECONDARY: {"name": "Dining"}})
    check("secondary zone counts as a player", lsdp.parse(secondary)[0].is_player)

    multi = lsdp.build_announce("192.168.1.53", b"\xee\xff", {
        lsdp.CLASS_PLAYER: {"name": "Office"},
        lsdp.CLASS_SERVER: {"name": "Office"},
    })
    parsed = lsdp.parse(multi)[0]
    check("multiple classes on one node", len(parsed.classes) == 2,
          str(parsed.classes))


def test_malformed_never_raises():
    print("\n4. Junk on the wire is ignored, never fatal")
    cases = {
        "empty": b"",
        "not LSDP": b"\x06XXXX\x01",
        "truncated header": b"\x06LS",
        "truncated body": bytes([6]) + b"LSDP" + bytes([1, 20, ord("A"), 6, 1, 2]),
        "bogus lengths": bytes([6]) + b"LSDP" + bytes([1, 255, ord("A"), 255]),
        "random bytes": bytes(range(40)),
        "a query, not an announce": lsdp.build_query(),
    }
    for label, data in cases.items():
        try:
            result = lsdp.parse(data)
            check(f"{label} -> [] without raising", result == [], str(result))
        except Exception as exc:  # noqa: BLE001
            check(f"{label} -> [] without raising", False, f"raised {exc!r}")


def test_source_ip_fallback():
    print("\n5. Odd address lengths fall back to the sender's IP")
    body = struct.pack("!B", 2) + b"\x01\x02"
    body += struct.pack("!B", 16) + bytes(16)      # IPv6-length address
    body += struct.pack("!B", 1)
    body += struct.pack("!HB", lsdp.CLASS_PLAYER, 0)
    msg = struct.pack("!BB", len(body) + 2, ord("A")) + body
    packet = struct.pack("!B4sB", 6, lsdp.MAGIC, lsdp.VERSION) + msg

    found = lsdp.parse(packet, source_ip="192.168.1.77")
    check("uses the source IP when the address field isn't IPv4",
          len(found) == 1 and found[0].address == "192.168.1.77", str(found))


def main() -> int:
    test_query_matches_spec()
    test_announce_round_trip()
    test_multi_class_and_non_players()
    test_malformed_never_raises()
    test_source_ip_fallback()

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
