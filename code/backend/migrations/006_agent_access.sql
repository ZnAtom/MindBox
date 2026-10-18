CREATE TABLE api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
    secret_hash TEXT NOT NULL,
    scopes_json TEXT NOT NULL CHECK (json_valid(scopes_json)),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);
CREATE TABLE api_key_cabinets (
    key_id TEXT NOT NULL REFERENCES api_keys(id),
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    PRIMARY KEY (key_id, cabinet_id)
);
CREATE TABLE api_owner_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('pairing', 'session')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE TABLE api_access_logs (
    id TEXT PRIMARY KEY,
    key_id TEXT REFERENCES api_keys(id),
    route_template TEXT NOT NULL,
    cabinet_id TEXT,
    status_code INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX api_access_logs_created ON api_access_logs(created_at);
CREATE INDEX api_access_logs_key ON api_access_logs(key_id, created_at);
CREATE INDEX api_owner_tokens_expiry ON api_owner_tokens(expires_at);
