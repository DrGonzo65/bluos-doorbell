"""discovery.subnet as a real CIDR.

Run: python -m tests.test_subnet
"""

from __future__ import annotations

import asyncio
import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from app.config import Config, DiscoveryConfig  # noqa: E402
from tools import netutil  # noqa: E402
from tools.discover import broadcast_targets, discover_sweep  # noqa: E402
from tools.netutil import (SubnetError, check_sweepable, parse_subnet,  # noqa: E402
                           resolve, sweep_hosts)
from tests.mock_player import MockPlayer, serve  # noqa: E402

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def rejects(value: str) -> str | None:
    try:
        parse_subnet(value)
        check_sweepable(parse_subnet(value))
    except SubnetError as exc:
        return str(exc)
    return None


def test_parsing():
    print("\n1. CIDR parsing")
    check("plain /24", str(parse_subnet("192.168.1.0/24")) == "192.168.1.0/24")
    check("/22 kept as /22", str(parse_subnet("10.0.4.0/22")) == "10.0.4.0/22")
    check("host bits normalised (192.168.1.37/24 -> 192.168.1.0/24)",
          str(parse_subnet("192.168.1.37/24")) == "192.168.1.0/24")
    check("whitespace tolerated", str(parse_subnet("  192.168.1.0/24 ")) == "192.168.1.0/24")
    check("legacy three octets still read as /24",
          str(parse_subnet("192.168.1")) == "192.168.1.0/24")


def test_rejections():
    print("\n2. Bad values fail loudly, with a useful message")
    err = rejects("192.168.1.5")
    check("bare address (no prefix) rejected — not silently a /32",
          err is not None and "prefix" in err, str(err))
    check("...and the message suggests the fix",
          err is not None and "192.168.1.5/24" in err, str(err))
    check("garbage rejected", rejects("not-a-network") is not None)
    check("octet out of range rejected", rejects("300.1.1.0/24") is not None)
    check("IPv6 rejected", "IPv6" in (rejects("fe80::/64") or ""))
    err = rejects("10.0.0.0/16")
    check("/16 refused as too large to sweep", err is not None and "/20" in err, str(err))
    check("/20 is the largest allowed", rejects("10.0.0.0/20") is None)
    check("/19 is refused", rejects("10.0.0.0/19") is not None)


def test_host_counts():
    print("\n3. Exactly the right addresses get probed")
    n = lambda c: len(sweep_hosts(ipaddress.ip_network(c)))  # noqa: E731
    check("/24 -> 254 (no network or broadcast)", n("192.168.1.0/24") == 254, str(n("192.168.1.0/24")))
    check("/23 -> 510", n("192.168.0.0/23") == 510, str(n("192.168.0.0/23")))
    check("/22 -> 1022", n("10.0.4.0/22") == 1022, str(n("10.0.4.0/22")))
    check("/30 -> 2", n("10.0.0.0/30") == 2)
    check("/31 point-to-point -> both addresses", n("10.0.0.0/31") == 2)
    check("/32 -> the single host", n("10.0.0.5/32") == 1)
    hosts = sweep_hosts(ipaddress.ip_network("10.0.4.0/22"))
    check("a /22 spans all four /24s",
          hosts[0] == "10.0.4.1" and hosts[-1] == "10.0.7.254"
          and "10.0.5.255" in hosts and "10.0.6.0" in hosts,
          f"{hosts[0]}..{hosts[-1]}")


def test_broadcast():
    print("\n4. LSDP broadcasts to the network's real directed broadcast")
    check("/24 -> x.x.x.255",
          broadcast_targets(ipaddress.ip_network("192.168.1.0/24"))
          == ["255.255.255.255", "192.168.1.255"])
    check("/22 -> 10.0.7.255, not 10.0.4.255",
          broadcast_targets(ipaddress.ip_network("10.0.4.0/22"))
          == ["255.255.255.255", "10.0.7.255"],
          str(broadcast_targets(ipaddress.ip_network("10.0.4.0/22"))))


def test_config_validation():
    print("\n5. config.yaml is validated at load time")
    check("blank stays blank (means: use this host's subnet)",
          DiscoveryConfig(subnet="").subnet == "")
    check("normalised on load",
          DiscoveryConfig(subnet="192.168.1.37/24").subnet == "192.168.1.0/24")
    check("legacy value from an existing config.yaml upgraded to CIDR",
          DiscoveryConfig(subnet="192.168.1").subnet == "192.168.1.0/24")
    for bad in ("192.168.1.5", "10.0.0.0/16", "banana"):
        try:
            Config.model_validate({"discovery": {"subnet": bad}})
            check(f"{bad!r} rejected at startup", False, "was accepted")
        except ValidationError as exc:
            check(f"{bad!r} rejected at startup, naming the field",
                  "discovery.subnet" in str(exc), str(exc)[:160])


def test_auto_derivation():
    print("\n6. Auto-derivation")
    original = netutil.primary_ip
    try:
        netutil.primary_ip = lambda: "127.0.0.1"
        try:
            resolve("")
            check("no LAN address -> clear error, not a /8 sweep", False)
        except SubnetError as exc:
            check("no LAN address -> clear error, not a /8 sweep",
                  "discovery.subnet" in str(exc) and "16," not in str(exc), str(exc))

        netutil.primary_ip = lambda: "192.168.50.23"
        original_mask = netutil._linux_netmask
        netutil._linux_netmask = lambda ip: "255.255.254.0"
        try:
            net, source = resolve("")
            check("uses the interface's real netmask when the OS reports it",
                  str(net) == "192.168.50.0/23" and "netmask" in source, f"{net} ({source})")
        finally:
            netutil._linux_netmask = original_mask

        netutil._linux_netmask = lambda ip: None
        try:
            net, source = resolve("")
            check("falls back to /24 when it can't read the netmask",
                  str(net) == "192.168.50.0/24" and "/24" in source, f"{net} ({source})")
        finally:
            netutil._linux_netmask = original_mask

        net, source = resolve("10.9.0.0/22")
        check("a configured CIDR wins over derivation",
              str(net) == "10.9.0.0/22" and source == "configured")
    finally:
        netutil.primary_ip = original


def test_real_sweep():
    print("\n7. An actual sweep of a CIDR finds a player in it")
    serve(MockPlayer(name="Loopback Node", port=11000))
    found = asyncio.run(discover_sweep("127.0.0.0/30"))
    check("CIDR string accepted by discover_sweep", isinstance(found, list))
    check("found the player at 127.0.0.1",
          any(h == "127.0.0.1" and n == "Loopback Node" for h, n, _ in found), str(found))
    found = asyncio.run(discover_sweep(ipaddress.ip_network("127.0.0.0/30")))
    check("IPv4Network accepted too", any(h == "127.0.0.1" for h, _, _ in found))


def main() -> int:
    test_parsing()
    test_rejections()
    test_host_counts()
    test_broadcast()
    test_config_validation()
    test_auto_derivation()
    test_real_sweep()

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
