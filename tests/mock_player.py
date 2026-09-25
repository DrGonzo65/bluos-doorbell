"""A fake BluOS player for testing the doorbell service without hardware.

Deliberately reproduces the two behaviours that trip up real integrations:

* ``/Status`` on a grouped secondary returns a COPY of the primary's status,
  including the primary's volume (API doc section 2.2).
* Transport commands sent to a secondary are proxied to the primary
  (section 8), so the secondary itself never changes source.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class MockPlayer:
    name: str
    port: int
    volume: int = 45
    mute: bool = False
    state: str = "play"
    service: str = "LocalMusic"
    stream_url: str | None = None
    input_id: str | None = None
    song: int | None = 7
    pid: int = 1234
    secs: int = 96
    totlen: int | None = 214
    can_seek: bool = True
    fixed_volume: bool = False

    master: "MockPlayer | None" = None
    slaves: list["MockPlayer"] = field(default_factory=list)
    group: str | None = None

    calls: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    volume_writes: list[tuple[int, str]] = field(default_factory=list)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def effective(self) -> "MockPlayer":
        """Whose playback state this player reports/obeys."""
        return self.master or self


def _status_xml(p: MockPlayer) -> str:
    src = p.effective  # secondaries mirror the primary, including its volume
    parts = [
        f"<state>{src.state}</state>",
        f"<volume>{src.volume}</volume>",
        f"<mute>{1 if src.mute else 0}</mute>",
        f"<secs>{src.secs}</secs>",
        f"<pid>{src.pid}</pid>",
        f"<canSeek>{1 if src.can_seek else 0}</canSeek>",
        f"<service>{src.service}</service>",
        "<syncStat>9</syncStat>",
    ]
    if src.stream_url:
        parts.append(f"<streamUrl>{src.stream_url}</streamUrl>")
    if src.input_id:
        parts.append(f"<inputId>{src.input_id}</inputId>")
    if src.song is not None:
        parts.append(f"<song>{src.song}</song>")
    if src.totlen is not None:
        parts.append(f"<totlen>{src.totlen}</totlen>")
    return f'<status etag="abc">{"".join(parts)}</status>'


def _sync_xml(p: MockPlayer) -> str:
    vol = -1 if p.fixed_volume else p.volume
    attrs = f'name="{p.name}" volume="{vol}" mac="00:11:22:33:44:{p.port % 100:02d}"'
    if p.group:
        attrs += f' group="{p.group}"'
    body = ""
    if p.master:
        host, port = p.master.address.split(":")
        body += f'<master port="{port}">{host}</master>'
    for s in p.slaves:
        host, port = s.address.split(":")
        body += f'<slave id="{host}" port="{port}"/>'
    return f"<SyncStatus {attrs}>{body}</SyncStatus>"


def _volume_xml(p: MockPlayer) -> str:
    vol = -1 if p.fixed_volume else p.volume
    return f'<volume db="-20.0" mute="{1 if p.mute else 0}" etag="v1">{vol}</volume>'


class _Handler(BaseHTTPRequestHandler):
    player: MockPlayer

    def log_message(self, *args: Any) -> None:  # silence
        pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        p = self.player
        p.calls.append((path, params))
        target = p.effective  # transport commands proxy to the primary

        if path == "/Status":
            body = _status_xml(p)
        elif path == "/SyncStatus":
            body = _sync_xml(p)
        elif path == "/Volume":
            if "level" in params and not p.fixed_volume:
                p.volume = int(params["level"][0])
                p.volume_writes.append((p.volume, params.get("tell_slaves", ["?"])[0]))
                if params.get("tell_slaves", ["0"])[0] == "1":
                    for s in p.slaves:
                        s.volume = p.volume
            body = _volume_xml(p)
        elif path == "/Play":
            if "url" in params:
                target.state = "stream"
                target.stream_url = params["url"][0]
                target.song = None
            elif "id" in params:
                target.state = "play"
                target.stream_url = None
                target.song = int(params["id"][0])
                target.secs = int(params.get("seek", [0])[0])
            elif "inputTypeIndex" in params or "InputId" in params:
                target.state = "stream"
                target.input_id = params.get("inputTypeIndex", params.get("InputId"))[0]
            else:
                target.state = "play"
            body = f"<state>{target.state}</state>"
        elif path == "/Pause":
            target.state = "pause"
            body = "<state>pause</state>"
        elif path == "/Stop":
            target.state = "stop"
            body = "<state>stop</state>"
        else:
            self.send_error(404)
            return

        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(player: MockPlayer) -> HTTPServer:
    handler = type(f"H{player.port}", (_Handler,), {"player": player})
    server = HTTPServer(("127.0.0.1", player.port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def group(primary: MockPlayer, *secondaries: MockPlayer, name: str = "Downstairs") -> None:
    primary.group = name
    primary.slaves = list(secondaries)
    for s in secondaries:
        s.master = primary
        s.group = name
