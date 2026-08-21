"""Find BluOS players and print ready-to-paste config YAML.

Three methods, run in order, because any one of them can come back empty for
reasons that have nothing to do with your players:

1. **LSDP** (UDP broadcast, port 11430) — what BluOS actually uses. Bluesound
   built their own discovery protocol rather than relying on Bonjour.
2. **mDNS** (``_musc._tcp``) — works on some firmware, absent on others.
3. **Subnet sweep** — asks every address on your subnet for /SyncStatus. Needs
   no multicast or broadcast at all, so it works when the other two are being
   blocked. Slower, but it is the one that gives a definite answer.

Usage:
    python -m tools.discover                     # all three, auto subnet
    python -m tools.discover --subnet 192.168.1  # force the range to sweep
    python -m tools.discover --method sweep      # skip straight to the sweep
    python -m tools.discover --timeout 6
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import time

import httpx

from . import lsdp

BLUOS_PORT = 11000


# --------------------------------------------------------------------------
# helpers

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


def broadcast_targets(ip: str) -> list[str]:
    """Where to send the LSDP query.

    255.255.255.255 only leaves via the default route on some stacks, so send
    to the /24 directed broadcast too.
    """
    targets = ["255.255.255.255"]
    parts = ip.split(".")
    if len(parts) == 4 and ip != "127.0.0.1":
        targets.append(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
    return targets


async def probe(client: httpx.AsyncClient, host: str, port: int = BLUOS_PORT
                ) -> tuple[str, str, str] | None:
    """Ask one address for /SyncStatus. Returns (host, name, model) or None."""
    try:
        resp = await client.get(f"http://{host}:{port}/SyncStatus", timeout=2.0)
        if resp.status_code != 200 or "SyncStatus" not in resp.text:
            return None
        text = resp.text
    except (httpx.HTTPError, OSError):
        return None

    def attr(key: str) -> str:
        marker = f'{key}="'
        start = text.find(marker)
        if start < 0:
            return ""
        start += len(marker)
        end = text.find('"', start)
        return text[start:end] if end > start else ""

    name = attr("name") or host
    model = attr("modelName") or attr("model") or "BluOS player"
    if "<master" in text:
        model += " (grouped secondary)"
    elif "<slave" in text:
        model += " (group primary)"
    return host, name, model


# --------------------------------------------------------------------------
# method 1: LSDP

def discover_lsdp(timeout: float) -> tuple[list[tuple[str, str, str]], str | None]:
    """Broadcast an LSDP query and collect announcements.

    Returns (found, error). `error` is a human-readable reason when the socket
    itself failed, which is the interesting case on macOS.
    """
    ip = primary_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    # Binding the well-known port also catches the periodic announcements
    # players emit every ~57s, not just replies to our query.
    try:
        sock.bind(("", lsdp.PORT))
    except OSError:
        try:
            sock.bind(("", 0))
        except OSError as exc:
            sock.close()
            return [], f"could not open a UDP socket: {exc}"

    query = lsdp.build_query()
    sent = 0
    send_error = None
    for target in broadcast_targets(ip):
        try:
            sock.sendto(query, (target, lsdp.PORT))
            sent += 1
        except OSError as exc:
            send_error = exc

    if not sent:
        sock.close()
        return [], f"broadcast was blocked: {send_error}"

    found: dict[str, tuple[str, str, str]] = {}
    deadline = time.monotonic() + timeout
    sock.settimeout(0.4)
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        for ann in lsdp.parse(data, source_ip=addr[0]):
            if ann.is_player and ann.address not in found:
                found[ann.address] = (ann.address, ann.name, ann.model)

    sock.close()
    return list(found.values()), None


# --------------------------------------------------------------------------
# method 2: mDNS

def discover_mdns(timeout: float) -> tuple[list[tuple[str, str, str]], str | None]:
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return [], "zeroconf is not installed (pip install zeroconf)"

    class _Listener(ServiceListener):
        def __init__(self) -> None:
            self.found: dict[str, tuple[str, int]] = {}

        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if info and info.addresses:
                host = socket.inet_ntoa(info.addresses[0])
                self.found[host] = (host, info.port or BLUOS_PORT)

        update_service = add_service

        def remove_service(self, zc, type_, name):
            pass

    try:
        zc = Zeroconf()
    except OSError as exc:
        return [], f"could not start mDNS: {exc}"

    listener = _Listener()
    try:
        ServiceBrowser(zc, "_musc._tcp.local.", listener)
        time.sleep(timeout)
    finally:
        zc.close()

    return [(h, h, "found via mDNS") for h, _ in listener.found.values()], None


# --------------------------------------------------------------------------
# method 3: subnet sweep

async def discover_sweep(subnet: str, concurrency: int = 64
                         ) -> list[tuple[str, str, str]]:
    hosts = [f"{subnet}.{i}" for i in range(1, 255)]
    semaphore = asyncio.Semaphore(concurrency)
    found: list[tuple[str, str, str]] = []

    async with httpx.AsyncClient(timeout=2.0) as client:
        async def one(host: str) -> None:
            async with semaphore:
                result = await probe(client, host)
            if result:
                found.append(result)

        done = 0
        tasks = [asyncio.create_task(one(h)) for h in hosts]
        for task in asyncio.as_completed(tasks):
            await task
            done += 1
            if done % 25 == 0 or done == len(hosts):
                print(f"\r  swept {done}/{len(hosts)} addresses, "
                      f"found {len(found)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return sorted(found, key=lambda r: [int(p) for p in r[0].split(".")])


# --------------------------------------------------------------------------

def emit(found: list[tuple[str, str, str]]) -> None:
    print("\nzones:")
    for host, name, model in found:
        print(f"  - name: {name}")
        print(f"    host: {host}")
        print(f"    # {model}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Find BluOS players on the LAN")
    parser.add_argument("--method", choices=["auto", "lsdp", "mdns", "sweep"],
                        default="auto")
    parser.add_argument("--subnet", help="first three octets to sweep, "
                                         "e.g. 192.168.1 (default: this host's)")
    parser.add_argument("--timeout", type=float, default=4.0,
                        help="seconds to listen for LSDP/mDNS (default 4)")
    args = parser.parse_args()

    ip = primary_ip()
    subnet = args.subnet or ".".join(ip.split(".")[:3])
    print(f"This host: {ip}   sweeping subnet: {subnet}.0/24", file=sys.stderr)

    lsdp_blocked = False

    if args.method in ("auto", "lsdp"):
        print(f"\n[1/3] LSDP broadcast on udp/{lsdp.PORT} "
              f"({args.timeout:.0f}s)...", file=sys.stderr)
        found, error = discover_lsdp(args.timeout)
        if error:
            print(f"      {error}", file=sys.stderr)
            lsdp_blocked = True
        print(f"      {len(found)} player(s)", file=sys.stderr)
        if found and args.method == "lsdp":
            emit(found)
            return 0
        if found:
            emit(found)
            return 0

    if args.method in ("auto", "mdns"):
        print(f"\n[2/3] mDNS browse for _musc._tcp ({args.timeout:.0f}s)...",
              file=sys.stderr)
        found, error = discover_mdns(args.timeout)
        if error:
            print(f"      {error}", file=sys.stderr)
        print(f"      {len(found)} player(s)", file=sys.stderr)
        if found:
            emit(found)
            return 0

    if args.method in ("auto", "sweep"):
        print(f"\n[3/3] Sweeping {subnet}.1-254 on port {BLUOS_PORT}...",
              file=sys.stderr)
        found = asyncio.run(discover_sweep(subnet))
        if found:
            print(f"      {len(found)} player(s)", file=sys.stderr)
            emit(found)
            if args.method == "auto":
                print("\n# Note: the sweep found these but broadcast/mDNS did not.",
                      file=sys.stderr)
                print("# That's a local network permission or firewall issue, not a",
                      file=sys.stderr)
                print("# player problem — the addresses above are correct and the",
                      file=sys.stderr)
                print("# doorbell service talks to them over plain HTTP anyway.",
                      file=sys.stderr)
                if sys.platform == "darwin" and lsdp_blocked:
                    print("# On macOS, grant your terminal Local Network access under",
                          file=sys.stderr)
                    print("# System Settings -> Privacy & Security -> Local Network.",
                          file=sys.stderr)
            return 0

    print("\nNo players found by any method.", file=sys.stderr)
    print("\nThings to check:", file=sys.stderr)
    print(f"  * Is the subnet right? Tried {subnet}.0/24 — override with --subnet",
          file=sys.stderr)
    print("  * Can you reach one directly? curl http://<player-ip>:11000/SyncStatus",
          file=sys.stderr)
    print("  * Get an IP from the BluOS app: Settings -> Player -> Network",
          file=sys.stderr)
    if sys.platform == "darwin":
        print("  * macOS 15+ blocks local network traffic until you allow it:",
              file=sys.stderr)
        print("    System Settings -> Privacy & Security -> Local Network -> "
              "enable your terminal", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
