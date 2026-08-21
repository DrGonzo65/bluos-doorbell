#!/bin/bash
# Pull the latest image from GHCR and recreate the container.
# Equivalent to clicking Apply Update on the Unraid Docker tab.
#
#   bash unraid/update.sh                      # uses $OWNER or prompts
#   OWNER=yourname bash unraid/update.sh

set -euo pipefail

NAME="bluos-doorbell"
APPDATA="${APPDATA:-/mnt/user/appdata/bluos-doorbell}"
OWNER="${OWNER:-}"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[31mxx\033[0m %s\n' "$*" >&2; exit 1; }

if [[ -z "$OWNER" ]]; then
    # Recover the owner from the running container so you don't have to retype it.
    OWNER="$(docker inspect --format '{{.Config.Image}}' "$NAME" 2>/dev/null \
             | sed -n 's#^ghcr\.io/\([^/]*\)/.*#\1#p')"
fi
[[ -n "$OWNER" ]] || die "set OWNER=<your github username> (no running container to read it from)"

IMAGE="ghcr.io/${OWNER,,}/${NAME}:latest"

before="$(docker inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || echo none)"

say "Pulling $IMAGE"
docker pull "$IMAGE"

after="$(docker inspect --format '{{.Id}}' "$IMAGE")"
if [[ "$before" == "$after" ]]; then
    say "Already on the latest image — nothing to do."
    exit 0
fi

say "New image. Recreating $NAME"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d \
    --name "$NAME" \
    --restart unless-stopped \
    --network host \
    -e DOORBELL_CONFIG=/config/config.yaml \
    -e DOORBELL_CHIME_DIR=/chimes \
    -e TZ="${TZ:-America/Los_Angeles}" \
    -l net.unraid.docker.managed=dockerman \
    -l net.unraid.docker.webui='http://[IP]:8095/health' \
    -v "$APPDATA/config:/config:ro" \
    -v "$APPDATA/chimes:/chimes:ro" \
    "$IMAGE" >/dev/null

PORT="$(grep -E '^listen_port:' "$APPDATA/config/config.yaml" 2>/dev/null | awk '{print $2}' | tr -d '"')"
PORT="${PORT:-8095}"

sleep 3
say "Now running:"
curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); b=d.get('build',{}); print('  build', b.get('git_sha','?')[:12], '|', b.get('built_at','?'), '| zones', d.get('zones'))" \
    || warn "service did not answer on :$PORT yet — check: docker logs $NAME"

say "Old images can be reclaimed with: docker image prune"
