"""Find BluOS players on the LAN and print ready-to-paste config YAML.

BluOS players advertise over mDNS as _musc._tcp. Run with:
    python -m tools.discover
Requires host networking (mDNS is multicast and won't cross a bridge network).
"""

from __future__ import annotations

import asyncio
import socket
import sys

import httpx
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

SERVICE = "_musc._tcp.local."


class _Listener(ServiceListener):
    def __init__(self) -> None:
        self.found: dict[str, tuple[str, int]] = {}

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        if not info or not info.addresses:
            return
        host = socket.inet_ntoa(info.addresses[0])
        self.found[name] = (host, info.port or 11000)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.found.pop(name, None)


async def _describe(host: str, port: int) -> tuple[str, str]:
    """Return (player name, model/role summary) via /SyncStatus."""
    url = f"http://{host}:{port}/SyncStatus"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        text = resp.text
    except httpx.HTTPError as exc:
        return host, f"unreachable ({exc})"

    def attr(key: str) -> str:
        marker = f'{key}="'
        start = text.find(marker)
        if start < 0:
            return ""
        start += len(marker)
        return text[start:text.find('"', start)]

    name = attr("name") or host
    model = attr("modelName") or attr("model") or "BluOS player"
    grouped = " (grouped)" if "<master" in text or "<slave" in text else ""
    return name, f"{model}{grouped}"


def main() -> int:
    zc = Zeroconf()
    listener = _Listener()
    ServiceBrowser(zc, SERVICE, listener)

    print(f"Browsing for {SERVICE} … (5s)", file=sys.stderr)
    try:
        import time
        time.sleep(5)
    finally:
        zc.close()

    if not listener.found:
        print("No players found. Check that this is running with host networking,\n"
              "or read the IPs from the BluOS app under Settings -> Player -> Network.",
              file=sys.stderr)
        return 1

    entries = sorted(listener.found.values())
    described = asyncio.run(_gather(entries))

    print("\nzones:")
    for (host, port), (name, model) in zip(entries, described):
        print(f"  - name: {name}")
        print(f"    host: {host}")
        if port != 11000:
            print(f"    port: {port}")
        print(f"    # {model}")
    return 0


async def _gather(entries: list[tuple[str, int]]):
    return await asyncio.gather(*(_describe(h, p) for h, p in entries))


if __name__ == "__main__":
    raise SystemExit(main())
