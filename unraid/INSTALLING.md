# Install on Unraid

In **Docker → Add Container → Advanced View**, enter:

| Field | Value |
|---|---|
| Name | `bluos-doorbell` |
| Repository | `ghcr.io/drgonzo65/bluos-doorbell:latest` |
| Network Type | Host |
| WebUI | `http://[IP]:8095/` |

Add these persistent paths, both **Read/Write**:

| Container path | Host path |
|---|---|
| `/config` | `/mnt/user/appdata/bluos-doorbell/config` |
| `/chimes` | `/mnt/user/appdata/bluos-doorbell/chimes` |

Click **Apply**, then open the WebUI. The container creates starter settings
and bundled sounds. On a fresh installation, unlock with `change-me`; on an
existing one, use the webhook token you already configured.

## Configure in the browser

1. Open **Settings → Access & webhook security**, generate a token, and save.
2. Under **Rooms**, wait for speakers to appear or click **Find speakers**.
   Enable the rooms you want; new rooms are silent by default.
3. Upload MP3s under **Sound library**. Duration is detected automatically.
4. Select a sound for each doorbell. Add another doorbell for another camera.
5. Save, then copy each webhook URL to its Protect Alarm Manager rule.

There is no need to enter speaker IPs, measure audio files, or edit YAML.
Most changes apply immediately. Only changing the listening address or port
requires restarting the container. If you change the port, also update the
Unraid WebUI field to match.

## Existing installations

Update the image after this version is published. Change your Unraid WebUI
link from `/health` to `/` to open the settings page. Existing tokens and chime
choices are loaded from the old configuration. Select your rooms and save.

The browser writes `/config/settings.json`, which takes precedence over the
old YAML while leaving it untouched. Discovered nodes are never written into
the YAML. Browser-managed room choices use hardware identity when available.
Back up both persistent folders.

Rooms that belong to the same BluOS group share audio. A disabled room may
still hear an enabled group member's chime. Ungroup rooms in BluOS for
independent control.

## If speakers do not appear

Use **Settings → Network & discovery** to set the subnet, for example
`192.168.20.0/24`, then save and scan again. Blank means automatic detection;
the maximum scan is a /20.

Allow the service to reach speakers on TCP 11000 and the speakers to reach
the service on TCP 8095 (or your chosen port). Discovery broadcasts use UDP
11430. If the players are on another VLAN, the subnet scan can find them when
HTTP routing is allowed. Set the player-facing service URL if automatic
address detection chooses the wrong network interface.

## Updates

The repository includes the browser interface, but your running container only
gets it after a new image is published and installed. Use **Check for Updates →
Apply Update** once that image is available. Config and audio persist across
updates. See [UPDATING.md](UPDATING.md) for the build and release flow.
