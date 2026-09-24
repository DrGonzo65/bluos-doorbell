"""Subnet handling for discovery — one place that parses, validates and derives.

Stdlib only, so the discovery CLI keeps working without the service's deps.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import sys

#: Refuse to sweep anything bigger than this. A /20 is 4094 hosts, which at
#: the sweep's concurrency takes a couple of minutes; a /16 would take the
#: better part of an hour and look like a port scan to anything watching.
MAX_SWEEP_HOSTS = 4096

_LEGACY_THREE_OCTETS = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")


class SubnetError(ValueError):
    pass


def parse_subnet(value: str) -> ipaddress.IPv4Network:
    """Parse a CIDR like ``192.168.1.0/24`` or ``10.0.4.0/22``.

    Host bits are tolerated (``192.168.1.37/24`` means ``192.168.1.0/24``)
    because that's how people copy it off an interface. A bare address with no
    prefix is rejected rather than silently read as a /32 sweep of one host.

    The old three-octet form (``192.168.1``) is still accepted as a /24 so an
    existing config.yaml keeps working.
    """
    raw = (value or "").strip()
    if not raw:
        raise SubnetError("subnet is empty")

    if _LEGACY_THREE_OCTETS.match(raw):
        raw = f"{raw}.0/24"

    if "/" not in raw:
        raise SubnetError(
            f"{value!r} has no prefix length — write it as CIDR, "
            f"e.g. {raw}/24"
        )

    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError as exc:
        raise SubnetError(f"{value!r} is not a valid CIDR: {exc}") from None

    if not isinstance(net, ipaddress.IPv4Network):
        raise SubnetError(f"{value!r} is IPv6 — BluOS discovery is IPv4 only")

    return net


def check_sweepable(net: ipaddress.IPv4Network) -> None:
    hosts = max(net.num_addresses - 2, 1)
    if hosts > MAX_SWEEP_HOSTS:
        raise SubnetError(
            f"{net} is {hosts:,} addresses — too many to sweep (limit "
            f"{MAX_SWEEP_HOSTS:,}, i.e. a /20). Narrow it to the range your "
            f"players actually sit in, or pin them under zones:."
        )


def sweep_hosts(net: ipaddress.IPv4Network) -> list[str]:
    """Every address worth probing. /31 and /32 have no network/broadcast pair,
    so ``hosts()`` already handles them correctly."""
    return [str(ip) for ip in net.hosts()]


def primary_ip() -> str:
    """The local address other hosts on the LAN would reach us on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _linux_netmask(ip: str) -> str | None:
    """Ask the kernel for the netmask of the interface holding ``ip``.

    Linux-only (SIOCGIFNETMASK), which is where the container runs. Anywhere
    else — or if anything goes wrong — return None and let the caller fall
    back.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        import fcntl
    except ImportError:
        return None

    SIOCGIFADDR, SIOCGIFNETMASK = 0x8915, 0x891B
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            req = struct.pack("256s", name[:15].encode())
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(sock, SIOCGIFADDR, req)[20:24])
            except OSError:
                continue
            if addr != ip:
                continue
            try:
                return socket.inet_ntoa(fcntl.ioctl(sock, SIOCGIFNETMASK, req)[20:24])
            except OSError:
                return None
    except OSError:
        return None
    finally:
        sock.close()
    return None


def local_network() -> tuple[ipaddress.IPv4Network, str]:
    """This host's own subnet, and how we worked it out.

    Raises SubnetError when the host has no usable LAN address.

    Uses the interface's real netmask where the OS will tell us, so a /23 or
    /22 LAN is swept in full. Otherwise assumes a /24 around our address,
    which is what most home networks are.
    """
    ip = primary_ip()
    if ipaddress.ip_address(ip).is_loopback:
        raise SubnetError(
            "couldn't work out this host's LAN address (no route out), so there "
            "is no subnet to sweep — set discovery.subnet to a CIDR, e.g. "
            "192.168.1.0/24"
        )
    mask = _linux_netmask(ip)
    if mask:
        net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
        return net, f"interface netmask of {ip}"
    return ipaddress.ip_network(f"{ip}/24", strict=False), f"assumed /24 around {ip}"


def resolve(configured: str | None) -> tuple[ipaddress.IPv4Network, str]:
    """The network to sweep: the configured CIDR if set, else this host's own.

    Raises SubnetError for a malformed or oversized value — the caller decides
    whether that's fatal.
    """
    if configured and configured.strip():
        net = parse_subnet(configured)
        source = "configured"
    else:
        net, source = local_network()
    check_sweepable(net)
    return net, source
