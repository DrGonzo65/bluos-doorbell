"""Entrypoint: bind the host and port from config.yaml.

Running uvicorn directly from the Dockerfile CMD would hardcode the port and
silently ignore ``listen_port`` in config.yaml. Starting it here keeps the
config file as the single source of truth, which matters on Unraid where the
default 8080 usually collides with something else.
"""

from __future__ import annotations

import sys

import uvicorn

from .config import load_config


def main() -> int:
    try:
        config = load_config()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "\nOn Unraid, map a host folder to /config and put config.yaml in it:\n"
            "  /mnt/user/appdata/bluos-doorbell/config  ->  /config",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - config errors should be readable
        print(f"error: config is invalid: {exc}", file=sys.stderr)
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
