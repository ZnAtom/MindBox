CREATE TABLE cabinets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    inventory_revision INTEGER NOT NULL DEFAULT 0 CHECK (inventory_revision >= 0),
    inventory_known INTEGER NOT NULL DEFAULT 0 CHECK (inventory_known IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE locations (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    label TEXT NOT NULL,
    UNIQUE (id, cabinet_id)
);
CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    role TEXT NOT NULL CHECK (role IN ('camera', 'sensor', 'voice')),
    credential_hash TEXT,
    last_seen_at TEXT,
    UNIQUE (id, cabinet_id)
);
CREATE TABLE events (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    boot_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('door.opened', 'door.closed', 'heartbeat')),
    device_time TEXT,
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    body_hash TEXT NOT NULL,
    UNIQUE (device_id, boot_id, seq)
);
CREATE TABLE media_captures (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    input_sha256 TEXT NOT NULL,
    source_name TEXT NOT NULL,
    captured_at TEXT,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    UNIQUE (id, cabinet_id)
);
CREATE TABLE media_segments (
    id TEXT PRIMARY KEY,
    capture_id TEXT NOT NULL REFERENCES media_captures(id),
    segment_no INTEGER NOT NULL CHECK (segment_no >= 0),
    source_uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    UNIQUE (capture_id, segment_no)
);
CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    capture_id TEXT NOT NULL,
    operation_no INTEGER NOT NULL CHECK (operation_no > 0),
    current_attempt INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL CHECK (state IN ('ready', 'pending', 'failed', 'committed')),
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE (cabinet_id, operation_no),
    UNIQUE (id, cabinet_id),
    FOREIGN KEY (capture_id, cabinet_id) REFERENCES media_captures(id, cabinet_id)
);
CREATE TABLE processing_runs (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    source_key TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    base_revision INTEGER NOT NULL CHECK (base_revision >= 0),
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'running')),
    analysis_status TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    created_at TEXT NOT NULL,
    UNIQUE (operation_id, attempt_no),
    UNIQUE (operation_id, source_key),
    UNIQUE (id, operation_id)
);
CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES processing_runs(id),
    local_ref TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('frame', 'crop')),
    asset_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    media_time_ms INTEGER NOT NULL CHECK (media_time_ms >= 0),
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    UNIQUE (run_id, local_ref),
    UNIQUE (id, run_id)
);
CREATE TABLE candidates (
    run_id TEXT NOT NULL REFERENCES processing_runs(id),
    candidate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    PRIMARY KEY (run_id, candidate_id)
);
CREATE TABLE pending_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES processing_runs(id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    location_id TEXT,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    category TEXT NOT NULL,
    presence_status TEXT NOT NULL CHECK (presence_status IN ('present', 'unseen', 'removed', 'uncertain')),
    version INTEGER NOT NULL CHECK (version > 0),
    data_json TEXT NOT NULL CHECK (json_valid(data_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (id, cabinet_id),
    FOREIGN KEY (location_id, cabinet_id) REFERENCES locations(id, cabinet_id)
);
CREATE TABLE item_observations (
    item_id TEXT NOT NULL REFERENCES items(id),
    observation_id TEXT NOT NULL REFERENCES observations(id),
    PRIMARY KEY (item_id, observation_id)
);
CREATE TABLE storage_visits (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id),
    entered_operation_id TEXT REFERENCES operations(id),
    exited_operation_id TEXT REFERENCES operations(id),
    entered_at TEXT,
    exited_at TEXT,
    entry_evidence_json TEXT NOT NULL CHECK (json_valid(entry_evidence_json)),
    exit_evidence_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(exit_evidence_json)),
    closed INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0, 1))
);
CREATE UNIQUE INDEX one_open_visit ON storage_visits(item_id) WHERE closed = 0;
CREATE TABLE inventory_commits (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    operation_id TEXT UNIQUE,
    run_id TEXT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    idempotency_key TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json)),
    created_at TEXT NOT NULL,
    UNIQUE (cabinet_id, revision),
    UNIQUE (cabinet_id, idempotency_key),
    FOREIGN KEY (operation_id, cabinet_id) REFERENCES operations(id, cabinet_id),
    FOREIGN KEY (run_id, operation_id) REFERENCES processing_runs(id, operation_id)
);
CREATE TABLE inventory_changes (
    id TEXT PRIMARY KEY,
    commit_id TEXT NOT NULL REFERENCES inventory_commits(id),
    item_id TEXT NOT NULL REFERENCES items(id),
    action TEXT NOT NULL CHECK (action IN ('add', 'remove', 'return', 'update', 'keep')),
    before_json TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
    after_json TEXT NOT NULL CHECK (json_valid(after_json)),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    change_index INTEGER NOT NULL,
    UNIQUE (commit_id, change_index)
);
CREATE TABLE verified_events (
    id TEXT PRIMARY KEY,
    commit_id TEXT NOT NULL REFERENCES inventory_commits(id),
    item_id TEXT NOT NULL REFERENCES items(id),
    event_index INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    UNIQUE (commit_id, event_index, item_id)
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    business_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    next_run_at TEXT NOT NULL,
    lease_token TEXT,
    lease_expires_at TEXT,
    last_error TEXT
);
CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    item_id TEXT,
    message TEXT NOT NULL,
    due_at TEXT NOT NULL,
    channels_json TEXT NOT NULL CHECK (json_valid(channels_json)),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'triggered', 'canceled', 'missed')),
    idempotency_key TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    triggered_at TEXT,
    UNIQUE (cabinet_id, idempotency_key),
    FOREIGN KEY (item_id, cabinet_id) REFERENCES items(id, cabinet_id)
);
CREATE TABLE deliveries (
    id TEXT PRIMARY KEY,
    reminder_id TEXT NOT NULL REFERENCES reminders(id),
    channel TEXT NOT NULL CHECK (channel IN ('web', 'voice')),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'received', 'played', 'failed', 'canceled')),
    received_at TEXT,
    played_at TEXT,
    UNIQUE (reminder_id, channel)
);
CREATE INDEX events_device_time ON events(device_id, received_at);
CREATE INDEX observations_run_time ON observations(run_id, media_time_ms);
CREATE INDEX items_search ON items(cabinet_id, presence_status, location_id);
CREATE INDEX pending_run ON pending_candidates(run_id, resolved_at);
CREATE INDEX jobs_due ON jobs(state, next_run_at);
CREATE INDEX reminders_due ON reminders(status, due_at);
