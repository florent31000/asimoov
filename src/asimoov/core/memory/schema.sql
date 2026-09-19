-- ASIMOOV memory store (plan.md section 4.3). SQLite + FTS5.
-- Embeddings live in face_embeddings only and never leave this file:
-- every read API returns text, ids or a similarity score.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS persons (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    created_at   REAL NOT NULL,
    last_seen_at REAL,
    relationship TEXT,
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS persons_name ON persons (name);

CREATE TABLE IF NOT EXISTS face_embeddings (
    person_id  TEXT NOT NULL REFERENCES persons (id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,
    quality    REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS face_embeddings_person ON face_embeddings (person_id);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  TEXT REFERENCES persons (id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source     TEXT NOT NULL DEFAULT 'conversation',
    ts         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS facts_person ON facts (person_id);

CREATE TABLE IF NOT EXISTS episodes (
    id           TEXT PRIMARY KEY,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    participants TEXT NOT NULL DEFAULT '',
    summary      TEXT,
    mood         TEXT
);

CREATE TABLE IF NOT EXISTS journal (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   REAL NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note'
);

-- One full-text index over facts, episode summaries and journal entries.
-- 'kind' and 'ref' are unindexed so recall() can tell where a snippet came
-- from without a second query.
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5 (
    text,
    kind UNINDEXED,
    ref UNINDEXED
);
