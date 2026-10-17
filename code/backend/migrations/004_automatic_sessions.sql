CREATE TABLE automatic_sessions (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    state TEXT NOT NULL CHECK (state IN ('capturing','queued','processing','committed','pending','failed','dismissed')),
    root TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX automatic_sessions_queue ON automatic_sessions(state, created_at);
