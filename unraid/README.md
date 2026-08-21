# Running on Unraid

**Start with [INSTALLING.md](INSTALLING.md)** — installing from the Unraid GUI
with no shell access at all, which is what you want for a first install.

**For the ongoing update flow, see [UPDATING.md](UPDATING.md)** — GitHub Actions
publishes an image to GHCR and Unraid pulls it, so no source lands on the host
and updates are one click on the Docker tab. That's the recommended setup.

The routes below build the image on the Unraid box instead. Useful for the
first install before CI exists, or for testing a change without pushing.

Whichever you pick, **host networking is required**. The players have to reach
this service to fetch the chime file, and auto-discovery listens for their UDP
announcements on port 11430 — broadcast doesn't cross a bridge network. On a
bridge the subnet sweep would still find players, but they couldn't fetch the
chime, so there's no way around it.

---

## Route 1 — the install script (fastest)

Clone or copy the repo somewhere persistent on the array — `/mnt/user/appdata`
is fine, or a `projects` share. **Not** `/tmp` or `/root`, which don't survive
a reboot.

```bash
cd /mnt/user/appdata/bluos-doorbell-src   # wherever you put the repo
bash unraid/install.sh
```

It creates `/mnt/user/appdata/bluos-doorbell/{config,chimes}`, seeds
`config.yaml` and the chime, builds the image, and starts the container with
`--restart unless-stopped`. Re-running it rebuilds and restarts without
touching your `config.yaml`.

Then edit the config and restart:

```bash
nano /mnt/user/appdata/bluos-doorbell/config/config.yaml   # set webhook.token
docker restart bluos-doorbell
```

---

## Route 2 — Compose Manager plugin

Install **Compose Manager** from Community Applications, then:

```bash
mkdir -p /mnt/user/appdata/bluos-doorbell/config /mnt/user/appdata/bluos-doorbell/chimes
cp config/config.example.yaml /mnt/user/appdata/bluos-doorbell/config/config.yaml
cp chimes/doorbell.mp3 /mnt/user/appdata/bluos-doorbell/chimes/

docker compose -f unraid/docker-compose.yml up -d --build
```

The compose file carries the `net.unraid.docker.managed=composeman` label, so
the container shows up properly on the Docker tab.

---

## Route 3 — Unraid Docker template (GUI)

Build the image first, then install the template:

```bash
docker build -t bluos-doorbell:latest /path/to/repo
cp templates/bluos-doorbell.xml /boot/config/plugins/dockerMan/templates-user/
```

Docker tab → **Add Container** → pick `bluos-doorbell` from the Template
dropdown. The appdata paths are pre-filled, and the container seeds its own
`config.yaml` and chime on first run — leave those folders empty.

Note this route doesn't rebuild the image for you. After changing the code,
re-run `docker build` and hit **Force Update** in the GUI.

---

## Unraid-specific gotchas

**Port 8095, not 8080.** The default is 8095 because 8080 collides with
something on nearly every Unraid box. The service binds whatever `listen_port`
in `config.yaml` says — change it there and restart, nothing else to edit.
Check what's free with:

```bash
netstat -tlnp | grep -E ':(8080|8095)'
```

**Appdata on the array, not the cache-only pool.** Nothing here is
write-heavy, so either is fine, but keep it wherever your other appdata lives
so it's covered by your usual backup.

**Rebuilding wipes nothing.** Config and chimes are bind-mounted from appdata,
so `docker build` and re-`run` never touch them. That's the whole reason they
live outside the image.

**Adding your own chime.** Drop the file in
`/mnt/user/appdata/bluos-doorbell/chimes/`, then set `chime.file` and — this
one matters — `chime.duration_seconds` to the file's real length. Too short
clips the chime; too long leaves a silent gap before the music comes back.

```bash
docker exec bluos-doorbell python -c "print(open('/chimes/doorbell.mp3','rb').read().__len__())"
```

or just check it in any audio player.

**If Protect and Unraid are on different VLANs**, the webhook has to be allowed
through. Same for the players fetching the chime — if they're on an IoT VLAN
that can't reach the server VLAN, set `service_base_url` explicitly to an
address they *can* reach, and open that one port.

---

## Verifying it works

```bash
IP=$(ip route get 1.1.1.1 | awk '{print $7; exit}')

curl -s "http://$IP:8095/health" | jq
curl -s "http://$IP:8095/inspect" | jq '.players[] | {name, role, own_volume, state, restore_plan}'
curl -s -X POST "http://$IP:8095/test/chime?token=<your-token>"
```

`/inspect` is the one to lean on. It shows each player's **own** volume, its
group role, and the exact command the service would use to restore whatever
that zone is currently playing. Start something different in each zone — local
library, a streaming service, radio, a physical input — and re-run it. That
tells you which sources restore cleanly before you rely on it.

## Logs

```bash
docker logs -f bluos-doorbell
```

A successful ring looks like:

```
captured Kitchen: state=play source=queue members={'Kitchen': 58, 'Dining': 31}
restored Kitchen + Dining
```

If a zone shows up in `skipped`, it was unreachable, idle with
`chime_when_idle: false`, or a group secondary under an untargeted primary.
