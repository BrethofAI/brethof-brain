-- brain-hub registry: who may hold a pipe, what they used, and the vocabulary
-- and briefs their curator runs on. SQLite — a file, not a service: this box
-- holds every prompt, the provider key and every customer's brief, and a
-- database server on it is a second process to harden for no gain. Nothing
-- here is a vector, an archive turn or a curated record; those live in the
-- customer's own container.
--
-- Every statement is IF NOT EXISTS and this runs on EVERY boot. The earlier
-- Postgres version ran its schema only on first boot, so a new table deployed
-- green and 500'd on its first request.

CREATE TABLE IF NOT EXISTS tenants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    -- sha256 hex of the tenant key; the key itself is shown once at mint and
    -- never stored.
    key_hash   TEXT NOT NULL UNIQUE,
    plan       TEXT NOT NULL DEFAULT 'selfhosted',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_daily (
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    day        TEXT NOT NULL,
    op         TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day, op)
);

-- RULES LIVE HERE, NOT ON THE CUSTOMER'S BOX. The deciding argument is
-- corruption, not privacy: rules are the system's own law, and a customer
-- hand-editing them in their psql makes the curator misbehave — with the
-- support ticket landing on us either way. One copy, here; the box holds a
-- read-only cache it can never write back.
CREATE TABLE IF NOT EXISTS rule (
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rule_id    TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL DEFAULT '',
    -- 'all' for every-project law, or the project name. Mirrors the data
    -- plane's meta->>'area' exactly, so a synced rule needs no translation.
    area       TEXT NOT NULL DEFAULT 'all',
    pin        INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, rule_id)
);
CREATE INDEX IF NOT EXISTS rule_by_tenant ON rule (tenant_id, area);

-- THERE IS NO REGISTRAR INDEX (founder 2026-09-08): names and kinds are
-- judged on the box, by the graph worker, against the real graph.

-- THERE IS NO BRIEF TABLE (founder 2026-09-02). The curator's brief is
-- COMPOSED at use time — base doctrine + the project's purpose rule verbatim +
-- one line per bearing rule from rule_verdict — and nothing compiles it, so
-- nothing has to be stored. The old `brief` table (teacher-compiled layer +
-- rendered prompt) is dropped by the guarded block in store.py once the
-- mechanical path has run live for a day; it is not created here.

-- STAGE 0's VERDICTS, cached where the filter runs. Keyed on the rule's
-- CONTENT hash so an edit that does not change the text costs nothing, and on
-- the project because the same rule bears differently in different domains:
-- brain kept 18 of 47 rules, nv kept 12 of 42, and a DIFFERENT twelve.
-- `line` (added by the guarded ALTER in store.py) is the INSTRUCTION the
-- curator reads in place of the rule — the filter writes it inside the same
-- verdict, so a rule costs one model call in its life and the brief is a
-- string operation from then on.
CREATE TABLE IF NOT EXISTS rule_verdict (
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project     TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    bears       INTEGER NOT NULL,
    why         TEXT NOT NULL DEFAULT '',
    decided_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, project, rule_id, content_sha)
);


-- THE LEDGER OFF-SWITCH (founder 2026-08-31: someone building a product on
-- the Brain does not want a task list extracted from their users' chats, or
-- the cost of one). A row here means the extractor is OFF: project '' turns
-- it off for the whole account, a named project for that project only. The
-- panel flips these through the control plane's admin door; the extractor
-- checks on every exchange. Heal has no such switch — without heal there is
-- no trustworthy curated memory.
CREATE TABLE IF NOT EXISTS ledger_off (
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, project)
);

-- THE SEED SWITCH (founder 2026-11-10, for Aurora: "your brain can do some
-- tweaks, which are only involved with this one brain instance"). The five
-- ★SEED rules teach a coding agent's habits (projects, the four doors, the
-- Ledger, the standing documents, rules); an app tenant that keeps one
-- project per end-user teaches its own through its general rules instead.
-- A row here means the seeds are OFF for that whole account: no brief, no
-- rule match and no curator reads them. One mechanism for every tenant.
CREATE TABLE IF NOT EXISTS seeds_off (
    tenant_id  TEXT NOT NULL PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE
);

-- THE INFERENCE BUDGET (2026-11-11). Every tenant spends the ONE provider
-- key, so a test tenant's burst spends everyone's window: Aurora's 28-day
-- run on t_auroratest (5,074 calls, 14.3M tokens on 2026-11-10) closed the
-- provider's five-hour window and four of the founder's own curations
-- failed on the 429s. A row here caps that tenant's tokens (in + out) over
-- a rolling five hours; past it the hub refuses its calls without spending
-- the key. NO ROW = NO CAP: nothing changes for any tenant without one.
CREATE TABLE IF NOT EXISTS infer_budget (
    tenant_id  TEXT NOT NULL PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    tokens_5h  INTEGER NOT NULL
);
-- What a capped tenant spent, one row per call, five hours kept. Only capped
-- tenants write here.
CREATE TABLE IF NOT EXISTS infer_spend (
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    ts         REAL NOT NULL,
    tokens     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS infer_spend_tenant_ts ON infer_spend (tenant_id, ts);
-- THE LOCAL HUB'S SETTINGS (2026-11-17, the local-first redesign): the
-- customer's own model connections and the settings page's admin token
-- hash. One row per key. Read once at boot into hub/models.CACHE; the
-- settings page writes through hub/models.set().
CREATE TABLE IF NOT EXISTS settings (
    k  TEXT PRIMARY KEY,
    v  TEXT NOT NULL
);
