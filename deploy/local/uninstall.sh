#!/bin/bash
# BRETHOF BRAIN — remove the memory's containers from this machine.
#
#   ./uninstall.sh                    stop and remove the containers, their
#                                     images and the boot unit; KEEP the memory
#                                     (data/, secrets/, .env, keys/, the hub's
#                                     devices and licence) so get.sh can bring
#                                     it back exactly as it was
#   ./uninstall.sh --delete-memory    also delete the memory and everything
#                                     in this folder — it cannot be undone
#
# Run it from the install folder (the one get.sh made).
set -euo pipefail
cd "$(dirname "$0")"
say()  { printf '\n== %s\n' "$*"; }
fail() { echo "UNINSTALL FAILED: $*" >&2; exit 1; }

DELETE=""
YES=""
for a in "$@"; do
  case "$a" in
    --delete-memory) DELETE=1 ;;
    --yes) YES=1 ;;
    *) fail "unknown option $a (use --delete-memory, and --yes to skip the question)" ;;
  esac
done
[ -f compose.yml ] && [ -f .env ] || fail "run this from the folder get.sh made (no compose.yml/.env here)"

# the same runtime get.sh chose
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ENGINE=docker; COMPOSE="docker compose"
elif command -v podman >/dev/null; then
  ENGINE=podman
  export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
  if command -v docker-compose >/dev/null; then COMPOSE="docker-compose"
  elif [ -x bin/docker-compose ]; then COMPOSE="$PWD/bin/docker-compose"
  else fail "no compose found (get.sh keeps one in ./bin)"; fi
else
  fail "need docker or podman"
fi
IS_PODMAN=""
[ "$ENGINE" = podman ] && IS_PODMAN=1
case "$(docker --version 2>&1)" in *[Pp]odman*) IS_PODMAN=1 ;; esac

if [ -n "$DELETE" ] && [ -z "$YES" ]; then
  [ -t 0 ] || fail "--delete-memory deletes your memory for good; add --yes to confirm when not typing at a terminal"
  echo "This DELETES the memory in $PWD — every conversation, record and setting. It cannot be undone."
  read -r -p "Type delete to go on: " ans
  [ "$ans" = delete ] || fail "not confirmed; nothing removed"
fi

PROJ=""
HUB_CID=$($COMPOSE ps -a -q brain-hub 2>/dev/null | head -1 || true)
[ -n "$HUB_CID" ] && PROJ=$($ENGINE inspect "$HUB_CID" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)
IMAGES=$($COMPOSE config --images 2>/dev/null || true)

if [ -n "$IS_PODMAN" ] && [ -n "$PROJ" ]; then
  UNIT="brethof-brain-$PROJ.service"
  say "boot unit $UNIT"
  systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/$UNIT"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

say "containers"
if [ -n "$DELETE" ]; then
  $COMPOSE down -v --remove-orphans >/dev/null || fail "compose down"
else
  $COMPOSE down --remove-orphans >/dev/null || fail "compose down"     # the hub's volume (devices, licence) stays
fi

if [ -n "$DELETE" ]; then
  say "the memory"
  HERE="$PWD"
  # the database files belong to the container's postgres user: remove them
  # from inside a container (before its image goes) so no sudo is needed
  STORAGE_IMG=$(printf '%s\n' $IMAGES | grep brain-storage | head -1 || true)
  if [ -d data ] && ! rm -rf data 2>/dev/null; then
    [ -n "$STORAGE_IMG" ] && $ENGINE run --rm -v "$HERE/data:/d" --entrypoint sh "$STORAGE_IMG" \
      -c 'rm -rf /d/* /d/.[!.]* 2>/dev/null; true' >/dev/null 2>&1 || true
    rm -rf data 2>/dev/null || echo "NOTE: $HERE/data could not be removed by this user; remove it with: sudo rm -rf $HERE/data"
  fi
fi

say "images"
for img in $IMAGES; do
  $ENGINE rmi "$img" >/dev/null 2>&1 && echo "removed $img" || true
done

if [ -n "$DELETE" ]; then
  rm -rf secrets keys model bin .env compose.yml install.sh
  cd .. && rm -f "$HERE/uninstall.sh" && rmdir "$HERE" 2>/dev/null || true
  echo "Deleted. Nothing of the memory is left on this machine${PROJ:+ (project $PROJ)}."
else
  cat <<TXT

  Removed the containers, their images${IS_PODMAN:+ and the boot unit}.
  Your memory is still here, in $PWD (data/, secrets/passphrase, .env, keys/).
  To bring it back:  curl -fsSL https://downloads.brethof.ai/brain/local3/get.sh | BRAIN_DIR=$PWD bash
  To delete it for good:  $PWD/uninstall.sh --delete-memory
TXT
fi
