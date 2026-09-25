# BluOS Doorbell

A local service that plays doorbell chimes on BluOS speakers, then restores their
previous playback and individual volumes. Trigger it with UniFi Protect webhooks
or another automation that can send an HTTP request.

Open **`http://<server>:8095/`** to manage everything in the browser. Speakers are
discovered automatically. **New speakers stay disabled until you enable them.**
No speaker addresses or chime durations need to be entered in a config file.

## Getting started

```bash
docker compose up -d --build
```

On Unraid, use the [installation guide](unraid/INSTALLING.md). The container uses
host networking and persistent folders for `/config` and `/chimes`. It seeds a
starter configuration and bundled sounds on first launch.

1. Open `http://<server>:8095/` and unlock with your existing webhook token.
   A fresh installation uses `change-me` until you replace it in Settings.
2. In **Rooms**, enable the discovered speakers that should chime. Set each
   room's volume or leave it blank to use the shared default.
3. In **Sound library**, upload an MP3 or use a bundled sound. Duration is
   measured from the file automatically. Preview plays on your browser's device.
4. In **Doorbells**, choose a sound for the default doorbell. Add another
   doorbell (for example `back`) and select its own sound.
5. **Save changes**, then copy each webhook URL into its doorbell automation.
   Use **Test on speakers** to check the saved configuration.

No restart is needed for room choices, chimes, playback settings, tokens, or
discovery settings. Changing the server's listening address or port requires a
container restart; the interface tells you when one is needed.

## Browser settings

- **Rooms:** enable/disable, per-room volume, idle and muted behavior. Rooms
  appear automatically and the page refreshes its list while there are no
  unsaved edits. Missing rooms keep their saved preferences.
- **Doorbells:** add/remove named webhooks, select sounds, set extra settling
  time, copy authenticated URLs, and test playback.
- **Sound library:** upload, preview, and delete unused MP3 files. Uploads are
  limited to 12 MB and 60 seconds. Invalid audio is rejected. Each upload gets
  a unique filename, so it cannot overwrite a sound currently in use.
- **Settings:** global volume, fades, debounce, pause restoration, group policy,
  timeouts, discovery subnet and refresh intervals, service URL, listening
  address/port, log level, webhook token and allowed devices.

**Grouped speakers share playback.** Disabling one room does not isolate it
from an enabled room in the same BluOS group. The whole group may hear the
chime. Ungroup rooms in BluOS if they need independent control. The interface
shows this limitation next to the room controls. `group_policy: skip` only
skips a secondary whose primary is not itself a selected room.

The existing mute setting permits attempting playback on a muted group; the
service does not explicitly unmute the hardware, so audibility depends on the
player's behavior.

## Where settings live

The discovered player inventory lives in memory and is rebuilt automatically.
It is **never written into `config.yaml`**. Browser choices are saved atomically
in `/config/settings.json`, alongside the existing YAML. Keep `/config` and
`/chimes` on persistent storage and include both in backups.

The YAML supplies initial/default settings. Once saved in the browser,
`settings.json` takes precedence. The browser uses automatic discovery and
clears legacy manual zone entries from its managed settings; the original YAML
is left untouched. Existing YAML room overrides are reflected for discovered
rooms when first opening the interface. A manually pinned player that cannot
be discovered is not retained by the browser configuration.

Room preferences use the player's reported MAC address when available, so a
rename or DHCP address change preserves them. Players without that identity
use their room name as a fallback; renaming those players requires selecting
them again. A newly discovered device is disabled by default. The interface
also offers an explicit option to automatically enable future discoveries.

Settings saves are rejected while a ring is in progress, so a restore cannot
switch configuration midway through playback. A revision check prevents a
second browser window from silently overwriting newer settings.

## Two doorbells

The default webhook remains:

```text
http://<server>:8095/doorbell?token=<your-token>
```

An additional doorbell named `back` uses:

```text
http://<server>:8095/doorbell/back?token=<your-token>
```

Both ring the same selected rooms at the same volumes, with different sounds.
In Protect, create one Alarm Manager rule per camera with a Doorbell Ring
trigger and an HTTP POST action using its own URL.

If the other doorbell rings during a chime, it queues its sound before the
original playback is restored. Debounce is per doorbell. Unknown names on
webhook routes fall back to the default sound and log a warning; the manual
test endpoint returns a 404 for unknown names.

Changing your webhook token invalidates old URLs. Save the new token and copy
the replacement URLs into your automations. The browser keeps the login token
in session storage for the current tab; **Lock settings** clears it.

## How playback works

1. Resolve groups from `/SyncStatus`.
2. Capture each primary's playback state and every member's own `/Volume`.
3. Fade members to their chime levels using `tell_slaves=0`.
4. Send one `/Play?url=...` to each primary. Players fetch the MP3 from this service.
5. Wait for the measured duration plus settling time, playing any queued chimes.
6. Restore the source and each individual volume.

Queued tracks resume at the captured position when seek is available. Streams
reopen their captured URL; physical inputs are reselected. Paused sources are
paused again after restoration, and stopped players are stopped.

This interrupts audio; it does not mix the chime over continuing music.
Streaming restoration is source-dependent and may need to buffer. State is
held in memory, so a process crash cannot recover an interrupted snapshot.

## Discovery and networking

The service listens for LSDP announcements on UDP 11430, periodically queries
for players, and occasionally sweeps a subnet using HTTP on port 11000.
The command-line discovery tool additionally supports mDNS.

In the interface, leave the subnet blank for automatic detection or enter a
CIDR such as `192.168.20.0/24`. Scans larger than a /20 are refused. The **Find
speakers** button forces a scan, including when no rooms are enabled yet.

Protect must reach the service's HTTP port; the service must reach speakers;
and speakers must reach the service to download the chime. If automatic URL
detection picks the wrong interface, set **Player-facing service URL** in the
browser. Deployment files use host networking for broadcast discovery.

## HTTP endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Browser interface. |
| `GET/POST /doorbell` | Default doorbell webhook. |
| `GET/POST /doorbell/{name}` | Named doorbell webhook. |
| `GET/POST /test/chime` | Test; accepts `doorbell` and repeated `zone` parameters. |
| `GET /health` | Public basic status; token unlocks details. |
| `GET /inspect` | Player state, group topology, and restoration plan. |
| `GET /discover` | Legacy discovery diagnostics. |
| `GET /chimes/<file>` | Public audio for the speakers. |
| `GET/PUT /api/settings` | Browser settings and room inventory. |
| `POST /api/discovery/refresh` | Browser discovery refresh. |
| `POST /api/chimes?filename=...` | Raw MP3 upload with automatic duration detection. |
| `DELETE /api/chimes/{filename}` | Remove an unassigned sound. |

Browser API requests use `X-Doorbell-Token`; writes also require
`X-Doorbell-Admin: 1`. Webhook/diagnostic routes accept the token as a query
parameter or header. A blank token disables authentication; the browser warns
about blank and starter tokens. Audio must remain public for speaker fetching.

## Development and tests

```bash
make venv
make test
make run
```

Open `http://localhost:8095/`. The local run uses `config/config.yaml`,
`config/settings.json`, and `chimes/`. `DOORBELL_SETTINGS` can override the
managed-settings path for isolated testing.

Nine suites cover playback, startup seeding, LSDP, discovery, authorization,
subnets, room tests, multiple doorbells, and browser administration. The latter
covers room opt-in, identity changes, persistence, measured uploads, invalid
files, limits, token rotation, conflicting edits, and storage failures.
Playback suites run against simulated speakers rather than real hardware.

GitHub Actions runs the suites before publishing an AMD64 container to GHCR.
See [updating on Unraid](unraid/UPDATING.md). Build information is available on
`/health` and in the browser's service status section.

## Code map

- `app/main.py`: service lifecycle, webhooks, and diagnostics.
- `app/admin.py`, `app/web/`: settings API and self-contained browser interface.
- `app/config.py`: configuration models and persisted settings overlay.
- `app/discovery.py`: live registry, identity lookup, and room preferences.
- `app/orchestrator.py`: capture, chime queue, and restoration.
- `app/bluos.py`: async XML API client.
- `app/bootstrap.py`, `app/__main__.py`: first-run setup and server entrypoint.
- `tools/`: discovery CLI, LSDP packets, and subnet utilities.
