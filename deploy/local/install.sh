#!/bin/bash
# BRETHOF BRAIN — first-time setup for the local stack (2026-11-17).
# Writes .env, the storage key, the disk passphrase and the key file the
# storage answers to. Safe to run again: it never overwrites what exists.
set -euo pipefail
cd "$(dirname "$0")"

rand() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

mkdir -p secrets keys data
chmod 700 secrets

if [ ! -s secrets/passphrase ]; then
  ( umask 077; rand 32 > secrets/passphrase )
  NEW_PASS=1
fi

if [ ! -s .env ]; then
  ( umask 077; cat > .env <<ENV
# written by install.sh — keep this file private
BRAIN_DB_PASSWORD=$(rand 24)
BRAIN_STORAGE_KEY=bs_$(rand 32)
# the fp32 EmbeddingGemma ONNX export (see the README)
BRAIN_MODEL_DIR=${BRAIN_MODEL_DIR:-}
# loopback by default; 0.0.0.0 to reach the hub from other machines
BRAIN_HUB_BIND=127.0.0.1
BRAIN_HUB_PORT=8600
# the release whose images run here (get.sh sets it)
BRAIN_VERSION=${BRAIN_VERSION:-local}
ENV
  )
fi

# THE HUB'S NAME (2026-11-18): the owner sees it on their account page and
# on each device's approve screen; without one the hub showed its container's
# random hostname. The machine's own name, letters, digits, dot and dash only.
if ! grep -q '^HUB_NAME=' .env; then
  NAME=$(uname -n 2>/dev/null | tr -cd 'A-Za-z0-9.-' | cut -c1-60)
  printf 'HUB_NAME=%s\n' "${NAME:-brethof-brain}" >> .env
fi

# THE DNS TRAP (found 2026-11-17 on the first real install): on a machine
# whose resolver is a local stub (systemd-resolved, 127.0.0.53), containers on
# a private network cannot resolve anything — the hub could not reach the
# licence server. Give the hub the machine's real upstream instead.
if ! grep -q '^BRAIN_DNS=' .env; then
  DNS=""
  if grep -qE '^nameserver 127\.' /etc/resolv.conf 2>/dev/null; then
    DNS=$(grep -m1 '^nameserver' /run/systemd/resolve/resolv.conf 2>/dev/null | awk '{print $2}')
  fi
  printf '# DNS for the hub when this machine resolves through a local stub (empty = the default)\nBRAIN_DNS=%s\n' "$DNS" >> .env
fi

# shellcheck disable=SC1091
. ./.env
if [ ! -s keys/v2keys ]; then
  HASH=$(printf '%s' "$BRAIN_STORAGE_KEY" | sha256sum | cut -d' ' -f1)
  printf '%s:memory:customer:hub\n' "$HASH" > keys/v2keys
  chmod 644 keys/v2keys
fi

[ -n "${BRAIN_MODEL_DIR:-}" ] || echo "Set BRAIN_MODEL_DIR in .env to the embedder model folder before starting."

cat <<TXT

  Ready. Start it with:   docker compose up -d
  Then open:              http://127.0.0.1:${BRAIN_HUB_PORT:-8600}/settings
  The settings password:  docker exec $(docker compose ps -q brain-hub) cat /srv/hub/admin-token
TXT
if [ "${NEW_PASS:-}" = 1 ]; then
  cat <<TXT

  Your memory is encrypted with the passphrase in $(pwd)/secrets/passphrase.
  BACK IT UP somewhere safe. Without it the memory cannot be read, by anyone.
TXT
fi
