"""Tests for the first-run seeding that makes a GUI-only install work.

Run: python -m tests.test_bootstrap
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from app import bootstrap  # noqa: E402
from app.config import Config, load_config  # noqa: E402

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def fresh_install(tmp: Path) -> tuple[Path, Path]:
    """An empty appdata pair, exactly as an Unraid GUI install leaves it."""
    config_dir, chime_dir = tmp / "config", tmp / "chimes"
    defaults = tmp / "defaults" / "chimes"
    config_dir.mkdir(parents=True)
    chime_dir.mkdir(parents=True)
    defaults.mkdir(parents=True)
    shutil.copy(Path(__file__).parents[1] / "chimes" / "doorbell.mp3",
                defaults / "doorbell.mp3")
    bootstrap.DEFAULTS_DIR = tmp / "defaults"
    return config_dir, chime_dir


def test_seeds_empty_install():
    print("\n1. Empty appdata folders get seeded")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        config_dir, chime_dir = fresh_install(tmp)
        config_path = config_dir / "config.yaml"

        bootstrap.run(config_path, chime_dir)

        check("config.yaml created", config_path.exists())
        check("default chime installed", (chime_dir / "doorbell.mp3").exists())

        cfg = load_config(config_path)
        check("seeded config loads", isinstance(cfg, Config))
        check("starts unconfigured (no fake zones)", not cfg.is_configured,
              f"zones={cfg.zones}")
        check("port is the Unraid-safe 8095", cfg.listen_port == 8095,
              str(cfg.listen_port))
        check("chime duration matches the bundled file",
              cfg.chime.duration_seconds == 2.3, str(cfg.chime.duration_seconds))
        check("webhook token placeholder is present",
              cfg.webhook.token == "change-me", cfg.webhook.token)


def test_never_overwrites():
    print("\n2. An existing config is never touched")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        config_dir, chime_dir = fresh_install(tmp)
        config_path = config_dir / "config.yaml"

        mine = "zones:\n  - name: Kitchen\n    host: 10.0.0.9\nlisten_port: 9999\n"
        config_path.write_text(mine)
        (chime_dir / "my-chime.mp3").write_bytes(b"not really an mp3")

        bootstrap.run(config_path, chime_dir)

        check("config.yaml byte-identical", config_path.read_text() == mine)
        check("my settings survive", load_config(config_path).listen_port == 9999)
        check("default chime NOT added over my own",
              not (chime_dir / "doorbell.mp3").exists(),
              str([p.name for p in chime_dir.iterdir()]))


def test_readonly_mount_does_not_crash():
    print("\n3. A read-only config mount degrades instead of crash-looping")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _, chime_dir = fresh_install(tmp)
        missing = tmp / "nonexistent-parent" / "sub" / "config.yaml"

        # Simulate an unwritable mount by pointing at a path we make fail.
        real_mkdir = Path.mkdir

        def boom(self, *a, **k):
            raise OSError(30, "Read-only file system")

        Path.mkdir = boom
        try:
            created = bootstrap.seed_config(missing)
        finally:
            Path.mkdir = real_mkdir

        check("seeding reports failure rather than raising", created is False)
        check("no file was created", not missing.exists())
        # The entrypoint's fallback is what keeps the container alive.
        check("an empty Config is still constructible for the fallback",
              Config().is_configured is False)


def test_starter_config_is_complete():
    print("\n4. The starter config covers every setting the service reads")
    raw = yaml.safe_load(bootstrap.STARTER_CONFIG)
    for key in ("service_base_url", "listen_host", "listen_port", "zones",
                "chime", "behaviour", "webhook"):
        check(f"has {key}", key in raw)
    for key in ("debounce_seconds", "fade_ms", "group_policy",
                "restore_pause_state"):
        check(f"behaviour.{key} documented", key in raw["behaviour"])
    check("tells the user where to edit it without SSH",
          "appdata" in bootstrap.STARTER_CONFIG)


def main() -> int:
    test_seeds_empty_install()
    test_never_overwrites()
    test_readonly_mount_does_not_crash()
    test_starter_config_is_complete()

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
