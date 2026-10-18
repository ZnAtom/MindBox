CREATE TABLE voice_interactions_next (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    origin TEXT NOT NULL CHECK (origin IN ('button','text','door','wake')),
    session_id TEXT REFERENCES automatic_sessions(id),
    audio_session_id TEXT UNIQUE,
    audio_path TEXT,
    state TEXT NOT NULL CHECK (state IN ('queued','waiting_inventory','processing','completed','failed')),
    transcript TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
INSERT INTO voice_interactions_next SELECT * FROM voice_interactions;
DROP TABLE voice_interactions;
ALTER TABLE voice_interactions_next RENAME TO voice_interactions;
CREATE INDEX voice_session ON voice_interactions(session_id, state);
