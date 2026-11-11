-- Schema version 1. Applied by the store migrations runner (backlog M1-01).
-- Only the orchestrator process writes this database (ADR-0002).

CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE runs (
  id          TEXT PRIMARY KEY,
  goal        TEXT NOT NULL,
  status      TEXT NOT NULL,
  repo_path   TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  budget_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);

CREATE TABLE tasks (
  id             TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs(id),
  title          TEXT NOT NULL,
  spec_json      TEXT NOT NULL,
  state          TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  worker         TEXT NOT NULL,
  write_set_json TEXT NOT NULL DEFAULT '[]',
  attempts       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX tasks_run_state ON tasks(run_id, state);

CREATE TABLE task_deps (
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  blocker_id TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, blocker_id),
  CHECK (task_id <> blocker_id)
);

CREATE TABLE events (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id        TEXT NOT NULL UNIQUE,
  stream          TEXT NOT NULL,
  stream_seq      INTEGER NOT NULL,
  type            TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1,
  ts              TEXT NOT NULL,
  actor           TEXT NOT NULL,
  causation_id    TEXT,
  correlation_id  TEXT,
  idempotency_key TEXT UNIQUE,
  payload_json    TEXT NOT NULL DEFAULT '{}',
  payload_ref     TEXT,
  UNIQUE (stream, stream_seq)
);
CREATE INDEX events_type ON events(type);

CREATE TABLE documents (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  updated_by   TEXT NOT NULL
);

CREATE TABLE context_pins (
  task_id  TEXT NOT NULL REFERENCES tasks(id),
  doc_id   TEXT NOT NULL REFERENCES documents(id),
  revision INTEGER NOT NULL,
  PRIMARY KEY (task_id, doc_id)
);

CREATE TABLE artifacts (
  hash    TEXT PRIMARY KEY,
  kind    TEXT NOT NULL,
  task_id TEXT,
  path    TEXT NOT NULL,
  bytes   INTEGER NOT NULL
);

CREATE TABLE leases (
  task_id    TEXT PRIMARY KEY REFERENCES tasks(id),
  holder     TEXT NOT NULL,
  pid        INTEGER,
  attempt    INTEGER NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE approvals (
  id          TEXT PRIMARY KEY,
  task_id     TEXT REFERENCES tasks(id),
  action_json TEXT NOT NULL,
  decision    TEXT,
  decided_by  TEXT,
  ts          TEXT NOT NULL
);

CREATE TABLE outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  kind            TEXT NOT NULL,
  payload_json    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  attempts        INTEGER NOT NULL DEFAULT 0
);
