-- Additive only: HNSW indexes on the two pgvector columns. Not expressible
-- through schema.prisma's DSL for an Unsupported("vector(384)") column, so
-- this is a separate, manually-applied raw SQL script rather than part of
-- `prisma db push`.
--
-- NOT YET APPLIED to the live Neon DB as of Phase 1 (2026-11-11) -- this is
-- a prepared script for the owner to run deliberately, not something this
-- session executed. See docs/PROGRESS.md "Known issues / Blockers".
--
-- CONCURRENTLY avoids holding a write lock on the table for the duration of
-- the build, but requires running outside an explicit transaction block
-- (run each statement on its own, e.g. via `psql $DATABASE_URL -f this_file`
-- or one at a time in your DB client -- do not wrap in BEGIN/COMMIT).
--
-- vector_l2_ops matches the "<->" L2-distance operator already used by
-- every query in backend/agent.py (SearchCache and DocumentChunk lookups).

CREATE INDEX CONCURRENTLY IF NOT EXISTS "SearchCache_queryEmbedding_hnsw_idx"
    ON "SearchCache" USING hnsw ("queryEmbedding" vector_l2_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS "DocumentChunk_embedding_hnsw_idx"
    ON "DocumentChunk" USING hnsw ("embedding" vector_l2_ops);
