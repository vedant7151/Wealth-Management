-- Runs once, automatically, when docker-compose's local `db` service's
-- data volume is first created (standard postgres-image behavior for
-- anything mounted into /docker-entrypoint-initdb.d/). The `pgvector/
-- pgvector` image already bundles the vector extension's binary; this
-- just enables it in the one database docker-compose creates
-- (POSTGRES_DB, see docker-compose.yml), matching schema.prisma's
-- `extensions = [vector]` datasource declaration.
CREATE EXTENSION IF NOT EXISTS vector;
