const { DatabaseSync } = require('node:sqlite');
const path = require('node:path');

const dbPath = process.env.DB_PATH || './xc_training.db';
const db = new DatabaseSync(path.resolve(dbPath));

// WAL mode for better concurrent reads; enforce FK constraints
db.exec('PRAGMA journal_mode = WAL');
db.exec('PRAGMA foreign_keys = ON');

db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS health_samples (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT    NOT NULL,

    -- Dedup key: platform UUID or server-synthesized surrogate (see utils/dedup.js)
    source_uuid      TEXT    NOT NULL,

    type             TEXT    NOT NULL,
    unit             TEXT    NOT NULL,
    value            TEXT    NOT NULL,  -- polymorphic value object, stored as JSON

    -- Denormalized copy of value.numericValue for cheap aggregation
    numeric_value    REAL,

    date_from        TEXT    NOT NULL,
    date_to          TEXT    NOT NULL,

    source_platform  TEXT    NOT NULL,
    source_device_id TEXT,
    source_id        TEXT,
    source_name      TEXT,
    recording_method TEXT    NOT NULL DEFAULT 'unknown',
    device_model     TEXT,

    workout_summary  TEXT,  -- JSON, present for WORKOUT samples only
    metadata         TEXT,  -- JSON, platform-specific extras

    ingested_at      TEXT    NOT NULL DEFAULT (datetime('now')),

    CONSTRAINT uq_health_sample UNIQUE (user_id, source_uuid, type, date_from),
    FOREIGN KEY (user_id) REFERENCES users(id)
  );

  CREATE INDEX IF NOT EXISTS ix_health_samples_user_type_time
    ON health_samples (user_id, type, date_to DESC);

  CREATE INDEX IF NOT EXISTS ix_health_samples_user_time
    ON health_samples (user_id, date_to DESC);
`);

module.exports = db;
