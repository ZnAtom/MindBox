CREATE TABLE voice_interactions (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    origin TEXT NOT NULL CHECK (origin IN ('button','text','door')),
    session_id TEXT REFERENCES automatic_sessions(id),
    audio_session_id TEXT UNIQUE,
    audio_path TEXT,
    state TEXT NOT NULL CHECK (state IN ('waiting_inventory','processing','completed','failed')),
    transcript TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE live_reminder_series (
    id TEXT PRIMARY KEY REFERENCES reminders(id),
    current_id TEXT NOT NULL UNIQUE REFERENCES reminders(id),
    timezone TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1))
);
CREATE INDEX voice_session ON voice_interactions(session_id, state);
