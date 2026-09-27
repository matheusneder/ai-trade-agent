#!/bin/sh
# Cria o usuário somente leitura do Grafana na PRIMEIRA inicialização do volume do Postgres.
# Em um volume já existente, rode os mesmos comandos manualmente (ver doc/runbook.md).
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE ROLE grafana_ro LOGIN PASSWORD '${GRAFANA_DB_PASSWORD:-grafana_ro_dev}';
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO grafana_ro;
	GRANT USAGE ON SCHEMA public TO grafana_ro;
	GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
	ALTER DEFAULT PRIVILEGES FOR ROLE "${POSTGRES_USER}" IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
EOSQL
