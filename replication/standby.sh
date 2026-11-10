#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
  mkdir -p "$PGDATA"
  chown postgres:postgres "$PGDATA"
  chmod 700 "$PGDATA"
  exec gosu postgres bash "$0" "$@"
fi
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  export PGPASSWORD="$REPLICATION_PASSWORD"
  # A failed/incomplete backup is never used as a cluster.
  backup="${PGDATA}/bootstrap"
  rm -rf "$backup"
  pg_basebackup -d "host=master user=replicator application_name=$REPLICA_NAME" \
    -D "$backup" -Fp -Xs -P -R --checkpoint=fast
  # Copy dotfiles too, then remove the staging directory.
  cp -a "$backup/." "$PGDATA/"
  rm -rf "$backup"
  chmod 700 "$PGDATA"
fi
exec docker-entrypoint.sh "$@"
