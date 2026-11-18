-- brethof-brain v2 — per-tenant schema.
--
-- One DATABASE per tenant. That is the isolation boundary (its own role, its own
-- files) and it makes pg_database_size() an exact per-customer number, which is
-- what v1 could not produce at all: SurrealDB keeps every tenant in one shared
-- keyspace, so disk was never attributable.
--
-- THE ONE BIG CHANGE FROM v1: project is a COLUMN, not a set of tables.
-- v1 created 3 tables + 11 indexes per project, which capped tenants at ~100
-- projects, forced DDL on the write path, and pushed an app author toward
-- "one project per end user" = 30,000 tables. Here a project costs one row in
-- `project` and nothing else. Unlimited projects, no DDL at runtime, no index
-- explosion.
--
-- :dim is substituted at provision time from BRAIN_EMBED_DIM (768 today —
-- EmbeddingGemma). Do not hardcode it.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── projects ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project (
    name        text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_]{0,31}$'),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ── curated memory ─────────────────────────────────────────────────────────
-- record_id is the stable, guessable id (topic_subtopic). UNIQUE with project
-- so an upsert updates in place — v1's "predictable ids, never fork" rule
-- enforced by the database instead of by prose.
CREATE TABLE IF NOT EXISTS document (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL REFERENCES project(name) ON DELETE CASCADE,
    record_id   text NOT NULL,
    type        text NOT NULL DEFAULT 'note',
    title       text NOT NULL DEFAULT '',
    content     text NOT NULL DEFAULT '',
    source      text,
    captured_at date,
    -- EVERY field that is not one of the columns above. v1 records are
    -- heterogeneous by design — `pin` and `area` drive which rules get
    -- injected, `platform` marks OS-bound records dormant rather than dead,
    -- and `status`/`scope`/`superseded` all carry meaning. A migration that
    -- kept only the typed columns would drop all of it silently and the
    -- session-start blob could no longer tell a pinned rule from any other
    -- row. Typed columns for what is queried structurally, jsonb for the rest.
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- halfvec: 2 bytes/dim. Measured 1,399 B/row vs SurrealDB's ~9,700.
    embedding   halfvec(:dim),
    -- 'simple' NOT 'english': this product is multilingual by design, and an
    -- English stemmer actively damages recall in other languages.
    tsv         tsvector GENERATED ALWAYS AS
                (to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(content,''))) STORED,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project, record_id)
);

-- ── chat archive — IMMUTABLE ───────────────────────────────────────────────
-- Everything curated can be rebuilt from this; nothing can rebuild this.
-- (session_id, turn_index) unique makes re-archiving idempotent rather than
-- duplicating, which v1 handled with a hashed id.
CREATE TABLE IF NOT EXISTS chat (
    id          bigserial PRIMARY KEY,
    -- ON DELETE RESTRICT, not CASCADE (2026-08-09, GLM engineering audit).
    -- chat is the immutable archive. Deleting it is allowed ONLY as an
    -- explicit, deliberate owner act (delete_project, cleanup_history,
    -- account deletion) — never as a side effect. Under CASCADE, ANY
    -- `DELETE FROM project` anywhere silently took the archive with it;
    -- RESTRICT forbids that, so the only way chat dies is code that names
    -- the chat table directly (delete_project runs `DELETE FROM chat`
    -- first, then the project row). Accident impossible, intent honoured.
    -- (document/state stay CASCADE — disposable, rebuildable.)
    project     text NOT NULL REFERENCES project(name) ON DELETE RESTRICT,
    session_id  text NOT NULL,
    turn_index  int  NOT NULL,
    role        text NOT NULL,
    text        text NOT NULL,
    embedding   halfvec(:dim),
    -- NOT DIALOGUE — kept, but silent (2026-08-12). The client's tool_result
    -- filter landed 2026-07-11; before it, MCP tool responses, raw JSON
    -- dumps and whole memory_index payloads echoed back into chat were
    -- archived as conversation. An echoed index matches nearly any query, so
    -- it crowds real turns out of recall. Marking beats deleting: the
    -- archive stays immutable and complete (it can still rebuild anything),
    -- the row simply stops being embedded and stops answering searches.
    embed_skip  boolean NOT NULL DEFAULT false,
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text,''))) STORED,
    ts          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, turn_index)
);

-- Rows whose embedding is still NULL are WORK TO DO, not a normal steady state.
-- v1 dropped embeddings silently on embedder timeout and nobody noticed for
-- months; this partial index makes the backlog cheap to see and to drain.
CREATE INDEX IF NOT EXISTS chat_unembedded ON chat (id)
    WHERE embedding IS NULL AND NOT embed_skip;
CREATE INDEX IF NOT EXISTS document_unembedded ON document (id) WHERE embedding IS NULL;

-- ── windowed embeddings for LONG texts (founder 2026-08-12: "the length
-- shouldn't be limited at all — split it and do it separately") ─────────────
-- A row's own embedding covers its FIRST model window; every additional
-- window gets one vector here, written by the drainer at embed time. The
-- recall vector streams search these too and credit the PARENT row with its
-- best window, so nothing a tenant stores is invisible to semantic search
-- at any length. FK cascade keeps them exactly as durable as their parent.
CREATE TABLE IF NOT EXISTS chat_chunk (
    chat_id     bigint NOT NULL REFERENCES chat(id) ON DELETE CASCADE,
    chunk_idx   int NOT NULL,
    embedding   halfvec(768) NOT NULL,
    PRIMARY KEY (chat_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS chat_chunk_hnsw_idx ON chat_chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE TABLE IF NOT EXISTS document_chunk (
    document_id bigint NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    chunk_idx   int NOT NULL,
    embedding   halfvec(768) NOT NULL,
    PRIMARY KEY (document_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS document_chunk_hnsw_idx ON document_chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);




-- ── graph ──────────────────────────────────────────────────────────────────
-- refs  = [[link]] between records; supersedes = lineage of replacement.
-- Shallow by design (1–2 hops), so a join table beats a graph engine.
CREATE TABLE IF NOT EXISTS edge (
    src_project text NOT NULL,
    src_record  text NOT NULL,
    dst_project text NOT NULL,
    dst_record  text NOT NULL,
    -- derived_from added 2026-07-26: v1 has a third edge kind and the first
    -- migration dropped all 20 of them because this CHECK could not hold one.
    kind        text NOT NULL CHECK (kind IN ('refs','supersedes','derived_from')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (src_project, src_record, dst_project, dst_record, kind)
);

-- (The old inventory graph — entity, entity_mention, entity_link — left on
-- 2026-09-27; api/main._DEAD_TABLES drops it from a tenant at boot.)


-- ── history-cleanup queue (2026-07-30) ─────────────────────────────────────
-- The PRUNE half of "buy more or prune". Chat stays immutable to the SYSTEM;
-- this queue holds the OWNER's deliberate, confirmed cleanups: optional
-- digest of each old session into curated memory, delete, then VACUUM FULL —
-- measured: without the vacuum the metered size does not drop at all.
CREATE TABLE IF NOT EXISTS prune_job (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    older_than_days int NOT NULL,
    mode        text NOT NULL DEFAULT 'delete',
    status      text NOT NULL DEFAULT 'pending',
    attempts    int  NOT NULL DEFAULT 0,
    last_error  text NOT NULL DEFAULT '',
    result      text NOT NULL DEFAULT '',
    next_try    timestamptz NOT NULL DEFAULT now(),
    requested_at timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);


-- ── dashboards + laws ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS state (
    area        text PRIMARY KEY,
    project     text REFERENCES project(name) ON DELETE CASCADE,
    status      text NOT NULL DEFAULT '',
    next_actions text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- SESSION NOTES (2026-09-03): the handover, keyed by session AND project;
-- a ring of five per project, injected whole at session start. Not records,
-- not chat — see core/notes.py.
CREATE TABLE IF NOT EXISTS note (
    project     text NOT NULL,
    session_id  text NOT NULL,
    title       text NOT NULL DEFAULT '',
    body        text NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, session_id)
);
CREATE INDEX IF NOT EXISTS note_project_recent ON note (project, written_at DESC);

CREATE TABLE IF NOT EXISTS rule (
    name        text PRIMARY KEY,
    area        text NOT NULL DEFAULT 'all',
    title       text NOT NULL,
    content     text NOT NULL,
    pinned      boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ── indexes ────────────────────────────────────────────────────────────────
-- Build HNSW AFTER bulk load, not before — 20k rows took 4.2 s when built last.
CREATE INDEX IF NOT EXISTS document_project_idx ON document (project);
CREATE INDEX IF NOT EXISTS document_updated_idx ON document (updated_at DESC);
CREATE INDEX IF NOT EXISTS document_tsv_idx     ON document USING gin (tsv);
CREATE INDEX IF NOT EXISTS document_meta_idx    ON document USING gin (meta);
CREATE INDEX IF NOT EXISTS document_hnsw_idx    ON document
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS chat_project_ts_idx  ON chat (project, ts DESC);
CREATE INDEX IF NOT EXISTS chat_session_idx     ON chat (session_id, turn_index);
CREATE INDEX IF NOT EXISTS chat_tsv_idx         ON chat USING gin (tsv);
-- v1 could not afford this at all (its HNSW was RAM-only, so chat had NO vector
-- index and fell back to brute force over every row). Postgres HNSW is on disk.
CREATE INDEX IF NOT EXISTS chat_hnsw_idx        ON chat
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS edge_dst_idx ON edge (dst_project, dst_record, kind);
CREATE INDEX IF NOT EXISTS rule_pinned_idx ON rule (area) WHERE pinned;

-- ── starter teaching ── moved to core/seedrules.py (2026-08-09) ───────────────
-- The fresh-tenant ★SEED rule set is fleet-managed code, not DDL:
-- provision_tenant applies this schema, then seedrules.ensure() writes the
-- canonical ★SEED rows (meta.seed='true', read-only through every door,
-- refreshed by deploy). Keeping it out of the DDL means one source of
-- truth for the text and an update path that DO NOTHING could never give.


