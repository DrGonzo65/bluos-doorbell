"""LSDP — Lenbrook Service Discovery Protocol.

This is how BluOS players actually announce themselves. Bluesound built it
instead of relying on mDNS/Bonjour, which is why an mDNS browse for
``_musc._tcp`` can come back empty even with players sitting right there.

Wire format (BluOS Custom Integration API v1.7, section 11). All multi-byte
values big-endian.

    Header:   length(1, includes itself) | "LSDP" | version(1)
    Query:    length(1) | 'Q' | count(1) | class(2) * count
    Announce: length(1) | 'A' | node_id_len(1) | node_id
              | addr_len(1) | addr | count(1)
              | [ class(2) | txt_count(1) | [ key_len(1) | key
                                            | val_len(1) | val ] * txt_count
                ] * count
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, field

PORT = 11430
MAGIC = b"LSDP"
VERSION = 1

CLASS_PLAYER = 0x0001
CLASS_SERVER = 0x0002
CLASS_SECONDARY = 0x0003
CLASS_PAIR_SLAVE = 0x0006
CLASS_HUB = 0x0008
CLASS_ALL = 0xFFFF

#: Classes that represent something we could send a chime to.
PLAYER_CLASSES = {CLASS_PLAYER, CLASS_SECONDARY, CLASS_PAIR_SLAVE}

CLASS_NAMES = {
    CLASS_PLAYER: "BluOS Player",
    CLASS_SERVER: "BluOS Server",
    CLASS_SECONDARY: "BluOS Player (secondary zone)",
    0x0004: "sovi-mfg",
    0x0005: "sovi-keypad",
    CLASS_PAIR_SLAVE: "BluOS Player (pair slave)",
    0x0007: "Remote Web App",
    CLASS_HUB: "BluOS Hub",
}


@dataclass
class Announcement:
    address: str
    node_id: str
    classes: set[int] = field(default_factory=set)
    txt: dict[str, str] = field(default_factory=dict)

    @property
    def is_player(self) -> bool:
        return bool(self.classes & PLAYER_CLASSES)

    @property
    def name(self) -> str:
        return self.txt.get("name") or self.txt.get("Name") or self.address

    @property
    def model(self) -> str:
        for key in ("model", "Model", "modelName", "mdl"):
            if key in self.txt:
                return self.txt[key]
        return ", ".join(sorted(CLASS_NAMES.get(c, hex(c)) for c in self.classes))


def build_query(classes: tuple[int, ...] = (CLASS_ALL,)) -> bytes:
    """A broadcast query packet asking every listening node to announce."""
    header = struct.pack("!B4sB", 6, MAGIC, VERSION)
    body = struct.pack("!B", len(classes)) + b"".join(
        struct.pack("!H", c) for c in classes
    )
    message = struct.pack("!BB", len(body) + 2, ord("Q")) + body
    return header + message


class _Reader:
    """Bounds-checked cursor — malformed packets must not raise IndexError."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def u8(self) -> int:
        if self.remaining() < 1:
            raise ValueError("truncated packet")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def u16(self) -> int:
        if self.remaining() < 2:
            raise ValueError("truncated packet")
        value = struct.unpack_from("!H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def blob(self, length: int) -> bytes:
        if self.remaining() < length:
            raise ValueError("truncated packet")
        value = self.data[self.pos:self.pos + length]
        self.pos += length
        return value


def parse(packet: bytes, source_ip: str | None = None) -> list[Announcement]:
    """Parse a datagram into any announcements it carries.

    Returns [] for queries, other message types, or anything malformed —
    discovery should never crash on a stray packet from an unrelated device.
    """
    r = _Reader(packet)
    try:
        header_len = r.u8()
        if header_len < 6:
            return []
        magic = r.blob(4)
        if magic != MAGIC:
            return []
        r.u8()  # protocol version
        # Skip any header bytes a future version might add.
        r.pos = header_len

        out: list[Announcement] = []
        while r.remaining() >= 2:
            msg_start = r.pos
            msg_len = r.u8()
            msg_type = r.u8()

            if msg_type != ord("A"):
                # Query or delete — skip using the declared length.
                if msg_len < 2:
                    break
                r.pos = msg_start + msg_len
                continue

            node_id = r.blob(r.u8()).hex()
            addr_bytes = r.blob(r.u8())
            address = (socket.inet_ntoa(addr_bytes) if len(addr_bytes) == 4
                       else (source_ip or ""))

            ann = Announcement(address=address or (source_ip or ""), node_id=node_id)
            for _ in range(r.u8()):
                ann.classes.add(r.u16())
                for _ in range(r.u8()):
                    key = r.blob(r.u8()).decode("utf-8", "replace")
                    val = r.blob(r.u8()).decode("utf-8", "replace")
                    ann.txt[key] = val

            if ann.address:
                out.append(ann)

            if msg_len >= 2:
                r.pos = msg_start + msg_len

        return out
    except (ValueError, struct.error, OSError):
        return []


def build_announce(address: str, node_id: bytes, classes: dict[int, dict[str, str]]) -> bytes:
    """Construct an announce packet. Used by the tests to verify the parser."""
    body = struct.pack("!B", len(node_id)) + node_id
    body += struct.pack("!B", 4) + socket.inet_aton(address)
    body += struct.pack("!B", len(classes))
    for class_id, txt in classes.items():
        body += struct.pack("!HB", class_id, len(txt))
        for key, value in txt.items():
            kb, vb = key.encode(), value.encode()
            body += struct.pack("!B", len(kb)) + kb + struct.pack("!B", len(vb)) + vb
    message = struct.pack("!BB", len(body) + 2, ord("A")) + body
    return struct.pack("!B4sB", 6, MAGIC, VERSION) + message
