"""First-run seeding so the container is usable without shell access.

Installing from the Unraid GUI (or Community Apps) creates the appdata folders
but leaves them empty. Without this, the container would crash-loop on a
missing config.yaml and the only fix would be SSH — which defeats the point of
a one-click install. Instead we write a starter config and copy in the default
chime, then run in an unconfigured-but-healthy state so the WebUI link works
and tells you what to do next.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("doorbell.bootstrap")

#: Shipped inside the image; copied out on first run.
DEFAULTS_DIR = Path(os.environ.get("DOORBELL_DEFAULTS", "/srv/defaults"))

STARTER_CONFIG = """\
# BluOS doorbell — configuration
#
# This file was created automatically on first run.
#
# You probably don't need to change anything except the webhook token. The
# service finds your players by itself and chimes in all of them.
#
# Editing without SSH: this file lives on the appdata share, so you can open it
# straight from Finder or Explorer at
#     \\\\<tower>\\appdata\\bluos-doorbell\\config\\config.yaml
#
# See what was found:  http://<host>:8095/discover?token=<your token>

webhook:
  # Shared secret, sent as ?token=... on the webhook URL. It also gates
  # /inspect, /discover and the player detail on /health.
  #
  # CHANGE THIS. The value below ships inside the public image, so it
  # protects nothing until you replace it:
  #     python3 -c "import secrets; print(secrets.token_urlsafe(24))"
  #
  # Leaving it blank switches authentication off entirely — reasonable on a
  # trusted VLAN, and the service warns about it at startup.
  token: "change-me"

  # Optional allowlist so only your front door can ring, matched as a
  # case-insensitive substring against the Protect payload.
  allowed_devices: []
    # - "Front Door"

discovery:
  # Find players automatically. Set false to use only the zones listed below.
  auto: true

  # Network to sweep, as CIDR: "192.168.1.0/24", "10.0.4.0/22". Blank = this
  # host's own subnet, using its real netmask. Set it if the players live on a
  # different VLAN from this server, or it has several interfaces and picks the
  # wrong one. Anything larger than a /20 is refused — narrow it instead.
  subnet: ""

  # How often to re-check. New or renamed players appear within this window;
  # a player that broadcasts its presence is noticed within about a minute.
  refresh_seconds: 300

  # Never chime these, by name or IP.
  exclude: []
    # - "Garage"
    # - 192.168.1.60

# Only needed to override a discovered player. Match by name or host and set
# just the fields you want changed — everything else stays automatic.
zones: []
  # - name: Primary Bedroom
  #   chime_when_idle: false    # don't wake a silent room
  #   chime_volume: 20
  #
  # - name: Garage
  #   enabled: false            # never chime here
  #
  # - name: Back Deck           # a player discovery can't see, pinned by hand
  #   host: 192.168.1.60

# The default doorbell — its webhook is /doorbell.
chime:
  file: doorbell.mp3
  # Must match the real length of the file. Too short clips the chime; too long
  # leaves a silent gap before the music comes back. The bundled one is 2.3s.
  duration_seconds: 2.3
  tail_seconds: 0.8
  default_volume: 30

# More doorbells, each with its own sound, ringing the same rooms at the same
# volumes. Each gets its own webhook: /doorbell/<name>. In UniFi Protect, make
# one Alarm Manager rule per doorbell camera, each pointing at its own URL.
# duration_seconds is required — it must match that file's real length.
doorbells: {}
  # back:
  #   file: back-door.mp3       # bundled: three quick descending notes
  #   duration_seconds: 2.05

behaviour:
  # Ignore repeat rings of the SAME doorbell inside this window. Counted per
  # doorbell, so the back door still rings right after the front door. A
  # press that arrives while a chime is playing is queued, not ignored.
  debounce_seconds: 8.0

  # Soft fade rather than a hard jump. 0 disables.
  fade_ms: 300
  fade_steps: 4

  state_ttl_seconds: 120.0

  # When a target zone is grouped under a primary that isn't itself a target:
  #   primary — chime via that primary (the whole group hears it)
  #   skip    — leave that group alone
  group_policy: primary

  restore_pause_state: true
  http_timeout_seconds: 5.0

# URL the PLAYERS use to fetch the chime. Blank = auto-detect this host's IP.
service_base_url: ""
listen_host: "0.0.0.0"
# 8080 collides with all sorts of things on an Unraid box, so the default is 8095.
listen_port: 8095

log_level: INFO
"""


def seed_config(config_path: Path) -> bool:
    """Write a starter config if none exists. Returns True if one was created."""
    if config_path.exists():
        return False

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(STARTER_CONFIG, encoding="utf-8")
    except OSError as exc:
        log.error("could not create %s: %s", config_path, exc)
        log.error("mount the config folder read-write, or create config.yaml yourself")
        return False

    log.warning("=" * 68)
    log.warning("Created a starter config at %s", config_path)
    log.warning("No zones are configured yet, so nothing will chime.")
    log.warning("Add your players to it and restart this container.")
    log.warning("=" * 68)
    return True


def seed_chimes(chime_dir: Path) -> None:
    """Add any bundled chime the chimes folder doesn't have yet.

    Runs on every start, not just the first, so chimes added in a later
    release reach existing installs. It only ever adds: a file that's already
    there — yours, or a bundled one you've replaced — is never overwritten.
    """
    source_dir = DEFAULTS_DIR / "chimes"
    if not source_dir.is_dir():
        return

    try:
        chime_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("chime folder %s is not usable: %s", chime_dir, exc)
        return

    for item in sorted(source_dir.iterdir()):
        target = chime_dir / item.name
        if not item.is_file() or target.exists():
            continue
        try:
            shutil.copy2(item, target)
            log.info("installed bundled chime %s", item.name)
        except OSError as exc:
            log.warning("could not copy %s: %s", item.name, exc)


def run(config_path: Path, chime_dir: Path) -> None:
    seed_chimes(chime_dir)
    seed_config(config_path)
