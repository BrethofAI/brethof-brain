#!/bin/bash
# The self-hosted memory container: Postgres on an encrypted filesystem the
# CUSTOMER holds the key to.
#
# WHY GOCRYPTFS AND NOT pg_tde. Measured 2026-08-27: pg_tde has NO runtime key
# eviction — deleting the key at the provider leaves a running server serving
# decrypted rows indefinitely, and pg_tde_delete_key() refuses outright while
# encrypted tables exist. Only process death evicts. gocryptfs gives the same
# eviction with far fewer moving parts, encrypts the WHOLE data directory
# (WAL and temp files included, not just tables), makes `du` an exact usage
# meter, and turns backup and migration into "copy the ciphertext directory".
#
# WHY EVERYTHING BELOW HAPPENS IN ONE `su` SESSION. gocryptfs daemonises after
# mounting; if the shell that started it exits, the mount goes with it. So the
# mount, initdb and the final exec all live in a single postgres-user session
# and Postgres becomes that session. Measured the hard way: mounting in one
# subshell and checking from the parent reports "not a mountpoint".
#
# A gocryptfs mount is also owned by the uid that made it — mounting as root
# and chowning does NOT give postgres access through FUSE.
#
# THE PASSPHRASE IS NEVER WRITTEN TO DISK. It reaches the inner session on
# stdin and is consumed by gocryptfs directly.
set -euo pipefail

CIPHER=${BRAIN_CIPHER_DIR:-/cipher}
PLAIN=${BRAIN_PLAIN_DIR:-/plain}

die() { echo "brain-db: $*" >&2; exit 1; }

if [ -n "${BRAIN_PASSPHRASE_FILE:-}" ]; then
  [ -r "$BRAIN_PASSPHRASE_FILE" ] || die "BRAIN_PASSPHRASE_FILE unreadable"
  PASS=$(cat "$BRAIN_PASSPHRASE_FILE")
else
  PASS=${BRAIN_PASSPHRASE:-}
fi
[ -n "$PASS" ] || die "no passphrase. Set BRAIN_PASSPHRASE or BRAIN_PASSPHRASE_FILE. \
Without it your memory cannot be unlocked - and we cannot unlock it for you."

# The Postgres ROLE password, which is a different thing from the passphrase
# above: the passphrase decrypts the disk, this authenticates the API
# container over the private network. pg_hba below demands scram-sha-256 for
# every TCP connection, so a role without a password can be reached by nothing
# at all -- the local socket works and every container does not.
DBPASS=${POSTGRES_PASSWORD:-}
[ -n "$DBPASS" ] || die "no POSTGRES_PASSWORD. The API container authenticates \
with it over the private network; without one the database accepts no TCP \
connections and the stack cannot talk to itself."

mkdir -p "$CIPHER" "$PLAIN"
chown postgres:postgres "$CIPHER" "$PLAIN"

cat > /run/brain-inner.sh <<'INNER'
#!/bin/bash
set -euo pipefail
CIPHER=$1; PLAIN=$2; PGUSER_NAME=$3
PGDATA=$PLAIN/db
# Both secrets arrive on stdin, in order, and neither is ever an argument.
read -r PASS
read -r DBPASS

if [ ! -e "$CIPHER/gocryptfs.conf" ]; then
  echo "brain-db: first run - creating encrypted store"
  printf '%s\n' "$PASS" | gocryptfs -init -q -nosyslog "$CIPHER"
fi
printf '%s\n' "$PASS" | gocryptfs -q -nosyslog "$CIPHER" "$PLAIN"
unset PASS
mountpoint -q "$PLAIN" || { echo "brain-db: unlock failed - wrong passphrase?" >&2; exit 1; }
echo "brain-db: encrypted store mounted"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "brain-db: initialising database"
  # --pwfile from a process substitution: initdb reads /dev/fd/N, so the
  # password exists only in that pipe -- never as a file on the encrypted
  # volume, never in the process arguments.
  initdb -D "$PGDATA" -U "$PGUSER_NAME" --data-checksums -E UTF8 --locale=C \
    --pwfile=<(printf '%s\n' "$DBPASS") >/dev/null
  unset DBPASS
  echo "host all all 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"
fi

# A MEMORY MADE BY ANOTHER POSTGRES MAJOR never starts here (2026-11-19, the
# storage moved 18 -> 19): Postgres would refuse with a line deep in its own
# log inside the encrypted directory, and the storage would just look dead.
have=$(cat "$PGDATA/PG_VERSION")
want=$(postgres --version | sed -E 's/[^0-9]*([0-9]+).*/\1/')
if [ "$have" != "$want" ]; then
  echo "brain-db: this memory was made by Postgres $have and this image runs Postgres $want." >&2
  echo "brain-db: it cannot open it directly. Move the memory across with a dump and restore (see the upgrade notes); nothing was changed." >&2
  exit 1
fi

# LOGS GO INSIDE THE ENCRYPTED DIRECTORY. Postgres logs carry query text; in
# plaintext beside the ciphertext they would undo the whole design.
exec postgres -D "$PGDATA" \
  -c listen_addresses="${BRAIN_LISTEN:-*}" \
  -c logging_collector=on \
  -c log_directory=log \
  -c shared_buffers="${BRAIN_SHARED_BUFFERS:-256MB}" \
  -c maintenance_work_mem="${BRAIN_MAINT_MEM:-256MB}" \
  -c max_connections="${BRAIN_MAX_CONN:-300}"
INNER
chmod 755 /run/brain-inner.sh

# setpriv, NOT su: su calls initgroups(), and on a hosted stack running
# from the box's shared image store inside a customer userns, setting
# supplementary groups hits an unmapped gid and dies with "cannot set
# groups" (measured 2026-08-31). postgres needs no supplementary groups —
# clearing them is both the fix and the least privilege.
#
# THE SECRETS GO IN BY PROCESS SUBSTITUTION, NOT A PIPE (2026-11-18): the
# last command of a pipe runs in a subshell, so `printf | exec setpriv` left
# this shell waiting and Postgres a child it never signalled — a stop killed
# the shell and the kernel SIGKILLed Postgres at the container's end, so
# every stop and every reboot was a crash and every start ran WAL recovery.
# With `< <(...)` the exec replaces this shell: this process BECOMES
# Postgres, and a stop's signal reaches it.
exec setpriv \
  --reuid postgres --regid postgres --clear-groups /bin/bash -c \
  "BRAIN_SHARED_BUFFERS=${BRAIN_SHARED_BUFFERS:-256MB} \
   BRAIN_MAINT_MEM=${BRAIN_MAINT_MEM:-256MB} \
   BRAIN_MAX_CONN=${BRAIN_MAX_CONN:-100} \
   BRAIN_LISTEN=${BRAIN_LISTEN:-*} \
   exec /run/brain-inner.sh $CIPHER $PLAIN ${POSTGRES_USER:-brain}" \
  < <(printf '%s\n%s\n' "$PASS" "$DBPASS")
