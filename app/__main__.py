"""Entrypoint: bind the host and port from config.yaml.

Running uvicorn directly from the Dockerfile CMD would hardcode the port and
silently ignore ``listen_port`` in config.yaml. Starting it here keeps the
config file as the single source of truth, which matters on Unraid where the
default 8080 usually collides with something else.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from . import bootstrap
from .config import Config, load_config


def main() -> int:
    # Seed a starter config and the default chime if this is a fresh install,
    # so a GUI-only install never needs a shell.
    # Seeding runs before the service configures logging, so set up a handler
    # now — otherwise "installed bundled chime …" never reaches the log.
    # main.py reconfigures with the level from config.yaml once it's loaded.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    bootstrap.run(
        Path(os.environ.get("DOORBELL_CONFIG", "/config/config.yaml")),
        Path(os.environ.get("DOORBELL_CHIME_DIR", "/chimes")),
    )

    try:
        config = load_config()
    except FileNotFoundError:
        # Seeding failed, which almost always means /config is mounted
        # read-only. Start anyway so /health and the logs can say so — a
        # crash-looping container is undebuggable without a shell.
        print(
            "error: no config.yaml and could not create one.\n"
            "The /config mount is probably read-only. On Unraid, edit the\n"
            "container and set the config path's Access Mode to Read/Write.\n"
            "Starting unconfigured so this message stays visible.",
            file=sys.stderr,
        )
        config = Config()
    except Exception as exc:  # noqa: BLE001 - config errors should be readable
        print(f"error: config is invalid: {exc}", file=sys.stderr)
        print("Fix config.yaml and restart. Nothing else will work until then.",
              file=sys.stderr)
        return 1

    uvicorn.run(
        "app.main:app",
        host=config.listen_host,
        port=config.listen_port,
        log_level=config.log_level.lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
