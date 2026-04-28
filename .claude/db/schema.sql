-- Code analysis pipeline schema
-- Natural keys use (file_path, qualified_name, kind); never line numbers.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at   TEXT,
    mode          TEXT NOT NULL CHECK (mode IN ('diff', 'worktree')),
    base_ref      TEXT,
    head_ref      TEXT,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'analyzed', 'finished', 'failed'))
);

CREATE TABLE IF NOT EXISTS symbols (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('func', 'class', 'method')),
    parent_id       INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    signature       TEXT,
    content_hash    TEXT NOT NULL,
    line_start      INTEGER,
    line_end        INTEGER,
    cyclomatic      INTEGER DEFAULT 1,
    loc             INTEGER DEFAULT 0,
    fan_in          INTEGER DEFAULT 0,
    fan_out         INTEGER DEFAULT 0,
    is_public       INTEGER DEFAULT 1,
    has_docstring   INTEGER DEFAULT 0,
    UNIQUE (file_path, qualified_name, kind)
);

CREATE TABLE IF NOT EXISTS dependencies (
    from_symbol_id  INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    to_symbol_id    INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    to_external     TEXT,
    kind            TEXT NOT NULL CHECK (kind IN ('call', 'import', 'inherit')),
    PRIMARY KEY (from_symbol_id, to_symbol_id, to_external, kind)
);

CREATE TABLE IF NOT EXISTS coverage (
    symbol_id       INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    test_symbol_id  INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    PRIMARY KEY (symbol_id, test_symbol_id)
);

CREATE TABLE IF NOT EXISTS git_blame (
    symbol_id        INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    last_commit      TEXT,
    last_author      TEXT,
    last_touched_at  TEXT
);

CREATE TABLE IF NOT EXISTS modifications (
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    symbol_id     INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    file_path     TEXT NOT NULL,
    qualified_name TEXT,
    change_type   TEXT NOT NULL CHECK (change_type IN ('added', 'modified', 'removed', 'moved')),
    old_hash      TEXT,
    new_hash      TEXT,
    PRIMARY KEY (run_id, file_path, qualified_name, change_type)
);

CREATE TABLE IF NOT EXISTS findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    agent        TEXT NOT NULL,
    symbol_id    INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'error', 'critical')),
    category     TEXT NOT NULL,
    message      TEXT NOT NULL,
    evidence     TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    run_id    INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    decision  TEXT NOT NULL CHECK (decision IN ('approve', 'warn', 'block')),
    score     INTEGER NOT NULL,
    summary   TEXT
);

CREATE TABLE IF NOT EXISTS run_artifacts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_lookup ON symbols(file_path, qualified_name);
CREATE INDEX IF NOT EXISTS idx_findings_run_agent ON findings(run_id, agent);
CREATE INDEX IF NOT EXISTS idx_dependencies_from ON dependencies(from_symbol_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_to ON dependencies(to_symbol_id);
CREATE INDEX IF NOT EXISTS idx_modifications_run ON modifications(run_id);
