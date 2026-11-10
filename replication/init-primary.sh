#!/usr/bin/env bash
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=repl_password="$REPLICATION_PASSWORD" <<'SQL'
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD :'repl_password';
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE TABLE replication_probe (
    run_id uuid NOT NULL,
    seq bigint NOT NULL,
    payload text NOT NULL,
    PRIMARY KEY (run_id, seq)
);
SQL
printf '\nhost replication replicator all scram-sha-256\n' >> "$PGDATA/pg_hba.conf"
