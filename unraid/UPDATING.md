# Keeping the Unraid host updated

The Unraid box never sees the source. GitHub Actions builds a container image
on every push to `main` and publishes it to GHCR; Unraid pulls that image.

```
  Mac                GitHub                     GHCR              Unraid
  git push  ──▶  test, then build  ──▶  ghcr.io/OWNER/...  ──▶  Apply Update
```

Config and chimes live in `/mnt/user/appdata/bluos-doorbell/`, mounted into the
container. An update replaces the code and never touches them.

Replace `OWNER` throughout with your GitHub username, **lowercase** — GHCR paths
are always lowercase even when your username isn't.

---

## One-time setup

### 1. Push to a private GitHub repo

```bash
cd ~/projects/bluos-doorbell
gh repo create bluos-doorbell --private --source=. --remote=origin --push
```

No secrets to configure. The workflow authenticates with the built-in
`GITHUB_TOKEN` and its `packages: write` permission, already declared in
`.github/workflows/build.yml`.

Watch the first run:

```bash
gh run watch
```

### 2. Make the *package* public (recommended)

This is the step that saves you real pain. GitHub lets a container package be
public even when its source repo is private — visibility is set per-package,
not inherited. A public package means Unraid pulls anonymously: no token on the
box, no credentials to persist across reboots, and Unraid's "Check for Updates"
just works.

GitHub → your profile → **Packages** → `bluos-doorbell` → **Package settings** →
**Change visibility** → Public.

Your code stays private. What becomes public is the built image — which holds
this application code and nothing else. Your `config.yaml`, with the player IPs
and webhook token, is gitignored and lives only in appdata; it is never in the
image.

If you'd rather keep the package private too, see [Private package](#private-package)
at the bottom — it works, it's just more moving parts.

### 3. Install on Unraid

```bash
mkdir -p /mnt/user/appdata/bluos-doorbell/config /mnt/user/appdata/bluos-doorbell/chimes
# copy your config.yaml and chime into those two folders
```

Then either the template (GUI) or compose:

```bash
# GUI route — edit OWNER in the file first (or see INSTALLING.md, no SSH)
cp templates/bluos-doorbell.xml /boot/config/plugins/dockerMan/templates-user/
# Docker tab -> Add Container -> pick bluos-doorbell from the Template dropdown

# or compose route — edit OWNER in the file first
docker compose -f unraid/docker-compose.ghcr.yml up -d
```

---

## The update loop, from here on

```bash
# on your Mac
git commit -am "shorter fade"
git push
```

CI runs the behavioural tests first and **only publishes if they pass** — a
broken build can't reach your doorbell. Then on Unraid:

**Docker tab → Check for Updates → Apply Update.**

That's it. Same one-click flow as every other container on the box.

Prefer the CLI:

```bash
OWNER=yourname bash unraid/update.sh
```

It pulls, skips the restart entirely if the image is unchanged, recreates the
container, and prints the running build.

### Confirming the update actually landed

The image is stamped with its git sha at build time, so you can check rather
than assume:

```bash
curl -s http://<unraid-ip>:8095/health | jq .build
```

```json
{ "git_sha": "a3f8c19...", "built_at": "2026-08-16T18:22:04Z" }
```

Compare against `git rev-parse HEAD` on your Mac. If it hasn't moved, the pull
didn't take — usually a cached `latest` or the container wasn't recreated.

### Rolling back

Every build is also tagged with its short sha, so a bad update is one edit away
from undone. Change the Repository field in the Unraid container to
`ghcr.io/OWNER/bluos-doorbell:sha-abc1234` and apply. Tag releases if you'd
rather pin to versions:

```bash
git tag v1.1.0 && git push --tags     # also publishes :1.1.0 and :1.1
```

---

## Why not the other options

**Rebuilding from source on Unraid** (`git pull && docker compose up -d --build`)
needs git on the box via the NerdTools plugin, puts a build toolchain on your
server, and — the real problem — Unraid's update button doesn't work for
locally built images, so you're back to SSHing in every time.

**Watchtower** auto-updating is tempting but wrong here. A doorbell that goes
silent because a build landed unattended at 3am is worse than one that's a week
behind. You said the same, and I agree.

**A bare repo on Unraid with a post-receive hook** is a fine homelab pattern and
fully local, but it's a build server on your NAS and it still can't use the
update button. Worth revisiting only if you want to drop GitHub entirely.

---

## Private package

If you keep the package private, Unraid needs credentials, and the wrinkle is
that Unraid's root filesystem is in RAM — `docker login` writes
`/root/.docker/config.json`, which is **gone after a reboot**, and your
container silently stops being updatable.

1. Create a classic PAT with only `read:packages`
   (GitHub → Settings → Developer settings → Tokens (classic)).

2. Log in on Unraid:

   ```bash
   echo '<token>' | docker login ghcr.io -u <github-username> --password-stdin
   ```

3. Make it survive reboots by persisting the config to the flash drive and
   restoring it at boot. Add to `/boot/config/go`, before the emhttp line:

   ```bash
   mkdir -p /root/.docker
   cp /boot/config/docker-auth.json /root/.docker/config.json
   ```

   and save the file you just generated:

   ```bash
   cp /root/.docker/config.json /boot/config/docker-auth.json
   ```

   That file contains a base64 credential — it's on the flash drive, so treat
   the flash backup accordingly.

Given that the alternative is one click on a settings page, a public package is
the better trade for a project with no secrets in it.

---

## Sources

- [Working with the Container registry — GitHub Docs](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
