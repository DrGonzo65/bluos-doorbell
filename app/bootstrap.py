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
# This file was created automatically on first run. Fill in your players and
# a webhook token, then restart the container.
#
# Editing without SSH: this file lives on the appdata share, so you can open it
# straight from Finder or Explorer at
#     \\\\<tower>\\appdata\\bluos-doorbell\\config\\config.yaml
#
# Find your players' IPs in the BluOS app under Settings -> Player -> Network.

# URL the PLAYERS use to fetch the chime. Leave blank to auto-detect this
# host's primary IP. Set it explicitly if the server is multi-homed or the
# players sit on a different VLAN.
service_base_url: ""
listen_host: "0.0.0.0"
# 8080 collides with all sorts of things on an Unraid box, so the default is 8095.
listen_port: 8095

log_level: INFO

# Each entry is one player that should chime. The service works out grouping
# on its own — list the rooms you want, not the group structure.
zones: []
  # - name: Kitchen
  #   host: 192.168.1.51
  #   chime_volume: 35          # optional, overrides chime.default_volume
  #
  # - name: Living Room
  #   host: 192.168.1.52
  #
  # - name: Primary Bedroom
  #   host: 192.168.1.54
  #   chime_when_idle: false    # don't wake a silent bedroom
  #   chime_volume: 20

chime:
  file: doorbell.mp3
  # Must match the real length of the file. Too short clips the chime; too long
  # leaves a silent gap before the music comes back. The bundled one is 2.3s.
  duration_seconds: 2.3
  tail_seconds: 0.8
  default_volume: 30

behaviour:
  # Ignore repeat rings inside this window. This is what stops a double-press
  # from capturing the already-ducked volume as the "previous" volume.
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

webhook:
  # Shared secret. UniFi Protect must send it as ?token=... on the webhook URL.
  # Change this. Leave blank only on a trusted VLAN.
  token: "change-me"

  # Optional allowlist so only your front door can ring, matched as a
  # case-insensitive substring against the Protect payload.
  allowed_devices: []
    # - "Front Door"
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
    """Copy the bundled chime into an empty chimes folder."""
    source_dir = DEFAULTS_DIR / "chimes"
    if not source_dir.is_dir():
        return

    try:
        chime_dir.mkdir(parents=True, exist_ok=True)
        existing = any(chime_dir.iterdir())
    except OSError as exc:
        log.warning("chime folder %s is not usable: %s", chime_dir, exc)
        return

    if existing:
        return

    for item in source_dir.iterdir():
        if not item.is_file():
            continue
        try:
            shutil.copy2(item, chime_dir / item.name)
            log.info("installed default chime %s", item.name)
        except OSError as exc:
            log.warning("could not copy %s: %s", item.name, exc)


def run(config_path: Path, chime_dir: Path) -> None:
    seed_chimes(chime_dir)
    seed_config(config_path)
