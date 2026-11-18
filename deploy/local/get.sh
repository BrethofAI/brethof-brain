#!/bin/bash
# BRETHOF BRAIN — install the whole memory on this machine, one command:
#
#   curl -fsSL https://downloads.brethof.ai/brain/local3/get.sh | bash
#
# It makes ./brethof-brain (or $BRAIN_DIR), downloads the embedding model
# (2.2 GB, once, checksummed), the three images (checksummed), writes your
# private settings, starts the stack, and prints the settings page address
# and its password. Run it again to update: your memory and settings stay.
set -euo pipefail
DL="${BRAIN_DL_URL:-https://downloads.brethof.ai/brain}"
BASE="$DL/local3"
DIR="${BRAIN_DIR:-$PWD/brethof-brain}"
MODEL_FILE=embeddinggemma-300m-fp32.tar.gz
say()  { printf '\n== %s\n' "$*"; }
fail() { echo "INSTALL FAILED: $*" >&2; exit 1; }

mkdir -p "$DIR" && cd "$DIR"

say "runtime"
# A PODMAN-ONLY SERVER IS THE NORMAL CASE (found 2026-11-17 on our own VPS:
# Ubuntu with podman, no compose of any kind). Podman's Docker-compatible
# socket plus Docker's own compose binary — pinned, checksummed, kept in
# ./bin — is the path that needs nothing installed system-wide.
COMPOSE_VER=v2.29.7
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ENGINE=docker; COMPOSE="docker compose"
elif command -v podman >/dev/null; then
  ENGINE=podman
  systemctl --user enable --now podman.socket >/dev/null 2>&1 || fail "cannot start podman.socket (systemctl --user)"
  export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
  if command -v docker-compose >/dev/null; then
    COMPOSE="docker-compose"
  else
    case "$(uname -m)" in
      x86_64) ARCH=x86_64; SUM=383ce6698cd5d5bbf958d2c8489ed75094e34a77d340404d9f32c4ae9e12baf0 ;;
      aarch64|arm64) ARCH=aarch64; SUM=6e9fbd5daa20dca5d7d89145081ae8155d68ef2928b497d9f85b54fe0f9dbb2c ;;
      *) fail "no compose for $(uname -m)" ;;
    esac
    if [ ! -x bin/docker-compose ]; then
      mkdir -p bin
      curl -fsSL -o bin/docker-compose.new \
        "https://github.com/docker/compose/releases/download/$COMPOSE_VER/docker-compose-linux-$ARCH" || fail "compose download"
      echo "$SUM  bin/docker-compose.new" | sha256sum -c --quiet - || fail "compose checksum MISMATCH"
      chmod +x bin/docker-compose.new && mv bin/docker-compose.new bin/docker-compose
    fi
    COMPOSE="$PWD/bin/docker-compose"
  fi
  # BOOT UNDER PODMAN (2026-11-18, measured on the VPS, podman 4.9.3):
  # podman-restart.service starts every boot-started container INSIDE its own
  # cgroup, so when any one start fails (a storage whose passphrase is not
  # there yet) the unit fails and systemd kills the monitors of all of them —
  # the hub and embedder die and podman is left with "Stopping" ghosts. And
  # 4.x starts only restart-policy=always. So: our containers are on-failure
  # (a crash still restarts them; podman-restart never takes them), and our
  # own unit (below, after the first start) starts each in its own scope.
  BRAIN_RESTART=on-failure
  if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != yes ]; then
    loginctl enable-linger "$(id -un)" 2>/dev/null \
      || echo "NOTE: run 'sudo loginctl enable-linger $(id -un)' or the memory stops when you log out and stays down after a reboot"
  fi
else
  fail "need docker with the compose plugin, or podman"
fi
# PODMAN WEARING THE DOCKER CLI (studio, 2026-11-18) takes the docker path
# for compose, but boots like podman: the boot fix below applies to both
IS_PODMAN=""
# (a case, not `| grep -q`: under pipefail grep's early exit SIGPIPEs docker
# and the pipe reads as false)
[ "$ENGINE" = podman ] && IS_PODMAN=1
case "$(docker --version 2>&1)" in *[Pp]odman*) IS_PODMAN=1 ;; esac
[ -n "$IS_PODMAN" ] && BRAIN_RESTART=on-failure
echo "using $ENGINE ($COMPOSE)${IS_PODMAN:+, podman underneath}"

VERSION="${BRAIN_VERSION:-$(curl -fsSL "$BASE/latest.txt" || true)}"
[ -n "$VERSION" ] || fail "no version published yet (set BRAIN_VERSION to test a build)"
echo "version $VERSION into $DIR"

say "the kit"
for f in compose.yml install.sh uninstall.sh; do
  curl -fsSL -o "$f.new" "$BASE/$VERSION/$f" || fail "download $f"
done
curl -fsSL -o kit.sha256 "$BASE/$VERSION/kit.sha256" || fail "download kit.sha256"
sed 's/\(  *\)\([^ ]*\)$/\1\2.new/' kit.sha256 | sha256sum -c --quiet - || fail "kit checksum MISMATCH"
mv compose.yml.new compose.yml; mv install.sh.new install.sh; mv uninstall.sh.new uninstall.sh
chmod +x install.sh uninstall.sh; rm -f kit.sha256

say "embedding model (search runs on this machine — text never leaves it)"
if [ -d model/embeddinggemma-300m-onnx ]; then
  echo "already present"
else
  mkdir -p model
  curl -fL -C - -o "model/$MODEL_FILE" "$DL/models/$MODEL_FILE" || fail "model download"
  curl -fsSL -o "model/$MODEL_FILE.sha256" "$DL/models/$MODEL_FILE.sha256" || fail "model checksum download"
  (cd model && sha256sum -c "$MODEL_FILE.sha256") || fail "model checksum MISMATCH — delete model/ and rerun"
  tar xzf "model/$MODEL_FILE" -C model && rm -f "model/$MODEL_FILE" "model/$MODEL_FILE.sha256"
fi

say "images"
if $ENGINE image inspect "downloads.brethof.ai/brain-hub:$VERSION" >/dev/null 2>&1; then
  echo "already loaded"
else
  B="brain-local3-images-$VERSION.tar.gz"
  curl -fL -C - -o "$B" "$BASE/$VERSION/$B" || fail "images download"
  curl -fsSL -o "$B.sha256" "$BASE/$VERSION/$B.sha256" || fail "images checksum download"
  sha256sum -c "$B.sha256" || fail "images checksum MISMATCH"
  gunzip -c "$B" | $ENGINE load >/dev/null || fail "image load"
  rm -f "$B" "$B.sha256"
fi

say "settings"
BRAIN_MODEL_DIR="$DIR/model/embeddinggemma-300m-onnx" BRAIN_VERSION="$VERSION" ./install.sh >/dev/null
sed -i "s|^BRAIN_VERSION=.*|BRAIN_VERSION=$VERSION|" .env

if [ -n "$IS_PODMAN" ]; then
  grep -q '^BRAIN_RESTART=' .env && sed -i "s|^BRAIN_RESTART=.*|BRAIN_RESTART=$BRAIN_RESTART|" .env \
    || echo "BRAIN_RESTART=$BRAIN_RESTART" >> .env
fi

say "starting"
$COMPOSE up -d >/dev/null || fail "compose up"
if [ -n "$IS_PODMAN" ]; then
  PROJ=$(podman inspect "$($COMPOSE ps -q brain-hub | head -1)" --format '{{index .Config.Labels "com.docker.compose.project"}}')
  UNIT="brethof-brain-$PROJ.service"
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/$UNIT" <<UNITEOF
[Unit]
Description=Brethof Brain ($PROJ) — starts the memory at boot
Wants=network-online.target
After=network-online.target podman.socket

[Service]
Type=oneshot
RemainAfterExit=yes
# each container in its own scope, so one that cannot start (e.g. its
# passphrase is not there yet) never takes the others down with this unit
ExecStart=/bin/sh -c 'for c in \$(podman ps -a -q --filter label=com.docker.compose.project=$PROJ); do systemd-run --user --scope --collect -q podman start \$c || echo "brethof-brain: could not start \$c"; done; exit 0'
ExecStop=/bin/sh -c 'podman stop -t 30 \$(podman ps -q --filter label=com.docker.compose.project=$PROJ) 2>/dev/null; exit 0'

[Install]
WantedBy=default.target
UNITEOF
  systemctl --user daemon-reload && systemctl --user enable "$UNIT" >/dev/null 2>&1 \
    || echo "NOTE: could not enable $UNIT — the memory will not start by itself after a reboot"
fi
PORT=$(grep '^BRAIN_HUB_PORT=' .env | cut -d= -f2)
for _ in $(seq 1 150); do curl -fs "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 2; done
curl -fs "http://127.0.0.1:$PORT/health" >/dev/null || fail "the hub did not come up — see: $COMPOSE logs brain-hub"
# `compose exec` fails under rootless Podman (crun cannot write oom_score_adj,
# found 2026-11-17); a plain exec into the container works everywhere
HUB_CID=$($COMPOSE ps -q brain-hub 2>/dev/null | head -1)
PW=$($ENGINE exec "$HUB_CID" cat /srv/hub/admin-token 2>/dev/null | tr -d '\r\n' || true)
[ -n "$PW" ] || PW="(not readable yet — run: $ENGINE exec \$($COMPOSE ps -q brain-hub) cat /srv/hub/admin-token)"
cat <<TXT

  Brethof Brain $VERSION is running.

  Settings page:      http://127.0.0.1:$PORT/settings
  Settings password:  $PW
    (keep it; to read it again: $ENGINE exec \$($COMPOSE ps -q brain-hub) cat /srv/hub/admin-token)

  On the settings page: connect your Brethof account, then set your AI model.
  Then connect this computer's agents:
      pip install "git+https://github.com/BrethofAI/brethof-brain-client.git" && brethof-brain pair http://127.0.0.1:$PORT
  (Other computers: open the hub to them first with BRAIN_HUB_BIND in $DIR/.env,
   then pair with this machine's address instead of 127.0.0.1.)
  To remove it later:  $DIR/uninstall.sh   (keeps your memory unless you say --delete-memory)

  Your memory is encrypted with the passphrase in $DIR/secrets/passphrase.
  BACK IT UP. Without it the memory cannot be read — by anyone, including us.
TXT
