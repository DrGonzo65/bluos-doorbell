# Installing without touching a shell

Short answer to "can I just get it from Apps?": **yes, eventually — but you
almost certainly don't need to.** Route 1 below gets you a GUI install and
one-click updates in about two minutes, with no SSH and no submission process.

The container now seeds its own `config.yaml` and chime on first run, so an
empty appdata folder is all it needs. That was the only thing that used to
require a shell.

---

## Route 1 — Add Container in the GUI (start here)

No files to copy anywhere. Unraid writes the template for you.

**Docker tab → Add Container**, switch to **Advanced View**, and fill in:

| Field | Value |
|---|---|
| Name | `bluos-doorbell` |
| Repository | `ghcr.io/drgonzo65/bluos-doorbell:latest` |
| Network Type | **Host** |
| WebUI | `http://[IP]:8095/health` |

Add two paths with **Add another Path, Port, Variable**:

| Config Type | Name | Container Path | Host Path | Access Mode |
|---|---|---|---|---|
| Path | Config | `/config` | `/mnt/user/appdata/bluos-doorbell/config` | **Read/Write** |
| Path | Chimes | `/chimes` | `/mnt/user/appdata/bluos-doorbell/chimes` | **Read/Write** |

Read/Write matters — the container writes the starter config on first run.

Click **Apply**. It creates the folders, writes a starter `config.yaml`,
installs the default chime, and comes up reporting `unconfigured`.

Then edit the config **from Finder**, no shell involved — appdata is an SMB
share:

```
\\<tower>\appdata\bluos-doorbell\config\config.yaml
```

Add your players, set a webhook token, save, and hit **Restart** on the
container. `http://<tower>:8095/health` should flip from `unconfigured` to
`ok`.

From then on, **Check for Updates → Apply Update** works exactly like every
other container, because the Repository field points at a registry tag. Unraid
saves what you entered as a private template, so this is a one-time setup.

---

## Route 2 — drop the template file on the flash share

Same result as Route 1, but the fields come pre-filled. Still no SSH — the
flash drive is an SMB share too.

1. Copy `templates/bluos-doorbell.xml` in Finder to:
   ```
   \\<tower>\flash\config\plugins\dockerMan\templates-user\
   ```
3. **Docker → Add Container →** pick `bluos-doorbell` from the **Template**
   dropdown. Everything is filled in. Click Apply.

If the flash share isn't visible in Finder, it's disabled in **Settings → SMB
→ Flash share**. Turning it on temporarily is still easier than SSH.

---

## Route 3 — a real Community Applications listing

This is the "get it from the Apps tab" answer. It's real, and the process is
now self-service via [ca.unraid.net/submit](https://ca.unraid.net/submit) — a
live scan parses your templates and `ca_profile.xml`, shows a preview, and
flags problems before you submit.

The repo already carries what's needed:

- `LICENSE` — MIT, an OSI-approved licence (required)
- `ca_profile.xml` — repository metadata (required, at the repo root)
- `templates/bluos-doorbell.xml` — the app template

**The catch: the repository must be public.** You picked private earlier, and
that's a fine choice — CA is a public catalogue of apps for everyone, so
listing there means publishing the project. Only worth it if you actually want
other BluOS-plus-Unraid owners to find and use this.

If you do go that way:

1. Make the repo public, and the GHCR package public.
2. Run the live scan at [ca.unraid.net/submit](https://ca.unraid.net/submit)
   and fix whatever it reports. Treat the portal as the authority on format —
   the scaffolding here is a starting point, not a guarantee.
3. Submit and wait for it to land in the catalogue.

What that buys you over Route 1: discoverability for other people, and a
one-click install on any future Unraid box. What it costs: publishing the
code, and a moderation round-trip. For a single server, Route 1 gives you the
identical end state — a GUI-managed container with working update checks.

---

## Which to pick

| | Route 1 | Route 2 | Route 3 |
|---|---|---|---|
| SSH needed | no | no | no |
| Setup time | ~2 min | ~2 min | days, incl. review |
| Repo can stay private | yes | yes | **no** |
| Shows in Apps tab | no | no | yes |
| Update button works | yes | yes | yes |

Route 1 unless you specifically want to publish this for other people.

---

## Finding your players without a shell

Once the container is up, open this in a browser:

```
http://<tower>:8095/discover?token=<your-token>
```

It returns a ready-to-paste `zones:` block. Copy it into `config.yaml` over the
SMB share and restart the container.

If it comes back with `count: 0`, the subnet guess was probably wrong — the
response includes `this_host`, so try the matching range explicitly:

```
http://<tower>:8095/discover?token=<your-token>&subnet=192.168.1
```

### If discovery finds nothing anywhere

Discovery is only a convenience — the service talks to players over ordinary
HTTP, so a player that discovery can't see still works fine once you put its IP
in `config.yaml` by hand. Get IPs from the BluOS app under
**Settings → Player → Network**, and confirm reachability with:

```bash
curl http://<player-ip>:11000/SyncStatus
```

If that returns XML, you're done — paste the IP into the config regardless of
what discovery says.

Common reasons broadcast discovery comes back empty:

- **macOS 15+ Local Network permission.** Terminal needs to be allowed under
  System Settings → Privacy & Security → Local Network. Without it, multicast
  and broadcast are silently dropped and every discovery tool returns nothing.
- **BluOS doesn't reliably advertise over mDNS.** Bluesound uses their own
  protocol (LSDP, UDP 11430), which is why a Bonjour browse can be empty with
  players sitting right there. The tool tries LSDP first now.
- **Client isolation / multicast filtering** on the WiFi SSID the players are
  joined to. UniFi calls it "Client Device Isolation" and Multicast Enhancement.
- **Different VLANs.** Broadcast and mDNS don't route. The subnet sweep still
  works if the VLANs can reach each other over HTTP.
