# BluOS Doorbell

A small service that plays a doorbell chime on Bluesound Node players and then
puts everything back *exactly* the way it was — the right volume in every zone,
the right track at the right position — including when players are grouped.

It finds the players itself. There is no list of IP addresses to maintain.

Triggered by a UniFi Protect Alarm Manager webhook. No Control4 involvement.

## Why this exists

BluOS has no audio mixer, so a chime always means *interrupt the source, play
the sound, restore the source*. That part is easy. Doing the restore correctly
is where the stock drivers fall over, in two specific ways:

**Volume comes back wrong.** On a grouped player, `/Status` returns a copy of
the *primary's* status — including the primary's volume (API doc §2.2). A
driver that reads volume from `/Status` and writes it back flattens every zone
in the group to one level. This service reads each player's own `GET /Volume`
and restores each one individually with `tell_slaves=0`.

**It fails on grouped players.** Transport commands sent to a group secondary
are proxied to the primary (§8), so announcing "to the Kitchen" when Kitchen is
a group member takes over the whole group — and looping over all members fires
N overlapping takeovers of the same player. This service resolves the topology
from `/SyncStatus` first and sends exactly one chime per group primary.

There's also a third failure mode worth naming: a **double press**. If the
"previous volume" is captured on the second press, it captures the *already
ducked* volume, and the restore leaves everything quiet. An in-flight gate here
makes that impossible — a ring during an active sequence repeats the chime
rather than starting a second capture.

## What it does, per ring

1. Reads `/SyncStatus` on every known player and resolves group topology.
2. Captures `/Status` on each group primary and `/Volume` on every individual
   member.
3. Ramps each member down to its chime volume (`tell_slaves=0`).
4. Sends one `/Play?url=<chime>` per group primary.
5. Waits for the chime, then restores the source:
   - play queue → `/Play?id=<song>&seek=<secs>`
   - radio / service stream → `/Play?url=<captured streamUrl>`
   - physical input → `/Play?inputTypeIndex=<input>`
   - was stopped → left stopped
6. Ramps each member's volume back to its captured level, individually.

## Setup

Running on Unraid? [`unraid/INSTALLING.md`](unraid/INSTALLING.md) covers a
GUI-only install with no shell access, and [`unraid/UPDATING.md`](unraid/UPDATING.md)
the update flow — CI publishes an image to GHCR and the box pulls it, so updating
is one click on the Docker tab. The container seeds its own config and chime on
first run.

```bash
docker compose up -d --build
```

That's the whole setup. The container writes its own `config.yaml`, finds every
BluOS player on the network, and chimes in all of them. Set a webhook token in
the config and you're done — there are no IP addresses to enter.

See what it found:

```
http://<docker-host>:8095/discover?token=<your-token>
```

Then check the service sees everything correctly:

```bash
curl -s http://<docker-host>:8095/inspect | jq
```

`/inspect` shows each player's **own** volume, its group role, and the exact
`restore_plan` the service would use for its current source. Play something
different in each zone and re-run it — that's the fastest way to confirm the
restore logic covers all the sources you actually use.

Fire the whole sequence by hand:

```bash
curl -X POST "http://<docker-host>:8095/test/chime?token=<your-token>"
```

## Wiring up UniFi Protect

In the UniFi Protect app: **Alarm Manager → Create Alarm**

- **Trigger:** Doorbell Ring, scoped to your doorbell camera
- **Action:** Webhook
- **URL:** `http://<docker-host>:8095/doorbell?token=<your-token>`
- **Method:** POST

Ring the doorbell once and check the container logs. If Protect can't reach the
Docker host, they're probably on different VLANs — either open the firewall for
that one destination or move the service somewhere both sides can see.

The `webhook.allowed_devices` list is an optional second filter, matched as a
case-insensitive substring against the Protect payload, so a broader alarm rule
can't ring the house by accident.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /doorbell?token=…` | Webhook target. Also accepts GET. |
| `GET /health` | Liveness, discovered players, the build's git sha, and the last ring. |
| `GET /inspect` | Per-player state, group topology, and restore plans. |
| `GET /discover?token=…` | What discovery currently knows. `?rescan=1` forces a fresh scan. |
| `POST /test/chime?token=…` | Run the full sequence, bypassing debounce. |
| `GET /chimes/<file>` | Serves the chime to the players. |

## Discovery

On by default. The service keeps a live picture of the network from three
sources, cheapest first:

- a **passive listener** on udp/11430 — BluOS players announce themselves about
  every 57 seconds, so a newly plugged-in player is picked up within a minute
- a **periodic LSDP query** asking everything to announce now
- a **subnet sweep** over HTTP when the first two come back empty, and
  occasionally afterwards to catch players whose broadcasts don't reach the
  server

Players that stop answering for 15 minutes are dropped; renames are picked up
automatically. `GET /health` shows what's known and how each was found.

`zones:` in `config.yaml` is for **exceptions only** — you never list players
just to include them:

```yaml
discovery:
  exclude: ["Garage"]           # never chime here

zones:
  - name: Primary Bedroom       # matched by name, everything else stays automatic
    chime_when_idle: false
    chime_volume: 20

  - name: Back Deck             # a player discovery can't see, pinned by hand
    host: 192.168.1.60
```

Set `discovery.auto: false` to ignore the network entirely and use only the
zones you list.

If discovery finds nothing, the subnet guess is the usual cause —
`/health` reports the host address it derived, and `discovery.subnet` overrides
it. Discovery needs host networking; on a bridge network the sweep still works
but players can't fetch the chime, so host networking is required regardless.

## Tuning

The settings you'll actually touch are in `config.yaml`:

- **`chime.duration_seconds`** — must match your file's real length. Too short
  clips the chime; too long leaves a silent gap before the music returns. The
  bundled `doorbell.mp3` is 2.3s.
- **`chime.default_volume`** / per-zone `chime_volume` — how loud the chime is,
  independent of what the music was doing.
- **`behaviour.fade_ms`** — 300ms feels intentional; 0 is a hard cut.
- **`behaviour.debounce_seconds`** — rings inside this window are ignored.
- **`chime_when_idle: false`** — per zone, so a silent bedroom stays silent.
- **`discovery.exclude`** — players that should never chime.

## Known limits

**No true ducking.** Music cannot continue underneath the chime — BluOS has no
mixer or overlay bus. What you get is fade down → chime → fade up. Mixing a
chime over live music would have to happen upstream in an amp or matrix.

**Resume isn't seamless.** Expect a 1–3 second gap while the source re-buffers.
Local library and play-queue sources restore cleanly. Streaming services vary,
and live radio can't be seeked at all — it restarts at the live edge. Test each
source you actually use via `/inspect` before assuming it's fine.

**Groups are all-or-nothing.** If a target zone is a secondary in a group, the
whole group hears the chime — that's how BluOS routes audio, not a choice this
service makes. Set `group_policy: skip` if you'd rather leave such groups alone.

## Working on it locally

```bash
make venv      # create .venv and install dependencies
make test      # run all four suites
make discover  # find players on this network
make run       # run the service against config/config.yaml
```

Dependencies are version ranges, not exact pins, so they install on whatever
Python you have. The container pins its base image instead, which is what
actually makes builds reproducible.

## Tests

```bash
make test
```

Four suites cover the choreography, first-run seeding, the LSDP wire format, and
the discovery/override merge. The main one runs the full choreography against
mock BluOS players that reproduce the real quirks — secondaries mirroring the primary's `/Status`, transport commands
proxying to the primary. Covers queue restore with seek, grouped save/restore,
double-press, radio streams, idle opt-out, fixed-volume players, and one dead
player not blocking the rest.

## Layout

```
app/__main__.py      entrypoint — binds host/port from config.yaml
app/bluos.py         BluOS API client
app/orchestrator.py  capture → duck → chime → restore
app/config.py        config schema
app/main.py          FastAPI service and webhook
app/discovery.py     live player registry: listener, refresh, override merge
tools/lsdp.py        Lenbrook Service Discovery Protocol (BluOS's own)
tools/discover.py    player discovery: LSDP, then mDNS, then a subnet sweep
app/bootstrap.py     first-run seeding of config.yaml and the default chime
.github/workflows/   CI: run tests, publish the image to GHCR
Makefile             make venv / test / run / discover / docker
templates/           Unraid Docker template (also used for a CA listing)
unraid/              install and update guides, compose files, scripts
tests/               mock players and end-to-end tests
chimes/doorbell.mp3  2.3s two-tone chime
```
