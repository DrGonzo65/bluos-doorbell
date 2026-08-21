#!/bin/bash
# One-shot installer for Unraid. Run from the repo root:
#
#   bash unraid/install.sh
#
# Creates the appdata folders, seeds config.yaml and the chime, builds the
# image, and starts the container. Safe to re-run — it never overwrites an
# existing config.yaml.

set -euo pipefail

APPDATA="${APPDATA:-/mnt/user/appdata/bluos-doorbell}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="bluos-doorbell"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[31mxx\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found — is this actually an Unraid box?"
[[ -d /mnt/user ]] || warn "/mnt/user not found; using APPDATA=$APPDATA anyway"

say "Creating $APPDATA"
mkdir -p "$APPDATA/config" "$APPDATA/chimes"

if [[ -f "$APPDATA/config/config.yaml" ]]; then
    say "config.yaml already exists — leaving it alone"
else
    cp "$REPO/config/config.example.yaml" "$APPDATA/config/config.yaml"
    warn "Seeded $APPDATA/config/config.yaml from the example."
    warn "Edit it with your player IPs and a webhook token BEFORE this is useful."
fi

for f in "$REPO"/chimes/*; do
    [[ -e "$f" ]] || continue
    base="$(basename "$f")"
    if [[ -f "$APPDATA/chimes/$base" ]]; then
        say "chime $base already present"
    else
        cp "$f" "$APPDATA/chimes/$base"
        say "Installed chime $base"
    fi
done

say "Building image $NAME:latest"
docker build -t "$NAME:latest" "$REPO"

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    say "Removing the existing container"
    docker rm -f "$NAME" >/dev/null
fi

say "Starting $NAME"
docker run -d \
    --name "$NAME" \
    --restart unless-stopped \
    --network host \
    -e DOORBELL_CONFIG=/config/config.yaml \
    -e DOORBELL_CHIME_DIR=/chimes \
    -e TZ="${TZ:-America/Los_Angeles}" \
    -l net.unraid.docker.managed=dockerman \
    -l net.unraid.docker.webui='http://[IP]:8095/health' \
    -v "$APPDATA/config:/config" \
    -v "$APPDATA/chimes:/chimes" \
    "$NAME:latest" >/dev/null

PORT="$(grep -E '^listen_port:' "$APPDATA/config/config.yaml" | awk '{print $2}' | tr -d '"')"
PORT="${PORT:-8095}"
HOST_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
HOST_IP="${HOST_IP:-<unraid-ip>}"

sleep 2
say "Container status:"
docker ps --filter "name=$NAME" --format '  {{.Names}}  {{.Status}}'

cat <<EOF

$(say "Done.")

  Config:   $APPDATA/config/config.yaml
  Chimes:   $APPDATA/chimes/
  Health:   http://$HOST_IP:$PORT/health
  Inspect:  http://$HOST_IP:$PORT/inspect
  Webhook:  http://$HOST_IP:$PORT/doorbell?token=<your-token>

  Logs:     docker logs -f $NAME
  Restart:  docker restart $NAME        (after editing config.yaml)

If the port is already taken on this box, change listen_port in config.yaml
and restart — the service binds whatever the config says.
EOF
